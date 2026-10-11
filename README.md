# Photon Finish

Photon Finish is an automated astrophotography processing pipeline for ZWO
Seestar S30, S30 Pro, and S50 stacked color FITS captures. It turns a Seestar stack
into a finished photograph—locally, automatically, and without changing the
original capture.

This is a hobby project for Seestar astrophotographers who enjoy taking the
photos more than learning a multi-application editing workflow. It coordinates
[GraXpert](https://graxpert.com), [Siril](https://siril.org),
[StarNet2](https://www.starnetastro.com), and
[ExifTool](https://exiftool.org) to remove gradients, reduce noise, reveal faint
detail, manage stars, prepare useful close-ups, and export finished images.

It is an experiment: an attempt to give Seestar owners better default photos
without learning GraXpert, Siril, StarNet2, and ExifTool. Feedback on what works
and what does not is very welcome—see [SUPPORT.md](SUPPORT.md).

The Seestar telescopes and these open-source tools are already remarkable. This
project does not replace them: it supplies an automated, measured workflow
across them. It would not exist without the work of ZWO and the communities
behind those tools.

<!-- PLACEHOLDER: before/after images pending from the owner. Restore the
"## Example results" heading above the table when adding them. Put the files in
docs/images/ (long edge about 1600 px, JPG, metadata stripped), then replace
this comment with the table below, one row per target.

| Seestar's own JPG | Photon Finish |
|---|---|
| ![M8 from the Seestar app](docs/images/m8-before.jpg) | ![M8 from Photon Finish](docs/images/m8-after.jpg) |
| ![M81 from the Seestar app](docs/images/m81-before.jpg) | ![M81 from Photon Finish](docs/images/m81-after.jpg) |
| ![M13 from the Seestar app](docs/images/m13-before.jpg) | ![M13 from Photon Finish](docs/images/m13-after.jpg) |

Same stacked capture in both columns; only the processing differs.
-->

## Is this for me?

It may be a good fit if you:

- have a ZWO Seestar S30, S30 Pro, or S50;
- save the telescope's stacked color `.fit` or `.fits` file;
- want a polished result without learning several photo-editing packages; and
- are comfortable letting the app make conservative choices from the image.

It may not be the right fit if you want complete manual control, want to
calibrate and stack raw subframes, or expect software to repair a poor capture.
The specialist tools are better places to learn a fully hands-on workflow.
Compact and emission nebulae, galaxies, and star clusters are the validated
core; very large, frame-filling nebulae remain best-effort.

## What it does

It starts from the Seestar's **stacked color FITS**, not the stock JPG, because
the FITS keeps far more of what the telescope recorded.

**GraXpert** removes light-pollution gradients and noise, **Siril** handles
color, brightness, and saturation, **StarNet2** separates the stars, and
**ExifTool** labels the finished files. Each of those tools has dozens of
settings, and the right ones change from photo to photo. Photon Finish looks at
each photo and makes those choices for you, automatically—you do not need to
know what the settings are. For example, it:

- keeps a nebula's red glow red, instead of letting color correction turn it
  pink;
- makes the sky dark without wiping out the faint stars in it;
- removes satellite and airplane streaks; and
- checks the finished photo for common problems—a gray or pitch-black sky,
  missing stars, or unnaturally bright color—and tells you if it finds one.

You get a 16-bit TIFF, PNG, and share-ready JPG, plus a processing record and
an adjustment recipe. Everything runs on your computer, and source captures are
treated as read-only.

Don't like the result? Your original is never changed. Try a gentler style,
change just the step you disagree with—every photo comes with a file listing
each choice and why it was made—or take the 16-bit TIFF into your own editor.

[See every step](docs/manual/how-it-works.md): what changes in your photo,
which application does it, and what Photon Finish decides and why.

The app does not control the telescope, capture images, stack raw subframes,
upload your photographs, or invent detail that the data does not contain.

## Start here

Photon Finish v1.1 supports:

- **macOS:** the release build is a signed, notarized app distributed in a DMG.
  [Download the latest release](https://github.com/ColinB-WB/photon-finish/releases/latest).
- **Windows 11:** a packaged application is coming soon. Run from source in
  the meantime.

Developers on either platform can also run from source.

All installations require GraXpert, Siril, StarNet2 (including its command-line
executable), and ExifTool to be installed separately.

Read:

1. [Installation](docs/manual/installation.md)
2. [Create your first photo](docs/manual/first-photo.md)
3. [User manual](docs/manual/index.md)

For a quick dependency check after a source installation:

```console
seestar doctor
```

Packaged Mac users do not need `seestar` on `PATH`; the app checks its helper
tools and reports anything missing.

## A few important promises

- **Your originals remain yours.** Choose a separate output folder; the app
  never edits or deletes the Seestar source library.
- **Your images stay on your computer.** The optional SIMBAD lookup sends only
  sky coordinates, never image data. A bundled Messier and Caldwell catalog
  supports many targets offline.
- **The photograph is the authority.** Catalog identity may help classify an
  uncertain field, but measurements and the FITS filter remain authoritative
  for processing.
- **Uncertainty is reported.** A qualified result is better than false
  confidence.

See [Privacy and network use](docs/privacy-and-network.md) and
[Capabilities and limits](docs/manual/capabilities-and-limits.md).

## How this differs from the Seestar app's built-in processing

The Seestar app now includes its own post-processing, and it keeps improving.
Its **AI Enhance** and **AI Denoise** tools give a one-tap, on-device result
that is genuinely convenient—if that meets your needs, use it. This project
exists for a different goal, and the two are not interchangeable.

- **Works from the stacked FITS with dedicated tools.** It processes the linear
  16-bit master through GraXpert, Siril, and StarNet, preserving the full
  dynamic range, and records what each stage did so you can inspect it.
- **Measured, never generative.** Some AI enhancement can add "detail" the data
  does not contain. This project only reveals what the photons support, and
  reports when a capture cannot be recovered—it never invents structure.
- **Transparent and repeatable.** Every result ships with a processing record
  and an adjustment recipe you can read, tune, and re-run. In-app sliders leave
  no comparable trail.
- **Standards-grade output.** A 16-bit TIFF, a lossless PNG, a share-ready JPG,
  and embedded capture metadata—not only an enhanced JPG.
- **Offline and non-destructive by design.** Processing runs on your own
  computer, and your source library is never modified (see the promises above).

If you want the fastest possible "good enough" image with no extra software,
the Seestar app is the right tool. If you want a faithful, inspectable result
from the same capture, that is what this project is for.

## For contributors

The engine is a deterministic functional core with an imperative shell around
the external tools. Start with:

- [Contributing](CONTRIBUTING.md)
- [Architecture](docs/development/architecture.md)
- [Validation](docs/development/validation.md)
- [Project tenets](docs/TENETS.md)

The detailed operator and engine contract remains in
[`AGENTS.md`](AGENTS.md) — the vendor-neutral entry point read by Codex,
Cursor, and other AI coding tools. [`CLAUDE.md`](CLAUDE.md) is a thin pointer
into the same content for Claude Code. Historical design records remain in
the private maintainer repository and are not part of the public project.

Photon Finish is the public project name. The Python distribution is
`photon-finish`; for v1 compatibility, its import package and command-line
interface remain `seestar`, and the macOS application is `Photon Finish.app`.

## Status and license

The current release is v1.1.1. See
[CHANGELOG.md](CHANGELOG.md) for release contents and
[SUPPORT.md](SUPPORT.md) for help.

The application is MIT licensed. Its bundled known-target catalog is derived
from OpenNGC and distributed under CC-BY-SA-4.0. External applications are
installed separately and keep their own licenses. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

This independent hobby project is not affiliated with or endorsed by ZWO,
GraXpert, Siril, StarNet, or ExifTool.
