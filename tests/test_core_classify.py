from seestar.core.classify import classify
from seestar.core.types import (
    Category,
    CategorySource,
    ChannelStats,
    ClassifyThresholds,
    FilterKind,
    Measurements,
    SNRClass,
)

T = ClassifyThresholds()


def _meas(bgnoise=0.002):
    ch = ChannelStats(name="mono", median=0.015, sigma=bgnoise, max_value=0.9, sat_frac=0.0)
    return Measurements(
        channels=(ch,),
        background=0.015,
        bgnoise=bgnoise,
        max_value=0.9,
        dims=(1920, 1080),
        pixscale_arcsec=None,
        dynamic_range=None,
    )


def test_lp_is_nebula_provisional():
    c = classify(FilterKind.LP, 51, _meas(), T)
    assert c.category is Category.NEBULA
    assert c.extended_provisional is True
    assert c.source is CategorySource.FILTER_DEFAULT


def test_ircut_no_e_is_starfield():
    c = classify(FilterKind.IRCUT, 51, _meas(), T)
    assert c.category is Category.STARFIELD
    assert c.extended_provisional is True


def test_ircut_high_e_is_galaxy():
    c = classify(FilterKind.IRCUT, 51, _meas(), T, e_value=0.04)
    assert c.category is Category.GALAXY
    assert c.extended_fraction == 0.04
    assert c.extended_provisional is False
    assert c.source is CategorySource.E_MEASURE


def test_ircut_low_e_is_starfield():
    c = classify(FilterKind.IRCUT, 51, _meas(), T, e_value=0.01)
    assert c.category is Category.STARFIELD


def test_unknown_filter_defaults_to_starfield():
    c = classify(FilterKind.UNKNOWN, 51, _meas(), T)
    assert c.category is Category.STARFIELD


def test_snr_noisy_by_frames():
    assert classify(FilterKind.LP, 44, _meas(), T).snr_class is SNRClass.NOISY
    assert classify(FilterKind.LP, 45, _meas(), T).snr_class is SNRClass.CLEAN


def test_snr_noisy_by_bgnoise():
    assert classify(FilterKind.LP, 51, _meas(bgnoise=0.003), T).snr_class is SNRClass.NOISY


def test_pure_no_mutation():
    m = _meas()
    before = m
    classify(FilterKind.LP, 51, m, T)
    assert m == before
