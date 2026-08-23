"""Boundary conversions and JSON serialization.

The core works with typed dataclasses and enums. Config files, user overrides,
QA scorecards, plan sidecars, metadata, and CLI/GUI reports are dict/JSON
surfaces, so this module owns the conversion between those shapes and typed
values. In-code defaults here must mirror `config.yaml`.
"""

from __future__ import annotations

import dataclasses
import json
import os
from enum import Enum
from pathlib import Path

import yaml

from .core.types import (
    ArtifactRef,
    ClassifyThresholds,
    ClusterConfig,
    Decisions,
    EngineDefaults,
    FramingConfig,
    GateThresholds,
    GhsParams,
    Overrides,
    PccBounds,
    PerCategory,
    Plan,
    RecoveryDefaults,
    RenderMetrics,
    SolverBounds,
    StretchTarget,
    StretchTargets,
    TargetMetric,
)

_CFG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge ``overlay`` onto ``base``. Dict values merge; every other
    value (including lists) is replaced by the overlay's. Returns a new dict."""
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | os.PathLike | None = None) -> dict:
    """Load the engine config, deep-merging a sibling ``config.local.yaml`` overlay
    when present. The overlay holds machine-specific, non-public values (real
    reference-capture paths under ``validation.targets*``) and is git-ignored, so the
    committed ``config.yaml`` stays free of personal paths. See docs/RELEASE_READINESS.md.

    Resolution order: an explicit ``path`` -> the repo-root ``config.yaml``
    (``_CFG_PATH``) if it exists -> the packaged resource
    ``seestar/data/config.default.yaml`` (installed-wheel fallback, no repo checkout
    on disk). The packaged-resource branch has no sibling ``config.local.yaml`` to
    overlay; the explicit-path and repo-root branches keep the overlay behavior
    unchanged."""
    if path is not None:
        resolved: str | os.PathLike = path
    elif _CFG_PATH.exists():
        resolved = _CFG_PATH
    else:
        # Installed wheel with no repo checkout on disk: load the packaged default.
        import importlib.resources as ir

        ref = ir.files("seestar.data") / "config.default.yaml"
        return yaml.safe_load(ref.read_text(encoding="utf-8")) or {}
    with open(resolved) as f:
        cfg = yaml.safe_load(f) or {}
    local = os.path.join(os.path.dirname(os.fspath(resolved)), "config.local.yaml")
    if os.path.exists(local):
        with open(local) as f:
            overlay = yaml.safe_load(f) or {}
        cfg = _deep_merge(cfg, overlay)
    return cfg


def _ghs(d: dict | None, default: dict) -> GhsParams | None:
    g = {**default, **(d or {})}
    if not g.get("enabled", True):
        return None
    return GhsParams(
        d=g["ght_d"],
        b=g["ght_b"],
        sp=g["ght_sp"],
        lp=g["ght_lp"],
        hp=g["ght_hp"],
        unsharp_sigma=g.get("unsharp_sigma", 0.0),
        unsharp_amount=g.get("unsharp_amount", 0.0),
        closeup=bool(g.get("closeup", False)),
    )


_GALAXY_GHS = {
    "ght_d": 1.5,
    "ght_b": 5.0,
    "ght_sp": 0.15,
    "ght_lp": 0.06,
    "ght_hp": 0.65,
    "unsharp_sigma": 0.0,
    "unsharp_amount": 0.0,
    "closeup": True,
}
_NEBULA_GHS = {"enabled": False, "ght_d": 1.5, "ght_b": 5.0, "ght_sp": 0.25, "ght_lp": 0.05, "ght_hp": 0.70}
_CLUSTER_GHS = {"enabled": True, "ght_d": 1.5, "ght_b": 5.0, "ght_sp": 0.20, "ght_lp": 0.05, "ght_hp": 0.70}


def _percat(d: dict | None, nebula: float, galaxy: float, starfield: float) -> PerCategory:
    d = d or {}
    return PerCategory(
        nebula=d.get("nebula", nebula), galaxy=d.get("galaxy", galaxy), starfield=d.get("starfield", starfield)
    )


def _target(d: dict | None, metric: str, level: float, sky_ceiling: float | None) -> StretchTarget:
    d = d or {}
    return StretchTarget(
        metric=TargetMetric(d.get("metric", metric)),
        level=d.get("level", level),
        sky_ceiling=d.get("sky_ceiling", sky_ceiling),
    )


def engine_defaults_from_cfg(engine_cfg: dict) -> EngineDefaults:
    e = engine_cfg or {}
    st = e.get("stretch") or {}
    dn = e.get("denoise") or {}
    tg = st.get("target") or {}
    cl = e.get("cluster") or {}
    cc = e.get("classify") or {}
    sh = e.get("sharpening") or {}
    fr = e.get("framing") or {}
    bge = e.get("bge") or {}
    return EngineDefaults(
        # None (absent) = measured per-edge automatic crop; an explicit config
        # value is the legacy forced symmetric crop (Diagnosis 1 compatibility).
        crop_border_frac=e.get("crop_border_frac"),
        crop_max_auto_frac=e.get("crop_max_auto_frac", 0.05),
        bge_smoothing=bge.get("smoothing", 0.5),
        bge_correction=bge.get("correction", "Subtraction"),
        denoise=_percat(dn, nebula=0.6, galaxy=0.65, starfield=0.6),
        denoise_noisy_bonus=dn.get("noisy_bonus", 0.1),
        asinh_d=_percat(st.get("asinh_d"), 75, 55, 65),
        blackpoint_factor=st.get("blackpoint_factor", 0.987),
        mtf_mid=_percat(st.get("mtf_mid"), 0.08, 0.11, 0.08),
        noisy_mtf_delta=st.get("noisy_mtf_delta", 0.03),
        noisy_level_scale=st.get("noisy_level_scale", 0.92),
        stretch_targets=StretchTargets(
            nebula=_target(tg.get("nebula"), "body_p90", 72, 18),
            galaxy=_target(tg.get("galaxy"), "body_p90", 72, 18),
            starfield=_target(tg.get("starfield"), "sky_median", 10, None),
        ),
        solve_stretch=st.get("solve_stretch", True),
        solver=SolverBounds(**{**{"lo": 0.02, "hi": 0.25, "tol": 0.5, "max_iter": 10}, **(st.get("solver") or {})}),
        saturation=_percat(e.get("saturation"), 0.60, 0.60, 0.55),
        galaxy_treatment=_ghs(e.get("galaxy_treatment"), _GALAXY_GHS),
        nebula_treatment=_ghs(e.get("nebula_treatment"), _NEBULA_GHS),
        cluster=ClusterConfig(concentration_min=cl.get("concentration_min", 50.0), block=cl.get("block", 32)),
        cluster_treatment=_ghs(e.get("cluster_treatment"), _CLUSTER_GHS),
        sharpening_undersampled_arcsec=sh.get("undersampled_arcsec", 2.5),
        sharpening_unsharp_cap=sh.get("undersampled_unsharp_cap", 0.0),
        print_black_lift=(e.get("print") or {}).get("black_lift", 0.05),
        star_reduce_k=e.get("star_reduce_k", 0.45),
        conservative_star_hero=e.get("conservative_star_hero", True),
        classify=ClassifyThresholds(
            extended_signal_threshold=cc.get("extended_signal_threshold", 0.03),
            noisy_frames_below=cc.get("noisy_frames_below", 45),
            noisy_bgnoise_above=cc.get("noisy_bgnoise_above", 0.0025),
        ),
        e_gate=e.get("e_gate", False),
        catalog_prior=e.get("catalog_prior", True),
        combine_sessions=e.get("combine_sessions", False),
        framing=FramingConfig(
            enabled=fr.get("enabled", False),
            fill_fraction=fr.get("fill_fraction", 0.80),
            k_extent=fr.get("k_extent", 2.2),
            subject_threshold_sigma=fr.get("subject_threshold_sigma", 4.0),
            trigger_max_frac=fr.get("trigger_max_frac", 0.62),
        ),
        trail_removal_enabled=(e.get("trail_removal") or {}).get("enabled", True),
        pcc=PccBounds(
            min_stars=(e.get("pcc") or {}).get("min_stars", 25),
            coeff_min=(e.get("pcc") or {}).get("coeff_min", 0.2),
            coeff_max=(e.get("pcc") or {}).get("coeff_max", 5.0),
            max_ratio=(e.get("pcc") or {}).get("max_ratio", 4.0),
        ),
        recovery=RecoveryDefaults(
            saturation_bracket=tuple((e.get("recovery") or {}).get("saturation_bracket", (0.85, 0.70))),
            body_saturation_ceiling=_percat_or_none((e.get("recovery") or {}).get("body_saturation_ceiling")),
        ),
    )


# ---------------------------------------------------------------------------
# QA gate thresholds / metrics: boundary conversions for core/qa_rules.py
# ---------------------------------------------------------------------------


def _percat_or_none(d: dict | None) -> PerCategory | None:
    """A whole threshold family absent from config (`d is None`) means that gate is
    skipped for every category. A category missing WITHIN a present dict (e.g. a
    test fixture that predates a category's addition) stores None in that slot --
    PerCategory.for_category then returns None and the gate skips for that
    category only, matching the old `(gates_cfg.get(...) or {}).get(category)`
    dict-lookup behavior exactly. A dict present but null in EVERY category (e.g.
    config.yaml's documented-but-disabled `{nebula: null, galaxy: null,
    starfield: null}`) collapses to None too -- every call site already guards
    with `if thresholds.X else None` before `.for_category`, so a present
    all-None family and an absent family are indistinguishable in behavior; only
    dataclass equality could tell them apart, which would wrongly break the
    empty-cfg/shipped-config parity invariant (`test_core_engine_types.py`)."""
    if d is None:
        return None
    pc = PerCategory(nebula=d.get("nebula"), galaxy=d.get("galaxy"), starfield=d.get("starfield"))
    if pc.nebula is None and pc.galaxy is None and pc.starfield is None:
        return None
    return pc


def gate_thresholds_from_cfg(gates_cfg: dict) -> GateThresholds:
    """Typed mirror of config.yaml's `qa.gates` block (mirrors engine_defaults_from_cfg).
    `gates_cfg` is that block itself (as already threaded around qa.py: `cfg["qa"]["gates"]`)."""
    g = gates_cfg or {}
    ref = g.get("reference") or {}
    ref_dims = ref.get("dims")
    rgb = g.get("body_rgb_max_ratio") or {}
    sp = g.get("star_preservation") or {}
    return GateThresholds(
        enabled=g.get("enabled", True),
        reference_pixscale=ref.get("pixscale_arcsec"),
        reference_dims=tuple(ref_dims) if ref_dims else None,
        star_preservation_min_retention=sp.get("min_retention"),
        star_preservation_catastrophe_floor=sp.get("catastrophe_floor"),
        fwhm_ceiling=_percat_or_none(g.get("fwhm_ceiling")),
        core_sat_ceiling=_percat_or_none(g.get("core_sat_ceiling")),
        faint_retention_min=(g.get("faint_structure") or {}).get("retention_min"),
        faint_catastrophe_floor=(g.get("faint_structure") or {}).get("catastrophe_floor"),
        near_black_ceiling=_percat_or_none(g.get("near_black_ceiling")),
        sky_median_ceiling=_percat_or_none(g.get("sky_median_ceiling")),
        pegged_chroma_ceiling=_percat_or_none(g.get("pegged_chroma_ceiling")),
        core_saturation_ceiling=_percat_or_none(g.get("core_saturation_ceiling")),
        star_saturation_ceiling=_percat_or_none(g.get("star_saturation_ceiling")),
        body_rgb_max_ratio_starfield=rgb.get("starfield"),
        body_rgb_max_ratio_galaxy=rgb.get("galaxy"),
        star_retention_warn=g.get("star_retention_warn", 0.35),
    )


def qa_report_to_dict(report) -> dict:
    """Typed QAReport -> JSON-safe dict, reusing the plan-sidecar `_ser` helper
    (dataclasses/Enum -> primitives) rather than writing a second serializer."""
    return _ser(report)


def hero_report_to_dict(report) -> dict:
    """Typed HeroQualityReport -> JSON-safe dict (reuses the same recursive
    serializer as qa_report_to_dict/plan_to_dict)."""
    return _ser(report)


def probes_to_list(probes) -> list[dict]:
    """Typed ProbeRecord tuple -> JSON-safe list, for the *_notes.json sidecar
    (which hand-builds its dict rather than using plan_to_dict's _ser)."""
    return [_ser(p) for p in probes]


def render_metrics_from_dict(m: dict) -> RenderMetrics:
    """qa.measure_render's dict (+ the star_count/fwhm qa.score adds) -> RenderMetrics.
    Tolerant of partial dicts: compatibility callers and unit tests may pass
    hand-built fixtures that omit fields the gate under test does not touch.
    Missing non-Optional numeric fields default to 0.0, which is safe because
    every gate that reads them is itself skipped when its threshold is absent,
    and every test that exercises an active gate supplies the metric.
    """
    dims = m.get("dimensions")
    body_rgb = m.get("body_rgb")
    return RenderMetrics(
        dimensions=tuple(dims) if dims else (0, 0),
        envelope_area_frac=m.get("envelope_area_frac", 0.0),
        body_p90=m.get("body_p90"),
        body_p95=m.get("body_p95"),
        body_rgb=tuple(body_rgb) if body_rgb else None,
        background_median_outside_mask=m.get("background_median_outside_mask"),
        near_black_frac_outside_mask=m.get("near_black_frac_outside_mask", 0.0),
        core_sat_frac=m.get("core_sat_frac", 0.0),
        pegged_chroma_frac_body=m.get("pegged_chroma_frac_body", 0.0),
        body_saturation_median=m.get("body_saturation_median", 0.0),
        core_saturation_median=m.get("core_saturation_median", 0.0),
        core_value_median=m.get("core_value_median", 0.0),
        star_saturation_median=m.get("star_saturation_median", 0.0),
        global_black_frac=m.get("global_black_frac", 0.0),
        star_count=m.get("star_count"),
        fwhm=m.get("fwhm"),
    )


# ---------------------------------------------------------------------------
# Params-dict overrides -> typed Overrides (prefs, recipe pins, caller overrides)
# ---------------------------------------------------------------------------

# The public dict-style override inputs map 1:1 onto Overrides fields. Any other
# key (for example "ght") has no Overrides field and is not convertible.
_OVERRIDE_FIELDS = frozenset(f.name for f in dataclasses.fields(Overrides))


def overrides_from_prefs_dict(overrides: dict | None) -> Overrides:
    """Convert a public params-dict override mapping to typed `Overrides`.

    Keys with no matching Overrides field (e.g. "ght") are NOT convertible; this
    raises ValueError naming them (the caller `overrides=` contract). prefs and
    recipe pins only ever emit convertible keys."""
    if not overrides:
        return Overrides()
    unknown = sorted(k for k in overrides if k not in _OVERRIDE_FIELDS)
    if unknown:
        raise ValueError(f"unconvertible override key(s) (no Overrides field): {', '.join(unknown)}")
    return Overrides(**{k: overrides[k] for k in overrides})


def decisions_to_params(d: Decisions) -> dict:
    """Typed Decisions -> public params dict.

    This compatibility shape is still consumed by metadata, recipes, QA, and
    CLI/GUI reports. Key presence is intentionally sparse: optional values such
    as `blackpoint_scale`, `annotate`, `black_lift`, `ght`, and
    `sharpening_capped` appear only when they matter.
    """
    params = {
        "category": d.category.value,
        "snr_class": d.snr_class.value,
        "profile": d.profile,
        "bge_smoothing": d.bge_smoothing,
        "bge_correction": d.bge_correction,
        "denoise_strength": d.denoise_strength,
        "pcc": d.pcc,
        "asinh_d": d.asinh_d,
        "blackpoint": d.blackpoint,
        "mtf_mid": d.mtf_mid,
        "stretch_target_scale": d.stretch_target_scale,
        "saturation": d.saturation,
        "star_forks": d.star_forks,
        "ghs_allowed": d.ghs_allowed,
        "sky_solve": d.sky_solve,
        # unconditional: recipe emission reads params.get("trail_removal_enabled")
        # first, so a pin/pref that disabled the stage displays correctly (the render
        # already honored it; only the recipe's shown current value was wrong).
        "trail_removal_enabled": d.trail_removal_enabled,
    }
    # sparse: present only when an explicit legacy override forces the symmetric
    # crop; absence means the measured per-edge automatic crop (Diagnosis 1)
    if d.crop_border_frac is not None:
        params["crop_border_frac"] = d.crop_border_frac
    if d.ght is not None:
        ght = {
            "ght_d": d.ght.d,
            "ght_b": d.ght.b,
            "ght_sp": d.ght.sp,
            "ght_lp": d.ght.lp,
            "ght_hp": d.ght.hp,
            "unsharp_sigma": d.ght.unsharp_sigma,
            "unsharp_amount": d.ght.unsharp_amount,
        }
        if d.ght.closeup:
            ght["closeup"] = True
        params["ght"] = ght
    if d.sharpening_capped is not None:
        params["sharpening_capped"] = {
            "pixscale_arcsec": d.sharpening_capped.pixscale_arcsec,
            "unsharp_cap": d.sharpening_capped.cap,
        }
    if d.blackpoint_scale != 1.0:
        params["blackpoint_scale"] = d.blackpoint_scale
    if d.annotate:
        params["annotate"] = True
    if d.black_lift:
        params["black_lift"] = d.black_lift
    return params


def decisions_to_report_dict(decisions: Decisions) -> dict:
    """Typed `Decisions` -> plain dict, for REPORTING only: the orchestrator's
    return-value `"params"` key (read by the CLI printout, the GUI, and
    `seestar qa`). Same underlying conversion as `decisions_to_params`, but this
    name makes the call site explicit: report serialization, not core policy.
    """
    return decisions_to_params(decisions)


# ---------------------------------------------------------------------------
# Plan / execution sidecar serialization
# ---------------------------------------------------------------------------

PLAN_FORMAT = 1


def _ser(v):
    """Recursively serialize a typed value to JSON-safe primitives: ArtifactRef ->
    {"name","role"}; Enum -> .value; nested dataclasses (GhsParams, SolveProvenance)
    -> their fields; tuples/lists -> lists."""
    if isinstance(v, ArtifactRef):
        return {"name": v.name, "role": v.role.value}
    if isinstance(v, Enum):
        return v.value
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return {f.name: _ser(getattr(v, f.name)) for f in dataclasses.fields(v)}
    if isinstance(v, tuple | list):
        return [_ser(x) for x in v]
    return v


def _ser_op(op) -> dict:
    """One operation as a dict, tagged with its class name (the discriminator)."""
    out = {"op": type(op).__name__}
    for f in dataclasses.fields(op):
        out[f.name] = _ser(getattr(op, f.name))
    return out


def plan_to_dict(plan: Plan) -> dict:
    return {
        "steps": [_ser_op(op) for op in plan.steps],
        "deliverables": [_ser(ref) for ref in plan.deliverables],
        "solve": _ser(plan.solve) if plan.solve is not None else None,
        # Diagnosis 4: which color path shipped (attempted/accepted/fallback + evidence)
        "color": _ser(plan.color) if plan.color is not None else None,
    }


def execution_to_dict(report) -> dict:
    """ExecutionReport -> JSON-safe dict. Absolute paths are dropped (portability);
    op_key + status carry the no-duplicate-work invariant the sidecar records."""
    return {
        "records": [
            {
                "op_key": r.op_key,
                "stage": r.stage,
                "adapter": r.adapter,
                "status": r.status,
                "outputs": [[name, size] for (name, _path, size) in r.outputs],
                "error": r.error,
            }
            for r in report.records
        ],
        "warnings": list(report.warnings),
    }


def write_plan_sidecar(path: str, plan: Plan, report, published: dict | None = None) -> None:
    """Write {"plan_format", "plan", "execution"} atomically (temp file + os.replace).
    A failure leaves no partial target file.

    `published`, when given, is recorded additively as `doc["execution"]["published"]`
    (logical output base -> published path, plus the run's certification) -- no
    `plan_format` bump; both the certified and uncertified namespaces get it."""
    doc = {"plan_format": PLAN_FORMAT, "plan": plan_to_dict(plan), "execution": execution_to_dict(report)}
    if published is not None:
        doc["execution"]["published"] = published
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2, default=str)
        os.replace(tmp, path)
    except OSError:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass  # best-effort: incidental scratch cleanup (CHANGE_SAFETY rule 5); the mandatory write already fails loudly via `raise` below
        raise


def run_manifest_to_dict(
    attempt: int,
    source_identity: dict | None,
    deliverables: list[dict],
    plan_sidecar: str,
    notes_sidecar: str,
    accepted_at: str,
    outcome: str = "pass",
    qa_artifacts: list[str] | None = None,
    superseded: list[str] | None = None,
) -> dict:
    """The single-commit-point run manifest: written LAST in promotion, after
    every deliverable file has already been renamed into place. Its presence
    (and its `deliverables` list) is the authoritative record of what an
    accepted run produced -- a failed/exhausted attempt must never reach the
    write that replaces this file (see orchestrate.py's promotion block).

    `outcome` is the hero's honest final state (HeroOutcome value: pass /
    source_limited / processing_failed). Phase 3C keeps publishing a best-effort
    image on PROCESSING_FAILED, so the manifest still lands -- but it now records
    the failure instead of implying success by its mere presence.

    `qa_artifacts` = promoted QA-role artifact basenames (e.g. the concentrated-
    cluster starless) -- attributable, not user deliverables. `superseded` =
    basenames scheduled for post-commit cleanup (Task 7 populates; empty until
    then). `accepted_at` = commit timestamp of this publication."""
    return {
        "manifest_format": 1,
        "attempt": attempt,
        "outcome": outcome,
        "source_identity": source_identity,
        "deliverables": deliverables,
        "qa_artifacts": list(qa_artifacts or []),
        "superseded": list(superseded or []),
        "plan_sidecar": plan_sidecar,
        "notes_sidecar": notes_sidecar,
        "accepted_at": accepted_at,
    }


def write_run_manifest(path: str, manifest: dict) -> None:
    """Atomic single-file commit: temp file + os.replace, mirroring
    write_plan_sidecar's pattern exactly. This IS the transactional
    publication point -- call it only after every deliverable rename in
    orchestrate.py's promotion block has already succeeded."""
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, default=str)
        os.replace(tmp, path)
    except OSError:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass  # best-effort: incidental scratch cleanup (CHANGE_SAFETY rule 5); the mandatory write already fails loudly via `raise` below
        raise
