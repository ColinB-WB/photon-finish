"""Satellite / aircraft trail removal (CLAUDE.md §6a: "remove any long thin straight
bright line -- objective, no fork").

We only have the stacked master (not the subs), so the usual fix -- sigma-rejection
across frames at stack time -- is unavailable. Instead we detect the trail on the
master and inpaint across it.

Deterministic, pure numpy (no scipy/skimage/cv2 dependency -- the engine's "law"
layer, CLAUDE.md §13). The method:

  1. High-pass the luminance (subtract a large box-mean) so the thin trail and the
     stars survive but the smooth galaxy + sky gradient are removed.
  2. Threshold to a binary bright-pixel map.
  3. Shear-and-sum (a discrete Radon transform) over a grid of slopes: along the
     trail's true slope, the bright pixels are COLLINEAR and sum to a long ridge;
     stars are scattered and the galaxy core is not a thin line, so only a real trail
     makes a tall, narrow accumulator peak. The peak's prominence + span gate against
     false positives -- on a trail-free frame nothing is removed (target-agnostic).
  4. Inpaint a thin band along the line by interpolating PERPENDICULAR to it, so the
     local background / galaxy gradient is preserved (not flattened).

Detection runs near-horizontal; a near-vertical trail is handled by transposing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np


@dataclass
class Trail:
    present: bool
    orientation: str = "h"  # 'h' near-horizontal, 'v' near-vertical
    slope: float = 0.0  # long-axis units: rows per column (h) / cols per row (v)
    intercept: float = 0.0  # at long-axis index 0, full-res pixels
    half_width: int = 0  # inpaint band half-width (px)
    prominence: float = 0.0  # peak accumulator / median accumulator
    length_frac: float = 0.0  # inlier span as a fraction of the long axis
    peak: float = 0.0


# --- helpers ---------------------------------------------------------------------


def _luma(data: np.ndarray) -> np.ndarray:
    a = data.astype(np.float64)
    if a.ndim == 3:
        axis = 0 if a.shape[0] in (3, 4) else -1
        a = a.mean(axis=axis)
    return a


def _block_max(a: np.ndarray, f: int) -> np.ndarray:
    """Downsample by max-pooling (factor f). Max (not strided/mean) so a 1px-thin
    bright trail SURVIVES the downsample -- strided subsampling skips the line's rows."""
    if f <= 1:
        return a
    H, W = a.shape
    H2, W2 = H // f, W // f
    return a[: H2 * f, : W2 * f].reshape(H2, f, W2, f).max(axis=(1, 3))


def _box_mean(a: np.ndarray, r: int) -> np.ndarray:
    """Mean filter with a (2r+1) square window via a summed-area table (separable,
    O(N)). Reflect-padded so edges are sane."""
    if r < 1:
        return a.copy()
    ap = np.pad(a, r, mode="reflect")
    cs = np.cumsum(np.cumsum(ap, axis=0), axis=1)
    cs = np.pad(cs, ((1, 0), (1, 0)), mode="constant")
    H, W = a.shape
    w = 2 * r + 1
    S = cs[w : w + H, w : w + W] - cs[0:H, w : w + W] - cs[w : w + H, 0:W] + cs[0:H, 0:W]
    return S / (w * w)


def _highpass(luma: np.ndarray, bg_radius: int):
    """High-pass (subtract a large box-mean) so the thin trail + stars survive but the
    smooth galaxy/sky go away. Returns (residual, robust MAD-sigma of the residual)."""
    resid = luma - _box_mean(luma, bg_radius)
    med = np.median(resid)
    mad = np.median(np.abs(resid - med)) or 1e-9
    return resid - med, 1.4826 * mad


def _shear_sum(weight: np.ndarray, slopes: np.ndarray):
    """For each slope s, integrate the (real-valued, star-clipped) weight map along
    sheared lines -- row offset round(s*col) per column -- into an intercept
    accumulator. A faint trail integrates along its whole length even when each pixel
    is sub-threshold; noise averages out. Returns (best_slope, best_intercept_row,
    peak, accumulator_at_best, all_peaks)."""
    H, W = weight.shape
    cols = np.arange(W)
    rows0 = np.arange(H)[:, None]
    best = (0.0, 0, -1.0, None)
    peaks = []
    for s in slopes:
        offsets = np.rint(s * cols).astype(np.int64)  # (W,)
        src = rows0 + offsets[None, :]  # (H,W) source rows
        valid = (src >= 0) & (src < H)
        src_c = np.clip(src, 0, H - 1)
        contrib = np.where(valid, weight[src_c, cols[None, :]], 0.0)
        acc = contrib.sum(axis=1)  # (H,) per-intercept sum
        pk = acc.max()
        peaks.append(pk)
        if pk > best[2]:
            best = (float(s), int(acc.argmax()), float(pk), acc)
    return best[0], best[1], best[2], best[3], np.array(peaks)


def _half_width(resid: np.ndarray, slope: float, intercept: int, max_hw: int) -> int:
    """Half-width of the trail from its perpendicular (~vertical) cross-section:
    the band where the along-line residual sum stays above half its peak."""
    H, W = resid.shape
    cols = np.arange(W)
    center = np.clip(np.rint(intercept + slope * cols).astype(np.int64), 0, H - 1)
    prof = []
    for d in range(-max_hw - 2, max_hw + 3):
        rows = np.clip(center + d, 0, H - 1)
        prof.append(resid[rows, cols].sum())
    prof = np.array(prof)
    peak = prof.max()
    base = np.median(prof)
    half = base + 0.5 * (peak - base)
    above = np.where(prof >= half)[0]
    if len(above) == 0:
        return 1
    hw = int(np.ceil((above.max() - above.min()) / 2.0))
    return max(1, min(hw, max_hw))


# --- detection -------------------------------------------------------------------


def detect_trail(
    data: np.ndarray,
    downsample: int = 2,
    bg_radius: int = 24,
    star_clip: float = 3.0,
    slope_max: float = 1.0,
    slope_step: float = 0.01,
    min_prominence: float = 2.5,
    min_length_frac: float = 0.5,
    line_sigma: float = 1.0,
    max_halfwidth: int = 6,
) -> Trail:
    """Detect a single dominant straight trail. Searches both near-horizontal and
    near-vertical orientations (the latter via transpose) and keeps the stronger.
    Integrates the high-pass residual (in MAD-sigma units) along candidate lines, with
    each pixel CLIPPED to `star_clip` sigma so a handful of bright stars on a chance
    line cannot fake a trail -- only a faint line coherent over a long span wins.
    Gated by `min_prominence` (peak vs the rest of its accumulator) AND
    `min_length_frac` (coverage: fraction of the span where the line is actually >
    `line_sigma`). Returns Trail(present=False) when nothing clears both."""
    full = _luma(data)
    # Compress (asinh in sky-sigma units) BEFORE high-pass: in linear data a faint trail
    # is a few sigma above sky while stars are ~100x brighter and dominate the Radon
    # accumulator. asinh pulls the bright stars down toward the faint trail so a coherent
    # faint line wins. (Real NGC5128 trail: coverage 0.30 linear -> 0.75 compressed.)
    sky = np.median(full)
    sig0 = 1.4826 * (np.median(np.abs(full - sky)) or 1e-9)
    full = np.arcsinh((full - sky) / sig0)
    f = max(1, int(downsample))
    small = _block_max(full, f)

    slopes = np.arange(-slope_max, slope_max + 1e-9, slope_step)
    best = None
    for orient in ("h", "v"):
        img = small if orient == "h" else small.T
        resid, sigma = _highpass(img, max(1, bg_radius // f))
        weight = np.clip(resid / sigma, 0.0, star_clip)  # sigma units, stars capped
        s, b, peak, acc, peaks = _shear_sum(weight, slopes)
        # Prominence = how far the trail's intercept bin towers over the OTHER intercepts
        # at the winning slope. A real line concentrates integrated signal into one bin;
        # a scattered star field spreads it, so its best bin barely beats the rest.
        prominence = peak / (np.percentile(acc, 90) + 1.0)
        # Coverage = continuity: fraction of the span where the residual ON the line
        # actually exceeds line_sigma. A real trail is present across most of the frame;
        # a chance alignment of a few stars is not.
        H2, W2 = img.shape
        cols = np.arange(W2)
        rows = np.clip(np.rint(b + s * cols).astype(np.int64), 0, H2 - 1)
        coverage = float(((resid[rows, cols] / sigma) > line_sigma).mean())
        cand = {
            "orient": orient,
            "slope": s,
            "intercept_ds": b,
            "peak": peak,
            "prominence": float(prominence),
            "length_frac": coverage,
        }
        if best is None or prominence > best["prominence"]:
            best = cand

    if best is None or best["prominence"] < min_prominence or best["length_frac"] < min_length_frac:
        return Trail(
            present=False,
            prominence=float(best["prominence"]) if best else 0.0,
            length_frac=float(best["length_frac"]) if best else 0.0,
        )

    # Map the downsampled line back to full-res coords. Slope (rows-per-col) is scale
    # invariant; intercept scales by the downsample factor.
    slope = best["slope"]
    intercept_full = best["intercept_ds"] * f + (f - 1) / 2.0  # block-center offset
    orient = best["orient"]
    # Width on the FULL-res residual (in the detection orientation).
    full_o = full if orient == "h" else full.T
    resid_full = full_o - _box_mean(full_o, bg_radius)
    hw = _half_width(resid_full, slope, int(round(intercept_full)), max_halfwidth)

    return Trail(
        present=True,
        orientation=orient,
        slope=float(slope),
        intercept=float(intercept_full),
        half_width=int(hw),
        prominence=float(best["prominence"]),
        length_frac=float(best["length_frac"]),
        peak=float(best["peak"]),
    )


# --- inpainting ------------------------------------------------------------------


def inpaint_trail(data: np.ndarray, trail: Trail, extra_width: int = 1) -> np.ndarray:
    """Inpaint the trail band by interpolating PERPENDICULAR to the line (so the local
    background / galaxy gradient is preserved). Operates per channel; near-vertical
    trails are handled by transposing the spatial axes."""
    if not trail.present:
        return data
    out = data.astype(np.float64).copy()
    transposed = trail.orientation == "v"

    def _spatial_T(arr):
        # transpose only the spatial axes, keeping channels first if present
        if arr.ndim == 3:
            axis0 = 0 if arr.shape[0] in (3, 4) else -1
            return np.swapaxes(arr, -2, -1) if axis0 == 0 else np.swapaxes(arr, 0, 1)
        return arr.T

    work = _spatial_T(out) if transposed else out
    chans = [work[i] for i in range(work.shape[0])] if work.ndim == 3 and work.shape[0] in (3, 4) else [work]

    H, W = chans[0].shape
    hw = trail.half_width + max(0, extra_width)
    cols = np.arange(W)
    center = np.rint(trail.intercept + trail.slope * cols).astype(np.int64)

    for ch in chans:
        for c in range(W):
            r0, r1 = center[c] - hw, center[c] + hw
            if r1 < 0 or r0 >= H:
                continue
            r0c, r1c = max(0, r0), min(H - 1, r1)
            a_row, b_row = r0c - 1, r1c + 1
            if a_row < 0 and b_row > H - 1:
                continue
            if a_row < 0:  # band touches the top edge
                ch[r0c : r1c + 1, c] = ch[b_row, c]
                continue
            if b_row > H - 1:  # band touches the bottom edge
                ch[r0c : r1c + 1, c] = ch[a_row, c]
                continue
            va, vb = ch[a_row, c], ch[b_row, c]
            n = (r1c - r0c) + 2
            t = (np.arange(r0c, r1c + 1) - a_row) / n
            ch[r0c : r1c + 1, c] = va + (vb - va) * t

    result = _spatial_T(work) if transposed else work
    return result.astype(data.dtype)


# --- FITS-level entry point ------------------------------------------------------


def remove_trail_fits(in_fits: str, out_fits: str, cfg: dict | None = None) -> dict:
    """Detect and (if present) inpaint a trail in a FITS, writing `out_fits` only when a
    trail is removed (preserving the header/WCS). Returns an info dict for the run notes;
    `present=False` means nothing was written and the caller keeps the input."""
    from astropy.io import fits

    tcfg = ((cfg or {}).get("engine", {}).get("trail_removal", {})) or {}
    with fits.open(in_fits) as h:
        data = h[0].data
        header = h[0].header.copy()
        dtype = data.dtype
        arr = np.array(data)

    trail = detect_trail(
        arr,
        downsample=int(tcfg.get("detect_downsample", 2)),
        bg_radius=int(tcfg.get("bg_radius", 24)),
        star_clip=float(tcfg.get("star_clip", 3.0)),
        min_prominence=float(tcfg.get("min_prominence", 2.5)),
        min_length_frac=float(tcfg.get("min_length_frac", 0.5)),
        max_halfwidth=int(tcfg.get("max_halfwidth", 6)),
    )
    info = {**asdict(trail)}
    if not trail.present:
        return info

    cleaned = inpaint_trail(arr, trail, extra_width=int(tcfg.get("extra_width", 1)))
    fits.writeto(out_fits, cleaned.astype(dtype), header, overwrite=True)
    info["output"] = out_fits
    return info
