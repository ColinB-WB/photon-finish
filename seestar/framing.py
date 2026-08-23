"""Auto feature-crop (CLAUDE.md framing fork).

Measures the subject's location and extent from the STARLESS image (stars removed,
so only the nebula/galaxy drives the measurement), then computes a crop box that
makes the subject fill the frame — but only when the subject is actually small.
Target-agnostic: a small galaxy gets cropped in; something already filling the frame
does not.
"""

from __future__ import annotations

import numpy as np
from astropy.io import fits
from astropy.stats import sigma_clipped_stats


def measure_subject(starless_fits: str, threshold_sigma: float = 4.0) -> dict | None:
    """Flux-weighted centroid + extent (std) of the subject in a starless image.
    Returns None if there's no extended signal (e.g. a pure star field)."""
    with fits.open(starless_fits) as h:
        data = h[0].data.astype(np.float64)
    if data.ndim == 3:
        lum = data.mean(axis=0 if data.shape[0] in (3, 4) else -1)
    else:
        lum = data
    H, W = lum.shape
    _, median, std = sigma_clipped_stats(lum, sigma=3.0, maxiters=5)
    w = np.clip(lum - (median + threshold_sigma * std), 0.0, None)
    total = w.sum()
    if total <= 0:
        return None
    xs = np.arange(W)
    ys = np.arange(H)
    wx = w.sum(axis=0)  # column weights
    wy = w.sum(axis=1)  # row weights
    cx = float((xs * wx).sum() / total)
    cy = float((ys * wy).sum() / total)
    sx = float(np.sqrt((wx * (xs - cx) ** 2).sum() / total))
    sy = float(np.sqrt((wy * (ys - cy) ** 2).sum() / total))
    return {"cx": cx, "cy": cy, "sx": sx, "sy": sy, "W": W, "H": H}


def feature_crop_box(
    m: dict, fill_fraction: float = 0.66, k_extent: float = 2.5, trigger_max_frac: float = 0.62
) -> tuple | None:
    """Crop box (x, y, w, h) that frames the subject at `fill_fraction` of the frame,
    preserving the native aspect ratio. Returns None if the subject already fills more
    than `trigger_max_frac` of the frame (no closeup needed)."""
    W, H = m["W"], m["H"]
    subj_w = 2 * k_extent * m["sx"]
    subj_h = 2 * k_extent * m["sy"]
    if subj_w / W > trigger_max_frac or subj_h / H > trigger_max_frac:
        return None  # already large enough; keep full frame
    crop_w = subj_w / fill_fraction
    crop_h = subj_h / fill_fraction
    aspect = W / H
    if crop_w / crop_h < aspect:  # enforce native aspect ratio
        crop_w = crop_h * aspect
    else:
        crop_h = crop_w / aspect
    crop_w = min(crop_w, W)
    crop_h = min(crop_h, H)
    x0 = min(max(m["cx"] - crop_w / 2, 0), W - crop_w)
    y0 = min(max(m["cy"] - crop_h / 2, 0), H - crop_h)
    return (int(round(x0)), int(round(y0)), int(round(crop_w)), int(round(crop_h)))
