"""Shell-side FITS measurements.

`measure_fits` converts FITS pixel data into `core.Measurements` in one
canonical numeric space: unit-float. Normalization happens here, once, at the
I/O boundary, so raw 16-bit ADU values never enter the core.
"""

from __future__ import annotations

import math
import os
import warnings
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.stats import sigma_clipped_stats

from .adapters.base import ToolPaths
from .core.types import ChannelStats, Measurements

_ADU_MAX = 65535.0


def norm_factor(data: np.ndarray) -> float:
    """Raw Seestar FITS are 16-bit ADU; post-GraXpert/Siril masters are float
    already in [0,1]. Pick the factor from the data range so both measure alike
    (the old bug measured the master with the 16-bit factor and zeroed the bp)."""
    mx = float(np.nanmax(data))
    return _ADU_MAX if mx > 2.0 else 1.0


def channels(data: np.ndarray):
    if data.ndim == 2:
        yield "mono", data
    elif data.ndim == 3:
        axis = 0 if data.shape[0] in (3, 4) else -1
        names = ("R", "G", "B", "A")
        for i in range(data.shape[axis]):
            yield names[i], (data[i] if axis == 0 else data[..., i])
    else:
        raise ValueError(f"unexpected data ndim={data.ndim}")


def measure_fits(path: str, pixscale_arcsec: float | None = None) -> Measurements:
    with fits.open(path) as hdul:
        data = hdul[0].data.astype(np.float64)
    nf = norm_factor(data)
    sat_level = 0.992 * nf
    per: list[ChannelStats] = []
    for name, ch in channels(data):
        _, median, std = sigma_clipped_stats(ch, sigma=3.0, maxiters=5)
        per.append(
            ChannelStats(
                name=name,
                median=float(median) / nf,
                sigma=float(std) / nf,
                max_value=float(ch.max()) / nf,
                sat_frac=float((ch >= sat_level).mean()),
            )
        )
    med = float(np.mean([c.median for c in per]))
    sig = float(np.mean([c.sigma for c in per]))
    mx = float(np.max([c.max_value for c in per]))
    return Measurements(
        channels=tuple(per),
        background=round(med, 6),
        bgnoise=round(sig, 6),
        max_value=round(mx, 6),
        dims=(int(data.shape[-1]), int(data.shape[-2])),
        pixscale_arcsec=pixscale_arcsec,
        dynamic_range=round((mx - med) / sig, 1) if sig else None,
    )


# ---------------------------------------------------------------------------
# Additional shell-side measurements used by the planner and QA. These routines
# call external tools or return small report dicts, so they stay outside the
# pure `core.Measurements` model.
# ---------------------------------------------------------------------------


# -- measured per-edge border scan (Diagnosis 1, spec 2026-08-12) -------------
# Only DEMONSTRATED invalid signatures count as a croppable border: non-finite
# pixels or dead/blank (exact-zero) pixels covering a sustained fraction of a
# full edge line. Astronomical content, gradients, vignetting, and merely dark
# sky are valid data -- a strong-but-finite darkening near an edge is reported
# as AMBIGUOUS (confident=False), which the core turns into a zero crop plus a
# recorded warning, never a trim. Thresholds are deliberately blunt: every real
# reference capture (S30/S30 Pro/S50, 2026-08-13 characterization) measures
# edge medians within ~4% of the interior with zero invalid pixels, far from
# both triggers.
_BORDER_INVALID_LINE_FRAC = 0.30  # line is invalid if >=30% of its px are dead
_BORDER_DARK_FACTOR = 0.5  # line median < 0.5x interior median = suspicious, ambiguous


def measure_border(path: str) -> dict:
    """Per-edge invalid border widths of a FITS, in RAW array terms (top = first
    rows of the data array, bottom = last rows, left/right = first/last columns).
    Returns {"left","top","right","bottom","confident","detail"} -- widths in px;
    `confident=False` withdraws the measurement (zero widths) when something
    looks wrong but cannot be demonstrated invalid."""
    with fits.open(path) as h:
        data = h[0].data.astype(np.float64)
    if data.ndim == 3:
        axis = 0 if data.shape[0] in (3, 4) else -1
        invalid = (~np.isfinite(data)).any(axis=axis) | (data == 0).all(axis=axis)
        luma = np.nanmean(np.where(np.isfinite(data), data, np.nan), axis=axis)
    else:
        invalid = ~np.isfinite(data) | (data == 0)
        luma = data
    H, W = invalid.shape
    interior = luma[H // 4 : (3 * H) // 4, W // 4 : (3 * W) // 4]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)  # all-NaN interior -> NaN, guarded below
        interior_med = float(np.nanmedian(interior))
    if not np.isfinite(interior_med) or interior_med <= 0:
        return {"left": 0, "top": 0, "right": 0, "bottom": 0, "confident": False, "detail": "interior_unmeasurable"}

    def _scan(inv_lines: np.ndarray, med_lines: np.ndarray, depth: int) -> tuple[int, str | None]:
        """inv_lines[i] = invalid fraction of line i from the edge inward;
        med_lines[i] = its median. Returns (width, note): width = contiguous
        demonstrated-invalid run; note = "blank:N", "dark_ambiguous", or None."""
        width = 0
        while width < depth and inv_lines[width] >= _BORDER_INVALID_LINE_FRAC:
            width += 1
        # beyond the demonstrated run: merely-dark lines are ambiguous, not croppable
        for i in range(width, depth):
            m = med_lines[i]
            if np.isfinite(m) and m < _BORDER_DARK_FACTOR * interior_med:
                return width, "dark_ambiguous"
        return width, (f"blank:{width}" if width else None)

    inv_f = invalid.astype(np.float64)
    with warnings.catch_warnings():
        # all-NaN lines (a fully non-finite border row) are expected here; the
        # isfinite guard in _scan handles their NaN medians
        warnings.simplefilter("ignore", category=RuntimeWarning)
        med_rows = np.nanmedian(luma, axis=1)
        med_cols = np.nanmedian(luma, axis=0)
    depth_y, depth_x = max(8, H // 10), max(8, W // 10)
    edges = {
        "top": _scan(inv_f.mean(axis=1)[:depth_y], med_rows[:depth_y], depth_y),
        "bottom": _scan(inv_f.mean(axis=1)[::-1][:depth_y], med_rows[::-1][:depth_y], depth_y),
        "left": _scan(inv_f.mean(axis=0)[:depth_x], med_cols[:depth_x], depth_x),
        "right": _scan(inv_f.mean(axis=0)[::-1][:depth_x], med_cols[::-1][:depth_x], depth_x),
    }
    ambiguous = [e for e, (_, note) in edges.items() if note == "dark_ambiguous"]
    if ambiguous:
        detail = ";".join(f"{e}=dark_ambiguous" for e in ambiguous)
        return {"left": 0, "top": 0, "right": 0, "bottom": 0, "confident": False, "detail": detail}
    notes = [f"{e}={note}" for e, (_, note) in edges.items() if note]
    return {
        "left": edges["left"][0],
        "top": edges["top"][0],
        "right": edges["right"][0],
        "bottom": edges["bottom"][0],
        "confident": True,
        "detail": ";".join(notes) if notes else "clean",
    }


# -- stable subject support (Diagnoses 5 + 3, spec 2026-08-12) -----------------
# One deterministic spatial support, measured from the run's LINEAR artifact via
# the established quick-stretch + StarNet workflow (the E-probe chain), BEFORE
# the stretch solve -- so the solver's every probe and the faint-structure QA
# reason about the same piece of the image, and neither depends causally on the
# final render. Measurement-only: production pixels are never changed.
# Validity floors. Tier 1 (compact/medium subjects) uses an ABSOLUTE pixel
# floor: what matters for a robust masked p90 is how many pixels the support
# holds, not what fraction of the frame they cover -- a fraction floor would
# make validity a function of camera FOV, the exact dependence Diagnosis 3
# removes (an ~11'x4' M82 is ~1% of the S50 frame yet ~20k px). Tier 2
# (frame-filling) keeps a frame-fraction floor: it exists precisely for
# pervasive signal.
_SUPPORT_MIN_PX = 1500
_SUPPORT_MIN_FRAC = 0.02  # tier-2 (frame-filling) floor
_SUPPORT_K_HARD = 3.0  # the established starless E law (med + 3 sigma)
_SUPPORT_K_LOW = 1.0  # frame-filling tier threshold (on the smoothed field)
_SUPPORT_BLOCK = 4  # block-mean size for the spatial-coherence check
_SUPPORT_COHERENCE_MIN = 0.5  # sigma(blurred)/sigma(raw): noise collapses ~1/block, structure survives
# NOTE (field evidence, 2026-08-13): a coherence-based "collapse detector" that
# preferred the tier-2 support whenever it dwarfed the hard tier was tried and
# REVERTED -- real quick-stretch starless fields measure coherent (gradients,
# neighbour halos), so it wrongly swapped the S50 M82 galaxy support (3%) for a
# 25% faint-background region and regressed a certifying render to near_black,
# while not making the frame-filling NGC7000 solve reachable either (that
# target's non-convergence predates this work: its global-median sky metric and
# nebula target level fight frame-filling nebulosity under any mask). Tier 2
# therefore engages only when the hard tier is genuinely too small in PIXELS.
#
# WHY COLLAPSE DETECTION NEEDS AN EXTERNAL EXPECTATION (measured 2026-08-14 on the
# REAL probe inputs for NGC7000 / M82 / M8, saved from live runs). Four candidate
# image-only discriminators were measured and ALL FOUR rank the compact-subject
# M82 field as MORE extended than the genuinely frame-filling NGC7000 field:
#     clipped-median elevation (med-bg)/sigma_sky : M82 12.4  vs NGC7000 3.3
#     area above bg + 3 sigma_sky                 : M82 0.741 vs NGC7000 0.548
#     mid-scale texture in the excluded region     : M82 5.97  vs NGC7000 1.47
#     excluded-but-bright fraction                 : M82 0.635 vs NGC7000 0.334
# The reason is physical, not a tuning failure: after a per-image quick stretch, a
# large diffuse INSTRUMENTAL glow (M82's amp glow + gradient) is indistinguishable
# from large diffuse ASTRONOMICAL signal by intensity statistics alone. This is the
# same wall the reverted 2026-08-13 attempt hit. So collapse is detected by
# comparing the measured support against an INDEPENDENT expectation of the
# subject's angular extent (the local catalog footprint vs the frame's own FOV),
# exactly the corroboration Task 4 Phase A specified -- "prior proposes,
# measurement disposes". No expectation available => no collapse claim, and the
# established hard tier stands unchanged (M82's path, and every uncatalogued
# target's).
_SUPPORT_COLLAPSE_RATIO = 0.5  # measured < half the catalogued extent => collapsed
_SUPPORT_K_DARK = 5.0  # frame-filling tier: sky-sigma above the DARK-anchored background
_SUPPORT_DARK_PCT = 20.0  # percentile of the smoothed field taken as "true sky"
# Drop support islands smaller than this many PIXELS. Absolute, not a frame
# fraction, for the same reason the validity floor above is absolute: a fraction
# floor makes the rule a function of camera FOV, the exact dependence Diagnosis 3
# removes. (An earlier 0.0005-of-frame version was 4147 px on the S30 Pro --
# larger than the 1500 px validity floor -- so it could erase a support that had
# just been judged valid, and it deleted a real 3025 px subject outright.)
# Calibrated on the real S50 M82 probe, whose islands are sharply bimodal: the two
# galaxies measure 22802 px and 9722 px while EVERY other island is <= 627 px
# (amp-glow speckle along the frame edges). 800 sits in that gap, below the
# validity floor, and is independent of frame size because speckle size is set by
# noise correlation, not by how many pixels the sensor has.
_SUPPORT_ARTIFACT_MIN_PX = 800
# A recovered frame-filling support must be STRUCTURED, not a smooth ramp: RMS
# after removing a best-fit plane, in sky-noise units. Measured on real probes --
# NGC7000 3.9, M8 42, M82 33 -- versus a pure linear gradient at 0.17. Without
# this, the dark-anchored tier happily calls 61% of a gradient-only frame
# "subject" (verified), which is precisely the "treat most background as subject"
# failure this tier exists to avoid.
_SUPPORT_STRUCTURE_MIN = 1.0
# ...and it may never claim essentially the whole frame, whatever the catalogue says.
_SUPPORT_MAX_FRAC = 0.85


def _block_mean(a: np.ndarray, k: int) -> np.ndarray:
    h, w = a.shape
    hb, wb = h // k, w // k
    return a[: hb * k, : wb * k].reshape(hb, k, wb, k).mean(axis=(1, 3))


def _label_4connected(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """4-connected component labels + per-label pixel counts, in numpy only.

    Deliberately NOT `scipy.ndimage.label`: scipy is not a declared dependency and
    BOTH packaged builds pass `--exclude-module scipy`, so importing it here would
    work in the dev venv and then crash the shipped app at the support
    measurement. Row runs plus union-find is linear in the number of runs (far
    fewer than pixels) and matches scipy's labelling exactly on real data.

    Returns (labels HxW int32 with 0 = background, counts indexed by label)."""
    h, w = mask.shape
    # run boundaries per row, found on a padded copy so runs cannot span rows
    padded = np.zeros((h, w + 2), dtype=bool)
    padded[:, 1:-1] = mask
    d = np.diff(padded.astype(np.int8), axis=1)
    starts = np.argwhere(d == 1)
    ends = np.argwhere(d == -1)
    if starts.size == 0:
        return np.zeros((h, w), dtype=np.int32), np.zeros(1, dtype=np.int64)
    rows = starts[:, 0]
    run_start = starts[:, 1]
    run_end = ends[:, 1]  # exclusive, same ordering (one end per start, per row)

    parent = list(range(len(rows) + 1))  # 0 unused so labels start at 1

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    # Union runs on adjacent rows that overlap horizontally, one ROW PAIR at a
    # time with a two-pointer sweep: runs are ordered by (row, start), so
    # advancing whichever interval ends first visits each run once per pair. The
    # obvious per-run rescan is quadratic in runs-per-row -- 106 s on a heavily
    # fragmented 2160x3840 mask (measured) versus 0.6 s here.
    n_runs = len(rows)
    for r in np.unique(rows):
        lo_a = int(np.searchsorted(rows, r, side="left"))
        hi_a = int(np.searchsorted(rows, r, side="right"))
        lo_b = hi_a
        hi_b = int(np.searchsorted(rows, r + 1, side="right"))
        if lo_b >= hi_b or rows[lo_b] != r + 1:
            continue  # next row has no runs
        i, j = lo_a, lo_b
        while i < hi_a and j < hi_b:
            if run_start[j] < run_end[i] and run_start[i] < run_end[j]:
                union(i + 1, j + 1)
            if run_end[i] <= run_end[j]:
                i += 1
            else:
                j += 1

    roots = np.array([find(i + 1) for i in range(n_runs)], dtype=np.int64)
    uniq, compact = np.unique(roots, return_inverse=True)
    labels_per_run = (compact + 1).astype(np.int32)

    out = np.zeros(h * w, dtype=np.int32)
    for i in range(n_runs):
        base = int(rows[i]) * w
        out[base + run_start[i] : base + run_end[i]] = labels_per_run[i]
    counts = np.bincount(out, minlength=len(uniq) + 1).astype(np.int64)
    counts[0] = 0
    return out.reshape(h, w), counts


def _planar_residual(small: np.ndarray, sky_sigma: float) -> float:
    """RMS of the smoothed field after removing a best-fit PLANE, in sky-noise
    units. A smooth gradient (amp glow, uncorrected sky ramp) is fully explained
    by the plane and scores near zero; real extended structure is not."""
    h, w = small.shape
    yy, xx = np.mgrid[0:h, 0:w]
    a = np.column_stack([np.ones(small.size), xx.ravel(), yy.ravel()])
    coef, *_ = np.linalg.lstsq(a, small.ravel(), rcond=None)
    resid = small.ravel() - a @ coef
    return float(np.std(resid)) / max(sky_sigma, 1e-9)


def _drop_speckle(mask: np.ndarray, min_px: int = _SUPPORT_ARTIFACT_MIN_PX) -> tuple[np.ndarray, int, float]:
    """Remove disconnected support islands smaller than `min_frac` of the frame.

    Detector artefacts (the S50's amp-glow speckle along frame corners/edges) pass
    a pure intensity threshold and would otherwise let faint-structure RETENTION
    score on hardware noise rather than on sky signal -- a gate passing on an
    artefact is the same class of defect as a gate that silently skips. Real
    extended structure is contiguous and far above this floor, so this only ever
    removes specks. Returns (cleaned, islands_removed, area_removed_frac)."""
    if not mask.any():
        return mask, 0, 0.0
    labels, counts = _label_4connected(mask)
    # NOTE: no early return for a single island -- the floor must apply to it too,
    # or whether a speck survives depends on whether it has company.
    keep = counts >= max(1, int(min_px))
    keep[0] = False
    cleaned = keep[labels]
    removed_px = int(mask.sum() - cleaned.sum())
    return cleaned, int((~keep[1:]).sum()), float(removed_px) / mask.size


def _frame_filling_support(starless: np.ndarray, med: float, std: float) -> tuple[np.ndarray, dict]:
    """Support for a subject that occupies most of the frame.

    A sigma-clipped median sits INSIDE pervasive signal, so `med + 3 sigma` lands
    near the top of the subject's own range and keeps only its brightest knots
    (NGC7000: threshold at background + 14.8 sky-sigma while the structure tops out
    near 21 -> 1.55% of the frame). This tier re-anchors on the DARK end instead:
    the true sky is the darkest coherent part of the field, and the subject is
    everything standing clear of it. Deliberately a local-background statistic, not
    a global one -- the global median is itself contaminated here."""
    k = _SUPPORT_BLOCK
    small = _block_mean(starless, k)
    thr = np.percentile(small, _SUPPORT_DARK_PCT)
    dark = small[small <= thr]
    bg = float(np.median(dark))
    sky_sigma = float(dark.std())
    if not np.isfinite(sky_sigma) or sky_sigma <= 0:
        sky_sigma = std / max(k, 1)
    mask_small = small > bg + _SUPPORT_K_DARK * sky_sigma
    h, w = starless.shape
    mask = np.zeros((h, w), dtype=bool)
    hb, wb = mask_small.shape
    mask[: hb * k, : wb * k] = np.repeat(np.repeat(mask_small, k, axis=0), k, axis=1)
    return mask, {
        "bg": round(bg, 4),
        "sky_sigma": round(sky_sigma, 4),
        "k": _SUPPORT_K_DARK,
        "structure": round(_planar_residual(small, sky_sigma), 2),
    }


def compute_support(starless: np.ndarray, expected_frac: float | None = None) -> tuple[np.ndarray, dict]:
    """Deterministic subject support from a STARLESS probe array (2-D luma or
    3-D color, any scale). Returns (bool mask HxW, summary dict with keys
    area_frac / dims / source / valid / detail).

    Tier 1 is the established starless E law (sigma-clipped med + 3 sigma) --
    right for compact/medium subjects. For FRAME-FILLING signal the clipped
    median (and sigma) sit inside the pervasive structure, so tier 1 collapses
    (the held linear-autostretch sigma-clip design measured only 0.24%-1.8%
    there -- documented failure, not repeated); tier 2 first proves the
    variation is spatially COHERENT (block-mean smoothing preserves structure's
    sigma but collapses pixel noise by ~1/block) and then supports the brighter
    coherent region (smoothed field > med + 1 sigma). Pure pixel noise fails
    the coherence check -> valid=False, empty mask; downstream then uses the
    recorded conservative global fallback, never an invented subject."""
    if starless.ndim == 3:
        starless = starless.mean(axis=0 if starless.shape[0] in (3, 4) else -1)
    h, w = starless.shape
    _, med, std = sigma_clipped_stats(starless, sigma=3.0, maxiters=5)
    if not np.isfinite(med) or std <= 0:
        return np.zeros((h, w), dtype=bool), {
            "area_frac": 0.0,
            "dims": (w, h),
            "source": "none",
            "valid": False,
            "detail": "stats_unmeasurable",
        }
    hard = starless > med + _SUPPORT_K_HARD * std
    # Frame-filling collapse (Owner Gate 2, 2026-08-14): the hard tier can hold
    # plenty of PIXELS and still have collapsed onto the brightest knots of a
    # subject that fills the frame. Only an INDEPENDENT extent expectation can
    # tell that apart from a genuinely compact subject (see the module note --
    # four image-only discriminators were measured and all four invert). When the
    # catalogued extent says the subject should cover materially more of the
    # frame than we measured, re-anchor on the dark end instead.
    collapsed = (
        expected_frac is not None and expected_frac > 0 and float(hard.mean()) < _SUPPORT_COLLAPSE_RATIO * expected_frac
    )
    declined = ""
    if collapsed:
        mask, anchor = _frame_filling_support(starless, med, std)
        mask, islands, removed = _drop_speckle(mask)
        frac = float(mask.mean())
        # The catalogue only says the subject SHOULD be large; it cannot say the
        # bright region we found is astronomical. Two independent guards before
        # this replaces a measured support: the field must carry real structure
        # (a smooth ramp is amp glow, not a nebula) and the result may never
        # claim essentially the whole frame.
        if anchor["structure"] < _SUPPORT_STRUCTURE_MIN:
            declined = f"; frame-filling declined (structure {anchor['structure']} < {_SUPPORT_STRUCTURE_MIN})"
        elif frac > _SUPPORT_MAX_FRAC:
            declined = f"; frame-filling declined (would claim {frac:.4f} > {_SUPPORT_MAX_FRAC} of frame)"
        elif frac < _SUPPORT_MIN_FRAC:
            declined = f"; frame-filling declined (recovered {frac:.4f} < {_SUPPORT_MIN_FRAC})"
        else:
            return mask, {
                "area_frac": round(frac, 4),
                "dims": (w, h),
                "source": "starless_dark_anchored",
                "valid": True,
                # the one case where the broad support IS the honest solve region
                # (the object mask is known to have collapsed here) -- a typed
                # flag so the core never string-matches this vocabulary
                "recovered": True,
                "detail": (
                    f"frame-filling: hard tier {hard.mean():.4f} < {_SUPPORT_COLLAPSE_RATIO}x "
                    f"catalogued extent {expected_frac:.4f}; dark bg {anchor['bg']} "
                    f"+{anchor['k']}x{anchor['sky_sigma']}; structure {anchor['structure']}; "
                    f"dropped {islands} speckle island(s) ({removed:.4f} of frame)"
                ),
            }
    # Speckle is removed BEFORE the validity floor is applied: cleaning after the
    # check let a support pass as valid and then be emptied, publishing an
    # all-black raster while still claiming validity (Tenet 7 -- a step that
    # silently degrades is a defect).
    hard, islands, removed = _drop_speckle(hard)
    if hard.sum() >= _SUPPORT_MIN_PX:
        detail = f"med+{_SUPPORT_K_HARD}sigma"
        if islands:
            detail += f"; dropped {islands} speckle island(s) ({removed:.4f} of frame)"
        return hard, {
            "area_frac": round(float(hard.mean()), 4),
            "dims": (w, h),
            "source": "starless_sigma",
            "valid": True,
            "detail": detail + declined,
        }
    k = _SUPPORT_BLOCK
    small = _block_mean(starless, k)
    _, med_b, std_b = sigma_clipped_stats(small, sigma=3.0, maxiters=5)
    coherence = float(std_b / std) if std > 0 else 0.0
    if np.isfinite(coherence) and coherence >= _SUPPORT_COHERENCE_MIN:
        mask_small = small > med_b + _SUPPORT_K_LOW * std_b
        mask = np.zeros((h, w), dtype=bool)
        hb, wb = mask_small.shape
        mask[: hb * k, : wb * k] = np.repeat(np.repeat(mask_small, k, axis=0), k, axis=1)
        if mask.mean() >= _SUPPORT_MIN_FRAC:
            return mask, {
                "area_frac": round(float(mask.mean()), 4),
                "dims": (w, h),
                "source": "starless_low_sigma",
                "valid": True,
                "detail": f"frame-filling: hard tier {hard.mean():.4f}; coherence {coherence:.2f}" + declined,
            }
    return np.zeros((h, w), dtype=bool), {
        "area_frac": 0.0,
        "dims": (w, h),
        "source": "none",
        "valid": False,
        "detail": f"support too small (hard {hard.mean():.4f}, coherence {coherence:.2f})" + declined,
    }


def catalogued_extent_frac(
    extent_major_arcmin: float | None,
    extent_minor_arcmin: float | None,
    dims: tuple[int, int],
    pixscale_arcsec: float | None,
) -> float | None:
    """The catalogued subject's angular area as a fraction of THIS frame, or None
    when any input is missing/unusable.

    An ellipse of the catalogued major/minor axes over the frame's own measured
    field of view -- so it is instrument-agnostic by construction (the same object
    yields a different fraction on a wider camera, which is exactly right). Used
    only to notice that a measured support has COLLAPSED, never to draw one."""
    if not extent_major_arcmin or not pixscale_arcsec or pixscale_arcsec <= 0:
        return None
    minor = extent_minor_arcmin or extent_major_arcmin
    w, h = dims
    if not w or not h:
        return None
    frame_w_arcmin = w * pixscale_arcsec / 60.0
    frame_h_arcmin = h * pixscale_arcsec / 60.0
    frame_area = frame_w_arcmin * frame_h_arcmin
    if frame_area <= 0:
        return None
    subject_area = math.pi * (extent_major_arcmin / 2.0) * (minor / 2.0)
    return min(1.0, subject_area / frame_area)


def measure_subject_support(
    linear_fit: str,
    workdir: str,
    target: str,
    tools: ToolPaths,
    support_png: str,
    expected_frac: float | None = None,
) -> tuple[np.ndarray, dict]:
    """Shell wrapper: run the established quick-stretch + StarNet probe chain on
    the run's linear artifact, compute the stable support, and write it as an
    8-bit PNG raster at `support_png` (0/255). Returns (mask, summary). The
    caller (execute.take_measurement) turns the summary into the typed
    SubjectSupportObservation; the raster is the shell-side pixel data the
    solver probes and QA read back.

    ROW ORDER (review BLOCK finding, 2026-08-13): the support is computed from
    StarNet's own starless TIFF, which is ALREADY in raster order (Siril
    `savetif` writes visual raster; StarNet preserves it) -- the same order as
    every consumer of the PNG (probe JPGs, the hero contract, QA's starless
    TIFF). The FITS round-trip is deliberately avoided here: Siril 1.4.4 stamps
    a TIFF-loaded save `ROWORDER='TOP-DOWN'` while storing rows bottom-up
    (empirically pinned 2026-08-13), so no header-based flip rule is
    trustworthy on that path."""
    # tag deliberately differs from the support raster's own basename so the
    # scratch cleanup below can never race the PNG written after it
    starless_tif = _starless_probe(linear_fit, workdir, f"{target}_supprobe", tools)
    try:
        data = _read_starless_raster(starless_tif)
        mask, info = compute_support(data, expected_frac=expected_frac)
    finally:
        _cleanup_glob(workdir, f"{target}_supprobe*")
    from PIL import Image

    Image.fromarray(mask.astype(np.uint8) * 255).save(support_png)
    return mask, info


def _read_starless_raster(path: str) -> np.ndarray:
    """Starless TIFF -> float array in RASTER order (row 0 = visual top).
    tifffile first (full 16-bit); PIL fallback for LZW-compressed outputs --
    StarNet writes LZW, which tifffile only decodes with the optional
    `imagecodecs` package while PIL decodes it natively (at 8-bit for 16-bit
    RGB, ample precision for a med+3sigma support MASK on the quick-stretched
    starless; orientation verified on real StarNet output, 2026-08-13). A file
    neither reader can open raises -- the planner records the failure and the
    solve proceeds mask-free."""
    try:
        import tifffile

        return tifffile.imread(path).astype(np.float64)
    except (ImportError, OSError, ValueError):
        from PIL import Image

        return np.asarray(Image.open(path)).astype(np.float64)


def _starless_probe(linear_fit: str, workdir: str, tag: str, tools: ToolPaths) -> str:
    """The established diagnostic starless chain (shared by the E-gate probe and
    the subject-support measurement): denoise -> quick fixed stretch -> StarNet.
    Returns StarNet's starless TIFF path (raster order; inside `workdir`,
    prefixed `tag`); the caller owns cleanup of `tag*`."""
    from .adapters.graxpert import denoise
    from .adapters.siril import run_siril
    from .adapters.starnet import starnet
    from .inspect import measure

    # Denoise the probe FIRST: pre-denoise noise inflates the background sigma and buries
    # faint extended (galaxy) light below median+3sigma -> E deflates and a galaxy reads
    # as a star field (NGC5128: E 0.028 raw vs 0.040 denoised). A globular has no smooth
    # light, so it stays near zero either way. Moderate fixed 0.5 (category not known yet).
    dn = denoise(linear_fit, tools, strength=0.5, out_base=f"{tag}_dn")
    stem = Path(dn).stem
    bp = round(0.987 * measure(dn)["background"], 6)
    run_siril([f'cd "{workdir}"', f'load "{stem}"', f"asinh 50 {bp}", "mtf 0 0.1 1", f"savetif {tag}"], tools)
    out = starnet(os.path.join(workdir, tag + ".tif"), workdir, tag, tools)
    return out["starless"]


def _cleanup_glob(workdir: str, pattern: str) -> None:
    import glob as _g

    for f in _g.glob(os.path.join(workdir, pattern)):
        try:
            os.remove(f)
        except OSError:
            pass  # best-effort: incidental scratch cleanup (CHANGE_SAFETY rule 5)


def probe_extended_signal(bge_fit: str, workdir: str, target: str, tools: ToolPaths) -> float:
    """E-gate: measure extended-signal fraction E from a quick stretch + StarNet of the
    BGE output, to tell a GALAXY (smooth extended light -> E high) from a STAR FIELD
    (point sources only -> E low) when the filter alone can't (both are IRCUT). Run
    BEFORE denoise so the refined category drives the right denoise/target. Cleans up.
    Shares the diagnostic starless chain with the subject-support measurement
    (`_starless_probe` -- Task 4 refactor, behavior unchanged: the TIFF->FITS
    hop for `measure_extended_signal` lives here now, and E is a global
    fraction, orientation-invariant)."""
    from .adapters.siril import run_siril

    tmp = f"{target}_eprobe"
    starless_tif = _starless_probe(bge_fit, workdir, tmp, tools)
    try:
        run_siril([f'cd "{workdir}"', f'load "{starless_tif}"', f"save {tmp}_starless"], tools)
        return measure_extended_signal(os.path.join(workdir, f"{tmp}_starless.fit"))
    finally:
        _cleanup_glob(workdir, tmp + "*")


def measure_extended_signal(starless_fits: str) -> float:
    """E = fraction of pixels in a STARLESS image above background+3sigma -> real
    extended structure (nebula/galaxy) vs near-empty (pure star field). Expects a
    FITS path (the runner saves the StarNet starless as FITS for this)."""
    with fits.open(starless_fits) as h:
        data = h[0].data.astype(np.float64)
    if data.ndim == 3:  # collapse color to luminance
        data = data.mean(axis=0 if data.shape[0] in (3, 4) else -1)
    _, median, std = sigma_clipped_stats(data, sigma=3.0, maxiters=5)
    return float((data > median + 3 * std).mean())


def measure_concentration(linear_fit: str, block: int = 32) -> dict:
    """Detect a CENTRALLY-CONCENTRATED cluster (globular) from the data alone -- no name, no
    catalog. Block-mean downsample the luminance so point sources wash out (one star's flux
    dilutes over block^2 px) and only DIFFUSE concentration survives; the brightest downsampled
    cell is the cluster center, and `concentration` C = how many robust-sigma it stands above the
    field. A packed globular core gives a high C; a uniform/sparse field gives a low C. Returns C
    + the center in full-res pixel coords (for a halo annulus / framing). Target-agnostic."""
    with fits.open(linear_fit) as h:
        data = h[0].data.astype(np.float64)
    if data.ndim == 3:
        data = data.mean(axis=0 if data.shape[0] in (3, 4) else -1)
    H, W = data.shape
    hb, wb = H // block, W // block
    small = data[: hb * block, : wb * block].reshape(hb, block, wb, block).mean(axis=(1, 3))
    med = float(np.median(small))
    sigma = 1.4826 * float(np.median(np.abs(small - med))) or 1e-9
    cy, cx = np.unravel_index(int(small.argmax()), small.shape)
    peak = float(small[cy, cx])
    return {
        "concentration": round((peak - med) / (sigma + 1e-9), 2),
        "center_xy": [round((cx + 0.5) * block, 1), round((cy + 0.5) * block, 1)],
        "block": block,
    }
