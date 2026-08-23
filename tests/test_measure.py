import numpy as np
from astropy.io import fits
from conftest import write_fake_stack

from seestar.core.types import Measurements
from seestar.measure import measure_fits


def test_measure_returns_typed_canonical(tmp_path):
    p = write_fake_stack(tmp_path / "s.fit")  # uint16, ~1000 ADU background
    m = measure_fits(p)
    assert isinstance(m, Measurements)
    assert 0.010 < m.background < 0.020  # ~1000/65535
    assert m.dims == (64, 64)
    assert len(m.channels) == 3
    assert {c.name for c in m.channels} == {"R", "G", "B"}


def test_scale_awareness_raw_vs_float_master(tmp_path):
    """The scale-bug law: a float [0,1] master and the same data in 16-bit ADU
    must measure the SAME canonical background."""
    rng = np.random.default_rng(1)
    lin = rng.normal(0.015, 0.002, size=(3, 64, 64)).clip(0, 1).astype("float32")
    f32 = tmp_path / "master.fits"
    fits.PrimaryHDU(data=lin).writeto(f32)
    u16 = tmp_path / "raw.fits"
    fits.PrimaryHDU(data=(lin * 65535).astype("uint16")).writeto(u16)
    a, b = measure_fits(str(f32)), measure_fits(str(u16))
    assert abs(a.background - b.background) < 0.0005


def test_mono_2d(tmp_path):
    p = write_fake_stack(tmp_path / "m.fit", shape=(64, 64))
    m = measure_fits(p)
    assert len(m.channels) == 1 and m.channels[0].name == "mono"


def test_pixscale_passthrough(tmp_path):
    p = write_fake_stack(tmp_path / "s.fit")
    assert measure_fits(p, pixscale_arcsec=3.99).pixscale_arcsec == 3.99
