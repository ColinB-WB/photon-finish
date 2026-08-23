#!/bin/bash
# Build "Photon Finish.app" — a self-contained bundle (its own Python + all deps) via PyInstaller.
# Unsigned here; signing/notarization happen in packaging/sign_notarize_dmg.sh.
#
# Uses a DEDICATED build venv (.venv-build) that deliberately has NO pytest: astropy's
# wcsaxes does `pytest.importorskip("matplotlib")` at import, and with pytest present that
# raises a pytest "Skipped" (a BaseException) that PyInstaller's astropy hook doesn't catch,
# aborting the build. Without pytest the same line is a plain ImportError, which the hook
# handles gracefully. The dev .venv (with pytest, for the test suite) is left untouched.
#
#   bash packaging/build_app.sh
set -e
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

BUILD_VENV="$HERE/.venv-build"
PY="$BUILD_VENV/bin/python"
APP_NAME="Photon Finish"
BUNDLE_ID="com.workingbackwards.photon-finish"

# Reproducible release env: the build venv is RECREATED from the committed, hashed macOS
# lock on every build — a pre-existing (possibly stale or hand-modified) .venv-build must
# never be reused, or the app would build from an environment the lock doesn't describe.
# pip is the one bundled with the recorded Python installation (no unpinned upgrade), and
# the local project is installed with --no-deps --no-build-isolation so the build uses the
# lock's setuptools instead of letting PEP 517 isolation resolve a newer one.
# See docs/development/releasing.md, "Pinned frozen-app build environment".
echo "Recreating the build venv (.venv-build) from the committed lock — no pytest…"
rm -rf "$BUILD_VENV"
python3 -m venv "$BUILD_VENV"
"$PY" -m pip install --quiet --require-hashes -r "$HERE/packaging/requirements-build-macos.lock"
"$PY" -m pip install --quiet --no-deps --no-build-isolation -e .
echo "Build venv: $("$PY" --version)"

echo "Cleaning previous build…"
rm -rf build "dist/$APP_NAME.app" "dist/$APP_NAME" "$APP_NAME.spec"

echo "Running PyInstaller (this takes a few minutes)…"
"$PY" -m PyInstaller --noconfirm --clean \
  --name "$APP_NAME" \
  --windowed \
  --osx-bundle-identifier "$BUNDLE_ID" \
  --add-data "config.yaml:." \
  --add-data "seestar/data/known_targets.json:seestar/data" \
  --add-data "seestar/gui/static:seestar/gui/static" \
  --collect-data seestar \
  --hidden-import seestar.data \
  --collect-submodules uvicorn \
  --copy-metadata photon-finish \
  --hidden-import seestar \
  --hidden-import PIL \
  --hidden-import yaml \
  --hidden-import astropy.io.fits \
  --hidden-import astropy.wcs \
  --exclude-module matplotlib \
  --exclude-module scipy \
  --exclude-module IPython \
  --exclude-module pytest \
  --exclude-module tkinter \
  packaging/app_main.py

PLIST="dist/$APP_NAME.app/Contents/Info.plist"
echo "Tuning Info.plist (version)…"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$HERE/seestar/__init__.py")"
[ -n "$VERSION" ] || { echo "❌ Could not read __version__ from seestar/__init__.py"; exit 1; }
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $VERSION" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $VERSION" "$PLIST"

echo
echo "Built: dist/$APP_NAME.app"
