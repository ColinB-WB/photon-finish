# Installation

Installation has two parts:

1. Install four specialist applications that do the image-processing work.
2. Install Photon Finish, which coordinates them.

## Install the four specialist applications

Each application has one job:

| Application | What it contributes |
|---|---|
| [GraXpert downloads](https://graxpert.com/) | Light-pollution gradient removal and denoising |
| [Siril downloads](https://siril.org/download/) | Cropping, color, stretching, saturation, and image operations |
| [StarNet2 command-line downloads](https://starnetastro.com/cli-tools/starnet/) | Separating stars from extended subjects |
| [ExifTool installation](https://exiftool.org/install.html) | Reading and writing descriptive metadata |

All four are required for a complete render. They are not bundled with this
project and remain governed by their own licenses.

!!! danger "Install the StarNet2 command-line package"
    StarNet2 has several different distribution routes. This processor requires
    the standalone **StarNet2 command-line (CLI) package** containing
    `starnet2` on macOS or `starnet2.exe` on Windows.

    A PixInsight module or a legacy graphical application by itself will not
    work. Use the link labeled **StarNet CLI tools** above and select the native
    installer for your operating system and processor.

### Which packages should I choose on macOS?

1. **GraXpert:** choose **macOS ARM64** for an Apple Silicon Mac or **macOS
   AMD64** for an Intel Mac. Install the application in `/Applications`.
2. **Siril:** choose the current **Apple Silicon macOS** or **Intel macOS**
   installer to match your Mac. v1 has been validated with Siril 1.4.4.
3. **StarNet2:** on the StarNet CLI page, choose the current:
   - **macOS ARM64 `.pkg`** for Apple Silicon; or
   - **macOS x64 `.pkg`** for Intel.

   The `.pkg` installer is the easiest route because it places `starnet2` and
   its support files in standard locations the processor recognizes. Do not
   choose only the PixInsight module.
4. **ExifTool:** download and run the official macOS `.pkg`. It installs the
   `exiftool` command-line program in a standard location.

### Which packages should I choose on Windows 11?

1. **GraXpert:** choose the **Windows AMD64** installer.
2. **Siril:** choose the current **64-bit UCRT64 Windows** installer.
3. **StarNet2:** on the StarNet CLI page, choose the current **Windows x64 CLI
   installer** whose filename ends in `_setup.exe`. The installer places
   `starnet2.exe` and its model files under `C:\Program Files\StarNet2`, which
   the processor checks automatically. Do not choose only the PixInsight module
   or a legacy GUI.
4. **ExifTool:** download the official 64-bit Windows executable package. Keep
   the accompanying `exiftool_files` folder beside the executable, rename
   `exiftool(-k).exe` to `exiftool.exe`, and place both under `C:\tools`:

```text
C:\tools\exiftool.exe
C:\tools\exiftool_files\
```

That location is detected automatically.

### Portable StarNet2 ZIP archives

The native StarNet2 installer is recommended. A portable CLI ZIP can also work,
but all extracted model and runtime files must stay together. You must then add
its executable folder to `PATH` or configure the full `starnet2` executable
path. Installing only the executable without its support files is not enough.

## macOS release application

The published v1 macOS release is distributed as a signed and notarized DMG.
You do not need Python or a terminal to use that application.

1. Install the four specialist applications above, using the StarNet2 CLI
   package.
2. Download `Photon Finish.dmg` from the
   [latest release](https://github.com/ColinB-WB/photon-finish/releases/latest)
   and open it.
3. Drag **Photon Finish** into **Applications**.
4. Open **Photon Finish** from Applications.
5. Follow [Create your first photo](first-photo.md).

The app opens in its own window, like any other Mac application. The processing
service runs on your Mac, not on a website — the window just happens to be a local
page, not a browser tab.

!!! note "Windows packaged application"
    A packaged Windows application is coming soon. For now, Windows users run
    the project from source — it's a few extra one-time setup steps below,
    but works the same way once installed.

## Windows 11 from source

1. Install the four specialist applications above, using the Windows StarNet2
   CLI installer.
2. Install Python 3.10 or newer from
   [python.org](https://www.python.org/downloads/windows/). Select **Add Python
   to PATH** during installation.
3. Download this repository as a ZIP and extract it, or clone it with Git.
4. Open PowerShell in the repository folder.
5. Install the application and local interface:

```powershell
py -m pip install -e ".[gui]"
```

6. Check the helper applications:

```powershell
py -m seestar.cli doctor
```

7. Start the interface:

```powershell
py -m seestar.gui
```

If the `py` command is unavailable, use `python` in the same commands.

## macOS from source

Developers may run the project from a checkout:

```console
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[gui]"
seestar doctor
seestar-gui
```

The repository also includes `setup.command` and `Photon Finish.command` for the
source-checkout workflow.

## Confirm a source installation

After installing the processor from source, run:

```console
seestar doctor
```

The report should show resolved paths for GraXpert, Siril, StarNet2, and
ExifTool. If one is missing, see
[The app cannot find a helper tool](troubleshooting.md#the-app-cannot-find-a-helper-tool).

Packaged Mac users do not need to run a Terminal command. The app looks in the
standard installation locations described above and reports any missing tool.
If you are comfortable using Terminal, this optional check confirms the two
command-line-only installations:

```console
/usr/local/bin/starnet2 --version
/usr/local/bin/exiftool -ver
```

## Updates

- For a packaged Mac release, install the newer DMG over the existing
  application.
- For a ZIP source installation, replace the source folder and reinstall.
- For a Git checkout, pull the approved release and reinstall:

```console
python -m pip install -e ".[gui]"
```
