"""Frozen-app stdio redirect (packaging/app_main.py).

2026-08-12 incident (NGC6992): on macOS, a double-clicked .app gets REAL stdio handles
pointing at /dev/null (launchd supplies them), so the original `sys.stdout is None`
trigger -- written for Windows --windowed builds, where the handles are None -- never
fired. Every traceback the GUI printed (including a failed render's) vanished into
/dev/null, making the failure undiagnosable. The redirect must treat "stdio is
/dev/null" the same as "stdio is None".

Importing app_main under pytest is safe: pytest supplies real (non-devnull) streams,
so the module-level redirect call is a no-op here.
"""

import os
import sys

import pytest

import packaging.app_main as app_main

# The devnull-stdio shape is the macOS/POSIX launchd case; on Windows a windowed build
# loses stdio as None instead, and _is_devnull is deliberately inert there (a Windows
# console handle is also a char device -- detection would risk redirecting a real,
# visible console). The POSIX-shaped tests below are skipped on Windows accordingly.
_posix_only = pytest.mark.skipif(sys.platform.startswith("win"), reason="devnull stdio is the POSIX launchd case")


class _NoFileno:
    """Stand-in for pytest/IDE stream wrappers whose fileno() raises."""

    def fileno(self):
        raise ValueError("no fileno")


@_posix_only
def test_devnull_stream_is_detected():
    with open(os.devnull, "w") as f:
        assert app_main._is_devnull(f) is True


def test_devnull_detection_is_inert_on_windows(monkeypatch):
    """A char-device heuristic on Windows could mistake a real CONSOLE handle for lost
    stdio and swallow visible terminal output into the log -- so on win32 the answer is
    always False, even for a genuinely null stream."""
    monkeypatch.setattr(app_main.sys, "platform", "win32")
    with open(os.devnull, "w") as f:
        assert app_main._is_devnull(f) is False


def test_regular_file_stream_is_not_devnull(tmp_path):
    p = tmp_path / "x.txt"
    with open(p, "w") as f:
        assert app_main._is_devnull(f) is False


def test_stream_without_fileno_is_not_devnull():
    assert app_main._is_devnull(_NoFileno()) is False
    assert app_main._is_devnull(None) is False


@_posix_only
def test_devnull_stdio_is_redirected_to_the_log_file(tmp_path, monkeypatch):
    """The macOS double-click case: real handles, but they point at /dev/null."""
    monkeypatch.setattr(app_main, "_windowed_log_dir", lambda: str(tmp_path))
    devnull = open(os.devnull, "w")
    monkeypatch.setattr(sys, "stdout", devnull)
    monkeypatch.setattr(sys, "stderr", devnull)
    log = app_main._redirect_windowed_stdio()
    try:
        assert log is not None
        print("traceback would land here")
        assert sys.stdout is log and sys.stderr is log
        with open(tmp_path / "app.log") as f:
            content = f.read()
        # line-buffered: visible without an explicit flush, so a crash can't eat it
        assert "traceback would land here" in content
    finally:
        if log is not None:
            log.close()
        devnull.close()


def test_none_stdio_is_redirected(tmp_path, monkeypatch):
    """The original Windows --windowed case must keep working."""
    monkeypatch.setattr(app_main, "_windowed_log_dir", lambda: str(tmp_path))
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    log = app_main._redirect_windowed_stdio()
    try:
        assert log is not None and sys.stdout is log
    finally:
        if log is not None:
            log.close()


def test_real_stdio_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(app_main, "_windowed_log_dir", lambda: str(tmp_path))
    keep = open(tmp_path / "fake_tty.txt", "w")
    monkeypatch.setattr(sys, "stdout", keep)
    monkeypatch.setattr(sys, "stderr", keep)
    try:
        assert app_main._redirect_windowed_stdio() is None
        assert sys.stdout is keep
        assert not os.path.exists(tmp_path / "app.log")
    finally:
        keep.close()


def test_log_dir_is_per_platform(monkeypatch):
    monkeypatch.setattr(app_main.sys, "platform", "darwin")
    assert app_main._windowed_log_dir() == os.path.expanduser("~/Library/Logs/Photon Finish")
    monkeypatch.setattr(app_main.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    assert app_main._windowed_log_dir() == os.path.join(r"C:\Users\x\AppData\Local", "Photon Finish", "logs")
