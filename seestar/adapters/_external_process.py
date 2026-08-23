"""Bounded external-process runner (H1 foundation, Task 3).

The four adapters (Siril, GraXpert, StarNet2, exiftool) shell out via
`subprocess.run` with no wall-clock bound today: a hung or slow tool call hangs
the whole pipeline run forever. This module adds a drop-in replacement,
`run_external`, that preserves every observable behavior of the current
`subprocess.run` calls (stdout/stderr capture, text/encoding/errors, `check=`)
while enforcing an absolute wall-clock deadline per settled decision 3
(`tools.timeouts.for_tool(tool)`, no idle-output or hardware-based timeout --
settled decision 4) and killing the WHOLE process tree it launched on timeout,
never a global name-based process scan (settled decision 6).

Task 4 (out of scope here) routes the four adapters through this runner. This
task builds the foundation only.

Darwin/POSIX semantics (experimentally confirmed on this Mac, darwin 25.5 --
see docs/superpowers/sdd for the verification notes):
  - `start_new_session=True` gives the launched child its own process group
    (child PGID == child PID);
  - `os.killpg` on that group reaches grandchildren the child spawned itself
    (all group members signalled); signalling only the direct child orphans
    them -- the motivating defect a plain `subprocess.run(timeout=...)` has;
  - `os.killpg` after the group has already exited raises
    `ProcessLookupError` -- a real race between the timeout firing and the
    child's natural exit, not a theoretical one. Every `killpg` call below
    catches it. The post-grace-period liveness check (`killpg(pgid, 0)`)
    also catches `PermissionError`: confirmed on a macOS CI runner (GitHub
    Actions run 30768964744, 2026-08-02) that the same already-exited-group
    race can surface as EPERM instead of ESRCH there.
  - PID reuse is guarded by only ever signalling while the `Popen` handle is
    unreaped (`proc.poll() is None`).

Windows: a new process group at spawn (`CREATE_NEW_PROCESS_GROUP`) plus a
bounded `taskkill /PID <pid> /T /F` (kills the whole tree by PID, not by
name), with a direct-child `Popen.kill()` fallback if the tree survives that.
This path cannot be exercised on a real Windows host from this repo; its
tree-walk logic is unit-tested against a fake `Popen` (see
`tests/test_external_process.py`).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from typing import TYPE_CHECKING

from .base import AdapterError

if TYPE_CHECKING:
    from .base import ToolPaths

# Bounded grace period between a first termination attempt (SIGTERM / taskkill)
# and a forced kill (SIGKILL / direct Popen.kill()). Deliberately a module-level
# name (not a function default) so tests can monkeypatch it down for fast
# unit tests of the platform-specific tree-walk logic.
_GRACE_PERIOD_S = 5.0

# Never used to identify the process by name -- only for the user-facing
# timeout message (settled decision 6: PID-scoped kill, never a name-based scan).
_DISPLAY_NAMES = {
    "siril": "Siril",
    "graxpert": "GraXpert",
    "starnet2": "StarNet2",
    "exiftool": "exiftool",
}


# --- Diagnosis 7: actionable failure summaries + a bounded diagnostic log -----
#
# What a failing tool prints is unbounded and front-loaded with banners, so the
# old first-200-character truncation reliably cut off the one line that said what
# went wrong. Three bounds replace it: a summary that keeps the ACTIONABLE tail,
# a message bound so a novice-facing surface stays readable, and a capped
# on-disk log holding the full output for later diagnosis.
SUMMARY_LIMIT = 400
MESSAGE_LIMIT = 700
LOG_CAP_BYTES = 256 * 1024
DIAGNOSTIC_LOG_NAME = "seestar_diagnostics.log"

_ACTIONABLE = re.compile(
    r"\b(error|failed|failure|cannot|can't|unable|no such|not found|invalid|exception|denied|refused|timed out)\b",
    re.IGNORECASE,
)


def summarize_tool_output(text: str, limit: int = SUMMARY_LIMIT) -> str:
    """A bounded, useful summary of a failed tool's output.

    Prefers the LAST few lines that read like a diagnosis; falls back to the tail
    when nothing matches, because a tool that dies without an error line still
    fails at the end, never at the banner. Never the first N characters -- that
    was the defect (Diagnosis 7)."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return "(the tool produced no output)"
    picked = [ln for ln in lines if _ACTIONABLE.search(ln)][-3:] or lines[-3:]
    summary = " | ".join(picked)
    if len(summary) > limit:
        summary = "…" + summary[-(limit - 1) :]
    return summary


def write_diagnostic_log(
    dir_path: str | None, *, tool: str, stage: str | None, text: str
) -> tuple[str | None, str | None]:
    """Append a failure's FULL output to the run's diagnostic log; return
    (path, warning).

    The log is diagnostic state, not a publication member: nothing references it,
    a later run may overwrite it, and callers must treat a failure to write it as
    a WARNING beside the original tool error, never as a replacement for it
    (CHANGE_SAFETY rule 5 -- a concealed secondary failure is worse than a loud
    one). `dir_path=None` (a call site with no run directory, e.g. `seestar
    inspect`) is not an error and produces no warning.

    Size is bounded by keeping the most recent `LOG_CAP_BYTES` and saying so, so
    a run that fails repeatedly cannot fill a disk and the newest failure -- the
    one being diagnosed -- is always the part retained."""
    if not dir_path:
        return None, None
    path = os.path.join(dir_path, DIAGNOSTIC_LOG_NAME)
    header = f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} {tool}" + (f" / {stage}" if stage else "") + " =====\n"
    note = "[... earlier diagnostics truncated to keep this log bounded ...]\n"
    # Truncate THIS entry first, keeping its header and the tail of the output:
    # a headerless block of text says nothing about which tool produced it, and
    # the tail is where a tool says why it stopped.
    body = text or ""
    entry_room = LOG_CAP_BYTES // 2 - len(header.encode("utf-8")) - len(note.encode("utf-8"))
    if len(body.encode("utf-8")) > entry_room:
        body = note + body.encode("utf-8")[-entry_room:].decode("utf-8", errors="replace")
    entry = header + body + "\n"
    try:
        existing = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                existing = f.read()
        content = existing + entry
        if len(content.encode("utf-8")) > LOG_CAP_BYTES:
            # drop the OLDEST entries; the newest one is already bounded above, so
            # it always survives whole, header included
            keep = LOG_CAP_BYTES - len(note.encode("utf-8"))
            content = note + content.encode("utf-8")[-keep:].decode("utf-8", errors="replace")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path, None
    except OSError as e:
        return None, f"could not write the diagnostic log ({type(e).__name__}); the tool error below still stands"


class ExternalToolFailure(AdapterError):
    """One typed external-tool failure (Diagnosis 7).

    Carries the tool, the pipeline stage (attached at the executor boundary,
    where the operation is known -- see `with_stage`), a failure class, the exit
    status or configured timeout, a BOUNDED safe summary, and where the full
    output was written. `__str__` is the novice-safe surface: CLI and GUI show it
    unmodified rather than replacing it with generic copy.

    Deliberately carries no argv, environment, or input paths: those can contain
    user file paths, and the bounded log is where the full detail belongs."""

    def __init__(
        self,
        *,
        tool: str,
        failure_class: str,
        summary: str,
        stage: str | None = None,
        exit_status: int | None = None,
        timeout_s: float | None = None,
        log_path: str | None = None,
        guidance: str | None = None,
        full_output: str | None = None,
    ) -> None:
        self.tool = tool
        self.failure_class = failure_class
        self.summary = summary
        self.stage = stage
        self.exit_status = exit_status
        self.timeout_s = timeout_s
        self.log_path = log_path
        self.guidance = guidance
        # The tool's COMPLETE output, for local classification only (e.g. Siril's
        # PCC failure class). Never enters the message: `str(self)` reaches EXIF
        # and novice-facing surfaces, and raw tool output echoes local file paths.
        self.full_output = full_output
        super().__init__(self._build_message())

    def _build_message(self) -> str:
        display = _DISPLAY_NAMES.get(self.tool, self.tool)
        during = f" while {self.stage}" if self.stage else ""
        status = f" (exit status {self.exit_status})" if self.exit_status is not None else ""
        tail = " No source photo was changed."
        if self.guidance:
            tail += f" {self.guidance}"
        if self.log_path:
            tail += f" Full output: {self.log_path}"
        head = f"{display} failed{during}{status}. "
        room = MESSAGE_LIMIT - len(head) - len(tail)
        summary = self.summary if len(self.summary) <= room else "…" + self.summary[-(room - 1) :]
        return head + summary + tail

    def with_stage(self, stage: str) -> ExternalToolFailure:
        """A NEW failure with the stage filled in (never a mutation: the original
        may already be chained as another exception's `__cause__`)."""
        return ExternalToolFailure(
            tool=self.tool,
            failure_class=self.failure_class,
            summary=self.summary,
            stage=self.stage or stage,
            exit_status=self.exit_status,
            timeout_s=self.timeout_s,
            log_path=self.log_path,
            guidance=self.guidance,
            full_output=self.full_output,
        )


class ExternalToolTimeout(AdapterError):
    """Raised when an external tool exceeds its configured wall-clock deadline.

    The message intentionally carries only the tool's display name, the
    configured duration, and recovery guidance -- never argv, captured
    output, environment, or input paths (those can contain user file paths
    and are not safe to surface in a bare exception string). `__str__` (via
    the message built by `_timeout_message`) is itself the novice-safe,
    actionable surface -- GUI/CLI error handling should show it unmodified
    rather than replacing it with generic copy (H1 Task 4)."""


def _format_duration(seconds: float) -> str:
    """Novice-facing duration for the timeout message: whole minutes when the
    configured timeout is at least a minute (the settled defaults -- 30/60/2
    minutes -- always land here), otherwise plain seconds so a short test
    timeout (e.g. 0.15s) still reads sensibly instead of "0 minutes"."""
    if seconds >= 60:
        minutes = round(seconds / 60.0)
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds:g} second{'s' if seconds != 1 else ''}"


def _timeout_message(tool: str, timeout_s: float) -> str:
    """Novice-safe, actionable timeout message -- tool name, configured
    duration (both a friendly phrase and the raw seconds value), and recovery
    guidance; never argv, captured output, environment, or input paths.
    GraXpert alone gets first-run-model-setup guidance (its models download and
    cache on first CLI invocation); Siril/StarNet2/exiftool get a generic
    retry/check-installation message. A standalone function (not inlined into
    `run_external`) so tests can assert its exact wording without spawning a
    real slow process."""
    display = _DISPLAY_NAMES.get(tool, tool)
    duration = _format_duration(timeout_s)
    if tool == "graxpert":
        guidance = "If this was GraXpert's first run, open GraXpert once to finish its model setup, then try again."
    else:
        guidance = "Try again, and check that the tool is installed correctly if this keeps happening."
    return (
        f"{display} did not finish within {duration} (configured timeout {timeout_s:g}s), "
        f"so Photon Finish stopped this photo. No source photo was changed. {guidance}"
    )


def _pgid_of(proc: subprocess.Popen) -> int | None:
    import os

    try:
        return os.getpgid(proc.pid)
    except ProcessLookupError:
        return None


def _terminate_process_tree_posix(proc: subprocess.Popen) -> str | None:
    """TERM the process group, wait a bounded grace period, then KILL any
    survivor. Returns a short, safe cleanup-status note if cleanup itself
    struggled, else None.

    The pgid is captured ONCE, at entry, while the direct child is still
    unreaped (`proc.poll() is None`) -- never re-derived from `proc.pid` later,
    because once the direct child is reaped its PID could be reused by an
    unrelated process, and `os.getpgid` on a reused PID would return that
    unrelated process's group.

    The escalation gate (whether to send SIGKILL) checks whether the
    PROCESS GROUP is still alive via `os.killpg(pgid, 0)` -- NOT whether the
    direct child is still alive via `proc.poll()`. A direct child with the
    default SIGTERM handler can die almost immediately once the group is
    TERM'd, while a grandchild that installs SIG_IGN for SIGTERM survives and
    can only be reaped by the follow-up SIGKILL; gating on `proc.poll()` alone
    would see the (already-reaped) direct child as "gone" and skip the KILL
    entirely, leaking the surviving grandchild (the confirmed regression this
    fixes). A pgid is not recycled while any process in the group remains, so
    `killpg(pgid, 0)` is a safe existence check for the bounded grace window.

    Every signal -- SIGTERM, the `killpg(pgid, 0)` liveness probe, and
    SIGKILL -- catches `ProcessLookupError` (the group may exit naturally
    between the timeout firing and the signal -- a confirmed real race, not
    theoretical). The liveness probe additionally catches `PermissionError`:
    a confirmed macOS kernel race (EPERM instead of ESRCH from an
    already-exited group) -- see module docstring."""
    import os
    import signal

    pgid = _pgid_of(proc) if proc.poll() is None else None
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + _GRACE_PERIOD_S
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)

    group_alive = False
    if pgid is not None:
        try:
            os.killpg(pgid, 0)  # signal 0: existence check only, no delivery
            group_alive = True
        except ProcessLookupError:
            group_alive = False
        except PermissionError:
            # Confirmed on a macOS CI runner (GitHub Actions run 30768964744,
            # 2026-08-02): killpg(pgid, 0) can raise EPERM instead of ESRCH
            # when the group has fully exited but the kernel hasn't finished
            # reclaiming the pgid. None of the four tools this runner
            # launches (Siril/GraXpert/StarNet2/exiftool) ever drop
            # privileges, so at THIS specific check an EPERM means "gone",
            # not "live and unreachable" -- treat it the same as
            # ProcessLookupError rather than letting it propagate and crash
            # cleanup (which would mask the original exception run_external
            # is already unwinding for).
            group_alive = False

    if group_alive:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=_GRACE_PERIOD_S)
        except subprocess.TimeoutExpired:
            return "cleanup: process group did not exit after SIGKILL"
        return None

    # Group is fully gone -- reap the direct child (a no-op if the wait loop
    # above already reaped it; Popen.wait() returns immediately once the
    # return code is already known).
    try:
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        return "cleanup: direct child did not reap promptly"
    return None


def _terminate_process_tree_windows(proc: subprocess.Popen) -> str | None:
    """Bounded `taskkill /PID <pid> /T /F` (kills the whole tree rooted at the
    launched process, by PID -- never a name-based scan), with a direct-child
    `Popen.kill()` fallback if the tree survives that call. Returns a short,
    safe cleanup-status note if cleanup struggled, else None."""
    if proc.poll() is None:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=_GRACE_PERIOD_S,
                # getattr, not a bare attribute: this function is Windows-only in
                # production (only ever called from _terminate_process_tree's win
                # branch), but its test exercises it directly on every CI platform,
                # and subprocess.CREATE_NO_WINDOW doesn't exist on Linux/macOS.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (subprocess.TimeoutExpired, OSError):
            pass

    deadline = time.monotonic() + _GRACE_PERIOD_S
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)

    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=_GRACE_PERIOD_S)
        except subprocess.TimeoutExpired:
            return "cleanup: process tree did not exit after taskkill + direct-kill fallback"
        return None

    try:
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        return "cleanup: direct child did not reap promptly"
    return None


def _terminate_process_tree(proc: subprocess.Popen) -> str | None:
    if sys.platform.startswith("win"):
        return _terminate_process_tree_windows(proc)
    return _terminate_process_tree_posix(proc)


def run_external(
    args: list[str],
    *,
    tool: str,
    tools: ToolPaths,
    capture_output: bool = False,
    check: bool = False,
    text: bool = False,
    encoding: str | None = None,
    errors: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """`subprocess.run`-equivalent for the four adapters, with a bounded
    wall-clock deadline (`tools.timeouts.for_tool(tool)`) and whole-process-tree
    cleanup on timeout.

    Preserves `subprocess.run`'s observable behavior: `capture_output` pipes
    stdout/stderr, `text`/`encoding`/`errors` control str-vs-bytes decoding
    exactly as `Popen` does natively (encoding/errors alone imply text mode,
    matching `subprocess.run`), and `check=True` raises `CalledProcessError`
    on a non-zero return code. `env`, when given, replaces the child's whole
    environment (as `subprocess.run` does); None inherits this process's.

    On timeout, raises `ExternalToolTimeout` (chained from the original
    `TimeoutExpired`) after terminating the whole process tree this call
    launched and reaping the direct child -- never leaves a hung or orphaned
    process behind, and never signals a PID this call didn't launch."""
    timeout_s = tools.timeouts.for_tool(tool)

    popen_kwargs: dict = {}
    if capture_output:
        popen_kwargs["stdout"] = subprocess.PIPE
        popen_kwargs["stderr"] = subprocess.PIPE
    if text:
        popen_kwargs["text"] = True
    if encoding is not None:
        popen_kwargs["encoding"] = encoding
    if errors is not None:
        popen_kwargs["errors"] = errors
    if env is not None:
        popen_kwargs["env"] = env

    if sys.platform.startswith("win"):
        # CREATE_NO_WINDOW: a --windowed frozen app has no console of its own, so without
        # this flag Windows auto-allocates a brand new conhost.exe window for every console-
        # subsystem child (Siril/GraXpert/StarNet2/exiftool are all console apps) -- one
        # flashing window per tool call, per render stage. Piped stdout/stderr (capture_output)
        # work identically with or without a console; this only suppresses the window.
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(args, **popen_kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        cleanup_note = _terminate_process_tree(proc)
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
        message = _timeout_message(tool, timeout_s)
        if cleanup_note:
            message += f" ({cleanup_note})"
        raise ExternalToolTimeout(message) from exc
    except BaseException:
        # Any other exception during communicate() -- most importantly
        # KeyboardInterrupt (Ctrl-C). The child runs with start_new_session=True /
        # CREATE_NEW_PROCESS_GROUP so it no longer receives the terminal's SIGINT;
        # without this handler a Ctrl-C here would leave the external tool (and
        # anything it spawned) running detached and unsupervised -- a regression
        # vs. plain `subprocess.run`, which kills its child on any exception. Own
        # every process this call launched: terminate the whole tree, close the
        # pipes, then re-raise the ORIGINAL exception unchanged (never swallowed,
        # never replaced by a synthetic timeout).
        _terminate_process_tree(proc)
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
        raise

    result = subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, args, output=stdout, stderr=stderr)
    return result
