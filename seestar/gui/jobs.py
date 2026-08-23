"""JobRegistry: the run lifecycle. No HTTP, no HTML (spec §3).

The structural fix for the 2026-08-12 incident class: jobs live in the SERVER
process, not a browser page session. Progress is STATE, not a stream — any
client may ask for the current snapshot at any time, so reattach-after-reload
is the trivial case instead of the broken one. All mutation happens under one
lock; readers get deep-copied snapshots, never references the worker can still
mutate. The completed job stays readable until the next job starts.
"""

from __future__ import annotations

import copy
import os
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace

from . import views


@dataclass(frozen=True)
class JobRequest:
    library_dir: str
    which: str
    profile: str
    output_root: str
    use_recipe: bool = False


@dataclass(frozen=True)
class EngineHooks:
    """Engine + validation callables injected by the composition root.

    process_target must catch its own per-target failures and return them in
    the entry's `error` field; an exception escaping it is a JOB-level failure
    (state 'failed' — the batch runner itself broke).

    The registry takes ownership of each returned entry's `images` list and
    mutates each image dict IN PLACE: `images[*]["path"]` is popped (the
    absolute path never appears in a snapshot) and a flat `k` index is added
    (`images[*]["k"]`). Every value in the entry must be `copy.deepcopy`-able
    and JSON-safe — snapshots deep-copy the whole job under the registry lock.
    """

    discover: Callable[[str], dict]
    validate: Callable[[JobRequest], list]
    process_target: Callable[[dict, JobRequest, Callable], dict]


class StartRefused(Exception):
    """A render request that must not create a job. Conflict refusals map to 409;
    busy conflicts carry the active snapshot so another page can attach."""

    def __init__(self, message: str, *, conflict: bool = False, active_snapshot=None):
        super().__init__(message)
        self.message = message
        self.conflict = conflict
        self.active_snapshot = active_snapshot


class _Job:
    """Mutable job record. Every field is guarded by JobRegistry._lock."""

    def __init__(self, request: JobRequest, targets: list):
        self.id = uuid.uuid4().hex
        self.created_at = time.time()
        self.request = request
        self.targets = targets
        self.state = "running"  # running | done | failed
        self.seq = 0
        self.progress = {
            "target": "",
            "target_i": 0,
            "target_n": len(targets),
            "stage": "Starting…",
            "tool": "",
            "step_i": 0,
            "step_n": 0,
        }
        self.started_monotonic = time.monotonic()
        # v1.0 parity: the status line's elapsed clock and the busy message's age
        # are PER-TARGET (gui._render_impl reset t0 per target, gui.py:518-521).
        self.target_started_monotonic = self.started_monotonic
        self.results: list = []
        self.image_paths: list = []  # flat; index == the /results/{id}/{k} key
        self.summary = None
        self.technical = None  # job-level failure traceback
        self.stop_requested = False
        self.skipped: list = []


class JobRegistry:
    def __init__(self, hooks: EngineHooks):
        self._hooks = hooks
        self._lock = threading.RLock()
        self._job: _Job | None = None  # the active OR most recent job
        self._thread: threading.Thread | None = None
        self._accepting = True

    # ---- queries --------------------------------------------------------

    def active(self) -> bool:
        with self._lock:
            return self._job is not None and self._job.state == "running"

    def snapshot(self, job_id: str | None = None) -> dict | None:
        with self._lock:
            job = self._job
            if job is None or (job_id is not None and job.id != job_id):
                return None
            return self._snapshot_locked(job)

    def image_path(self, job_id: str, k: int) -> str | None:
        """The realpath recorded at result time, or None. The server re-verifies
        at request time (spec §8 symlink-swap TOCTOU)."""
        with self._lock:
            job = self._job
            if job is None or job.id != job_id or not (0 <= k < len(job.image_paths)):
                return None
            return job.image_paths[k]

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # ---- lifecycle ------------------------------------------------------

    @staticmethod
    def _normalize_request(request: JobRequest) -> JobRequest:
        def path(value: str) -> str:
            value = value.strip()
            return os.path.realpath(os.path.abspath(os.path.expanduser(value))) if value else ""

        return replace(
            request,
            library_dir=path(request.library_dir),
            which=request.which.strip(),
            profile=request.profile.strip(),
            output_root=path(request.output_root),
        )

    def start(self, request: JobRequest) -> dict:
        """Validate, resolve `which` against a FRESH discovery snapshot, and
        atomically claim the single active slot. Validation failures never
        create a job (spec §4)."""
        request = self._normalize_request(request)
        errors = self._hooks.validate(request)
        if errors:
            raise StartRefused(errors[0])
        with self._lock:
            if not self._accepting:
                # Load-bearing, not redundant with the post-discovery check below:
                # a start() call made after shutdown has already closed admission
                # must never invoke self._hooks.discover(...) again. Deleting this
                # short-circuit makes
                # test_render_start_and_shutdown_are_one_atomic_admission_protocol
                # hang ~10s (BrokenBarrierError) instead of failing cleanly, because
                # that test's discover stub is gated by a single-use Barrier with
                # no thread left to rendezvous with on a second call.
                raise StartRefused(views.MSG_SHUTTING_DOWN, conflict=True)
        discovery = self._hooks.discover(request.library_dir)
        targets = discovery["targets"]
        if request.which != views.ALL_TARGETS:
            targets = [t for t in targets if t["target"] == request.which]
        if not targets:
            raise StartRefused(views.MSG_NO_MATCH)
        with self._lock:
            if not self._accepting:
                raise StartRefused(views.MSG_SHUTTING_DOWN, conflict=True)
            if self._job is not None and self._job.state == "running":
                running = self._job
                elapsed = time.monotonic() - running.target_started_monotonic
                active = self._snapshot_locked(running)
                raise StartRefused(
                    views.busy_message(running.progress.get("target") or running.request.which, elapsed),
                    conflict=True,
                    active_snapshot=active,
                )
            job = _Job(request, targets)
            self._job = job
            thread = threading.Thread(target=self._worker, args=(job,), daemon=True)
            self._thread = thread
            snap = self._snapshot_locked(job)
        try:
            thread.start()
        except BaseException:
            with self._lock:
                if self._job is job:
                    self._job = None
                    self._thread = None
            raise
        return snap

    def begin_shutdown(self) -> tuple[bool, dict | None]:
        """Atomically refuse shutdown beside active work or close admission while idle.

        Idempotent after admission is already closed. This and start() share the
        same lock, so render-start and shutdown cannot both be accepted.
        """
        with self._lock:
            if self._job is not None and self._job.state == "running":
                return False, self._snapshot_locked(self._job)
            self._accepting = False
            return True, None

    def request_stop(self, job_id: str) -> tuple:
        """Batch-boundary graceful stop (spec §4): only a running multi-target
        job has a boundary; the in-flight target always completes."""
        with self._lock:
            job = self._job
            has_future_boundary = (
                job is not None
                and job.id == job_id
                and job.state == "running"
                and 0 < job.progress["target_i"] < len(job.targets)
            )
            if not has_future_boundary:
                return False, views.MSG_STOP_UNAVAILABLE
            if not job.stop_requested:
                job.stop_requested = True
                job.seq += 1
            return True, "Photon Finish will finish the current photo, then stop."

    # ---- worker ---------------------------------------------------------

    def _worker(self, job: _Job) -> None:
        done: list = []
        failed: list = []
        try:
            for i, target in enumerate(job.targets, 1):
                with self._lock:
                    if job.stop_requested:
                        job.skipped = [t["target"] for t in job.targets[i - 1 :]]
                        job.seq += 1
                        break
                    job.progress.update(
                        target=target["target"], target_i=i, stage="Starting…", tool="", step_i=0, step_n=0
                    )
                    job.target_started_monotonic = time.monotonic()
                    job.seq += 1

                def on_stage(key, label, tool, step_i, step_n, _job=job):
                    with self._lock:
                        _job.progress.update(stage=label, tool=tool, step_i=step_i, step_n=step_n)
                        _job.seq += 1

                entry = self._hooks.process_target(target, job.request, on_stage)
                with self._lock:
                    for image in entry.get("images", []):
                        image["k"] = len(job.image_paths)
                        job.image_paths.append(os.path.realpath(image.pop("path")))
                    job.results.append(entry)
                    (done if entry.get("ok") else failed).append(entry["target"])
                    job.seq += 1
            try:
                summary = views.summary_line(done, failed, job.skipped, job.request.profile, job.request.output_root)
            except Exception:
                summary = views.job_failed_line([])
            with self._lock:
                job.summary = summary
                job.state = "done"
                job.seq += 1
        except Exception:
            # Terminality first: state/technical must land even if the summary
            # formatting below raises (e.g. a hook-contract violation like a
            # non-string target name) — otherwise the worker dies leaving
            # state == "running" forever and the registry wedges.
            with self._lock:
                job.technical = traceback.format_exc()
                job.state = "failed"
                try:
                    job.summary = views.job_failed_line(done)
                except Exception:
                    job.summary = views.job_failed_line([])
                job.seq += 1
        finally:
            with self._lock:
                if job.state == "running":  # belt-and-braces: exactly one terminal state, always
                    # Reaches here on a BaseException (e.g. SystemExit) escaping
                    # the try/except above, since that isn't caught by `except
                    # Exception`. State and technical must still be set — the
                    # "Review the technical details below" copy must not lie.
                    job.state = "failed"
                    job.technical = job.technical or traceback.format_exc()
                    try:
                        job.summary = job.summary or views.job_failed_line(done)
                    except Exception:
                        job.summary = job.summary or views.job_failed_line([])
                    job.seq += 1

    # ---- snapshot -------------------------------------------------------

    def _snapshot_locked(self, job: _Job) -> dict:
        p = job.progress
        status = None
        if job.state == "running":
            elapsed = time.monotonic() - job.target_started_monotonic
            status = views.status_text(
                p["target"] or job.request.which,
                p["target_i"] or 1,
                p["target_n"],
                p["stage"],
                p["tool"],
                p["step_i"],
                p["step_n"],
                elapsed,
            )
        return copy.deepcopy(
            {
                "id": job.id,
                "created_at": job.created_at,
                "state": job.state,
                "seq": job.seq,
                "request": asdict(job.request),
                "progress": p,
                "status_text": status,
                "results": job.results,
                "summary": job.summary,
                "technical": job.technical,
                "stop": {
                    "requested": job.stop_requested,
                    "available": (
                        job.state == "running"
                        and 0 < job.progress["target_i"] < len(job.targets)
                        and not job.stop_requested
                    ),
                },
                "skipped": job.skipped,
            }
        )
