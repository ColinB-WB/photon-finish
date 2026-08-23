# Build "Photon Finish" as a single self-contained Windows .exe (its own Python + all
# deps) via PyInstaller --onefile -- one file to download and double-click, matching
# the Mac DMG's no-install-step feel. Trade-off: --onefile self-extracts to a fresh
# temp directory on every launch, so startup is a few seconds slower than --onedir.
#
# Mirrors packaging/build_app.sh (macOS): a DEDICATED build venv (.venv-build) so the
# frozen app is built from a clean, known set of dependencies rather than whatever is
# in the dev .venv. Uses Python 3.12 specifically (via the `py` launcher), matching the
# macOS build lock's interpreter and avoiding the astropy/wcsaxes + pytest interaction
# noted there (this script's venv never installs pytest, so that failure mode does not
# apply here either).
#
# Unsigned: Windows SmartScreen will show a "Windows protected your PC" prompt on first
# run for anyone who downloads this (see README.md for the documented explanation).
# There is no free fix -- only a paid code-signing certificate removes it -- and signing
# is deliberately out of scope here, matching the Mac script split (build vs.
# sign_notarize_dmg.sh).
#
#   powershell -File packaging\build_app_windows.ps1
#
$ErrorActionPreference = "Stop"

$Here = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Here

# This machine's antivirus (Norton) does TLS interception, re-signing every outbound HTTPS
# connection with its own root cert. pip bundled with a fresh Python 3.12 install (24.0)
# does not trust that root by default and fails every PyPI fetch with a cert-verify error;
# point it at Norton's cert bundle explicitly. Harmless / a no-op on a machine without this AV
# (the pip calls below simply verify against this file instead of pip's vendored bundle).
$NortonCert = "C:\ProgramData\Norton\Antivirus\wscert.pem"
if (Test-Path $NortonCert) {
    $env:SSL_CERT_FILE = $NortonCert
    $env:PIP_CERT = $NortonCert
}

$BuildVenv = Join-Path $Here ".venv-build"
$AppName = "Photon Finish"

# Reproducible-ish build env: always start from a fresh venv so a stale/hand-modified
# .venv-build never leaks into a release build. (Unlike the macOS build, this does not
# yet install from a hashed lock file -- packaging/requirements-build.in is unpinned on
# Windows today; hardening this to a committed, hashed lock is a documented follow-up,
# see docs/development/releasing.md.)
Write-Host "Recreating the build venv (.venv-build) with Python 3.12 -- no pytest..."
if (Test-Path $BuildVenv) { Remove-Item -Recurse -Force $BuildVenv }
py -3.12 -m venv $BuildVenv
$Py = Join-Path $BuildVenv "Scripts\python.exe"

& $Py -m pip install --quiet --upgrade pip
& $Py -m pip install --quiet ".[gui,desktop]" pyinstaller
Write-Host "Build venv: $(& $Py --version)"

Write-Host "Cleaning previous build..."
foreach ($p in @("build", "dist\$AppName", "dist\$AppName.exe", "$AppName.spec")) {
    if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}

Write-Host "Running PyInstaller (this takes a few minutes)..."
& $Py -m PyInstaller --noconfirm --clean `
  --name "$AppName" `
  --windowed `
  --onefile `
  --add-data "config.yaml;." `
  --add-data "seestar\data\known_targets.json;seestar\data" `
  --add-data "seestar\gui\static;seestar\gui\static" `
  --collect-data seestar `
  --hidden-import seestar.data `
  --collect-submodules uvicorn `
  --copy-metadata photon-finish `
  --hidden-import seestar `
  --hidden-import PIL `
  --hidden-import yaml `
  --hidden-import astropy.io.fits `
  --hidden-import astropy.wcs `
  --exclude-module matplotlib `
  --exclude-module scipy `
  --exclude-module IPython `
  --exclude-module pytest `
  --exclude-module tkinter `
  packaging\app_main.py

if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

$VersionLine = Select-String -Path "$Here\seestar\__init__.py" -Pattern '^__version__ = "(.*)"'
if (-not $VersionLine) { throw "Could not read __version__ from seestar\__init__.py" }
$Version = $VersionLine.Matches[0].Groups[1].Value

Write-Host ""
Write-Host "Built: dist\$AppName.exe  (version $Version, single-file, no install step)"
