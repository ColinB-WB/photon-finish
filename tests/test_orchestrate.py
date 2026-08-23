"""Task 19 acceptance: the planner-driven orchestrator, end-to-end with fake tools
(same boundary fakes as test_run_ops). Asserts the plan sidecar lands, the
deliverable set is unchanged, and no op executed twice in the recorded execution.

Placeholder target name "X1" (never a real astronomical object) per CLAUDE.md's
target-agnostic rule."""

from __future__ import annotations

import json
import os

import pytest

import seestar.orchestrate as orch
import seestar.qa as qa_module
from seestar.io import load_config

FAKE_FIT_NAME = "Stacked_51_X1_10.0s_LP_20260615-221349.fit"


class _FakeTools:
    siril = "siril-cli"
    graxpert = "GraXpert"
    starnet2 = "starnet2"
    exiftool = "exiftool"


def _make_target_dir(tmp_path):
    from conftest import write_fake_stack

    target_dir = tmp_path / "X1_import"
    target_dir.mkdir()
    write_fake_stack(target_dir / FAKE_FIT_NAME, obj="X1")
    return str(target_dir)


def _cfg():
    cfg = load_config()
    cfg["engine"]["trail_removal"]["enabled"] = False
    cfg["engine"]["catalog_prior"] = False
    return cfg


def _apply_mocks(monkeypatch):
    from conftest import (
        fake_measure_subject_support,
        fake_probe_metric,
        install_fake_op_adapters,
        install_fake_run_siril,
    )

    import seestar.execute as exec_module

    monkeypatch.setattr(orch, "resolve_tools", lambda cfg: _FakeTools())
    monkeypatch.setattr(orch, "embed", lambda *a, **kw: None)
    install_fake_op_adapters(monkeypatch)
    install_fake_run_siril(monkeypatch)
    monkeypatch.setattr(exec_module, "_probe_metric", fake_probe_metric)
    import seestar.measure as measure_module

    monkeypatch.setattr(measure_module, "measure_subject_support", fake_measure_subject_support)
    monkeypatch.setattr(qa_module, "input_star_metrics", lambda *a, **kw: {})
    # The immediate hero evaluation measures faint-structure evidence off the
    # candidate starless + support rasters (owner ruling, 2026-08-14). This
    # harness renders synthetic pixels, so pin HEALTHY evidence here the same way
    # `_passing_metrics` pins healthy metrics -- the call site stays live, and
    # the immediate-vs-replay parity invariant is proven against the real
    # rasters in tests/test_qa_gate_parity.py.
    from seestar.core.types import FaintEvidence

    monkeypatch.setattr(orch, "faint_evidence", lambda *a, **kw: (FaintEvidence(0.9, 0.2, "valid"), None))


def _run(monkeypatch, target_dir, out_dir, **kw):
    _apply_mocks(monkeypatch)
    return orch.run_target(target_dir, out_dir, _cfg(), **kw)


def _force_pass(monkeypatch):
    """Make evaluate_hero classify the candidate PASS. REQUIRED for any certified-path
    test: the hermetic harness's fake renders fail star_count/fwhm/sky_median by
    default, so without this every end-to-end run is processing_failed."""
    from dataclasses import replace as dc_replace

    from seestar.core.types import HeroOutcome

    real = orch.evaluate_hero

    def _passed(*a, **kw):
        hero = real(*a, **kw)
        return dc_replace(
            hero, outcome=HeroOutcome.PASS, processing_failed_gates=(), source_limited_gates=(), reasons=()
        )

    monkeypatch.setattr(orch, "evaluate_hero", _passed)


# --- Task 5: shell dispatch for LookupLocalTarget / FetchNetworkPrior ------------

_KT_FRAME_RA, _KT_FRAME_DEC = 150.0, -10.0


def _add_wcs(fits_path, ra_deg, dec_deg):
    """Stamp a minimal, real celestial WCS onto a fake stack (`write_fake_stack`
    omits one) -- a CD matrix, not CDELT, matching what `inspect.read_header`
    actually parses for `pixscale_arcsec` (CD1_1/CD1_2)."""
    from astropy.io import fits as _afits

    with _afits.open(fits_path, mode="update") as hdul:
        h = hdul[0].header
        h["CTYPE1"] = "RA---TAN"
        h["CTYPE2"] = "DEC--TAN"
        h["CRVAL1"] = ra_deg
        h["CRVAL2"] = dec_deg
        h["CRPIX1"] = 32.5
        h["CRPIX2"] = 32.5
        h["CD1_1"] = -0.0011
        h["CD1_2"] = 0.0
        h["CD2_1"] = 0.0
        h["CD2_2"] = 0.0011
        hdul.flush()


def _add_wcs_pc_cdelt(fits_path, ra_deg, dec_deg):
    """Stamp a PC+CDELT WCS (no CD keys at all) -- the Bug-1 regression case:
    `inspect.read_header` only special-cases a CD1_1/CD1_2 matrix into
    `pixscale_arcsec`, so a header plate-solved into this (equally valid) FITS WCS
    convention has `has_wcs=True` but `pixscale_arcsec` absent."""
    from astropy.io import fits as _afits

    with _afits.open(fits_path, mode="update") as hdul:
        h = hdul[0].header
        h["CTYPE1"] = "RA---TAN"
        h["CTYPE2"] = "DEC--TAN"
        h["CRVAL1"] = ra_deg
        h["CRVAL2"] = dec_deg
        h["CRPIX1"] = 32.5
        h["CRPIX2"] = 32.5
        h["CDELT1"] = -0.0011
        h["CDELT2"] = 0.0011
        h["PC1_1"] = 1.0
        h["PC1_2"] = 0.0
        h["PC2_1"] = 0.0
        h["PC2_2"] = 1.0
        hdul.flush()


def _add_wcs_no_pixel_scale(fits_path, ra_deg, dec_deg):
    """Stamp a WCS with CTYPE/CRVAL/CRPIX but NO CD/PC/CDELT convention at all --
    `has_wcs` reads True (CTYPE1 + CRVAL1 present) and `field_center()` still solves
    (CRPIX/CRVAL alone are enough for `pixel_to_world`), but there is genuinely no
    usable pixel scale anywhere in the header: neither `_fov_long_arcmin` (no
    `pixscale_arcsec`) nor `_fov_long_arcmin_from_wcs` (no CD/PC/CDELT key present)
    can derive a FOV. The regression this guards: `match_target`'s
    `_fov_radius_arcmin` does `max(0.0, fov_arcmin)` -- passing None crashes with
    TypeError; the dispatch must instead return a clean recorded miss."""
    from astropy.io import fits as _afits

    with _afits.open(fits_path, mode="update") as hdul:
        h = hdul[0].header
        h["CTYPE1"] = "RA---TAN"
        h["CTYPE2"] = "DEC--TAN"
        h["CRVAL1"] = ra_deg
        h["CRVAL2"] = dec_deg
        h["CRPIX1"] = 32.5
        h["CRPIX2"] = 32.5
        hdul.flush()


def _make_ircut_target_dir(tmp_path, name="X2", wcs_fn=_add_wcs):
    from conftest import write_fake_stack

    target_dir = tmp_path / f"{name}_import"
    target_dir.mkdir()
    fit_path = target_dir / f"Stacked_51_{name}_10.0s_IRCUT_20260615-221349.fit"
    write_fake_stack(fit_path, obj=name, filt="IRCUT")
    wcs_fn(fit_path, _KT_FRAME_RA, _KT_FRAME_DEC)
    return str(target_dir)


def _local_galaxy_record(entry_id="NGC0000", category=None, common_name=None):
    from seestar.core.types import Category
    from seestar.known_targets import LocalTargetRecord

    return LocalTargetRecord(
        entry_id=entry_id,
        canonical_id=entry_id,
        common_name=common_name,
        aliases=(),
        equivalent_ids=(),
        category=category if category is not None else Category.GALAXY,
        ra_deg=_KT_FRAME_RA,
        dec_deg=_KT_FRAME_DEC,
        extent_major_arcmin=20.0,
        extent_minor_arcmin=10.0,
        search_footprints=(),
        morphology_traits=(),
        associated=(),
        provenance={},
        lookup_role="primary",
    )


def test_local_catalog_hit_decides_ircut_galaxy_with_no_network_call(tmp_path, monkeypatch):
    """Value-gate mechanism in miniature: a WCS-matched local catalog record settles
    IRCUT galaxy-vs-starfield entirely offline. `core.planning`'s `reconcile()` owns
    the category call, not any shell-side gate (spec sec5.5) -- so a catalog GALAXY
    hit overrides the provisional IRCUT FILTER_DEFAULT STARFIELD guess, and the
    network SIMBAD path is never touched."""
    import seestar.known_targets as kt
    import seestar.prior as prior_module

    target_dir = _make_ircut_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")

    record = _local_galaxy_record()
    monkeypatch.setattr(kt, "load_catalog", lambda *a, **kw: (record,))

    def _boom(*a, **kw):
        raise AssertionError("query_simbad must not be called on a local hit")

    monkeypatch.setattr(prior_module, "query_simbad", _boom)

    result = _run(monkeypatch, target_dir, out_dir, catalog_prior=True)

    assert result["params"]["category"] == "galaxy"


def test_missing_local_catalog_falls_back_to_ircut_network_path(tmp_path, monkeypatch):
    """An empty/corrupt bundled catalog degrades to `local_catalog_unavailable`
    (spec sec5.7) and IRCUT still gets its second-round network fallback -- the two
    ops compose instead of a missing local catalog silently stalling."""
    import seestar.known_targets as kt
    import seestar.prior as prior_module

    target_dir = _make_ircut_target_dir(tmp_path, name="X3")
    out_dir = str(tmp_path / "out")

    load_calls = []
    monkeypatch.setattr(kt, "load_catalog", lambda *a, **kw: (load_calls.append(1), ())[1])

    calls = []

    def _fake_query_simbad(ra, dec, **kw):
        calls.append((ra, dec))
        return [
            {
                "main_id": "NGC0000",
                "otype": "G",
                "otype_txt": "Galaxy",
                "ra": ra,
                "dec": dec,
                "galdim_majaxis": 20.0,
                "galdim_minaxis": 10.0,
                "dist": 0.0,
            }
        ]

    monkeypatch.setattr(prior_module, "query_simbad", _fake_query_simbad)

    _apply_mocks(monkeypatch)
    cfg = _cfg()
    cfg["engine"]["catalog_prior"] = True
    cfg["prior"] = {**cfg.get("prior", {}), "cache_dir": str(tmp_path / "prior_cache")}

    result = orch.run_target(target_dir, out_dir, cfg, catalog_prior=True)

    assert load_calls, "known_targets.load_catalog() must actually be consulted (local_catalog_unavailable path)"
    assert calls, "the IRCUT network path (query_simbad) must run after a local-catalog miss"
    assert result["params"]["category"] == "galaxy"


# --- Task 6: --no-prior decouples LookupLocalTarget (always-on) from FetchNetworkPrior --


def test_no_prior_still_runs_local_lookup_and_never_calls_network(tmp_path, monkeypatch):
    """Spec sec5.7: `--no-prior` (catalog_prior=False / use_prior=False) disables
    ONLY FetchNetworkPrior. LookupLocalTarget is always-on for a plate-solved frame,
    so a local HIT must still settle the category entirely offline, and the network
    (SIMBAD) path must never be touched even though it would normally be eligible
    for this IRCUT frame."""
    import seestar.known_targets as kt
    import seestar.prior as prior_module

    target_dir = _make_ircut_target_dir(tmp_path, name="X4")
    out_dir = str(tmp_path / "out")

    record = _local_galaxy_record()
    monkeypatch.setattr(kt, "load_catalog", lambda *a, **kw: (record,))

    def _boom(*a, **kw):
        raise AssertionError("query_simbad must never be called under --no-prior")

    monkeypatch.setattr(prior_module, "query_simbad", _boom)

    result = _run(monkeypatch, target_dir, out_dir, catalog_prior=False)

    assert result["params"]["category"] == "galaxy"
    assert result["notes"]["local_prior"]["outcome"] == "local-hit"
    assert "identity" in result["notes"]["local_prior"]["influenced"]
    assert "category" in result["notes"]["local_prior"]["influenced"]


def test_no_prior_local_miss_never_falls_back_to_network(tmp_path, monkeypatch):
    """The decoupling's other half: on a local MISS under --no-prior, the network
    round -- which WOULD fire for this same IRCUT frame with use_prior on (see
    `test_missing_local_catalog_falls_back_to_ircut_network_path`) -- must be
    suppressed. Category falls back to the IRCUT filter default (starfield)."""
    import seestar.known_targets as kt
    import seestar.prior as prior_module

    target_dir = _make_ircut_target_dir(tmp_path, name="X5")
    out_dir = str(tmp_path / "out")

    monkeypatch.setattr(kt, "load_catalog", lambda *a, **kw: ())  # empty catalog -> miss

    def _boom(*a, **kw):
        raise AssertionError("query_simbad must never be called under --no-prior")

    monkeypatch.setattr(prior_module, "query_simbad", _boom)

    result = _run(monkeypatch, target_dir, out_dir, catalog_prior=False)

    assert result["params"]["category"] == "starfield"
    assert result["notes"]["local_prior"]["outcome"] == "local_catalog_unavailable"


# --- P1 bug fix: missing pixel-scale must never crash the catalog lookup --------


def test_local_lookup_derives_fov_from_full_wcs_when_pixscale_absent(tmp_path, monkeypatch):
    """Bug 1: a header plate-solved into a PC+CDELT convention (no CD matrix) has
    `has_wcs=True` but `pixscale_arcsec` absent (`inspect.read_header` only
    special-cases CD1_1/CD1_2). Before the fix, `_prior._fov_long_arcmin(header)`
    returned None and `match_target`'s `_fov_radius_arcmin` did `max(0.0, None)` ->
    TypeError, crashing the render AFTER a successful WCS solve. The fix must derive
    the FOV from the full WCS instead and let the lookup proceed to a hit, exactly
    like the CD-matrix case."""
    import seestar.known_targets as kt
    import seestar.prior as prior_module

    target_dir = _make_ircut_target_dir(tmp_path, name="X6", wcs_fn=_add_wcs_pc_cdelt)
    out_dir = str(tmp_path / "out")

    record = _local_galaxy_record()
    monkeypatch.setattr(kt, "load_catalog", lambda *a, **kw: (record,))

    def _boom(*a, **kw):
        raise AssertionError("query_simbad must not be called on a local hit")

    monkeypatch.setattr(prior_module, "query_simbad", _boom)

    result = _run(monkeypatch, target_dir, out_dir, catalog_prior=True)

    assert result["params"]["category"] == "galaxy"
    assert result["notes"]["local_prior"]["outcome"] == "local-hit"


def test_local_lookup_no_usable_pixel_scale_is_clean_miss_not_a_crash(tmp_path, monkeypatch):
    """Bug 1's other half: a header with a real WCS (`has_wcs=True`, `field_center()`
    solves) but genuinely no pixel-scale convention anywhere (no CD, no PC, no
    CDELT) must never crash the render. It must degrade to a clean recorded miss
    (category falls back to the IRCUT filter default) with a status distinguishable
    from a plain spatial miss, never an unhandled exception."""
    import seestar.known_targets as kt
    import seestar.prior as prior_module

    target_dir = _make_ircut_target_dir(tmp_path, name="X7", wcs_fn=_add_wcs_no_pixel_scale)
    out_dir = str(tmp_path / "out")

    record = _local_galaxy_record()
    monkeypatch.setattr(kt, "load_catalog", lambda *a, **kw: (record,))

    def _boom(*a, **kw):
        raise AssertionError("query_simbad must never be called under --no-prior")

    monkeypatch.setattr(prior_module, "query_simbad", _boom)

    # must not raise
    result = _run(monkeypatch, target_dir, out_dir, catalog_prior=False)

    assert result["params"]["category"] == "starfield"  # IRCUT filter default, unaffected
    lp = result["notes"]["local_prior"]
    assert lp["outcome"] == "missing"
    assert lp.get("reason") == "no_fov"


def test_local_lookup_failed_plate_solve_is_clean_miss_distinct_from_spatial_miss(tmp_path, monkeypatch):
    """final-fixwave (f7d0e84): a header that CLAIMS a WCS (`has_wcs=True`: CTYPE1 +
    CRVAL1 present) but whose full plate solve fails -- `prior.field_center()` returns
    None -- must degrade to a clean recorded miss, never crash, and must be
    distinguishable from a plain spatial miss (`reason == "wcs_solve_failed"`, not a
    bare "nothing matched"). Sibling of the `no_fov` case above; both are Tenet-7
    graceful-degradation branches introduced by the P1 fixwave."""
    import seestar.known_targets as kt
    import seestar.prior as prior_module

    target_dir = _make_ircut_target_dir(tmp_path, name="X8", wcs_fn=_add_wcs)  # has_wcs=True
    out_dir = str(tmp_path / "out")

    record = _local_galaxy_record()
    monkeypatch.setattr(kt, "load_catalog", lambda *a, **kw: (record,))
    # simulate a malformed/failed plate solve: WCS present in header, but field_center
    # cannot be computed.
    monkeypatch.setattr(prior_module, "field_center", lambda *a, **kw: None)

    def _boom(*a, **kw):
        raise AssertionError("query_simbad must never be called under --no-prior")

    monkeypatch.setattr(prior_module, "query_simbad", _boom)

    # must not raise
    result = _run(monkeypatch, target_dir, out_dir, catalog_prior=False)

    assert result["params"]["category"] == "starfield"  # IRCUT filter default, unaffected
    lp = result["notes"]["local_prior"]
    assert lp["outcome"] == "missing"
    assert lp.get("reason") == "wcs_solve_failed"


def test_no_wcs_frame_records_advisory_note_and_category_unaffected(tmp_path, monkeypatch):
    """Spec sec5.7: without a WCS, LookupLocalTarget is never even dispatched
    (planning.py gates it on has_wcs) -- but the run must still record an honest
    advisory note (Tenet 7: no silent skip), clearly marked as name-only and
    non-authoritative, and category must be unaffected (LP filter law still owns
    it)."""
    target_dir = _make_target_dir(tmp_path)  # LP, no WCS (conftest's write_fake_stack default)
    out_dir = str(tmp_path / "out")

    result = _run(monkeypatch, target_dir, out_dir)

    lp = result["notes"]["local_prior"]
    assert lp["advisory"] == "no_wcs"
    assert lp["influenced"] == []
    assert result["params"]["category"] == "nebula"


def test_local_hit_unknown_category_is_identity_only_not_contradicted(tmp_path, monkeypatch):
    """A catalog record with `Category.UNKNOWN` asserts NO category, so a WCS hit on it
    can never be `contradicted` (there is nothing to disagree with the final
    classification). It must be reported as a plain `local-hit` with
    `local_disposition == "identity_only"`, influence `["identity"]`, and NO warning --
    and it must not change any decision (category stays the catalog-absent IRCUT
    filter default)."""
    import seestar.known_targets as kt
    import seestar.prior as prior_module
    from seestar.core.types import Category

    target_dir = _make_ircut_target_dir(tmp_path, name="X9", wcs_fn=_add_wcs)  # has_wcs=True
    out_dir = str(tmp_path / "out")

    record = _local_galaxy_record(category=Category.UNKNOWN)  # WCS-matched, but no type claim
    monkeypatch.setattr(kt, "load_catalog", lambda *a, **kw: (record,))

    def _boom(*a, **kw):
        raise AssertionError("query_simbad must never be called under --no-prior")

    monkeypatch.setattr(prior_module, "query_simbad", _boom)

    result = _run(monkeypatch, target_dir, out_dir, catalog_prior=False)  # network off

    lp = result["notes"]["local_prior"]
    assert lp["outcome"] == "local-hit"
    assert lp["local_disposition"] == "identity_only"
    assert lp["influenced"] == ["identity"]
    assert "warning" not in lp
    # decisions unaffected: category is the catalog-absent IRCUT filter default.
    assert result["params"]["category"] == "starfield"


def test_contradicted_local_hit_produces_warn_note():
    """Unit coverage of `_prior_outcome_note`'s post-classification contradiction
    check: it compares the catalog hit's ASSERTED category (`record.category`)
    against the FINAL authoritative classification `state.classification.category`
    -- this REPLACES the old pre-measurement `contradicted` flag that was gated on
    LookupLocalTarget-dispatch-time source and was structurally unreachable (that
    dispatch always runs in SOURCE round 1, before `state.classification` settles).
    The end-to-end version of this exact scenario (an LP frame whose local hit
    asserts a non-nebula category, disagreeing with the LP-locked NEBULA filter law)
    is `tests/test_known_targets_ablation.py::
    test_type_disagreeing_entry_keeps_identity_drops_hint_uses_generic_path`, which
    now asserts "contradicted" is REACHABLE through a real planner run."""
    from seestar.core.types import Category, CategorySource, Classification, FilterKind, SNRClass
    from seestar.orchestrate import _prior_outcome_note

    record = _local_galaxy_record(entry_id="NGC9999")  # category=GALAXY, disagrees with LP-locked NEBULA
    lp = {"status": "hit", "record": record, "conflict": "type_disagreement"}

    class _FakeState:
        # This is the FINAL classification (post-planning) that `_prior_outcome_note`
        # is called with -- NEBULA here, differing from the record's asserted GALAXY.
        classification = Classification(
            category=Category.NEBULA,
            snr_class=SNRClass.CLEAN,
            filter_kind=FilterKind.LP,
            extended_fraction=None,
            extended_provisional=True,
            source=CategorySource.FILTER_DEFAULT,
        )

    note = _prior_outcome_note(lp, None, _FakeState())
    assert note["outcome"] == "contradicted"
    assert note["local_disposition"] == "contradicted"
    assert "warning" in note and "NGC9999" in note["warning"]
    assert "category" not in note["influenced"]  # the disputed hint was dropped, never applied
    assert "identity" in note["influenced"]  # identity is preserved regardless


def test_agreeing_local_hit_has_agreed_disposition_no_warning():
    """The non-contradiction control for the same helper: when the catalog hit's
    asserted category AGREES with the final classification, `local_disposition`
    reads "agreed" and no warning is emitted."""
    from seestar.core.types import Category, CategorySource, Classification, FilterKind, SNRClass
    from seestar.orchestrate import _prior_outcome_note

    record = _local_galaxy_record(entry_id="NGC0001")  # category=GALAXY

    class _FakeState:
        classification = Classification(
            category=Category.GALAXY,  # agrees with the record
            snr_class=SNRClass.CLEAN,
            filter_kind=FilterKind.IRCUT,
            extended_fraction=None,
            extended_provisional=True,
            source=CategorySource.CATALOG_PRIOR,
        )

    lp = {"status": "hit", "record": record, "conflict": "none"}
    note = _prior_outcome_note(lp, None, _FakeState())
    assert note["outcome"] == "local-hit"
    assert note["local_disposition"] == "agreed"
    assert "warning" not in note
    assert set(note["influenced"]) == {"identity", "category"}


def test_plan_sidecar_lands_with_format_and_no_duplicate_work(tmp_path, monkeypatch):
    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")

    _force_pass(monkeypatch)  # certified-path intent: asserts the certified X1_plan.json name
    result = _run(monkeypatch, target_dir, out_dir)
    assert result["params"]["category"] == "nebula"

    sidecar = os.path.join(out_dir, "X1_plan.json")
    assert os.path.exists(sidecar)
    doc = json.loads(open(sidecar, encoding="utf-8").read())
    assert doc["plan_format"] == 1
    assert set(doc) == {"plan_format", "plan", "execution"}

    # ops carry their class-name discriminator; deliverables are refs
    assert all("op" in s for s in doc["plan"]["steps"])
    assert doc["plan"]["deliverables"], "the plan must ship deliverables"

    # no op executed (ok/failed) more than once in the recorded execution
    counts: dict[str, int] = {}
    for r in doc["execution"]["records"]:
        if r["status"] in ("ok", "failed"):
            counts[r["op_key"]] = counts.get(r["op_key"], 0) + 1
    dupes = {k: c for k, c in counts.items() if c > 1}
    assert not dupes, f"duplicate work: {dupes}"


def test_deliverable_set_conservative_showcase_nebula(tmp_path, monkeypatch):
    """LP nebula under showcase (Phase 3A conservative star policy) -> the hero
    _final is the stars-on look; star-reduced and starless ship as variants."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")

    _force_pass(monkeypatch)  # certified-path intent: asserts the certified X1_final/* names
    result = _run(monkeypatch, target_dir, out_dir)

    for base in ("X1_final", "X1_star-reduced", "X1_starless"):
        for ext in ("jpg", "png", "tif"):
            assert os.path.exists(os.path.join(out_dir, f"{base}.{ext}")), f"missing {base}.{ext}"
    # the legacy separate _stars-on file is gone -- _final now carries that look
    assert not os.path.exists(os.path.join(out_dir, "X1_stars-on.jpg"))
    assert {o["variant"] for o in result["outputs"]} == {"stars-on", "starless", "star-reduced"}


def test_analyze_emits_step_one_of_n(tmp_path, monkeypatch):
    """Progress honesty: "analyze" is announced BEFORE the raw measurement runs,
    with (0, 0) because the step total is unknown until planning seeds — the GUI
    shows the bare label and suppresses the step counter. Once planning starts,
    the same stage is re-announced with the seeded (1, N) — the displayed
    counter still never reads "Step 0/0"."""
    target_dir = _make_target_dir(tmp_path)
    calls = []

    _run(
        monkeypatch,
        target_dir,
        str(tmp_path / "out"),
        on_stage=lambda key, label, tool, i, n: calls.append((key, i, n)),
    )

    assert calls[0] == ("analyze", 0, 0), f"first event must be the pre-measurement analyze, got {calls[0]}"
    assert calls[1][0] == "analyze"
    assert calls[1][1] == 1 and calls[1][2] > 1, f"seeded analyze emitted {calls[1]}"


def test_recipe_trail_removal_value_honors_override_off(tmp_path, monkeypatch):
    """decisions_to_params carries trail_removal_enabled, so the emitted recipe's
    displayed current value tracks a pin/pref that disabled the stage (the render
    always honored it; the display used to fall back to the engine default).
    The fake render cannot pass the real QA gates (no _force_pass here), so this
    is a processing_failed run -- Task 6 emits its recipe under the uncertified
    namespace, never the certified name, so the recipe filename is resolved from
    `result["published"]["certified"]` rather than hardcoded."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")
    _apply_mocks(monkeypatch)
    cfg = load_config()
    cfg["engine"]["catalog_prior"] = False  # trail_removal stays ON in the engine

    result = orch.run_target(target_dir, out_dir, cfg, overrides={"trail_removal_enabled": False})

    assert result["params"]["trail_removal_enabled"] is False
    recipe_name = "X1_recipe.json" if result["published"]["certified"] else "X1_uncertified_recipe.json"
    doc = json.loads(open(os.path.join(out_dir, recipe_name), encoding="utf-8").read())
    by = {e["adjustment"]: e for e in doc["adjustments"]}
    assert by["trail_removal"]["value"] is False


def test_run_surfaces_hero_status_consistently(tmp_path, monkeypatch):
    """Phase 3C: the hero outcome stops being swallowed. run_target's return dict
    carries a status, and it agrees with what the manifest and the notes sidecar
    record -- one honest outcome, surfaced everywhere. (The fake render cannot pass
    the real QA gates, so the specific value is exercised end-to-end on hardware;
    here we pin that the three surfaces never disagree.) Task 3: the manifest's
    location is namespace-aware (certified vs uncertified), so it is resolved via
    the returned `published` sidecars rather than a hardcoded name."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")

    result = _run(monkeypatch, target_dir, out_dir)

    status = result["status"]
    assert status in {"pass", "source_limited", "processing_failed"}
    assert result["published"]["certified"] is (status != "processing_failed")
    run_name = result["published"]["sidecars"]["run"]
    manifest = json.loads(open(os.path.join(out_dir, run_name), encoding="utf-8").read())
    assert manifest["outcome"] == status
    assert result["notes"]["hero_contract"]["outcome"] == status


def test_processing_failed_publishes_uncertified_never_final(tmp_path, monkeypatch):
    """Successor to test_processing_failed_surfaces_status_but_keeps_final: the
    Phase 3C best-effort-_final decision is REVERSED (spec 2026-08-13). A failed
    hero ships as <T>_uncertified.* with uncertified sidecars; no _final is
    created or modified."""
    import seestar.execute as exec_module

    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")

    _apply_mocks(monkeypatch)
    # body_p90 probe never reaches the target (72) -> solver never converges.
    monkeypatch.setattr(exec_module, "_probe_metric", lambda jpg, metric, mask_cfg=None, **kw: 10.0)

    result = orch.run_target(target_dir, out_dir, _cfg())

    assert result["status"] == "processing_failed"
    assert not os.path.exists(os.path.join(out_dir, "X1_final.tif"))
    assert not os.path.exists(os.path.join(out_dir, "X1_final.jpg"))
    assert not os.path.exists(os.path.join(out_dir, "X1_final.png"))
    assert not os.path.exists(os.path.join(out_dir, "X1_run.json"))
    assert not os.path.exists(os.path.join(out_dir, "X1_plan.json"))
    assert not os.path.exists(os.path.join(out_dir, "X1_notes.json"))
    assert os.path.exists(os.path.join(out_dir, "X1_uncertified.tif"))
    assert os.path.exists(os.path.join(out_dir, "X1_uncertified.jpg"))
    assert os.path.exists(os.path.join(out_dir, "X1_uncertified_run.json"))
    assert os.path.exists(os.path.join(out_dir, "X1_uncertified_plan.json"))
    assert os.path.exists(os.path.join(out_dir, "X1_uncertified_notes.json"))
    with open(os.path.join(out_dir, "X1_uncertified_run.json")) as f:
        man = json.load(f)
    assert man["outcome"] == "processing_failed"
    for d in man["deliverables"]:
        for ext in ("tif", "png", "jpg"):
            if d.get(ext):
                assert "_uncertified" in os.path.basename(d[ext])
    assert result["published"]["certified"] is False
    assert result["published"]["hero_base"] == "X1_uncertified"


def test_fail_closed_when_active_star_and_fwhm_thresholds_have_no_measurement(tmp_path, monkeypatch):
    """M2: an ACTIVE star_preservation/fwhm_ceiling with no `findstar` measurement
    (the detector failed) must ship PROCESSING_FAILED, never a silent PASS -- the
    real run_target harness end to end, not just the pure evaluate_gates unit.

    The category and BOTH thresholds are pinned explicitly in this test's config
    (never relying on config.yaml's ambient defaults): X1/LP resolves to NEBULA
    (conftest's write_fake_stack default, no catalog prior), and `star_count`/
    `fwhm` measurements are left UNPATCHED (unlike `_drive_body_sat`, which patches
    `orch.star_count` healthy) -- the fake `run_siril` (installed by
    `install_fake_run_siril`) returns "" for the `findstar` script line, so the
    real `qa.star_count` genuinely measures (None, None), exactly the scenario
    this fail-closed policy exists for.

    Asserts every promised surface independently (CHANGE_SAFETY rule 8): the
    returned result, the persisted notes sidecar, the persisted run manifest, and
    a persisted QA report (the separate `seestar qa` pass over the same real
    deliverables) all agree -- and the source .fit is untouched throughout.

    qa_target namespace assertions (plan Task 11): a standalone `seestar qa`
    pass over this same UNCERTIFIED output resolves the uncertified namespace
    (never crashing on the pre-namespace `{target}_final.jpg` guess), and its
    persisted scorecard reports the manifest's own outcome honestly."""
    target_dir = _make_target_dir(tmp_path)
    source_fit = os.path.join(target_dir, FAKE_FIT_NAME)
    source_bytes_before = open(source_fit, "rb").read()
    out_dir = str(tmp_path / "out")

    _apply_mocks(monkeypatch)
    cfg = _cfg()
    # Pin the category-active thresholds explicitly -- this test is the sole
    # coverage of the fail-closed contract end-to-end, so it must not depend on
    # config.yaml's ambient values ever staying what they are today.
    cfg["qa"]["gates"]["enabled"] = True
    cfg["qa"]["gates"]["star_preservation"] = {"min_retention": 0.5, "catastrophe_floor": 20}
    cfg["qa"]["gates"]["fwhm_ceiling"] = {"nebula": 3.3, "starfield": 3.6, "galaxy": 3.5}

    result = orch.run_target(target_dir, out_dir, cfg)

    assert result["params"]["category"] == "nebula"  # sanity: the pinned threshold applies

    # (1) returned result
    assert result["status"] == "processing_failed"

    # (2) returned hero-contract reasons name star_preservation (and fwhm),
    # and the recorded evidence carries the fail-closed measurement state
    reasons = " ".join(result["notes"]["hero_contract"]["reasons"])
    assert "star_preservation" in reasons, reasons
    assert "fwhm" in reasons, reasons
    sp = result["notes"]["hero_contract"]["star_preservation"]
    assert sp["output_count"] is None  # findstar failed -> recorded, not hidden

    # (3) persisted {target}_notes.json agrees with the returned notes
    notes_doc = json.loads(open(os.path.join(out_dir, "X1_uncertified_notes.json"), encoding="utf-8").read())
    assert notes_doc["notes"]["hero_contract"] == result["notes"]["hero_contract"]

    # (5) committed {target}_run.json outcome
    manifest = json.loads(open(os.path.join(out_dir, "X1_uncertified_run.json"), encoding="utf-8").read())
    assert manifest["outcome"] == "processing_failed"
    # Task 4: the extended run's frozen subject-support raster is published as a
    # manifest-referenced qa_artifact inside the same transaction
    assert "X1_support_uncertified.png" in manifest["qa_artifacts"]
    assert os.path.exists(os.path.join(out_dir, "X1_support_uncertified.png"))

    # (4) a standalone `seestar qa` pass resolves the UNCERTIFIED namespace from
    # this run's own manifest (never crashing on the pre-namespace `X1_final.jpg`
    # guess, which does not exist for an uncertified publication) and reports
    # the manifest's own outcome honestly in sc["publication"]; the persisted
    # scorecard round-trips the returned dict exactly (CHANGE_SAFETY rule 7).
    # Category/profile are pinned the same way `seestar qa` normally reads them
    # (embedded XMP), since `embed` is faked to a no-op in this harness --
    # without the pin the category-active star/fwhm gates below would be
    # skipped rather than fail-closed. `faint_structure_from_render` is stubbed
    # because the fake starless deliverable is an empty stub file (not a real
    # image) -- irrelevant to the star_count/fwhm verdicts under test.
    monkeypatch.setattr(qa_module, "resolve_tools", lambda cfg: _FakeTools())
    monkeypatch.setattr(
        qa_module, "read_xmp_subject", lambda render, cfg, tools=None: "nebula, LP, ZWO Seestar S30, profile:showcase"
    )
    monkeypatch.setattr(qa_module, "faint_structure_from_render", lambda *a, **kw: (None, None))

    sc = qa_module.qa_target(out_dir, cfg, target="X1")
    assert sc["publication"]["manifest_outcome"] == result["status"] == "processing_failed"

    # star_preservation/fwhm gate-verdict + "unavailable" detail assertions (M2 fail-closed):
    # with the category pin restored, the recomputation genuinely fails closed on
    # the same missing findstar measurement the original run hit, so the recomputed
    # hero outcome and the returned result's status agree -- the four-surface
    # agreement the docstring promises.
    gates_by_name = {g["gate"]: g for g in sc["gate_report"]["gates"]}
    assert gates_by_name["star_preservation"]["verdict"] == "fail"
    assert gates_by_name["star_preservation"]["detail"].startswith("star_preservation unavailable")
    assert gates_by_name["fwhm"]["verdict"] == "fail"
    assert gates_by_name["fwhm"]["detail"].startswith("fwhm unavailable")
    assert sc["hero"]["outcome"] == result["status"]

    persisted_qa = json.loads(open(os.path.join(out_dir, "X1_uncertified_qa.json"), encoding="utf-8").read())
    assert persisted_qa == sc

    # Diagnosis 2: the immediate run QA and the later `seestar qa` replay judge
    # star preservation from the SAME persisted source evidence (the run's
    # notes.input_quality), so their typed records agree field-for-field here
    # (both measurements failed in this harness -> both sides record that state).
    assert sc["star_preservation"]["input_count"] == sp["input_count"]
    assert sc["star_preservation"]["output_count"] == sp["output_count"]
    assert notes_doc["notes"]["input_quality"]["status"] == "unavailable"

    # (6) no source file changed
    assert open(source_fit, "rb").read() == source_bytes_before


def _spy_saturations(monkeypatch):
    """Record the StretchRender saturation of every plan handed to execute_plan,
    in call order (base render first, then any recovery re-renders)."""
    from seestar.core.types import StretchRender

    real = orch.execute_plan
    satus: list[list[float]] = []

    def spy(plan, ctx, **kw):
        satus.append([op.saturation for op in plan.steps if isinstance(op, StretchRender)])
        return real(plan, ctx, **kw)

    monkeypatch.setattr(orch, "execute_plan", spy)
    return satus


def test_chroma_recovery_rerenders_at_lower_saturation_and_promotes(tmp_path, monkeypatch):
    """Phase 3B: a hero that fails the pegged_chroma gate triggers ONE lower-
    saturation re-render (bracket[0]=0.85), and the recovered candidate is the one
    promoted. Variants are re-finalized at the winning saturation; the manifest and
    notes record the recovery attempt."""
    from seestar.core.recovery import RecoveryAction, RecoveryDecision

    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")
    _apply_mocks(monkeypatch)
    _force_pass(monkeypatch)  # certified-path intent: asserts the certified X1_final/X1_run.json names
    satus = _spy_saturations(monkeypatch)

    # drive the loop directly: one RECOVER, then ACCEPT (policy is unit-tested)
    actions = iter([RecoveryAction.RECOVER, RecoveryAction.ACCEPT, RecoveryAction.ACCEPT])
    monkeypatch.setattr(
        orch,
        "decide_candidate",
        lambda hero, attempt, max_attempts, **kw: RecoveryDecision(next(actions), "test"),
    )

    result = orch.run_target(target_dir, out_dir, _cfg())

    assert len(satus) == 2, "base render + exactly one recovery re-render"
    base, recovered = satus[0][0], satus[1][0]
    assert recovered == round(base * 0.85, 4)  # lowered through bracket[0]
    assert os.path.exists(os.path.join(out_dir, "X1_final.jpg"))
    manifest = json.loads(open(os.path.join(out_dir, "X1_run.json"), encoding="utf-8").read())
    assert manifest["attempt"] == 1
    assert result["notes"]["hero_contract"]["attempt"] == 1
    assert result["notes"]["hero_contract"]["recovered_saturation"] == recovered
    # variants ship at their canonical names (re-finalized from the recovered render)
    for base_name in ("X1_star-reduced", "X1_starless"):
        assert os.path.exists(os.path.join(out_dir, f"{base_name}.jpg")), f"missing {base_name}.jpg"


def test_chroma_recovery_is_bounded_by_the_bracket(tmp_path, monkeypatch):
    """Phase 3B: the loop cannot run away. Even if the policy keeps asking to
    RECOVER, the bracket length (2) caps re-renders, then the best-effort image is
    still shipped -- but (Task 3, spec rev 6) an exhausted-bracket render is still
    genuinely PROCESSING_FAILED (the harness's fake renders fail the real gates by
    construction, and this test does not force a PASS), so it ships under the
    `_uncertified` namespace, never `_final`."""
    from seestar.core.recovery import RecoveryAction, RecoveryDecision

    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")
    _apply_mocks(monkeypatch)
    satus = _spy_saturations(monkeypatch)

    monkeypatch.setattr(
        orch,
        "decide_candidate",
        lambda hero, attempt, max_attempts, **kw: RecoveryDecision(RecoveryAction.RECOVER, "x"),
    )

    orch.run_target(target_dir, out_dir, _cfg())

    assert len(satus) == 3, "base render + at most len(bracket)=2 recovery re-renders"
    assert not os.path.exists(os.path.join(out_dir, "X1_final.jpg"))
    assert os.path.exists(os.path.join(out_dir, "X1_uncertified.jpg"))
    manifest = json.loads(open(os.path.join(out_dir, "X1_uncertified_run.json"), encoding="utf-8").read())
    assert manifest["attempt"] == 2  # capped at the bracket length


def test_source_limited_fwhm_excused_and_pipeline_limited_fwhm_is_soft(tmp_path, monkeypatch):
    """The source-limited fwhm attribution is wired into the RUNTIME hero eval. With
    the render fwhm always failing the ceiling, the outcome must depend on the INPUT:
    - input worse than the ceiling => SOURCE-LIMITED, fwhm excused from processing fails
      (this is what lets a soft capture reach the chroma-only condition 3B needs);
    - input sharper than the ceiling => PIPELINE-LIMITED -- fwhm never becomes a
      "hard gate(s) failed" reason either way (2026-08-01 policy change, the M8
      false-positive finding: an end user should never see a failure screen over
      stars that came out a bit fat). It's still measured and still recorded, just
      as a non-blocking soft quality note rather than a hard gate -- a real
      pipeline defect stays visible to whoever reads the notes, it just doesn't
      alarm the person who only wants their JPG.
    """
    import seestar.qa as qa_module

    target_dir = _make_target_dir(tmp_path)

    def hero_reasons_for(input_fwhm, out):
        _apply_mocks(monkeypatch)  # resets input_star_metrics to {}
        monkeypatch.setattr(orch, "star_count", lambda *a, **kw: (500, 99.0))  # render fwhm fails
        monkeypatch.setattr(
            qa_module,
            "input_star_metrics",
            lambda *a, **kw: {"fwhm_px": input_fwhm, "star_count": 500, "measured_on": "linear"},
        )
        result = orch.run_target(target_dir, out, _cfg())
        return result["notes"]["hero_contract"]["reasons"]

    # input already over the ceiling (99px) -> data-limited -> fwhm excused. (This
    # fake harness's other fixed metrics also trip sky_median/drift here, so the
    # outcome itself isn't PASS/SOURCE_LIMITED -- the fwhm-specific claim is just
    # that it never appears as a hard gate reason.)
    src_reasons = hero_reasons_for(99.0, str(tmp_path / "out_src"))
    src_hard = [r for r in src_reasons if r.startswith("hard gate")]
    assert not any("fwhm" in r for r in src_hard), f"source-limited fwhm must be excused: {src_hard}"

    # input sharp (1px) but render bloated -> pipeline's fault -> fwhm is a soft
    # note, never a hard gate reason
    pipe_reasons = hero_reasons_for(1.0, str(tmp_path / "out_pipe"))
    pipe_hard = [r for r in pipe_reasons if r.startswith("hard gate")]
    assert not any("fwhm" in r for r in pipe_hard), f"pipeline-limited fwhm must not be a hard gate: {pipe_hard}"
    assert any("soft quality note" in r and "fwhm" in r for r in pipe_reasons), (
        f"pipeline-limited fwhm must still be recorded as a soft note: {pipe_reasons}"
    )


def test_combined_sessions_provenance_caveat(tmp_path, monkeypatch):
    """A SUCCESSFUL opt-in combine ships combined pixels with latest-session frame
    metadata (re-measure deferred, spec section 7) -- never silently: the
    multi_session note records the caveat."""
    from conftest import write_fake_stack

    target_dir = tmp_path / "X3_import"
    target_dir.mkdir()
    write_fake_stack(
        target_dir / "Stacked_40_X3_10.0s_LP_20250601-000000.fit",
        obj="X3",
        stackcnt=40,
        date_obs="2025-06-01T00:00:00",
        seed=1,
    )
    write_fake_stack(target_dir / "Stacked_51_X3_10.0s_LP_20260615-221349.fit", obj="X3", stackcnt=51, seed=2)
    _apply_mocks(monkeypatch)
    cfg = _cfg()
    cfg["engine"]["combine_sessions"] = True

    result = orch.run_target(str(target_dir), str(tmp_path / "out"), cfg)

    ms = result["notes"]["multi_session"]
    assert ms["mode"] == "combined" and ms["sessions"] == 2
    assert "re-measure deferred" in ms["provenance_caveat"]
    assert "combine_failed" not in result["notes"]


def _passing_metrics(body_sat):
    """A full measure_render-style dict that passes every gate, varying only
    body_saturation_median."""
    return {
        "dimensions": [1080, 1920],
        "envelope_area_frac": 0.20,
        "body_p90": 72.0,
        "body_p95": 90.0,
        "body_rgb": [40.0, 22.0, 20.0],
        "background_median_outside_mask": 10.0,
        "near_black_frac_outside_mask": 0.0,
        "core_sat_frac": 0.0,
        "pegged_chroma_frac_body": 0.05,  # well under the 0.20 neon ceiling
        "body_saturation_median": body_sat,
        "core_saturation_median": 0.5,
        "star_saturation_median": 0.1,
        "global_black_frac": 0.5,
    }


def _drive_body_sat(monkeypatch, values):
    it = iter(values)
    monkeypatch.setattr(orch, "measure_render", lambda *a, **kw: _passing_metrics(next(it)))
    # keep star_count / input fwhm passing so the hero is a clean PASS
    monkeypatch.setattr(orch, "star_count", lambda *a, **kw: (800, 2.5))
    monkeypatch.setattr(
        qa_module, "input_star_metrics", lambda *a, **kw: {"fwhm_px": 2.4, "star_count": 800, "measured_on": "linear"}
    )


def test_aesthetic_backoff_lowers_saturation_and_ships_pass(tmp_path, monkeypatch):
    """A clean PASS render measured over the ceiling backs off through the bracket and
    ships the FIRST candidate under the ceiling -- as PASS, not PROCESSING_FAILED."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")
    _apply_mocks(monkeypatch)
    satus = _spy_saturations(monkeypatch)
    _drive_body_sat(monkeypatch, [0.82, 0.70, 0.55])  # base, after 0.85x, after 0.70x

    cfg = _cfg()
    cfg["engine"]["recovery"]["body_saturation_ceiling"] = {"nebula": 0.60, "starfield": 0.60, "galaxy": 0.60}
    result = orch.run_target(target_dir, out_dir, cfg)

    assert len(satus) == 3, "base + two aesthetic re-renders before dropping under 0.60"
    assert result["status"] == "pass"
    manifest = json.loads(open(os.path.join(out_dir, "X1_run.json"), encoding="utf-8").read())
    assert manifest["outcome"] == "pass"
    assert manifest["attempt"] == 2
    # honest reporting: the target WAS met (final 0.55 <= ceiling 0.60)
    sr = result["notes"]["hero_contract"]["saturation_recovery"]
    assert sr["status"] == "target_met" and sr["target_met"] is True
    assert sr["ceiling"] == 0.60 and sr["initial_metric"] == 0.82 and sr["final_metric"] == 0.55


def test_aesthetic_backoff_exhausted_still_ships_pass(tmp_path, monkeypatch):
    """Bracket can't reach the ceiling -> ship the lowest-satu best-effort candidate,
    still PASS (over-saturation is an aesthetic target, never PROCESSING_FAILED)."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")
    _apply_mocks(monkeypatch)
    satus = _spy_saturations(monkeypatch)
    _drive_body_sat(monkeypatch, [0.90, 0.88, 0.86])  # never reaches 0.60

    cfg = _cfg()
    cfg["engine"]["recovery"]["body_saturation_ceiling"] = {"nebula": 0.60, "starfield": 0.60, "galaxy": 0.60}
    result = orch.run_target(target_dir, out_dir, cfg)

    assert len(satus) == 3  # base + bracket length (2)
    assert result["status"] == "pass"  # NOT processing_failed
    # honest reporting: manifest/notes must NOT imply the target was met when it wasn't
    sr = result["notes"]["hero_contract"]["saturation_recovery"]
    assert sr["status"] == "best_effort_exhausted" and sr["target_met"] is False
    assert sr["ceiling"] == 0.60 and sr["initial_metric"] == 0.90 and sr["final_metric"] == 0.86


def test_aesthetic_backoff_dormant_when_ceiling_unset(tmp_path, monkeypatch):
    """Default config ships body_saturation_ceiling null -> no aesthetic backoff even
    on a high body_saturation_median (parity: today's behavior)."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")
    _apply_mocks(monkeypatch)
    satus = _spy_saturations(monkeypatch)
    _drive_body_sat(monkeypatch, [0.95])
    result = orch.run_target(target_dir, out_dir, _cfg())  # default cfg: ceiling null
    assert len(satus) == 1, "no re-render when the aesthetic ceiling is unset"
    # inert path: no saturation_recovery block at all (nothing to report)
    assert "saturation_recovery" not in result["notes"]["hero_contract"]


def test_planning_path_timeout_message_survives_to_friendly_error(tmp_path, monkeypatch):
    """C2 (final review): an ExternalToolTimeout raised deep inside a production op
    (here BackgroundExtract, standing in for the real GraXpert adapter) must reach
    `run_target`'s caller with BOTH of the GUI's `_friendly_error` markers intact --
    not eaten by execute_ops's op-repr-heavy ExecutionError message, nor by the
    200-char truncation in orchestrate.py's `_do` boundary. Before the fix, `_do`
    built `reason = f"{type(e).__name__}: {str(e)[:200]}"` from an ExecutionError
    whose message was `f"{repr(op)}: {exc}"` -- repr(op) alone runs ~195 chars for a
    real op, so the actionable timeout guidance never survived into the 200-char cap,
    and `_friendly_error` fell through to its generic fallback copy."""
    import sys

    from seestar.adapters import base as B
    from seestar.adapters._external_process import run_external
    from seestar.core.types import BackgroundExtract
    from seestar.gui.views import friendly_error

    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")
    _apply_mocks(monkeypatch)

    class _SleeperAdapter:
        """Stands in for the real GraXpert BGE adapter: routes through the REAL
        `run_external` against a real, genuinely-hanging subprocess with a tiny
        configured timeout, so the ExternalToolTimeout raised here is the real
        production exception, not a stand-in string."""

        name = "fake-graxpert-sleeper"

        def execute(self, op, ctx):
            tools = B.ToolPaths(
                siril="s",
                graxpert="g",
                starnet2="n",
                exiftool="e",
                timeouts=B.ToolTimeouts(siril=1800.0, graxpert=0.15, starnet2=3600.0, exiftool=120.0),
            )
            run_external(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                tool="graxpert",
                tools=tools,
            )
            return {}  # unreached -- run_external raises

    monkeypatch.setitem(B.REGISTRY, BackgroundExtract, _SleeperAdapter())

    with pytest.raises(RuntimeError) as exc_info:
        orch.run_target(target_dir, out_dir, _cfg())

    message = str(exc_info.value)
    lower = message.lower()
    assert "did not finish within" in lower, message
    assert "no source photo was changed" in lower, message

    friendly = friendly_error(message)
    assert friendly == message, (
        "expected friendly_error to recognize the timeout markers and return the actionable "
        f"ExternalToolTimeout copy unchanged, got the generic fallback instead: {friendly!r}"
    )


def test_run_target_rejects_out_name_outside_the_safe_set(tmp_path):
    """Names are not injective (spec §1 rev 5): out_name='starless' would collide with
    the showcase starless fork's uncertified name. Hard listed error, before rendering."""
    import seestar.run as run_module
    from seestar.io import load_config

    src = tmp_path / "src"
    src.mkdir()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    with pytest.raises(ValueError, match="starless.*not a supported output name"):
        run_module.run_target(str(src), str(out_dir), load_config(), out_name="starless")
    assert list(out_dir.iterdir()) == []  # nothing rendered, nothing written


def test_failed_documentation_run_produces_only_uncertified_annotated(tmp_path, monkeypatch):
    """Task 9: a documentation-profile run whose hero fails must annotate under the
    SAME uncertified identity as its hero -- never leak a certified-named annotated
    JPG for a run that never certified anything. Same fakes as
    test_run_ops.py::test_run_target_documentation_profile_annotation_finds_the_promoted_hero
    (which exercises the certified path for this profile), plus this file's
    _probe_metric processing-failed injection (see
    test_processing_failed_publishes_uncertified_never_final above)."""
    import seestar.execute as exec_module

    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")

    _apply_mocks(monkeypatch)
    # body_p90 probe never reaches the target (72) -> solver never converges -> processing_failed.
    monkeypatch.setattr(exec_module, "_probe_metric", lambda jpg, metric, mask_cfg=None, **kw: 10.0)

    result = orch.run_target(target_dir, out_dir, _cfg(), profile="documentation", out_name="documentation")

    assert result["status"] == "processing_failed"
    assert os.path.exists(os.path.join(out_dir, "X1_documentation_uncertified_annotated.jpg"))
    assert not os.path.exists(os.path.join(out_dir, "X1_documentation_annotated.jpg"))


# ---------------------------------------------------------------------------
# Diagnosis 7 (Task 6): the run's own surfaces. A tool failure that stops a
# render must arrive as a bounded, actionable reason that names the tool AND the
# stage -- not a 200-character slice of the tool's banner.
# ---------------------------------------------------------------------------


def test_tool_failure_reason_names_tool_and_stage_and_is_not_truncated(tmp_path, monkeypatch):
    """Red before Task 6: `_do` cut every failure reason to 200 characters, which
    on a chatty tool discarded the actionable line and the guidance. A typed
    failure is bounded by construction, so it now travels whole."""
    import pytest as _pytest

    from seestar.adapters import base as B
    from seestar.adapters._external_process import ExternalToolFailure
    from seestar.core.types import BackgroundExtract

    target_dir = _make_target_dir(tmp_path)
    _apply_mocks(monkeypatch)

    class _FailingBge:
        name = "fake-failing-bge"

        def execute(self, op, ctx):
            raise ExternalToolFailure(
                tool="graxpert",
                failure_class="no_output",
                summary="Error: could not download the denoise model (network unreachable)",
                exit_status=1,
                log_path="/out/X1/seestar_diagnostics.log",
                guidance="Open GraXpert once to finish its model setup, then try again.",
            )

    monkeypatch.setitem(B.REGISTRY, BackgroundExtract, _FailingBge())

    with _pytest.raises(orch.PipelineError) as exc_info:
        orch.run_target(target_dir, str(tmp_path / "out"), _cfg())

    reason = str(exc_info.value)
    assert "GraXpert" in reason
    assert "removing light-pollution gradient" in reason, "the executor must attach the STAGE"
    assert "could not download the denoise model" in reason, "the actionable line must survive"
    assert "seestar_diagnostics.log" in reason, "the bounded full log must stay discoverable"
    assert "try again" in reason.lower()
    assert len(reason) > 200, "a bounded, actionable message must not be re-truncated at 200 chars"


def test_unbounded_non_tool_failure_is_still_truncated(tmp_path, monkeypatch):
    """The 200-char bound still applies to everything untyped: it exists so an
    arbitrary exception cannot flood the notes sidecar."""
    import pytest as _pytest

    from seestar.adapters import base as B
    from seestar.core.types import BackgroundExtract

    target_dir = _make_target_dir(tmp_path)
    _apply_mocks(monkeypatch)

    class _Chatty:
        name = "fake-chatty"

        def execute(self, op, ctx):
            raise B.AdapterError("x" * 5000)

    monkeypatch.setitem(B.REGISTRY, BackgroundExtract, _Chatty())

    with _pytest.raises(orch.PipelineError) as exc_info:
        orch.run_target(target_dir, str(tmp_path / "out"), _cfg())

    assert len(str(exc_info.value)) < 400


# --- 2026-08-20: the exported photo's common name comes from the catalog match ---


def test_catalog_hit_stamps_its_common_name_into_every_exported_file(tmp_path, monkeypatch):
    """End-to-end: the offline catalog entry matched by WCS footprint is what
    names the deliverables. Before this, a six-entry table in metadata.py did --
    so an object nobody here had photographed got no name at all."""
    import seestar.known_targets as kt

    target_dir = _make_ircut_target_dir(tmp_path, name="X6")
    out_dir = str(tmp_path / "out")
    monkeypatch.setattr(kt, "load_catalog", lambda *a, **kw: (_local_galaxy_record(common_name="A Named Thing"),))

    _apply_mocks(monkeypatch)
    _force_pass(monkeypatch)
    seen: list = []
    monkeypatch.setattr(orch, "embed", lambda *a, **kw: seen.append(kw.get("common_name")))

    orch.run_target(target_dir, out_dir, _cfg(), catalog_prior=False)

    assert seen, "the run must embed metadata into at least one deliverable"
    assert set(seen) == {"A Named Thing"}, "every exported file carries the same catalog name"


def test_catalog_miss_embeds_no_common_name(tmp_path, monkeypatch):
    """Most of the sky is unnamed, and an unmatched field must read as "no name
    known" -- never a guess derived from the OBJECT header string."""
    import seestar.known_targets as kt

    target_dir = _make_ircut_target_dir(tmp_path, name="X7")
    out_dir = str(tmp_path / "out")
    monkeypatch.setattr(kt, "load_catalog", lambda *a, **kw: ())  # empty catalog -> miss

    _apply_mocks(monkeypatch)
    _force_pass(monkeypatch)
    seen: list = []
    monkeypatch.setattr(orch, "embed", lambda *a, **kw: seen.append(kw.get("common_name")))

    orch.run_target(target_dir, out_dir, _cfg(), catalog_prior=False)

    assert seen and set(seen) == {None}
