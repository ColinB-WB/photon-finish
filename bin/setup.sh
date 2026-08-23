#!/bin/bash
# Build the Python environment for Photon Finish.
# Path-independent (works wherever this folder lives) and idempotent (safe to re-run).
set -e
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

echo "Photon Finish — setup"
echo "==================="

if ! command -v python3 >/dev/null 2>&1; then
  echo "❌ Python 3 is required but was not found on this Mac."
  echo "   Install it from https://www.python.org/downloads/macos/ , then run setup again."
  exit 1
fi
echo "Using $(python3 --version) at $(command -v python3)"

if [ ! -x ".venv/bin/python" ]; then
  echo "Creating the Python environment (.venv)…"
  python3 -m venv .venv
fi
echo "Installing Photon Finish and its Python dependencies…"
./.venv/bin/python -m pip install --upgrade pip >/dev/null
./.venv/bin/python -m pip install -e ".[gui]"
echo "✅ Python environment ready."

# Check — but do NOT install — the external apps the engine shells out to.
echo
echo "Checking the external apps the engine uses (install these yourself — see INSTALL.md):"
missing=0
check() {
  if [ -e "$2" ] || command -v "$2" >/dev/null 2>&1; then
    echo "  ✅ $1"
  else
    echo "  ⚠️  $1 not found — expected at: $2"
    missing=1
  fi
}
check "GraXpert" "/Applications/GraXpert.app/Contents/MacOS/GraXpert"
check "Siril"    "/Applications/Siril.app/Contents/MacOS/siril-cli"
check "StarNet2" "/usr/local/bin/starnet2"
check "exiftool" "exiftool"
if [ "$missing" = "1" ]; then
  echo
  echo "  ⚠️  Some external apps are missing. The GUI will still launch, but a render"
  echo "      needs all of them. See INSTALL.md for where to get each one."
fi
echo
echo "Setup complete. To start the app, double-click \"Photon Finish.command\"."
