"""Composition root: request validation, per-target composition, mode selection,
startup-error surface, real server boot. Engine faked; no window, no tools."""

import pytest

import seestar.gui as gui
from seestar.gui import views
from seestar.gui.jobs import JobRequest


def test_saved_state_is_schema_checked_and_unknown_keys_are_dropped(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text('{"library":"/lib","output":42,"profile":"bogus","extra":"no"}')
    monkeypatch.setattr(gui, "_STATE_PATH", str(path))
    assert gui._load_state() == {"library": "/lib"}


def test_concurrent_preference_updates_merge_via_atomic_replace(tmp_path, monkeypatch):
    import threading

    path = tmp_path / "state.json"
    monkeypatch.setattr(gui, "_STATE_PATH", str(path))
    gate = threading.Barrier(3)

    def save(**value):
        gate.wait(timeout=10)
        gui._save_state(**value)

    threads = [
        threading.Thread(target=save, kwargs={"library": "/lib"}),
        threading.Thread(target=save, kwargs={"output": "/out"}),
    ]
    for thread in threads:
        thread.start()
    gate.wait(timeout=10)
    for thread in threads:
        thread.join(timeout=10)
    assert gui._load_state() == {"library": "/lib", "output": "/out"}
    assert not list(tmp_path.glob("*.tmp"))


def test_validate_requires_fields(tmp_path):
    validate = gui._make_validate(cfg={})
    lib = tmp_path / "lib"
    lib.mkdir()
    out = str(tmp_path / "out")
    assert views.MSG_CHOOSE_LIBRARY in validate(JobRequest("/missing", "M8", "showcase", out))
    assert views.MSG_CHOOSE_OUTPUT in validate(JobRequest(str(lib), "M8", "showcase", ""))
    assert views.MSG_CHOOSE_PHOTO in validate(JobRequest(str(lib), "", "showcase", out))
    assert validate(JobRequest(str(lib), "M8", "showcase", out)) == []


def test_validate_output_disjoint_from_source(tmp_path):
    """Parity: assert_output_disjoint_from_source still guards every render —
    now with a friendly message instead of v1.0's bare ValueError."""
    validate = gui._make_validate(cfg={})
    lib = tmp_path / "lib"
    lib.mkdir()
    assert validate(JobRequest(str(lib), "M8", "showcase", str(lib / "out"))) == [views.MSG_OUTPUT_OVERLAPS_SOURCE]


def test_validate_recipe_rules(tmp_path):
    validate = gui._make_validate(cfg={})
    lib = tmp_path / "lib"
    lib.mkdir()
    out = tmp_path / "out"
    (out / "M8").mkdir(parents=True)
    assert views.MSG_RECIPE_SINGLE in validate(
        JobRequest(str(lib), views.ALL_TARGETS, "showcase", str(out), use_recipe=True)
    )
    r = JobRequest(str(lib), "M8", "showcase", str(out), use_recipe=True)
    assert validate(r) == [views.msg_no_recipe("M8")]
    (out / "M8" / "M8_recipe.json").write_text("{}")
    assert validate(r) == []
    assert validate(JobRequest(str(lib), "../evil", "showcase", str(out), use_recipe=True)) == [views.MSG_NO_MATCH]


def _request(out_root, use_recipe=False):
    return JobRequest("/lib", "M8", "showcase", str(out_root), use_recipe=use_recipe)


def _passing_qa(proc_dir, cfg):
    return {"passed": True, "category": "nebula", "metrics": {}, "hard_fails": [], "warnings": [], "absolute_gates": {}}


def test_process_target_success_composes_images_qa_warnings(tmp_path):
    proc = tmp_path / "M8"
    proc.mkdir()
    jpg = proc / "M8_final.jpg"
    jpg.write_bytes(b"jpg")
    seen = {}

    def fake_run(folder, out_dir, cfg, **kw):
        seen.update(kw, out_dir=out_dir)
        return {
            "target": "M8",
            "status": "pass",
            "outputs": [{"jpg": str(jpg), "variant": "final"}],
            "notes": {},
            "warnings": ["A previous render of this target was interrupted"],
        }

    process = gui._make_process_target({}, run=fake_run, qa=_passing_qa)
    entry = process({"target": "M8", "folder": "/lib/M8"}, _request(tmp_path), lambda *a: None)
    assert entry["ok"] is True
    assert entry["images"][0]["label"] == "Recommended"
    assert entry["qa"]["heading"] == "Result check: ✅ Ready to use"
    # B-10: run_target's top-level warnings surface here — the frozen app has no
    # stderr, so the GUI result entry is their ONLY surface.
    assert "interrupted" in entry["warnings"][0].lower()
    assert seen["catalog_prior"] is None
    assert seen["out_name"] == "final"
    assert seen["recipe_path"] is None
    assert seen["out_dir"] == str(proc)
    assert entry["replay"] is None


def test_process_target_failure_is_caught_with_friendly_and_technical(tmp_path):
    def fake_run(*a, **k):
        raise RuntimeError("[Errno 13] Permission denied")

    process = gui._make_process_target({}, run=fake_run, qa=_passing_qa)
    entry = process({"target": "M8", "folder": "/lib/M8"}, _request(tmp_path), lambda *a: None)
    assert entry["ok"] is False
    assert "write" in entry["error"]["friendly"].lower()
    assert "RuntimeError" in entry["error"]["technical"]


def test_process_target_qa_failure_is_best_effort(tmp_path):
    proc = tmp_path / "M8"
    proc.mkdir()
    jpg = proc / "M8_final.jpg"
    jpg.write_bytes(b"jpg")

    def bad_qa(*a):
        raise RuntimeError("qa exploded")

    process = gui._make_process_target(
        {},
        run=lambda *a, **k: {"outputs": [{"jpg": str(jpg), "variant": "final"}], "notes": {}, "warnings": []},
        qa=bad_qa,
    )
    entry = process({"target": "M8", "folder": "/lib/M8"}, _request(tmp_path), lambda *a: None)
    assert entry["ok"] is True
    assert entry["qa"]["heading"] == "Result check unavailable"
    assert "qa exploded" in entry["qa"]["technical"]


def test_process_target_replay_report_on_recipe_runs(tmp_path):
    proc = tmp_path / "M8"
    proc.mkdir()
    jpg = proc / "M8_final.jpg"
    jpg.write_bytes(b"jpg")

    def fake_run(folder, out_dir, cfg, **kw):
        assert kw["recipe_path"].endswith("M8_recipe.json")  # server-derived, never client-supplied (v1 §12b)
        return {
            "outputs": [{"jpg": str(jpg), "variant": "final"}],
            "warnings": [],
            "notes": {"recipe_replay": {"changes": [], "warnings": ["pin had no effect"]}},
        }

    process = gui._make_process_target({}, run=fake_run, qa=_passing_qa)
    entry = process({"target": "M8", "folder": "/lib/M8"}, _request(tmp_path, use_recipe=True), lambda *a: None)
    assert entry["replay"] == {"changes": [], "warnings": ["pin had no effect"]}


def test_process_target_refuses_to_claim_success_without_current_run_jpg(tmp_path):
    old = tmp_path / "M8" / "M8_final.jpg"
    old.parent.mkdir()
    old.write_bytes(b"stale")
    process = gui._make_process_target(
        {}, run=lambda *a, **k: {"outputs": [], "notes": {}, "warnings": []}, qa=_passing_qa
    )
    entry = process({"target": "M8", "folder": "/lib/M8"}, _request(tmp_path), lambda *a: None)
    assert entry["ok"] is False
    assert entry["images"] == []
    assert entry["error"]["friendly"] == views.MSG_RESULT_CONTRACT


def test_select_mode():
    assert gui._select_mode(True, platform="darwin") == "browser"
    assert gui._select_mode(False, platform="linux") == "browser"
    assert gui._select_mode(False, platform="darwin") == "window"
    assert gui._select_mode(False, platform="win32") == "window"


def test_select_mode_never_demotes_to_browser_when_the_webview_backend_is_missing(monkeypatch):
    """Tenet 7 -- a step that silently degrades is a defect. A missing pywebview or
    WebView2 Runtime must NOT quietly become browser mode; window mode is still
    selected and the failure surfaces as an explicit error out of run_window()."""

    def boom():
        raise ImportError("no webview backend")

    monkeypatch.setattr(gui, "_import_webview", boom, raising=False)
    assert gui._select_mode(False, platform="darwin") == "window"
    assert gui._select_mode(False, platform="win32") == "window"


@pytest.mark.parametrize(("accepted", "expected"), [(False, False), (True, True)])
def test_window_close_uses_atomic_shutdown_admission(monkeypatch, accepted, expected):
    import sys
    import types

    from seestar.gui.window import run_window

    hooks = []
    dialogs = []

    class Closing:
        def __iadd__(self, callback):
            hooks.append(callback)
            return self

    window = types.SimpleNamespace(
        events=types.SimpleNamespace(closing=Closing()),
        create_confirmation_dialog=lambda *args: dialogs.append(args),
    )
    fake_webview = types.SimpleNamespace(
        create_window=lambda *a, **k: window,
        start=lambda **kw: outcomes.append(hooks[0]()),
    )
    outcomes = []

    class Registry:
        def begin_shutdown(self):
            return accepted, None if accepted else {"id": "busy"}

    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    ref = {}
    run_window("http://local", registry=Registry(), window_ref=ref)
    assert outcomes == [expected]
    assert bool(dialogs) is (not accepted)
    assert ref["window"] is window


def test_frozen_app_surfaces_visible_dialog(monkeypatch):
    calls = []
    monkeypatch.setattr(gui.sys, "frozen", True, raising=False)
    monkeypatch.setattr(gui.sys, "platform", "darwin")
    monkeypatch.setattr(gui.subprocess, "run", lambda *a, **k: calls.append(a[0]))
    gui._show_startup_error("the lock is held")
    assert calls, "frozen .app must surface a visible dialog, not just stderr"
    argv = calls[0]
    # message is an osascript ARGUMENT, never interpolated into the script
    assert argv[0] == "osascript" and argv[-1] == "the lock is held"


def test_start_server_serves_the_authenticated_page_and_stops():
    pytest.importorskip("fastapi")
    pytest.importorskip("uvicorn")
    import http.cookiejar
    import urllib.request

    from seestar.gui.jobs import EngineHooks

    hooks = EngineHooks(
        discover=lambda d: {"targets": [], "skipped": []}, validate=lambda r: [], process_target=lambda t, r, cb: None
    )
    running = gui.start_server(cfg={}, mode="browser", hooks=hooks)
    try:
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        with opener.open(running.url, timeout=5) as resp:  # follows the 303 exchange
            body = resp.read().decode()
        assert "Photon Finish" in body
    finally:
        assert running.stop(timeout=10) is True, "orderly shutdown must join the server thread"
        assert running.stop(timeout=10) is True, "stop is idempotent"
        rebound = gui.bind_server_socket(running.port)
        rebound.close()


def test_running_server_refuses_to_stop_beside_active_job():
    pytest.importorskip("fastapi")
    pytest.importorskip("uvicorn")
    import threading

    from seestar.gui.jobs import EngineHooks

    release = threading.Event()

    def process_target(target, request, callback):
        release.wait(timeout=30)
        return {"target": "M8", "ok": True, "images": [], "qa": None, "replay": None, "warnings": [], "error": None}

    hooks = EngineHooks(
        discover=lambda d: {"targets": [{"target": "M8", "folder": "/lib/M8"}], "skipped": []},
        validate=lambda r: [],
        process_target=process_target,
    )
    running = gui.start_server(cfg={}, mode="browser", hooks=hooks)
    try:
        running.registry.start(JobRequest("/lib", "M8", "showcase", "/out"))
        assert running.stop(timeout=0.1) is False
        assert running.thread.is_alive()
    finally:
        release.set()
        running.registry.join(timeout=10)
        assert running.stop(timeout=10) is True


def test_start_server_readiness_timeout_unwinds(monkeypatch):
    """Spec §11: readiness timeout. A server that never reports started must raise
    (and close its socket) instead of handing back a dead RunningServer."""
    pytest.importorskip("fastapi")
    uvicorn = pytest.importorskip("uvicorn")
    import time as _time

    class StuckServer:
        def __init__(self, config):
            self.started = False
            self.should_exit = False

        def run(self, sockets=None):
            while not self.should_exit:
                _time.sleep(0.01)

    monkeypatch.setattr(uvicorn, "Server", StuckServer)
    monkeypatch.setattr(gui, "_READY_TIMEOUT_S", 0.3)
    from seestar.gui.jobs import EngineHooks

    hooks = EngineHooks(
        discover=lambda d: {"targets": [], "skipped": []}, validate=lambda r: [], process_target=lambda t, r, cb: None
    )
    probe = gui.bind_server_socket(None)
    port = probe.getsockname()[1]
    probe.close()
    with pytest.raises(RuntimeError):
        gui.start_server(cfg={}, port=port, mode="browser", hooks=hooks)
    rebound = gui.bind_server_socket(port)
    rebound.close()


def test_app_construction_failure_closes_the_prebound_socket(monkeypatch):
    pytest.importorskip("fastapi")
    import seestar.gui.server as server_module
    from seestar.gui.jobs import EngineHooks

    probe = gui.bind_server_socket(None)
    port = probe.getsockname()[1]
    probe.close()
    monkeypatch.setattr(server_module, "create_app", lambda **kw: (_ for _ in ()).throw(RuntimeError("app failed")))
    hooks = EngineHooks(
        discover=lambda d: {"targets": [], "skipped": []}, validate=lambda r: [], process_target=lambda t, r, cb: None
    )
    with pytest.raises(RuntimeError, match="app failed"):
        gui.start_server(cfg={}, port=port, mode="browser", hooks=hooks)
    rebound = gui.bind_server_socket(port)
    rebound.close()


def test_thread_start_failure_closes_the_prebound_socket(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("uvicorn")
    from seestar.gui.jobs import EngineHooks

    class DeadThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("thread failed")

        def is_alive(self):
            return False

    probe = gui.bind_server_socket(None)
    port = probe.getsockname()[1]
    probe.close()
    monkeypatch.setattr(gui.threading, "Thread", DeadThread)
    hooks = EngineHooks(
        discover=lambda d: {"targets": [], "skipped": []}, validate=lambda r: [], process_target=lambda t, r, cb: None
    )
    with pytest.raises(RuntimeError, match="thread failed"):
        gui.start_server(cfg={}, port=port, mode="browser", hooks=hooks)
    rebound = gui.bind_server_socket(port)
    rebound.close()


def test_startup_failure_releases_the_lock(monkeypatch):
    """Spec §11: lock release on every startup failure — main() must unwind the
    OS lock when start_server dies, and must not have written metadata."""
    events = []

    class FakeLock:
        def acquire(self):
            events.append("acquire")

        def write_metadata(self, pid, port):
            events.append("metadata")

        def release(self):
            events.append("release")

    monkeypatch.setattr(gui, "SingleInstanceLock", FakeLock)
    monkeypatch.setattr(gui, "_show_startup_error", lambda msg: events.append(("error", msg)))

    def boom(*a, **k):
        raise RuntimeError("no server")

    monkeypatch.setattr(gui, "start_server", boom)
    with pytest.raises(SystemExit):
        gui.main([])
    assert "release" in events
    assert "metadata" not in events
    assert any(isinstance(e, tuple) and e[0] == "error" for e in events)


def test_window_backend_failure_errors_instead_of_opening_a_browser(monkeypatch):
    """R3 fallback REMOVED (owner decision, 2026-08-20). When run_window() raises --
    pywebview missing, or the WebView2 Runtime absent on Windows -- main() must show an
    explicit error naming WebView2 and exit, never silently demote to browser mode.
    Tenet 7: a step that silently degrades is a defect, and the old fallback's print()
    went to a null stdout in the --windowed build, so the user saw an unexplained
    browser tab. Removing it also drops a second Windows execution path."""
    import threading

    import seestar.gui.window as window_module

    events = []

    class FakeLock:
        def acquire(self):
            events.append("acquire")

        def write_metadata(self, pid, port):
            events.append("metadata")

        def release(self):
            events.append("release")

    # Faithful to the real server thread: it stays alive until stop(), so main()'s
    # R6 _watch thread BLOCKS in join() rather than racing ahead and reporting a
    # spurious "engine stopped unexpectedly" dialog.
    class FakeThread:
        def __init__(self):
            self.done = threading.Event()

        def is_alive(self):
            return not self.done.is_set()

        def join(self, timeout=None):
            self.done.wait(timeout)

    class FakeServer:
        should_exit = False

    class FakeRegistry:
        def begin_shutdown(self):
            return True, 0

        def active(self):
            return False

        def join(self):
            pass

    class FakeRunning:
        def __init__(self):
            self.port = 7777
            self.url = "http://127.0.0.1:7777/?launch=tok"
            self.thread = FakeThread()
            self.server = FakeServer()
            self.registry = FakeRegistry()
            # Simulate run_window having already stashed the pywebview window
            # before webview.start() raised.
            self.window_ref = {"window": object()}
            self.stopped = False

        def stop(self, timeout=10):
            # Real stop() sets should_exit BEFORE the thread ends -- that ordering is
            # what keeps the R6 watcher quiet on a deliberate shutdown.
            self.server.should_exit = True
            self.thread.done.set()
            self.stopped = True
            return True

    def boom(*a, **k):
        raise RuntimeError("WebView2 runtime not found")

    created = []

    def fake_start_server(**kw):
        r = FakeRunning()
        created.append(r)
        return r

    monkeypatch.setattr(gui, "SingleInstanceLock", FakeLock)
    monkeypatch.setattr(gui, "_select_mode", lambda *a, **k: "window")
    monkeypatch.setattr(gui, "start_server", fake_start_server)
    monkeypatch.setattr(window_module, "run_window", boom)
    monkeypatch.setattr(gui, "_show_startup_error", lambda msg: events.append(("error", msg)))
    monkeypatch.setattr(gui.webbrowser, "open", lambda url: events.append(("opened", url)))

    with pytest.raises(SystemExit) as excinfo:
        gui.main([])

    assert excinfo.value.code == 1
    errors = [e[1] for e in events if isinstance(e, tuple) and e[0] == "error"]
    opened = [e[1] for e in events if isinstance(e, tuple) and e[0] == "opened"]
    assert len(errors) == 1
    assert "WebView2" in errors[0]
    # The whole point: no browser is opened, and no second execution path is entered.
    assert opened == []
    running = created[0]
    assert running.stopped is True
    assert "release" in events


def test_metadata_failure_stops_server_and_releases_lock(monkeypatch):
    events = []

    class FakeLock:
        def acquire(self):
            events.append("acquire")

        def write_metadata(self, pid, port):
            raise OSError("metadata failed")

        def release(self):
            events.append("release")

    class FakeRunning:
        port = 7777

        def stop(self, timeout=10):
            events.append("stop")
            return True

    monkeypatch.setattr(gui, "SingleInstanceLock", FakeLock)
    monkeypatch.setattr(gui, "start_server", lambda **kw: FakeRunning())
    monkeypatch.setattr(gui, "_show_startup_error", lambda msg: events.append(("error", msg)))
    with pytest.raises(SystemExit):
        gui.main([])
    assert events.index("stop") < events.index("release")
