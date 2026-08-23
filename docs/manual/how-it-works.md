# How it works

The short version: the Seestar captures and stacks the photons; this app develops
the fuller data in the stacked FITS into a finished photograph.

Nothing in the process paints new nebula, stars, or galaxy detail. Each step is
intended to reveal or present signal that the telescope actually recorded.

## The processing journey

1. **Qualify the source.** The app confirms that the file is a stacked,
   three-channel Seestar capture and reads its filter, integration, date, and
   sky-coordinate information.
2. **Measure before deciding.** It measures background, noise, stars, scale, and
   visible structure. These measurements drive processing choices.
3. **Prepare the frame.** It corrects the known orientation convention and crops
   unusable stack borders.
4. **Even out the sky.** GraXpert removes broad gradients caused by light
   pollution, moonlight, or uneven illumination.
5. **Reduce noise.** GraXpert denoises within conservative limits chosen for the
   kind and quality of signal in the frame.
6. **Develop color and brightness.** Siril calibrates color where appropriate
   and stretches faint data into a range the eye and screen can see. A stretch
   brightens faint structure much more than already-bright stars and cores.
7. **Treat visible structure.** Bounded, content-aware steps can lift a nebula's
   envelope, preserve a galaxy's dust lane, or protect the resolved stars in a
   cluster.
8. **Handle trails and stars.** Detected satellite trails are conservatively
   repaired where possible. StarNet2 can separate stars from an extended
   subject so the app can prepare full-star, reduced-star, and starless
   versions.
9. **Compose useful versions.** A galaxy may receive a close-up; clusters keep
   their stars because the stars are the subject.
10. **Export and check.** The app writes finished files, uses ExifTool to embed
    capture and processing metadata, records its decisions, and checks the hero
    image against category-aware quality gates.

## How the app knows what it is looking at

The FITS filter and the photograph's measurements are the main authorities. A
bundled catalog of deduplicated Messier and Caldwell targets can confirm many
objects from their WCS sky position while offline. An optional SIMBAD lookup can
help with fields outside that catalog.

This lookup matters most for a broadband IRCUT image where the measurements
cannot confidently distinguish a compact galaxy from a dense star cluster. The
app uses the catalog category to choose the appropriate general treatment:

- a galaxy can receive treatment that reveals its faint disk and dust structure,
  along with a useful close-up; while
- a star cluster keeps its stars as the subject and receives cluster-appropriate
  treatment rather than star removal.

For an LP-filter nebula, the filter and photograph already settle the category,
so the catalog does not change its processing.

Catalog identity never supplies target-specific editing values. It cannot decide
brightness, color, denoise strength, crop, trail repair, or quality results.
When a catalog match is ambiguous or conflicts with stronger evidence, the app
abstains or ignores it.

The optional network request sends only sky coordinates and a search radius—not
the image. See [Privacy and network use](../privacy-and-network.md).

## Why use existing applications?

GraXpert, Siril, StarNet2, and ExifTool each solve a difficult specialist problem
well. The app's role is orchestration: select a suitable operation, calculate or
bound its settings, pass work to the appropriate tool, and check the result.

If you eventually want detailed manual control, the emitted adjustment recipe
also tells you which application owns each kind of adjustment.
