# Packaging — self-contained desktop builds (macOS + Windows)

Produces a self-contained, double-clickable Mac app (its own Python + all deps bundled, so
recipients need **no** Python) that is code-signed with a Developer ID, notarized by Apple,
and delivered in a notarized DMG — so non-technical users see **no Gatekeeper prompt**. They
still install the four external apps manually (GraXpert / Siril / StarNet2 /
ExifTool — see `INSTALL.md`).

## One-time per machine
Store the notarization credential in your keychain (in your own Terminal, not in a script —
it prompts for an **app-specific password** from appleid.apple.com):

```
xcrun notarytool store-credentials "seestar-notary" \
  --apple-id "<your-apple-id>" --team-id "<your-team-id>"
```

## Build + release
```
bash packaging/build_app.sh          # -> dist/Photon Finish.app  (unsigned, self-contained)
bash packaging/sign_notarize_dmg.sh  # signs, notarizes, staples; -> dist/Photon Finish.dmg
```
Ship `dist/Photon Finish.dmg`. Recipients open it, drag the app to Applications, done.

## How it works / why each piece
- **build_app.sh** — PyInstaller freeze into a `.app`. Recreates a dedicated `.venv-build`
  from the committed, hashed lock `requirements-build-macos.lock` on every build
  (bundled pip, `--require-hashes`, then the local project with
  `--no-deps --no-build-isolation`; a pre-existing venv is never reused. Lock
  regeneration is a maintainer action — see `docs/development/releasing.md`, "Pinned
  frozen-app build environment"). (Runtime deps + PyInstaller, **no pytest** — astropy's `wcsaxes` does
  `pytest.importorskip("matplotlib")`, which with pytest present raises a `Skipped`
  BaseException the astropy hook doesn't catch; without pytest it's a handled ImportError).
  Collects the `seestar.gui` package data (the static front end under
  `seestar/gui/static/`, belt-and-suspenders with `--add-data`); bundles `config.yaml`;
  excludes matplotlib/scipy/IPython/tkinter (unused). The app is a normal windowed app
  (Dock icon + menu bar; `LSUIElement` is NOT set) — it opens its own native window via
  pywebview, and the user quits from the window/menu like any other Mac app.
- **app_main.py** — frozen entry point; hands off to `seestar.gui.main()`, which acquires
  the single-instance lock (a second launch shows an actionable "already open" dialog),
  starts the local job server on an ephemeral port, and opens the app window (or a
  browser in `--browser` mode).
- **entitlements.plist** — hardened-runtime relaxations a bundled CPython needs (JIT/unsigned
  memory/library-validation) so numpy/astropy import after notarization.
- **sign_notarize_dmg.sh** — signs nested Mach-O inside-out, then the bundle (hardened runtime
  + entitlements + secure timestamp); notarizes and **staples the app** (works offline), then
  builds, signs, notarizes, and staples the DMG.

## Notes
- Signing identity / Team ID are read from the environment (`SEESTAR_SIGN_IDENTITY`,
  `SEESTAR_TEAM_ID`) — set both in your shell before running `sign_notarize_dmg.sh`.
  Use your own Apple Developer ID Application certificate and Team ID.
- Build outputs (`dist/`, `build/`, `.venv-build/`, `*.spec`) are git-ignored; only these
  scripts are tracked.

## Windows

```
powershell -File packaging\build_app_windows.ps1
```
Produces a single `dist\Photon Finish.exe` (PyInstaller `--onefile`; self-contained, own
Python + all deps — no folder, no zip, no install step, matching the Mac DMG's one-file
feel). Mirrors `build_app.sh`: a dedicated `.venv-build` recreated from scratch every run
(Python 3.12, no pytest, same astropy/wcsaxes reasoning as macOS), PyInstaller `--onefile`
freeze. **Unsigned** — there is no Windows equivalent of `sign_notarize_dmg.sh` yet, so
first launch shows a SmartScreen "Windows protected your PC" prompt; only a paid
code-signing certificate removes that.

**Always test by double-clicking the `.exe`, never by launching it from a terminal.** A
`--windowed` PyInstaller app launched with no console gets `sys.stdout`/`sys.stderr` set to
`None`; anything that touches them before `packaging/app_main.py`'s redirect-to-logfile guard
runs will crash silently in a way a terminal launch (which always supplies real stdio) cannot
reproduce. `%LOCALAPPDATA%\Photon Finish\logs\app.log` is where stdout/stderr land when there's
no console — check it first for any post-launch report.
