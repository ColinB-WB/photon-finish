"""Deterministic tests for satellite-trail detection + inpainting (pure numpy, no
external data). A synthetic frame = smooth 'galaxy' + random 'stars' + optional thin
straight line; the detector must find the line, reject frames without one, and the
inpaint must collapse the along-line residual without flattening the galaxy."""

import numpy as np

from seestar.adapters import _trail_impl as T


def _gauss(H, W, cy, cx, sy, sx, amp):
    y, x = np.ogrid[:H, :W]
    return amp * np.exp(-(((y - cy) ** 2) / (2 * sy**2) + ((x - cx) ** 2) / (2 * sx**2)))


def _scene(H=400, W=240, seed=0, with_line=True, slope=0.2, intercept=150.0, line_amp=0.3, channels=True):
    rng = np.random.default_rng(seed)
    img = np.full((H, W), 0.010) + rng.normal(0, 0.002, (H, W))
    img += _gauss(H, W, H * 0.5, W * 0.5, 35, 30, 0.25)  # 'galaxy'
    for _ in range(30):  # 'stars'
        cy, cx = rng.uniform(0, H), rng.uniform(0, W)
        img += _gauss(H, W, cy, cx, 1.3, 1.3, rng.uniform(0.15, 0.5))
    if with_line:
        cols = np.arange(W)
        rows = np.clip(np.rint(intercept + slope * cols).astype(int), 0, H - 1)
        img[rows, cols] += line_amp  # 1px-thin trail
    img = np.clip(img, 0, None)
    if channels:
        img = np.stack([img, img * 0.9, img * 0.8], axis=0)  # 3xHxW, channel-first
    return img.astype(np.float32)


def _along_line_residual(arr, det):
    L = T._luma(arr)
    r = L - T._box_mean(L, 24)
    W = L.shape[1]
    cols = np.arange(W)
    rows = np.clip(np.rint(det.intercept + det.slope * cols).astype(int), 0, L.shape[0] - 1)
    return float(r[rows, cols].mean())


def test_detects_horizontal_trail():
    arr = _scene(with_line=True, slope=0.2, intercept=150.0)
    det = T.detect_trail(arr)
    assert det.present
    assert det.orientation == "h"
    assert abs(det.slope - 0.2) < 0.05
    assert abs(det.intercept - 150.0) < 4.0
    assert det.prominence >= 2.5
    assert det.length_frac >= 0.5


def test_no_false_positive_without_trail():
    arr = _scene(with_line=False)
    det = T.detect_trail(arr)
    assert not det.present  # galaxy + scattered stars must not read as a line


def test_inpaint_removes_the_streak():
    arr = _scene(with_line=True, slope=0.2, intercept=150.0)
    det = T.detect_trail(arr)
    before = _along_line_residual(arr, det)
    cleaned = T.inpaint_trail(arr, det)
    after = _along_line_residual(cleaned, det)
    assert after < before * 0.25  # streak energy collapsed
    assert cleaned.shape == arr.shape
    assert cleaned.dtype == arr.dtype


def test_inpaint_preserves_galaxy_gradient():
    # The galaxy center brightness must survive (interpolation across a thin band,
    # not a flatten). Compare a galaxy-core column away from the trail edges.
    arr = _scene(with_line=True, slope=0.0, intercept=150.0)
    cleaned = T.inpaint_trail(arr, T.detect_trail(arr))
    core_before = arr[0, 200, 120]  # row 200 (center), unaffected by the row-150 band
    core_after = cleaned[0, 200, 120]
    assert abs(float(core_after) - float(core_before)) < 1e-5


def test_detects_vertical_trail():
    # A near-vertical trail = a horizontal scene transposed in the spatial axes.
    h = _scene(with_line=True, slope=0.15, intercept=120.0, channels=True)
    v = np.swapaxes(h, -2, -1)  # 3 x W x H
    det = T.detect_trail(v)
    assert det.present
    assert det.orientation == "v"
    cleaned = T.inpaint_trail(v, det)
    assert cleaned.shape == v.shape


def test_remove_trail_fits_roundtrip(tmp_path):
    from astropy.io import fits

    arr = _scene(with_line=True)
    p_in = tmp_path / "in.fit"
    p_out = tmp_path / "out.fit"
    fits.writeto(p_in, arr, overwrite=True)
    info = T.remove_trail_fits(str(p_in), str(p_out), {"engine": {"trail_removal": {}}})
    assert info["present"] and p_out.exists()
    with fits.open(p_out) as h:
        assert h[0].data.shape == arr.shape

    # Trail-free input: nothing written, present False.
    arr2 = _scene(with_line=False)
    p_in2, p_out2 = tmp_path / "in2.fit", tmp_path / "out2.fit"
    fits.writeto(p_in2, arr2, overwrite=True)
    info2 = T.remove_trail_fits(str(p_in2), str(p_out2), {"engine": {"trail_removal": {}}})
    assert not info2["present"] and not p_out2.exists()
