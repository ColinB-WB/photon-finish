"""Imperative shell that turns one target folder into finished deliverables.

The pure planner in `seestar.core.planning` decides which work is needed. This
module performs that work in the real world: it resolves source FITS files,
executes typed operations through adapters, performs measurements, fetches an
optional catalog prior, and feeds typed observations back to the planner.

Exceptions are caught at this boundary and reported to the planner as failed or
unavailable work; the core stays pure and deterministic. Once planning returns a
final Plan, this module executes it, writes the plan sidecar, emits metadata,
annotations, recipes, and notes, then cleans the run workspace.
"""

from __future__ import annotations

import dataclasses
import datetime
import glob
import json
import os
import sys
import uuid
from dataclasses import replace

from . import execute as _execute
from . import recipe as R
from .adapters._external_process import ExternalToolFailure, ExternalToolTimeout
from .adapters.base import ExecContext
from .core.classify import classify as core_classify
from .core.planning import (
    NeedWork,
    PlanComplete,
    PlanningRefused,
    PlanningRequest,
    advance,
    start_planning,
)
from .core.publication import (
    FORK_LABELS,
    QA_ARTIFACT_LABELS,
    annotated_name,
    decide_publication,
    published_base,
    published_fork_base,
    published_qa_base,
    sidecar_names,
    solve_region_artifact_name,
    validate_extra_basename,
    validate_member_basename,
    validate_out_name,
)
from .core.qa_rules import evaluate_gates, evaluate_hero, scale_factors, star_preservation
from .core.recovery import RecoveryAction, chroma_backoff_steps, decide_candidate
from .core.types import (
    ArtifactRef,
    ArtifactRole,
    Category,
    CategorySource,
    Export,
    FetchNetworkPrior,
    FilterKind,
    LookupLocalTarget,
    Measure,
    MeasureKind,
    OpsDone,
    Overrides,
    PriorObservation,
    RunOps,
    SeparateStars,
    StageKey,
    WorkFailed,
    WorkSucceeded,
    WorkUnavailable,
)
from .debug import Debugger
from .execute import (
    ExecutionReport,
    assert_no_duplicate_work,
    execute_ops,
    execute_plan,
    take_measurement,
)
from .inspect import inspect as inspect_file
from .inspect import sanitize_label
from .io import (
    decisions_to_params,
    decisions_to_report_dict,
    engine_defaults_from_cfg,
    gate_thresholds_from_cfg,
    overrides_from_prefs_dict,
    probes_to_list,
    render_metrics_from_dict,
    run_manifest_to_dict,
    write_plan_sidecar,
    write_run_manifest,
)
from .measure import measure_fits
from .metadata import embed
from .presentation import present
from .qa import attribute_fwhm, faint_evidence, measure_render, star_count
from .tools import resolve_tools
from .workspace import Workspace

# Stage keys that are implementation details map to the first visible user stage.
# Everything else is already a public progress key.
_STAGE_KEYMAP = {"prepare_source": "crop", "combine_sessions": "crop"}

# The ordered stage-key strings that MAY appear in a plan's progress estimate, plus
# the subset that is unconditional. This stays here (not in presentation.py,
# which is display-copy only) because estimating step count is a shell/progress
# concern, not a presentation concern.
_STAGE_ORDER = [
    "analyze",
    "prior",
    "crop",
    "trail",
    "bge",
    "probe",
    "denoise",
    "color",
    "solve_stretch",
    "stretch",
    "starless",
    "star_reduce",
    "closeup",
    "export",
]
_ALWAYS = {"analyze", "crop", "bge", "denoise", "color", "stretch", "export"}


def plan_stages(flags: dict) -> list[str]:
    """The ordered stage keys that WILL run for this target, given resolved `flags`.
    Pure function -> unit-testable; the GUI uses it to show "Step i/N". Flags (all bool):
    `prior`, `trail` (default on), `probe`, `solve_stretch` (default on), `forks`
    (gates both `starless` and `star_reduce`), `closeup`. Always-on stages are unconditional."""
    gate = {
        "prior": flags.get("prior", False),
        "trail": flags.get("trail", True),
        "probe": flags.get("probe", False),
        "solve_stretch": flags.get("solve_stretch", True),
        # starless runs for extended forks AND for a concentrated cluster (QA halo artifact)
        "starless": flags.get("forks", False) or flags.get("cluster", False),
        "star_reduce": flags.get("forks", False),
        "closeup": flags.get("closeup", False),
    }
    return [k for k in _STAGE_ORDER if k in _ALWAYS or gate.get(k, False)]


_CATALOG_CAT = {"galaxy": Category.GALAXY, "starfield": Category.STARFIELD, "nebula": Category.NEBULA}


def _map_catalog_category(raw: str | None) -> Category:
    return _CATALOG_CAT.get(raw or "", Category.UNKNOWN)


class PipelineError(RuntimeError):
    """A render that stopped for a reason the app can explain: a planning refusal
    or a tool failure that reached the top. Typed so the CLI can print the
    bounded, actionable message instead of a traceback, while an untyped
    RuntimeError (a genuine defect) still surfaces as one. Subclasses
    RuntimeError so existing callers/tests that expect one keep working."""


def _candidate_starless_path(plan, output_map: dict, ws, target: str) -> str | None:
    """This attempt's starless raster, as a candidate path -- the same evidence a
    later `seestar qa` reads from the PUBLISHED `{target}_starless.tif`.

    Two producers, in the order `seestar qa` would find them: the starless FORK
    export (extended targets under a star-taming profile) and, failing that, the
    concentrated cluster's QA-role starless. Fork exports are candidate-role
    (Task 7b), so the ref is resolved through `output_map` -- reading the
    pre-rename ref would measure a previous attempt's file. None when this render
    has no starless at all, which `faint_evidence` reports as not-measured."""
    for op in plan.steps:
        if isinstance(op, Export) and op.output_base == f"{target}_starless":
            for ref in op.outputs:
                if ref.name.endswith(".tif"):
                    return ws.find_producer(output_map.get(ref, ref))
    for op in plan.steps:
        if isinstance(op, SeparateStars) and op.starless.role is ArtifactRole.QA:
            return ws.find_producer(op.starless)
    return None


def _solve_region_path(ws, target: str, ss) -> str | None:
    name = solve_region_artifact_name(ss.region_source if ss is not None else None, target)
    return ws.find_producer(ArtifactRef(name, ArtifactRole.QA)) if name else None


def _read_valid_uncertified_manifest(output_dir: str, target: str, out_name: str) -> tuple[dict | None, list[str]]:
    """Read + strictly validate the same-identity uncertified manifest (spec §5).
    Returns (manifest, validated member BASENAMES); (None, []) when the file is
    missing, unparseable, has a certified-shaped outcome, or ANY member fails
    core.publication.validate_member_basename -- one invalid member voids the
    whole inventory (deletion authorization is all-or-nothing). Member paths may
    carry a stale directory (moved output root): validation and all later
    deletion operate on os.path.basename(member) only."""
    path = os.path.join(output_dir, sidecar_names(target, out_name, False)["run"])
    if not os.path.exists(path):
        return None, []
    try:
        with open(path, encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        # ValueError covers json.JSONDecodeError AND UnicodeDecodeError (a
        # non-UTF-8 manifest file raises the latter while reading, both
        # subclasses of ValueError) -- any unparseable manifest degrades.
        return None, []
    if not isinstance(manifest, dict):
        return None, []
    if manifest.get("outcome") != "processing_failed":
        return None, []
    deliverables = manifest.get("deliverables") or []
    qa = manifest.get("qa_artifacts") or []
    if not isinstance(deliverables, list) or not isinstance(qa, list):
        return None, []
    members: list[str] = []
    for d in deliverables:
        if not isinstance(d, dict):
            return None, []
        for ext in ("tif", "png", "jpg"):
            val = d.get(ext)
            if not val:
                continue
            if not isinstance(val, str):
                return None, []
            members.append(os.path.basename(val))
    for p in qa:
        if not isinstance(p, str):
            return None, []
        members.append(os.path.basename(p))
    sn = sidecar_names(target, out_name, False)
    members.extend([sn["plan"], sn["notes"], sn["run"]])
    if not all(validate_member_basename(m, target, out_name) for m in members):
        return None, []
    return manifest, members


@dataclasses.dataclass(frozen=True)
class _Source:
    """The one capture this run renders, and what its header says about it."""

    sessions: list[str]  # every stacked master found in the target folder
    path: str  # the session this run renders
    index: int  # its position in `sessions` (the planner's session identity)
    inspection: dict
    header: dict
    target: str


@dataclasses.dataclass(frozen=True)
class _RecipePins:
    """A validated recipe replay, reduced to what planning needs (§12b)."""

    doc: dict | None
    pinned: frozenset[str]
    overrides: Overrides
    provenance: dict
    pinned_solved: frozenset[str]


def _warn_prior_backups(output_dir: str) -> list[str]:
    """Task 6 Step 3b (B-10): warn -- never touch -- crash-era .prev_* backups.

    A hard crash between promotion and the manifest commit can leave `*.prev_*`
    backups that may be the ONLY surviving copy of the prior accepted deliverables
    (tenet 2, "source data is sacred": never delete or modify them). Owner decision
    (2026-07-28): warn-and-continue. Surface on BOTH the CLI (stderr) and the GUI
    (the returned `warnings` list) -- a frozen .app never shows stdout/stderr."""
    warnings: list[str] = []
    orphans = sorted(os.path.basename(o) for o in glob.glob(os.path.join(os.path.abspath(output_dir), "*.prev_*")))
    if orphans:
        orphan_warning = (
            f"A previous render of this target was interrupted; {len(orphans)} recovery backup "
            f"file(s) remain ({', '.join(orphans)}). They are left untouched. This render produces a "
            f"fresh, consistent result; if it looks wrong, the .prev_ files are your prior deliverables "
            f"— delete them once you're satisfied."
        )
        warnings.append(orphan_warning)
        print(orphan_warning, file=sys.stderr)
    return warnings


def _select_source(target_dir: str, classify_cfg: dict) -> _Source:
    """Choose the capture to render and read it (identity is content-first, §2).

    Multiple sessions default to the LATEST by capture datetime; combining them is
    planner-driven and opt-in (§7). Nothing here writes: the import is read-only."""
    from . import run as _run  # lazy: run.run_target delegates here (avoids import cycle)

    sessions = _run._find_raw(target_dir)
    metas = {r: _run.file_meta(r) for r in sessions}
    latest = max(sessions, key=lambda r: metas[r].get("datetime") or "")
    insp = inspect_file(latest, classify_cfg=classify_cfg)
    target = insp["meta"]["target"] or sanitize_label(os.path.basename(os.path.normpath(target_dir)))
    return _Source(
        sessions=sessions,
        path=latest,
        index=sessions.index(latest),
        inspection=insp,
        header=insp["header"],
        target=target,
    )


def _resolve_preferences(output_dir: str, category: Category, engine, notes: dict) -> tuple[Overrides, dict]:
    """The user tuning layer (§12a), as typed overrides plus its provenance.

    An unreadable/invalid preferences.yaml degrades to no preferences and is
    RECORDED in `notes` -- never silently ignored (tenet 7)."""
    from . import prefs as _prefs

    try:
        prefs_ov, pref_provenance = _prefs.overrides_for_run(output_dir, category, engine)
    except ValueError as e:
        notes["preferences_error"] = str(e)
        return Overrides(), {}
    if prefs_ov != Overrides():
        notes["preferences"] = pref_provenance
    return prefs_ov, pref_provenance


def _load_recipe_pins(recipe_path: str | None, notes: dict) -> _RecipePins:
    """Load and validate a replay recipe BEFORE any work happens (§12b).

    An invalid recipe is a hard error listing every problem, raised before the
    first tool runs -- never a partially-rendered target."""
    if not recipe_path:
        return _RecipePins(None, frozenset(), Overrides(), {}, frozenset())
    doc = R.load(recipe_path)
    errs = R.validate(doc)
    if errs:
        raise R.RecipeError("Recipe errors:\n" + "\n".join(f"  • {e}" for e in errs))
    pins_ov, pinned, provenance = R.overrides_from_pins(doc)
    notes["recipe"] = {"path": recipe_path, "pinned": sorted(pinned)}
    return _RecipePins(
        doc=doc,
        pinned=pinned,
        overrides=pins_ov,
        provenance=provenance,
        pinned_solved=frozenset(n for n in ("blackpoint", "mtf_mid") if n in pinned),
    )


_DEBUG_SNAPSHOTS = {
    "Crop": ("crop", True),
    "BackgroundExtract": ("bge", True),
    "Denoise": ("denoise", True),
    "ColorCalibrate": ("color", True),
    "StretchRender": ("stretch", False),
}

# Two separate concerns, two separate tables: which progress stage a measurement
# reports as (what the user sees running), and which `side` key its measured
# detail is recorded under (what notes/QA/reports read afterwards). A kind absent
# from a table simply has no stage / no recorded detail.
_MEASURE_STAGE = {
    MeasureKind.BORDER_SCAN: "crop",
    MeasureKind.SUBJECT_SUPPORT: "solve_stretch",
    MeasureKind.TRAIL_SCAN: "trail",
    MeasureKind.EXTENDED_SIGNAL: "probe",
    MeasureKind.RENDER_METRIC: "solve_stretch",
    MeasureKind.SUBJECT_BOX: "closeup",
}
_MEASURE_SIDE_KEY = {
    MeasureKind.BORDER_SCAN: "border_scan",
    MeasureKind.SUBJECT_SUPPORT: "subject_support",
    MeasureKind.LINEAR_STATS: "linear_stats",
    MeasureKind.EXTENDED_SIGNAL: "e",
    MeasureKind.CONCENTRATION: "concentration",
    MeasureKind.SUBJECT_BOX: "subject_box",
    MeasureKind.TRAIL_SCAN: "trail",
    MeasureKind.INPUT_STARS: "input_stars",
}


def _snapshot_ops(ops, ws, dbg) -> None:
    """DEBUG only (§9a): save a small viewable JPG after each linear/stretch stage
    so every component's effect can be inspected. Never touches pipeline data."""
    if not dbg.enabled:
        return
    for op in ops:
        nm = _DEBUG_SNAPSHOTS.get(type(op).__name__)
        if nm is None:
            continue
        out = getattr(op, "output", None)
        if out is None:
            continue
        path = ws.find_producer(out)
        if path:
            dbg.snap(path, nm[0], linear=nm[1])


def _work_failure_reason(e: Exception) -> str:
    """The bounded, novice-safe `WorkFailed` reason for an exception at this boundary."""
    # C2 (final review): the 200-char cap below exists to keep a routine
    # WorkFailed reason bounded (it can end up in notes/logs), but an
    # ExternalToolTimeout's message (seestar/adapters/_external_process.py::
    # _timeout_message) is itself already novice-safe and bounded -- and the
    # GUI's `_friendly_error` recognizes it ONLY by its two trailing markers
    # ("did not finish within" ... "no source photo was changed"). A blind
    # [:200] truncation on a wrapped ExecutionError (whose message can carry
    # an op-name prefix ahead of the real exception text) could cut the
    # message before those markers, silently downgrading an actionable
    # timeout error into generic fallback copy. Exempt any message carrying
    # both markers from truncation so they always survive intact; every
    # other exception keeps the same 200-char bound as before.
    msg = str(e)
    lower_msg = msg.lower()
    # Diagnosis 7: a TYPED tool failure is already bounded by construction
    # (a summary capped at SUMMARY_LIMIT inside a message capped at
    # MESSAGE_LIMIT) and every character of it is actionable, so cutting
    # it at 200 would only destroy the part that says what to do. The
    # marker test stays as a second line of defence for a message whose
    # typed cause was lost in wrapping.
    typed = isinstance(e, ExternalToolFailure | ExternalToolTimeout) or isinstance(
        getattr(e, "__cause__", None), ExternalToolFailure | ExternalToolTimeout
    )
    if typed or ("did not finish within" in lower_msg and "no source photo was changed" in lower_msg):
        return f"{type(e).__name__}: {msg}"
    return f"{type(e).__name__}: {msg[:200]}"


def _lookup_local_target(item, state, ctx, source_path, header, raw_m):
    """Offline local-catalog lookup, by WCS footprint overlap -- never by name.

    Reports what the catalog knows about this field; the core's `reconcile()`
    remains the authority on what the category ends up being."""
    from . import known_targets as _kt
    from . import prior as _prior

    records = _kt.load_catalog()
    if not records:
        return WorkUnavailable(item.id, "local_catalog_unavailable"), (
            "local_prior",
            {"status": "local_catalog_unavailable"},
        )
    center = _prior.field_center(source_path)
    if center is None:
        # No WCS: an OBJECT-name match would be notes-only/advisory and
        # cannot supply a category (spec sec5.7) -- the full advisory note
        # is Task 6's; here just decline cleanly.
        return WorkUnavailable(item.id, "miss"), ("local_prior", {"status": "no_wcs"})
    fov_arcmin = _prior._fov_long_arcmin(header)
    if fov_arcmin is None:
        # Header lacks the convenience `pixscale_arcsec` field (e.g. a
        # PC+CDELT-convention WCS -- `inspect.read_header` only special-
        # cases a CD1_1/CD1_2 matrix). Derive it from the full WCS
        # instead of ever handing `match_target` a None FOV: its
        # `_fov_radius_arcmin` does `max(0.0, fov_arcmin)`, which raises
        # TypeError on None (P1 bug -- crashed the render after a
        # successful WCS solve).
        fov_arcmin = _prior._fov_long_arcmin_from_wcs(source_path)
    if fov_arcmin is None:
        # Genuinely no usable pixel scale anywhere (header nor full WCS)
        # -- a clean recorded miss, never a crash (Tenet 7: no silent
        # skip/degrade). Distinct status from a plain spatial miss so
        # notes/debugging can tell it apart.
        return WorkUnavailable(item.id, "miss"), ("local_prior", {"status": "no_fov"})
    result = _kt.match_target(records, center, fov_arcmin, None)
    if result.status == "ambiguous":
        return WorkUnavailable(item.id, "ambiguous"), ("local_prior", {"status": "ambiguous"})
    if result.status in ("miss", "spatially_rejected"):
        return WorkUnavailable(item.id, "miss"), ("local_prior", {"status": result.status})

    # hit -- category AUTHORITY belongs to the core's reconcile(), never to
    # this shell gate (spec sec5.5: "category disagreement is handled by
    # the existing reconcile() authority"). classify_conflict is consulted
    # here for one narrower purpose only, which does not withhold the
    # category from a plausible hit:
    #   "spatially_impossible" -- the record's OWN stored position is
    #   invalid; a data-integrity guard (Task 2), not a live decision.
    # Whether this hit's asserted category was ultimately ADOPTED or
    # CONTRADICTED can only be known once the pipeline settles its FINAL
    # authoritative classification -- this dispatch runs in SOURCE round 1,
    # before any E-measurement/reconcile has necessarily happened. That
    # comparison is made post-classification, in `_prior_outcome_note`
    # (called from `_build_notes` near the end of run_target, once
    # `state.classification` is final), against `record.category` carried
    # in this side-detail dict.
    record = result.record
    conflict = _kt.classify_conflict(record, state.classification.category)
    if conflict == "spatially_impossible":
        return WorkUnavailable(item.id, "miss"), (
            "local_prior",
            {"status": "spatially_impossible", "record": record},
        )
    # Owner Gate 2: the catalogued extent, as a fraction of THIS frame,
    # is the independent expectation that lets the support measurement
    # notice it has collapsed onto a frame-filling subject's brightest
    # knots. Shell-side only -- it never enters `to_observation` and so
    # never crosses the core boundary (v1 rule: footprint stays here).
    from .measure import catalogued_extent_frac

    ctx.subject_extent_frac = catalogued_extent_frac(
        record.extent_major_arcmin,
        record.extent_minor_arcmin,
        raw_m.dims,
        raw_m.pixscale_arcsec,
    )
    return WorkSucceeded(item.id, _kt.to_observation(record)), (
        "local_prior",
        {"status": "hit", "record": record, "conflict": conflict},
    )


def _fetch_network_prior(item, source_path, header, cfg):
    """Astrophysical prior for this field from the network catalog (SIMBAD).

    Unavailable is a normal outcome, reported as such -- the planner's failure
    table degrades to the measured classification."""
    from . import prior as _prior

    pri = _prior.prior_for(source_path, header, cfg)
    if not pri.get("available"):
        return WorkUnavailable(item.id, pri.get("source") or "unavailable"), ("prior", pri)
    obs = PriorObservation(
        catalog_category=_map_catalog_category(pri.get("catalog_category")),
        fov_fraction=pri.get("fov_fraction"),
    )
    return WorkSucceeded(item.id, obs), ("prior", pri)


def _dispatch_work(item, state, *, ctx, cfg, tools, ws, dbg, report, source_path, header, raw_m, emit, on_op_stage):
    """Perform ONE typed work request from the planner and report the outcome.

    This is the shell side of the core boundary: the work happens against real
    tools and files here, and every exception is caught and handed back as typed
    `WorkFailed`/`WorkUnavailable` -- the core never sees an exception, and
    `core/planning.py`'s failure-policy table decides what each failure means.
    Returns `(result, side_entry)`, where the side entry (or None) is the
    measured detail that notes, QA, and the reports read afterwards."""
    req = item.request
    try:
        if isinstance(req, RunOps):
            execute_ops(req.ops, ctx, report=report, on_stage=on_op_stage)
            _snapshot_ops(req.ops, ws, dbg)
            # Diagnosis 4: drain any PCC evidence the Siril adapter reported
            # for this batch so the core can judge it (never judged shell-side).
            pcc_ev = ctx.pcc_evidence[-1] if ctx.pcc_evidence else None
            ctx.pcc_evidence.clear()
            return WorkSucceeded(item.id, OpsDone(pcc_evidence=pcc_ev)), None
        if isinstance(req, Measure):
            stage = _MEASURE_STAGE.get(req.kind)
            if stage is not None:
                emit(stage)
            obs, detail = take_measurement(req, ctx, cfg, tools)
            key = _MEASURE_SIDE_KEY.get(req.kind)
            return WorkSucceeded(item.id, obs), (key, detail) if key else None
        if isinstance(req, LookupLocalTarget):
            emit("prior")
            return _lookup_local_target(item, state, ctx, source_path, header, raw_m)
        if isinstance(req, FetchNetworkPrior):
            emit("prior")
            return _fetch_network_prior(item, source_path, header, cfg)
    except Exception as e:  # boundary: exceptions never reach the core
        # Diagnosis 4: a batch that appended PCC evidence and THEN failed must
        # not leave that entry behind for the next batch's drain to claim.
        ctx.pcc_evidence.clear()
        return WorkFailed(item.id, _work_failure_reason(e)), None
    return WorkFailed(item.id, f"unhandled work request {type(req).__name__}"), None


def _drive_planner(request, *, work_deps: dict, collect, replan, emit) -> PlanComplete:
    """Run the planner's NeedWork loop to completion and return the final plan.

    The core asks for work in typed rounds; this loop performs each round through
    `_dispatch_work` and hands the typed results back with `advance`. A planning
    REFUSAL at any point is a bounded, explainable stop (`PipelineError`), never a
    partially-rendered target."""
    outcome = start_planning(request)
    if isinstance(outcome, PlanningRefused):
        raise PipelineError(outcome.reason)
    # Seed the step estimate from the initial planning state before the first
    # progress event, so "analyze" reports as step 1 of N rather than 0 of 0.
    replan(outcome.state)
    emit("analyze")
    while isinstance(outcome, NeedWork):
        state = outcome.state
        replan(state)
        results = []
        for item in outcome.items:
            res, entry = _dispatch_work(item, state, **work_deps)
            results.append(res)
            collect(entry)
        outcome = advance(state, tuple(results))
        if isinstance(outcome, PlanningRefused):
            raise PipelineError(outcome.reason)

    assert isinstance(outcome, PlanComplete)
    return outcome


def _to_candidate_plan(plan, target: str, out_name: str):
    """Rewrite every Export in `plan` to render into scratch as a CANDIDATE.

    Returns `(hero_export, exec_plan, output_map)`, where `hero_export` is the
    ORIGINAL recommended-name export op (None when this plan has none) and
    `output_map` maps each original deliverable ref to the candidate ref that
    actually gets rendered.

    Task 7b: the hero and every fork (`{T}_stars-on`/`{T}_starless`/
    `{T}_star-reduced`/`{T}_closeup` -- the DEFAULT deliverable set for showcase
    nebulae/galaxies, CLAUDE.md Stage 5) go through this together, so the
    promotion loop inside the publication transaction is the ONLY thing that ever
    writes into output_dir under a real deliverable name. A crash any time before
    that promotion (e.g. mid hero-candidate validation) leaves EVERY prior
    deliverable -- hero and fork alike -- untouched, instead of the fork exports
    having already overwritten the prior run's files during a plain execute_plan
    (the clobber the reviewer reproduced)."""
    hero_export = None
    for op in plan.steps:
        if isinstance(op, Export) and op.output_base == f"{target}_{out_name}":
            hero_export = op
            break

    output_map: dict[ArtifactRef, ArtifactRef] = {}
    exec_steps_list: list = list(plan.steps)

    if hero_export is not None:
        candidate_hero = replace(hero_export, role=ArtifactRole.CANDIDATE)
        exec_steps_list = [candidate_hero if op is hero_export else op for op in exec_steps_list]
        # hero_export.outputs and candidate_hero.outputs are same-length tuples
        # built from the same `formats` tuple in the same order (only `role`
        # differs) -- zip pairs each old deliverable ref with its candidate
        # counterpart so plan.deliverables can be substituted ref-for-ref.
        output_map.update(zip(hero_export.outputs, candidate_hero.outputs, strict=True))

    for fork_op in [op for op in plan.steps if isinstance(op, Export) and op is not hero_export]:
        candidate_fork = replace(fork_op, role=ArtifactRole.CANDIDATE)
        exec_steps_list = [candidate_fork if op is fork_op else op for op in exec_steps_list]
        output_map.update(zip(fork_op.outputs, candidate_fork.outputs, strict=True))

    candidate_deliverables = tuple(output_map.get(ref, ref) for ref in plan.deliverables)
    exec_plan = replace(plan, steps=tuple(exec_steps_list), deliverables=candidate_deliverables)
    return hero_export, exec_plan, output_map


@dataclasses.dataclass(frozen=True)
class _HeroEvaluator:
    """Measures ONE executed candidate hero and judges it against the gates.

    Everything the judgement depends on is fixed for the run and bound once here,
    so every recovery attempt is judged by exactly the same standard: the run's
    workspace and tools, the QA thresholds, the source-relative star and fwhm
    evidence from the input's own linear master, and the solve provenance that
    says WHICH pixels the contract metric must be re-measured over (Diagnosis 5).
    `measure` returns `(hero, paths, body_saturation_median, star_count, gates)`."""

    ws: Workspace
    tools: object
    plan: object
    output_map: dict
    target: str
    category: Category
    profile: str
    thresholds: object
    mask_cfg: object
    pixscale: float | None
    tol_cfg: object
    solve: object
    region_path: str | None
    input_fwhm: float | None
    input_star_count: int | None
    support_png: str | None
    support_recorded: bool
    run_warnings: list[str]

    def measure(self, cand_outputs):
        paths = {ref.name.rsplit(".", 1)[-1]: self.ws.find_producer(ref) for ref in cand_outputs}
        m = measure_render(paths.get("jpg"), mask_cfg=self.mask_cfg)
        cnt, fwhm = star_count(paths.get("tif"), self.tools)
        m["star_count"], m["fwhm"] = cnt, fwhm
        faint, faint_warn = faint_evidence(
            _candidate_starless_path(self.plan, self.output_map, self.ws, self.target),
            self.support_png,
            self.support_recorded,
        )
        if faint_warn and faint_warn not in self.run_warnings:
            self.run_warnings.append(faint_warn)  # once, not once per recovery attempt
        qa_report = evaluate_gates(
            render_metrics_from_dict(m),
            faint,
            self.category,
            self.thresholds,
            self.pixscale,
            self.profile,
            input_star_count=self.input_star_count,
        )
        solve_converged = self.solve.converged if self.solve is not None else None
        contract_delta = None
        solve_miss = None
        if self.solve is not None:
            if self.solve.metric.value == "body_p90" and self.region_path is not None:
                achieved = _execute._probe_metric(paths.get("jpg"), "body_p90", support_path=self.region_path)
            elif self.solve.metric.value == "body_p90":
                achieved = m.get("body_p90")
            else:
                achieved = m.get("background_median_outside_mask")
            if achieved is not None:
                contract_delta = round(achieved - self.solve.achieved, 1)
            # How far the solver's OWN final probe landed from its target (distinct
            # from contract_delta, which is drift introduced by LATER stages) --
            # lets evaluate_hero treat a razor-thin non-convergence as a near miss
            # rather than a blocking failure (2026-08-01, the M8 finding).
            solve_miss = abs(self.solve.achieved - self.solve.target_level)
        # Source-limited fwhm attribution (Phase 2 runtime completion, mirroring
        # qa.py's `seestar qa` path exactly): when the INPUT's own stars already
        # exceed the scaled fwhm ceiling, a failing render fwhm is the data's
        # fault, not the pipeline's -- excuse it from the hero's PROCESSING
        # failures. This is what lets a genuinely soft capture reach the
        # chroma-only condition Phase 3B recovery requires, instead of an fwhm
        # fail permanently blocking it. One attribution, reused; never two.
        ref_ceil = self.thresholds.fwhm_ceiling.for_category(self.category) if self.thresholds.fwhm_ceiling else None
        fwhm_scale, _ = scale_factors(self.thresholds, self.pixscale, m.get("dimensions"))
        scaled_ceil = round(ref_ceil * fwhm_scale, 2) if ref_ceil is not None else None
        attr = attribute_fwhm(m.get("fwhm"), self.input_fwhm, scaled_ceil)
        source_limited = frozenset({"fwhm"}) if attr == "source-limited" else frozenset()
        return (
            evaluate_hero(
                qa_report,
                solve_converged,
                contract_delta,
                self.tol_cfg,
                source_limited_gates=source_limited,
                solve_miss=solve_miss,
            ),
            paths,
            m.get("body_saturation_median", 0.0),
            m.get("star_count"),
            qa_report,
        )


@dataclasses.dataclass(frozen=True)
class _Recovery:
    """The candidate hero this run ships, and how it got there."""

    hero: object
    paths: dict
    gates: object
    star_count: int | None
    body_saturation: float
    initial_body_saturation: float | None
    attempt: int
    base_saturation: float
    final_saturation: float
    saturation_ceiling: float | None


def _run_recovery(
    evaluator, hero_export, exec_plan, output_map, *, engine, category, base_saturation, ctx, report, on_op_stage
) -> _Recovery:
    """Phase 3B bounded recovery: measure the executed CANDIDATE hero; if it fails
    ONLY the pegged_chroma gate on a converged solve, lower saturation through a
    bounded bracket and re-render (reusing the upstream BGE/denoise/color
    artifacts), keeping the strongest candidate that clears the gate. attempt 0 is
    byte-for-byte the pre-3B path, so every passing target is unchanged by
    construction.

    `output_map` is updated IN PLACE to point at whatever each attempt actually
    rendered -- the publication transaction promotes from it afterwards."""
    # remap each attempt from the PRISTINE exec_plan so the suffixes stay flat
    # (satu1, satu2, ...) and every attempt reuses the same upstream artifacts.
    # The loop's own bound (attempt <= max_attempts) is a hard backstop even if
    # decide_candidate ever misbehaves.
    bracket = engine.recovery.saturation_bracket
    max_attempts = len(bracket)
    sat_ceiling = (
        engine.recovery.body_saturation_ceiling.for_category(category)
        if engine.recovery.body_saturation_ceiling
        else None
    )
    # Task 7b: attempt-0 snapshot of orig-ref -> candidate-ref, frozen so each
    # attempt's rename (always computed straight from exec_plan.steps, per
    # chroma_backoff_steps's own "remap from the PRISTINE exec_plan" contract)
    # can look up "what did THIS attempt rename the attempt-0 candidate to".
    output_map0 = dict(output_map)
    attempt = 0
    initial_body_sat: float | None = None
    while True:
        hero_cand_outputs = tuple(output_map[r] for r in hero_export.outputs)
        hero, candidate_paths, body_sat, hero_star_count, hero_gates = evaluator.measure(hero_cand_outputs)
        if initial_body_sat is None:
            initial_body_sat = body_sat
        decision = decide_candidate(
            hero, attempt, max_attempts, body_saturation_median=body_sat, saturation_ceiling=sat_ceiling
        )
        if decision.action is not RecoveryAction.RECOVER or attempt >= max_attempts:
            break
        new_saturation = round(base_saturation * bracket[attempt], 4)
        attempt += 1
        recovered_steps = chroma_backoff_steps(exec_plan.steps, new_saturation, f"satu{attempt}")
        rename = {
            oref: nref
            for o, n in zip(exec_plan.steps, recovered_steps, strict=False)
            for oref, nref in zip(o.outputs, n.outputs, strict=False)
            if oref != nref
        }
        active_plan = replace(
            exec_plan, steps=recovered_steps, deliverables=tuple(rename.get(r, r) for r in exec_plan.deliverables)
        )
        # Re-point EVERY tracked candidate ref (hero AND every fork -- Task 7b
        # made forks CANDIDATE-role too, so a single `next(op for op in
        # recovered_steps if op.role is CANDIDATE)` is no longer unambiguous)
        # at whatever this attempt actually rendered it as.
        for orig_ref, cand0_ref in output_map0.items():
            if cand0_ref in rename:
                output_map[orig_ref] = rename[cand0_ref]
        execute_plan(active_plan, ctx, on_stage=on_op_stage, report=report)

    return _Recovery(
        hero=hero,
        paths=candidate_paths,
        gates=hero_gates,
        star_count=hero_star_count,
        body_saturation=body_sat,
        initial_body_saturation=initial_body_sat,
        attempt=attempt,
        base_saturation=base_saturation,
        final_saturation=round(base_saturation * bracket[attempt - 1], 4) if attempt > 0 else base_saturation,
        saturation_ceiling=sat_ceiling,
    )


def _hero_contract_note(rec: _Recovery, thresholds, input_star_count) -> dict:
    """The persisted record of the contract the SHIPPED hero was judged against."""
    note = {
        "outcome": rec.hero.outcome.value,
        "reasons": list(rec.hero.reasons),
        "attempt": rec.attempt,
        # The gate verdicts the DECIDING surface actually reached, persisted so
        # a later `seestar qa` over the same committed identity can be shown to
        # reach the same ones (owner ruling, 2026-08-14 -- the two surfaces
        # silently disagreed on faint_structure before this).
        "gates": {g.gate: g.verdict.value for g in rec.gates.gates},
    }
    # Source-relative star-preservation evidence for the SHIPPED hero (Diagnosis
    # 2): both counts, ratio, and thresholds, so notes and a later `seestar qa`
    # replay carry the same record the gate verdict was computed from.
    _, sp_record = star_preservation(input_star_count, rec.star_count, thresholds)
    note["star_preservation"] = dataclasses.asdict(sp_record)
    if rec.attempt > 0:
        note["recovered_saturation"] = rec.final_saturation
    # Aesthetic saturation-target reporting (Phase 4C). Emitted ONLY when a ceiling
    # is configured -- the default null-ceiling path stays clean. States plainly
    # whether the shipped hero met the aesthetic target or is a best-effort at the
    # bracket floor, so the manifest/notes never imply success the controller
    # didn't achieve (external review, 2026-07-18).
    if rec.saturation_ceiling is not None:
        target_met = rec.body_saturation <= rec.saturation_ceiling
        note["saturation_recovery"] = {
            "status": "target_met" if target_met else "best_effort_exhausted",
            "target_met": target_met,
            "ceiling": rec.saturation_ceiling,
            "initial_metric": rec.initial_body_saturation,
            "final_metric": rec.body_saturation,
            "initial_saturation": rec.base_saturation,
            "final_saturation": rec.final_saturation,
        }
    return note


def _published_base_for(orig_base: str, target: str, out_name: str, certified: bool) -> str:
    """The published basename for one planner export base (hero, fork, or QA
    artifact) under this run's certification. The ONE naming contract every
    promoted file goes through -- an unmapped base is a defect, not a fallback."""
    if orig_base == f"{target}_{out_name}":
        return published_base(target, out_name, certified)
    for fork in FORK_LABELS:
        if orig_base == f"{target}_{fork}":
            return published_fork_base(target, fork, certified)
    for label in QA_ARTIFACT_LABELS:
        if orig_base == f"{target}_{label}":
            return published_qa_base(target, label, certified)
    raise AssertionError(
        f"unmapped export base {orig_base!r} — every planner export must resolve "
        "through the publication naming contract"
    )


def _assemble_publication_set(
    ws,
    plan,
    hero_export,
    output_map,
    candidate_paths,
    state,
    *,
    target: str,
    out_name: str,
    certified: bool,
    canon: bool,
):
    """Everything this run publishes, as `(plan_pairs, variants, qa_artifact_names)`.

    `plan_pairs` is (candidate_path -> final deliverable path) for every raster the
    transaction promotes: the hero, every fork, and the QA-role artifacts (the
    concentrated-cluster starless, the frozen support raster, the frozen solve
    region). `variants` groups the deliverables by output base for metadata embed;
    `qa_artifact_names` are the basenames the manifest records as attributable
    evidence rather than user deliverables. Raises before any promotion if a
    candidate this set names does not exist."""
    plan_pairs: list[tuple[str, str]] = []  # (candidate_path, final_deliverable_path)
    variants: dict[str, dict[str, str]] = {}  # output_base -> {ext: final_path}

    def _add(base: str, ext: str, cand: str) -> None:
        fp = os.path.join(ws.output_dir, f"{_published_base_for(base, target, out_name, certified)}.{ext}")
        plan_pairs.append((cand, fp))
        variants.setdefault(base, {})[ext] = fp

    for ext, candidate_path in candidate_paths.items():
        _add(hero_export.output_base, ext, candidate_path)
    for op in plan.steps:
        if isinstance(op, Export) and op is not hero_export:
            for orig_ref in op.outputs:
                _add(op.output_base, orig_ref.name.rsplit(".", 1)[-1], ws.find_producer(output_map[orig_ref]))

    # ---- Task 4: QA-role artifacts (the concentrated-cluster starless,
    # core/planning.py's `_done` -- the faint-structure gate's evidence)
    # execute into scratch (workspace.py's _OUTPUT_ROLES no longer
    # includes QA) and are promoted INSIDE this same transaction, never
    # written to output_dir directly. Recorded (basenames) in the
    # manifest's `qa_artifacts` -- attributable, not a user deliverable. ----
    qa_artifact_names: list[str] = []
    for op in plan.steps:
        if isinstance(op, SeparateStars) and op.starless.role is ArtifactRole.QA:
            cand = ws.find_producer(op.starless)
            if cand:
                ext = os.path.splitext(cand)[1]
                fp = os.path.join(
                    ws.output_dir, f"{_published_base_for(op.starless.name, target, out_name, certified)}{ext}"
                )
                plan_pairs.append((cand, fp))
                qa_artifact_names.append(os.path.basename(fp))
    # Diagnoses 5+3: the frozen subject-support raster is durable QA evidence
    # (a later `seestar qa` re-measures the contract metric and judges faint
    # structure against it) -- promoted inside this same transaction as a
    # manifest-referenced qa_artifact, exactly like the cluster starless.
    if state.structure_support is not None and canon:
        support_cand = ws.find_producer(ArtifactRef(f"{target}_support", ArtifactRole.QA))
        if support_cand:
            fp = os.path.join(
                ws.output_dir, f"{_published_base_for(f'{target}_support', target, out_name, certified)}.png"
            )
            plan_pairs.append((support_cand, fp))
            qa_artifact_names.append(os.path.basename(fp))
    # ...and so is the frozen SOLVE REGION (owner ruling, 2026-08-14): a later
    # `seestar qa` must re-measure the contract metric over the very pixels
    # the solve drove, not over a fresh object mask of the final render that
    # merely resembles them. Published only when the solve actually froze one
    # (the recovery case reuses the support raster above -- see
    # `solve_region_artifact_name`, the ONE mapping both surfaces read).
    froze_own_region = (state.solve.region_source if state.solve is not None else None) == "frozen_object_mask"
    if canon and froze_own_region:
        region_cand = ws.find_producer(ArtifactRef(f"{target}_solve_region", ArtifactRole.QA))
        if region_cand:
            fp = os.path.join(
                ws.output_dir, f"{_published_base_for(f'{target}_solve_region', target, out_name, certified)}.png"
            )
            plan_pairs.append((region_cand, fp))
            qa_artifact_names.append(os.path.basename(fp))

    missing = [c for c, _ in plan_pairs if not (c and os.path.exists(c))]
    if missing:
        raise FileNotFoundError(f"deliverable candidate(s) missing before promotion: {missing}")
    return plan_pairs, variants, qa_artifact_names


def _capture_superseded(
    ws, *, target: str, out_name: str, certified: bool, cleans, cert_run_name: str, run_warnings: list[str]
) -> list[str]:
    """Task 7 (spec rev 6, §5/§8): the same-out_name uncertified generation this
    certified commit will clean up AFTERWARDS.

    NOTHING deletes here -- only the union of what post-commit cleanup will remove
    is computed, so a transaction failure leaves every uncertified file untouched
    (asserted by test_certified_transaction_failure_leaves_uncertified_set_
    untouched). Two independently-validated sources are captured and unioned
    (current takes precedence over the prior record): the CURRENT uncertified
    manifest's own inventory (if one validly exists), and the PRIOR certified
    manifest's `superseded` record (covers a crash between an earlier cleanup's
    manifest-removal and its member removal -- the "stale record" repair case). An
    uncertified manifest FILE that exists but fails validation freezes BOTH
    inventories (may belong to a damaged newer publication -- never guess)."""
    if not (certified and cleans is not None):
        return []
    # Task 7 review fix #1: the recorded union must include the identity's exact
    # expected EXTRAS names (recipe/qa/annotated), not only the manifest's member
    # inventory (spec §5 steps 1(a)+2) -- otherwise a crash between manifest-first
    # removal and extras removal strands them forever (a later repair run's
    # `prior_record` would never learn they existed).
    cur_man, cur_members = _read_valid_uncertified_manifest(ws.output_dir, target, out_name)
    sn_u = sidecar_names(target, out_name, False)
    unc_manifest_file_exists = os.path.exists(os.path.join(ws.output_dir, sn_u["run"]))
    prior_record: list[str] = []
    prior_cert_path = os.path.join(ws.output_dir, cert_run_name)
    if os.path.exists(prior_cert_path):
        try:
            with open(prior_cert_path, encoding="utf-8") as f:
                prior_record = [
                    m
                    for m in (json.load(f).get("superseded") or [])
                    if validate_member_basename(m, target, out_name) or validate_extra_basename(m, target, out_name)
                ]
        except (OSError, json.JSONDecodeError):
            prior_record = []
    if cur_man is not None:
        current_set = cur_members + [sn_u["recipe"], sn_u["qa"], annotated_name(target, out_name, False)]
        return current_set + [m for m in prior_record if m not in current_set]
    if not unc_manifest_file_exists:
        return prior_record  # legitimate interrupted-repair state
    # manifest present but invalid: freeze BOTH inventories (spec §5 step 6)
    w = (
        f"uncertified manifest {sn_u['run']} exists but cannot be validated; "
        "leaving all uncertified files in place (may belong to a damaged newer publication)"
    )
    run_warnings.append(w)
    print(w, file=sys.stderr)
    return []


def _transaction_targets(
    ws, plan_pairs, sidecars, *, target: str, out_name: str, certified: bool, run_warnings: list[str]
) -> list[str]:
    """Every path the publication transaction may write, back up, or restore.

    The new set (promoted rasters + the three sidecars) plus, for an UNCERTIFIED
    publication, the prior uncertified generation's own members: a member with no
    new candidate is backed up and thereby "removed", restored on rollback, and
    dropped on success -- the union rule, with no delete call anywhere."""
    txn_targets = [f for _, f in plan_pairs] + list(sidecars)

    if not certified:
        run_name = sidecar_names(target, out_name, certified)["run"]
        prior_man, prior_members = _read_valid_uncertified_manifest(ws.output_dir, target, out_name)
        if prior_man is None and os.path.exists(os.path.join(ws.output_dir, run_name)):
            # listing-for-reporting (spec §5): NAME the now-unaccounted files; never delete from a
            # listing. Scoped to exactly the member-shaped names of THIS identity that the invalid
            # manifest would have accounted for -- not other identities' uncertified files or extras.
            # Exclude names this very run's new set already accounts for (txn_targets, assembled
            # above) -- those are being freshly written/replaced, not stranded.
            txn_basenames = {os.path.basename(t) for t in txn_targets}
            stranded = sorted(
                n
                for n in os.listdir(ws.output_dir)
                if validate_member_basename(n, target, out_name) and n not in txn_basenames
            )
            w = (
                f"prior uncertified manifest {run_name} is invalid; replacing with the new set only. "
                f"Unaccounted uncertified files left in place: {', '.join(stranded) if stranded else '(none)'}"
            )
            run_warnings.append(w)
            print(w, file=sys.stderr)
        for m in prior_members:
            fp = os.path.join(ws.output_dir, m)
            if fp not in txn_targets:
                # backup (os.replace to .prev_) removes the file; a member with no new
                # candidate is thereby "removed", restored on rollback, and its backup
                # dropped on success -- exactly the union-rule semantics, no delete call.
                txn_targets.append(fp)

    return txn_targets


def _commit_publication(
    ws,
    *,
    txn_targets: list[str],
    plan_pairs: list[tuple[str, str]],
    variants: dict,
    outputs: list[dict],
    finalize,
    variant_for,
    plan,
    report,
    published: dict,
    plan_path: str,
    notes_path: str,
    notes_payload: dict,
    run_path: str,
    manifest_fields: dict,
) -> list[str]:
    """The publication transaction (B-10): promote the hero+fork deliverables, embed
    metadata, write the plan and notes sidecars, and commit with the run manifest.

    Atomic through the manifest write (docs/TENETS.md tenet 7 -- "a step or gate
    that silently changes, skips, or degrades is a defect"). Every pre-existing
    transaction-set file is backed up to `.prev_<uuid8>` before anything is
    overwritten; on ANY in-process failure (raster promotion, embed, plan/notes/
    manifest write) everything this run created is removed and every backup is
    restored, then the exception re-raises -- so a prior accepted run is left
    byte-identical. Backups are retained until the manifest write (the commit)
    succeeds and dropped only then -- never on failure (tenet 2, "source data is
    sacred": a hard-crash-era backup is the last surviving copy and is handled by
    the `_warn_prior_backups` warning, never auto-deleted here).

    Returns the post-commit backup-cleanup warnings (CHANGE_SAFETY rule 5 class 2:
    a leftover backup affects later runs, so it warns rather than failing)."""
    backup_cleanup_warnings: list[str] = []
    pre_existing = {f for f in txn_targets if os.path.exists(f)}
    backups: list[tuple[str, str]] = []  # (final_path, backup_path) for files that existed
    try:
        for final_path in pre_existing:  # back up prior versions (RETAIN through commit)
            bak = final_path + f".prev_{uuid.uuid4().hex[:8]}"
            os.replace(final_path, bak)
            backups.append((final_path, bak))
        for cand, final_path in plan_pairs:  # promote every deliverable raster (hero + forks)
            os.replace(cand, final_path)
        for base, ext_map in variants.items():  # register + finalize -> embeds, fills `outputs`
            for ext, fp in ext_map.items():
                # Register the promoted path under the ORIGINAL DELIVERABLE-role
                # ref, so later DELIVERABLE-role lookups (e.g. the documentation
                # annotate step below, which resolves the hero jpg by that exact
                # ref) keep finding it exactly as they did before this candidate
                # detour -- execute_ops only ever registered the CANDIDATE-role ref.
                ws.register(f"promoted:{base}", ArtifactRef(f"{base}.{ext}", ArtifactRole.DELIVERABLE), fp)
            finalize(ext_map, variant_for(base))
        write_plan_sidecar(  # relocated: was written early, before promotion
            plan_path,
            plan,
            report,
            published=published,
        )
        with open(notes_path, "w") as f:  # relocated: was written after the manifest -- SAME payload+kwargs
            json.dump(notes_payload, f, indent=2, default=str)
        manifest = run_manifest_to_dict(
            deliverables=outputs,  # ALL variants (hero + forks), not hero-only
            accepted_at=datetime.datetime.now().isoformat(timespec="seconds"),
            **manifest_fields,
        )
        write_run_manifest(run_path, manifest)  # THE COMMIT
    except BaseException as original_exc:
        # CHANGE_SAFETY.md rule 5, class 1: rollback failures must be REPORTED
        # while PRESERVING the original exception -- collected here, never
        # silently ignored (a concealed incomplete rollback is worse than a
        # loud one).
        rollback_failures: list[tuple[str, OSError]] = []
        for final_path in txn_targets:  # remove anything CREATED this run (no backup exists)
            if final_path not in pre_existing:
                try:
                    os.remove(final_path)
                except FileNotFoundError:
                    pass  # the failure happened before this file was ever created -- nothing to roll back
                except OSError as e:
                    rollback_failures.append((final_path, e))
        for final_path, bak in backups:  # restore every prior version
            try:
                os.replace(bak, final_path)
            except OSError as e:
                rollback_failures.append((final_path, e))
        if rollback_failures:
            print(
                "ROLLBACK INCOMPLETE after a publication failure -- the following "
                "path(s) could not be restored/removed and may be left "
                "inconsistent; any remaining .prev_ backups are left untouched:",
                file=sys.stderr,
            )
            for failed_path, e in rollback_failures:
                print(f"  - {failed_path}: {e}", file=sys.stderr)
            failed_list = ", ".join(p for p, _ in rollback_failures)
            raise RuntimeError(
                f"rollback incomplete after publication failure: {len(rollback_failures)} "
                f"path(s) could not be restored ({failed_list})"
            ) from original_exc
        raise
    for _f, bak in backups:  # success only: drop backups LAST, after the commit
        try:
            os.remove(bak)
        except OSError as e:
            # CHANGE_SAFETY.md rule 5, class 2: post-commit cleanup failure --
            # WARN (never silent): a leftover backup can affect later runs
            # (Task 6 Step 3b's orphan-warning path above handles it next time).
            msg = (
                f"Could not remove post-commit backup {bak}: {e}. The run itself "
                "succeeded; it is safe to delete this leftover file manually."
            )
            backup_cleanup_warnings.append(msg)
            print(msg, file=sys.stderr)
    return backup_cleanup_warnings


def _cleanup_superseded(
    ws, superseded_union: list[str], *, target: str, out_name: str, run_warnings: list[str]
) -> None:
    """Task 7 (spec rev 6, §5/§8): post-commit cleanup of the superseded
    uncertified generation.

    Called ONLY after the transaction above committed, so a transaction failure
    never reaches here. Order matters: the stale manifest is removed FIRST --
    "unpublish" it atomically so a crash mid-cleanup can never leave a manifest
    pointing at partially-deleted members -- then the rest of the union, each name
    dispatched to member vs extras handling BY THE VALIDATORS (review fix #2),
    never by which source list it came from: a name that reached the union via the
    prior-record path (an interrupted repair) must get the SAME recipe
    edit-preservation gate as one captured fresh this run; dispatching by
    provenance instead of shape is what let a pinned recipe named in a stale
    `superseded` record get deleted via the plain member path with no warning.
    Every removal is warn-and-continue (CHANGE_SAFETY rule 5 class 2: post-commit
    cleanup failures warn, never fail an already-succeeded run)."""

    sn_u = sidecar_names(target, out_name, False)
    unc_run_path = os.path.join(ws.output_dir, sn_u["run"])

    def _rm(path: str, what: str) -> None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as e:  # CHANGE_SAFETY rule 5 class 2: warn, never fail the run
            w = f"could not remove superseded {what} {os.path.basename(path)}: {e}"
            run_warnings.append(w)
            print(w, file=sys.stderr)

    _rm(unc_run_path, "manifest")  # manifest FIRST: unpublish atomically
    for m in superseded_union:
        if m == sn_u["run"]:
            continue  # already removed above
        p = os.path.join(ws.output_dir, m)
        if validate_extra_basename(m, target, out_name):
            if m == sn_u["recipe"]:
                if os.path.exists(p):
                    if R.recipe_is_unedited(p):
                        _rm(p, "recipe")
                    else:
                        w = (
                            f"superseded recipe {m} contains your_value pins and was kept; "
                            f"if the new {target}_recipe.json does not carry your pins, copy "
                            f"them over before deleting it"
                        )
                        run_warnings.append(w)
                        print(w, file=sys.stderr)
                continue
            _rm(p, "extra")
        elif validate_member_basename(m, target, out_name):
            _rm(p, "member")
        # else: not attributable to this identity by either grammar -- every
        # entry in superseded_union was validated at capture time, so this
        # should be unreachable; skip rather than delete on an unproven name.


def _catalog_common_name(side: dict) -> str | None:
    """The matched catalog entry's common name, for cosmetic metadata only.

    Comes from the offline local-catalog lookup this run already performed by WCS
    footprint overlap (`known_targets.match_target`) -- identity from where the
    telescope pointed, never from the OBJECT header string, so the target-agnostic
    law is untouched (§13). A `contradicted` hit still supplies its name: what was
    rejected there is the catalog's asserted CATEGORY, while its identity is kept
    and reported (see `_prior_outcome_note`). `spatially_impossible` supplies
    nothing -- that status means the record's own stored position is invalid, so
    nothing about it is trustworthy enough to print on a photo."""
    lp = side.get("local_prior") or {}
    if lp.get("status") != "hit":
        return None
    record = lp.get("record")
    return getattr(record, "common_name", None) or None


def _emit_annotated(
    ws,
    workdir,
    insp,
    header,
    params,
    decisions,
    cfg,
    tools,
    *,
    target: str,
    out_name: str,
    certified: bool,
    common_name: str | None,
) -> dict | None:
    """documentation profile (§8a): the labelled JPG -- title, scale bar, N/E, date.

    A terminal side effect, deliberately outside the publication transaction's
    production-artifact graph. Returns the deliverable entry, or None when this
    run published no hero jpg to annotate."""
    from .adapters import _annotate_impl as A
    from .metadata import instrument

    hero_jpg = ws.find_producer(ArtifactRef(f"{target}_{out_name}.jpg", ArtifactRole.DELIVERABLE))
    if not hero_jpg:
        return None
    m = insp["meta"]
    common = (common_name or "").strip()
    info = {
        "title": target + (f" — {common}" if common else ""),
        "subtitle": decisions.category.value.capitalize(),
        "lines": [
            f"{m.get('frames')}x{m.get('sub_exposure_s')}s = "
            f"{m.get('total_integration_min')} min  |  {m.get('filter')} filter",
            f"{(m.get('datetime') or '')[:10]}  |  {instrument(insp, cfg)}",
        ],
        "pixscale_arcsec": header.get("pixscale_arcsec"),
        "cd": header if header.get("has_wcs") else None,
    }
    ann = A.annotate(hero_jpg, os.path.join(workdir, annotated_name(target, out_name, certified)), info)
    embed(
        ann,
        insp,
        params,
        f"{out_name}-annotated",
        cfg,
        tools,
        certification="certified" if certified else "uncertified",
        common_name=common_name,
    )
    return {"variant": f"{out_name}-annotated", "jpg": ann}


def _emit_recipe(
    workdir, source_name, decisions, notes, engine, recipe, *, target: str, out_name: str, filt, certified: bool | None
) -> None:
    """Write this render's adjustment recipe (§12b) -- the user's round-trip file.

    Non-transactional: the recipe is not referenced by the committed run manifest,
    so a write failure costs a tuning file a plain re-render regenerates, never a
    manifest whose deliverables are inconsistent (CHANGE_SAFETY rule 5). An
    UNCERTIFIED hero emits under the uncertified namespace, seeding your_value
    pins from the certified recipe -- so a failed run touches nothing certified
    (Task 6, red-team finding 21) while still carrying forward any prior pins for
    the next replay attempt. `certified` is None when this run assembled no hero
    export at all, which keeps the certified-name emission unchanged."""
    recipe_kwargs: dict = {}
    if certified is False:
        recipe_kwargs["filename"] = sidecar_names(target, out_name, False)["recipe"]
        recipe_kwargs["seed_your_values_from"] = os.path.join(workdir, f"{target}_recipe.json")
    try:
        R.emit(
            workdir,
            target,
            source_name,
            filt,
            decisions,
            notes,
            engine,
            prior=recipe.doc,
            pinned=recipe.pinned,
            **recipe_kwargs,
        )
    except OSError:
        # best-effort, per the docstring above: never fail a committed render for
        # the round-trip tuning file.
        pass


class _Progress:
    """Per-stage progress for the GUI/CLI: "Step i of N", named by presentation.py.

    The step TOTAL is not known until the planner has decided what will run, so
    `replan` re-derives it from each planning state (`plan_stages`, a pure
    function) and `emit` reports the current key's position within it. A key that
    is not part of this run's estimate reports as step 0, which the GUI shows as a
    bare label."""

    def __init__(self, on_stage, *, has_wcs: bool, filter_kind, engine, out_name: str):
        self._on_stage = on_stage
        self._has_wcs = has_wcs
        self._filter_kind = filter_kind
        self._engine = engine
        self._out_name = out_name
        self._keys: list[str] = []

    def replan(self, state) -> None:
        dec = state.decisions
        prior_used = state.classification.source is CategorySource.CATALOG_PRIOR
        self._keys = plan_stages(
            {
                # LookupLocalTarget is always-on for any plate-solved frame (spec sec5.7,
                # decoupled from use_prior); FetchNetworkPrior additionally needs IRCUT +
                # use_prior. Either alone means a "prior" stage step will run.
                "prior": self._has_wcs,
                "trail": dec.trail_removal_enabled,
                "probe": (self._filter_kind is FilterKind.IRCUT and self._engine.e_gate and not prior_used),
                "solve_stretch": self._engine.solve_stretch,
                "forks": bool(dec.star_forks),
                "cluster": bool(dec.ght is not None and dec.category is Category.STARFIELD),
                "closeup": (
                    dec.category is Category.GALAXY
                    and dec.ght is not None
                    and dec.ght.closeup
                    and self._out_name == "final"
                ),
            }
        )

    def emit(self, key: str) -> None:
        if self._on_stage is None:
            return
        try:
            label, tool = present(StageKey(key))
        except (ValueError, KeyError):
            return
        idx = self._keys.index(key) + 1 if key in self._keys else 0
        self._on_stage(key, label, tool, idx, len(self._keys))

    def op_hook(self, stage) -> None:
        self.emit(_STAGE_KEYMAP.get(stage.value, stage.value))


def _report_extras(d: dict, *, decisions, color, pref_provenance: dict | None, recipe_provenance: dict) -> dict:
    """Add this run's provenance to a decisions dict (EXIF UserComment / report)."""
    # Public report compatibility: concentrated clusters are classified as
    # starfields plus a cluster halo treatment, and downstream report/recipe
    # code expects the explicit "cluster" marker.
    if decisions.category is Category.STARFIELD and decisions.ght is not None:
        d["cluster"] = True
    if pref_provenance is not None:
        d["_pref_provenance"] = pref_provenance
    if recipe_provenance:
        d["_recipe_provenance"] = recipe_provenance
    # Diagnosis 4: stamp the judged color path into EXIF provenance whenever a
    # PCC attempt happened (accepted or native fallback); the ordinary
    # policy-off native path stays unstamped ("no PCC" already says it).
    if color is not None and color.attempted:
        if color.accepted:
            # "qualified" is spelled out, never folded into a bare accept:
            # the embedded EXIF is the copy that travels with a shared photo
            d["_pcc_provenance"] = (
                f"accepted-qualified({color.reason})" if color.qualified else f"accepted({color.reason})"
            )
        else:
            d["_pcc_provenance"] = f"native-fallback({color.reason})"
    return d


def _variant_label(base: str, *, target: str, out_name: str, decisions, canon: bool) -> str:
    """The variant name embedded in an export's metadata, from its output base."""
    if base == f"{target}_stars-on":
        return "stars-on"
    if base == f"{target}_starless":
        return "starless"
    if base == f"{target}_star-reduced":
        return "star-reduced"
    if base == f"{target}_closeup":
        return "closeup"
    if base == f"{target}_{out_name}":
        # Phase 3A: the conservative hero IS the stars-on look, so label it as
        # such (the star-reduced render ships under its own _star-reduced name).
        if decisions.star_forks and canon and decisions.conservative_star_hero:
            return "stars-on"
        if decisions.star_forks:
            return "star-reduced" if canon else out_name
        return out_name
    return base


def run_target(
    target_dir: str,
    output_dir: str,
    cfg: dict,
    profile: str = "showcase",
    debug: bool = False,
    overrides: dict | None = None,
    keep_scratch: bool = False,
    catalog_prior: bool | None = None,
    out_name: str = "final",
    recipe_path: str | None = None,
    on_stage=None,
) -> dict:
    problems = validate_out_name(out_name)
    if problems:
        raise ValueError("; ".join(problems))

    from .paths import assert_output_disjoint_from_source

    assert_output_disjoint_from_source(target_dir, output_dir)

    run_warnings: list[str] = _warn_prior_backups(output_dir)

    tools = resolve_tools(cfg)
    engine_cfg = cfg["engine"]
    ccfg = engine_cfg.get("classify") or {}
    engine = engine_defaults_from_cfg(engine_cfg)

    # ---- source selection (latest capture; combine is planner-driven / opt-in) ----
    src = _select_source(target_dir, ccfg)
    raws, work_input, latest_index = src.sessions, src.path, src.index
    insp, header, target = src.inspection, src.header, src.target

    notes: dict = {}
    if len(raws) > 1 and not engine.combine_sessions:
        notes["multi_session"] = {
            "mode": "latest",
            "selected": os.path.basename(work_input),
            "available": len(raws),
        }

    px = header.get("pixscale_arcsec")
    if on_stage is not None:
        # Announced BEFORE the measurement so the label is on screen during the
        # work; the step total is unknown until planning seeds, and (0, 0) tells
        # the GUI to show the bare label without a step counter.
        _label, _tool = present(StageKey.ANALYZE)
        on_stage("analyze", _label, _tool, 0, 0)
    raw_m = measure_fits(work_input, pixscale_arcsec=px)
    frame_dims = (int(header.get("NAXIS1") or raw_m.dims[0]), int(header.get("NAXIS2") or raw_m.dims[1]))
    filter_kind = FilterKind.from_raw(insp["meta"].get("filter"))
    frames = insp["meta"].get("frames") or 0

    # initial (filter-based) category -- drives prefs scope, exactly like run.py's
    # first compute_params before any prior/e-gate refinement.
    init_cls = core_classify(filter_kind, frames, raw_m, engine.classify)

    # ---- preferences layer (ONE typed entry point -- no dict-override bridge) ----
    prefs_ov, pref_provenance = _resolve_preferences(output_dir, init_cls.category, engine, notes)

    # ---- recipe replay: validate FIRST (RecipeError before any work) ----
    recipe = _load_recipe_pins(recipe_path, notes)
    recipe_doc, recipe_pinned = recipe.doc, recipe.pinned
    pins_ov, r_prov = recipe.overrides, recipe.provenance
    pinned_solved = recipe.pinned_solved
    caller_ov = overrides_from_prefs_dict(overrides)

    use_prior = engine.catalog_prior if catalog_prior is None else catalog_prior

    request = PlanningRequest(
        target_label=target,
        out_name=out_name,
        session_count=len(raws),
        latest_session_index=latest_index,
        frames=frames,
        filter_kind=filter_kind,
        has_wcs=bool(header.get("has_wcs")),
        raw_measurements=raw_m,
        frame_dims=frame_dims,
        engine=engine,
        profile=profile,
        prefs=prefs_ov,
        pins=pins_ov,
        pinned_solved=pinned_solved,
        caller=caller_ov,
        use_prior=use_prior,
        debug=debug,
    )

    # ---- workspace + shared execution report ----
    ws = Workspace(output_dir)
    try:
        workdir = ws.output_dir
        # Diagnosis 7: point every tool wrapper at this run's diagnostic log. Set ONCE,
        # here, where the output dir is first known -- `tools` is already threaded to
        # every wrapper, so no call site needs a new parameter. `dataclasses.replace`
        # keeps ToolPaths frozen; a duck-typed fake in the tests simply has no such
        # field and reports the bounded summary alone.
        if dataclasses.is_dataclass(tools):
            tools = dataclasses.replace(tools, diagnostics_dir=workdir)
        ctx = ExecContext(workspace=ws, tools=tools, resolve={"__session_paths__": raws, "__target__": target})
        report = ExecutionReport()
        dbg = Debugger(os.path.join(workdir, f"{target}_debug"), tools, enabled=debug)

        # ---- progress ----
        progress = _Progress(
            on_stage, has_wcs=bool(header.get("has_wcs")), filter_kind=filter_kind, engine=engine, out_name=out_name
        )

        # ---- side-data captured during the loop for notes, QA, and reports ----
        side: dict = {}

        def _collect(entry) -> None:
            if entry is None:
                return
            kind, data = entry
            side[kind] = data

        # Everything `_dispatch_work` needs from this run, bound once: the shell
        # context, this run's source/measurements, and the two progress callbacks.
        work_deps = dict(
            ctx=ctx,
            cfg=cfg,
            tools=tools,
            ws=ws,
            dbg=dbg,
            report=report,
            source_path=work_input,
            header=header,
            raw_m=raw_m,
            emit=progress.emit,
            on_op_stage=progress.op_hook,
        )

        # ---- drive the machine ----
        complete = _drive_planner(
            request, work_deps=work_deps, collect=_collect, replan=progress.replan, emit=progress.emit
        )
        state = complete.state
        plan = complete.plan
        decisions = state.decisions

        # ---- candidate lifecycle: intercept every export before it happens, so no
        # render ever touches its real deliverable path directly ----
        hero_export, exec_plan, output_map = _to_candidate_plan(plan, target, out_name)

        # ---- execute the final plan (shared workspace + report) ----
        execute_plan(exec_plan, ctx, on_stage=progress.op_hook, report=report)
        assert_no_duplicate_work(report)

        # ---- metadata/report parameter dicts ----
        report_extras = dict(
            decisions=decisions,
            color=state.color,
            pref_provenance=pref_provenance if prefs_ov != Overrides() else None,
            recipe_provenance=r_prov,
        )
        params = _report_extras(decisions_to_params(decisions), **report_extras)

        profile_final = decisions.profile
        insp = {**insp, "classification": {**insp["classification"], "category": decisions.category.value}}
        filt = insp["meta"].get("filter")
        canon = out_name == "final"

        # If combining sessions failed, say which single session became the fallback.
        combine_failed = False
        for pn in state.notes:
            if pn.key == "combine_failed":
                combine_failed = True
                notes["combine_failed"] = f"{pn.detail}; used latest {os.path.basename(work_input)}"
        # Successful opt-in combine: the pixels are combined but the combine RE-MEASURE
        # (frame sum / re-classify) is not implemented yet, so frames/integration
        # metadata still reflect the latest single session. Never silent -- record
        # the caveat as provenance.
        if len(raws) > 1 and engine.combine_sessions and not combine_failed:
            notes["multi_session"] = {
                "mode": "combined",
                "provenance_caveat": "frame count/integration reflect the latest session; combine re-measure deferred",
                "sessions": len(raws),
            }

        total_min = insp["meta"].get("total_integration_min")
        _build_notes(
            notes,
            state,
            decisions,
            params,
            engine,
            engine_cfg,
            ccfg,
            raw_m,
            side,
            pinned_solved,
            init_cls,
            filt,
            total_min,
        )

        # ---- metadata embed + deliverable list ----
        outputs: list[dict] = []
        # The hero's honest final state, surfaced in the return dict (Phase 3C): stays
        # None only when this run assembles no recommended-name hero export (the
        # candidate lifecycle below sets it from evaluate_hero when one exists).
        hero_status: str | None = None
        pub = None
        sn = None

        # Cosmetic identity for the exported files, from the catalog match this run
        # already made by WCS footprint (never from the target label itself).
        common_name = _catalog_common_name(side)

        def finalize(d: dict, variant: str) -> None:
            certification = "certified" if pub is None or pub.certified else "uncertified"
            for f in (d.get("jpg"), d.get("png"), d.get("tif")):
                if f:
                    embed(f, insp, params, variant, cfg, tools, certification=certification, common_name=common_name)
            outputs.append({"variant": variant, **d})

        # ---- candidate lifecycle (Phase 3B bounded recovery): measure the executed
        # CANDIDATE hero; if it fails ONLY the pegged_chroma gate on a converged
        # solve, lower saturation through a bounded bracket and re-render (reusing the
        # upstream BGE/denoise/color artifacts), keeping the strongest candidate that
        # clears the gate; then promote the accepted hero. attempt 0 is byte-for-byte
        # the pre-3B path, so every passing target is unchanged by construction. ----
        # CHANGE_SAFETY.md rule 5, class 2: post-commit backup-cleanup failures WARN
        # (surfaced below on both stderr and this returned list) rather than failing
        # silently -- declared here so it's defined regardless of whether this run
        # ever reaches the transaction below.
        backup_cleanup_warnings: list[str] = []
        if hero_export is not None:
            gates_cfg = (cfg.get("qa") or {}).get("gates") or {}
            mask_cfg = (cfg.get("qa") or {}).get("mask")
            thresholds = gate_thresholds_from_cfg(gates_cfg)
            pixscale = header.get("pixscale_arcsec")
            tol_cfg = (gates_cfg.get("hero_contract_tolerance") or {}).get(decisions.category.value)
            ss = state.solve
            # The INPUT's intrinsic star quality (measured once on the linear master,
            # constant across recovery attempts) bounds what any stretch can deliver.
            input_fwhm = (side.get("input_stars") or {}).get("fwhm_px")
            # ... and its star COUNT is the source evidence the source-relative
            # star_preservation gate judges retention against (Diagnosis 2). The same
            # value is persisted in notes.input_quality, so a later `seestar qa`
            # replays the identical comparison.
            input_star_count = (side.get("input_stars") or {}).get("star_count")
            # Diagnosis 5: when the solve measured its probes on a frozen region, the
            # hero's CONTRACT metric must be measured on the SAME region -- comparing
            # a region-p90 solve against an object-mask p90 hero would manufacture
            # false drift. Which raster that is comes from the solve's own
            # provenance, never guessed: the frozen object mask normally, the
            # recovered structure support where the frame-filling recovery fired.
            # Other QA metrics keep their own conventions untouched.
            region_path = _solve_region_path(ws, target, ss)

            # Owner ruling (2026-08-14): the immediate candidate evaluation used to
            # pass faint=None while a later `seestar qa` judged persisted faint
            # evidence -- two surfaces, two verdicts, from one committed identity.
            # Measure the SAME evidence here, from this attempt's own candidate
            # starless and the structure-support raster, so the gate that decides
            # publication is the gate a replay reproduces.
            support_png = ws.find_producer(ArtifactRef(f"{target}_support", ArtifactRole.QA))
            support_recorded = state.structure_support is not None and state.structure_support.valid

            hero_eval = _HeroEvaluator(
                ws=ws,
                tools=tools,
                plan=plan,
                output_map=output_map,
                target=target,
                category=decisions.category,
                profile=profile_final,
                thresholds=thresholds,
                mask_cfg=mask_cfg,
                pixscale=pixscale,
                tol_cfg=tol_cfg,
                solve=ss,
                region_path=region_path,
                input_fwhm=input_fwhm,
                input_star_count=input_star_count,
                support_png=support_png,
                support_recorded=support_recorded,
                run_warnings=run_warnings,
            )
            rec = _run_recovery(
                hero_eval,
                hero_export,
                exec_plan,
                output_map,
                engine=engine,
                category=decisions.category,
                base_saturation=decisions.saturation,
                ctx=ctx,
                report=report,
                on_op_stage=progress.op_hook,
            )
            hero, attempt = rec.hero, rec.attempt

            hero_status = hero.outcome.value
            pub = decide_publication(hero.outcome, out_name)
            sn = sidecar_names(target, out_name, pub.certified)
            notes["hero_contract"] = _hero_contract_note(rec, thresholds, input_star_count)

            # ---- recipe replay change report (mode (a) == mode (b) with no pins): pure
            # in-memory `notes` mutation, like hero_contract/saturation above -- must run
            # BEFORE the transaction below so the notes it writes into {target}_notes.json
            # are the SAME notes the caller's returned result dict carries. Moved here
            # (was after the transaction, past the manifest commit) because it used to
            # diverge: the returned result had recipe_replay but the persisted, manifest-
            # referenced sidecar did not (owner-reproduced defect, corrective commit). ----
            if recipe_doc is not None:
                cw = R.context_warnings(recipe_doc, decisions.category.value, profile_final, filt)
                notes["recipe_replay"] = R.change_report(recipe_doc, decisions, notes, engine, recipe_pinned, cw)

            # ---- publication transaction (B-10): hero+fork deliverables + plan +
            # notes + manifest, atomic through the manifest commit (docs/TENETS.md
            # tenet 7 -- "a step or gate that silently changes, skips, or degrades is
            # a defect"). Every pre-existing transaction-set file is backed up to
            # `.prev_<uuid8>` before anything is overwritten; on ANY in-process
            # failure (raster promotion, embed, plan/notes/manifest write) everything
            # this run created is removed and every backup is restored, then the
            # exception re-raises -- so a prior accepted run is left byte-identical.
            # Backups are retained until the manifest write (the commit) succeeds and
            # dropped only then -- never on failure (tenet 2, "source data is
            # sacred": a hard-crash-era backup is the last surviving copy and is
            # handled by the Step 3b warning above, never auto-deleted here). ----
            plan_pairs, variants, qa_artifact_names = _assemble_publication_set(
                ws,
                plan,
                hero_export,
                output_map,
                rec.paths,
                state,
                target=target,
                out_name=out_name,
                certified=pub.certified,
                canon=canon,
            )

            plan_path = os.path.join(ws.output_dir, sn["plan"])
            notes_path = os.path.join(ws.output_dir, sn["notes"])
            run_path = os.path.join(ws.output_dir, sn["run"])
            txn_targets = [f for _, f in plan_pairs] + [plan_path, notes_path, run_path]

            if not pub.certified:
                prior_man, prior_members = _read_valid_uncertified_manifest(ws.output_dir, target, out_name)
                if prior_man is None and os.path.exists(os.path.join(ws.output_dir, sn["run"])):
                    # listing-for-reporting (spec §5): NAME the now-unaccounted files; never delete from a
                    # listing. Scoped to exactly the member-shaped names of THIS identity that the invalid
                    # manifest would have accounted for -- not other identities' uncertified files or extras.
                    # Exclude names this very run's new set already accounts for (txn_targets, assembled
                    # above) -- those are being freshly written/replaced, not stranded.
                    txn_basenames = {os.path.basename(t) for t in txn_targets}
                    stranded = sorted(
                        n
                        for n in os.listdir(ws.output_dir)
                        if validate_member_basename(n, target, out_name) and n not in txn_basenames
                    )
                    w = (
                        f"prior uncertified manifest {sn['run']} is invalid; replacing with the new set only. "
                        f"Unaccounted uncertified files left in place: {', '.join(stranded) if stranded else '(none)'}"
                    )
                    run_warnings.append(w)
                    print(w, file=sys.stderr)
                for m in prior_members:
                    fp = os.path.join(ws.output_dir, m)
                    if fp not in txn_targets:
                        # backup (os.replace to .prev_) removes the file; a member with no new
                        # candidate is thereby "removed", restored on rollback, and its backup
                        # dropped on success -- exactly the union-rule semantics, no delete call.
                        txn_targets.append(fp)

            plan_path = os.path.join(ws.output_dir, sn["plan"])
            notes_path = os.path.join(ws.output_dir, sn["notes"])
            run_path = os.path.join(ws.output_dir, sn["run"])
            txn_targets = _transaction_targets(
                ws,
                plan_pairs,
                (plan_path, notes_path, run_path),
                target=target,
                out_name=out_name,
                certified=pub.certified,
                run_warnings=run_warnings,
            )

            superseded_union = _capture_superseded(
                ws,
                target=target,
                out_name=out_name,
                certified=pub.certified,
                cleans=pub.cleans,
                cert_run_name=sn["run"],
                run_warnings=run_warnings,
            )

            backup_cleanup_warnings = _commit_publication(
                ws,
                txn_targets=txn_targets,
                plan_pairs=plan_pairs,
                variants=variants,
                outputs=outputs,
                finalize=finalize,
                variant_for=lambda base: _variant_label(
                    base, target=target, out_name=out_name, decisions=decisions, canon=canon
                ),
                plan=plan,
                report=report,
                published={
                    "certified": pub.certified,
                    "outputs": {
                        orig_base: _published_base_for(orig_base, target, out_name, pub.certified)
                        for orig_base in variants
                    },
                },
                plan_path=plan_path,
                notes_path=notes_path,
                notes_payload={
                    "target": target,
                    "profile": profile_final,
                    "category": decisions.category.value,
                    "notes": notes,
                },
                run_path=run_path,
                manifest_fields={
                    "attempt": attempt,
                    "source_identity": None,
                    "plan_sidecar": sn["plan"],
                    "notes_sidecar": sn["notes"],
                    "outcome": hero.outcome.value,
                    "qa_artifacts": qa_artifact_names,
                    "superseded": superseded_union,
                },
            )

            if pub.certified and superseded_union:
                _cleanup_superseded(ws, superseded_union, target=target, out_name=out_name, run_warnings=run_warnings)

        # ---- documentation: annotated JPG ----
        if decisions.annotate:
            annotated = _emit_annotated(
                ws,
                workdir,
                insp,
                header,
                params,
                decisions,
                cfg,
                tools,
                target=target,
                out_name=out_name,
                certified=True if pub is None else pub.certified,
                common_name=common_name,
            )
            if annotated is not None:
                outputs.append(annotated)

        progress.emit("export")

        # ---- recipe emission: the user round-trip artifact, outside the transaction ----
        _emit_recipe(
            workdir,
            os.path.basename(work_input),
            decisions,
            notes,
            engine,
            recipe,
            target=target,
            out_name=out_name,
            filt=filt,
            certified=None if pub is None else pub.certified,
        )

        # ---- report-only params (CLI printout / GUI / `seestar qa`) ----
        # `decisions_to_report_dict` is the ONE consumer-facing name for this purpose;
        # `params` above (built via `decisions_to_params` directly) is for `metadata.embed`
        # only. Metadata and annotation are terminal side effects and stay outside
        # the planner's reusable production-artifact graph.
        report_params = _report_extras(decisions_to_report_dict(decisions), **report_extras)

        return {
            "target": target,
            "profile": profile_final,
            "status": hero_status,
            "params": report_params,
            "inspection": insp,
            "outputs": outputs,
            "notes": notes,
            "debug_dir": dbg.dir if debug else None,
            "scratch_dir": ws.scratch_dir if (keep_scratch or debug) else None,
            "warnings": run_warnings + backup_cleanup_warnings,
            "published": None
            if pub is None
            else {
                "hero_base": published_base(target, out_name, pub.certified),
                "sidecars": {"plan": sn["plan"], "notes": sn["notes"], "run": sn["run"]},
                "certified": pub.certified,
            },
        }
    finally:
        # M3: the run-scoped scratch dir holds full-size working FITS, and it is
        # OURS -- disposable files this run created and can reproduce (tenet 2
        # allows removing only those). In a `finally` so a failed render cleans up
        # too: without it, the retry loop a struggling user hits (fail, retry,
        # fail) orphaned hundreds of MB per attempt. Ordered here, at the outermost
        # level, it necessarily runs AFTER the publication transaction's own
        # rollback (inside `_commit_publication`), so it can never remove something
        # a rollback still needs. `keep_scratch`/`debug` opt out, as on the success
        # path -- the working files are most useful precisely when a render failed.
        if not keep_scratch and not debug:
            try:
                ws.cleanup()
            except OSError as e:
                # Incidental cleanup (CHANGE_SAFETY rule 5, last bullet): scratch
                # has no effect on later runs, so a removal failure WARNS. It must
                # never mask the render failure this `finally` may be unwinding,
                # and never fail a render whose deliverables are already committed.
                print(f"Could not remove this run's working files ({ws.scratch_dir}): {e}", file=sys.stderr)


def _prior_outcome_note(lp: dict | None, pri: dict | None, state) -> dict:
    """Task 6 (spec sec5.6): summarize the LookupLocalTarget/FetchNetworkPrior
    outcome as ONE outcome tag -- `local-hit` / `network` / `missing` / `ambiguous` /
    `contradicted` / `local_catalog_unavailable` -- plus what it influenced. v1 has
    only two decision channels: identity and/or category (spec sec5.3/5.4).

    Footprint/morphology ride along here for the REPORT ONLY: sec5.4 keeps them off
    the core boundary entirely (`PriorObservation` carries no such field), so they
    can never appear in `influenced` and never move a decision -- they are recorded
    purely so a human reading the report can see what the catalog said.
    """
    category_decided = state.classification.source is CategorySource.CATALOG_PRIOR
    influenced: list[str] = []
    entry: dict = {}

    lp_status = (lp or {}).get("status")
    contradicted = False
    if lp_status == "hit":
        record = lp["record"]
        # The contradiction check: compare the catalog's ASSERTED category against
        # the FINAL authoritative classification `state` carries at this point (this
        # function is called from `_build_notes` near the end of run_target, once
        # planning has settled -- never at LookupLocalTarget-dispatch time, which
        # only ever sees the round-1 provisional classification). If the pipeline's
        # final category differs from what this local hit asserted, the catalog's
        # category was REJECTED -- identity is still kept, but Decisions never
        # applied it (they are already whatever the catalog-absent path would have
        # produced -- no decision is changed here, only reported).
        # `Category.UNKNOWN` asserts NO category (the catalog recognized the target
        # but made no type claim), so it can never be "contradicted" -- there is
        # nothing to disagree with the final classification. Such a hit is
        # identity-only: identity is recorded, no category is asserted or applied,
        # and no warning is raised.
        identity_only = record.category is Category.UNKNOWN
        contradicted = (not identity_only) and (record.category != state.classification.category)
        influenced.append("identity")
        entry.update(
            entry_id=record.entry_id,
            canonical_id=record.canonical_id,
            common_name=record.common_name,
            aliases=list(record.aliases),
            catalog_category=record.category.value,
            conflict=lp.get("conflict"),
            associated=list(record.associated),
            # report-only -- never a decision input (sec5.4).
            footprint_report_only={
                "extent_major_arcmin": record.extent_major_arcmin,
                "extent_minor_arcmin": record.extent_minor_arcmin,
                "search_footprints": list(record.search_footprints),
            },
            morphology_report_only=list(record.morphology_traits),
        )
        if identity_only:
            entry["local_disposition"] = "identity_only"
        elif contradicted:
            entry["local_disposition"] = "contradicted"
            entry["warning"] = (
                f"catalog identity {record.entry_id} ({record.category.value}) disagrees with the final "
                f"authoritative category ({state.classification.category.value}); the catalog category was "
                "NOT applied"
            )
        else:
            entry["local_disposition"] = "agreed"

    if pri is not None and pri.get("available"):
        # a network round only ever follows a local miss/ambiguous/undecided IRCUT
        # frame with use_prior on (planning.py's needs_network) -- a present, available
        # `pri` means the network is what ultimately spoke. This is compatible with a
        # local HIT that was itself contradicted (e.g. an IRCUT frame whose local hit
        # asserts nebula -- not authoritative for IRCUT -- and the network round then
        # ran and settled a different category): `outcome` still reports "network",
        # but the rejected local assertion stays visible via `local_disposition` +
        # `warning`, set above, so it is never silently dropped (Tenet 7).
        outcome = "network"
        if "identity" not in influenced:
            influenced.append("identity")
    elif lp_status == "hit" and contradicted:
        # Reachable now (unlike the old pre-measurement flag this replaces): e.g. an
        # LP frame whose local hit asserts a non-nebula category -- the LP filter law
        # is unconditional, so the final category is always nebula regardless, and no
        # network round ever runs for LP. Or an IRCUT frame whose local hit asserts
        # nebula (not authoritative) with no network round available/needed.
        outcome = "contradicted"
    elif lp_status == "hit":
        outcome = "local-hit"
    elif lp_status == "ambiguous":
        outcome = "ambiguous"
    elif lp_status == "local_catalog_unavailable":
        outcome = "local_catalog_unavailable"
    elif lp_status == "no_wcs":
        # final-fixwave Fix 3: `has_wcs` was True (the header claims a WCS) but
        # `prior.field_center()` still returned None -- a malformed/failed plate
        # solve, not a genuine spatial miss (catalog present, position simply
        # unusable). Distinguish it from a plain "nothing matched" miss so the note
        # is legible; category still degrades correctly either way (Tenet 7).
        outcome = "missing"
        entry["reason"] = "wcs_solve_failed"
        entry["detail"] = (
            "WCS present but field_center() could not be computed (malformed/failed "
            "plate solve); local catalog lookup was skipped, not a spatial miss"
        )
    elif lp_status == "no_fov":
        # P1 bug fix: field_center() solved, but no usable pixel scale could be
        # derived from either the header's `pixscale_arcsec` convenience field or
        # the full WCS (no CD/PC/CDELT convention present at all) -- distinguish
        # this from a plain spatial miss/ambiguous outcome so the note is legible.
        outcome = "missing"
        entry["reason"] = "no_fov"
        entry["detail"] = (
            "WCS present and field_center() solved, but no usable pixel scale could be "
            "derived (no pixscale_arcsec, and the full WCS has no CD/PC/CDELT convention); "
            "local catalog lookup was skipped, not a spatial miss"
        )
    else:  # miss / spatially_impossible / None
        outcome = "missing"

    if category_decided and "category" not in influenced:
        influenced.append("category")

    entry["outcome"] = outcome
    entry["influenced"] = influenced
    return entry


def _build_notes(
    notes, state, decisions, params, engine, engine_cfg, ccfg, raw_m, side, pinned_solved, init_cls, filt, total_min
):
    """Build the public notes sidecar from planner state and measured side data.

    `seestar qa`, recipe emission, and CLI/GUI reporting all consume this shape,
    so new fields should be additive and existing keys should remain stable.
    """
    # border crop (Diagnosis 1): the measured per-edge decision, its evidence,
    # and a human summary. All-zero widths must read "no border crop" publicly.
    cd = state.crop_decision
    if cd is not None:
        zero = (cd.left, cd.top, cd.right, cd.bottom) == (0, 0, 0, 0)
        if zero:
            summary = "no border crop (all edges kept)"
        else:
            summary = f"border crop: left={cd.left}px top={cd.top}px right={cd.right}px bottom={cd.bottom}px"
        notes["crop"] = {
            "applied_px": {"left": cd.left, "top": cd.top, "right": cd.right, "bottom": cd.bottom},
            "reason": cd.reason,
            "forced_symmetric_override": cd.forced,
            "summary": summary,
        }
        if side.get("border_scan"):
            notes["crop"]["measured"] = side["border_scan"]
        if cd.warning is not None:
            notes["crop"]["warning"] = cd.warning
    # blackpoint
    bp_factor = engine.blackpoint_factor * decisions.blackpoint_scale
    raw_bg = raw_m.background
    lin_bg = state.linear_background
    bp_pinned = "blackpoint" in pinned_solved
    if lin_bg is not None:
        bp_on_linear = params["blackpoint"] if bp_pinned else round(bp_factor * lin_bg, 6)
        notes["blackpoint"] = {
            "factor": bp_factor,
            "raw_background_median": raw_bg,
            "linear_background_median": lin_bg,
            "raw_minus_linear_median": round(raw_bg - lin_bg, 6),
            "blackpoint_on_linear": bp_on_linear,
            "blackpoint_on_raw_would_have_been": round(bp_factor * raw_bg, 6),
            "pinned_via_recipe": bp_pinned,
        }
        sp = state.solve
        if sp is not None and sp.bp_iterations > 0:
            sky_ceiling = engine.stretch_targets.for_category(decisions.category).sky_ceiling
            notes["blackpoint"]["solved_for_sky"] = {
                "bp": sp.blackpoint,
                "sky": round(sp.sky, 1),
                "sky_ceiling": sky_ceiling,
                "bp_iterations": sp.bp_iterations,
            }

    # input quality. `status` records whether the INPUT_STARS measurement itself
    # succeeded -- this persisted evidence is what the source-relative
    # star_preservation gate (run QA AND later `seestar qa`) compares against,
    # so its availability must be explicit, never inferred from key absence.
    metrics = side.get("input_stars", {}) or {}
    lin_stats = side.get("linear_stats") or {}
    notes["input_quality"] = {
        **metrics,
        "status": "measured" if metrics.get("star_count") is not None else "unavailable",
        "pixscale_arcsec": raw_m.pixscale_arcsec,
        "bgnoise": lin_stats.get("bgnoise"),
        "total_integration_min": total_min,
    }

    # stretch solve
    solver_cfg = (engine_cfg.get("stretch") or {}).get("solver")
    if "mtf_mid" in pinned_solved:
        notes["stretch_solve"] = {"skipped": "mtf_mid pinned via recipe", "mtf_mid": params["mtf_mid"]}
    elif state.solve is not None:
        sp = state.solve
        notes["stretch_solve"] = {
            "metric": sp.metric.value,
            "target": sp.target_level,
            "level_base": sp.level_base,
            "level_scale": sp.level_scale,
            "achieved": round(sp.achieved, 1),
            "sky": round(sp.sky, 1),
            "solved_mtf": sp.mtf_mid,
            "ght_in_probe": sp.ght_in_probe,
            "asinh_d": sp.asinh_d,
            # what the solve measured its body metric OVER (the solve region) --
            # deliberately named apart from notes["subject_support"], which is
            # what the SOURCE contained (owner ruling, 2026-08-14)
            "region_source": sp.region_source,
            "region_area_frac": sp.region_area_frac,
            "nonmonotonic": sp.nonmonotonic,
            "probes": probes_to_list(sp.probes),
            "pinned": {
                "mtf": sp.mtf_pinned,
                "mtf_converged": sp.converged,
                "bp": sp.bp_pinned,
                "bounds": {
                    "lo": engine.solver.lo,
                    "hi": engine.solver.hi,
                    "max_iter": engine.solver.max_iter,
                    "tol": engine.solver.tol,
                    **(solver_cfg or {}),
                    "bp_hi_factor": 0.999,
                },
            },
        }

    # structure support (Diagnoses 5+3): what the SOURCE contained -- the
    # faint-structure gate's reference and the two-point solve's sky complement.
    # Recorded whether valid or not. The solve REGION is recorded separately
    # below; the two were one field until the M81/M100 under-lift proved they
    # answer different questions (owner ruling, 2026-08-14).
    if "subject_support" in side and isinstance(side["subject_support"], dict):
        ss_info = dict(side["subject_support"])
        ss_info["dims"] = list(ss_info.get("dims") or [])
        notes["subject_support"] = ss_info
    for pn in state.notes:
        if pn.key in ("subject_support_failed", "subject_support_invalid"):
            notes.setdefault("subject_support", {})["warning"] = f"{pn.key}: {pn.detail}"

    # solve region: where the level the solver drove was measured. A failure to
    # freeze is recorded as a warning, never left silent -- the solve then ran
    # its named global fallback (Tenet 7: a step that silently degrades is a
    # defect).
    if state.solve_region is not None:
        notes["solve_region"] = {
            "source": state.solve_region.source,
            "area_frac": state.solve_region.area_frac,
            "dims": list(state.solve_region.dims),
            "valid": state.solve_region.valid,
            "detail": state.solve_region.detail,
            "artifact": solve_region_artifact_name(
                state.solve.region_source if state.solve is not None else None, state.request.target_label
            ),
        }
    for pn in state.notes:
        if pn.key == "solve_region_unavailable":
            notes.setdefault("solve_region", {})["warning"] = f"{pn.key}: {pn.detail}"

    # color path (Diagnosis 4): every surface (this notes sidecar, the plan
    # sidecar's plan.color, EXIF _pcc_provenance, QA's run_notes copy) must agree
    # on which color path shipped and why.
    c = state.color
    if c is not None:
        color_note = {
            "attempted": c.attempted,
            "accepted": c.accepted,
            "fallback": c.fallback,
            "reason": c.reason,
            "qualified": c.qualified,
        }
        if c.evidence is not None:
            color_note["evidence"] = {
                "succeeded": c.evidence.succeeded,
                "imprecise": c.evidence.imprecise,
                "few_stars_warning": c.evidence.few_stars_warning,
                "stars_used": c.evidence.stars_used,
                "coefficients": list(c.evidence.coefficients) if c.evidence.coefficients else None,
                "detail": c.evidence.detail,
            }
        # one stable novice-facing explanation per outcome (plan Task 5 item 5)
        # "unreadable report" is kept DISTINCT from "untrustworthy solution": the
        # first means the app could not read the tool's own report at all, which
        # on a healthy tool implies a parser/tool drift and must be loud rather
        # than quietly recorded as an ordinary quality rejection.
        unreadable = c.reason in ("no_evidence", "not_confirmed", "stars_unreported", "coefficients_unreported")
        # a LIST, because two independent warnings can apply at once (an unreadable
        # report AND an unhonorable recipe pin) -- a single key would silently drop
        # one of them, which is the exact silent-gate failure this task guards
        warnings: list[str] = []
        if c.accepted and c.qualified:
            # accepted, but the tool hedged -- say so plainly on the public
            # surface AND raise a warning, so this can never read as a clean pass
            color_note["summary"] = (
                f"star colors calibrated against the catalog ({c.evidence.stars_used} stars), "
                "with the tool reporting lower confidence in the fit"
            )
            warnings.append(
                "the colour-calibration tool flagged its own solution as imprecise (it suggests a "
                "residual background gradient); the calibration was applied because every other "
                "measure was within bounds, but this photo's colour is less certain than usual"
            )
        elif c.accepted:
            color_note["summary"] = f"star colors calibrated against the catalog ({c.evidence.stars_used} stars)"
        elif c.fallback and ("catalog" in c.reason or "no_wcs" in c.reason):
            color_note["summary"] = (
                "color calibration needs an internet connection to a star catalog and a "
                "plate-solved photo; it was unavailable, so the photo keeps its native color"
            )
            if "catalog" in c.reason:
                # Diagnosis 7 item 4: the catalog class is the one PCC failure a
                # user can usually FIX, and it is indistinguishable from a broken
                # tool without being told so. Stable guidance, in the warnings
                # list the CLI and GUI both surface -- not buried in the notes
                # summary alone.
                warnings.append(
                    "the colour-calibration catalog could not be reached; check this computer's "
                    "internet connection and run this photo again to calibrate its star colours"
                )
        elif c.fallback and unreadable:
            color_note["summary"] = (
                "the color-calibration result could not be read, so the photo keeps its native color"
            )
            warnings.append(
                f"could not read the color-calibration tool's own report ({c.reason}); "
                "native color shipped. If this repeats on every photo, the tool version "
                "or its output format may have changed"
            )
        elif c.fallback:
            color_note["summary"] = (
                f"the color-calibration attempt was not trustworthy ({c.reason}); the photo keeps its native color"
            )
        else:
            color_note["summary"] = "native color by policy (no calibration attempted)"
        for pn in state.notes:  # full tool text stays local; EXIF gets only the class
            if pn.key == "pcc_unavailable_detail":
                color_note["unavailable_detail"] = pn.detail
        if c.fallback and state.request.pins.pcc is True:
            # the silent-gate lesson: a recipe pin the run could not honor warns loudly
            warnings.append("recipe pinned pcc=true, but the attempt was rejected/unavailable; native color shipped")
        if warnings:
            color_note["warnings"] = warnings
        notes["color"] = color_note

    # trail removal
    if "trail" in side and isinstance(side["trail"], dict):
        notes["trail_removal"] = side["trail"]

    # e-gate
    if "e" in side:
        notes["e_gate"] = {
            "E": round(side["e"]["E"], 4),
            "category": decisions.category.value,
            "threshold": ccfg.get("extended_signal_threshold", 0.03),
        }

    # catalog prior
    if "prior" in side:
        from . import prior as _prior

        pri = side["prior"]
        pnote = _prior.reconcile(filt, init_cls.category.value, pri)[1]
        notes["catalog_prior"] = {
            "available": pri.get("available"),
            "source": pri.get("source"),
            "field_center": pri.get("field_center"),
            "object": pri.get("object"),
            "otype": pri.get("otype"),
            "otype_label": pri.get("otype_label"),
            "catalog_category": pri.get("catalog_category"),
            "angular_size_arcmin": pri.get("angular_size_arcmin"),
            "fov_fraction": pri.get("fov_fraction"),
            **pnote,
        }

    # local catalog / network prior outcome + no-WCS advisory (Task 6, spec sec5.6/5.7)
    lp = side.get("local_prior")
    pri_net = side.get("prior")
    if lp is not None or pri_net is not None:
        notes["local_prior"] = _prior_outcome_note(lp, pri_net, state)
    elif not state.request.has_wcs:
        # LookupLocalTarget is never even dispatched without a WCS (planning.py's
        # start_planning gates it on has_wcs) -- but sec5.7 still requires an honest
        # record rather than a silent skip (Tenet 7): any OBJECT-name identity on a
        # non-plate-solved frame is name-only advisory and supplies no category.
        notes["local_prior"] = {
            "outcome": "missing",
            "advisory": "no_wcs",
            "detail": (
                f"no WCS on this frame; OBJECT name {state.request.target_label!r} is "
                "name-only advisory and cannot supply category"
            ),
            "influenced": [],
        }

    # cluster
    if "concentration" in side and isinstance(side["concentration"], dict):
        conc = side["concentration"]
        cmin = engine.cluster.concentration_min
        notes["cluster"] = {
            **conc,
            "min": cmin,
            "is_concentrated_cluster": conc.get("concentration", 0) >= cmin,
        }

    # closeup
    if "subject_box" in side:
        box = side["subject_box"].get("box")
        if box:
            notes["closeup"] = {"box_xywh": list(box), "from": "starless subject bbox", "crop_source": "stars-on"}
        else:
            notes["closeup"] = {"skipped": "subject not small enough or unmeasurable"}
