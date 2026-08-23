"""Linear Plan executor.

This shell module walks `Plan.steps` in order, resolves input artifacts through
the Workspace, calls the adapter registered for each operation type, and records
what happened. It does not decide which operations should run or which parameter
values to use; those choices are already encoded in the Plan.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from seestar.adapters import base as adapters_base
from seestar.adapters.base import AdapterError, adapter_for
from seestar.core.plan_checks import validate_plan
from seestar.core.types import (
    BorderObservation,
    ConcentrationObservation,
    EValueObservation,
    InputStarsObservation,
    LinearStatsObservation,
    Measure,
    MeasureKind,
    Observation,
    Plan,
    ProbeMetricObservation,
    RemoveTrail,
    SolveRegionObservation,
    SubjectBoxObservation,
    SubjectSupportObservation,
    TrailObservation,
)


@dataclass(frozen=True)
class OpRecord:
    """One operation's execution outcome: runtime facts, not core state."""

    op_key: str  # repr(op) — the operation identity
    stage: str  # StageKey.value
    adapter: str
    status: str  # "ok" | "reused" | "failed"
    duration_s: float
    outputs: tuple[tuple[str, str, int], ...]  # (ref.name, path, size_bytes)
    error: str | None = None


@dataclass
class ExecutionReport:
    """Mutable accumulator across one or more `execute_ops` calls.

    Deliberately not frozen: it is a log of what happened, not a value the core
    reasons about.
    """

    records: list[OpRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def executed_counts(self) -> dict[str, int]:
        """op_key -> number of ACTUAL executions (status ok/failed; reused excluded)."""
        counts: dict[str, int] = {}
        for r in self.records:
            if r.status in ("ok", "failed"):
                counts[r.op_key] = counts.get(r.op_key, 0) + 1
        return counts

    def failed(self) -> list[OpRecord]:
        return [r for r in self.records if r.status == "failed"]


class ExecutionError(RuntimeError):
    """An operation failed during execution after planning completed."""


def execute_ops(ops, ctx, report: ExecutionReport | None = None, on_stage=None) -> ExecutionReport:
    """Execute `ops` in order against `ctx`, recording into (or creating) `report`.

    For each op:
      1. Call `on_stage(op.stage)` if given — announced as the op STARTS, so a
         live progress display shows each stage during its work (and names the
         stage that was running if it fails), never only after the fact.
      2. Resolve every input ref via `ctx.workspace.find_producer` (any
         producing op_key — the ref is unique per plan; validate_plan
         guarantees a producer) into `ctx.resolve`. Missing -> ExecutionError
         before the adapter is ever called.
      3. Reuse: if every output ref is already registered under THIS op's
         own op_key (repr(op)) and its file exists, record "reused" and skip
         the adapter entirely — a fully-reused op never needs one routed.
      4. Otherwise look up the adapter (a routing miss is a "failed" record
         + ExecutionError, same contract as a tool failure), call
         `adapter.execute(op, ctx)`, register every returned (ref, path)
         under op_key, and record "ok" with sizes. An `AdapterError` records
         "failed" and is re-raised as `ExecutionError`.
    """
    if report is None:
        report = ExecutionReport()

    for op in ops:
        if on_stage is not None:
            on_stage(op.stage)
        op_key = repr(op)

        for ref in op.inputs:
            path = ctx.workspace.find_producer(ref)
            if path is None:
                raise ExecutionError(f"{op_key}: no producer for input '{ref.name}' ({ref.role.value})")
            ctx.resolve[ref] = path

        existing = {ref: ctx.workspace.lookup(op_key, ref) for ref in op.outputs}
        if all(path is not None for path in existing.values()):
            routed = adapters_base.REGISTRY.get(type(op))  # non-raising: reuse needs no adapter
            report.records.append(
                OpRecord(
                    op_key=op_key,
                    stage=op.stage.value,
                    adapter=routed.name if routed is not None else "<unrouted>",
                    status="reused",
                    duration_s=0.0,
                    outputs=tuple((ref.name, path, _size(path)) for ref, path in existing.items()),
                )
            )
            continue

        try:
            adapter = adapter_for(op)
        except AdapterError as exc:
            report.records.append(
                OpRecord(
                    op_key=op_key,
                    stage=op.stage.value,
                    adapter="<unrouted>",
                    status="failed",
                    duration_s=0.0,
                    outputs=(),
                    error=str(exc),
                )
            )
            # C2 (final review): key the ExecutionError message by the op's TYPE
            # NAME, not the full `repr(op)` op_key -- a real op's repr runs ~195
            # chars, which starved downstream truncation (orchestrate.py's `_do`)
            # of room for the actual exception text (e.g. an ExternalToolTimeout's
            # actionable guidance). The OpRecord above still carries the full
            # op_key/repr for the journal.
            raise ExecutionError(f"{type(op).__name__}: {_staged(exc, op)}") from exc

        start = time.monotonic()
        try:
            written = adapter.execute(op, ctx)
        except AdapterError as exc:
            duration = time.monotonic() - start
            report.records.append(
                OpRecord(
                    op_key=op_key,
                    stage=op.stage.value,
                    adapter=adapter.name,
                    status="failed",
                    duration_s=duration,
                    outputs=(),
                    error=str(exc),
                )
            )
            raise ExecutionError(f"{type(op).__name__}: {_staged(exc, op)}") from exc
        duration = time.monotonic() - start

        for ref, path in written.items():
            ctx.workspace.register(op_key, ref, path)

        report.records.append(
            OpRecord(
                op_key=op_key,
                stage=op.stage.value,
                adapter=adapter.name,
                status="ok",
                duration_s=duration,
                outputs=tuple((ref.name, path, _size(path)) for ref, path in written.items()),
            )
        )

    return report


def execute_plan(plan: Plan, ctx, on_stage=None, report: ExecutionReport | None = None) -> ExecutionReport:
    """Validate `plan`, then execute its steps in order.

    `report` lets the caller thread ONE accumulator across planning-time RunOps and
    the final plan execution, so `assert_no_duplicate_work` sees every op (planning
    ops re-seen here register as `reused`, never re-executed)."""
    errors = validate_plan(plan)
    if errors:
        raise ExecutionError("invalid plan:\n" + "\n".join(errors))
    return execute_ops(plan.steps, ctx, report=report, on_stage=on_stage)


# ---------------------------------------------------------------------------
# Measurement dispatch: the shell side of the planner's Measure
# work items. Every exception is the ORCHESTRATOR's to catch and convert to a
# WorkFailed/WorkUnavailable -- this function may raise freely.
# ---------------------------------------------------------------------------


def _staged(exc: Exception, op) -> Exception:
    """Diagnosis 7: fill a typed tool failure's STAGE in at the one boundary that
    knows which operation was running, using the same novice label the progress
    UI shows -- so "GraXpert failed" becomes "GraXpert failed while removing the
    light-pollution gradient". Any other exception passes through untouched."""
    from .adapters._external_process import ExternalToolFailure
    from .presentation import present

    if isinstance(exc, ExternalToolFailure) and exc.stage is None:
        return exc.with_stage(present(op.stage)[0].lower())
    return exc


_SKY_OUTSIDE_MIN_FRAC = 0.05  # need this much un-supported frame to trust an outside-sky median


_REGION_MIN_PX = 50  # below this a mask cannot carry a trustworthy percentile


def _probe_metric(
    jpg_path: str,
    metric: str,
    mask_cfg: dict | None = None,
    support_path: str | None = None,
    force_global: bool = False,
    sky_outside_support: bool = False,
    freeze_to: str | None = None,
) -> float:
    """Measure the output level the stretch solver targets, from a probe JPG.

    `support_path` (Diagnosis 5): the FROZEN raster every probe of one solve
    measures body_p90 over -- the solve REGION, so the metric responds to mtf
    alone, never to mask membership churn. A geometry mismatch or too-small
    raster takes the conservative GLOBAL p90 fallback (never a fresh per-probe
    mask). Without one (starfields, a recorded failure to freeze) the legacy
    per-probe object_mask path is preserved unchanged; `mask_cfg` (config
    qa.mask) MUST then match what QA uses. `force_global` exposes the global
    fallback for callers that need to compare against it.

    For the `sky_median` metric the raster passed in is the broad STRUCTURE
    support instead, whose complement is the honest sky (Owner Gate 2).

    `freeze_to` (owner ruling, 2026-08-14) asks THIS probe to freeze the solve
    region: the object mask -- deliberately the same rule `qa.measure_render`
    applies, so the level the solver drives and the level QA reports measure the
    same pixels -- is written there as an 8-bit 0/255 PNG and used for this
    probe's own measurement. A mask too small to carry a percentile writes
    NOTHING and takes the global fallback, which the caller reads back as "no
    region was frozen". Folded into this function on purpose: it is the ONE seam
    that reads a probe JPG (the hermetic suite stubs it; a second reader bypassed
    those stubs and broke 105 tests once already).

    Only the RENDER_METRIC branch of `take_measurement` calls this. The planner
    drives `core.solve` probe-by-probe and asks the shell to render and measure
    each probe artifact.
    """
    import numpy as np

    from .qa import _mask_kw, load_rgb_luma, object_mask

    _, luma, _ = load_rgb_luma(jpg_path)
    if metric == "sky_median":  # star fields: faint-field background level
        # Owner Gate 2: on a frame-filling subject the GLOBAL median sits inside
        # the nebulosity, so a two-point solve would chase the subject it is meant
        # to leave alone. With a frozen support that still leaves honest sky,
        # measure the complement. Compact subjects are unaffected (their support
        # is a small part of the frame either way), and a support covering nearly
        # everything falls back to the global median with the solve's own
        # convergence reporting carrying the uncertainty.
        if sky_outside_support and support_path is not None:
            from PIL import Image

            m = np.asarray(Image.open(support_path).convert("L")) > 127
            if m.shape == luma.shape:
                outside = ~m
                if outside.mean() >= _SKY_OUTSIDE_MIN_FRAC:
                    return float(np.median(luma[outside]))
        return float(np.median(luma))
    if force_global:
        return float(np.percentile(luma, 90))
    if freeze_to is not None:
        from PIL import Image

        from .qa import _mask_kw as _mkw
        from .qa import object_mask as _objmask

        m, _, _ = _objmask(luma, **_mkw(mask_cfg))
        if m.sum() < _REGION_MIN_PX:
            return float(np.percentile(luma, 90))  # nothing written: no region frozen
        Image.fromarray(m.astype(np.uint8) * 255).save(freeze_to)
        return float(np.percentile(luma[m], 90))
    if support_path is not None:
        from PIL import Image

        m = np.asarray(Image.open(support_path).convert("L")) > 127
        if m.shape != luma.shape or m.sum() < 50:
            # named conservative fallback -- never recompute a per-probe mask
            return float(np.percentile(luma, 90))
        return float(np.percentile(luma[m], 90))
    m, _, _ = object_mask(luma, **_mask_kw(mask_cfg))  # legacy: per-probe subject mask
    if m.sum() < 50:  # no subject found -> global fallback
        return float(np.percentile(luma, 90))
    return float(np.percentile(luma[m], 90))


def _region_observation(freeze_to: str) -> SolveRegionObservation:
    """Typed summary of the solve region that was actually PERSISTED at
    `freeze_to`, or a valid=False observation when no raster is there.

    Deliberately measured off the file rather than off the in-memory mask: the
    raster is what every later probe, the hero contract, and a replay read, so
    the provenance must describe it and not our intent. Absent file = the probe
    took its recorded global fallback (mask too small, or a stubbed probe seam in
    the hermetic tests)."""
    if not os.path.exists(freeze_to):
        return SolveRegionObservation(
            area_frac=0.0,
            dims=(0, 0),
            source="none",
            valid=False,
            detail=f"no solve region frozen (object mask below the {_REGION_MIN_PX}px minimum)",
        )
    import numpy as np
    from PIL import Image

    m = np.asarray(Image.open(freeze_to).convert("L")) > 127
    h, w = m.shape
    return SolveRegionObservation(
        area_frac=round(float(m.mean()), 4),
        dims=(w, h),
        source="frozen_object_mask",
        valid=True,
        detail="object mask of the first real-chain body probe",
    )


def _sky_metric(jpg_path: str, support_path: str | None = None) -> float:
    """The background level a two-point solve holds down.

    Thin wrapper over `_probe_metric` so there stays exactly ONE seam that reads a
    probe JPG (tests stub that seam; a second reader silently bypassed the stubs
    and broke the hermetic suite)."""
    return _probe_metric(jpg_path, "sky_median", support_path=support_path, sky_outside_support=True)


def take_measurement(measure: Measure, ctx, cfg: dict, tools) -> tuple[Observation, dict]:
    """Execute one planner Measure against real artifacts, returning the typed
    Observation the core consumes plus a `detail` dict for notes/reporting.

    The detail dict can include richer shell-only context than the core needs;
    the orchestrator turns it into public notes sidecar fields.
    """
    from .adapters import siril as _siril_adapter
    from .measure import measure_concentration, measure_fits, probe_extended_signal

    ws = ctx.workspace
    kind = measure.kind

    def _resolve(ref) -> str:
        path = ws.find_producer(ref)
        if path is None:
            raise ExecutionError(f"take_measurement: no producer for {ref.name} ({ref.role.value})")
        return path

    if kind is MeasureKind.BORDER_SCAN:
        from .measure import measure_border

        d = measure_border(_resolve(measure.ref))
        return (
            BorderObservation(
                left=d["left"],
                top=d["top"],
                right=d["right"],
                bottom=d["bottom"],
                confident=d["confident"],
                detail=d["detail"],
            ),
            d,
        )

    if kind is MeasureKind.LINEAR_STATS:
        m = measure_fits(_resolve(measure.ref))
        return LinearStatsObservation(background=m.background, bgnoise=m.bgnoise), {
            "background": m.background,
            "bgnoise": m.bgnoise,
        }

    if kind is MeasureKind.EXTENDED_SIGNAL:
        target = ctx.resolve.get("__target__", "probe")
        E = probe_extended_signal(_resolve(measure.ref), ws.scratch_dir, target, tools)
        return EValueObservation(e_value=E), {"E": E}

    if kind is MeasureKind.CONCENTRATION:
        block = ((cfg.get("engine") or {}).get("cluster") or {}).get("block", 32)
        d = measure_concentration(_resolve(measure.ref), block=block)
        return ConcentrationObservation(concentration=d["concentration"]), d

    if kind is MeasureKind.RENDER_METRIC:
        probe_fits = _resolve(measure.ref)
        metric = measure.metric.value
        mask_cfg = (cfg.get("qa") or {}).get("mask")
        # Diagnosis 5: when the planner attached a frozen solve region, every
        # probe of this solve measures over that same raster (no per-probe mask
        # churn). The structure support is a SEPARATE raster, used only for the
        # sky complement below.
        region_path = ws.find_producer(measure.region) if measure.region is not None else None
        support_path = ws.find_producer(measure.structure_support) if measure.structure_support is not None else None
        stem = os.path.splitext(os.path.basename(probe_fits))[0]
        jpg_stem = f"{stem}_metric"
        _siril_adapter.run_siril([f'cd "{ws.scratch_dir}"', f'load "{probe_fits}"', f"savejpg {jpg_stem} 85"], tools)
        jpg = os.path.join(ws.scratch_dir, jpg_stem + ".jpg")
        # Owner ruling (2026-08-14): the FIRST body probe of a solve freezes the
        # object mask -- the region target level 72 was calibrated against -- and
        # publishes it as the raster every later probe, the hero contract, and a
        # later `seestar qa` all measure over. Frozen off a real chain probe, so
        # it is a mask of the actual render, not of a synthetic preview.
        freeze_to = ws.path_for(measure.output, ".png") if measure.output is not None else None
        val = _probe_metric(jpg, metric, mask_cfg=mask_cfg, support_path=region_path, freeze_to=freeze_to)
        # The observation is read back from the raster that was actually written,
        # never from what we intended to write: if the freeze did not happen the
        # planner must hear so and stop asking (Tenet 7 -- a step that silently
        # degrades is a defect).
        region_obs = _region_observation(freeze_to) if freeze_to is not None else None
        if region_obs is not None and region_obs.valid:
            ws.register("solve_region_freeze", measure.output, freeze_to)
        # Owner Gate 2: the two-point solve's "sky" must be measured OUTSIDE the
        # subject. A global frame median is a valid sky proxy only while the
        # subject is a small part of the field; on a frame-filling nebula it sits
        # INSIDE the nebulosity, so the bp search would chase the subject it is
        # supposed to leave alone. With a frozen support available, measure the
        # complement instead; without one, the global median stands as before.
        sky = val if metric == "sky_median" else _sky_metric(jpg, support_path)
        try:
            os.remove(jpg)
        except OSError:
            pass  # best-effort: incidental scratch cleanup (CHANGE_SAFETY rule 5)
        # The region rides back as a TYPED observation, not as loose detail: the
        # planner records it on its own state and the notes are built from there
        # (RENDER_METRIC has no detail key in the orchestrator's note map).
        return ProbeMetricObservation(value=val, sky=sky, region=region_obs), {}

    if kind is MeasureKind.SUBJECT_SUPPORT:
        from .measure import measure_subject_support

        target = ctx.resolve.get("__target__", "target")
        out_png = ws.path_for(measure.output, ".png")
        _mask, info = measure_subject_support(
            _resolve(measure.ref),
            ws.scratch_dir,
            target,
            tools,
            out_png,
            expected_frac=getattr(ctx, "subject_extent_frac", None),
        )
        if info["valid"]:
            # register under a fixed measurement key so probe metrics (and later
            # publication) resolve the raster via the planner's support ref
            ws.register("subject_support_measure", measure.output, out_png)
        return (
            SubjectSupportObservation(
                area_frac=info["area_frac"],
                dims=tuple(info["dims"]),
                source=info["source"],
                valid=info["valid"],
                detail=info["detail"],
                recovered=bool(info.get("recovered", False)),
            ),
            info,
        )

    if kind is MeasureKind.SUBJECT_BOX:
        from . import framing as F

        starless = _resolve(measure.ref)
        # measure_subject wants a FITS; StarNet emits a TIF -> convert once via Siril.
        if not starless.lower().endswith((".fit", ".fits")):
            conv = os.path.join(ws.scratch_dir, "_subject_starless")
            _siril_adapter.run_siril([f'cd "{ws.scratch_dir}"', f'load "{starless}"', "save _subject_starless"], tools)
            starless = conv + ".fit"
        fr = (cfg.get("engine") or {}).get("framing") or {}
        subj = F.measure_subject(starless, threshold_sigma=fr.get("subject_threshold_sigma", 4.0))
        box = (
            F.feature_crop_box(
                subj,
                fill_fraction=fr.get("fill_fraction", 0.66),
                k_extent=fr.get("k_extent", 2.5),
                trigger_max_frac=fr.get("trigger_max_frac", 0.62),
            )
            if subj
            else None
        )
        return SubjectBoxObservation(box=box), {"box": list(box) if box else None}

    if kind is MeasureKind.TRAIL_SCAN:
        from .adapters._trail_impl import remove_trail_fits

        in_path = _resolve(measure.ref)
        out_path = ws.path_for(measure.output, ".fit")
        info = remove_trail_fits(in_path, out_path, cfg)
        present = bool(info.get("present"))
        if present:
            # Register the detrailed artifact under the SAME op_key the planner's
            # RemoveTrail op will carry, so the plan's RemoveTrail reuses this file
            # instead of re-inpainting (single execution, no duplicate work).
            reuse_key = repr(RemoveTrail(input=measure.ref, output=measure.output))
            ws.register(reuse_key, measure.output, out_path)
        return TrailObservation(present=present), info

    if kind is MeasureKind.INPUT_STARS:
        from .qa import input_star_metrics

        metrics = input_star_metrics(_resolve(measure.ref), ws.scratch_dir, tools)
        return InputStarsObservation(star_count=metrics.get("star_count"), fwhm_px=metrics.get("fwhm_px")), metrics

    raise ExecutionError(f"take_measurement: unhandled MeasureKind {kind}")


def assert_no_duplicate_work(report: ExecutionReport) -> None:
    """Assert that no operation actually executed more than once.

    Reused operations do not count; they prove the artifact cache/reuse path is
    working. Duplicate actual executions usually mean the planner/executor
    contract has been broken.
    """
    offenders = sorted(key for key, count in report.executed_counts().items() if count > 1)
    if offenders:
        raise AssertionError(f"duplicate work detected for op_key(s): {offenders}")


def _size(path: str) -> int:
    return os.path.getsize(path)
