# Compatibility

## Telescopes and input

| Item | v1 support |
|---|---|
| ZWO Seestar S30 | Supported |
| ZWO Seestar S30 Pro | Supported |
| Qualifying stacked color FITS | Supported |
| Stock Seestar JPG as input | Not supported |
| Raw subframes | Not supported |
| Other telescopes or cameras | Not supported |

The file may use `.fit` or `.fits` and does not need a particular filename. The
application verifies the FITS content and header rather than trusting the name.

## Operating systems

| Platform | v1 route |
|---|---|
| macOS | Signed, notarized packaged app; source route for contributors |
| Windows 11 | Run from source; a packaged application is **not yet released** — it builds, but has not been verified hands-on on Windows |
| Linux | Not a supported v1 release platform |

The interface opens in its own native app window (macOS and Windows), not a browser
tab. A `--browser` flag is available for anyone who prefers a browser tab instead; a
browser-mode local page needs a current Safari, Chrome, Firefox, or Edge release, and
does not mean photographs are being uploaded — processing stays local either way.

**Windows requires the Microsoft Edge WebView2 Runtime**, which draws the app window.
It is preinstalled on Windows 11 and reaches Windows 10 through Microsoft Edge updates,
so nearly every machine already has it; it is otherwise a free download from
<https://developer.microsoft.com/microsoft-edge/webview2/>. If it is missing, Photon
Finish says so and stops rather than silently opening a browser tab — one execution
path, and an explained failure instead of a confusing one. A future packaged Windows
application must ensure this runtime at install time.

## Required applications

GraXpert, Siril, StarNet2, and ExifTool must be installed separately. The
processor checks and calls their command-line interfaces; it does not automate
their graphical interfaces.

Use `seestar doctor` to see the exact program path selected on your computer.

## Storage

Allow room for:

- the copied Seestar source;
- a 16-bit TIFF, PNG, JPG, and optional variants for each target; and
- temporary processing files during a run.

The output folder must be separate from the source folder. Debug operation from
the CLI creates additional intermediate files and can use substantially more
space.
