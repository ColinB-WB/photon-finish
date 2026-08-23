import dataclasses

import pytest

import seestar.core.types as T
from seestar.core.classify import classify
from seestar.core.policy import PROFILE_OVERLAYS, compute_decisions
from seestar.core.types import (
    Category,
    ChannelStats,
    ClassifyThresholds,
    FilterKind,
    GhsParams,
    Measurements,
    Overrides,
    SNRClass,
)
from seestar.io import engine_defaults_from_cfg

ENG = engine_defaults_from_cfg({})  # in-code defaults == config.yaml (Task 6 test)


def _meas(pixscale=3.99, bgnoise=0.002):
    ch = ChannelStats(name="mono", median=0.015, sigma=bgnoise, max_value=0.9, sat_frac=0.0)
    return Measurements(
        channels=(ch,),
        background=0.015,
        bgnoise=bgnoise,
        max_value=0.9,
        dims=(1920, 1080),
        pixscale_arcsec=pixscale,
        dynamic_range=None,
    )


def _cls(filt=FilterKind.LP, frames=51, e=None, m=None):
    return classify(filt, frames, m or _meas(), ClassifyThresholds(), e_value=e)


def test_nebula_showcase_defaults():
    d = compute_decisions(_cls(FilterKind.LP), _meas(), ENG, "showcase")
    assert d.category is Category.NEBULA and d.snr_class is SNRClass.CLEAN
    assert d.denoise_strength == 0.6 and d.asinh_d == 75 and d.saturation == 0.60
    assert d.pcc is False  # LP: PCC always off
    assert d.blackpoint == round(0.987 * 0.015, 6)
    assert d.ght is None  # nebula treatment disabled in config
    assert d.star_forks is True and d.ghs_allowed is True and d.sky_solve is True
    assert d.black_lift == 0.0 and d.annotate is False
    assert d.stretch_target_scale == 1.0  # showcase: unscaled solved target level


def test_galaxy_gets_pcc_and_ghs_and_closeup():
    d = compute_decisions(_cls(FilterKind.IRCUT, e=0.04), _meas(), ENG, "showcase")
    assert d.category is Category.GALAXY and d.pcc is True
    assert isinstance(d.ght, GhsParams) and d.ght.sp == 0.15 and d.ght.closeup is True
    assert d.denoise_strength == 0.65


def test_starfield_pcc_off_no_ghs():
    d = compute_decisions(_cls(FilterKind.IRCUT, e=0.01), _meas(), ENG, "showcase")
    assert d.category is Category.STARFIELD and d.pcc is False
    assert d.ght is None and d.star_forks is False and d.saturation == 0.55


def test_noisy_scales_target_level():
    d = compute_decisions(_cls(frames=30), _meas(), ENG, "showcase")
    assert d.snr_class is SNRClass.NOISY
    assert d.stretch_target_scale == 0.92
    assert d.denoise_strength == round(0.6 + 0.1, 3)


def test_natural_overlay():
    d = compute_decisions(_cls(FilterKind.IRCUT, e=0.04), _meas(), ENG, "natural")
    assert d.ght is None and d.ghs_allowed is False  # honest base stretch
    assert d.star_forks is False
    assert d.saturation == round(0.60 * 0.6, 3)
    assert d.stretch_target_scale == 0.95
    assert d.pcc is True  # engine rule untouched


def test_documentation_overlay():
    d = compute_decisions(_cls(), _meas(), ENG, "documentation")
    assert d.saturation == 0.1 and d.stretch_target_scale == 0.7
    assert d.blackpoint_scale == 0.97 and d.sky_solve is False and d.annotate is True
    assert d.ghs_allowed is False and d.ght is None  # honest base stretch, no GHS


def test_print_overlay():
    d = compute_decisions(_cls(FilterKind.IRCUT, e=0.04), _meas(), ENG, "print")
    assert d.black_lift == 0.05 and d.denoise_strength == round(0.65 + 0.1, 3)
    assert d.ghs_allowed is True and d.star_forks is True


def test_no_profile_touches_pcc():
    for name in PROFILE_OVERLAYS:
        assert PROFILE_OVERLAYS[name].pcc is None, f"{name} must not own the PCC decision"


def test_sharpening_cap_from_measured_pixscale():
    eng = dataclasses.replace(ENG, galaxy_treatment=dataclasses.replace(ENG.galaxy_treatment, unsharp_amount=0.5))
    d = compute_decisions(_cls(FilterKind.IRCUT, e=0.04), _meas(pixscale=3.99), eng, "showcase")
    assert d.ght.unsharp_amount == 0.0 and d.sharpening_capped is not None
    d2 = compute_decisions(_cls(FilterKind.IRCUT, e=0.04), _meas(pixscale=2.0), eng, "showcase")
    assert d2.ght.unsharp_amount == 0.5 and d2.sharpening_capped is None


def test_unknown_profile_raises():
    with pytest.raises(ValueError):
        compute_decisions(_cls(), _meas(), ENG, "wow")


def test_decisions_frozen_and_pure():
    m = _meas()
    c = _cls()
    d = compute_decisions(c, m, ENG, "showcase")
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.saturation = 9.9
    assert compute_decisions(c, m, ENG, "showcase") == d  # same inputs -> same output


# ---- controller precedence (Task 22 examples, pulled forward into Task 18) ----


def test_override_precedence_engine_profile_prefs_pins_caller():
    cls, m = _cls(), _meas()
    prefs = T.Overrides(saturation=0.9)
    pins = T.Overrides(saturation=0.7)
    caller = T.Overrides(saturation=0.5)
    assert compute_decisions(cls, m, ENG, "showcase", prefs=prefs).saturation == 0.9
    assert compute_decisions(cls, m, ENG, "showcase", prefs=prefs, pins=pins).saturation == 0.7
    assert compute_decisions(cls, m, ENG, "showcase", prefs=prefs, pins=pins, caller=caller).saturation == 0.5


def test_prefs_profile_override_reenters_overlay():
    d = compute_decisions(_cls(), _meas(), ENG, "showcase", prefs=T.Overrides(profile="natural"))
    assert d.profile == "natural" and d.ghs_allowed is False


def test_caller_profile_beats_pins_and_prefs():
    d = compute_decisions(
        _cls(),
        _meas(),
        ENG,
        "showcase",
        prefs=Overrides(profile="natural"),
        pins=Overrides(profile="documentation"),
        caller=Overrides(profile="print"),
    )
    assert d.profile == "print"


def test_star_forks_override_can_only_undo():
    # extended + showcase -> star_forks True by the category rule; a False pin turns it off
    d_off = compute_decisions(_cls(FilterKind.LP), _meas(), ENG, "showcase", pins=Overrides(star_forks=False))
    assert d_off.star_forks is False
    # a True override cannot force forks onto a starfield (AND with the category rule)
    d_sf = compute_decisions(_cls(FilterKind.IRCUT, e=0.01), _meas(), ENG, "showcase", pins=Overrides(star_forks=True))
    assert d_sf.category is Category.STARFIELD and d_sf.star_forks is False


def test_structure_disabled_strips_ght():
    d = compute_decisions(
        _cls(FilterKind.IRCUT, e=0.04), _meas(), ENG, "showcase", caller=Overrides(structure_enabled=False)
    )
    assert d.category is Category.GALAXY and d.ght is None and d.structure_enabled is False


def test_new_decision_fields_default():
    d = compute_decisions(_cls(), _meas(), ENG, "showcase")
    assert d.structure_enabled is True and d.closeup_enabled is True
    assert d.trail_removal_enabled is ENG.trail_removal_enabled
    assert d.stretch_target_level is None and d.star_reduce_k == ENG.star_reduce_k


# ---- ported from tests/test_profiles.py (Task 23: policy.py/profiles.py deleted;
# tests/test_core_policy.py is the single typed-level home for this coverage) ----

_ALL_PROFILES = ("showcase", "natural", "documentation", "print")


def test_profiles_produce_distinct_overlays_and_exclusive_flags():
    """The four profiles must differ in the knobs that define their §8a intent;
    documentation is the only annotated profile, print the only black-lifted one."""
    got = {p: compute_decisions(_cls(FilterKind.LP), _meas(), ENG, p) for p in _ALL_PROFILES}
    keyset = ("saturation", "stretch_target_scale", "ghs_allowed", "star_forks", "pcc")
    signatures = {p: tuple(getattr(got[p], k) for k in keyset) for p in _ALL_PROFILES}
    assert len(set(signatures.values())) == 4, f"profiles not distinct: {signatures}"
    assert [p for p in _ALL_PROFILES if got[p].annotate] == ["documentation"]
    assert [p for p in _ALL_PROFILES if got[p].black_lift] == ["print"]


def test_no_profile_overrides_engine_pcc_across_all_profiles_and_categories():
    """2026-07 M81 red-cast fix: showcase/print forcing pcc=False shipped an
    uncalibrated red cast on IRCUT galaxies -- the one case where the engine's
    filter/category rule turns PCC on. No profile may own the decision, for any
    of the four profiles (not just showcase, which the other per-profile tests
    in this file exercise)."""
    for prof in _ALL_PROFILES:
        assert PROFILE_OVERLAYS[prof].pcc is None, f"{prof} must defer PCC to the engine"
        assert compute_decisions(_cls(FilterKind.IRCUT, e=0.04), _meas(), ENG, prof).pcc is True
        assert compute_decisions(_cls(FilterKind.LP), _meas(), ENG, prof).pcc is False
        assert compute_decisions(_cls(FilterKind.IRCUT, e=0.01), _meas(), ENG, prof).pcc is False


def test_sky_solve_gating_across_all_profiles():
    """Two-point solve (2026-07 M81 gray-wash fix): profiles that promise a dark
    background allow the bp-raising sky solve; documentation's full-DR promise
    forbids it. showcase/print/documentation already pinned individually above
    (test_nebula_showcase_defaults/test_print_overlay/test_documentation_overlay);
    natural was not -- closing that gap here as a loop over all four."""
    for prof in _ALL_PROFILES:
        d = compute_decisions(_cls(FilterKind.IRCUT, e=0.04), _meas(), ENG, prof)
        assert d.sky_solve is (prof != "documentation")


def test_natural_scale_preserves_background():
    """2026-07 bracket lock: 'gentler' must not mean 'much dimmer' -- at scale 0.85
    the sky measured 5.5% near-black (crushed); 0.95 measured 0.8% (preserved) while
    still visibly dimmer than showcase. Guard the bracketed floor."""
    assert PROFILE_OVERLAYS["natural"].target_level_scale >= 0.95


def test_natural_strips_nebula_ghs_when_enabled():
    """Natural strips ANY category's GHS (showcase/natural galaxy case already covers
    the `if not ov.ghs: ght = None` branch generically via test_natural_overlay), but
    nebula_treatment is disabled by default in config (test_nebula_showcase_defaults)
    so there is nothing to strip there normally -- force it on to exercise the branch
    for the nebula category specifically."""
    import dataclasses

    eng = dataclasses.replace(ENG, nebula_treatment=ENG.galaxy_treatment)  # any enabled GhsParams
    show = compute_decisions(_cls(FilterKind.LP), _meas(), eng, "showcase")
    nat = compute_decisions(_cls(FilterKind.LP), _meas(), eng, "natural")
    assert show.ght is not None, "showcase honors an enabled nebula GHS treatment"
    assert nat.ght is None, "natural renders the honest base stretch (no GHS)"
    assert nat.saturation < show.saturation
    assert nat.stretch_target_scale < show.stretch_target_scale


def test_print_profile_paper_tuning_vs_showcase():
    show = compute_decisions(_cls(FilterKind.LP), _meas(), ENG, "showcase")
    pr = compute_decisions(_cls(FilterKind.LP), _meas(), ENG, "print")
    assert pr.black_lift > 0 and show.black_lift == 0.0
    assert pr.denoise_strength > show.denoise_strength  # cleaner for paper
    assert pr.saturation < show.saturation  # print-safe gamut


def test_star_forks_matches_category_and_profile_across_all_profiles():
    """star_forks = category in {NEBULA, GALAXY} AND profile.star_forks (the
    core/policy.py formula) -- ported from tests/test_star_treatment.py (Task 23):
    extended + a star-taming profile (showcase/print) gets the three-look set;
    natural/documentation stay stars-on; starfield (clusters) never star-treated,
    any profile, because the stars ARE the subject."""
    for prof in _ALL_PROFILES:
        expect_forks = PROFILE_OVERLAYS[prof].star_forks
        for filt, e in ((FilterKind.LP, None), (FilterKind.IRCUT, 0.04)):
            d = compute_decisions(_cls(filt, e=e), _meas(), ENG, prof)
            assert d.category in (Category.NEBULA, Category.GALAXY)
            assert d.star_forks is expect_forks
        starfield = compute_decisions(_cls(FilterKind.IRCUT, e=0.01), _meas(), ENG, prof)
        assert starfield.category is Category.STARFIELD and starfield.star_forks is False
