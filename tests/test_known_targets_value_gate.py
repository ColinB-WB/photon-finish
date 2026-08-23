"""Task 10: the HARD PRODUCT-VALUE GATE (spec sec7.11) on the REAL NGC5128 frame.

Read first: docs/TENETS.md -- esp. "The photons are the authority" (a catalog entry
may only ever PROPOSE identity/category, never inject structure or a processing
value) and "Uncertainty is an honest outcome" (a transient data/tool failure here
must leave the gate PENDING, never a faked PASS).
See docs/history/superpowers/specs/2026-07-25-known-targets-prototype-design.md sec7.11 for
the pre-registered baseline/treatment/reject recipe and the NGC7000 must-hold safety
paragraph, and .superpowers/sdd/task-10-brief.md for the exact go/no-go steps.

**This is a Decisions-level gate -- no Siril render.** compute_decisions (called from
core/planning.py's `_compute`, itself driven by `start_planning`/`advance`) is a pure
function of classification + measurements + engine/profile/overrides; it settles
category/pcc/ghs/star_forks/closeup identically whether the LINEAR/MEASURE/SOLVE
phases actually render pixels or not (Task 9's
`test_compute_decisions_is_blind_to_category_source_criterion_3_and_4` is the formal
proof). So driving the planner through the SOURCE phase only -- and capturing
`state.decisions` the moment SOURCE hands off to LINEAR -- captures the exact same
PCC/GHS/closeup decision a full render would ship, without needing a fake Siril/
GraXpert/StarNet2 tool chain at all.

**Harness fidelity:** the LookupLocalTarget dispatch below is a byte-for-byte mirror
of `orchestrate.py`'s `_do` branch for `LookupLocalTarget` (Task 5): `load_catalog()`
-> `prior.field_center(fits)` -> `prior._fov_long_arcmin(header)` ->
`known_targets.match_target(records, center, fov, None)` -> on a hit,
`classify_conflict(record, state.classification.category)` (spatially_impossible ->
treated as miss) -> `to_observation(record)`. See the docstring on
`_drive_source_phase` for the side-by-side reasoning.

**A field-level note on the brief's "ghs_allowed"/"closeup_enabled" language:**
`Decisions.ghs_allowed` is a *profile*-level gate (`ProfileOverlay.ghs`, True for
every showcase-profile render regardless of category) and `Decisions.closeup_enabled`
is a constant `True` placeholder overridable only by prefs/pins/caller (see
`core/policy.py::compute_decisions`) -- neither field is itself category-driven, so
neither literally flips "off" between the starfield baseline and the galaxy
treatment. The actual per-category GHS/closeup signal is `Decisions.ght` (None for a
plain starfield; a `GhsParams` with `closeup=True` for the IRCUT galaxy dust-lane
treatment) together with `category`. This was verified by direct execution against
the real NGC5128 measurements before writing these assertions (see
`.superpowers/sdd/task-10-report.md`) -- the tests below assert the real, executed
behavior (`ght`, plus the two profile-level flags staying True/True as supporting
evidence they are gates-not-deciders), not the brief's shorthand literally.
"""

from __future__ import annotations

import dataclasses
import os

import pytest

import seestar.known_targets as kt
import seestar.prior as prior_module
from seestar.core.planning import (
    NeedWork,
    PlanningRefused,
    PlanningRequest,
    PlanPhase,
    advance,
    start_planning,
)
from seestar.core.types import (
    Category,
    CategorySource,
    Classification,
    Decisions,
    FetchNetworkPrior,
    FilterKind,
    LookupLocalTarget,
    OpsDone,
    Overrides,
    PriorObservation,
    RunOps,
    WorkSucceeded,
    WorkUnavailable,
)
from seestar.inspect import inspect as inspect_file
from seestar.io import engine_defaults_from_cfg, load_config
from seestar.measure import measure_fits

# ---------------------------------------------------------------------------
# Real frame paths -- resolved from config.local.yaml (git-ignored, machine-
# specific), never hardcoded. Both are asserted present in the task brief;
# PREFLIGHT below still checks and skips the whole module with a clear reason
# if this machine doesn't have them mounted (e.g. CI, another owner's Mac).
# ---------------------------------------------------------------------------


def _resolve_target_dir(name: str) -> str | None:
    cfg = load_config()
    targets = cfg.get("validation", {}).get("targets_darwin", {})
    return targets.get(name)


def _find_stacked_fit(target_dir: str) -> str | None:
    """The one real `Stacked_*.fit` directly inside `target_dir` (never descends
    into `raw/`/`processed/` subfolders -- mirrors `run.py::_session_fits`'
    `os.listdir`, not `os.walk`)."""
    if not target_dir or not os.path.isdir(target_dir):
        return None
    for name in sorted(os.listdir(target_dir)):
        if name.lower().endswith((".fit", ".fits")):
            return os.path.join(target_dir, name)
    return None


_NGC5128_DIR = _resolve_target_dir("NGC5128")
_NGC7000_DIR = _resolve_target_dir("NGC7000")
_NGC5128_FITS = _find_stacked_fit(_NGC5128_DIR) if _NGC5128_DIR else None
_NGC7000_FITS = _find_stacked_fit(_NGC7000_DIR) if _NGC7000_DIR else None

# NOTE (Task 7 value-gate fix): there is deliberately NO module-level `pytestmark`
# here. A module-wide skipif silently skips every test in the file -- including the
# frame-INDEPENDENT crowded-neighborhood tests, the M1 single-hit control, and the
# architecture-delegation test -- on any machine without the two mounted reference
# frames (CI, another owner's Mac). That violates the explicit fail-not-skip
# requirement: those guarantees must actually RUN everywhere. Instead, gate ONLY the
# tests that consume a real FITS frame with a per-test `@pytest.mark.skipif(...)`
# keyed on the exact frame(s) that test drives through `_drive_source_phase`.
_skip_no_ngc5128 = pytest.mark.skipif(
    not _NGC5128_FITS,
    reason=(
        "real NGC5128 frame not found via config.local.yaml validation.targets_darwin "
        "-- this test requires this machine's mounted reference library (see "
        "task-10-report.md PREFLIGHT for the paths checked)"
    ),
)
_skip_no_ngc7000 = pytest.mark.skipif(
    not _NGC7000_FITS,
    reason=(
        "real NGC7000 frame not found via config.local.yaml validation.targets_darwin "
        "-- this test requires this machine's mounted reference library (see "
        "task-10-report.md PREFLIGHT for the paths checked)"
    ),
)


# ---------------------------------------------------------------------------
# The harness: measure the real FITS offline, build a PlanningRequest exactly
# like orchestrate.run_target does, and drive start_planning/advance through
# the SOURCE phase only, dispatching each WorkItem by hand.
# ---------------------------------------------------------------------------


def _build_request(fits_path: str, use_prior: bool, cache_dir: str) -> tuple[PlanningRequest, dict]:
    """Mirrors orchestrate.run_target's request assembly (orchestrate.py ~145-221):
    resolve tools/engine config, inspect the header, measure the raw FITS, and pack a
    PlanningRequest with empty prefs/pins/caller overrides and profile=showcase (the
    default). `cache_dir` isolates the prior cache per call (defense-in-depth; with
    use_prior=False no network round fires regardless -- see the assertions below)."""
    cfg = load_config()
    cfg["engine"]["e_gate"] = False  # already false in config.yaml; explicit here too
    cfg["prior"] = {**cfg.get("prior", {}), "cache_dir": cache_dir}
    engine_cfg = cfg["engine"]
    ccfg = engine_cfg.get("classify") or {}
    engine = engine_defaults_from_cfg(engine_cfg)

    insp = inspect_file(fits_path, classify_cfg=ccfg)
    header = insp["header"]
    px = header.get("pixscale_arcsec")
    raw_m = measure_fits(fits_path, pixscale_arcsec=px)
    frame_dims = (int(header.get("NAXIS1") or raw_m.dims[0]), int(header.get("NAXIS2") or raw_m.dims[1]))
    filter_kind = FilterKind.from_raw(insp["meta"].get("filter"))
    frames = insp["meta"].get("frames") or 0

    request = PlanningRequest(
        target_label=insp["meta"]["target"] or "TEST",
        out_name="final",
        session_count=1,
        latest_session_index=0,
        frames=frames,
        filter_kind=filter_kind,
        has_wcs=bool(header.get("has_wcs")),
        raw_measurements=raw_m,
        frame_dims=frame_dims,
        engine=engine,
        profile="showcase",
        prefs=Overrides(),
        pins=Overrides(),
        pinned_solved=frozenset(),
        caller=Overrides(),
        use_prior=use_prior,
        debug=False,
    )
    return request, header


@dataclasses.dataclass
class SourcePhaseResult:
    decisions: Decisions
    classification: Classification
    local_status: str | None  # "hit" | "miss" | "spatially_rejected" | "ambiguous" |
    #                           "local_catalog_unavailable" | "no_wcs" | None (no WCS -> never dispatched)
    local_record_id: str | None
    network_called: bool
    network_status: str | None  # "succeeded" | "unavailable" | None


def _drive_source_phase(
    fits_path: str,
    use_prior: bool,
    cache_dir: str,
    network_obs: PriorObservation | None = None,
) -> SourcePhaseResult:
    """Drives `start_planning`/`advance` through PlanPhase.SOURCE only, hand-
    dispatching each WorkItem. The catalog itself is NOT a parameter here -- the
    caller must arrange what `known_targets.load_catalog()` returns BEFORE calling
    this function (via `_install_catalog(monkeypatch, records)`, or by leaving the
    real `kt.load_catalog` unpatched to exercise the actual shipped bundled
    resource), exactly mirroring how `orchestrate.py`'s `_do` branch calls
    `_kt.load_catalog()` itself rather than receiving records as an argument:

    - `RunOps(PrepareSource)` -> `WorkSucceeded(id, OpsDone())` (no real render --
      PrepareSource pixels are never read by anything the SOURCE phase or
      `compute_decisions` inspects).
    - `LookupLocalTarget` -> the REAL shell lookup sequence, mirroring
      `orchestrate.py`'s `_do` branch verbatim: `known_targets.load_catalog()` ->
      `prior.field_center(fits_path)` -> `prior._fov_long_arcmin(header)` ->
      `known_targets.match_target(records, center, fov, None)` -> on a hit,
      `classify_conflict(record, state.classification.category)` (a
      "spatially_impossible" own-position defect degrades to a miss, exactly like
      orchestrate.py) -> `to_observation(record)`.
    - `FetchNetworkPrior` -> `WorkSucceeded(id, network_obs)` when the caller
      supplied one (the "known-good network-backed reference" scenario), else
      `WorkUnavailable` -- and this branch must simply never fire in the
      network-disabled evaluations (asserted via `network_called` below), since
      `needs_network` in `core/planning.py::_advance_source` requires
      `req.use_prior` True.

    Returns as soon as `advance()` hands off to `PlanPhase.LINEAR` (i.e. the SOURCE
    round is done) -- `state.decisions` at that instant already carries the final
    category/pcc/ght/star_forks (compute_decisions is deterministic and blind to
    *how* the category was reached; nothing in LINEAR/MEASURE/SOLVE revises those
    fields for a plain galaxy/starfield comparison -- see the module docstring)."""
    request, header = _build_request(fits_path, use_prior, cache_dir)

    local_status = None
    local_record_id = None
    network_called = False
    network_status = None

    outcome = start_planning(request)
    assert isinstance(outcome, NeedWork), f"start_planning refused: {outcome}"
    while isinstance(outcome, NeedWork) and outcome.state.phase is PlanPhase.SOURCE:
        state = outcome.state
        results = []
        for item in outcome.items:
            req = item.request
            if isinstance(req, RunOps):
                results.append(WorkSucceeded(item.id, OpsDone()))
            elif isinstance(req, LookupLocalTarget):
                records = kt.load_catalog()
                if not records:
                    local_status = "local_catalog_unavailable"
                    results.append(WorkUnavailable(item.id, "local_catalog_unavailable"))
                    continue
                center = prior_module.field_center(fits_path)
                if center is None:
                    local_status = "no_wcs"
                    results.append(WorkUnavailable(item.id, "miss"))
                    continue
                fov_arcmin = prior_module._fov_long_arcmin(header)
                match = kt.match_target(records, center, fov_arcmin, None)
                if match.status == "ambiguous":
                    local_status = "ambiguous"
                    results.append(WorkUnavailable(item.id, "ambiguous"))
                    continue
                if match.status in ("miss", "spatially_rejected"):
                    local_status = match.status
                    results.append(WorkUnavailable(item.id, "miss"))
                    continue
                record = match.record
                conflict = kt.classify_conflict(record, state.classification.category)
                if conflict == "spatially_impossible":
                    local_status = "spatially_impossible"
                    results.append(WorkUnavailable(item.id, "miss"))
                    continue
                local_status = "hit"
                local_record_id = record.canonical_id
                results.append(WorkSucceeded(item.id, kt.to_observation(record)))
            elif isinstance(req, FetchNetworkPrior):
                network_called = True
                if network_obs is None:
                    network_status = "unavailable"
                    results.append(WorkUnavailable(item.id, "unavailable"))
                else:
                    network_status = "succeeded"
                    results.append(WorkSucceeded(item.id, network_obs))
            else:
                raise AssertionError(f"unexpected work request in SOURCE phase: {type(req).__name__}")
        outcome = advance(state, tuple(results))
        if isinstance(outcome, PlanningRefused):
            raise AssertionError(f"planning refused during SOURCE drive: {outcome.reason}")

    final_state = outcome.state
    return SourcePhaseResult(
        decisions=final_state.decisions,
        classification=final_state.classification,
        local_status=local_status,
        local_record_id=local_record_id,
        network_called=network_called,
        network_status=network_status,
    )


def _install_catalog(monkeypatch, records: tuple) -> None:
    monkeypatch.setattr(kt, "load_catalog", lambda *a, **kw: records)


# ---------------------------------------------------------------------------
# The fields compute_decisions genuinely varies by category (verified by direct
# execution against the real NGC5128 measurements -- see the module docstring's
# note on ghs_allowed/closeup_enabled). Every one of these is a documented
# CLAUDE.md sec6a per-category table lookup (denoise/asinh_d/mtf_mid/saturation),
# the color-calibration law (pcc), the star-taming eligibility rule (star_forks),
# or the structure-treatment attachment itself (ght) -- never a footprint/
# morphology-driven side channel, since `to_observation()` carries only `category`.
# ---------------------------------------------------------------------------
_EXPECTED_GALAXY_TREATMENT_DIFF_FIELDS = frozenset(
    {"category", "denoise_strength", "pcc", "asinh_d", "mtf_mid", "saturation", "star_forks", "ght"}
)


def _decisions_diff_fields(a: Decisions, b: Decisions) -> frozenset[str]:
    return frozenset(f.name for f in dataclasses.fields(a) if getattr(a, f.name) != getattr(b, f.name))


# ---------------------------------------------------------------------------
# Evaluation 1: BASELINE (control)
# ---------------------------------------------------------------------------


@_skip_no_ngc5128
def test_baseline_ngc5128_catalog_absent_network_disabled_is_wrong_category(tmp_path, monkeypatch):
    """spec sec7.11 control run: NGC5128, EMPTY catalog, network disabled
    (use_prior=False), isolated empty prior cache -> falls all the way to the IRCUT
    filter-default STARFIELD guess (classify.py's FILTER_DEFAULT) -- the WRONG
    category for a galaxy: no PCC, no dust-lane GHS. This establishes the baseline
    the benefit must be falsifiable against."""
    _install_catalog(monkeypatch, ())
    result = _drive_source_phase(_NGC5128_FITS, use_prior=False, cache_dir=str(tmp_path / "cache"))

    assert result.decisions.category is Category.STARFIELD
    assert result.classification.source is CategorySource.FILTER_DEFAULT
    assert result.decisions.pcc is False
    assert result.decisions.ght is None  # no GHS structure treatment attaches
    # supporting evidence that ghs_allowed/closeup_enabled are profile gates, not
    # category deciders (see module docstring) -- both stay True even on the wrong
    # category; what actually differs is `ght`.
    assert result.decisions.ghs_allowed is True
    assert result.decisions.closeup_enabled is True

    assert result.local_status == "local_catalog_unavailable"
    assert not result.network_called, "network must be disabled in the control run"


# ---------------------------------------------------------------------------
# Evaluation 2: TREATMENT -- the core value claim
# ---------------------------------------------------------------------------


@_skip_no_ngc5128
def test_treatment_ngc5128_offline_catalog_reproduces_network_backed_galaxy_decision(tmp_path, monkeypatch):
    """spec sec7.11 treatment run: NGC5128, the REAL SHIPPED bundled catalog
    (`seestar/data/known_targets.json`, no monkeypatch -- this is literally what v1.0
    ships), network disabled -> WCS-confirmed galaxy, entirely offline. Its PCC/GHS/
    closeup Decisions must equal a known-good NETWORK-BACKED reference run (local
    catalog empty, use_prior=True, FetchNetworkPrior succeeding with
    PriorObservation(GALAXY)) -- proving the offline local catalog reproduces the
    network path's treatment -- and differ from the baseline in exactly the
    category-attributable fields (never footprint/morphology)."""
    # no monkeypatch here: exercise the REAL bundled catalog resource (what v1.0 ships).
    treatment = _drive_source_phase(_NGC5128_FITS, use_prior=False, cache_dir=str(tmp_path / "cache_treatment"))

    assert treatment.decisions.category is Category.GALAXY
    assert treatment.classification.source is CategorySource.CATALOG_PRIOR
    assert treatment.local_status == "hit"
    assert treatment.local_record_id == "NGC5128"
    assert not treatment.network_called, "the local hit must decide before any network round is even considered"

    # known-good network-backed reference: local catalog EMPTY, use_prior=True, the
    # IRCUT network round succeeds with PriorObservation(GALAXY) -- the pre-catalog
    # v1.0 path this feature is being measured against.
    monkeypatch.setattr(kt, "load_catalog", lambda *a, **kw: ())
    network_ref = _drive_source_phase(
        _NGC5128_FITS,
        use_prior=True,
        cache_dir=str(tmp_path / "cache_network_ref"),
        network_obs=PriorObservation(catalog_category=Category.GALAXY, fov_fraction=None),
    )
    assert network_ref.decisions.category is Category.GALAXY
    assert network_ref.classification.source is CategorySource.CATALOG_PRIOR
    assert network_ref.network_called, "the reference run must actually exercise the network round"

    # THE core value claim: offline local-catalog Decisions == network-backed Decisions.
    assert treatment.decisions == network_ref.decisions

    # and it differs from the baseline in exactly the galaxy-treatment fields.
    monkeypatch.setattr(kt, "load_catalog", lambda *a, **kw: ())
    baseline = _drive_source_phase(_NGC5128_FITS, use_prior=False, cache_dir=str(tmp_path / "cache_baseline2"))
    assert baseline.decisions.category is Category.STARFIELD  # sanity: still the wrong-category control

    diff = _decisions_diff_fields(baseline.decisions, treatment.decisions)
    assert diff == _EXPECTED_GALAXY_TREATMENT_DIFF_FIELDS, f"unexpected diff set: {sorted(diff)}"
    assert treatment.decisions.pcc is True
    assert baseline.decisions.pcc is False
    assert treatment.decisions.ght is not None
    assert treatment.decisions.ght.closeup is True  # galaxy closeup fork reachable
    assert baseline.decisions.ght is None


# ---------------------------------------------------------------------------
# Evaluation 3: REJECT -- a spatially-wrong entry must never cause a false success
# ---------------------------------------------------------------------------


@_skip_no_ngc5128
def test_reject_ngc5128_spatially_wrong_catalog_entry_falls_back_to_baseline(tmp_path, monkeypatch):
    """spec sec7.11 reject run: a catalog whose ONLY NGC5128-labeled entry sits at a
    valid-but-wildly-wrong position (far from the real frame's WCS footprint) must be
    rejected by `match_target`'s overlap geometry (status 'spatially_rejected', mapped
    to 'miss' by the shell -- mirroring orchestrate.py) and fall back to the SAME
    result as the catalog-absent baseline. No false success from a wrong entry."""
    real_records = kt.load_catalog()
    ngc5128 = next(r for r in real_records if r.canonical_id == "NGC5128")
    # valid celestial range (not "spatially_impossible" -- that's a different,
    # own-position-invalid defect) but nowhere near the real frame's WCS center.
    wrong_position = dataclasses.replace(ngc5128, ra_deg=30.0, dec_deg=60.0)

    _install_catalog(monkeypatch, (wrong_position,))
    reject = _drive_source_phase(_NGC5128_FITS, use_prior=False, cache_dir=str(tmp_path / "cache_reject"))
    assert reject.local_status == "spatially_rejected"
    assert reject.decisions.category is Category.STARFIELD
    assert reject.classification.source is CategorySource.FILTER_DEFAULT
    assert not reject.network_called

    monkeypatch.setattr(kt, "load_catalog", lambda *a, **kw: ())
    baseline = _drive_source_phase(_NGC5128_FITS, use_prior=False, cache_dir=str(tmp_path / "cache_baseline3"))

    assert reject.decisions == baseline.decisions
    assert reject.classification == baseline.classification


# ---------------------------------------------------------------------------
# Evaluation 4: NGC7000 must-hold safety -- footprint loaded but INERT
# ---------------------------------------------------------------------------


@_skip_no_ngc7000
def test_ngc7000_real_frame_catalog_hit_is_inert_category_and_decisions_unchanged(tmp_path, monkeypatch):
    """spec sec7.11's must-hold safety paragraph on a REAL frame: NGC7000 (LP,
    frame-filling nebula), the real shipped catalog present and genuinely
    WCS-matching (asserted, not assumed -- a vacuous 'miss' would make this test
    meaningless) -> category stays NEBULA (the unconditional LP filter law, per
    core/planning.py's `_reconcile`) and the FULL Decisions dataclass is field-equal
    to the catalog-ABSENT run: the footprint/morphology are loaded and would be
    reported, but wire into NOTHING in the decision path (sec5.4 inertness)."""
    real_records = kt.load_catalog()

    # confirm this is a genuine WCS hit on the real frame, not a vacuous absence.
    _, header = _build_request(_NGC7000_FITS, use_prior=False, cache_dir=str(tmp_path / "probe_cache"))
    center = prior_module.field_center(_NGC7000_FITS)
    fov = prior_module._fov_long_arcmin(header)
    match = kt.match_target(real_records, center, fov, None)
    assert match.status == "hit" and match.record.canonical_id == "NGC7000", (
        f"NGC7000 catalog-present precondition failed: match status={match.status!r} "
        f"(the inertness claim below would be vacuous without a genuine hit)"
    )

    _install_catalog(monkeypatch, real_records)
    catalog_on = _drive_source_phase(_NGC7000_FITS, use_prior=False, cache_dir=str(tmp_path / "cache_on"))
    assert catalog_on.local_status == "hit"
    assert catalog_on.local_record_id == "NGC7000"

    monkeypatch.setattr(kt, "load_catalog", lambda *a, **kw: ())
    catalog_off = _drive_source_phase(_NGC7000_FITS, use_prior=False, cache_dir=str(tmp_path / "cache_off"))
    assert catalog_off.local_status == "local_catalog_unavailable"

    assert catalog_on.decisions.category is Category.NEBULA
    assert catalog_off.decisions.category is Category.NEBULA
    assert catalog_on.classification.source is CategorySource.FILTER_DEFAULT  # filter law decided, not the catalog
    assert catalog_off.classification.source is CategorySource.FILTER_DEFAULT

    assert catalog_on.decisions == catalog_off.decisions, "a catalog hit that can't change category must change NOTHING"
    assert not catalog_on.network_called and not catalog_off.network_called  # LP never escalates to network


# ---------------------------------------------------------------------------
# Task 7 (Messier+Caldwell expansion): strict crowded-neighborhood abstention.
#
# match_target-level, no FITS needed. For each confirmed-ambiguous field (from the
# 6b-2 enumeration), center a synthetic S30 frame (FOV 130.0') at the group's mean
# RA/Dec against the REAL shipped 219-record catalog and assert `ambiguous`
# STRICTLY -- never "hit or ambiguous". A missing group id is a DEFECT (all M/C are
# selected), so members are resolved by canonical_id or equivalent_id and a missing
# member FAILS the test, never skips. These prove the expansion did not silently
# turn a genuinely-crowded field into a false single-object hit.
# ---------------------------------------------------------------------------

# Caldwell-only objects use their NGC canonical id (C11 -> NGC7635, per the
# canonical-id policy: a Caldwell-only object keeps its primary OpenNGC name).
_SAME_FAMILY = [("M31", "M32", "M110"), ("M81", "M82")]  # same-family galaxy groups
_MIXED = [("M20", "M21"), ("M42", "M43"), ("M97", "M108"), ("M52", "NGC7635")]  # mixed/conflicting


def _resolve_members(records: tuple, group: tuple[str, ...]) -> list:
    by_id = {r.canonical_id: r for r in records if r.lookup_role == "primary"}
    equiv = {e: r for r in records for e in (list(r.equivalent_ids) + [r.canonical_id])}
    return [by_id.get(c) or equiv.get(c) for c in group]


@pytest.mark.parametrize("group", _SAME_FAMILY + _MIXED)
def test_crowded_neighborhood_abstains(group):
    """A frame centered on a crowded neighborhood of >=2 primaries must ABSTAIN
    (`match_target(...).status == "ambiguous"`), never resolve to a single object."""
    records = kt.load_catalog()
    assert records, "real bundled catalog failed to load (value gate must not run empty)"
    members = _resolve_members(records, group)
    assert all(m is not None for m in members), f"{group}: missing catalog ids (defect, not skip)"
    ra = sum(m.ra_deg for m in members) / len(members)
    dec = sum(m.dec_deg for m in members) / len(members)
    res = kt.match_target(records, (ra, dec), 130.0, None)
    assert res.status == "ambiguous", f"{group}: expected abstain, got {res.status}"


# ---------------------------------------------------------------------------
# Task 7 (FIX-2b): committed centered-M1 single-hit control -- the non-vacuity
# counterpart to test_crowded_neighborhood_abstains above. Proves a genuinely
# NON-crowded field still resolves to a single "hit" under the exact same
# centered-synthetic-frame recipe (real 219-record catalog, S30 FOV 130.0'), so the
# crowded-neighborhood abstentions are demonstrating real crowding, not a matcher
# that has become universally over-cautious. match_target-level only, no FITS
# needed -- runs unconditionally on every machine.
# ---------------------------------------------------------------------------


def test_centered_m1_is_single_hit_non_vacuity_control():
    """M1 (Crab Nebula, NGC1952) sits in an isolated field -- it does not appear in
    any of the 36 confirmed-ambiguous centered fields (validation/
    known_targets_ambiguity_impact.md). A synthetic S30 frame centered on M1's own
    catalog RA/Dec against the real shipped catalog must resolve to a single "hit"
    on M1 itself, never "ambiguous" -- proving the crowded-neighborhood abstention
    tests are exercising real crowding, not a matcher that abstains everywhere."""
    records = kt.load_catalog()
    assert records, "real bundled catalog failed to load (value gate must not run empty)"
    by_id = {r.canonical_id: r for r in records if r.lookup_role == "primary"}
    equiv = {e: r for r in records for e in (list(r.equivalent_ids) + [r.canonical_id])}
    m1 = by_id.get("M1") or equiv.get("M1")
    assert m1 is not None, "M1 missing from catalog (defect, not skip)"

    res = kt.match_target(records, (m1.ra_deg, m1.dec_deg), 130.0, None)
    assert res.status == "hit", f"M1: expected single hit, got {res.status}"
    assert res.record is not None and res.record.canonical_id == "M1", (
        f"M1: expected hit resolving to canonical_id 'M1', got {res.record.canonical_id if res.record else None!r}"
    )


# ---------------------------------------------------------------------------
# Task 7: catalog-inert invariance at the Decisions level (CORRECTED from the plan
# draft, which wrongly used NGC5128 -- that frame IS in the catalog and correctly
# flips to GALAXY, so full-vs-empty legitimately DIFFERS there). Prove inertness
# where MEASUREMENT is already the authority: the NGC7000 (LP) frame -- the LP
# filter law drives category to NEBULA regardless, so a genuine catalog hit must
# not change ANY Decision. Uses the module's own SOURCE-phase harness, monkeypatch-
# ing `kt.load_catalog` -> () for the empty run at the exact seam the harness
# consults it (LookupLocalTarget dispatch calls `kt.load_catalog()`).
# ---------------------------------------------------------------------------


@_skip_no_ngc7000
def test_ngc7000_full_vs_empty_catalog_decisions_equal(tmp_path, monkeypatch):
    """Full REAL shipped catalog vs empty catalog: on the LP NGC7000 frame the
    Decisions must be byte-equal -- the catalog identity plays no role in the render
    decision because the LP filter law already fixes category to NEBULA."""
    # full run: the REAL bundled catalog, unpatched (literally what v1.0 ships).
    full = _drive_source_phase(_NGC7000_FITS, use_prior=False, cache_dir=str(tmp_path / "full"))
    assert full.local_status == "hit" and full.local_record_id == "NGC7000", (
        f"NGC7000 catalog-present precondition failed (status={full.local_status!r}); "
        "the inertness claim would be vacuous without a genuine catalog hit"
    )

    # empty run: monkeypatch load_catalog -> () at the harness's dispatch seam.
    monkeypatch.setattr(kt, "load_catalog", lambda *a, **k: ())
    empty = _drive_source_phase(_NGC7000_FITS, use_prior=False, cache_dir=str(tmp_path / "empty"))
    assert empty.local_status == "local_catalog_unavailable"

    assert full.decisions == empty.decisions, "catalog hit that can't change category must change NOTHING"
    assert full.decisions.category is Category.NEBULA


# ---------------------------------------------------------------------------
# Task 7: no target-name branch in runtime source. The catalog can only ever
# PROPOSE a category (never a name conditional in processing code); the guarantee
# is enforced by the architecture sweep. Delegate to it so the value gate itself
# fails if the expansion (or any code touched with it) ever introduced a
# target-name branch into `seestar/` processing source.
# ---------------------------------------------------------------------------


def test_no_target_name_branch_in_runtime_source_delegates_to_architecture_sweep():
    """Delegation: run the canonical architecture sweep that forbids target-name
    literals/conditionals in every `seestar/` processing + core module. Category is
    the only field that may cross from the catalog into decisions -- never a name."""
    from tests.test_architecture import (
        test_no_target_name_conditionals_in_processing_source as _sweep,
    )

    _sweep()  # raises AssertionError on any object-name literal or `target ==`/`in` branch
