"""JobRegistry pure-logic tests — no HTTP, no engine, no tools (spec §11).

The 2026-08-12 (NGC6992) single-flight regressions from tests/test_gui.py port
over here as registry tests: thread liveness (not page/generator liveness) is
the authority, and a reattaching client is just another snapshot() reader."""

import threading
import time

import pytest

from seestar.gui import views
from seestar.gui.jobs import EngineHooks, JobRegistry, JobRequest, StartRefused

REQ = JobRequest(library_dir="/lib", which="M8", profile="showcase", output_root="/out")
TARGETS = {"targets": [{"target": "M8", "folder": "/lib/M8"}], "skipped": []}


def entry(target, ok=True, images=(), warnings=(), error=None):
    return {
        "target": target,
        "ok": ok,
        "images": list(images),
        "qa": {"heading": "Result check: ✅ Ready to use"},
        "replay": None,
        "warnings": list(warnings),
        "error": error,
    }


def make_registry(process_target, discover=lambda d: TARGETS, validate=lambda r: []):
    return JobRegistry(EngineHooks(discover=discover, validate=validate, process_target=process_target))


def wait_terminal(reg, job_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = reg.snapshot(job_id)
        if snap and snap["state"] != "running":
            return snap
        time.sleep(0.01)
    raise AssertionError("job never reached a terminal state")


def test_validation_failure_never_creates_a_job():
    reg = make_registry(lambda t, r, cb: entry("M8"), validate=lambda r: [views.MSG_CHOOSE_OUTPUT])
    with pytest.raises(StartRefused) as e:
        reg.start(REQ)
    assert e.value.conflict is False and "saved" in e.value.message
    assert reg.snapshot() is None


def test_no_matching_target_refuses():
    reg = make_registry(lambda t, r, cb: entry("M8"))
    with pytest.raises(StartRefused) as e:
        reg.start(JobRequest("/lib", "M99", "showcase", "/out"))
    assert e.value.message == views.MSG_NO_MATCH


def test_accepted_request_is_canonicalized_before_validation_and_discovery(tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    out = tmp_path / "out"
    seen = {}

    def validate(request):
        seen["validated"] = request
        return []

    def discover(path):
        seen["discovered"] = path
        return {"targets": [{"target": "M8", "folder": str(lib / "M8")}], "skipped": []}

    reg = make_registry(lambda t, r, cb: entry("M8"), discover=discover, validate=validate)
    started = reg.start(JobRequest(str(lib / ".." / "library"), " M8 ", " showcase ", str(out / ".." / "out")))
    request = started["request"]
    assert request == {
        "library_dir": str(lib.resolve()),
        "which": "M8",
        "profile": "showcase",
        "output_root": str(out.resolve()),
        "use_recipe": False,
    }
    assert seen["validated"].library_dir == str(lib.resolve())
    assert seen["discovered"] == str(lib.resolve())


def test_single_flight_second_start_busy_and_first_completes():
    release = threading.Event()

    def slow(target, request, cb):
        release.wait(timeout=30)
        return entry(target["target"])

    reg = make_registry(slow)
    snap = reg.start(REQ)
    with pytest.raises(StartRefused) as e:
        reg.start(REQ)
    assert e.value.conflict is True and e.value.active_snapshot["id"] == snap["id"]
    assert "M8" in e.value.message and "reattached" in e.value.message.lower()
    release.set()
    done = wait_terminal(reg, snap["id"])
    assert done["state"] == "done" and done["summary"].startswith("✅ Finished: M8")
    # once finished, a new start is allowed and REPLACES the readable last job
    release.set()
    snap2 = reg.start(REQ)
    assert snap2["id"] != snap["id"]


def test_simultaneous_starts_accept_exactly_one():
    release = threading.Event()
    gate = threading.Barrier(3)

    def slow(target, request, cb):
        release.wait(timeout=30)
        return entry("M8")

    reg = make_registry(slow)
    outcomes = []

    def attempt():
        gate.wait(timeout=10)
        try:
            outcomes.append(reg.start(REQ))
        except StartRefused as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    gate.wait(timeout=10)
    for thread in threads:
        thread.join(timeout=10)
    assert sum(isinstance(item, dict) for item in outcomes) == 1
    refused = next(item for item in outcomes if isinstance(item, StartRefused))
    assert refused.conflict and refused.active_snapshot
    release.set()
    reg.join(timeout=10)


def test_reattach_snapshot_is_readable_without_the_starting_client():
    """The orphan case: the starting client vanished; a NEW reader sees live state."""
    release = threading.Event()

    def slow(target, request, cb):
        cb("stretch", "Stretching", "Siril", 5, 9)
        release.wait(timeout=30)
        return entry(target["target"])

    reg = make_registry(slow)
    started = reg.start(REQ)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        snap = reg.snapshot()  # a fresh page: no job id needed
        if snap["progress"]["step_i"] == 5:
            break
        time.sleep(0.01)
    assert snap["id"] == started["id"] and snap["state"] == "running"
    assert "Stretching (Siril)" in snap["status_text"] and "Step 5 of 9" in snap["status_text"]
    release.set()
    wait_terminal(reg, started["id"])


def test_snapshots_are_copies_not_references():
    release = threading.Event()

    def slow(target, request, cb):
        release.wait(timeout=30)
        return entry(
            target["target"],
            images=[{"path": "/out/M8/M8_final.jpg", "label": "Recommended", "filename": "M8_final.jpg"}],
        )

    reg = make_registry(slow)
    snap = reg.start(REQ)
    snap["progress"]["stage"] = "TAMPERED"
    snap["request"]["which"] = "TAMPERED"
    assert reg.snapshot()["progress"]["stage"] != "TAMPERED"
    assert reg.snapshot()["request"]["which"] == "M8"
    release.set()
    done = wait_terminal(reg, snap["id"])
    done["results"].clear()
    assert reg.snapshot()["results"], "mutating a snapshot must not touch registry state"


def test_partial_batch_failure_is_done_not_failed():
    targets = {"targets": [{"target": "M8", "folder": "a"}, {"target": "M17", "folder": "b"}], "skipped": []}

    def run(target, request, cb):
        if target["target"] == "M17":
            return entry(
                "M17",
                ok=False,
                error={"friendly": "Processing stopped before a new image could be saved.", "technical": "trace"},
            )
        return entry(target["target"])

    reg = make_registry(run, discover=lambda d: targets)
    snap = reg.start(JobRequest("/lib", views.ALL_TARGETS, "showcase", "/out"))
    done = wait_terminal(reg, snap["id"])
    assert done["state"] == "done"
    assert done["summary"].startswith("⚠️ Finished M8.") and "M17" in done["summary"]
    assert [r["ok"] for r in done["results"]] == [True, False]


def test_worker_exception_is_job_level_failure_with_exactly_one_terminal_state():
    def boom(target, request, cb):
        raise RuntimeError("assembly broke")

    reg = make_registry(boom)
    snap = reg.start(REQ)
    done = wait_terminal(reg, snap["id"])
    assert done["state"] == "failed"
    assert "internal problem" in done["summary"]
    assert "assembly broke" in done["technical"]


def test_discover_exception_during_start_raises_not_half_starts():
    def bad_discover(d):
        raise OSError("disk gone")

    reg = make_registry(lambda t, r, cb: entry("M8"), discover=bad_discover)
    with pytest.raises(OSError):
        reg.start(REQ)
    assert reg.snapshot() is None and reg.active() is False


def test_worker_thread_start_failure_rolls_back_the_job(monkeypatch):
    reg = make_registry(lambda t, r, cb: entry("M8"))

    def fail_start(self):
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    with pytest.raises(RuntimeError, match="thread unavailable"):
        reg.start(REQ)
    assert reg.snapshot() is None and reg.active() is False


def test_images_get_stable_flat_indices_and_realpaths(tmp_path):
    img = tmp_path / "M8_final.jpg"
    img.write_bytes(b"jpg")

    def run(target, request, cb):
        return entry(target["target"], images=[{"path": str(img), "label": "Recommended", "filename": "M8_final.jpg"}])

    reg = make_registry(run)
    snap = reg.start(REQ)
    done = wait_terminal(reg, snap["id"])
    k = done["results"][0]["images"][0]["k"]
    assert reg.image_path(done["id"], k) == str(img.resolve())
    assert reg.image_path(done["id"], k + 999) is None
    assert reg.image_path("nope", k) is None
    # The in-place mutation (pop "path", add "k") is the only thing keeping
    # absolute filesystem paths out of client-visible snapshots — assert it.
    assert "path" not in done["results"][0]["images"][0]
    assert set(done["results"][0]["images"][0]) == {"k", "label", "filename"}


def test_graceful_stop_between_targets():
    targets = {
        "targets": [
            {"target": "M8", "folder": "a"},
            {"target": "M17", "folder": "b"},
            {"target": "M20", "folder": "c"},
        ],
        "skipped": [],
    }
    id_ready = threading.Event()  # the worker can outrun the main thread's id assignment
    stopped = threading.Event()
    reg_box = {}

    def run(target, request, cb):
        if target["target"] == "M8":
            assert id_ready.wait(timeout=10)
            ok, msg = reg_box["reg"].request_stop(reg_box["id"])  # user clicks during target 1
            assert ok and "finish the current photo" in msg.lower()
            stopped.set()
        return entry(target["target"])

    reg = make_registry(run, discover=lambda d: targets)
    reg_box["reg"] = reg
    snap = reg.start(JobRequest("/lib", views.ALL_TARGETS, "showcase", "/out"))
    reg_box["id"] = snap["id"]
    id_ready.set()
    assert stopped.wait(timeout=10)
    done = wait_terminal(reg, snap["id"])
    assert [r["target"] for r in done["results"]] == ["M8"]  # in-flight target completed
    assert done["skipped"] == ["M17", "M20"]
    assert done["state"] == "done"
    assert "Stopped before M17, M20 at your request." in done["summary"]


def test_stop_refused_for_single_target_and_when_idle():
    release = threading.Event()

    def slow(target, request, cb):
        release.wait(timeout=30)
        return entry(target["target"])

    reg = make_registry(slow)
    snap = reg.start(REQ)  # single target: no batch boundary exists
    ok, _msg = reg.request_stop(snap["id"])
    assert ok is False
    assert reg.snapshot(snap["id"])["stop"]["available"] is False
    release.set()
    wait_terminal(reg, snap["id"])
    ok, _msg = reg.request_stop(snap["id"])
    assert ok is False  # idle: nothing to stop


def test_stop_not_offered_before_first_target_or_during_last_target():
    release_first = threading.Event()
    release_last = threading.Event()
    targets = {"targets": [{"target": "M8", "folder": "a"}, {"target": "M17", "folder": "b"}], "skipped": []}

    def run(target, request, cb):
        (release_first if target["target"] == "M8" else release_last).wait(timeout=30)
        return entry(target["target"])

    reg = make_registry(run, discover=lambda d: targets)
    snap = reg.start(JobRequest("/lib", views.ALL_TARGETS, "showcase", "/out"))
    # Wait until target 1 actually starts; before that no in-flight photo exists.
    while reg.snapshot(snap["id"])["progress"]["target_i"] == 0:
        time.sleep(0.01)
    assert reg.snapshot(snap["id"])["stop"]["available"] is True
    release_first.set()
    while reg.snapshot(snap["id"])["progress"]["target_i"] < 2:
        time.sleep(0.01)
    assert reg.snapshot(snap["id"])["stop"]["available"] is False
    release_last.set()
    wait_terminal(reg, snap["id"])


def test_render_start_and_shutdown_are_one_atomic_admission_protocol():
    """Exactly one wins: shutdown cannot be accepted beside a newly accepted job."""
    barrier = threading.Barrier(2)

    def discover(_):
        barrier.wait(timeout=10)
        return TARGETS

    release = threading.Event()

    def slow(target, request, cb):
        release.wait(timeout=30)
        return entry("M8")

    reg = make_registry(slow, discover=discover)
    outcome = {}

    def start():
        try:
            outcome["start"] = reg.start(REQ)
        except StartRefused as exc:
            outcome["start"] = exc

    thread = threading.Thread(target=start)
    thread.start()
    barrier.wait(timeout=10)  # start() has completed validation/discovery, before claim
    outcome["shutdown"] = reg.begin_shutdown()
    thread.join(timeout=10)
    started = isinstance(outcome["start"], dict)
    shutdown_accepted = outcome["shutdown"][0]
    assert started != shutdown_accepted
    if shutdown_accepted:
        with pytest.raises(StartRefused) as exc:
            reg.start(REQ)
        assert exc.value.conflict and exc.value.message == views.MSG_SHUTTING_DOWN
    release.set()
    reg.join(timeout=10)


def test_seq_increases_across_events():
    def run(target, request, cb):
        cb("bge", "Removing light-pollution gradient", "GraXpert", 4, 9)
        return entry(target["target"])

    reg = make_registry(run)
    snap = reg.start(REQ)
    done = wait_terminal(reg, snap["id"])
    assert done["seq"] > snap["seq"]


def test_summary_formatting_failure_still_reaches_a_terminal_state():
    """Gate finding: a hook-contract violation (non-string target name) must not
    leave state='running' forever — terminality is unconditional."""
    targets = {"targets": [{"target": 123, "folder": "a"}], "skipped": []}

    def run(target, request, cb):
        return entry(target["target"])  # target name is an int: summary join would raise

    reg = make_registry(run, discover=lambda d: targets)
    snap = reg.start(JobRequest("/lib", views.ALL_TARGETS, "showcase", "/out"))
    done = wait_terminal(reg, snap["id"])
    assert done["state"] == "done"
    assert done["summary"] == views.job_failed_line([])
    assert reg.active() is False
    accepted, _ = reg.begin_shutdown()
    assert accepted is True  # the registry must never wedge
