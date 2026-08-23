"""Background neutralization after PCC (2026-07 M81 gray-wash fix).

PCC calibrates channel gains to catalog STAR colors and says nothing about the sky:
post-PCC the channel backgrounds diverge, and the scalar black point (0.987 x mean
median) then sits below the brightest channel's sky, rendering a tinted gray wash.
neutralize_background applies per-channel OFFSETS so the sky medians re-align while
star/body color (differences ABOVE the sky) is untouched.
"""

import numpy as np
from astropy.io import fits

from seestar.adapters.siril import neutralize_background


def _color_fit(tmp_path, offsets=(0.020, 0.012, 0.015), star=0.5):
    rng = np.random.default_rng(7)
    data = rng.normal(0.0, 0.0005, size=(3, 64, 64)).astype(np.float32)
    for i, off in enumerate(offsets):
        data[i] += off
        data[i, 30:34, 30:34] += star  # a "star" riding on each channel's sky
    p = str(tmp_path / "lin.fit")
    fits.PrimaryHDU(data=data).writeto(p)
    return p


def test_sky_medians_equalized_and_signal_preserved(tmp_path):
    p = _color_fit(tmp_path)
    info = neutralize_background(p)
    data = fits.open(p)[0].data
    meds = [float(np.median(data[i])) for i in range(3)]
    assert max(meds) - min(meds) < 1e-4, "channel sky medians must align"
    assert abs(np.mean(meds) - info["medians_after"]) < 1e-3
    # star signal ABOVE each channel's sky is an offset-invariant -- color preserved
    for i in range(3):
        assert abs((data[i, 31, 31] - meds[i]) - 0.5) < 0.01


def test_mono_is_a_noop(tmp_path):
    p = str(tmp_path / "mono.fit")
    fits.PrimaryHDU(data=np.full((16, 16), 0.02, dtype=np.float32)).writeto(p)
    before = fits.open(p)[0].data.copy()
    assert neutralize_background(p)["medians_before"] is None
    assert np.array_equal(fits.open(p)[0].data, before)


def test_integer_data_promoted_not_wrapped(tmp_path):
    data = np.zeros((3, 16, 16), dtype=np.uint16)
    data[0] += 1300
    data[1] += 1200
    data[2] += 1250
    p = str(tmp_path / "int.fit")
    fits.PrimaryHDU(data=data).writeto(p)
    neutralize_background(p)
    out = fits.open(p)[0].data
    assert np.issubdtype(out.dtype, np.floating)
    assert np.all(out > 0), "offset subtraction must not wrap"
    assert np.allclose([np.median(out[i]) for i in range(3)], 1250, atol=1)
