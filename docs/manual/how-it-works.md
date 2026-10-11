# How it works

The short version: the Seestar captures and stacks the photons; this app develops
the fuller data in the stacked FITS into a finished photograph.

Nothing in the process paints new nebula, stars, or galaxy detail. Each step is
intended to reveal or present signal that the telescope actually recorded.

## Who does what

The four helper applications do the image processing. Each one is a powerful
tool, but each needs the right settings for each photo, and the right settings
change from one subject to the next. Photon Finish's job is the part you would
otherwise do by hand:

- **measure** the photo: sky brightness, noise, stars, and how much nebula or
  galaxy structure is present;
- **decide** which steps to run and with what settings, from those measurements
  and the filter the Seestar used—never from the object's name;
- **run** each helper application with those settings, in the right order; and
- **check** the finished photo for known failures before calling it ready.

## What happens to your photo, step by step

| Step | What changes in your photo | Done by | What Photon Finish decides for you |
|---|---|---|---|
| 1. Check the file | Nothing yet | Photon Finish | Makes sure the file is a photo the Seestar stacked, and reads the details the Seestar saved with it: filter, number of exposures, date, and where in the sky it was pointing. |
| 2. Measure | Nothing yet | Photon Finish | Measures how bright the sky is, how grainy the photo is, how many stars there are, and how much nebula or galaxy is visible. Every choice below is based on these measurements. |
| 3. Orientation and edges | The photo is the right way up, and any ragged edges left by stacking are trimmed | Siril | Turns the photo to match the Seestar's own picture. Trims an edge only if it is actually damaged, so a clean photo keeps all of its sky. |
| 4. Satellite trails | Satellite and airplane streaks fade into the sky | Photon Finish | Finds long straight streaks and fills them in from the sky around them. If a streak can't be removed cleanly, it is left alone rather than smeared. |
| 5. Sky glow | Glow from light pollution or the moon is removed, so the sky is even | GraXpert | Nothing—this step works the same way on every photo. |
| 6. Grain | Smoother sky; faint detail is easier to see | GraXpert | How much smoothing to use. Faint galaxies and short sessions get more. Star clusters get enough to bring out their faintest stars, but not so much that the stars blur together. |
| 7. Color | Natural-looking color and a sky without a color tint | Siril | Whether to correct the color, based on the filter and the subject. Nebulae keep the Seestar's own color, so their red glow stays red instead of turning pink. Galaxies are corrected so they don't come out orange. Star clusters are left alone so they don't turn pink. |
| 8. Brightness | Faint nebula, galaxy, or cluster becomes visible against a dark sky | Siril | How much to brighten. This is the most important choice. Photon Finish keeps the sky dark without hiding the faint stars in it, then brightens the subject to a consistent level and checks the result, trying again until it gets there. |
| 9. Detail | The faint outer parts of a nebula, a galaxy's arms and dust lanes, or a cluster's glow are brought out | Siril | How much to bring out, based on the subject. Only in the Showcase and Print styles. |
| 10. Color strength | Richer color | Siril | How vivid to make the color. Star clusters get a little less, because too much makes them look pink. |
| 11. Star versions | Extra versions with fewer stars or no stars | StarNet2 separates the stars; Siril blends them back | Whether to make these versions. Only for nebulae and galaxies; star clusters always keep their stars, because the stars are the subject. The recommended photo keeps every star. |
| 12. Close-up | An extra close-up version | Siril | Whether a close-up is worth making, and where to frame it. Only for galaxies that are small in the frame; the main photo always shows the whole field. |
| 13. Save | 16-bit TIFF, PNG, and JPG files, labeled with the target, date, filter, and settings | Siril saves; ExifTool writes the labels | Nothing—this step works the same way on every photo. |
| 14. Check | The result says Ready to use, Finished—capture-limited, or Needs attention | Photon Finish | Looks for common problems—a gray or pitch-black sky, missing stars, blown-out bright areas, unnaturally bright color, or a flipped photo—and tells you if it finds one. |

These choices come from testing on real Seestar captures and comparing against
the Seestar's own output, not from rules of thumb.

## If you don't like the result

Your original Seestar file is never changed, so you can always set the result
aside and start again. Or:

- **Try another style.** Natural is gentler than the default Showcase. See
  [Styles and versions](styles-and-versions.md).
- **Change one step.** Every photo comes with an adjustment file listing each
  choice in the table above: the value Photon Finish picked, the application
  that applied it, and what a wrong value looks like. Change only the ones you
  disagree with and re-create the photo; everything else is decided as usual.
  See [Fine-tune one image](fine-tuning.md).
- **Take over by hand.** Open the 16-bit TIFF in any photo editor, or start
  from your original file in GraXpert and Siril, using the adjustment file as
  a map of what was done.

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
well, and Photon Finish does not try to replace them. Its contribution is the
table above: the measuring, the per-photo settings, the order, and the checks.

If you eventually want detailed manual control, the emitted adjustment recipe
also tells you which application owns each kind of adjustment.
