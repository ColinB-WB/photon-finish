#!/bin/bash
# One-command macOS release: authorize signing -> ensure notary credential -> build -> sign ->
# notarize -> staple -> DMG. Run this in YOUR OWN Terminal.app (it needs your normal internet
# connection and your keychain). It reads the signing identity + Team ID from
# SEESTAR_SIGN_IDENTITY / SEESTAR_TEAM_ID in your environment (set both before running — see
# below); it still prompts interactively for your Mac login password and, the first time, your
# Apple ID email — you never type those secrets into the command itself.
#
#   bash packaging/release_mac.sh
set -e
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

# Signing identity + Team ID come from the environment — never hard-code them here
# (this file ships in the public repo). Set both in your own shell before running:
#   export SEESTAR_SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
#   export SEESTAR_TEAM_ID="TEAMID"
TEAM_ID="${SEESTAR_TEAM_ID:?Set SEESTAR_TEAM_ID to your Apple Developer Team ID}"
: "${SEESTAR_SIGN_IDENTITY:?Set SEESTAR_SIGN_IDENTITY, e.g. 'Developer ID Application: Your Name (TEAMID)'}"
PROFILE="seestar-notary"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

echo "════════════════════════════════════════════════════════════"
echo " Photon Finish — build a signed, notarized DMG"
echo "════════════════════════════════════════════════════════════"

echo
echo "[1/4] Authorize code signing (stops the repeated keychain prompt)…"
echo "      Type your Mac login password at the prompt (it stays hidden)."
printf "      Mac login password: "
read -rs PW; echo
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$PW" "$KEYCHAIN" >/dev/null \
  && echo "      ✓ signing authorized" \
  || { echo "      ✗ could not authorize — wrong password, or use Keychain Access (see packaging/README.md)"; unset PW; exit 1; }
unset PW

echo
echo "[2/4] Notarization credential…"
if xcrun notarytool history --keychain-profile "$PROFILE" >/dev/null 2>&1; then
  echo "      ✓ '$PROFILE' already stored"
else
  echo "      Not stored yet — let's create it. You need an app-specific password from"
  echo "      appleid.apple.com → Sign-In & Security → App-Specific Passwords."
  printf "      Your Apple ID email: "
  read -r APPLEID
  xcrun notarytool store-credentials "$PROFILE" --apple-id "$APPLEID" --team-id "$TEAM_ID"
  echo "      ✓ credential stored"
fi

echo
echo "[3/4] Building the app (local, ~1–2 min)…"
bash packaging/build_app.sh

echo
echo "[4/4] Signing + notarizing + packaging (uploads to Apple; a few minutes)…"
bash packaging/sign_notarize_dmg.sh

echo
echo "🎉  Done. Ship this file to your friends:"
echo "    $HERE/dist/Photon Finish.dmg"
