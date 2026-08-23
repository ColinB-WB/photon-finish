"""Frozen-app entry point for Photon Finish (.app bundle).

This is what the bundled Python runs when the user double-clicks the app. It just hands off
to the existing GUI main(), which acquires the single-instance lock, starts the local job
server, and opens the app window (or a browser in `--browser` mode). Kept tiny so the freeze
surface is the real package, not this shim.

`SEESTAR_SELFTEST` env var: a tiny headless escape hatch for the clean-build packaging
gate (docs/CLEAN_BUILD_TEST.md sec1a) -- it needs to EXERCISE things from inside the
actual frozen binary that only break when frozen: `known-targets` proves
`importlib.resources.files("seestar.data")` resolves in PyInstaller's frozen filesystem;
`gui-static` proves the static UI (Task 6 package data) shipped inside the bundle;
`gui-server` boots the real server factory headless and fetches the authenticated page
through the launch-token exchange. Without this hook there is no way to run the frozen
app in a non-GUI mode at all. Not a general CLI -- these three values are supported
today; unrecognized values fall through to the normal GUI launch so this can never
surprise an end user.
"""

import multiprocessing
import os
import stat
import sys
import time


def _is_devnull(stream) -> bool:
    """True when `stream` is a real handle that points at the null device. On macOS a
    double-clicked .app gets exactly that from launchd -- stdio exists but everything
    written to it vanishes. Streams without a usable fileno (pytest capture wrappers,
    None) are not devnull.

    POSIX-only by design: Windows windowed builds lose stdio as None (the other branch
    of the redirect trigger), NUL has no meaningful st_rdev to compare, and a Windows
    CONSOLE handle is also a char device -- so any Windows char-device heuristic would
    risk redirecting a real, visible console into the log file."""
    if sys.platform.startswith("win"):
        return False
    try:
        st = os.fstat(stream.fileno())
        dn = os.stat(os.devnull)
        return stat.S_ISCHR(st.st_mode) and st.st_rdev == dn.st_rdev
    except Exception:
        return False


def _windowed_log_dir() -> str:
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Logs/Photon Finish")
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "."
        return os.path.join(base, "Photon Finish", "logs")
    return os.path.expanduser("~/.local/state/photon-finish")


def _redirect_windowed_stdio():
    """Point lost stdio at a findable log file; return the log file, or None if stdio
    is healthy and untouched.

    Two ways a windowed launch loses stdio, both must redirect:
    - Windows --windowed double-click: sys.stdout/sys.stderr are None. Anything that
      touches them crashes before the GUI appears -- uvicorn's logging setup calls
      stream.isatty() unconditionally, raising AttributeError on None, which aborts
      main() with no server ever listening. (Launching from a terminal supplies real
      stdio and hides the bug.)
    - macOS double-clicked .app: launchd supplies REAL handles that point at /dev/null,
      so nothing crashes -- but every traceback silently vanishes. 2026-08-12 (NGC6992):
      a failed render's only diagnostic went to /dev/null, making it undiagnosable.

    Line-buffered, so a crash right after a traceback can't eat it. Rotated once at
    ~5 MB (append-mode would otherwise grow forever)."""
    if not (sys.stdout is None or sys.stderr is None or _is_devnull(sys.stdout) or _is_devnull(sys.stderr)):
        return None
    try:
        log_dir = _windowed_log_dir()
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "app.log")
        try:
            if os.path.getsize(log_path) > 5 * 1024 * 1024:
                os.replace(log_path, log_path + ".old")
        except OSError:
            pass  # rotation is incidental; a missing/unstattable log is fine
        log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    except Exception:
        if sys.stdout is None or sys.stderr is None:
            raise  # None streams crash uvicorn later anyway -- fail loudly now
        return None  # devnull streams still work; losing the log beats losing the app
    sys.stdout = log_file
    sys.stderr = log_file
    print(f"--- Photon Finish launched {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
    return log_file


_redirect_windowed_stdio()


def _selftest_known_targets() -> int:
    from seestar.known_targets import load_catalog

    records = load_catalog()
    if len(records) < 4:
        print(f"FAIL: expected >= 4 known-target records, got {len(records)}", file=sys.stderr)
        return 1
    canonical_ids = {r.canonical_id for r in records}
    if "M8" not in canonical_ids:
        print(f"FAIL: M8 not present in canonical ids {sorted(canonical_ids)}", file=sys.stderr)
        return 1
    print(f"OK {len(records)} records")
    return 0


def _selftest_gui_static() -> int:
    """Prove the static UI shipped inside the frozen bundle (packaging gate for the
    Task 6 package data — the frozen app is the only place this can silently break)."""
    from importlib import resources

    static = resources.files("seestar.gui").joinpath("static")
    required = ["index.html", "app.js", "state.js", "app.css", "favicon.svg", "package.json"]
    missing = [name for name in required if not static.joinpath(name).is_file()]
    if missing:
        print(f"FAIL: missing static assets: {missing}", file=sys.stderr)
        return 1
    if "Photon Finish" not in static.joinpath("index.html").read_text(encoding="utf-8"):
        print("FAIL: index.html has unexpected content", file=sys.stderr)
        return 1
    print("OK gui static assets present")
    return 0


def _selftest_gui_server() -> int:
    """Boot the REAL server factory headless inside the frozen binary and fetch the
    authenticated page through the launch-token exchange (the skeleton gate's
    selftest, now against production code — no WebKit touched)."""
    import http.cookiejar
    import urllib.request

    from seestar.gui import start_server
    from seestar.gui.jobs import EngineHooks

    hooks = EngineHooks(
        discover=lambda d: {"targets": [], "skipped": []}, validate=lambda r: [], process_target=lambda t, r, cb: None
    )
    running = start_server(cfg={}, mode="browser", hooks=hooks)
    try:
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        with opener.open(running.url, timeout=10) as resp:
            body = resp.read().decode()
        if "Photon Finish" not in body:
            print("FAIL: served page missing the app shell", file=sys.stderr)
            return 1
    finally:
        # A plain assignment here (not `return` inside finally, which would silence
        # any exception raised by the try block -- ruff B012) so a genuine failure
        # above still propagates; the stop check below only fires on a clean exit.
        stopped = running.stop(timeout=10)
    if not stopped:
        print("FAIL: server thread did not stop", file=sys.stderr)
        return 1
    print("OK gui server selftest")
    return 0


def _run():
    selftest = os.environ.get("SEESTAR_SELFTEST")
    if selftest == "known-targets":
        raise SystemExit(_selftest_known_targets())
    if selftest == "gui-static":
        raise SystemExit(_selftest_gui_static())
    if selftest == "gui-server":
        raise SystemExit(_selftest_gui_server())

    from seestar.gui import main

    # Single-instance: gui.main() holds an OS lock (~/.seestar_gui.lock); a second
    # launch shows a native "already open" dialog (gui._show_startup_error -- sys.frozen
    # is True in the packaged app) before SystemExit(1). Do NOT wrap in
    # try/except SystemExit -- the dialog is the windowed app's only signal;
    # swallowing the exit would make the app silently vanish.
    main()


if __name__ == "__main__":
    multiprocessing.freeze_support()  # required: uvicorn may spawn child processes
    _run()
