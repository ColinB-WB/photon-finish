import dataclasses
import typing

import pytest

from seestar.core.types import (
    Category,
    EngineDefaults,
    FetchNetworkPrior,
    LookupLocalTarget,
    PerCategory,
    PriorObservation,
    TargetMetric,
    WorkRequest,
)
from seestar.io import engine_defaults_from_cfg, load_config


def test_per_category_refuses_unknown():
    p = PerCategory(nebula=0.6, galaxy=0.65, starfield=0.6)
    assert p.for_category(Category.GALAXY) == 0.65
    with pytest.raises(ValueError):
        p.for_category(Category.UNKNOWN)


def test_engine_defaults_from_real_config():
    eng = engine_defaults_from_cfg(load_config()["engine"])
    assert isinstance(eng, EngineDefaults)
    # Diagnosis 1: no automatic fixed crop -- None = measured per-edge crop, with
    # a bounded automatic trim ceiling
    assert eng.crop_border_frac is None
    assert eng.crop_max_auto_frac == 0.05
    assert eng.denoise.starfield == 0.6 and eng.denoise_noisy_bonus == 0.1
    assert eng.asinh_d.nebula == 75
    assert eng.blackpoint_factor == 0.987
    assert eng.stretch_targets.nebula.metric is TargetMetric.BODY_P90
    assert eng.stretch_targets.nebula.level == 72
    assert eng.stretch_targets.nebula.sky_ceiling == 18
    # both extended categories carry a sky ceiling (arms the two-point solve); a
    # starfield's mtf solve already targets the sky directly, so it carries none
    # (ported from tests/test_profiles.py::test_extended_targets_have_sky_ceiling,
    # Task 23 -- policy.py/profiles.py deleted).
    assert eng.stretch_targets.galaxy.sky_ceiling == 18
    assert eng.stretch_targets.starfield.metric is TargetMetric.SKY_MEDIAN
    assert eng.stretch_targets.starfield.sky_ceiling is None
    assert eng.solver.lo == 0.02 and eng.solver.max_iter == 10
    assert eng.saturation.starfield == 0.55
    assert eng.galaxy_treatment.sp == 0.15 and eng.galaxy_treatment.closeup is True
    assert eng.nebula_treatment is None  # enabled: false -> no treatment
    assert eng.cluster_treatment is not None and eng.cluster_treatment.sp == 0.20
    assert eng.cluster.concentration_min == 50.0
    assert eng.star_reduce_k == 0.45
    assert eng.print_black_lift == 0.05
    assert eng.e_gate is False and eng.catalog_prior is True
    assert eng.combine_sessions is False
    assert eng.trail_removal_enabled is True
    assert eng.classify.extended_signal_threshold == 0.03


def test_engine_defaults_from_empty_cfg_matches_config_yaml():
    """The in-code defaults ARE config.yaml's numbers (one source of truth rule:
    a cfg-less build must behave identically to the shipped config)."""
    assert engine_defaults_from_cfg({}) == engine_defaults_from_cfg(load_config()["engine"])


def test_lookup_local_target_and_fetch_network_prior_are_frozen_work_requests():
    """Task 3: LookupLocalTarget (new, always-on local catalog lookup) and
    FetchNetworkPrior (renamed from FetchPrior, IRCUT-only network fetch) must
    both be frozen, no-field WorkRequest members."""
    local = LookupLocalTarget()
    network = FetchNetworkPrior()

    with pytest.raises(dataclasses.FrozenInstanceError):
        local.x = 1  # type: ignore[attr-defined]
    with pytest.raises(dataclasses.FrozenInstanceError):
        network.x = 1  # type: ignore[attr-defined]

    assert dataclasses.fields(LookupLocalTarget) == ()
    assert dataclasses.fields(FetchNetworkPrior) == ()

    members = typing.get_args(WorkRequest)
    assert LookupLocalTarget in members
    assert FetchNetworkPrior in members


def test_prior_observation_has_no_footprint_or_morphology_fields():
    """Type-shape inertness guard: PriorObservation is the reduced core boundary
    (category-bearing only). Task 3 must NOT add footprint/morphology/
    search_footprints -- that richness stays shell-side (LocalTargetRecord)."""
    field_names = {f.name for f in dataclasses.fields(PriorObservation)}
    assert "footprint" not in field_names
    assert "morphology" not in field_names
    assert "search_footprints" not in field_names
    assert "lookup_role" not in field_names  # shell-side matching-authority field, must not cross the core boundary
