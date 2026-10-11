# Capabilities and limits

## Supported in v1

- ZWO Seestar S30, S30 Pro, and S50 stacked, three-channel color FITS files.
- Seestar library folders and single-target folders.
- Compact and emission nebulae, galaxies, globular and open-cluster fields.
- Showcase, Natural, Reference, and Print output styles.
- Local GUI and command-line workflows.
- A macOS packaged application, and source operation on macOS and Windows.
- A bundled, offline Messier and Caldwell target catalog.
- Optional network-assisted object classification using WCS sky coordinates.

## Best-effort areas

- Very large, frame-filling nebulae are difficult because little empty sky
  remains for estimating the background.
- Satellite trails can be detected and repaired only when the evidence and
  geometry support a safe correction. A remnant may remain.
- Star separation can leave halos or traces around difficult stars.
- Printed appearance depends on display calibration, printer, ink, paper, and
  viewing light.
- Object identity outside the bundled catalog may be unavailable offline or
  ambiguous in crowded fields.

## Not supported in v1

- Telescope control, target selection, capture planning, or live stacking.
- Calibration or stacking of raw subframes.
- Combining multiple sessions; the latest qualifying session is used.
- Mono data, narrowband channel combination, mosaics, or non-Seestar cameras.
- Editing the stock Seestar JPG as the source.
- Cloud accounts, image uploads, or remote rendering.
- A packaged Windows application (coming soon — the source workflow is
  available now).
- Target-specific secret recipes or processing based only on an object's name.

## What software cannot fix

Processing can reveal captured signal and reduce some artifacts. It cannot
recreate detail lost to:

- inadequate integration;
- severe defocus or tracking problems;
- clipped highlights;
- clouds, dew, or obstruction;
- an incorrect field; or
- signal absent from the original stack.

The result check distinguishes a processing problem from a capture-limited
outcome where it can. When the evidence is uncertain, the application is
designed to qualify or abstain rather than invent confidence.

## A note for experienced editors

This project optimizes for consistency and ease, not maximum manual control on
every target. Skilled users of GraXpert, Siril, StarNet2, PixInsight, Photoshop,
or other tools may produce a result that better matches their taste. The
high-bit-depth output and recipe are available if you want to continue by hand.
