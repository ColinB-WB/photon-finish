#!/bin/bash
# Launch the Photon Finish GUI. Path-independent: works wherever this folder
# lives and for any user. Runs first-time setup automatically if the environment is missing.
set -e
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

if [ ! -x ".venv/bin/python" ]; then
  echo "First-time setup needed — building the Python environment…"
  echo
  bash "$HERE/bin/setup.sh"
  echo
fi

echo "Starting Photon Finish — a browser tab will open automatically (this setup"
echo "installs the browser-mode [gui] extra; the packaged app instead opens its own"
echo "native window). Only one Photon Finish can run at a time — a second launch"
echo "just reports that it's already open."
echo "KEEP THIS WINDOW OPEN while you work. Close it to quit — but while a photo is"
echo "being created, quitting (including Ctrl-C) is refused until it finishes."
echo
# The app takes an OS-held single-instance lock and opens a browser tab (or, with
# the desktop extra installed, a native app window) — seestar/gui/__init__.py main().
exec ./.venv/bin/python -m seestar.gui
