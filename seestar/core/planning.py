"""Pure planning state machine.

`start_planning` and `advance` take and return frozen values only. When the core
needs real-world work, it emits typed `WorkItem`s for the shell to execute:
operations, measurements, or an optional catalog-prior lookup. The shell feeds
typed observations back in, and the planner either asks for more work, refuses
with a reason, or returns a complete path-free `Plan`.

`PlanPhase` is the coarse driver; within a phase, the planner dispatches on the
pending WorkItem kind. The final assembly (`_done`) applies the fork/deliverable
rules and asserts `validate_plan(plan) == ()` before returning. A planner that
emits an invalid plan is a bug, so it fails loudly.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import Enum

from .classify import classify
from .plan_checks import validate_plan
from .policy import compute_decisions, evaluate_pcc_evidence
from .solve import (
    MONO_TOL_FACTOR,
    ProbeRequest,
    SolveRequest,
    SolveState,
    apply_monotonicity_guard,
    observe_probe,
    solve_result,
    start_solve,
)
from .types import (
    ArtifactRef,
    ArtifactRole,
    BackgroundExtract,
    BorderObservation,
    Category,
    CategorySource,
    Classification,
    ColorCalibrate,
    ColorProvenance,
    CombineSessions,
    ConcentrationObservation,
    Crop,
    CropDecision,
    CropTo,
    Decisions,
    Denoise,
    EmbedMetadata,
    EngineDefaults,
    EValueObservation,
    Export,
    FetchNetworkPrior,
    FilterKind,
    InputStarsObservation,
    LinearStatsObservation,
    LookupLocalTarget,
    Measure,
    MeasureKind,
    Measurements,
    Operation,
    Overrides,
    PccEvidence,
    Plan,
    PrepareSource,
    PriorObservation,
    ProbeMetricObservation,
    RemoveTrail,
    RunOps,
    ScreenBlend,
    SeparateStars,
    SolveProvenance,
    SolveRegionObservation,
    StageKey,
    StretchRender,
    SubjectBoxObservation,
    SubjectSupportObservation,
    TargetMetric,
    TrailObservation,
    WorkItem,
    WorkRequest,
    WorkResult,
    WorkSucceeded,
)

# ---- canonical artifact refs (colon-free, space-free names) ----
_WORKING = ArtifactRef("working_copy", ArtifactRole.SOURCE)
_WORKING_LATEST = ArtifactRef("working_copy_latest", ArtifactRole.SOURCE)  # combine fallback
_CROPPED = ArtifactRef("cropped", ArtifactRole.PRODUCTION)
_DETRAILED = ArtifactRef("detrailed", ArtifactRole.PRODUCTION)
_BGE = ArtifactRef("bge", ArtifactRole.PRODUCTION)
_DENOISED = ArtifactRef("denoised", ArtifactRole.PRODUCTION)
_LINEAR = ArtifactRef("linear_master", ArtifactRole.PRODUCTION)
# Diagnosis 4: the FRESH native-color artifact a rejected/unavailable PCC attempt
# falls back to. A distinct ref guarantees no downstream consumer can ever read
# the partially calibrated PCC output.
_LINEAR_NATIVE = ArtifactRef("linear_native", ArtifactRole.PRODUCTION)
_STRETCHED = ArtifactRef("stretched", ArtifactRole.PRODUCTION)
_STARLESS = ArtifactRef("starless", ArtifactRole.PRODUCTION)
_STARS = ArtifactRef("stars", ArtifactRole.PRODUCTION)
_REDUCED = ArtifactRef("reduced", ArtifactRole.PRODUCTION)
_CLOSEUP_CROP = ArtifactRef("closeup_cropped", ArtifactRole.PRODUCTION)


class PlanPhase(Enum):
    SOURCE = "source"  # prepare/combine + prior fetch
    LINEAR = "linear"  # crop -> trail -> bge -> (E probe) -> denoise -> color
    MEASURE = "measure"  # linear stats, concentration, input stars
    SUPPORT = "support"  # stable subject support (Diagnoses 5+3), before the solve
    SOLVE = "solve"  # probe loop via core/solve
    FRAME = "frame"  # starless + subject box (galaxy closeup only)
    DONE = "done"


@dataclass(frozen=True)
class PlanningRequest:
    """Everything the planner needs, measured/resolved by the shell up front.

    Shell contract: `frame_dims` must be the dims of the selected session's FITS
    (the one `latest_session_index` names). The combine path re-measuring dims on
    the combined master is deferred with combine itself."""

    target_label: str
    out_name: str
    session_count: int
    latest_session_index: int
    frames: int
    filter_kind: FilterKind
    has_wcs: bool
    raw_measurements: Measurements
    frame_dims: tuple[int, int]  # (width, height) working FITS dims for the crop math
    engine: EngineDefaults
    profile: str
    prefs: Overrides
    pins: Overrides
    pinned_solved: frozenset[str]  # subset of {"blackpoint", "mtf_mid"}
    caller: Overrides
    use_prior: bool
    debug: bool


@dataclass(frozen=True)
class PlanningNote:
    """Provenance of a fallback/decision. `detail` is a machine string, never display copy."""

    key: str
    detail: str


@dataclass(frozen=True)
class PlanningState:
    request: PlanningRequest
    phase: PlanPhase
    pending: tuple[WorkItem, ...]
    classification: Classification | None
    decisions: Decisions | None
    executed: tuple[Operation, ...]  # reusable production work already run
    crop_decision: CropDecision | None
    # The broad subject support: what structure the SOURCE contains. Valid only;
    # None = never measured / unusable. Feeds faint-structure QA and the
    # two-point solve's sky complement -- NOT, by itself, the solve region.
    structure_support: SubjectSupportObservation | None
    solve_state: SolveState | None
    solve: SolveProvenance | None
    linear_background: float | None
    concentration: float | None
    closeup_box: tuple[int, int, int, int] | None
    notes: tuple[PlanningNote, ...]
    next_id: int
    # Diagnosis 4 (defaults keep old constructors stable): which linear artifact
    # downstream stages read (the PCC fallback swaps it to _LINEAR_NATIVE), and
    # the shipped color path's provenance.
    linear_ref: ArtifactRef = _LINEAR
    color: ColorProvenance | None = None
    # The frozen SOLVE REGION (owner ruling, 2026-08-14). Set once per solve:
    # either the recovered structure support (frame-filling recovery fired) or
    # the object mask frozen off the first real-chain body probe. An observation
    # with valid=False is a RECORDED failure to freeze -- it still stops the
    # planner asking again, so every later probe measures the same (fallback)
    # way. None = not applicable (sky_median solve) or not yet frozen.
    solve_region: SolveRegionObservation | None = None


@dataclass(frozen=True)
class NeedWork:
    state: PlanningState
    items: tuple[WorkItem, ...]  # execute IN ORDER, feed all results back together


@dataclass(frozen=True)
class PlanComplete:
    state: PlanningState
    plan: Plan


@dataclass(frozen=True)
class PlanningRefused:
    reason: str
    stage: StageKey | None


Outcome = NeedWork | PlanComplete | PlanningRefused


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _item(nid: int, request: WorkRequest) -> tuple[WorkItem, int]:
    return WorkItem(str(nid), request), nid + 1


def _items(nid: int, requests: Sequence[WorkRequest]) -> tuple[tuple[WorkItem, ...], int]:
    out = []
    for r in requests:
        it, nid = _item(nid, r)
        out.append(it)
    return tuple(out), nid


def _note(state: PlanningState, key: str, detail: str) -> PlanningState:
    return replace(state, notes=state.notes + (PlanningNote(key, detail),))


def _compute(request: PlanningRequest, classification: Classification) -> Decisions:
    return compute_decisions(
        classification,
        request.raw_measurements,
        request.engine,
        request.profile,
        prefs=request.prefs,
        pins=request.pins,
        caller=request.caller,
    )


def _decided(state: PlanningState) -> Decisions:
    """`state.decisions`, which every phase from the first `_compute` onward has.

    The field is Optional because decisions do not EXIST before SOURCE round 1
    computes them -- that Optional is a fact about the state's history, not about
    the value any later phase holds. Narrowing through here states the phase
    invariant once, where both a reader and a type checker can see it, instead of
    leaving it implicit at ~14 call sites."""
    assert state.decisions is not None, "planning phase invariant: decisions are computed before this phase"
    return state.decisions


def _classified(state: PlanningState) -> Classification:
    """`state.classification`, set by the same first `_compute` -- see `_decided`."""
    assert state.classification is not None, "planning phase invariant: classification precedes this phase"
    return state.classification


def _solving(state: PlanningState) -> SolveState:
    """`state.solve_state`, which exists for the whole span of a solve -- see `_decided`."""
    assert state.solve_state is not None, "planning phase invariant: a solve is in progress here"
    return state.solve_state


def _linear_median(state: PlanningState) -> float:
    """The linear master's measured background, set by the mandatory LINEAR_STATS
    measurement before any solve begins (the blackpoint law depends on it)."""
    assert state.linear_background is not None, "planning phase invariant: LINEAR_STATS precedes the solve"
    return state.linear_background


def _output_ref(op: Operation) -> ArtifactRef:
    """The single artifact a production op writes.

    Three members of the closed `Operation` union write something other than one
    output ref (`SeparateStars` writes two, `Export` a set, `EmbedMetadata` none),
    so this is valid only for the production ops the phase functions thread
    through `executed`. Narrowing here keeps that assumption visible instead of
    implicit at each call site."""
    assert not isinstance(op, SeparateStars | Export | EmbedMetadata), f"{type(op).__name__} has no single output"
    return op.output


def _fail_reason(result: WorkResult | None) -> str:
    """The reason to report for a work item that did not succeed.

    `None` is a legitimate argument, not a defensive default: an optional
    measurement's result slot (e.g. INPUT_STARS) stays unfilled when the planner
    never issued it, and that case reports "unknown" exactly like a result whose
    own reason is missing."""
    return getattr(result, "reason", "unknown")


# The adapter's machine-stable marker for a pcc command that could not produce a
# solution, e.g. "pcc_unavailable(catalog)".
_PCC_UNAVAILABLE_MARKER = "pcc_unavailable("


def _unavailable_reason(result: WorkResult) -> str:
    """The bounded color reason for a failed pcc attempt. ONLY the adapter's
    failure class travels, because this string reaches ColorProvenance.reason and
    from there the EXIF of every exported photo -- captured tool text (which
    echoes local file paths) must never ride along, whatever the shell put in the
    failure string. The full text stays in the run's local notes. Plain string
    parsing, not `re`: the core's import allowlist is an architecture invariant
    (tests/test_architecture.py), not a convenience to trade away."""
    reason = _fail_reason(result)
    _, marker, rest = reason.partition(_PCC_UNAVAILABLE_MARKER)
    if not marker:
        return "unavailable:tool"
    cls, closed, _ = rest.partition(")")
    if not closed or not cls.replace("_", "").isalpha():
        return "unavailable:tool"
    return f"unavailable:{cls}"


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------


def start_planning(request: PlanningRequest) -> NeedWork | PlanningRefused:
    cls = classify(request.filter_kind, request.frames, request.raw_measurements, request.engine.classify)
    dec = _compute(request, cls)

    engine = request.engine
    if engine.combine_sessions and request.session_count > 1:
        src_op: Operation = CombineSessions(session_indices=tuple(range(request.session_count)), output=_WORKING)
    else:
        src_op = PrepareSource(session_index=request.latest_session_index, output=_WORKING)

    requests: list[WorkRequest] = [RunOps((src_op,))]
    # Round 1 (SOURCE): the local catalog lookup is ALWAYS-ON (spec sec 5.7) -- offline
    # and cheap, it runs for every filter whenever the frame is plate-solved, regardless
    # of `use_prior`/`--no-prior` -- no IRCUT gate here, no use_prior gate here either.
    # The catalog prior exists to settle the one case measurement can't: IRCUT galaxy vs
    # globular. On LP the dual-band filter law is unconditional (reconcile() always
    # keeps nebula regardless of the catalog), so the local hit is still recorded
    # (identity/provenance), but it can never change an LP decision -- see
    # _advance_source, which is where the network round (and its IRCUT-only gate, PLUS
    # the use_prior gate -- `--no-prior` disables only the network fetch) now lives.
    if request.has_wcs:
        requests.append(LookupLocalTarget())
    items, nid = _items(0, requests)

    state = PlanningState(
        request=request,
        phase=PlanPhase.SOURCE,
        pending=items,
        classification=cls,
        decisions=dec,
        executed=(),
        crop_decision=None,
        structure_support=None,
        solve_state=None,
        solve=None,
        linear_background=None,
        concentration=None,
        closeup_box=None,
        notes=(),
        next_id=nid,
    )
    return NeedWork(state, items)


def advance(state: PlanningState, results: tuple[WorkResult, ...]) -> Outcome:
    by_id = {r.id: r for r in results}
    pend_ids = {it.id for it in state.pending}
    if set(by_id) != pend_ids:
        # unknown/unexpected work id (or a missing one) is a SHELL bug, not a planner
        # state -- fail loudly (totality contract).
        raise ValueError(f"result ids {sorted(by_id)} do not match pending ids {sorted(pend_ids)}")

    phase = state.phase
    if phase is PlanPhase.SOURCE:
        return _advance_source(state, by_id)
    if phase is PlanPhase.LINEAR:
        return _advance_linear(state, by_id)
    if phase is PlanPhase.MEASURE:
        return _advance_measure(state, by_id)
    if phase is PlanPhase.SUPPORT:
        return _advance_support(state, by_id)
    if phase is PlanPhase.SOLVE:
        return _advance_solve(state, by_id)
    if phase is PlanPhase.FRAME:
        return _advance_frame(state, by_id)
    raise ValueError(f"advance called in terminal phase {phase}")


# ---------------------------------------------------------------------------
# SOURCE: prepare/combine + prior fetch + reconciliation (run.py 253-414)
# ---------------------------------------------------------------------------


def _reconcile(filt: FilterKind, measured: Category, catalog: Category) -> tuple[Category, bool, str]:
    """Port of prior.reconcile (prior.py:455). Authority order:
    filter law (LP -> nebula, unconditional) > catalog (IRCUT galaxy/starfield) >
    measured default. Returns (final_category, used, note_detail); `used` True only
    when the catalog actually decided the category."""
    if catalog is Category.UNKNOWN:  # no dominant object -> catalog says nothing
        return measured, False, "no_catalog_category"
    if filt is FilterKind.LP:  # unconditional instrument law -> nebula
        return measured, False, f"filter_law_lp;catalog={catalog.value}"
    if catalog in (Category.GALAXY, Category.STARFIELD):  # the case measurement can't settle
        return catalog, True, f"catalog={catalog.value};from={measured.value}"
    # IRCUT but catalog says nebula: keep measured, flag for review (don't silently flip)
    return measured, False, f"ircut_catalog_nebula;kept={measured.value}"


def _handle_prior(state: PlanningState, result: WorkResult) -> PlanningState:
    if not isinstance(result, WorkSucceeded):
        return _note(state, "prior_unavailable", _fail_reason(result))
    obs = result.observation
    assert isinstance(obs, PriorObservation)
    final_cat, used, detail = _reconcile(state.request.filter_kind, _classified(state).category, obs.catalog_category)
    state = _note(state, "catalog_prior", detail)
    if used:
        # set source=CATALOG_PRIOR even when the category is unchanged: this records
        # that the prior DECIDED (run.py `prior_used`), which suppresses the E-gate.
        cls = replace(_classified(state), category=final_cat, source=CategorySource.CATALOG_PRIOR)
        return replace(state, classification=cls, decisions=_compute(state.request, cls))
    return state


def _advance_source(state: PlanningState, by_id: dict[str, WorkResult]) -> Outcome:
    src_item: WorkItem | None = None
    src_ops: tuple[Operation, ...] | None = None
    local_item = None
    network_item = None
    for it in state.pending:
        if isinstance(it.request, LookupLocalTarget):
            local_item = it
        elif isinstance(it.request, FetchNetworkPrior):
            network_item = it
        elif isinstance(it.request, RunOps):
            src_item, src_ops = it, it.request.ops

    # Totality guard: every SOURCE round emits either a RunOps (round 1 / the
    # combine-fallback round) or a FetchNetworkPrior (round 2), never neither --
    # the branches below read src_item/network_item unconditionally. A round with
    # neither is a SHELL/planner bug, not a state this function can proceed from.
    if src_item is None and network_item is None:
        raise ValueError(
            f"SOURCE round has neither a RunOps nor a FetchNetworkPrior item: "
            f"pending ids={[it.id for it in state.pending]}"
        )

    # Second SOURCE round: network-only (mirrors the combine-fallback re-entry below,
    # but for identity resolution rather than pixels). Source pixels were already
    # secured in round 1 -- `executed[-1]` is that src_op, still the only entry at
    # this point in SOURCE, so its `.output` is the ref LINEAR must read from.
    if network_item is not None:
        state = _handle_prior(state, by_id[network_item.id])
        return _start_linear(state, _output_ref(state.executed[-1]))

    # local lookup is present only in the first SOURCE round (the combine-fallback
    # round omits it, mirroring how the old prior-fetch round was never re-issued).
    if local_item is not None:
        state = _handle_prior(state, by_id[local_item.id])

    # The totality guard above: reaching here means this round emitted a RunOps.
    assert src_item is not None and src_ops is not None, "SOURCE round emitted neither RunOps nor FetchNetworkPrior"
    src_res = by_id[src_item.id]
    src_op = src_ops[0]
    if isinstance(src_res, WorkSucceeded):
        state = replace(state, executed=state.executed + (src_op,))
        # The local lookup DECIDED the category iff reconcile() actually used the
        # catalog (source flips to CATALOG_PRIOR only then). If it didn't -- miss,
        # unavailable, ambiguous/UNKNOWN, or an IRCUT-catalog-nebula disagreement --
        # and the filter is IRCUT, the network lookup is the only remaining chance to
        # settle galaxy vs starfield; escalate to a second SOURCE round for it. LP's
        # filter law is unconditional (reconcile() never lets the catalog move it), so
        # LP never escalates -- the IRCUT gate lives on this network step only. Because
        # the local lookup is now always-on (independent of use_prior, see start_planning),
        # `--no-prior`/use_prior=False must be checked explicitly here -- it gates ONLY
        # this network round, never the local lookup above.
        decided_locally = _classified(state).source is CategorySource.CATALOG_PRIOR
        req = state.request
        needs_network = (
            local_item is not None and not decided_locally and req.filter_kind is FilterKind.IRCUT and req.use_prior
        )
        if needs_network:
            item, nid = _item(state.next_id, FetchNetworkPrior())
            state = replace(state, pending=(item,), next_id=nid, phase=PlanPhase.SOURCE)
            return NeedWork(state, (item,))
        return _start_linear(state, _output_ref(src_op))

    # source prep failed
    if isinstance(src_op, CombineSessions):
        # combine failure -> fall back to PrepareSource(latest) under a FRESH ref
        # (the executor may have registered the original ref to combined pixels on a
        # late failure -- a different ref guarantees downstream reads the fallback).
        state = _note(state, "combine_failed", _fail_reason(src_res))
        # KNOWN LIMITATION -- v1 regression scoped to the deferred combine_sessions
        # feature (opt-in, DEFAULT-OFF, spec §7), NOT a pre-existing quirk: moving
        # the network prior fetch to an explicit second SOURCE round that only
        # fires after source prep succeeds (Task 4) means this fallback round --
        # entered here, with no `local_item` of its own -- can never recompute
        # `needs_network` and so never escalates to FetchNetworkPrior, even when
        # the round-1 local catalog lookup was inconclusive. An IRCUT galaxy
        # absent from the local catalog, whose combine also fails, is left at the
        # filter-default (starfield) category. The full fix (persist a
        # `local_inconclusive` flag on PlanningState so `needs_network` survives
        # the fallback round) is deferred with the rest of combine_sessions.
        # For now, record that the step was dropped so the degrade is visible,
        # not silent (Tenet 7).
        decided_locally = _classified(state).source is CategorySource.CATALOG_PRIOR
        req = state.request
        if req.filter_kind is FilterKind.IRCUT and req.use_prior and req.has_wcs and not decided_locally:
            state = _note(state, "network_prior_skipped", "combine_fallback")
        fb = PrepareSource(session_index=state.request.latest_session_index, output=_WORKING_LATEST)
        item, nid = _item(state.next_id, RunOps((fb,)))
        state = replace(state, pending=(item,), next_id=nid, phase=PlanPhase.SOURCE)
        return NeedWork(state, (item,))
    # PrepareSource (original or fallback) failed -> refuse (production work is mandatory)
    return PlanningRefused(reason=_fail_reason(src_res), stage=src_op.stage)


# ---------------------------------------------------------------------------
# LINEAR: border scan -> crop -> trail -> bge -> (E probe) -> denoise -> color
# ---------------------------------------------------------------------------


def decide_crop(
    frame_dims: tuple[int, int],
    obs: BorderObservation | None,
    max_auto_frac: float,
    forced_frac: float | None,
) -> CropDecision:
    """Deterministic per-edge crop decision (Diagnosis 1, spec 2026-08-12).

    Zero is the safe default: only DEMONSTRATED invalid edges (a confident
    measured observation, within the automatic bound) or an explicit legacy
    `crop_border_frac` remove pixels. An edge whose measured width exceeds
    `max_auto_frac` of its dimension is refused (kept) with a warning -- a bad
    detector must never silently discard a large fraction of a frame."""
    nx, ny = frame_dims
    if forced_frac is not None:
        bx, by = int(nx * forced_frac), int(ny * forced_frac)
        return CropDecision(
            left=bx, top=by, right=bx, bottom=by, reason="forced_border_frac", forced=True, warning=None
        )
    if obs is None:
        return CropDecision(
            left=0,
            top=0,
            right=0,
            bottom=0,
            reason="scan_failed",
            forced=False,
            warning="border scan unavailable; no crop applied (safe default)",
        )
    if not obs.confident:
        return CropDecision(
            left=0,
            top=0,
            right=0,
            bottom=0,
            reason="ambiguous",
            forced=False,
            warning=f"border scan ambiguous ({obs.detail}); no crop applied (uncertain pixels are kept)",
        )
    bound_x, bound_y = int(max_auto_frac * nx), int(max_auto_frac * ny)
    widths = {
        "left": (obs.left, bound_x),
        "right": (obs.right, bound_x),
        "top": (obs.top, bound_y),
        "bottom": (obs.bottom, bound_y),
    }
    applied: dict[str, int] = {}
    refused: list[str] = []
    for edge, (w, bound) in widths.items():
        if w > bound:
            applied[edge] = 0
            refused.append(f"{edge}={w}px>bound {bound}px")
        else:
            applied[edge] = w
    warning = None
    if refused:
        warning = "oversized measured border refused (kept as valid data): " + "; ".join(refused)
    return CropDecision(
        left=applied["left"],
        top=applied["top"],
        right=applied["right"],
        bottom=applied["bottom"],
        reason="measured",
        forced=False,
        warning=warning,
    )


def _emit_crop(state: PlanningState, source_ref: ArtifactRef, decision: CropDecision) -> NeedWork:
    nx, ny = state.request.frame_dims
    crop = Crop(
        input=source_ref,
        output=_CROPPED,
        x=decision.left,
        y=decision.top,
        width=nx - decision.left - decision.right,
        height=ny - decision.top - decision.bottom,
        mirror_x=True,  # ROWORDER instrument convention -- unconditional, even at zero crop
    )
    state = replace(state, crop_decision=decision)
    state = _note(
        state,
        "crop",
        f"left={decision.left};top={decision.top};right={decision.right};bottom={decision.bottom};"
        f"reason={decision.reason}",
    )
    if decision.warning is not None:
        state = _note(state, "crop_warning", decision.warning)
    item, nid = _item(state.next_id, RunOps((crop,)))
    return NeedWork(replace(state, phase=PlanPhase.LINEAR, pending=(item,), next_id=nid), (item,))


def _start_linear(state: PlanningState, source_ref: ArtifactRef) -> Outcome:
    forced = _decided(state).crop_border_frac
    if forced is not None:
        # explicit legacy override (config/prefs/recipe pin): symmetric crop, no scan
        decision = decide_crop(state.request.frame_dims, None, state.request.engine.crop_max_auto_frac, forced)
        return _emit_crop(state, source_ref, decision)
    item, nid = _item(state.next_id, Measure(MeasureKind.BORDER_SCAN, ref=source_ref))
    return NeedWork(replace(state, phase=PlanPhase.LINEAR, pending=(item,), next_id=nid), (item,))


def _after_border_scan(state: PlanningState, source_ref: ArtifactRef, result: WorkResult) -> Outcome:
    obs: BorderObservation | None = None
    if isinstance(result, WorkSucceeded):
        o = result.observation
        assert isinstance(o, BorderObservation)
        obs = o
    # scan failure is never fatal (safe default = zero crop); decide_crop(obs=None)
    # records the reason. Bounds come from engine.crop_max_auto_frac.
    decision = decide_crop(state.request.frame_dims, obs, state.request.engine.crop_max_auto_frac, None)
    if obs is None:
        decision = replace(
            decision, warning=f"border scan failed ({_fail_reason(result)}); no crop applied (safe default)"
        )
    return _emit_crop(state, source_ref, decision)


def _emit(state: PlanningState, request: WorkRequest) -> NeedWork:
    item, nid = _item(state.next_id, request)
    return NeedWork(replace(state, pending=(item,), next_id=nid), (item,))


def _emit_bge(state: PlanningState, bge_input: ArtifactRef) -> NeedWork:
    dec = _decided(state)
    op = BackgroundExtract(input=bge_input, output=_BGE, smoothing=dec.bge_smoothing, correction=dec.bge_correction)
    return _emit(state, RunOps((op,)))


def _after_crop(state: PlanningState) -> Outcome:
    if _decided(state).trail_removal_enabled:
        return _emit(state, Measure(MeasureKind.TRAIL_SCAN, ref=_CROPPED, output=_DETRAILED))
    return _emit_bge(state, _CROPPED)


def _after_trail(state: PlanningState, result: WorkResult) -> Outcome:
    if not isinstance(result, WorkSucceeded):
        # trail scan failed -> proceed without trail removal (objective, non-fatal)
        return _emit_bge(_note(state, "trail_scan_failed", _fail_reason(result)), _CROPPED)
    obs = result.observation
    assert isinstance(obs, TrailObservation)
    if obs.present:
        rm = RemoveTrail(input=_CROPPED, output=_DETRAILED)
        return _emit(state, RunOps((rm,)))
    return _emit_bge(state, _CROPPED)


def _after_bge(state: PlanningState) -> Outcome:
    req = state.request
    # E-gate: only IRCUT, only when engine.e_gate, only when the prior didn't decide
    # (source==CATALOG_PRIOR means prior_used). run.py 478.
    prior_decided = _classified(state).source is CategorySource.CATALOG_PRIOR
    if req.filter_kind is FilterKind.IRCUT and req.engine.e_gate and not prior_decided:
        return _emit(state, Measure(MeasureKind.EXTENDED_SIGNAL, ref=_BGE))
    return _emit_denoise(state)


def _after_eprobe(state: PlanningState, result: WorkResult) -> Outcome:
    if not isinstance(result, WorkSucceeded):
        # E probe failed -> keep the provisional filter-based classification. run.py contract.
        return _emit_denoise(_note(state, "e_probe_failed", _fail_reason(result)))
    obs = result.observation
    assert isinstance(obs, EValueObservation)
    req = state.request
    cls = classify(req.filter_kind, req.frames, req.raw_measurements, req.engine.classify, e_value=obs.e_value)
    state = replace(state, classification=cls, decisions=_compute(req, cls))
    state = _note(state, "e_gate", f"E={round(obs.e_value, 4)};category={cls.category.value}")
    return _emit_denoise(state)


def _emit_denoise(state: PlanningState) -> Outcome:
    op = Denoise(input=_BGE, output=_DENOISED, strength=_decided(state).denoise_strength)
    return _emit(state, RunOps((op,)))


def _emit_color(state: PlanningState) -> Outcome:
    op = ColorCalibrate(input=_DENOISED, output=_LINEAR, pcc=_decided(state).pcc)
    return _emit(state, RunOps((op,)))


def _emit_native_fallback(state: PlanningState, reason: str, evidence: PccEvidence | None) -> NeedWork:
    """Diagnosis 4: a rejected or unavailable PCC attempt is RECOVERABLE. The
    fallback reads the untouched denoised master into a FRESH native artifact --
    never the partially calibrated PCC output -- and every later stage follows
    `state.linear_ref` to it. The rejected op never enters `executed`, so the
    final Plan carries only the color path that actually shipped."""
    state = _note(state, "pcc_fallback", reason)
    state = replace(
        state,
        decisions=replace(_decided(state), pcc=False),
        color=ColorProvenance(attempted=True, accepted=False, fallback=True, reason=reason, evidence=evidence),
    )
    op = ColorCalibrate(input=_DENOISED, output=_LINEAR_NATIVE, pcc=False)
    return _emit(state, RunOps((op,)))


def _after_color(state: PlanningState, op: ColorCalibrate, result: WorkResult) -> Outcome:
    if not op.pcc:
        # native color (policy-off original OR the pcc fallback) is mandatory
        # production work: its failure refuses planning (failure-table row).
        if not isinstance(result, WorkSucceeded):
            return PlanningRefused(reason=_fail_reason(result), stage=op.stage)
        if state.color is None:
            state = replace(
                state,
                color=ColorProvenance(
                    attempted=False, accepted=False, fallback=False, reason="policy_off", evidence=None
                ),
            )
        state = replace(state, executed=state.executed + (op,), linear_ref=op.output)
        return _start_measure(state)
    if not isinstance(result, WorkSucceeded):
        # the full failure text still reaches the run's own notes via the
        # PlanningNote below; only the bounded class travels to EXIF.
        state = _note(state, "pcc_unavailable_detail", _fail_reason(result))
        return _emit_native_fallback(state, _unavailable_reason(result), None)
    evidence = getattr(result.observation, "pcc_evidence", None)
    verdict = evaluate_pcc_evidence(evidence, state.request.engine.pcc)
    if not verdict.accepted:
        return _emit_native_fallback(state, verdict.reason, evidence)
    state = _note(state, "pcc_accepted", verdict.reason)
    state = replace(
        state,
        executed=state.executed + (op,),
        linear_ref=op.output,
        color=ColorProvenance(
            attempted=True,
            accepted=True,
            fallback=False,
            reason=verdict.reason,
            evidence=evidence,
            qualified=verdict.qualified,
        ),
    )
    return _start_measure(state)


def _advance_linear(state: PlanningState, by_id: dict[str, WorkResult]) -> Outcome:
    item = state.pending[0]
    result = by_id[item.id]
    req = item.request

    if isinstance(req, RunOps):
        op = req.ops[0]
        if isinstance(op, ColorCalibrate):
            return _after_color(state, op, result)  # recoverable step (Diagnosis 4)
        if not isinstance(result, WorkSucceeded):
            return PlanningRefused(reason=_fail_reason(result), stage=op.stage)
        state = replace(state, executed=state.executed + (op,))
        if isinstance(op, Crop):
            return _after_crop(state)
        if isinstance(op, RemoveTrail):
            return _emit_bge(state, _DETRAILED)  # downstream consumes the detrailed ref
        if isinstance(op, BackgroundExtract):
            return _after_bge(state)
        if isinstance(op, Denoise):
            return _emit_color(state)
        raise ValueError(f"unexpected op {type(op).__name__} in LINEAR")

    # Measure -- the op branches above returned for every RunOps round.
    assert isinstance(req, Measure), "LINEAR issues only RunOps and Measure requests"
    if req.kind is MeasureKind.BORDER_SCAN:
        return _after_border_scan(state, req.ref, result)
    if req.kind is MeasureKind.TRAIL_SCAN:
        return _after_trail(state, result)
    if req.kind is MeasureKind.EXTENDED_SIGNAL:
        return _after_eprobe(state, result)
    raise ValueError(f"unexpected measure {req.kind} in LINEAR")


# ---------------------------------------------------------------------------
# MEASURE: linear stats + input stars + (concentration) (run.py 501-565)
# ---------------------------------------------------------------------------


def _start_measure(state: PlanningState) -> Outcome:
    dec = _decided(state)
    engine = state.request.engine
    reqs = [
        Measure(MeasureKind.LINEAR_STATS, ref=state.linear_ref),
        Measure(MeasureKind.INPUT_STARS, ref=state.linear_ref),
    ]
    if (
        dec.category is Category.STARFIELD
        and engine.cluster_treatment is not None
        and dec.structure_enabled
        and dec.ghs_allowed
    ):
        reqs.append(Measure(MeasureKind.CONCENTRATION, ref=state.linear_ref))
    items, nid = _items(state.next_id, reqs)
    return NeedWork(replace(state, phase=PlanPhase.MEASURE, pending=items, next_id=nid), items)


def _advance_measure(state: PlanningState, by_id: dict[str, WorkResult]) -> Outcome:
    ls_res = is_res = conc_res = None
    for it in state.pending:
        r = by_id[it.id]
        req_i = it.request
        assert isinstance(req_i, Measure), "the MEASURE phase issues only Measure requests"
        kind = req_i.kind
        if kind is MeasureKind.LINEAR_STATS:
            ls_res = r
        elif kind is MeasureKind.INPUT_STARS:
            is_res = r
        elif kind is MeasureKind.CONCENTRATION:
            conc_res = r

    # LINEAR_STATS is mandatory -- the blackpoint law needs the linear median.
    if not isinstance(ls_res, WorkSucceeded):
        return PlanningRefused(reason=_fail_reason(ls_res), stage=StageKey.STRETCH_SOLVE)
    stats = ls_res.observation
    assert isinstance(stats, LinearStatsObservation)
    linear_background = stats.background
    state = replace(state, linear_background=linear_background)

    # input stars: QA attribution only; failure is skipped with a note.
    if isinstance(is_res, WorkSucceeded):
        s = is_res.observation
        assert isinstance(s, InputStarsObservation)
        state = _note(state, "input_stars", f"count={s.star_count};fwhm={s.fwhm_px}")
    else:
        state = _note(state, "input_stars_failed", _fail_reason(is_res))

    # blackpoint: measured on the LINEAR MASTER (BGE shifted the median). Pinned bp
    # short-circuits the recompute (run.py 510).
    dec = _decided(state)
    if "blackpoint" in state.request.pinned_solved:
        bp = dec.blackpoint  # the pinned value (compute_decisions applied pins.blackpoint)
    else:
        bp = round(state.request.engine.blackpoint_factor * dec.blackpoint_scale * linear_background, 6)
    dec = replace(dec, blackpoint=bp)

    # concentration: attach the cluster GHS when a packed core stands above the field.
    concentration = None
    if conc_res is not None:
        if isinstance(conc_res, WorkSucceeded):
            c = conc_res.observation
            assert isinstance(c, ConcentrationObservation)
            concentration = c.concentration
            cmin = state.request.engine.cluster.concentration_min
            if concentration >= cmin:
                dec = replace(dec, ght=state.request.engine.cluster_treatment)
                state = _note(state, "cluster", f"concentration={round(concentration, 3)};min={cmin}")
            else:
                state = _note(state, "cluster_sparse", f"concentration={round(concentration, 3)};min={cmin}")
        else:
            state = _note(state, "concentration_failed", _fail_reason(conc_res))

    state = replace(state, decisions=dec, concentration=concentration)
    return _start_support(state)


# ---------------------------------------------------------------------------
# STRUCTURE SUPPORT: the broad subject support, measured BEFORE the solve
# (Diagnoses 5+3). It answers "what structure does the SOURCE contain": the
# faint-structure gate judges retention against it, the two-point solve uses its
# complement as honest sky, and the catalogue-corroborated frame-filling
# recovery produces it. The SOLVE REGION is a separate concept (see below).
# ---------------------------------------------------------------------------


def _structure_support_ref(state: PlanningState) -> ArtifactRef:
    return ArtifactRef(f"{state.request.target_label}_support", ArtifactRole.QA)


def _solve_region_ref(state: PlanningState) -> ArtifactRef:
    return ArtifactRef(f"{state.request.target_label}_solve_region", ArtifactRole.QA)


def _start_support(state: PlanningState) -> Outcome:
    """Extended (body_p90) targets and concentrated clusters get ONE structure
    support measurement from the linear artifact. Sparse star fields skip it
    entirely (their sky_median solve is mask-free and they have no extended
    structure to gate)."""
    dec = _decided(state)
    engine = state.request.engine
    metric = engine.stretch_targets.for_category(dec.category).metric
    needed = metric is TargetMetric.BODY_P90 or (dec.category is Category.STARFIELD and dec.ght is not None)
    if not needed:
        return _start_solve(state)
    item, nid = _item(
        state.next_id, Measure(MeasureKind.SUBJECT_SUPPORT, ref=state.linear_ref, output=_structure_support_ref(state))
    )
    return NeedWork(replace(state, phase=PlanPhase.SUPPORT, pending=(item,), next_id=nid), (item,))


def _advance_support(state: PlanningState, by_id: dict[str, WorkResult]) -> Outcome:
    result = by_id[state.pending[0].id]
    # support failure/invalidity is NEVER fatal: the solve proceeds mask-free with
    # its recorded conservative global fallback, and faint-structure QA reports
    # "not measured" -- degrade, record, continue (failure-policy table row).
    if not isinstance(result, WorkSucceeded):
        return _start_solve(_note(state, "subject_support_failed", _fail_reason(result)))
    obs = result.observation
    assert isinstance(obs, SubjectSupportObservation)
    if not obs.valid:
        return _start_solve(_note(state, "subject_support_invalid", obs.detail))
    # Geometry law: the support raster must match the post-crop frame the probes
    # render at. A mismatch is a NAMED invalidation here -- the solve then runs
    # its recorded mask-free path -- never a silent in-probe fallback while the
    # provenance claims a support was used (review finding, 2026-08-13).
    cd = state.crop_decision
    nx, ny = state.request.frame_dims
    expected = (nx - cd.left - cd.right, ny - cd.top - cd.bottom) if cd is not None else (nx, ny)
    if tuple(obs.dims) != expected:
        return _start_solve(
            _note(state, "subject_support_invalid", f"geometry {tuple(obs.dims)} != post-crop frame {expected}")
        )
    state = replace(state, structure_support=obs)
    state = _note(state, "subject_support", f"source={obs.source};area_frac={obs.area_frac}")
    return _start_solve(state)


# ---------------------------------------------------------------------------
# SOLVE: probe loop via core/solve (run.py 567-652)
# ---------------------------------------------------------------------------


def _level_base(dec: Decisions, engine: EngineDefaults) -> float:
    target = engine.stretch_targets.for_category(dec.category)
    return target.level if dec.stretch_target_level is None else dec.stretch_target_level


def _start_solve(state: PlanningState) -> Outcome:
    req = state.request
    engine = req.engine
    dec = _decided(state)

    if "mtf_mid" in req.pinned_solved:  # mtf pinned -> no solve at all (run.py _solve_mode "skip")
        return _start_frame(_note(replace(state, solve=None), "stretch_solve", "skipped_mtf_pinned"))
    if not engine.solve_stretch:  # solver off -> keep the fallback mtf_mid
        return _start_frame(_note(replace(state, solve=None), "stretch_solve", "skipped_solve_off"))

    target = engine.stretch_targets.for_category(dec.category)
    target_level = round(_level_base(dec, engine) * dec.stretch_target_scale, 1)
    # sky_ceiling drives the two-point (bp-for-sky) search; None => single-point mtf
    # solve. Off when bp is pinned (mtf_only) or the profile opts out (documentation).
    if "blackpoint" in req.pinned_solved or not dec.sky_solve:
        sky_ceiling = None
    else:
        sky_ceiling = target.sky_ceiling
    sreq = SolveRequest(
        metric=target.metric,
        target_level=target_level,
        bounds=engine.solver,
        blackpoint=dec.blackpoint,
        linear_median=_linear_median(state),
        sky_ceiling=sky_ceiling,
        sky_tol=2.0,
        max_outer=3,
    )
    solve_state, probe = start_solve(sreq)
    state = replace(state, phase=PlanPhase.SOLVE, solve_state=solve_state)
    # Owner ruling (2026-08-14): where the frame-filling recovery fired, the
    # recovered broad support IS the solve region -- sending such a frame back to
    # the object mask would return it to the collapse the recovery detected.
    # Every other body_p90 solve freezes the object mask off its first probe.
    sup = state.structure_support
    if target.metric is TargetMetric.BODY_P90 and sup is not None and sup.recovered:
        state = _note(state, "solve_region", f"source=recovered_structure_support;area_frac={sup.area_frac}")
        state = replace(
            state,
            solve_region=SolveRegionObservation(
                area_frac=sup.area_frac,
                dims=sup.dims,
                source="recovered_structure_support",
                valid=True,
                detail=sup.detail,
            ),
        )
    return _emit_probe(state, probe)


def _emit_probe(state: PlanningState, probe: ProbeRequest) -> NeedWork:
    dec = _decided(state)
    n = state.next_id
    probe_ref = ArtifactRef(f"probe_{n}", ArtifactRole.PROBE)  # unique per iteration
    render = StretchRender(
        input=state.linear_ref,
        output=probe_ref,
        asinh_d=dec.asinh_d,
        blackpoint=probe.blackpoint,
        mtf_mid=probe.mtf_mid,
        saturation=dec.saturation,
        ght=dec.ght,  # Option-A: the probe renders the SAME chain the final ships
        black_lift=0.0,  # black_lift is a paper mapping applied OUTSIDE the solve
    )
    metric = _solving(state).request.metric
    body = metric is TargetMetric.BODY_P90
    # Diagnosis 5: every body_p90 probe of one solve measures its BODY metric
    # over the SAME frozen region, so the metric responds to mtf alone. The first
    # probe freezes that region (`output`); later probes read it back (`region`).
    # A recorded failure to freeze (valid=False) leaves both None -- the shell's
    # named global fallback, chosen once, never re-derived per probe.
    region_in = region_out = None
    if body:
        reg = state.solve_region
        if reg is None:
            region_out = _solve_region_ref(state)  # first probe: freeze it
        elif reg.valid:
            region_in = (
                _structure_support_ref(state)
                if reg.source == "recovered_structure_support"
                else _solve_region_ref(state)
            )
    # Owner Gate 2, unchanged: the two-point solve's sky is measured OUTSIDE the
    # broad STRUCTURE support (on a frame-filling subject the global median sits
    # inside the nebulosity). That is the support's job, not the region's.
    structure_support = _structure_support_ref(state) if (state.structure_support is not None and body) else None
    run_item = WorkItem(str(n), RunOps((render,)))
    meas_item = WorkItem(
        str(n + 1),
        Measure(
            MeasureKind.RENDER_METRIC,
            ref=probe_ref,
            metric=metric,
            output=region_out,
            region=region_in,
            structure_support=structure_support,
        ),
    )
    items = (run_item, meas_item)
    return NeedWork(replace(state, pending=items, next_id=n + 2), items)


def _advance_solve(state: PlanningState, by_id: dict[str, WorkResult]) -> Outcome:
    run_res = met_res = None
    for it in state.pending:
        r = by_id[it.id]
        if isinstance(it.request, RunOps):
            run_res = r
        else:
            met_res = r
    # A blind solver cannot converge: if either the probe render or its metric
    # fails, planning refuses the target rather than guessing a stretch value.
    if not isinstance(run_res, WorkSucceeded):
        return PlanningRefused(reason=_fail_reason(run_res), stage=StageKey.STRETCH_SOLVE)
    if not isinstance(met_res, WorkSucceeded):
        return PlanningRefused(reason=_fail_reason(met_res), stage=StageKey.STRETCH_SOLVE)
    obs = met_res.observation
    assert isinstance(obs, ProbeMetricObservation)
    # The freezing probe reports what it froze (or that it could not). Record it
    # ONCE: valid or not, the planner stops asking, so every later probe of this
    # solve measures the same way -- a region that silently changed mid-solve is
    # the churn Diagnosis 5 exists to prevent.
    if obs.region is not None and state.solve_region is None:
        state = replace(state, solve_region=obs.region)
        state = _note(
            state,
            "solve_region" if obs.region.valid else "solve_region_unavailable",
            f"source={obs.region.source};area_frac={obs.region.area_frac};{obs.region.detail}",
        )

    solve_state, probe = observe_probe(_solving(state), obs.value, obs.sky)
    if probe is not None:  # keep probing (probe ops NEVER enter executed / the Plan)
        return _emit_probe(replace(state, solve_state=solve_state), probe)

    result = solve_result(solve_state)
    # Diagnosis 5: a converged claim off a non-monotonic probe trace is not
    # trustworthy -- demote to the conservative best-valid-probe fallback and
    # record the reason (the full trace stays in the provenance either way).
    result, violation = apply_monotonicity_guard(result, solve_state.request)
    nonmonotonic = violation > MONO_TOL_FACTOR * solve_state.request.bounds.tol
    if nonmonotonic:
        state = _note(state, "solver_nonmonotonic", f"violation={violation};tol={solve_state.request.bounds.tol}")
    dec = _decided(state)
    engine = state.request.engine
    target = engine.stretch_targets.for_category(dec.category)
    reg = state.solve_region
    prov = SolveProvenance(
        metric=target.metric,
        target_level=solve_state.request.target_level,
        achieved=result.achieved,
        sky=result.sky,
        mtf_mid=result.mtf_mid,
        blackpoint=result.blackpoint,
        converged=result.converged,
        mtf_pinned=result.mtf_pinned,
        bp_pinned=result.bp_pinned,
        bp_iterations=result.bp_iterations,
        ght_in_probe=dec.ght is not None,
        level_base=_level_base(dec, engine),
        level_scale=dec.stretch_target_scale,
        asinh_d=dec.asinh_d,
        probes=result.probes,
        # region fields describe what THIS SOLVE measured its body metric over:
        # only a body_p90 solve has a region. A cluster's sky_median solve is
        # mask-free even though a structure support was measured for its
        # faint-structure QA (that record lives in notes.subject_support, not
        # here), and a recorded failure to freeze reports None, never a region
        # the probes did not actually use.
        region_source=reg.source if (reg is not None and reg.valid) else None,
        region_area_frac=reg.area_frac if (reg is not None and reg.valid) else None,
        nonmonotonic=nonmonotonic,
    )
    dec = replace(dec, mtf_mid=result.mtf_mid, blackpoint=result.blackpoint)
    return _start_frame(replace(state, decisions=dec, solve=prov, solve_state=solve_state))


# ---------------------------------------------------------------------------
# FRAME: galaxy closeup only. The production stretch + star separation run during
# planning so the subject box can be measured from a real starless artifact.
# ---------------------------------------------------------------------------


def _production_stretch(dec: Decisions, linear_ref: ArtifactRef) -> StretchRender:
    return StretchRender(
        input=linear_ref,
        output=_STRETCHED,
        asinh_d=dec.asinh_d,
        blackpoint=dec.blackpoint,
        mtf_mid=dec.mtf_mid,
        saturation=dec.saturation,
        ght=dec.ght,
        black_lift=dec.black_lift,
    )


def _start_frame(state: PlanningState) -> Outcome:
    dec = _decided(state)
    req = state.request
    do_closeup = (
        dec.category is Category.GALAXY
        and dec.ght is not None
        and dec.ght.closeup
        and dec.closeup_enabled
        and req.out_name == "final"
    )
    if not do_closeup:
        return _done(state)
    render = _production_stretch(dec, state.linear_ref)
    sep = SeparateStars(input=_STRETCHED, starless=_STARLESS, stars=_STARS)
    run_item = WorkItem(str(state.next_id), RunOps((render, sep)))
    box_item = WorkItem(str(state.next_id + 1), Measure(MeasureKind.SUBJECT_BOX, ref=_STARLESS))
    items = (run_item, box_item)
    return NeedWork(replace(state, phase=PlanPhase.FRAME, pending=items, next_id=state.next_id + 2), items)


def _advance_frame(state: PlanningState, by_id: dict[str, WorkResult]) -> Outcome:
    run_res = box_res = run_ops = None
    for it in state.pending:
        r = by_id[it.id]
        if isinstance(it.request, RunOps):
            run_res, run_ops = r, it.request.ops
        else:
            box_res = r
    if not isinstance(run_res, WorkSucceeded):
        return PlanningRefused(reason=_fail_reason(run_res), stage=StageKey.STAR_SEPARATION)
    # The production stretch + separation are reusable work, so they enter the
    # executed list and will be marked reused during final Plan execution.
    assert run_ops is not None, "a succeeded RunOps result always carries its ops"
    state = replace(state, executed=state.executed + run_ops)

    if isinstance(box_res, WorkSucceeded):
        obs = box_res.observation
        assert isinstance(obs, SubjectBoxObservation)
        if obs.box is not None:
            state = _note(replace(state, closeup_box=obs.box), "closeup", f"box={obs.box}")
        else:
            state = _note(state, "closeup_skipped", "subject_unmeasurable")
    else:
        state = _note(state, "subject_box_failed", _fail_reason(box_res))
    return _done(state)


# ---------------------------------------------------------------------------
# DONE: assemble the final Plan and deliverable set.
# ---------------------------------------------------------------------------


def _done(state: PlanningState) -> Outcome:
    req = state.request
    dec = _decided(state)
    target = req.target_label
    out_name = req.out_name
    canon = out_name == "final"

    steps: list[Operation] = list(state.executed)
    if not dec.star_forks:
        # FRAME's SeparateStars becomes PLANNING-ONLY work when no fork consumes the
        # starless (star_forks off on a galaxy, e.g. a "versions: single" preference):
        # its purpose was the SUBJECT_BOX measurement, so it is omitted from the final
        # Plan entirely -- its outputs stay in workspace scratch and are cleaned.
        # The FRAME StretchRender stays: `stretched` IS
        # consumed by Export/CropTo. Deliberately NOT a byproducts widening -- a
        # forgotten starless Export in the fork path must still orphan-fail.
        steps = [op for op in steps if not isinstance(op, SeparateStars)]
    produced: set[ArtifactRef] = set()
    for op in steps:
        produced.update(op.outputs)
    exports: list[Export] = []

    def add(op: Operation) -> None:
        steps.append(op)
        produced.update(op.outputs)

    if _STRETCHED not in produced:
        add(_production_stretch(dec, state.linear_ref))

    if dec.star_forks:  # extended target under a star-taming profile: the three-look set
        if _STARLESS not in produced:  # already executed in FRAME for the galaxy closeup
            add(SeparateStars(input=_STRETCHED, starless=_STARLESS, stars=_STARS))
        add(ScreenBlend(starless=_STARLESS, stars=_STARS, output=_REDUCED, k=dec.star_reduce_k))
        if canon and dec.conservative_star_hero:
            # Phase 3A conservative policy: the recommended hero is the untamed
            # stars-on look; star-reduced and starless are "More versions" variants.
            # Fixed star-reduction-as-hero emptied both the NGC7000 and M8 fields.
            e_sr = Export(input=_REDUCED, output_base=f"{target}_star-reduced")
            e_sl = Export(input=_STARLESS, output_base=f"{target}_starless")
            add(e_sr)
            add(e_sl)
            exports += [e_sr, e_sl]
            hero = Export(input=_STRETCHED, output_base=f"{target}_{out_name}")
        elif canon:  # legacy: showcase owns the canonical stars-on / starless names
            e1 = Export(input=_STRETCHED, output_base=f"{target}_stars-on")
            e2 = Export(input=_STARLESS, output_base=f"{target}_starless")
            add(e1)
            add(e2)
            exports += [e1, e2]
            hero = Export(input=_REDUCED, output_base=f"{target}_{out_name}")
        else:  # non-canon (print): NO stars-on/starless exports -- ship only the tamed hero
            hero = Export(input=_REDUCED, output_base=f"{target}_{out_name}")
        add(hero)
        exports.append(hero)
    else:  # single stars-on hero (natural/documentation, star fields)
        hero = Export(input=_STRETCHED, output_base=f"{target}_{out_name}")
        add(hero)
        exports.append(hero)
        # Concentrated cluster (canonical): emit a QA starless TIFF so the faint
        # gate can measure the halo glow. The stars layer is production scratch,
        # a byproduct of SeparateStars, and is exempt from deliverable checks.
        if dec.category is Category.STARFIELD and dec.ght is not None and canon:
            add(
                SeparateStars(
                    input=_STRETCHED,
                    starless=ArtifactRef(f"{target}_starless", ArtifactRole.QA),
                    stars=ArtifactRef("cluster_stars", ArtifactRole.PRODUCTION),
                )
            )

    if state.closeup_box is not None:  # galaxy closeup (crops the stars-on render)
        add(CropTo(input=_STRETCHED, output=_CLOSEUP_CROP, box=state.closeup_box))
        ec = Export(input=_CLOSEUP_CROP, output_base=f"{target}_closeup")
        add(ec)
        exports.append(ec)

    deliverables = tuple(ref for e in exports for ref in e.outputs)
    plan = Plan(steps=tuple(steps), deliverables=deliverables, solve=state.solve, color=state.color)
    errs = validate_plan(plan)
    if errs:  # a planner that emits an invalid plan is a bug -- fail loudly
        raise AssertionError(f"planner produced an invalid plan: {errs}")
    return PlanComplete(state=replace(state, phase=PlanPhase.DONE, pending=()), plan=plan)
