"""Photon Finish GUI — FastAPI job server + native window (pywebview) or browser.

Replaces the v1.0 Gradio GUI. main() is the composition root: it loads config
once and injects it plus the engine entry points into JobRegistry/create_app —
no import-time _CFG, no hidden server singleton (spec §3). Startup order:
OS lock -> bound loopback socket -> authenticated app -> uvicorn worker thread ->
bounded readiness barrier -> viewer. Every startup failure unwinds in reverse.

Run:  python -m seestar.gui     (or the `seestar-gui` console script)
      seestar-gui --browser     (serve for a normal browser; dev / no-window use)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from dataclasses import dataclass, field

from ..io import load_config
from ..paths import assert_output_disjoint_from_source, safe_output_dir
from ..qa import qa_target
from ..run import discover_targets, run_target
from . import pickers, views
from .instance import AlreadyRunning, SingleInstanceLock, bind_server_socket
from .jobs import EngineHooks, JobRegistry

# Where the GUI remembers the last library/output/profile between sessions.
_STATE_PATH = os.path.expanduser("~/.seestar_gui_state.json")
_STATE_LOCK = threading.RLock()
_PROFILES = {"showcase", "natural", "documentation", "print"}
_READY_TIMEOUT_S = 15.0


def _read_state_unlocked() -> dict:
    try:
        with open(_STATE_PATH) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        clean = {}
        if isinstance(data.get("library"), str):
            clean["library"] = data["library"]
        if isinstance(data.get("output"), str):
            clean["output"] = data["output"]
        if data.get("profile") in _PROFILES:
            clean["profile"] = data["profile"]
        return clean
    except Exception:
        return {}


def _load_state() -> dict:
    """Schema-checked last-session choices; serialized with writers."""
    with _STATE_LOCK:
        return _read_state_unlocked()


def _save_state(**kw) -> None:
    """Locked read/merge + same-directory atomic replace. Preferences are
    best-effort, but failures are logged rather than silently corrupting state."""
    update = {}
    for key in ("library", "output"):
        if key in kw and kw[key] is not None and isinstance(kw[key], str):
            update[key] = kw[key]
    if kw.get("profile") in _PROFILES:
        update["profile"] = kw["profile"]
    tmp = None
    with _STATE_LOCK:
        try:
            state = _read_state_unlocked()
            state.update(update)
            parent = os.path.dirname(_STATE_PATH) or "."
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=parent, prefix=".seestar_gui_state.", suffix=".tmp", delete=False
            ) as f:
                tmp = f.name
                json.dump(state, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, _STATE_PATH)
            tmp = None
        except Exception as exc:
            print(f"WARNING: could not save Photon Finish preferences: {exc}", file=sys.stderr)
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


def _make_validate(cfg):
    """JobRequest -> friendly errors (empty list = valid). Injected into JobRegistry;
    validation failures never create a job (spec §4)."""

    def validate(request) -> list:
        errors = []
        if not os.path.isdir(request.library_dir):
            errors.append(views.MSG_CHOOSE_LIBRARY)
        if not request.output_root:
            errors.append(views.MSG_CHOOSE_OUTPUT)
        if not request.which:
            errors.append(views.MSG_CHOOSE_PHOTO)
        if errors:
            return errors
        try:
            assert_output_disjoint_from_source(request.library_dir, request.output_root)
        except ValueError:
            errors.append(views.MSG_OUTPUT_OVERLAPS_SOURCE)
        if request.use_recipe:
            # The plain Render button never reads a recipe; this is the explicit
            # "use my saved adjustments" path, server-derived only (v1 §12b).
            if request.which == views.ALL_TARGETS:
                errors.append(views.MSG_RECIPE_SINGLE)
            else:
                try:
                    if not os.path.exists(views.recipe_path(request.output_root, request.which)):
                        errors.append(views.msg_no_recipe(request.which))
                except ValueError:
                    errors.append(views.MSG_NO_MATCH)
        return errors

    return validate


def _make_process_target(cfg, run=run_target, qa=qa_target):
    """One target -> one result entry. Catches its own per-target failures — an
    exception ESCAPING this function is a job-level failure by contract (jobs.py)."""

    def process_target(target, request, on_stage):
        name = target["target"]
        out_dir = safe_output_dir(request.output_root, name)
        recipe = views.recipe_path(request.output_root, name) if request.use_recipe else None
        entry = {"target": name, "ok": True, "images": [], "qa": None, "replay": None, "warnings": [], "error": None}
        try:
            res = run(
                target["folder"],
                out_dir,
                cfg,
                profile=request.profile,
                catalog_prior=None,  # engine default (on); the GUI exposes no toggle
                out_name=views.out_name(request.profile),
                recipe_path=recipe,
                on_stage=on_stage,
            )
        except Exception as e:
            entry.update(
                ok=False, error={"friendly": views.friendly_error(str(e)), "technical": traceback.format_exc()}
            )
            return entry
        entry["images"] = views.images_from_result(res, name)
        if not entry["images"]:
            entry.update(
                ok=False,
                error={
                    "friendly": views.MSG_RESULT_CONTRACT,
                    "technical": "run_target returned without a current-run JPG deliverable",
                },
            )
            return entry
        try:
            entry["qa"] = views.qa_view(qa(out_dir, cfg))
        except Exception as e:  # QA is best-effort in the GUI (parity with v1.0)
            entry["qa"] = views.qa_unavailable_view(e)
        # B-10: run_target's top-level warnings (e.g. crash-era .prev_* notice) —
        # the frozen app never shows stdout/stderr, so this is their ONLY surface.
        entry["warnings"] = [views.friendly_warning(w) for w in (res or {}).get("warnings") or []]
        if recipe:
            entry["replay"] = views.replay_view((res or {}).get("notes"))
        return entry

    return process_target


def _select_mode(force_browser: bool, platform: str | None = None) -> str:
    """window mode on macOS/Windows, browser elsewhere. `--browser` forces (owner
    decision: the flag is the only macOS exposure). A missing pywebview / WebView2
    is deliberately NOT demoted to browser mode — it surfaces as an explicit error
    from run_window() (TENETS 7: a step that silently degrades is a defect)."""
    platform = platform or sys.platform
    if force_browser:
        return "browser"
    if platform != "darwin" and not platform.startswith("win"):
        return "browser"
    return "window"


def _static_dir() -> str:
    from importlib import resources

    return str(resources.files("seestar.gui").joinpath("static"))


def _osascript_argv(msg: str) -> list:
    """osascript command that shows `msg` via `on run argv` — the message is a
    process ARGUMENT, never interpolated into AppleScript source (user text can
    contain quotes)."""
    return [
        "osascript",
        "-e",
        "on run argv",
        "-e",
        'display dialog (item 1 of argv) buttons {"OK"} with icon caution',
        "-e",
        "end run",
        msg,
    ]


def _show_startup_error(msg: str) -> None:
    """stderr AND, in the frozen app (no terminal), a native dialog — the only
    signal a windowed .app user ever sees."""
    print(msg, file=sys.stderr)
    if getattr(sys, "frozen", False):
        try:
            if sys.platform == "darwin":
                subprocess.run(_osascript_argv(msg), capture_output=True)
            elif sys.platform.startswith("win"):
                import ctypes

                ctypes.windll.user32.MessageBoxW(0, msg, "Photon Finish", 0x30)
        except Exception:
            pass


@dataclass
class RunningServer:
    server: object
    thread: threading.Thread
    port: int
    url: str  # the one-time tokened launch URL
    socket: object  # the exact pre-bound socket; this object owns its closure
    registry: JobRegistry
    auth: object
    window_ref: dict
    request_quit: object
    _stop_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _stopped: bool = field(default=False, repr=False)

    def stop(self, timeout: float = 10.0) -> bool:
        with self._stop_lock:
            if self._stopped:
                return not self.thread.is_alive()
            accepted, _active = self.registry.begin_shutdown()
            if not accepted:
                return False  # never strand an active worker by removing its server
            self.server.should_exit = True  # thread-safe uvicorn shutdown request
            self.thread.join(timeout)
            stopped = not self.thread.is_alive()
            if stopped:
                try:
                    self.socket.close()
                finally:
                    self._stopped = True
            return stopped


def _make_pick_folder(window_ref: dict):
    """Build the window-mode folder picker.

    WINDOWS TAKES THE SUBPROCESS PICKER even though a native window exists.
    pywebview's winforms backend calls the folder dialog directly on the CALLING
    thread (platforms/winforms.py::create_file_dialog) -- unlike every other UI
    operation in that same backend, which marshals through Control.Invoke. FastAPI
    runs the sync /api/pick-folder route in a threadpool worker, so the native
    dialog would be built off the UI thread, where it HANGS FOREVER holding
    pick_lock: both Browse buttons dead, no dialog on screen, and an empty log --
    the diagnostic print below only runs once the call returns. Reproduced live on
    Windows 11 (2026-08-31). The subprocess picker is thread-safe by construction
    (a child process owns its own main thread) and still shows the real native
    Windows folder chooser.

    macOS marshals correctly (cocoa.py: AppHelper.callAfter + a semaphore), so it
    keeps the native dialog, which is properly app-modal and parented to the window.
    """

    def pick_folder(current: str):
        w = window_ref.get("window")
        if w is None or sys.platform.startswith("win"):
            return pickers.pick_directory(current) or None
        import webview

        start = current if current and os.path.isdir(current) else os.path.expanduser("~")
        try:
            picked = w.create_file_dialog(webview.FileDialog.FOLDER, directory=start)
        except Exception as e:
            # Never swallow this: a dialog that fails looks IDENTICAL to a cancel
            # from the front end (both leave the old path on screen), which reads
            # as "the app reverted my choice" — a silent degrade (tenet 7). Logged
            # for diagnosis and re-raised so the API answers with a real message.
            print(f"[pick_folder] native dialog raised: {type(e).__name__}: {e}", file=sys.stderr)
            raise
        # Gate fact (2026-08-12): returns a LIST of paths; None/empty on cancel.
        # Logged either way — an owner report of "it reverts" is unanswerable
        # without knowing whether the dialog returned nothing or was cancelled.
        print(f"[pick_folder] start={start!r} returned={picked!r}", file=sys.stderr)
        return picked[0] if picked else None

    return pick_folder


def start_server(cfg=None, *, port=None, mode="browser", hooks=None) -> RunningServer:
    """Bind -> build -> start -> wait ready (spec §3 startup order). Used by main()
    and by the frozen selftest (SEESTAR_SELFTEST=gui-server). Raises OSError on a
    busy fixed port; RuntimeError if uvicorn never reports ready (socket closed
    on the way out)."""
    import uvicorn

    from .server import AuthState, create_app

    cfg = load_config() if cfg is None else cfg
    sock = None
    server = None
    thread = None
    holder = {}

    def request_quit():
        # Let the /api/quit response flush, then stop uvicorn, JOIN the server
        # thread (bounded), and tear the window down LAST (spec §4 shutdown
        # ordering; a failed join is also logged in main()'s finally).
        def _stop():
            running = holder.get("running")
            if running is None:
                return
            if not running.stop(timeout=10):
                print("WARNING: server thread still alive at window teardown", file=sys.stderr)
                _show_startup_error(
                    "Photon Finish could not stop its local engine cleanly. The window "
                    "will stay open; check the app log before closing it from the OS."
                )
                return
            w = running.window_ref.get("window")
            if w is not None:
                try:
                    w.destroy()
                except Exception:
                    print("window destroy failed:", traceback.format_exc())

        threading.Timer(0.5, _stop).start()

    try:
        sock = bind_server_socket(port)
        bound_port = sock.getsockname()[1]
        registry = JobRegistry(
            hooks
            or EngineHooks(
                discover=discover_targets,
                validate=_make_validate(cfg),
                process_target=_make_process_target(cfg),
            )
        )
        auth = AuthState()
        window_ref = {"window": None}

        pick_folder = _make_pick_folder(window_ref)

        app = create_app(
            registry=registry,
            auth=auth,
            expected_hosts={f"127.0.0.1:{bound_port}", f"localhost:{bound_port}"},
            static_dir=_static_dir(),
            mode=mode,
            load_state=_load_state,
            save_state=_save_state,
            discover=discover_targets,
            pick_folder=pick_folder,
            request_quit=request_quit,
            open_recipe=pickers.open_in_editor,
        )
        # access_log=False (spec §8): request lines must not record launch tokens.
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + _READY_TIMEOUT_S
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("server did not become ready")
            time.sleep(0.05)
        url = f"http://127.0.0.1:{bound_port}/?launch={auth.launch_token}"
        running = RunningServer(
            server=server,
            thread=thread,
            port=bound_port,
            url=url,
            socket=sock,
            registry=registry,
            auth=auth,
            window_ref=window_ref,
            request_quit=request_quit,
        )
        holder["running"] = running
        return running
    except BaseException:
        # One owner, one unwind path: every post-bind failure requests shutdown,
        # joins a started thread, and closes the exact pre-bound socket.
        if server is not None:
            server.should_exit = True
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
        if sock is not None:
            sock.close()
        raise


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="seestar-gui", description="Photon Finish — local GUI")
    parser.add_argument(
        "--browser", action="store_true", help="serve for your web browser instead of opening the app window"
    )
    return parser.parse_args(argv)


def main(argv=None) -> None:
    args = _parse_args(argv)
    lock = SingleInstanceLock()
    try:
        lock.acquire()
    except AlreadyRunning as e:
        _show_startup_error(str(e))
        raise SystemExit(1) from None
    except OSError as e:
        # open() itself failed (e.g. unwritable $HOME) — still a dialog, not a
        # silent vanish (the frozen app has no terminal).
        _show_startup_error(
            f"Photon Finish could not create its startup file ({e}). Check that your home folder is writable."
        )
        raise SystemExit(1) from None

    env_port = os.environ.get("SEESTAR_GUI_PORT")
    mode = _select_mode(args.browser)
    try:
        running = start_server(port=int(env_port) if env_port else None, mode=mode)
    except OSError:
        lock.release()
        if env_port:
            _show_startup_error(
                f"Photon Finish could not open local port {env_port}. Another program "
                "is using it — unset SEESTAR_GUI_PORT or choose a different port."
            )
        else:
            _show_startup_error(
                "Photon Finish could not open a local network port. Check your network settings and try again."
            )
        raise SystemExit(1) from None
    except Exception:
        lock.release()
        traceback.print_exc()
        _show_startup_error("Photon Finish could not start its local engine. Check the app log and try again.")
        raise SystemExit(1) from None
    try:
        lock.write_metadata(os.getpid(), running.port)
    except Exception:
        running.stop()
        lock.release()
        _show_startup_error(
            "Photon Finish started its local engine but could not record its startup "
            "information. The engine was stopped; check your home-folder permissions."
        )
        raise SystemExit(1) from None

    def _watch():
        # R6: if the server thread dies while a render worker lives, the page
        # freezes — the front end shows the degraded state; this adds the native
        # surface. A deliberate stop() sets should_exit first and is ignored here.
        running.thread.join()
        if not running.server.should_exit:
            _show_startup_error(
                "Photon Finish's local engine stopped unexpectedly. A render may still "
                "be running; do not start another copy yet. Please close Photon Finish "
                "fully, check the app log, then reopen it."
            )

    threading.Thread(target=_watch, daemon=True).start()

    try:
        if mode == "window":
            from .window import run_window

            try:
                run_window(running.url, registry=running.registry, window_ref=running.window_ref)
            except Exception:
                # MANDATORY-FAIL tier: the native backend is unavailable (pywebview
                # missing, or the WebView2 Runtime absent on Windows). The R3 browser
                # fallback was REMOVED 2026-08-20 — it silently degraded (TENETS 7),
                # its print() went to a null stdout in the --windowed build so the user
                # saw an unexplained browser tab, and it committed the project to a
                # second Windows execution path. The `finally` below stops the server
                # and releases the single-instance lock.
                traceback.print_exc()
                _show_startup_error(views.MSG_WEBVIEW_UNAVAILABLE)
                raise SystemExit(1) from None
        if mode == "browser":
            print(f"Photon Finish is running: {running.url}")
            threading.Timer(1.0, lambda: webbrowser.open(running.url)).start()
            while running.thread.is_alive():
                try:
                    running.thread.join(0.5)
                except KeyboardInterrupt:
                    accepted, _active = running.registry.begin_shutdown()
                    if accepted:
                        break
                    print(views.MSG_QUIT_BUSY, file=sys.stderr)
    finally:
        stopped = running.stop()
        if not stopped and running.registry.active():
            # An unexpected viewer/server exit must not release the process lock
            # beside a live writer. Keep the process alive until the accepted job
            # reaches terminal state, then perform the ordinary shutdown.
            print(views.MSG_QUIT_BUSY, file=sys.stderr)
            running.registry.join()
            stopped = running.stop()
        if not stopped:
            print("WARNING: server thread did not exit within 10s", file=sys.stderr)
        lock.release()
