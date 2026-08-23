"""Core type contracts: frozen, validated, UNKNOWN-explicit."""

import dataclasses

import pytest

from seestar.core.types import (
    Category,
    CategorySource,
    ChannelStats,
    Classification,
    ClassifyThresholds,
    FilterKind,
    Measurements,
    SNRClass,
)


def _stats(**kw):
    d = dict(name="R", median=0.015, sigma=0.002, max_value=0.9, sat_frac=0.0)
    d.update(kw)
    return ChannelStats(**d)


def _meas(**kw):
    d = dict(
        channels=(_stats(), _stats(name="G"), _stats(name="B")),
        background=0.015,
        bgnoise=0.002,
        max_value=0.9,
        dims=(1920, 1080),
        pixscale_arcsec=3.99,
        dynamic_range=440.0,
    )
    d.update(kw)
    return Measurements(**d)


def test_types_are_frozen():
    m = _meas()
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.background = 0.5
    c = Classification(
        category=Category.NEBULA,
        snr_class=SNRClass.CLEAN,
        filter_kind=FilterKind.LP,
        extended_fraction=None,
        extended_provisional=True,
        source=CategorySource.FILTER_DEFAULT,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.category = Category.GALAXY


def test_measurements_collections_are_tuples():
    m = _meas()
    assert isinstance(m.channels, tuple)
    assert isinstance(m.dims, tuple)


def test_bounds_validation():
    with pytest.raises(ValueError):
        _stats(median=-0.1)
    with pytest.raises(ValueError):
        _stats(sat_frac=1.5)
    with pytest.raises(ValueError):
        _meas(background=1.5)  # canonical space is unit-float
    with pytest.raises(ValueError):
        _meas(bgnoise=-1.0)
    with pytest.raises(ValueError):
        _meas(dims=(0, -5))
    with pytest.raises(ValueError):
        _meas(max_value=2.5)
    with pytest.raises(ValueError):
        _meas(pixscale_arcsec=-1)
    with pytest.raises(ValueError):
        Classification(
            category=Category.NEBULA,
            snr_class=SNRClass.CLEAN,
            filter_kind=FilterKind.LP,
            extended_fraction=5.0,  # a pixel fraction — must be in [0,1]
            extended_provisional=False,
            source=CategorySource.E_MEASURE,
        )


def test_filter_kind_from_raw():
    assert FilterKind.from_raw("LP") is FilterKind.LP
    assert FilterKind.from_raw("lp") is FilterKind.LP
    assert FilterKind.from_raw("IRCUT") is FilterKind.IRCUT
    assert FilterKind.from_raw(None) is FilterKind.UNKNOWN
    assert FilterKind.from_raw("weird") is FilterKind.UNKNOWN


def test_unknown_variants_exist():
    # absence is a typed variant, never None (spec section 5)
    assert Category.UNKNOWN.value == "unknown"
    assert FilterKind.UNKNOWN.value == "unknown"


def test_classify_thresholds_defaults_mirror_config():
    t = ClassifyThresholds()
    assert t.extended_signal_threshold == 0.03
    assert t.noisy_frames_below == 45
    assert t.noisy_bgnoise_above == 0.0025


def test_hero_quality_report_is_frozen_and_typed():
    from seestar.core.types import Category, GateResult, HeroOutcome, HeroQualityReport, QAReport, Verdict

    qa = QAReport(category=Category.NEBULA, gates=(GateResult("fwhm", Verdict.PASS, ""),), warnings=(), passed=True)
    report = HeroQualityReport(
        outcome=HeroOutcome.PASS,
        qa=qa,
        solve_converged=True,
        contract_delta=0.3,
        source_limited_gates=(),
        processing_failed_gates=(),
        reasons=(),
    )
    assert report.outcome is HeroOutcome.PASS
    import dataclasses

    assert dataclasses.is_dataclass(report)
    try:
        report.outcome = HeroOutcome.PROCESSING_FAILED  # type: ignore[misc]
        raise AssertionError("HeroQualityReport must be frozen")
    except dataclasses.FrozenInstanceError:
        pass


def test_artifact_role_has_a_candidate_member():
    from seestar.core.types import ArtifactRole

    assert ArtifactRole.CANDIDATE.value == "candidate"


def test_export_defaults_to_deliverable_role_unchanged():
    from seestar.core.types import ArtifactRef, ArtifactRole, Export

    ref = ArtifactRef("linear_master", ArtifactRole.PRODUCTION)
    exp = Export(input=ref, output_base="T_final")
    assert all(o.role is ArtifactRole.DELIVERABLE for o in exp.outputs)


def test_export_can_target_candidate_role():
    from seestar.core.types import ArtifactRef, ArtifactRole, Export

    ref = ArtifactRef("linear_master", ArtifactRole.PRODUCTION)
    exp = Export(input=ref, output_base="T_final", role=ArtifactRole.CANDIDATE)
    assert all(o.role is ArtifactRole.CANDIDATE for o in exp.outputs)
