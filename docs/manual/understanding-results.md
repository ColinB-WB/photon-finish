# Understand the results

Each target is saved in its own folder beneath the output location.

## Finished images

The exact set depends on the subject and selected style. Common files include:

- `<target>_final.tif` — high-bit-depth master for archiving or further editing;
- `<target>_final.png` — lossless, broadly viewable image;
- `<target>_final.jpg` — share-ready Recommended image;
- named JPG, PNG, or TIFF variants such as `closeup`, `stars-on`,
  `star-reduced`, `starless`, `natural`, `documentation`, or `print`; and
- an annotated JPG for the Reference style.

In filenames and technical records, the Reference style may be identified as
`documentation`; files containing `_documentation` are Reference-style
results.

Keep the TIFF if disk space permits. The JPG is convenient for messaging and
web use, but it is not the best future editing master.

## Processing records

The folder also contains records such as:

- `<target>_recipe.json` — plain-language adjustable settings and why they were
  chosen;
- `<target>_notes.json` — measured decisions and processing provenance; and
- quality or scorecard data used to explain the result check.

These files make a render understandable and help diagnose a problem. They do
not contain your original image.

## Embedded metadata

ExifTool writes available capture facts and processing provenance into exported
images. This may include target, capture date, integration, filter, telescope,
software version, and the processing variant. The exact fields depend on what
the source FITS provides.

## Result-check meanings

### Ready to use

The hero image passed the automatic category-aware checks. This is not a claim
that every aesthetic choice will match your taste; it means the app found no
known processing failure.

### Finished—capture-limited

The pipeline completed, but evidence in the source limits the result. Examples
include weak signal, poor focus, heavy clipping, or insufficient recoverable
faint structure. More integration or a better capture may help more than
different processing.

### Needs attention

A processing check failed or a required result could not be validated. Read the
message and technical details. Do not assume the image is ready simply because
a JPG exists.

## Why a JPG may be smaller than the Seestar preview

A cleaner, darker, less noisy sky compresses efficiently. The output JPG may
therefore be much smaller than the stock Seestar JPG even when the subject is
better resolved. File size alone is not an image-quality measure.
