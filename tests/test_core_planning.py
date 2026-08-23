"""Planning state machine (Task 18). Drives start_planning/advance with SYNTHETIC
observations only -- no tools, no files. Covers the three archetype happy paths, the
spec §6 failure table row by row, recipe-pin behavior, and advance totality."""

import pytest

from seestar.core.planning import (
    NeedWork,
    PlanComplete,
    PlanningRefused,
    PlanningRequest,
    _reconcile,
    advance,
    start_planning,
)
from seestar.core.types import (
    ArtifactRef,
    ArtifactRole,
    BackgroundExtract,
    BorderObservation,
    Category,
    CategorySource,
    ChannelStats,
    ColorCalibrate,
    CombineSessions,
    ConcentrationObservation,
    Crop,
    CropTo,
    EValueObservation,
    Export,
    FetchNetworkPrior,
    FilterKind,
    InputStarsObservation,
    LinearStatsObservation,
    LookupLocalTarget,
    MeasureKind,
    Measurements,
    OpsDone,
    Overrides,
    PccEvidence,
    PrepareSource,
    PriorObservation,
    ProbeMetricObservation,
    RemoveTrail,
    RunOps,
    ScreenBlend,
    SeparateStars,
    SolveRegionObservation,
    StageKey,
    StretchRender,
    SubjectBoxObservation,
    SubjectSupportObservation,
    TrailObservation,
    WorkFailed,
    WorkSucceeded,
    WorkUnavailable,
)
from seestar.io import engine_defaults_from_cfg

ENG = engine_defaults_from_cfg({})


def _meas(background=0.015, bgnoise=0.002, pixscale=3.99):
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


def _req(**kw):
    d = dict(
        target_label="M8",
        out_name="final",
        session_count=1,
        latest_session_index=0,
        frames=51,
        filter_kind=FilterKind.LP,
        has_wcs=False,
        raw_measurements=_meas(),
        frame_dims=(1920, 1080),
        engine=ENG,
        profile="showcase",
        prefs=Overrides(),
        pins=Overrides(),
        pinned_solved=frozenset(),
        caller=Overrides(),
        use_prior=False,
        debug=False,
    )
    d.update(kw)
    return PlanningRequest(**d)


# ---------------------------------------------------------------------------
# synthetic responder
# ---------------------------------------------------------------------------


class Responder:
    """Default happy responder. `overrides` maps a (kind-or-tag) -> callable(item)->WorkResult
    to inject failures/alternates for one specific work kind."""

    def __init__(
        self, *, prior=None, local=None, trail=False, e_value=None, concentration=60.0, box=(10, 10, 100, 100)
    ):
        self.prior = prior  # network FetchNetworkPrior response: PriorObservation | "unavailable" | None
        self.local = local  # local LookupLocalTarget response: PriorObservation | "unavailable" | None
        self.trail = trail
        self.e_value = e_value
        self.concentration = concentration
        self.box = box
        self.hooks = {}  # tag -> callable(item) -> WorkResult
        self.calls = []  # tags of every item this responder actually answered, in order
        self._probe_bright = None

    def hook(self, tag, fn):
        self.hooks[tag] = fn
        return self

    def _tag(self, item):
        r = item.request
        if isinstance(r, RunOps):
            return type(r.ops[0]).__name__  # "PrepareSource", "Crop", "StretchRender", ...
        if isinstance(r, FetchNetworkPrior):
            return "FetchNetworkPrior"
        if isinstance(r, LookupLocalTarget):
            return "LookupLocalTarget"
        return r.kind  # MeasureKind

    def __call__(self, item):
        tag = self._tag(item)
        self.calls.append(tag)
        if tag in self.hooks:
            return self.hooks[tag](item)
        r = item.request
        if isinstance(r, RunOps):
            op = r.ops[0]
            if isinstance(op, StretchRender) and op.output.name.startswith("probe_"):
                self._probe_bright = self._brightness(op.mtf_mid)
            if isinstance(op, ColorCalibrate) and op.pcc:
                # a real pcc run reports evidence (Diagnosis 4); default = healthy
                # M81-style solution so the calibrated path stays the happy path
                return WorkSucceeded(
                    item.id,
                    OpsDone(
                        pcc_evidence=PccEvidence(
                            succeeded=True,
                            imprecise=False,
                            few_stars_warning=False,
                            stars_used=330,
                            coefficients=(0.471, 0.713, 1.0),
                            detail="using 330 stars",
                        )
                    ),
                )
            return WorkSucceeded(item.id, OpsDone())
        if isinstance(r, LookupLocalTarget):
            if self.local in (None, "unavailable"):
                return WorkUnavailable(item.id, "no-local-catalog")
            return WorkSucceeded(item.id, self.local)
        if isinstance(r, FetchNetworkPrior):
            if self.prior in (None, "unavailable"):
                return WorkUnavailable(item.id, "no-network")
            return WorkSucceeded(item.id, self.prior)
        # Measure
        if r.kind is MeasureKind.BORDER_SCAN:
            return WorkSucceeded(
                item.id, BorderObservation(left=0, top=0, right=0, bottom=0, confident=True, detail="clean")
            )
        if r.kind is MeasureKind.SUBJECT_SUPPORT:
            return WorkSucceeded(
                item.id,
                SubjectSupportObservation(
                    area_frac=0.12, dims=(1920, 1080), source="starless_sigma", valid=True, detail="med+3.0sigma"
                ),
            )
        if r.kind is MeasureKind.TRAIL_SCAN:
            return WorkSucceeded(item.id, TrailObservation(present=self.trail))
        if r.kind is MeasureKind.EXTENDED_SIGNAL:
            return WorkSucceeded(item.id, EValueObservation(e_value=self.e_value or 0.05))
        if r.kind is MeasureKind.LINEAR_STATS:
            return WorkSucceeded(item.id, LinearStatsObservation(background=0.015, bgnoise=0.002))
        if r.kind is MeasureKind.INPUT_STARS:
            return WorkSucceeded(item.id, InputStarsObservation(star_count=900, fwhm_px=2.4))
        if r.kind is MeasureKind.CONCENTRATION:
            return WorkSucceeded(item.id, ConcentrationObservation(concentration=self.concentration))
        if r.kind is MeasureKind.RENDER_METRIC:
            val = self._probe_bright if self._probe_bright is not None else 72.0
            # a probe asked to FREEZE the solve region reports what it froze
            region = (
                SolveRegionObservation(
                    area_frac=0.08,
                    dims=(1920, 1080),
                    source="frozen_object_mask",
                    valid=True,
                    detail="object mask of the first real-chain body probe",
                )
                if r.output is not None
                else None
            )
            return WorkSucceeded(item.id, ProbeMetricObservation(value=val, sky=val * 0.05, region=region))
        if r.kind is MeasureKind.SUBJECT_BOX:
            return WorkSucceeded(item.id, SubjectBoxObservation(box=self.box))
        raise AssertionError(f"unhandled {tag}")

    @staticmethod
    def _brightness(mtf):
        lo, hi = 0.02, 0.25
        t = (mtf - lo) / (hi - lo)
        return 300.0 - t * 299.0  # monotonic decreasing; wide range -> any target reachable


def drive(request, respond):
    outcome = start_planning(request)
    for _ in range(200):
        if not isinstance(outcome, NeedWork):
            return outcome
        results = tuple(respond(i) for i in outcome.items)
        outcome = advance(outcome.state, results)
    raise AssertionError("planning did not terminate")


def _export_bases(plan):
    return [s.output_base for s in plan.steps if isinstance(s, Export)]


# ---------------------------------------------------------------------------
# archetype happy paths
# ---------------------------------------------------------------------------


def test_lp_nebula_showcase_three_looks():
    out = drive(_req(), Responder())
    assert isinstance(out, PlanComplete)
    plan = out.plan
    # the COMPLETE ordered step sequence: (type, primary output ref name).
    # No prior fetch (use_prior False), no trail op (scan said absent), no closeup.
    # Phase 3A conservative star policy: the _final hero is the stars-on look
    # (input `stretched`); the star-reduced render ships as its own variant.
    seq = [(type(s).__name__, s.outputs[0].name if s.outputs else None) for s in plan.steps]
    assert seq == [
        ("PrepareSource", "working_copy"),
        ("Crop", "cropped"),
        ("BackgroundExtract", "bge"),
        ("Denoise", "denoised"),
        ("ColorCalibrate", "linear_master"),
        ("StretchRender", "stretched"),
        ("SeparateStars", "starless"),
        ("ScreenBlend", "reduced"),
        ("Export", "M8_star-reduced.tif"),
        ("Export", "M8_starless.tif"),
        ("Export", "M8_final.tif"),
    ]
    # and the input wiring of the star-treatment tail
    sep = next(s for s in plan.steps if isinstance(s, SeparateStars))
    assert sep.input.name == "stretched" and sep.stars.name == "stars"
    blend = next(s for s in plan.steps if isinstance(s, ScreenBlend))
    assert blend.starless.name == "starless" and blend.stars.name == "stars"
    # the conservative hero exports the stars-on `stretched`, not the reduced blend
    hero = next(s for s in plan.steps if isinstance(s, Export) and s.output_base == "M8_final")
    assert hero.input.name == "stretched"
    assert set(_export_bases(plan)) == {"M8_star-reduced", "M8_starless", "M8_final"}
    names = {r.name for r in plan.deliverables}
    for base in ("M8_star-reduced", "M8_starless", "M8_final"):
        assert {f"{base}.tif", f"{base}.png", f"{base}.jpg"} <= names
    assert plan.solve is not None and plan.solve.converged
    assert out.state.decisions.category is Category.NEBULA


def test_ircut_galaxy_prior_closeup():
    # local defaults to a miss here, so this exercises the full round-1-local ->
    # round-2-network escalation (Task 4), landing on the same GALAXY decision a
    # round-1-only prior hit produced before this task.
    req = _req(target_label="NGC5128", filter_kind=FilterKind.IRCUT, has_wcs=True, use_prior=True)
    resp = Responder(prior=PriorObservation(catalog_category=Category.GALAXY, fov_fraction=0.1))
    out = drive(req, resp)
    assert isinstance(out, PlanComplete)
    assert "LookupLocalTarget" in resp.calls and "FetchNetworkPrior" in resp.calls
    dec = out.state.decisions
    assert dec.category is Category.GALAXY and dec.pcc is True and dec.ght is not None
    # closeup produced a CropTo + a closeup export
    assert any(isinstance(s, CropTo) for s in out.plan.steps)
    assert "NGC5128_closeup" in _export_bases(out.plan)
    # no E-probe was run (prior decided); no ExtendedSignal measure anywhere
    assert dec.pcc is True


def test_ircut_starfield_cluster_single_hero():
    req = _req(target_label="M5", filter_kind=FilterKind.IRCUT, has_wcs=False, use_prior=False)
    out = drive(req, Responder(concentration=60.0))  # >= concentration_min (50)
    assert isinstance(out, PlanComplete)
    dec = out.state.decisions
    assert dec.category is Category.STARFIELD and dec.ght is not None  # cluster GHS attached
    assert _export_bases(out.plan) == ["M5_final"]  # single stars-on hero
    # QA starless emitted (SeparateStars) but not exported; the stars layer is
    # PRODUCTION scratch (a byproduct -- cleaned, never a deliverable-dir leak)
    seps = [s for s in out.plan.steps if isinstance(s, SeparateStars)]
    assert len(seps) == 1 and seps[0].starless.name == "M5_starless"
    assert seps[0].starless.role is ArtifactRole.QA
    assert seps[0].stars == ArtifactRef("cluster_stars", ArtifactRole.PRODUCTION)
    assert out.state.concentration == 60.0


def test_sparse_starfield_no_cluster_no_separate():
    req = _req(target_label="M5", filter_kind=FilterKind.IRCUT, use_prior=False)
    out = drive(req, Responder(concentration=10.0))  # below min -> not a cluster
    assert isinstance(out, PlanComplete)
    assert out.state.decisions.ght is None
    assert not any(isinstance(s, SeparateStars) for s in out.plan.steps)
    assert _export_bases(out.plan) == ["M5_final"]


# ---------------------------------------------------------------------------
# LINEAR extras: trail, e-gate
# ---------------------------------------------------------------------------


def test_trail_present_inserts_removetrail():
    out = drive(_req(), Responder(trail=True))
    assert isinstance(out, PlanComplete)
    assert any(isinstance(s, RemoveTrail) for s in out.plan.steps)
    # BGE consumes the detrailed ref
    bge = next(s for s in out.plan.steps if isinstance(s, BackgroundExtract))
    assert bge.input.name == "detrailed"


def test_egate_reclassifies_to_galaxy():
    eng = engine_defaults_from_cfg({"e_gate": True})
    req = _req(target_label="G", filter_kind=FilterKind.IRCUT, engine=eng, use_prior=False)
    out = drive(req, Responder(e_value=0.20))
    assert isinstance(out, PlanComplete)
    assert out.state.decisions.category is Category.GALAXY
    assert any(n.key == "e_gate" for n in out.state.notes)


# ---------------------------------------------------------------------------
# _reconcile (core-ported prior.reconcile, coverage-matrix M10 "disagree" cases):
# the LP-filter-law and IRCUT-catalog-says-nebula branches both keep the MEASURED
# category over the catalog's -- i.e. the two ways prior and measurement can
# genuinely disagree. test_ircut_galaxy_prior_closeup / test_prior_unavailable_degrades
# already cover "used" and "unavailable-degrade"; this closes the third state.
# ---------------------------------------------------------------------------


def test_reconcile_lp_filter_law_overrides_catalog_disagreement():
    # M8: catalog (mis-resolved to an embedded open cluster) says starfield, but the
    # LP dual-band filter is an unconditional instrument law -> stays nebula, and the
    # disagreement is recorded in the note detail (never silently dropped).
    cat, used, detail = _reconcile(FilterKind.LP, Category.NEBULA, Category.STARFIELD)
    assert cat is Category.NEBULA and used is False
    assert "filter_law_lp" in detail and "catalog=starfield" in detail


def test_reconcile_ircut_catalog_nebula_kept_disagreement():
    # IRCUT (broadband) + catalog says nebula: not a case measurement can settle
    # (nebula needs LP), so the measured category is KEPT, flagged for review --
    # never silently flipped to a category IRCUT alone can't distinguish.
    cat, used, detail = _reconcile(FilterKind.IRCUT, Category.STARFIELD, Category.NEBULA)
    assert cat is Category.STARFIELD and used is False
    assert "ircut_catalog_nebula" in detail and "kept=starfield" in detail


def test_reconcile_ircut_catalog_decides():
    cat, used, detail = _reconcile(FilterKind.IRCUT, Category.STARFIELD, Category.GALAXY)
    assert cat is Category.GALAXY and used is True
    assert "catalog=galaxy" in detail


# ---------------------------------------------------------------------------
# measured per-edge crop (Diagnosis 1, spec 2026-08-12): the planner emits a
# BORDER_SCAN before Crop; the crop rectangle comes from the measured decision.
# Zero crop is the safe default on ambiguity/failure; an explicit legacy
# crop_border_frac (config/prefs/pin) forces the symmetric crop and skips the scan.
# ---------------------------------------------------------------------------


def _plan_crop(out):
    return next(s for s in out.plan.steps if isinstance(s, Crop))


def test_border_scan_clean_yields_zero_crop_full_frame():
    resp = Responder()
    out = drive(_req(), resp)
    assert isinstance(out, PlanComplete)
    assert MeasureKind.BORDER_SCAN in resp.calls
    crop = _plan_crop(out)
    assert (crop.x, crop.y, crop.width, crop.height) == (0, 0, 1920, 1080)
    assert crop.mirror_x is True  # orientation normalization survives a zero crop
    assert out.state.crop_decision is not None and out.state.crop_decision.warning is None
    assert any(n.key == "crop" for n in out.state.notes)


def test_border_scan_measured_edges_crop_asymmetrically():
    resp = Responder()
    resp.hook(
        MeasureKind.BORDER_SCAN,
        lambda it: WorkSucceeded(
            it.id,
            BorderObservation(left=0, top=12, right=5, bottom=0, confident=True, detail="top=blank:12;right=blank:5"),
        ),
    )
    out = drive(_req(), resp)
    assert isinstance(out, PlanComplete)
    crop = _plan_crop(out)
    # frame_dims (1920, 1080): left/right trim columns (x/width), top/bottom rows (y/height)
    assert (crop.x, crop.y, crop.width, crop.height) == (0, 12, 1915, 1068)


def test_border_scan_failure_zero_crop_and_noted():
    resp = Responder()
    resp.hook(MeasureKind.BORDER_SCAN, lambda it: WorkFailed(it.id, "read error"))
    out = drive(_req(), resp)
    assert isinstance(out, PlanComplete)  # scan failure is never fatal -- safe default
    crop = _plan_crop(out)
    assert (crop.x, crop.y, crop.width, crop.height) == (0, 0, 1920, 1080)
    assert out.state.crop_decision.warning is not None
    assert any(n.key == "crop_warning" for n in out.state.notes)


def test_border_scan_ambiguous_zero_crop_with_warning():
    resp = Responder()
    resp.hook(
        MeasureKind.BORDER_SCAN,
        lambda it: WorkSucceeded(
            it.id, BorderObservation(left=0, top=0, right=0, bottom=0, confident=False, detail="top=dark_ambiguous")
        ),
    )
    out = drive(_req(), resp)
    assert isinstance(out, PlanComplete)
    crop = _plan_crop(out)
    assert (crop.x, crop.y, crop.width, crop.height) == (0, 0, 1920, 1080)
    assert out.state.crop_decision.warning is not None
    assert any(n.key == "crop_warning" for n in out.state.notes)


def test_border_scan_oversized_edge_refused():
    # 0.05 bound on a 1080-row frame = 54 px; a 300-row claim is refused (kept),
    # while the in-bounds right edge still crops
    resp = Responder()
    resp.hook(
        MeasureKind.BORDER_SCAN,
        lambda it: WorkSucceeded(
            it.id,
            BorderObservation(left=0, top=300, right=4, bottom=0, confident=True, detail="top=blank:300;right=blank:4"),
        ),
    )
    out = drive(_req(), resp)
    assert isinstance(out, PlanComplete)
    crop = _plan_crop(out)
    assert (crop.x, crop.y, crop.width, crop.height) == (0, 0, 1916, 1080)
    assert out.state.crop_decision.warning is not None


def test_forced_crop_border_frac_pin_skips_scan_symmetric():
    req = _req(pins=Overrides(crop_border_frac=0.015))
    resp = Responder()
    out = drive(req, resp)
    assert isinstance(out, PlanComplete)
    assert MeasureKind.BORDER_SCAN not in resp.calls  # forced override: no scan
    crop = _plan_crop(out)
    # legacy symmetric semantics preserved exactly: 28/16 px borders on 1920x1080
    assert (crop.x, crop.y, crop.width, crop.height) == (28, 16, 1864, 1048)
    assert out.state.crop_decision.forced is True


# ---------------------------------------------------------------------------
# session strategy
# ---------------------------------------------------------------------------


def test_combine_sessions_when_enabled():
    eng = engine_defaults_from_cfg({"combine_sessions": True})
    req = _req(engine=eng, session_count=2)
    nw = start_planning(req)
    assert isinstance(nw.items[0].request.ops[0], CombineSessions)


def test_combine_failure_falls_back_to_latest():
    eng = engine_defaults_from_cfg({"combine_sessions": True})
    req = _req(engine=eng, session_count=2)

    resp = Responder()
    resp.hook("CombineSessions", lambda it: WorkFailed(it.id, "scale mismatch"))
    out = drive(req, resp)
    assert isinstance(out, PlanComplete)
    steps = out.plan.steps
    assert isinstance(steps[0], PrepareSource) and steps[0].output.name == "working_copy_latest"
    assert any(n.key == "combine_failed" for n in out.state.notes)


def test_combine_failure_drops_ircut_network_prior_non_silently():
    # Task 4 moved the IRCUT network-prior fetch into a second SOURCE round that
    # only fires after source prep succeeds. On the combine_sessions path (opt-in,
    # default-off), a failed CombineSessions re-enters SOURCE via a fresh
    # PrepareSource(latest) fallback round that carries no local_item, so
    # needs_network can never be recomputed and the network prior is lost even
    # though the round-1 local lookup was inconclusive. That degrade must be
    # recorded (Tenet 7), not silent.
    eng = engine_defaults_from_cfg({"combine_sessions": True})
    req = _req(
        target_label="NGC5128",
        engine=eng,
        session_count=2,
        filter_kind=FilterKind.IRCUT,
        has_wcs=True,
        use_prior=True,
    )
    resp = Responder()  # local defaults to a miss (WorkUnavailable) -> inconclusive
    resp.hook("CombineSessions", lambda it: WorkFailed(it.id, "scale mismatch"))
    out = drive(req, resp)
    assert isinstance(out, PlanComplete)
    steps = out.plan.steps
    assert isinstance(steps[0], PrepareSource) and steps[0].output.name == "working_copy_latest"
    assert any(n.key == "combine_failed" for n in out.state.notes)
    assert any(n.key == "network_prior_skipped" and n.detail == "combine_fallback" for n in out.state.notes)
    # the known limitation: the network round is genuinely never issued
    assert "FetchNetworkPrior" not in resp.calls
    # left at the filter-default (measured) category since neither local nor
    # network ever decided it
    assert out.state.decisions.category is Category.STARFIELD
    assert out.state.classification.source is not CategorySource.CATALOG_PRIOR


# ---------------------------------------------------------------------------
# failure table (spec §6)
# ---------------------------------------------------------------------------


def test_prior_unavailable_degrades():
    req = _req(filter_kind=FilterKind.IRCUT, has_wcs=True, use_prior=True)
    resp = Responder()  # local AND network both None -> WorkUnavailable
    out = drive(req, resp)
    assert isinstance(out, PlanComplete)
    assert any(n.key == "prior_unavailable" for n in out.state.notes)
    # both rounds were actually attempted (local miss escalated to network, which also missed)
    assert resp.calls.count("LookupLocalTarget") == 1
    assert resp.calls.count("FetchNetworkPrior") == 1


# ---------------------------------------------------------------------------
# Task 4: SOURCE sequencing -- local lookup in round 1, IRCUT network fallback
# in an explicit second SOURCE round. `_reconcile` and `_handle_prior` are shared
# unchanged between the two rounds (see their docstrings); these tests cover the
# NEW sequencing decision: whether round 1's local result was "usable" enough to
# skip the round-2 network fetch entirely.
# ---------------------------------------------------------------------------


def test_ircut_local_hit_decides_no_network():
    # IRCUT, local hit (galaxy): round-1 LookupLocalTarget alone decides the category
    # -- the round-2 network fetch must never be emitted.
    req = _req(target_label="NGC5128", filter_kind=FilterKind.IRCUT, has_wcs=True, use_prior=True)
    resp = Responder(local=PriorObservation(catalog_category=Category.GALAXY, fov_fraction=0.1))
    out = drive(req, resp)
    assert isinstance(out, PlanComplete)
    assert out.state.decisions.category is Category.GALAXY
    assert out.state.classification.source is CategorySource.CATALOG_PRIOR
    assert "LookupLocalTarget" in resp.calls
    assert "FetchNetworkPrior" not in resp.calls


def test_ircut_local_miss_escalates_to_network():
    # IRCUT, local miss (WorkUnavailable): round 1 can't decide, so a SECOND SOURCE
    # round emits FetchNetworkPrior; its result reconciles exactly as a round-1 prior
    # hit did before this task.
    req = _req(target_label="NGC5128", filter_kind=FilterKind.IRCUT, has_wcs=True, use_prior=True)
    resp = Responder(local="unavailable", prior=PriorObservation(catalog_category=Category.GALAXY, fov_fraction=0.1))
    out = drive(req, resp)
    assert isinstance(out, PlanComplete)
    # round 1 answered both PrepareSource + LookupLocalTarget before round 2's FetchNetworkPrior
    assert resp.calls.index("LookupLocalTarget") < resp.calls.index("FetchNetworkPrior")
    assert resp.calls.index("PrepareSource") < resp.calls.index("FetchNetworkPrior")
    assert resp.calls.count("FetchNetworkPrior") == 1
    assert out.state.decisions.category is Category.GALAXY
    assert out.state.classification.source is CategorySource.CATALOG_PRIOR
    assert any(n.key == "prior_unavailable" for n in out.state.notes)  # round-1 local miss noted
    assert any(n.key == "catalog_prior" for n in out.state.notes)  # round-2 network hit noted
    # closeup still ships -- proof the second-round category feeds the rest of planning
    assert any(isinstance(s, CropTo) for s in out.plan.steps)


def test_ircut_local_ambiguous_escalates_to_network():
    # IRCUT, local ambiguous (catalog had no dominant object -> UNKNOWN): treated the
    # same as a miss -- a second round fetches the network prior.
    req = _req(target_label="NGC5128", filter_kind=FilterKind.IRCUT, has_wcs=True, use_prior=True)
    resp = Responder(
        local=PriorObservation(catalog_category=Category.UNKNOWN, fov_fraction=None),
        prior=PriorObservation(catalog_category=Category.GALAXY, fov_fraction=0.1),
    )
    out = drive(req, resp)
    assert isinstance(out, PlanComplete)
    assert "FetchNetworkPrior" in resp.calls
    assert out.state.decisions.category is Category.GALAXY
    assert out.state.classification.source is CategorySource.CATALOG_PRIOR


def test_lp_local_hit_records_identity_no_network():
    # LP, local hit: LookupLocalTarget is emitted and handled (identity recorded via the
    # catalog_prior note), but the LP filter law is unconditional in `_reconcile` -- the
    # category stays nebula regardless -- and the network step is IRCUT-only, so it must
    # never fire even though a local hit occurred.
    req = _req(filter_kind=FilterKind.LP, has_wcs=True, use_prior=True)
    resp = Responder(local=PriorObservation(catalog_category=Category.STARFIELD, fov_fraction=0.1))
    out = drive(req, resp)
    assert isinstance(out, PlanComplete)
    assert "LookupLocalTarget" in resp.calls
    assert "FetchNetworkPrior" not in resp.calls
    assert out.state.decisions.category is Category.NEBULA
    assert any(n.key == "catalog_prior" for n in out.state.notes)  # identity recorded, filter law kept it


def test_lp_skips_prior_fetch_even_when_enabled():
    # LP, local miss: round 1 still runs LookupLocalTarget (all filters incl. LP), but
    # the LP filter law makes catalog data irrelevant, so the planner must NEVER escalate
    # to the network round -- clean fallback to the measured category. (Regression guard,
    # extended for Task 4's round-1-local / round-2-network split.)
    req = _req(filter_kind=FilterKind.LP, has_wcs=True, use_prior=True)
    # A Responder that WOULD answer a network prior if ever asked; local misses by default.
    resp = Responder(prior=PriorObservation(catalog_category=Category.GALAXY, fov_fraction=0.1))
    out = drive(req, resp)
    assert isinstance(out, PlanComplete)
    assert "LookupLocalTarget" in resp.calls
    assert "FetchNetworkPrior" not in resp.calls
    prior_notes = [(n.key, n.detail) for n in out.state.notes if n.key in ("catalog_prior", "prior_unavailable")]
    assert prior_notes == [("prior_unavailable", "no-local-catalog")]
    assert out.state.decisions.category is Category.NEBULA


def test_trail_scan_failure_proceeds():
    resp = Responder()
    resp.hook(MeasureKind.TRAIL_SCAN, lambda it: WorkFailed(it.id, "scan crashed"))
    out = drive(_req(), resp)
    assert isinstance(out, PlanComplete)
    assert any(n.key == "trail_scan_failed" for n in out.state.notes)
    assert not any(isinstance(s, RemoveTrail) for s in out.plan.steps)


def test_e_probe_failure_keeps_provisional():
    eng = engine_defaults_from_cfg({"e_gate": True})
    req = _req(filter_kind=FilterKind.IRCUT, engine=eng, use_prior=False)
    resp = Responder()
    resp.hook(MeasureKind.EXTENDED_SIGNAL, lambda it: WorkUnavailable(it.id, "starnet missing"))
    out = drive(req, resp)
    assert isinstance(out, PlanComplete)
    assert any(n.key == "e_probe_failed" for n in out.state.notes)
    assert out.state.decisions.category is Category.STARFIELD  # provisional IRCUT default


def test_production_runops_failure_refuses():
    resp = Responder()
    resp.hook("Crop", lambda it: WorkFailed(it.id, "siril crashed"))
    out = drive(_req(), resp)
    assert isinstance(out, PlanningRefused)
    assert out.stage is Crop.stage and "siril" in out.reason


def test_removetrail_op_failure_refuses():
    # RemoveTrail is a production RunOps (TRAIL_SCAN only detects); its failure is
    # a refusal like any production op. Unreachable via the totality sweep (its
    # default responder never reports a trail), so covered explicitly here.
    resp = Responder(trail=True)
    resp.hook("RemoveTrail", lambda it: WorkFailed(it.id, "inpaint failed"))
    out = drive(_req(), resp)
    assert isinstance(out, PlanningRefused)
    assert out.stage is StageKey.TRAIL


def test_frame_batch_failure_refuses():
    # the FRAME RunOps batch is (StretchRender, SeparateStars) -- the Responder tags
    # by ops[0], so a SeparateStars failure inside it needs a bespoke responder.
    req = _req(target_label="G", filter_kind=FilterKind.IRCUT, has_wcs=True, use_prior=True)
    base = Responder(prior=PriorObservation(catalog_category=Category.GALAXY, fov_fraction=0.1))

    def respond(item):
        r = item.request
        if isinstance(r, RunOps) and len(r.ops) == 2 and isinstance(r.ops[1], SeparateStars):
            return WorkFailed(item.id, "starnet crashed")
        return base(item)

    out = drive(req, respond)
    assert isinstance(out, PlanningRefused)
    assert out.stage is StageKey.STAR_SEPARATION and "starnet" in out.reason


def test_linear_stats_failure_refuses():
    resp = Responder()
    resp.hook(MeasureKind.LINEAR_STATS, lambda it: WorkFailed(it.id, "stat failed"))
    out = drive(_req(), resp)
    assert isinstance(out, PlanningRefused)


def test_render_metric_failure_refuses():
    resp = Responder()
    resp.hook(MeasureKind.RENDER_METRIC, lambda it: WorkFailed(it.id, "probe render lost"))
    out = drive(_req(), resp)
    assert isinstance(out, PlanningRefused)


def test_concentration_failure_skips_cluster():
    req = _req(target_label="M5", filter_kind=FilterKind.IRCUT, use_prior=False)
    resp = Responder()
    resp.hook(MeasureKind.CONCENTRATION, lambda it: WorkFailed(it.id, "block fail"))
    out = drive(req, resp)
    assert isinstance(out, PlanComplete)
    assert out.state.decisions.ght is None
    assert any(n.key == "concentration_failed" for n in out.state.notes)


def test_input_stars_failure_skips_noted():
    resp = Responder()
    resp.hook(MeasureKind.INPUT_STARS, lambda it: WorkFailed(it.id, "findstar fail"))
    out = drive(_req(), resp)
    assert isinstance(out, PlanComplete)
    assert any(n.key == "input_stars_failed" for n in out.state.notes)


def test_subject_box_none_no_closeup():
    req = _req(target_label="G", filter_kind=FilterKind.IRCUT, has_wcs=True, use_prior=True)
    resp = Responder(prior=PriorObservation(catalog_category=Category.GALAXY, fov_fraction=0.1))
    resp.hook(MeasureKind.SUBJECT_BOX, lambda it: WorkSucceeded(it.id, SubjectBoxObservation(box=None)))
    out = drive(req, resp)
    assert isinstance(out, PlanComplete)
    assert not any(isinstance(s, CropTo) for s in out.plan.steps)
    assert any(n.key == "closeup_skipped" for n in out.state.notes)


def test_galaxy_closeup_with_star_forks_off():
    # showcase galaxy with star_forks pinned off ("versions: single"): FRAME still
    # runs (the closeup gate ignores star_forks, like legacy), but its SeparateStars
    # is planning-only work -- the non-fork plan must omit it, NOT orphan-fail.
    req = _req(
        target_label="G",
        filter_kind=FilterKind.IRCUT,
        has_wcs=True,
        use_prior=True,
        caller=Overrides(star_forks=False),
    )
    resp = Responder(prior=PriorObservation(catalog_category=Category.GALAXY, fov_fraction=0.1))
    out = drive(req, resp)
    assert isinstance(out, PlanComplete)
    plan = out.plan
    assert not any(isinstance(s, SeparateStars) for s in plan.steps)  # planning-only, omitted
    assert any(isinstance(s, CropTo) for s in plan.steps)  # closeup still ships
    assert set(_export_bases(plan)) == {"G_final", "G_closeup"}  # single hero + closeup
    hero = next(s for s in plan.steps if isinstance(s, Export) and s.output_base == "G_final")
    assert hero.input.name == "stretched"  # stars-on hero, no blend


# ---------------------------------------------------------------------------
# subject support (Diagnoses 5+3, Task 4): measured ONCE before the solve from
# the linear artifact; every body_p90 probe carries the same frozen support ref;
# sky_median (starfield) solves stay mask-free; failure/invalid degrades to the
# recorded mask-free path, never a refusal; a non-monotonic converged trace is
# demoted to the conservative fallback.
# ---------------------------------------------------------------------------


class _CaptureResponder(Responder):
    """Responder that also records every Measure request object (not just tags)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.measures = []

    def __call__(self, item):
        from seestar.core.types import Measure as _Measure

        if isinstance(item.request, _Measure):
            self.measures.append(item.request)
        return super().__call__(item)


def test_extended_solve_freezes_object_mask_region_on_first_probe():
    # Owner ruling (2026-08-14): the region every probe measures over is the
    # OBJECT MASK -- the region target level 72 was calibrated against -- frozen
    # once off the first real-chain body probe. The structure support is
    # measured too, but only as the sky complement and the faint gate's
    # reference; solving on it shipped M81/M100 at half their brightness.
    resp = _CaptureResponder()
    out = drive(_req(), resp)  # LP nebula -> body_p90 solve
    assert isinstance(out, PlanComplete)
    assert resp.calls.count(MeasureKind.SUBJECT_SUPPORT) == 1  # measured once, before the solve
    probes = [m for m in resp.measures if m.kind is MeasureKind.RENDER_METRIC]
    assert len(probes) > 1, "need more than one probe to prove the region is frozen, not re-derived"
    first, rest = probes[0], probes[1:]
    # the first probe FREEZES the region; only it carries an output ref
    assert first.output is not None and first.output.name == "M8_solve_region"
    assert first.output.role is ArtifactRole.QA and first.region is None
    # ...and every later probe reads back that same frozen raster
    assert all(m.output is None for m in rest)
    regions = {(m.region.name, m.region.role) for m in rest}
    assert regions == {("M8_solve_region", ArtifactRole.QA)}
    # the structure support rides along on every body probe, for the sky complement
    assert {m.structure_support.name for m in probes} == {"M8_support"}
    assert out.state.structure_support is not None and out.state.structure_support.valid
    assert out.state.solve_region is not None and out.state.solve_region.valid
    prov = out.plan.solve
    assert prov.region_source == "frozen_object_mask" and prov.region_area_frac == 0.08
    assert prov.nonmonotonic is False
    assert any(n.key == "subject_support" for n in out.state.notes)
    assert any(n.key == "solve_region" for n in out.state.notes)


def test_frame_filling_recovery_support_is_the_solve_region():
    # ...except where the catalogue-corroborated recovery fired: sending that
    # frame back to the object mask would restore the collapse it detected.
    resp = _CaptureResponder()
    resp.hook(
        MeasureKind.SUBJECT_SUPPORT,
        lambda it: WorkSucceeded(
            it.id,
            SubjectSupportObservation(
                area_frac=0.3245,
                dims=(1920, 1080),
                source="starless_dark_anchored",
                valid=True,
                detail="frame-filling",
                recovered=True,
            ),
        ),
    )
    out = drive(_req(), resp)
    assert isinstance(out, PlanComplete)
    probes = [m for m in resp.measures if m.kind is MeasureKind.RENDER_METRIC]
    assert probes and all(m.output is None for m in probes)  # nothing to freeze
    assert {m.region.name for m in probes} == {"M8_support"}  # the recovered support IS the region
    prov = out.plan.solve
    assert prov.region_source == "recovered_structure_support" and prov.region_area_frac == 0.3245


def test_region_freeze_failure_recorded_and_probes_fall_back_once():
    resp = _CaptureResponder()

    def unfreezable(it):
        region = (
            SolveRegionObservation(
                area_frac=0.0, dims=(1920, 1080), source="none", valid=False, detail="object mask 12px < 50px minimum"
            )
            if it.request.output is not None
            else None
        )
        return WorkSucceeded(it.id, ProbeMetricObservation(value=72.0, sky=10.0, region=region))

    resp.hook(MeasureKind.RENDER_METRIC, unfreezable)
    out = drive(_req(), resp)
    assert isinstance(out, PlanComplete)  # never fatal: degrade, record, continue
    probes = [m for m in resp.measures if m.kind is MeasureKind.RENDER_METRIC]
    # asked ONCE, then never again: a later probe must not re-derive a region the
    # earlier probes did not measure against
    assert sum(1 for m in probes if m.output is not None) == 1
    assert all(m.region is None for m in probes)
    assert any(n.key == "solve_region_unavailable" for n in out.state.notes)
    assert out.plan.solve.region_source is None  # never claim a region the probes did not use


def test_starfield_sky_solve_stays_mask_free():
    req = _req(target_label="M5", filter_kind=FilterKind.IRCUT, use_prior=False)
    resp = _CaptureResponder(concentration=10.0)  # sparse: no cluster GHS either
    out = drive(req, resp)
    assert isinstance(out, PlanComplete)
    assert MeasureKind.SUBJECT_SUPPORT not in resp.calls
    probes = [m for m in resp.measures if m.kind is MeasureKind.RENDER_METRIC]
    assert probes and all(m.region is None and m.output is None for m in probes)
    assert all(m.structure_support is None for m in probes)
    assert out.plan.solve.region_source is None


def test_concentrated_cluster_measures_support_for_qa_only():
    # the cluster-halo diagnostic needs the support reference, but the starfield
    # sky_median solve itself stays mask-free and freezes no region
    req = _req(target_label="M5", filter_kind=FilterKind.IRCUT, use_prior=False)
    resp = _CaptureResponder(concentration=60.0)
    out = drive(req, resp)
    assert isinstance(out, PlanComplete)
    assert resp.calls.count(MeasureKind.SUBJECT_SUPPORT) == 1
    probes = [m for m in resp.measures if m.kind is MeasureKind.RENDER_METRIC]
    assert probes and all(m.region is None and m.output is None for m in probes)  # sky solve mask-free
    assert out.state.structure_support is not None
    assert out.state.solve_region is None


def test_support_failure_recorded_and_region_still_freezes():
    # the two concepts are independent: losing the structure support costs the
    # faint gate its reference and the solve its sky complement, but the body
    # metric still measures over a frozen object-mask region
    resp = _CaptureResponder()
    resp.hook(MeasureKind.SUBJECT_SUPPORT, lambda it: WorkFailed(it.id, "starnet crashed"))
    out = drive(_req(), resp)
    assert isinstance(out, PlanComplete)  # never fatal: degrade, record, continue
    assert any(n.key == "subject_support_failed" for n in out.state.notes)
    probes = [m for m in resp.measures if m.kind is MeasureKind.RENDER_METRIC]
    assert probes and all(m.structure_support is None for m in probes)
    assert probes[0].output is not None  # region still frozen
    assert out.plan.solve.region_source == "frozen_object_mask"


def test_invalid_support_recorded_and_region_still_freezes():
    resp = _CaptureResponder()
    resp.hook(
        MeasureKind.SUBJECT_SUPPORT,
        lambda it: WorkSucceeded(
            it.id,
            SubjectSupportObservation(
                area_frac=0.0, dims=(1920, 1080), source="none", valid=False, detail="support too small"
            ),
        ),
    )
    out = drive(_req(), resp)
    assert isinstance(out, PlanComplete)
    assert any(n.key == "subject_support_invalid" for n in out.state.notes)
    probes = [m for m in resp.measures if m.kind is MeasureKind.RENDER_METRIC]
    assert probes and all(m.structure_support is None for m in probes)
    assert out.plan.solve.region_source == "frozen_object_mask"


def test_nonmonotonic_converged_solve_demoted_to_fallback():
    # probe values reverse the lower-mtf-is-brighter law by +5 before landing a
    # within-tolerance probe: the guard must demote the converged claim
    resp = Responder()
    seq = iter([90.0, 95.0, 71.8, 71.8, 71.8, 71.8, 71.8, 71.8, 71.8, 71.8])

    def jumpy(it):
        v = next(seq)
        return WorkSucceeded(it.id, ProbeMetricObservation(value=v, sky=10.0))

    resp.hook(MeasureKind.RENDER_METRIC, jumpy)
    out = drive(_req(), resp)
    assert isinstance(out, PlanComplete)
    prov = out.plan.solve
    assert prov.nonmonotonic is True
    assert prov.converged is False  # never publish a false converged result
    assert any(n.key == "solver_nonmonotonic" for n in out.state.notes)


# ---------------------------------------------------------------------------
# recipe pins (M7-M9)
# ---------------------------------------------------------------------------


def test_mtf_pinned_skips_solve():
    req = _req(pins=Overrides(mtf_mid=0.09), pinned_solved=frozenset({"mtf_mid"}))
    out = drive(req, Responder())
    assert isinstance(out, PlanComplete)
    assert out.plan.solve is None
    render = next(s for s in out.plan.steps if isinstance(s, StretchRender))
    assert render.mtf_mid == 0.09
    assert any(n.key == "stretch_solve" and "mtf_pinned" in n.detail for n in out.state.notes)


def test_blackpoint_pinned_single_point_solve():
    req = _req(pins=Overrides(blackpoint=0.02), pinned_solved=frozenset({"blackpoint"}))
    out = drive(req, Responder())
    assert isinstance(out, PlanComplete)
    # bp pinned -> the two-point sky search never runs; bp stays the pinned value
    render = next(s for s in out.plan.steps if isinstance(s, StretchRender))
    assert render.blackpoint == 0.02
    assert out.plan.solve is not None and out.plan.solve.bp_iterations == 0


def test_solve_off_keeps_fallback_mtf():
    eng = engine_defaults_from_cfg({"stretch": {"solve_stretch": False}})
    req = _req(engine=eng)
    out = drive(req, Responder())
    assert isinstance(out, PlanComplete)
    assert out.plan.solve is None
    render = next(s for s in out.plan.steps if isinstance(s, StretchRender))
    assert render.mtf_mid == out.state.decisions.mtf_mid


def test_pin_survives_egate_reclassification():
    eng = engine_defaults_from_cfg({"e_gate": True})
    req = _req(
        filter_kind=FilterKind.IRCUT,
        engine=eng,
        use_prior=False,
        pins=Overrides(saturation=0.33),
        prefs=Overrides(),
    )
    out = drive(req, Responder(e_value=0.20))
    assert isinstance(out, PlanComplete)
    assert out.state.decisions.category is Category.GALAXY
    assert out.state.decisions.saturation == 0.33  # pin re-applied after reclassify


# ---------------------------------------------------------------------------
# plan validity + provenance
# ---------------------------------------------------------------------------


def test_every_completed_plan_is_valid():
    from seestar.core.plan_checks import validate_plan

    for kw, resp in [
        (dict(), Responder()),
        (
            dict(filter_kind=FilterKind.IRCUT, has_wcs=True, use_prior=True),
            Responder(prior=PriorObservation(Category.GALAXY, 0.1)),
        ),
        (dict(filter_kind=FilterKind.IRCUT), Responder(concentration=60.0)),
        (dict(profile="natural"), Responder()),
        (dict(profile="documentation"), Responder()),
        (dict(profile="print", out_name="print"), Responder()),
    ]:
        out = drive(_req(**kw), resp)
        assert isinstance(out, PlanComplete), kw
        assert validate_plan(out.plan) == (), kw


def test_print_noncanon_omits_starson_exports():
    out = drive(_req(profile="print", out_name="print"), Responder())
    assert isinstance(out, PlanComplete)
    bases = _export_bases(out.plan)
    assert bases == ["M8_print"]  # only the tamed hero, no stars-on / starless exports


def test_print_black_lift_reaches_production_render():
    # coverage-matrix M6: print's black_lift (a paper/shadow-floor mapping, CLAUDE.md
    # §6a) must reach the ACTUAL (non-probe) StretchRender that ships -- the probe
    # render deliberately hardcodes black_lift=0.0 (it's a post-solve paper mapping,
    # not part of the solved look; see _emit_probe), so only checking the Decisions
    # object (test_print_overlay) or the export names (test above) doesn't prove the
    # value actually threads through to the production render.
    out = drive(_req(profile="print", out_name="print"), Responder())
    assert isinstance(out, PlanComplete)
    dec = out.state.decisions
    assert dec.black_lift > 0.0
    renders = [s for s in out.plan.steps if isinstance(s, StretchRender)]
    assert len(renders) == 1  # only the production render ships (probes never enter the Plan)
    assert renders[0].black_lift == dec.black_lift


def test_solve_provenance_fields():
    out = drive(_req(), Responder())
    prov = out.plan.solve
    assert prov.level_scale == 1.0 and prov.asinh_d == 75 and prov.ght_in_probe is False
    assert prov.metric.value == "body_p90"


def test_plan_complete_solve_provenance_includes_probe_history():
    out = drive(_req(), Responder())
    assert isinstance(out, PlanComplete)
    assert out.plan.solve is not None
    assert len(out.plan.solve.probes) >= 1
    assert out.plan.solve.probes[-1].mtf_mid == out.plan.solve.mtf_mid


# ---------------------------------------------------------------------------
# totality: every WorkResult variant for every pending WorkItem kind
# ---------------------------------------------------------------------------

_VARIANTS = ["succeed", "unavailable", "failed"]
_KINDS = [
    "PrepareSource",
    "Crop",
    "BackgroundExtract",
    "Denoise",
    "ColorCalibrate",
    "StretchRender",
    MeasureKind.BORDER_SCAN,
    MeasureKind.SUBJECT_SUPPORT,
    MeasureKind.TRAIL_SCAN,
    MeasureKind.EXTENDED_SIGNAL,
    MeasureKind.LINEAR_STATS,
    MeasureKind.INPUT_STARS,
    MeasureKind.CONCENTRATION,
    MeasureKind.RENDER_METRIC,
    MeasureKind.SUBJECT_BOX,
    "LookupLocalTarget",
    "FetchNetworkPrior",
]


@pytest.mark.parametrize("variant", _VARIANTS)
@pytest.mark.parametrize("kind", _KINDS)
def test_totality_every_kind_x_variant(kind, variant):
    # a request that exercises every kind: IRCUT galaxy via prior + e_gate + cluster path.
    # `local` defaults to a genuine-but-ambiguous WorkSucceeded (UNKNOWN catalog category)
    # so the "succeed" variant of LookupLocalTarget exercises a real reconcile pass while
    # still leaving the category undecided, which keeps the round-2 FetchNetworkPrior kind
    # reachable (and thus its own succeed/unavailable/failed variants exercised) for every
    # other kind's cases too.
    eng = engine_defaults_from_cfg({"e_gate": True})
    req = _req(target_label="T", filter_kind=FilterKind.IRCUT, has_wcs=True, use_prior=True, engine=eng)
    base = Responder(
        local=PriorObservation(catalog_category=Category.UNKNOWN, fov_fraction=None),
        prior=PriorObservation(catalog_category=Category.GALAXY, fov_fraction=0.1),
        concentration=60.0,
    )

    if variant != "succeed":

        def fail(it, _v=variant):
            return (WorkUnavailable if _v == "unavailable" else WorkFailed)(it.id, "injected")

        base.hook(kind, fail)
    # must return a typed outcome, never raise (except the shell-bug ValueError path,
    # which this test never triggers because every pending id is answered).
    out = drive(req, base)
    assert isinstance(out, PlanComplete | PlanningRefused)


def test_unknown_result_id_raises():
    nw = start_planning(_req())
    with pytest.raises(ValueError):
        advance(nw.state, (WorkSucceeded("does-not-exist", OpsDone()),))


def test_advance_in_terminal_phase_raises():
    out = drive(_req(), Responder())
    with pytest.raises(ValueError):
        advance(out.state, ())
