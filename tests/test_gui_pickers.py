"""Subprocess folder pickers + editor opener (no real dialogs spawned).

These are the browser-mode pickers (spec §6) — window mode uses the native
pywebview dialog. No gradio import: this file must run in the post-Task-7
gradio-free environment too."""

import pytest  # noqa: F401

from seestar.gui import pickers


def test_pick_directory_windows_suppresses_console_flash(monkeypatch):
    """The PowerShell folder-picker child process must not flash a console window,
    same CREATE_NO_WINDOW convention as the tool-adapter subprocess calls
    (seestar/adapters/_external_process.py, seestar/qa.py)."""
    monkeypatch.setattr(pickers.sys, "platform", "win32")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)

        class R:
            stdout = "C:\\chosen"

        return R()

    monkeypatch.setattr(pickers.subprocess, "run", fake_run)

    result = pickers.pick_directory("")

    assert result == "C:\\chosen"
    assert captured.get("creationflags") == getattr(pickers.subprocess, "CREATE_NO_WINDOW", 0)


def test_pick_directory_windows_forces_the_dialog_to_the_foreground(monkeypatch):
    """Real-world bug (v1.0.0): the folder-picker's dialog opens but never
    becomes the foreground window, so the user never sees or can interact with
    it -- it just sits there until the 5-minute subprocess timeout kills it.
    Windows' foreground-lock security feature silently blocks a
    background-launched process from stealing focus. Live reproduction found
    this is true even when the OWNER form is explicitly shown and foregrounded
    before ShowDialog() -- the actual FolderBrowserDialog window (created
    separately, once Windows processes the ShowDialog call) does not inherit
    that foreground status. The dialog itself is genuinely visible (confirmed:
    a real "Browse For Folder" window exists in the window list) just never
    focused. Fix: a WinForms Timer ticking during the modal message loop finds
    the dialog's own window by title ("Browse For Folder") once it exists and
    forces THAT window to the foreground -- confirmed via live reproduction
    this actually grabs real OS-level foreground status in this exact
    environment (no console, CREATE_NO_WINDOW, background-launched), unlike
    foregrounding the owner alone. This test locks in that the generated
    script starts a Timer, searches for the dialog by its exact title via
    FindWindow, and calls SetForegroundWindow on the result -- and that the
    owner form is shown at 1x1 off-screen (not default size), since showing it
    at full size previously flashed a visible, unwanted blank window."""
    monkeypatch.setattr(pickers.sys, "platform", "win32")
    captured_cmd = []

    def fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)

        class R:
            stdout = "C:\\chosen"

        return R()

    monkeypatch.setattr(pickers.subprocess, "run", fake_run)

    pickers.pick_directory("")

    script = captured_cmd[-1]
    assert "SetForegroundWindow" in script
    assert "ShowWindow" in script
    assert "FindWindow" in script
    assert "'Browse For Folder'" in script
    assert "$o.Show()" in script
    # The owner form must stay invisible to the user -- 1x1, borderless, off-screen --
    # not the default-sized blank window an earlier fix attempt accidentally showed.
    assert "Width=1; Height=1" in script
    assert "FormBorderStyle='None'" in script
    # The timer must be armed and the dialog opened while it's still running -- if
    # ShowDialog ran before the timer started, the dialog could open and close
    # (a very fast user click) before any activation attempt ever fires.
    assert script.index("$timer.Start()") < script.index("ShowDialog")
    assert script.index("$timer.Add_Tick") < script.index("$timer.Start()")


def test_pick_directory_macos_passes_start_dir_as_argv_not_interpolated(monkeypatch):
    """macOS folder picker must pass the start dir as an osascript argv item, never
    interpolate it into AppleScript source (injection via a path containing a quote,
    reachable through /api/pick-folder in browser mode)."""
    monkeypatch.setattr(pickers.sys, "platform", "darwin")
    monkeypatch.setattr(pickers.os.path, "isdir", lambda p: True)
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd

        class R:
            stdout = "/chosen"

        return R()

    monkeypatch.setattr(pickers.subprocess, "run", fake_run)
    evil = '/tmp/a" & (do shell script "touch /tmp/pwned") & "'
    pickers.pick_directory(evil)
    cmd = captured["cmd"]
    assert cmd[0] == "osascript"
    assert cmd[-1] == evil  # the path is the trailing ARGV item, verbatim
    assert "on run argv" in cmd[2]  # read via argv, not interpolated
    assert evil not in cmd[2]  # the evil path never appears in the script source


class _FakeResult:
    def __init__(self, returncode):
        self.returncode = returncode


def test_open_in_editor_reports_nonzero_exit(tmp_path, monkeypatch):
    # A non-Windows `open`/`xdg-open` can exit non-zero (e.g. no default app registered)
    # without raising -- that used to be reported as a false success.
    p = tmp_path / "X1_recipe.json"
    p.write_text("{}")
    monkeypatch.setattr(pickers.sys, "platform", "darwin")
    monkeypatch.setattr(pickers.subprocess, "run", lambda *a, **k: _FakeResult(1))
    msg = pickers.open_in_editor(str(p))
    assert "Couldn't open an editor" in msg
    assert str(p) in msg
    assert "**" not in msg and "`" not in msg


def test_open_in_editor_success_message_on_zero_exit(tmp_path, monkeypatch):
    p = tmp_path / "X1_recipe.json"
    p.write_text("{}")
    monkeypatch.setattr(pickers.sys, "platform", "darwin")
    monkeypatch.setattr(pickers.subprocess, "run", lambda *a, **k: _FakeResult(0))
    msg = pickers.open_in_editor(str(p))
    assert "Opened" in msg
    assert "**" not in msg and "`" not in msg
