"""OS-integration helpers that shell out to a separate process.

`pick_directory` is the BROWSER-MODE folder picker (spec §6): window mode uses the
native pywebview dialog instead; this subprocess picker — including the hard-won
Windows Timer/FindWindow foreground fix (v1.0.0 pulled-exe bug) — is the fallback
for dev and run-from-source use. Moved verbatim from seestar/gui.py @ f425414.
`open_in_editor` hands a recipe file to the OS default text editor.
"""

from __future__ import annotations

import os
import subprocess
import sys


def pick_directory(current: str = "") -> str:
    """Open the OS's native folder chooser and return the picked path; if the user cancels (or
    no dialog is available) keep the current value. Cross-platform and ALWAYS run in a SEPARATE
    PROCESS: Gradio event handlers run on worker threads, where an in-process GUI dialog (tkinter
    / WinForms) crashes because those toolkits require the main thread. A child process owns its
    own main thread, so this is safe everywhere.
      - macOS:   AppleScript Finder dialog via `osascript`.
      - Windows: native FolderBrowserDialog via PowerShell (no tkinter dependency; -STA required
                 because the dialog is a WinForms/COM control).
      - other:   tkinter askdirectory in a fresh `python -c` child."""
    current = (current or "").strip()
    start = current if current and os.path.isdir(current) else os.path.expanduser("~")
    try:
        if sys.platform == "darwin":
            # Pass `start` as an argv item (never interpolated into AppleScript
            # source) — same hardening as __init__._osascript_argv. A path
            # containing a double-quote can otherwise inject AppleScript, and
            # `start` is client-supplied via /api/pick-folder in browser mode.
            script = (
                "on run argv\n"
                'return POSIX path of (choose folder with prompt "Select folder" '
                "default location POSIX file (item 1 of argv))\n"
                "end run"
            )
            r = subprocess.run(
                ["osascript", "-e", script, start],
                capture_output=True,
                text=True,
                timeout=300,
            )
            picked = r.stdout.strip().rstrip("/") or r.stdout.strip()
        elif sys.platform.startswith("win"):
            # v1.0.0 real bug: a TopMost owner form alone is not enough, and neither
            # is foregrounding the OWNER before ShowDialog -- confirmed by live
            # reproduction that the actual FolderBrowserDialog window does not
            # inherit that foreground status once Windows creates it (a background
            # child process cannot steal foreground focus; the OS's foreground-lock
            # security feature blocks it). The dialog genuinely opens (a real,
            # visible "Browse For Folder" window exists) but never becomes the
            # foreground window, so it sits there unnoticed until the 5-minute
            # subprocess timeout kills it. Fix: a WinForms Timer ticking during the
            # modal ShowDialog() message loop, which finds the dialog's own window
            # by title once it exists and forces THAT window (not the owner) to the
            # foreground. The owner form itself is 1x1, borderless, and positioned
            # off-screen -- it must still be shown (for ownership/Z-order), but
            # showing it at default size previously flashed a visible blank window.
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "Add-Type @'\n"
                "using System;\n"
                "using System.Runtime.InteropServices;\n"
                "public class SeestarWin32 {\n"
                '    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);\n'
                '    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);\n'
                '    [DllImport("user32.dll", CharSet = CharSet.Auto)] public static extern IntPtr FindWindow(string lpClassName, string lpWindowName);\n'
                "}\n"
                "'@\n"
                "$o = New-Object System.Windows.Forms.Form -Property @{TopMost=$true; ShowInTaskbar=$false;"
                " Width=1; Height=1; FormBorderStyle='None'; StartPosition='Manual';"
                " Location=(New-Object System.Drawing.Point(-2000,-2000))};"
                "$o.Show();"
                "$timer = New-Object System.Windows.Forms.Timer;"
                "$timer.Interval = 150;"
                "$timer.Add_Tick({"
                "  $h = [SeestarWin32]::FindWindow($null, 'Browse For Folder');"
                "  if ($h -ne [IntPtr]::Zero) {"
                "    [SeestarWin32]::ShowWindow($h, 9) | Out-Null;"
                "    [SeestarWin32]::SetForegroundWindow($h) | Out-Null;"
                "    $timer.Stop();"
                "  }"
                "});"
                "$timer.Start();"
                "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
                f"$d.SelectedPath = {_ps_quote(start)};"
                "$result = $d.ShowDialog($o);"
                "$timer.Stop();"
                "if($result -eq [System.Windows.Forms.DialogResult]::OK)"
                "{[Console]::Out.Write($d.SelectedPath)}"
            )
            r = subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-Command", ps],
                capture_output=True,
                text=True,
                timeout=300,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            picked = r.stdout.strip()
        else:
            code = (
                "import tkinter as tk, sys\n"
                "from tkinter import filedialog\n"
                "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
                f"p = filedialog.askdirectory(initialdir={start!r})\n"
                "sys.stdout.write(p or '')\n"
            )
            r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300)
            picked = r.stdout.strip()
        return picked or current
    except Exception:
        return current


def _ps_quote(s: str) -> str:
    """Single-quote a string for PowerShell (escape embedded single quotes by doubling)."""
    return "'" + s.replace("'", "''") + "'"


def open_in_editor(path: str) -> str:
    """Open the recipe in the OS default text editor.

    Failure shows the path so the user can open it by hand.
    """
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: S606 — deliberate: hand off to the OS editor
        elif sys.platform == "darwin":
            r = subprocess.run(["open", path], check=False)
            if r.returncode != 0:
                return f"⚠️ Couldn't open an editor (exit {r.returncode}). The file is at: {path}"
        else:
            r = subprocess.run(["xdg-open", path], check=False)
            if r.returncode != 0:
                return f"⚠️ Couldn't open an editor (exit {r.returncode}). The file is at: {path}"
        return (
            f"📝 Opened {os.path.basename(path)} in your text editor. "
            "Edit a 'your_value' setting, save the file, then choose Re-create using saved adjustments."
        )
    except Exception as e:
        return f"⚠️ Couldn't open an editor ({e}). The file is at: {path}"
