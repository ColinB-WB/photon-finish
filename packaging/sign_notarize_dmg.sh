#!/bin/bash
# Sign, notarize, and package "dist/Photon Finish.app" into a distributable DMG.
# Both the .app AND the .dmg are notarized + stapled, so the app runs with no Gatekeeper
# prompt even offline / after being dragged out of the DMG.
#
# Prereqs:
#   1) bash packaging/build_app.sh        (produces dist/Photon Finish.app)
#   2) one-time, in your own Terminal:
#        xcrun notarytool store-credentials "seestar-notary" \
#          --apple-id "<your-apple-id>" --team-id "<your-team-id>"
#
# Usage:  bash packaging/sign_notarize_dmg.sh
set -e
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

# Signing identity + Team ID come from the environment — never hard-code them here
# (this file ships in the public repo). Set both in your own shell before running:
#   export SEESTAR_SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
#   export SEESTAR_TEAM_ID="TEAMID"
IDENTITY="${SEESTAR_SIGN_IDENTITY:?Set SEESTAR_SIGN_IDENTITY, e.g. 'Developer ID Application: Your Name (TEAMID)'}"
TEAM_ID="${SEESTAR_TEAM_ID:?Set SEESTAR_TEAM_ID to your Apple Developer Team ID}"

APP_NAME="Photon Finish"
APP="dist/$APP_NAME.app"
DMG="dist/$APP_NAME.dmg"
ENTITLEMENTS="packaging/entitlements.plist"
PROFILE="seestar-notary"

[ -d "$APP" ] || { echo "❌ $APP not found — run packaging/build_app.sh first."; exit 1; }
if ! xcrun notarytool history --keychain-profile "$PROFILE" >/dev/null 2>&1; then
  echo "❌ Notary credential '$PROFILE' not found. In your own Terminal, run once:"
  echo "   xcrun notarytool store-credentials \"$PROFILE\" --apple-id \"<you>\" --team-id \"$TEAM_ID\""
  exit 1
fi

echo "=== 1/8  Signing nested Mach-O inside-out (dylibs/.so, then nested executables) ==="
find "$APP" -type f \( -name "*.dylib" -o -name "*.so" \) -print0 \
  | xargs -0 -P4 -I {} codesign --force --timestamp --options runtime --sign "$IDENTITY" {}
find "$APP/Contents" -type f -perm +111 ! -name "*.dylib" ! -name "*.so" -print0 \
  | while IFS= read -r -d '' f; do
      file "$f" | grep -q "Mach-O" && \
        codesign --force --timestamp --options runtime --sign "$IDENTITY" "$f" || true
    done

echo "=== 2/8  Signing the app bundle (hardened runtime + entitlements) ==="
codesign --force --timestamp --options runtime --entitlements "$ENTITLEMENTS" \
  --sign "$IDENTITY" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

echo "=== 3/8  Notarizing the app (zip upload; waits for Apple) ==="
ZIP="dist/$APP_NAME.zip"; rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"
xcrun notarytool submit "$ZIP" --keychain-profile "$PROFILE" --wait
rm -f "$ZIP"

echo "=== 4/8  Stapling the app ==="
xcrun stapler staple "$APP"

echo "=== 5/8  Building the DMG (from the stapled app + Applications shortcut) ==="
rm -f "$DMG"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"

echo "=== 6/8  Signing the DMG ==="
codesign --force --timestamp --sign "$IDENTITY" "$DMG"

echo "=== 7/8  Notarizing the DMG ==="
xcrun notarytool submit "$DMG" --keychain-profile "$PROFILE" --wait

echo "=== 8/8  Stapling the DMG ==="
xcrun stapler staple "$DMG"

echo
echo "✅ Done: $DMG"
echo "Gatekeeper assessment of the app:"
spctl -a -vvv -t exec "$APP" 2>&1 || true
echo "Staple validation:"
xcrun stapler validate "$DMG" 2>&1 || true
