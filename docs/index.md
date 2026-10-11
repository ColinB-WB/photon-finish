# Photon Finish: better Seestar photos, with less editing

Photon Finish turns the stacked color FITS from a ZWO Seestar S30, S30 Pro, or S50
into a finished photograph. It is for hobby astrophotographers who want to
spend their time capturing the sky—not learning a sequence of specialist
photo-editing applications.

Choose the folder copied from your Seestar, choose an output style, and choose
where the results should go. The app measures the frame, coordinates a careful
workflow through GraXpert, Siril, StarNet2, and ExifTool, and returns finished
images plus an understandable result check.

[Install the app](manual/installation.md){ .md-button .md-button--primary }
[Create a first photo](manual/first-photo.md){ .md-button }

<!-- PLACEHOLDER: before/after images pending from the owner. Restore the
"## Example results" heading above the table when adding them. Same files as
the README (docs/images/), referenced relative to docs/. Replace this comment
with the table below once the files exist.

| Seestar's own JPG | Photon Finish |
|---|---|
| ![M8 from the Seestar app](images/m8-before.jpg) | ![M8 from Photon Finish](images/m8-after.jpg) |
| ![M81 from the Seestar app](images/m81-before.jpg) | ![M81 from Photon Finish](images/m81-after.jpg) |
| ![M13 from the Seestar app](images/m13-before.jpg) | ![M13 from Photon Finish](images/m13-after.jpg) |

Same stacked capture in both columns; only the processing differs.
-->

## What you can expect

- A cleaner, more even sky after light-pollution gradient removal.
- Less noise while preserving stars and faint structure.
- A measured stretch that reveals detail without simply making everything
  brighter.
- Color treatment appropriate to the frame and its filter.
- Satellite-trail repair when the detected trail can be treated safely.
- Star-reduced or starless versions for suitable extended objects.
- A galaxy close-up when the subject and data warrant one.
- A high-quality TIFF, PNG, share-ready JPG, embedded metadata, and a processing
  record.

[How it works](manual/how-it-works.md) shows each step: what changes in your
photo, which application does it, and what Photon Finish decides and why.

The result is not generated or painted. The app works only with signal already
present in the stacked FITS. If the capture is limiting, it says so.

## Built on excellent tools

The Seestar telescopes make deep-sky imaging unusually approachable. GraXpert,
Siril, StarNet2, and ExifTool give astrophotographers extraordinarily capable
open-source tools for developing those captures.

This project is a small convenience layer around that ecosystem. It automates a
repeatable path through those tools for people who do not want to learn and
operate each one separately. It is possible only because their developers and
communities did the hard work first.

Learn more in [Acknowledgements](acknowledgements.md).

## What it is—and is not

The processor:

- starts from a qualifying Seestar stacked color `.fit` or `.fits` file;
- runs locally and treats the source library as read-only;
- supports the S30, S30 Pro, and S50;
- makes deterministic, explainable choices from measurements, filter
  information, and bounded catalog evidence; and
- provides an optional recipe when you want to adjust one result.

It does not:

- control the telescope or help with capture;
- calibrate or stack raw subframes;
- edit the Seestar's stock JPG;
- upload your image to a service;
- promise to recover signal that was never captured; or
- replace a hands-on workflow for photographers who want detailed manual
  control.

Read the complete [Capabilities and limits](manual/capabilities-and-limits.md).

## Where to go next

- **New here?** Start with the [User manual](manual/index.md).
- **Checking your computer?** See [Compatibility](compatibility.md).
- **Something went wrong?** See [Troubleshooting](manual/troubleshooting.md).
- **Curious about privacy?** See [Privacy and network use](privacy-and-network.md).
- **Want to contribute?** See the [Developer guide](development/index.md).
