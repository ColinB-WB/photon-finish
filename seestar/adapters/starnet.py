"""StarNet2 adapter: star / starless separation.

StarNet2 wants a STRETCHED 16-bit TIFF; `--unscreen` yields the star-only layer for
screen-blending stars back at reduced strength. When the Plan contains a FITS
stretched artifact, this adapter converts it to a StarNet-readable TIFF at the
tool boundary so the Plan graph can stay FITS-native.
"""

from __future__ import annotations

import os

from seestar.core.types import SeparateStars

from ._external_process import run_external
from .base import AdapterError, ExecContext, ToolPaths, register


def starnet(stretched_tif: str, workdir: str, base: str, tools: ToolPaths) -> dict:
    """Bounded by `tools.timeouts.starnet2` (H1 Task 4) -- `ExternalToolTimeout`
    propagates to this helper's callers."""
    starless = os.path.join(workdir, f"{base}_starless.tif")
    stars = os.path.join(workdir, f"{base}_stars.tif")
    run_external(
        [tools.starnet2, "--input", stretched_tif, "--output", starless, "--unscreen", stars],
        tool="starnet2",
        tools=tools,
        check=True,
    )
    return {"starless": starless, "stars": stars}


def _fits_to_starnet_tif(input_path: str) -> str:
    """starnet2 reads TIFF/PNG only, but the stretched artifact SeparateStars
    consumes in a Plan is Siril's .fit -- convert at the tool boundary, keeping the
    plan graph unchanged. The conversion must mirror Siril `savetif` EXACTLY in BOTH
    respects, so StarNet sees the same pixels it would have seen on Siril's own
    export (milestone validation guards drift between the two paths):
      - quantization: clip float data to [0,1], x65535, round half AWAY FROM ZERO;
      - ROW ORDER: FITS row 0 is the BOTTOM scanline (FITS standard / Siril
        ROWORDER=BOTTOM-UP), a TIFF raster's row 0 is the TOP -- flip vertically.
        Writing FITS rows straight into the TIFF shipped every StarNet-path
        deliverable vertically flipped (owner-caught milestone regression; the
        rotation-invariant metrics missed it). ROWORDER=TOP-DOWN data (already
        raster order) is written as-is; absent header -> BOTTOM-UP per the standard.
    The converted TIF is written next to the input (scratch dir for PRODUCTION
    artifacts), UNREGISTERED -- it is cleaned with the scratch dir."""
    import numpy as np
    import tifffile
    from astropy.io import fits

    with fits.open(input_path) as hdul:
        data = np.asarray(hdul[0].data, dtype="float64")
        roworder = str(hdul[0].header.get("ROWORDER") or "BOTTOM-UP").strip().upper()
    if data.ndim == 3 and data.shape[0] == 3:  # channel-first (3,H,W) -> (H,W,3)
        data = np.transpose(data, (1, 2, 0))
    if roworder != "TOP-DOWN":  # FITS bottom-up rows -> raster top-down
        data = data[::-1]
    # round half away from zero: values are >= 0 after the clip, so floor(x + 0.5)
    q = np.floor(np.clip(data, 0.0, 1.0) * 65535.0 + 0.5).astype("uint16")
    out_path = os.path.splitext(input_path)[0] + ".tif"
    tifffile.imwrite(out_path, q)
    return out_path


class StarNetAdapter:
    """Executes SeparateStars by shelling to starnet2."""

    operations = (SeparateStars,)
    name = "starnet2"

    def execute(self, op, ctx: ExecContext) -> dict:
        if not isinstance(op, SeparateStars):
            raise AdapterError(f"StarNetAdapter cannot execute {type(op).__name__}")
        input_path = ctx.resolve[op.input]
        if input_path.lower().endswith((".fit", ".fits")):
            input_path = _fits_to_starnet_tif(input_path)
        starless_path = ctx.workspace.path_for(op.starless, ".tif")
        stars_path = ctx.workspace.path_for(op.stars, ".tif")
        run_external(
            [ctx.tools.starnet2, "--input", input_path, "--output", starless_path, "--unscreen", stars_path],
            tool="starnet2",
            tools=ctx.tools,
            check=True,
        )
        return {op.starless: starless_path, op.stars: stars_path}


register(StarNetAdapter())
