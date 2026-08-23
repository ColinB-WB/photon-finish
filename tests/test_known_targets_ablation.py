"""Task 9: controlled-ablation harness + acceptance criteria (spec sec7) for the
known-targets local catalog prior.

Read first: docs/TENETS.md (tenet-critical feature -- *The photons are the authority*
governs: a catalog entry may only ever PROPOSE identity/category, never inject
structure; *Uncertainty is an honest outcome* governs the conflict-handling branches).
See docs/history/superpowers/specs/2026-07-25-known-targets-prototype-design.md sec5 (runtime
consumption) and sec7 (testing + the ten acceptance criteria this file pins).

**The ablation harness (two layers, deliberately):**

1. A PURE layer directly against `core.policy.compute_decisions` -- no shell, no
   fake tools, no I/O. `compute_decisions` takes `classification.category`,
   `.snr_class`, `.filter_kind` plus measurements/engine/profile/overrides; it never
   reads `.source` or `.extended_fraction`. This makes it possible to prove, with
   zero confounds (no solver, no fake-render noise), that a catalog-decided category
   (`CategorySource.CATALOG_PRIOR`) produces IDENTICAL `Decisions` to the SAME
   category reached by any other source -- the formal proof behind criteria 3/4
   ("only category, and only via the existing measured treatment").

2. An END-TO-END layer through `orchestrate.run_target`, reusing the exact fake-tool
   harness `tests/test_orchestrate.py` already established (`conftest.write_fake_stack`
   + `install_fake_op_adapters`/`install_fake_run_siril`, small REAL FITS/JPGs, no
   external tool binaries) -- `_run_capturing_decisions()` below is the "plan/run a
   target with a given injected catalog and return its Decisions (+ notes)" harness
   the task brief asks for. Catalogs are injected via `known_targets._read_catalog_text`
   (so the REAL `load_catalog()` parser exercises the fixture JSON -- proving the
   fixtures actually match the shipped schema), except where a scenario specifically
   wants to simulate a missing/corrupt bundled resource (`known_targets.load_catalog`
   mocked directly -- never reaches JSON parsing at all).

No live FITS/WCS solve or real network calls are used anywhere in this file --
`query_simbad` is boom-mocked in every scenario that must stay offline, matching
CLAUDE.md sec13's "operator runs measured controls" evidence discipline (deterministic,
hermetic tests) and the sec5.7 requirement that `--no-prior`/no-network scenarios never
silently fall through to a real network call.
"""

from __future__ import annotations

import json
import os

import seestar.known_targets as kt
import seestar.orchestrate as orch
import seestar.prior as prior_module
import seestar.qa as qa_module
from seestar.core.policy import compute_decisions
from seestar.core.types import (
    Category,
    CategorySource,
    ChannelStats,
    Classification,
    FilterKind,
    Measurements,
    SNRClass,
)
from seestar.io import engine_defaults_from_cfg, load_config
from seestar.orchestrate import _prior_outcome_note

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "known_targets")

ENG = engine_defaults_from_cfg({})  # in-code defaults, same pattern as test_core_policy.py


# ---------------------------------------------------------------------------
# fixture loading
# ---------------------------------------------------------------------------


def _fixture_text(name: str) -> str:
    with open(os.path.join(FIXTURES_DIR, name), encoding="utf-8") as f:
        return f.read()


def _fixture_records(name: str) -> tuple:
    """Parse a fixture through the REAL `known_targets._record_from_dict` (the exact
    function `load_catalog()` uses) -- confirms the fixture matches the shipped
    schema, not just that it's valid JSON."""
    payload = json.loads(_fixture_text(name))
    return tuple(kt._record_from_dict(r) for r in payload["records"])


def test_fixtures_parse_through_the_real_schema_and_have_expected_shape():
    """Sec7's four fixtures, sanity-checked against the real loader/parser before
    any scenario relies on them. Also a light determinism check (criterion 8's
    'reproducible' half; the full content-hash reproducibility of the GENERATED
    catalog is Task 7's job -- see test_build_known_targets.py::
    test_build_is_deterministic_content_hash /
    test_committed_catalog_matches_fresh_build_lockfile)."""
    full = _fixture_records("full_catalog.json")
    half = _fixture_records("half_catalog.json")
    empty = _fixture_records("empty_catalog.json")
    conflicting = _fixture_records("conflicting_catalog.json")

    assert {r.canonical_id for r in full} == {"M8", "NGC6530", "NGC7000", "NGC5128", "NGC5139"}
    assert {r.canonical_id for r in half} == {"M8", "NGC5128", "NGC5139"}
    assert half  # present
    assert {"NGC7000", "NGC6530"} - {r.canonical_id for r in half}  # deliberately absent
    assert empty == ()
    assert {r.canonical_id for r in conflicting} == {"BADCOORD1", "TYPECONFLICT1"}

    # deterministic: parsing the same fixture text twice yields equal records.
    assert _fixture_records("full_catalog.json") == full
    assert _fixture_records("conflicting_catalog.json") == conflicting

    # every fixture carries a real provenance block (schema fidelity).
    for name in ("full_catalog.json", "half_catalog.json", "empty_catalog.json", "conflicting_catalog.json"):
        payload = json.loads(_fixture_text(name))
        assert payload["schema_version"] == 1
        assert "source" in payload["provenance"]


# ---------------------------------------------------------------------------
# pure-core harness: compute_decisions is blind to category SOURCE
#
# This is the formal proof of criteria 3 + 4 -- no solver, no fake render, no
# confounds. compute_decisions(classification, measurements, engine, profile, ...)
# reads classification.category / .snr_class / .filter_kind and nothing else off
# Classification; it never reads .source or .extended_fraction. So the ONLY channel
# by which a catalog hit can reach Decisions is `category` -- proven by showing the
# SAME category, reached via a DIFFERENT source, produces a byte-for-byte-equal
# (dataclass field-equal) Decisions.
# ---------------------------------------------------------------------------


def _meas(pixscale=3.99, bgnoise=0.002, background=0.015):
    ch = ChannelStats(name="mono", median=background, sigma=bgnoise, max_value=0.9, sat_frac=0.0)
    return Measurements(
        channels=(ch,),
        background=background,
        bgnoise=bgnoise,
        max_value=0.9,
        dims=(1920, 1080),
        pixscale_arcsec=pixscale,
        dynamic_range=None,
    )


def test_compute_decisions_is_blind_to_category_source_criterion_3_and_4():
    """Criterion 3 ('a catalog entry changes only identity and category -- never any
    processing value') and criterion 4 ('any category change ... still flows through
    the existing measured treatment'): for a fixed category/snr/filter/measurements/
    overrides, Decisions must be identical whichever CategorySource produced that
    category. If the catalog secretly influenced anything beyond `category` (e.g. a
    footprint-driven side channel), this equality would break -- it can't, because
    compute_decisions's signature doesn't even expose `.source` to read."""
    m = _meas()
    base = dict(
        category=Category.GALAXY,
        snr_class=SNRClass.CLEAN,
        filter_kind=FilterKind.IRCUT,
        extended_fraction=None,
        extended_provisional=True,
    )
    catalog_cls = Classification(source=CategorySource.CATALOG_PRIOR, **base)
    d_catalog = compute_decisions(catalog_cls, m, ENG, "showcase")

    for other_source in (CategorySource.E_MEASURE, CategorySource.FILTER_DEFAULT):
        other_cls = Classification(source=other_source, **base)
        d_other = compute_decisions(other_cls, m, ENG, "showcase")
        assert d_catalog == d_other, f"Decisions differ for source={other_source}"

    # ...and for a DIFFERENT category, the SAME source-blindness must hold, so the
    # earlier equality isn't an accident of one particular category.
    base_starfield = {**base, "category": Category.STARFIELD}
    d_cat_sf = compute_decisions(
        Classification(source=CategorySource.CATALOG_PRIOR, **base_starfield), m, ENG, "showcase"
    )
    d_other_sf = compute_decisions(
        Classification(source=CategorySource.E_MEASURE, **base_starfield), m, ENG, "showcase"
    )
    assert d_cat_sf == d_other_sf

    # and it's not vacuous: STARFIELD and GALAXY decisions genuinely differ (pcc,
    # denoise, ght, saturation) -- category itself IS the live channel, just the
    # only one.
    assert d_catalog != d_cat_sf
    assert d_catalog.pcc is True and d_cat_sf.pcc is False
    assert d_catalog.ght is not None and d_cat_sf.ght is None


# ---------------------------------------------------------------------------
# end-to-end harness: orchestrate.run_target with a fake tool chain
# ---------------------------------------------------------------------------


class _FakeTools:
    siril = "siril-cli"
    graxpert = "GraXpert"
    starnet2 = "starnet2"
    exiftool = "exiftool"


def _cfg():
    cfg = load_config()
    cfg["engine"]["trail_removal"]["enabled"] = False
    cfg["engine"]["catalog_prior"] = False  # per-test opt-in via catalog_prior=True kwarg
    cfg["engine"]["e_gate"] = False  # controlled baseline: no E-gate probe (spec sec7.11 recipe)
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


def _boom_query_simbad(*a, **kw):
    raise AssertionError("query_simbad must not be called in this scenario")


def _run_capturing_decisions(monkeypatch, target_dir, out_dir, cfg=None, **kw):
    """The ablation harness proper: plans/runs `target_dir` -> `out_dir` through the
    real orchestrator with fake adapters, and returns `(decisions, result)` where
    `decisions` is the actual frozen `core.types.Decisions` instance the run computed
    (captured by spying on `orchestrate.decisions_to_report_dict`, the ONE place
    `run_target` converts it to the public report dict -- see orchestrate.py's
    `report_params = _with_report_extras(decisions_to_report_dict(decisions))`)."""
    _apply_mocks(monkeypatch)
    captured: dict = {}
    real = orch.decisions_to_report_dict

    def _spy(decisions):
        captured["decisions"] = decisions
        return real(decisions)

    monkeypatch.setattr(orch, "decisions_to_report_dict", _spy)
    result = orch.run_target(target_dir, out_dir, cfg if cfg is not None else _cfg(), **kw)
    return captured["decisions"], result


def _install_catalog(monkeypatch, fixture_name: str | None):
    """Inject a fixture catalog via `_read_catalog_text` -- the REAL `load_catalog()`
    parser runs on it (validating the fixture against the shipped schema on every
    use). `fixture_name=None` simulates a genuinely missing/corrupt bundled resource
    (never reaches JSON parsing) -- the pre-feature 'no catalog at all' baseline."""
    if fixture_name is None:
        monkeypatch.setattr(kt, "_read_catalog_text", lambda name: None)
        return
    text = _fixture_text(fixture_name)
    monkeypatch.setattr(kt, "_read_catalog_text", lambda name: text)


def _install_catalog_bypassing_parser(monkeypatch, records):
    """Inject records by mocking `load_catalog` directly (bypasses JSON parsing
    entirely) -- used only where a scenario needs to isolate the parser as a
    variable (criterion 2)."""
    monkeypatch.setattr(kt, "load_catalog", lambda *a, **kw: records)


def _add_wcs(fits_path, ra_deg, dec_deg):
    """Stamp a minimal real celestial WCS (same convention as test_orchestrate.py's
    `_add_wcs`) -- `inspect.py`'s `has_wcs` only checks CTYPE1/CRVAL1 PRESENCE, not
    validity, which is exploited by the spatially-impossible test below (a normal,
    valid WCS is stamped so has_wcs=True fires, then `prior.field_center` is
    monkeypatched to return the deliberately-invalid coordinate instead of relying
    on astropy's TAN projection to tolerate an out-of-range declination)."""
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


def _make_target_dir(tmp_path, name, ra=None, dec=None, filt="LP", subdir=None):
    """A target folder holding one small, real, Seestar-shaped FITS. `ra`/`dec`
    given -> WCS stamped (has_wcs=True); omitted -> no WCS at all (LookupLocalTarget
    is never even dispatched, per planning.py's has_wcs gate)."""
    from conftest import write_fake_stack

    target_dir = tmp_path / (subdir or f"{name}_import")
    target_dir.mkdir()
    fit_path = target_dir / f"Stacked_51_{name}_10.0s_{filt}_20260615-221349.fit"
    write_fake_stack(fit_path, obj=name, filt=filt)
    if ra is not None:
        _add_wcs(fit_path, ra, dec)
    return str(target_dir)


# --- criterion 1: a missing entry falls back cleanly (local miss -> IRCUT network
# -> measured) -----------------------------------------------------------------

# Frame position that matches NOTHING in half_catalog.json (M8/NGC5128/NGC5139 are
# all near RA~200-270; this sits far from any of them).
_MISS_RA, _MISS_DEC = 30.0, 60.0


def test_local_miss_falls_back_to_network_hit(tmp_path, monkeypatch):
    """Half of criterion 1's chain: local MISS (half_catalog has no record here) ->
    the IRCUT network round runs and its result decides the category."""
    target_dir = _make_target_dir(tmp_path, "C1A", ra=_MISS_RA, dec=_MISS_DEC, filt="IRCUT")
    out_dir = str(tmp_path / "out")

    _install_catalog(monkeypatch, "half_catalog.json")

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

    cfg = _cfg()
    cfg["engine"]["catalog_prior"] = True
    cfg["prior"] = {**cfg.get("prior", {}), "cache_dir": str(tmp_path / "prior_cache")}
    decisions, result = _run_capturing_decisions(monkeypatch, target_dir, out_dir, cfg=cfg, catalog_prior=True)

    assert calls, "local miss must fall through to the network round"
    assert decisions.category is Category.GALAXY
    assert result["notes"]["local_prior"]["outcome"] == "network"  # the network round is what ultimately spoke


def test_local_miss_and_network_unavailable_falls_back_to_measured(tmp_path, monkeypatch):
    """The other half of criterion 1's chain: local MISS, network attempted but
    finds no dominant object -> falls all the way to the measured/filter-default
    category (IRCUT with no E-gate probe -> starfield, classify.py's FILTER_DEFAULT).
    Never crashes, never silently stalls."""
    target_dir = _make_target_dir(tmp_path, "C1B", ra=_MISS_RA, dec=_MISS_DEC, filt="IRCUT")
    out_dir = str(tmp_path / "out")

    _install_catalog(monkeypatch, "half_catalog.json")

    calls = []

    def _fake_query_simbad_empty(ra, dec, **kw):
        calls.append((ra, dec))
        return []  # no candidates near center

    monkeypatch.setattr(prior_module, "query_simbad", _fake_query_simbad_empty)

    cfg = _cfg()
    cfg["engine"]["catalog_prior"] = True
    cfg["prior"] = {**cfg.get("prior", {}), "cache_dir": str(tmp_path / "prior_cache")}
    decisions, result = _run_capturing_decisions(monkeypatch, target_dir, out_dir, cfg=cfg, catalog_prior=True)

    assert calls, "the network round must still be attempted on a local miss"
    assert decisions.category is Category.STARFIELD  # IRCUT filter default, measured path
    assert result["params"]["category"] == "starfield"


# --- criterion 2: catalog-absent == today's Decisions (deterministic) ---------


def test_catalog_absent_equals_no_catalog_baseline(tmp_path, monkeypatch):
    """A structurally-valid-but-EMPTY catalog (parsed via the real JSON loader) must
    produce IDENTICAL Decisions and notes to a genuinely MISSING/corrupt bundled
    resource (never reaches JSON parsing) -- proving v1 shipping the catalog feature
    changes nothing about today's behavior when there's nothing to match. Both arms
    stay fully offline (query_simbad boom-mocked) since catalog_prior stays False."""
    target_dir = _make_target_dir(tmp_path, "C2", ra=_MISS_RA, dec=_MISS_DEC, filt="IRCUT")

    monkeypatch.setattr(prior_module, "query_simbad", _boom_query_simbad)
    _install_catalog(monkeypatch, "empty_catalog.json")
    dec_empty, result_empty = _run_capturing_decisions(monkeypatch, target_dir, str(tmp_path / "out_empty"))

    monkeypatch.setattr(prior_module, "query_simbad", _boom_query_simbad)
    _install_catalog(monkeypatch, None)  # simulate missing/corrupt bundled resource
    dec_missing, result_missing = _run_capturing_decisions(monkeypatch, target_dir, str(tmp_path / "out_missing"))

    assert dec_empty == dec_missing
    assert result_empty["notes"]["local_prior"] == result_missing["notes"]["local_prior"]
    assert result_empty["notes"]["local_prior"]["outcome"] == "local_catalog_unavailable"


# --- criteria 3 + 4 (end-to-end illustration) + spec sec7's NGC7000 must-hold
# safety case: a catalog hit that does NOT change category must change NOTHING ---


def test_ngc7000_style_lp_hit_leaves_every_decision_unchanged(tmp_path, monkeypatch):
    """spec sec7's must-hold safety case, generalized to the fixture: an LP frame
    whose WCS matches a catalog nebula record (full_catalog.json's NGC7000, footprint
    'frame_filling', morphology loaded) -- the filter law keeps category=nebula
    regardless, so criterion 3 predicts ZERO Decisions differ from the catalog-absent
    run. This is the strongest possible proof of 'never any processing value, and
    never via footprint/morphology': full dataclass equality, not a hand-picked
    field subset."""
    ngc7000 = next(r for r in _fixture_records("full_catalog.json") if r.canonical_id == "NGC7000")
    target_dir = _make_target_dir(tmp_path, "C3N7K", ra=ngc7000.ra_deg, dec=ngc7000.dec_deg, filt="LP")

    monkeypatch.setattr(prior_module, "query_simbad", _boom_query_simbad)  # LP must never touch network
    _install_catalog(monkeypatch, "full_catalog.json")
    dec_on, result_on = _run_capturing_decisions(monkeypatch, target_dir, str(tmp_path / "out_on"), catalog_prior=True)

    monkeypatch.setattr(prior_module, "query_simbad", _boom_query_simbad)
    _install_catalog(monkeypatch, "empty_catalog.json")
    dec_off, result_off = _run_capturing_decisions(
        monkeypatch, target_dir, str(tmp_path / "out_off"), catalog_prior=True
    )

    assert dec_on == dec_off, "a catalog hit that doesn't change category must change NOTHING else"
    assert dec_on.category is Category.NEBULA is dec_off.category

    lp_on = result_on["notes"]["local_prior"]
    assert lp_on["outcome"] == "local-hit"
    assert lp_on["local_disposition"] == "agreed"  # catalog's nebula assertion AGREES with final -- no contradiction
    assert "warning" not in lp_on
    assert lp_on["influenced"] == ["identity"]  # never "category" -- filter law owns it
    assert lp_on["canonical_id"] == "NGC7000"
    # footprint/morphology are loaded and REPORTED (report-only, sec5.4) ...
    assert lp_on["footprint_report_only"]["search_footprints"]
    assert lp_on["morphology_report_only"]
    # ... but report-only-ness is exactly why dec_on == dec_off holds despite them
    # being present in the note.

    lp_off = result_off["notes"]["local_prior"]
    assert lp_off["outcome"] == "local_catalog_unavailable"
    assert lp_off["influenced"] == []


def test_ircut_galaxy_value_case_category_flows_through_existing_treatment(tmp_path, monkeypatch):
    """Criteria 3+4, concrete end-to-end illustration (the pure test above is the
    formal proof): full_catalog.json's NGC5128 (galaxy) WCS-matched on an IRCUT
    frame settles category -> galaxy entirely offline, and that category then flows
    through the SAME PCC/GHS policy an IRCUT galaxy gets via any other path
    (CLAUDE.md sec6a: PCC on for IRCUT galaxy, off for starfield; GHS dust-lane
    treatment attaches only for galaxy) -- never a footprint/morphology-driven
    value. Catalog-absent control: e_gate is off (spec sec7.11's controlled-baseline
    recipe) so the miss falls to the IRCUT FILTER_DEFAULT starfield guess -- the
    wrong category for a galaxy, exactly the gap the catalog corrects."""
    ngc5128 = next(r for r in _fixture_records("full_catalog.json") if r.canonical_id == "NGC5128")
    target_dir = _make_target_dir(tmp_path, "C4GAL", ra=ngc5128.ra_deg, dec=ngc5128.dec_deg, filt="IRCUT")

    monkeypatch.setattr(prior_module, "query_simbad", _boom_query_simbad)
    _install_catalog(monkeypatch, "full_catalog.json")
    dec_on, result_on = _run_capturing_decisions(monkeypatch, target_dir, str(tmp_path / "out_on"), catalog_prior=True)

    monkeypatch.setattr(prior_module, "query_simbad", _boom_query_simbad)
    _install_catalog(monkeypatch, "empty_catalog.json")
    dec_off, result_off = _run_capturing_decisions(
        monkeypatch, target_dir, str(tmp_path / "out_off"), catalog_prior=True
    )

    assert dec_on.category is Category.GALAXY
    assert dec_off.category is Category.STARFIELD  # the wrong category the catalog corrects
    assert dec_on.pcc is True  # IRCUT galaxy -> PCC on (CLAUDE.md sec6a)
    assert dec_off.pcc is False  # IRCUT starfield -> PCC off
    assert dec_on.ght is not None  # galaxy dust-lane GHS attaches
    assert dec_off.ght is None  # starfield gets no galaxy treatment

    lp_on = result_on["notes"]["local_prior"]
    assert lp_on["outcome"] == "local-hit"
    assert lp_on["local_disposition"] == "agreed"  # catalog's galaxy assertion IS the final category -- adopted
    assert set(lp_on["influenced"]) == {"identity", "category"}
    assert lp_on["canonical_id"] == "NGC5128"


# --- criterion 5: spatial-reject vs type-disagreement, different consequences --


def test_spatially_impossible_entry_is_rejected_treated_as_miss(tmp_path, monkeypatch):
    """conflicting_catalog.json's badcoord1 has dec_deg=95.5 (outside +/-90deg) --
    its OWN stored position is invalid regardless of the frame. classify_conflict()
    must reject it entirely; the shell treats this exactly like a local miss (falls
    to measured, since catalog_prior stays False here). A normal, VALID WCS is
    stamped (has_wcs=True fires on CTYPE1/CRVAL1 presence alone, not validity) and
    `prior.field_center` is monkeypatched to hand LookupLocalTarget the deliberately
    invalid coordinate directly -- isolating the geometry/conflict logic from
    whatever astropy's TAN projection would do with an out-of-range CRVAL2."""
    target_dir = _make_target_dir(tmp_path, "C5BAD", ra=270.0, dec=-24.0, filt="IRCUT")

    monkeypatch.setattr(prior_module, "field_center", lambda fits_path: (270.0, 95.5))
    monkeypatch.setattr(prior_module, "_fov_long_arcmin", lambda header: 10.0)
    monkeypatch.setattr(prior_module, "query_simbad", _boom_query_simbad)
    _install_catalog(monkeypatch, "conflicting_catalog.json")

    decisions, result = _run_capturing_decisions(monkeypatch, target_dir, str(tmp_path / "out"))

    assert decisions.category is Category.STARFIELD  # IRCUT filter default -- rejected, fell to measured
    lp = result["notes"]["local_prior"]
    assert lp["outcome"] == "missing"
    assert lp["influenced"] == []
    assert "canonical_id" not in lp  # no identity was ever accepted


def test_type_disagreeing_entry_keeps_identity_drops_hint_uses_generic_path(tmp_path, monkeypatch):
    """conflicting_catalog.json's typeconflict1 sits at a PLAUSIBLE position (M8's
    RA/Dec -- a real WCS match) but is classified 'galaxy' where this LP frame's
    filter law is authoritative for nebula. classify_conflict() must return
    type_disagreement: identity is kept (recorded), the disputed category hint is
    dropped (never applied -- category stays nebula), and the generic/measured path
    is used.

    This is reachable case 1 from the owner's `contradicted` design (evaluated AFTER
    the pipeline settles its FINAL classification, not at the pre-measurement local
    lookup): the catalog's asserted GALAXY disagrees with the final NEBULA (LP filter
    law, unconditional), so `local_disposition` reads "contradicted", a warning is
    emitted, and (since LP never escalates to a network round) the outcome tag itself
    is now "contradicted" -- REACHABLE through a real end-to-end planner run, unlike
    the old pre-measurement flag this replaces. The decisive proof this is reporting
    ONLY, never a decision change: `decisions` is FIELD-EQUAL to the catalog-absent
    baseline run on the identical frame."""
    typeconflict = next(r for r in _fixture_records("conflicting_catalog.json") if r.canonical_id == "TYPECONFLICT1")
    target_dir = _make_target_dir(tmp_path, "C5TYPE", ra=typeconflict.ra_deg, dec=typeconflict.dec_deg, filt="LP")

    monkeypatch.setattr(prior_module, "query_simbad", _boom_query_simbad)  # LP never touches network
    _install_catalog(monkeypatch, "conflicting_catalog.json")

    decisions, result = _run_capturing_decisions(monkeypatch, target_dir, str(tmp_path / "out"), catalog_prior=True)

    assert decisions.category is Category.NEBULA  # the disputed "galaxy" hint was NEVER applied
    lp = result["notes"]["local_prior"]
    assert lp["conflict"] == "type_disagreement"
    assert lp["canonical_id"] == "TYPECONFLICT1"  # identity IS kept/recorded
    assert "category" not in lp["influenced"]  # ... but never influenced a decision
    assert "identity" in lp["influenced"]
    assert lp["outcome"] == "contradicted"  # reachable now (post-classification check)
    assert lp["local_disposition"] == "contradicted"
    assert "warning" in lp and typeconflict.entry_id in lp["warning"]

    # Decisions-equality: the SAME frame with the catalog absent must produce
    # IDENTICAL Decisions -- the contradicted local hit changed NOTHING but the
    # report (no decision ever adopted the rejected "galaxy" assertion).
    monkeypatch.setattr(prior_module, "query_simbad", _boom_query_simbad)
    _install_catalog(monkeypatch, "empty_catalog.json")
    decisions_absent, result_absent = _run_capturing_decisions(
        monkeypatch, target_dir, str(tmp_path / "out_absent"), catalog_prior=True
    )
    assert decisions == decisions_absent
    assert result_absent["notes"]["local_prior"]["outcome"] == "local_catalog_unavailable"


def test_type_disagreement_note_warns_when_measured_category_is_authoritative():
    """Direct unit coverage of `_prior_outcome_note`'s post-classification
    contradiction check (mirroring test_orchestrate.py's
    `test_contradicted_local_hit_produces_warn_note`), using this file's own
    conflicting_catalog.json record rather than duplicating the ad hoc record
    test_orchestrate.py builds by hand. This is the OLD unreachable-flag mechanism's
    replacement: the comparison is now `record.category` (the catalog's assertion)
    vs. `state.classification.category` (the FINAL classification), not a stashed
    `contradicted` flag computed at LookupLocalTarget-dispatch time. The full
    end-to-end proof that this is reachable through a real planner run lives in
    `test_type_disagreeing_entry_keeps_identity_drops_hint_uses_generic_path` above."""
    typeconflict = next(r for r in _fixture_records("conflicting_catalog.json") if r.canonical_id == "TYPECONFLICT1")
    lp = {"status": "hit", "record": typeconflict, "conflict": "type_disagreement"}

    class _FakeState:
        # The FINAL classification: NEBULA, disagreeing with typeconflict1's
        # asserted GALAXY.
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
    assert "warning" in note and typeconflict.entry_id in note["warning"]
    assert "category" not in note["influenced"]
    assert "identity" in note["influenced"]


def _nebula_record_at(entry_id: str, ra_deg: float, dec_deg: float):
    """A minimal local-catalog hit asserting NEBULA at a given position, built
    directly (bypassing JSON parsing) so this scenario doesn't need its own fixture
    file -- mirrors test_orchestrate.py's `_local_galaxy_record` helper."""
    from seestar.known_targets import LocalTargetRecord

    return LocalTargetRecord(
        entry_id=entry_id,
        canonical_id=entry_id,
        common_name=None,
        aliases=(),
        equivalent_ids=(),
        category=Category.NEBULA,
        ra_deg=ra_deg,
        dec_deg=dec_deg,
        extent_major_arcmin=20.0,
        extent_minor_arcmin=10.0,
        search_footprints=(),
        morphology_traits=(),
        associated=(),
        provenance={},
        lookup_role="primary",
    )


def test_local_hit_contradicted_but_network_speaks_keeps_outcome_network(tmp_path, monkeypatch):
    """The network-fallback contradiction case (owner's reachable case 2): an IRCUT
    frame's local catalog HIT asserts NEBULA. NEBULA is not one of the categories
    `core/planning.py`'s `_reconcile` lets an IRCUT catalog hit decide (only
    GALAXY/STARFIELD do -- an IRCUT-catalog-nebula disagreement is the documented
    "keep measured, flag for review" branch), so the local round does NOT decide
    (`used=False`), `decided_locally` stays False, and the second-round network
    lookup still fires (`needs_network` only requires the local round didn't decide,
    not that it missed outright -- core/planning.py's `_advance_source`). The network
    stub then returns GALAXY, which becomes the FINAL category via `_reconcile`'s
    catalog-wins branch.

    `outcome` must stay "network" (the network round is what ultimately spoke), but
    the rejected local NEBULA assertion must stay visible: `local_disposition ==
    "contradicted"` + a warning naming both categories -- so a locally-asserted,
    ultimately-overridden identity is never silently dropped (Tenet 7)."""
    ra, dec = 100.0, 20.0
    record = _nebula_record_at("NEBHIT1", ra, dec)
    target_dir = _make_target_dir(tmp_path, "C7NET", ra=ra, dec=dec, filt="IRCUT")

    _install_catalog_bypassing_parser(monkeypatch, (record,))

    def _fake_query_simbad(ra_, dec_, **kw):
        return [
            {
                "main_id": "NGC0002",
                "otype": "G",
                "otype_txt": "Galaxy",
                "ra": ra_,
                "dec": dec_,
                "galdim_majaxis": 20.0,
                "galdim_minaxis": 10.0,
                "dist": 0.0,
            }
        ]

    monkeypatch.setattr(prior_module, "query_simbad", _fake_query_simbad)

    cfg = _cfg()
    cfg["engine"]["catalog_prior"] = True
    cfg["prior"] = {**cfg.get("prior", {}), "cache_dir": str(tmp_path / "prior_cache")}
    decisions, result = _run_capturing_decisions(
        monkeypatch, target_dir, str(tmp_path / "out"), cfg=cfg, catalog_prior=True
    )

    assert decisions.category is Category.GALAXY  # the network round is what ultimately spoke
    lp = result["notes"]["local_prior"]
    assert lp["outcome"] == "network"
    assert lp["local_disposition"] == "contradicted"
    assert "warning" in lp and "NEBHIT1" in lp["warning"]
    assert lp["canonical_id"] == "NEBHIT1"  # identity is still preserved/reported
    assert "identity" in lp["influenced"]


# --- criterion 6: notes carry the influence tag --------------------------------


def test_notes_influence_tag_matches_what_actually_moved(tmp_path, monkeypatch):
    """Direct, compact pin of criterion 6 across the three shapes the influence tag
    can take: identity+category (IRCUT catalog decides), identity-only (LP hit that
    can never move category), and neither (no catalog present at all) -- reusing the
    three prior scenarios' recorded notes rather than re-running them."""
    ngc5128 = next(r for r in _fixture_records("full_catalog.json") if r.canonical_id == "NGC5128")
    ircut_dir = _make_target_dir(tmp_path, "C6I", ra=ngc5128.ra_deg, dec=ngc5128.dec_deg, filt="IRCUT")
    monkeypatch.setattr(prior_module, "query_simbad", _boom_query_simbad)
    _install_catalog(monkeypatch, "full_catalog.json")
    _, result_ircut = _run_capturing_decisions(monkeypatch, ircut_dir, str(tmp_path / "out_ircut"), catalog_prior=True)
    assert set(result_ircut["notes"]["local_prior"]["influenced"]) == {"identity", "category"}

    ngc7000 = next(r for r in _fixture_records("full_catalog.json") if r.canonical_id == "NGC7000")
    lp_dir = _make_target_dir(tmp_path, "C6L", ra=ngc7000.ra_deg, dec=ngc7000.dec_deg, filt="LP")
    monkeypatch.setattr(prior_module, "query_simbad", _boom_query_simbad)
    _install_catalog(monkeypatch, "full_catalog.json")
    _, result_lp = _run_capturing_decisions(monkeypatch, lp_dir, str(tmp_path / "out_lp"), catalog_prior=True)
    assert result_lp["notes"]["local_prior"]["influenced"] == ["identity"]

    none_dir = _make_target_dir(tmp_path, "C6N", ra=_MISS_RA, dec=_MISS_DEC, filt="LP")
    monkeypatch.setattr(prior_module, "query_simbad", _boom_query_simbad)
    _install_catalog(monkeypatch, "empty_catalog.json")
    _, result_none = _run_capturing_decisions(monkeypatch, none_dir, str(tmp_path / "out_none"), catalog_prior=True)
    assert result_none["notes"]["local_prior"]["influenced"] == []
