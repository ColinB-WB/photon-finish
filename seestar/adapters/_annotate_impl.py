"""Documentation-profile annotation overlay (CLAUDE.md §8a): label, capture caption,
scale bar (from the measured plate scale), N/E arrows (from the WCS CD matrix), and date.

The render is the `documentation` deliverable -- a faithful record for comparison/photometry,
so the overlay states the facts the engine measured/parsed, never invents them. The scale bar
is exact (pixscale x pixels).

N/E are derived from the WCS CD matrix (inverse -> pixel direction of +Dec / +RA). Orientation
was VALIDATED empirically: a star-pattern correlation of the saved deliverable against the raw
FITS array (as astropy reads it) wins at IDENTITY (NGC5128: 0.074 vs flipud 0.017) -- Siril's
one `mirrorx` nets the saved raster back to astropy's array frame -- so the CD-inverse pixel
directions apply directly with `flipud=True` (keep the array-row y sign; PIL is y-down). Cross-
checked against astropy.wcs.world_to_pixel on the same field: N up, E left for NGC5128.
"""

from __future__ import annotations

import os

import numpy as np

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pillow is a declared dep
    Image = None

_NICE_ARCMIN = [1, 2, 5, 10, 15, 20, 30, 60]  # pick a round scale-bar length


def ne_vectors_image(cd: dict, flipud: bool = True):
    """Unit pixel vectors (x_right, y_down) pointing to celestial North and East in the
    SAVED raster. CD maps FITS pixel (dx,dy) -> (dRA_proj, dDec) deg; invert to get the
    pixel direction of +Dec (North) and +RA (East). `flipud` accounts for the pipeline's
    mirrorx. Returns (north_xy, east_xy) or (None, None) if the CD matrix is incomplete."""
    try:
        m = np.array([[cd["CD1_1"], cd["CD1_2"]], [cd["CD2_1"], cd["CD2_2"]]], float)
        inv = np.linalg.inv(m)
    except (KeyError, TypeError, np.linalg.LinAlgError):
        return None, None
    north = inv @ np.array([0.0, 1.0])  # +Dec
    east = inv @ np.array([1.0, 0.0])  # +RA

    def to_image(v):
        x, y = float(v[0]), float(v[1])
        # FITS pixel y is up; the saved raster is y-down and mirrorx-flipped -> y sign.
        y = y if flipud else -y
        n = (x * x + y * y) ** 0.5
        return (x / n, y / n) if n else (0.0, 0.0)

    return to_image(north), to_image(east)


def _font(size: int):
    for path in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
    ):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass  # best-effort: font fallback, not a manifest-referenced write (CHANGE_SAFETY rule 5)
    return ImageFont.load_default()


def _scale_bar_len(pixscale_arcsec: float, width_px: int):
    """Pick a round arcmin length whose bar is ~12-22% of the image width."""
    target_px = 0.17 * width_px
    target_arcmin = target_px * pixscale_arcsec / 60.0
    best = min(_NICE_ARCMIN, key=lambda a: abs(a - target_arcmin))
    bar_px = best * 60.0 / pixscale_arcsec
    return best, int(round(bar_px))


def annotate(in_render: str, out_jpg: str, info: dict, jpg_quality: int = 95) -> str:
    """Draw the overlay on `in_render` and save `out_jpg`. `info` keys: title, subtitle,
    lines (list[str]), pixscale_arcsec, cd (dict | None)."""
    im = Image.open(in_render).convert("RGB")
    W, H = im.size
    d = ImageDraw.Draw(im)
    s = max(14, int(W / 52))  # base font size scales with image
    f_title, f_body = _font(int(s * 1.25)), _font(s)
    pad = int(s * 0.8)
    white, shadow = (240, 240, 240), (0, 0, 0)

    def text(xy, t, fnt, anchor="la"):
        d.text((xy[0] + 1, xy[1] + 1), t, font=fnt, fill=shadow, anchor=anchor)
        d.text(xy, t, font=fnt, fill=white, anchor=anchor)

    # caption block: title + subtitle + fact lines (top-left)
    y = pad
    if info.get("title"):
        text((pad, y), info["title"], f_title)
        y += int(s * 1.5)
    if info.get("subtitle"):
        text((pad, y), info["subtitle"], f_body)
        y += int(s * 1.35)
    for ln in info.get("lines", []):
        text((pad, y), ln, f_body)
        y += int(s * 1.25)

    # scale bar (bottom-left), exact from the plate scale
    px = info.get("pixscale_arcsec")
    if px:
        arcmin, bar = _scale_bar_len(px, W)
        x0, yb = pad, H - pad - int(s * 0.6)
        d.line([(x0 + 1, yb + 1), (x0 + bar + 1, yb + 1)], fill=shadow, width=4)
        d.line([(x0, yb), (x0 + bar, yb)], fill=white, width=3)
        for xx in (x0, x0 + bar):  # end ticks
            d.line([(xx, yb - int(s * 0.3)), (xx, yb + int(s * 0.3))], fill=white, width=3)
        text((x0, yb - int(s * 1.2)), f"{arcmin}'", f_body)

    # N/E compass (bottom-right) from the CD matrix
    n_xy, e_xy = ne_vectors_image(info.get("cd") or {})
    if n_xy:
        cx, cy = W - pad - int(s * 2.2), H - pad - int(s * 2.2)
        L = int(s * 1.7)
        for (vx, vy), lab in ((n_xy, "N"), (e_xy, "E")):
            ex, ey = cx + vx * L, cy + vy * L
            d.line([(cx + 1, cy + 1), (ex + 1, ey + 1)], fill=shadow, width=4)
            d.line([(cx, cy), (ex, ey)], fill=white, width=3)
            text((cx + vx * (L + s * 0.6), cy + vy * (L + s * 0.6)), lab, f_body, anchor="mm")

    im.save(out_jpg, quality=jpg_quality)
    return out_jpg
