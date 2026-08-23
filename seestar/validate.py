"""Back-validation harness (`seestar validate`).

Re-renders each configured validation target from its raw stack into a scratch
dir, QA-scores it, and diffs the fresh scorecard against the committed baseline
in validation/baselines/. An engine change is DONE only when every available
reference comes out identical-or-better: metric drift beyond tolerance, a lost
`passed`, or any NEW gate-fail kind is a regression.

Raw paths are machine-specific; a missing path is skipped with a warning, never
an error. Renders are slow because they shell to GraXpert, Siril, and StarNet,
so validation runs sequentially.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys

# Mirrors config.yaml validation.tolerances -- the in-code fallback.
DEFAULT_TOLERANCES = {
    "star_count_rel": 0.05,  # +/-5% relative
    "fwhm_abs": 0.15,  # +/-0.15 px
    "level_abs": 2.0,  # +/-2 on 0-255 levels
    "fraction_rel": 0.20,  # +/-20% relative on fraction metrics
    "fraction_abs_floor": 0.005,  # near-zero fractions: abs delta under this always passes
}

# metric name -> (path into the qa json, tolerance kind)
COMPARED = {
    "star_count": (("metrics", "star_count"), "star_count_rel"),
    "fwhm": (("metrics", "fwhm"), "fwhm_abs"),
    "body_p90": (("metrics", "body_p90"), "level_abs"),
    "body_p95": (("metrics", "body_p95"), "level_abs"),
    "sky_median": (("metrics", "background_median_outside_mask"), "level_abs"),
    "envelope_area_frac": (("metrics", "envelope_area_frac"), "fraction_rel"),
    "near_black_frac": (("metrics", "near_black_frac_outside_mask"), "fraction_rel"),
    "core_sat_frac": (("metrics", "core_sat_frac"), "fraction_rel"),
    "pegged_chroma_frac": (("metrics", "pegged_chroma_frac_body"), "fraction_rel"),
    # Diagnosis 3 (2026-08): faint structure is SOURCE-RELATIVE -- the compared
    # value is the retained supported fraction, not the old FOV-scaled frame
    # fraction. Baselines that predate this carry only `faint_structure_frac`;
    # diff_qa surfaces the missing-metric warning rather than silently accepting
    # the old wording -- re-baseline deliberately.
    "faint_retention": (("absolute_gates", "faint_retention"), "fraction_rel"),
}

# Masked percentiles move when the subject MASK moves -- a mask-membership fact, not
# a brightness regression (NGC5128, 2026-08: a 32% larger measured envelope, not a
# visibly dimmer image). They stay REPORTED -- a metric that silently stopped being
# checked would be the defect docs/TENETS.md tenet 7 names -- but they no longer fail
# the run. `fixed_area_p90` gates brightness in their place.
DIAGNOSTIC = {"body_p90", "body_p95", "envelope_area_frac"}


def fixed_area_p90(luma, frac: float):
    """90th percentile of luma within the brightest `frac` fraction of the FRAME.

    Mask-independent by construction: the pixel COUNT is pinned by `frac` (taken from
    the BASELINE), so a subject mask that grows or shrinks cannot move the number --
    only pixels actually getting brighter or dimmer can. Returns None for a nonsense
    fraction; the caller must report that skip, never swallow it.
    """
    import numpy as np

    if not (0.0 < float(frac) <= 1.0):
        return None
    flat = np.asarray(luma, dtype=np.float64).ravel()
    n = min(max(1, int(round(flat.size * float(frac)))), flat.size)
    top = np.partition(flat, flat.size - n)[flat.size - n :]
    return float(np.percentile(top, 90))


_GATE_KIND = re.compile(
    r"^(star_preservation|star_count|fwhm|core_sat|faint_structure|dimensions"
    r"|background crushed|gray wash|neon color|orientation)"
)


def gate_kinds(fails: list[str]) -> set[str]:
    """Normalize gate-fail strings to their KIND so the embedded numbers don't defeat
    set comparison ('fwhm 4.20 > ...' and 'fwhm 4.53 > ...' are the same kind; a NEW
    kind appearing is a regression, a number moving within the same kind is not)."""
    kinds = set()
    for f in fails or []:
        m = _GATE_KIND.match(f)
        kinds.add(m.group(1) if m else f.split()[0])
    return kinds


def tiff_diff(path_a: str, path_b: str) -> dict:
    """Pure pixel comparison of two 16-bit TIFF renders.

    Reports both max absolute per-pixel diff and RMSE; RMSE alone can average
    localized errors away over millions of pixels. Units are 16-bit ADU.
    """
    import numpy as np
    import tifffile

    a = tifffile.imread(path_a).astype(np.int64)
    b = tifffile.imread(path_b).astype(np.int64)
    if a.shape != b.shape:
        return {"max_abs": None, "rmse": None, "error": f"shape {a.shape} vs {b.shape}"}
    d = a - b
    return {
        "max_abs": int(np.abs(d).max()),
        "rmse": float(np.sqrt((d.astype(np.float64) ** 2).mean())),
    }


def _get(d: dict, path: tuple) -> object:
    for k in path:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def diff_qa(baseline: dict, fresh: dict, tol: dict | None = None, extra_metrics: dict | None = None) -> dict:
    """PURE: compare a fresh qa scorecard against its baseline.
    Returns {"metrics": {name: {baseline, fresh, delta, tolerance, ok}}, "hard": {...},
    "ok": bool, "warnings": [...]}. Hard regressions (always fail): baseline passed
    but fresh did not; a NEW gate-fail kind. A metric missing on either side is
    skipped with a warning, never a fail (starfields have no faint_structure)."""
    t = {**DEFAULT_TOLERANCES, **(tol or {})}
    out: dict = {"metrics": {}, "warnings": []}

    for name, (path, kind) in COMPARED.items():
        b, f = _get(baseline, path), _get(fresh, path)
        if b is None or f is None:
            out["metrics"][name] = {"baseline": b, "fresh": f, "ok": None, "note": "skipped (missing)"}
            if (b is None) != (f is None):
                out["warnings"].append(f"{name}: present on one side only (baseline={b}, fresh={f})")
            continue
        delta = f - b
        if kind.endswith("_rel"):
            limit = t[kind] * abs(b)
            if kind == "fraction_rel" and abs(delta) <= t["fraction_abs_floor"]:
                ok = True  # near-zero fractions: tiny absolute wobble is noise
            else:
                ok = abs(delta) <= limit
        else:
            limit = t[kind]
            ok = abs(delta) <= limit
        out["metrics"][name] = {
            "baseline": b,
            "fresh": f,
            "delta": round(delta, 4),
            "tolerance": round(limit, 4),
            "ok": ok,
        }

    # Metrics the HARNESS measures from the renders rather than reading out of the qa
    # json (fixed_area_p90). Same tolerance machinery, same reporting row -- so the
    # comparator has one code path and one printout, not two.
    for name, spec in (extra_metrics or {}).items():
        b, f = spec.get("baseline"), spec.get("fresh")
        if b is None or f is None:
            out["metrics"][name] = {"baseline": b, "fresh": f, "ok": None, "note": "skipped (missing)"}
            out["warnings"].append(f"{name}: not measurable (baseline={b}, fresh={f})")
            continue
        kind = spec.get("kind", "level_abs")
        limit = t[kind] * abs(b) if kind.endswith("_rel") else t[kind]
        out["metrics"][name] = {
            "baseline": b,
            "fresh": f,
            "delta": round(f - b, 4),
            "tolerance": round(limit, 4),
            "ok": abs(f - b) <= limit,
        }

    b_kinds = gate_kinds(baseline.get("hard_fails") or [])
    f_kinds = gate_kinds(fresh.get("hard_fails") or [])
    new_kinds = sorted(f_kinds - b_kinds)
    passed_regressed = bool(baseline.get("passed")) and not fresh.get("passed")
    out["hard"] = {
        "passed_baseline": baseline.get("passed"),
        "passed_fresh": fresh.get("passed"),
        "passed_regressed": passed_regressed,
        "new_gate_fail_kinds": new_kinds,
        "resolved_gate_fail_kinds": sorted(b_kinds - f_kinds),
    }
    for name, m in out["metrics"].items():
        m["diagnostic"] = name in DIAGNOSTIC
    metric_fails = [n for n, m in out["metrics"].items() if m["ok"] is False and not m["diagnostic"]]
    out["metric_fails"] = metric_fails
    out["ok"] = not passed_regressed and not new_kinds and not metric_fails
    return out


def _raw_identity(staged_dir: str) -> dict:
    """SHA-256 + capture facts of the .fit `run_target` will actually discover and
    process in `staged_dir` (the post-`stage_raw` folder) -- the exact same file the
    render uses, not the configured path (which may be a multi-session folder)."""
    from .run import _find_raw, file_meta

    try:
        raws = _find_raw(staged_dir)
    except FileNotFoundError:
        return {}
    if not raws:
        return {}
    latest = max(raws, key=lambda r: file_meta(r).get("datetime") or "")
    h = hashlib.sha256()
    with open(latest, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    m = file_meta(latest)
    return {
        "sha256": h.hexdigest(),
        "basename": os.path.basename(latest),
        "frames": m.get("frames"),
        "datetime": m.get("datetime"),
        "filter": m.get("filter"),
    }


def _render_label(src: str, name: str) -> str:
    """Best-effort reproduction of `orchestrate.run_target`'s derived target label
    (header `OBJECT`, filename fallback, then a sanitized folder name) for callers
    that only have the staged raw dir, not `run_target`'s return value. A render's
    identity is content-derived, NEVER the validation config key `name` (CLAUDE.md
    §2/§13) -- e.g. an `M8_noisy` validation entry (a short/noisy M8 capture) still
    renders as `M8_*` because its header's `OBJECT` still reads "M 8". Falls back to
    `name` only when nothing can be determined (mirrors orchestrate's own last-resort
    fallback semantics)."""
    from .inspect import file_meta, sanitize_label
    from .run import _find_raw

    try:
        raws = _find_raw(src)
    except FileNotFoundError:
        return name
    if not raws:
        return name
    latest = max(raws, key=lambda r: file_meta(r).get("datetime") or "")
    return file_meta(latest).get("target") or sanitize_label(os.path.basename(os.path.normpath(src))) or name


def stage_raw(name: str, raw: str, scratch: str) -> str | None:
    """Resolve a validation raw entry to a runnable target folder. A folder is used
    as-is and treated as read-only. A single .fit FILE is copied into
    <scratch>/<name>_input/ and that folder is returned, so the file's home
    directory is never written to. Missing path -> None (caller skips)."""
    if os.path.isdir(raw):
        return raw
    if os.path.isfile(raw) and raw.lower().endswith((".fit", ".fits")):
        staged = os.path.join(scratch, f"{name}_input")
        os.makedirs(staged, exist_ok=True)
        dest = os.path.join(staged, os.path.basename(raw))
        if not os.path.exists(dest):
            shutil.copy2(raw, dest)
        return staged
    return None


def validate_target(name: str, raw: str, cfg: dict, scratch: str, baselines: str, update: bool = False) -> dict:
    """Render `name` from `raw` into <scratch>/<name>/, QA it, and diff against the
    committed baseline (or capture it with update=True). Refuses to render (and
    refuses to compare) when the staged raw's identity doesn't match the baseline's
    recorded identity -- different photons are not a regression (docs/VALIDATION.md P2)."""
    from .qa import qa_target
    from .run import run_target

    src = stage_raw(name, raw, scratch)
    if src is None:
        return {"target": name, "status": "skipped", "reason": f"raw not found: {raw}"}

    identity = _raw_identity(src)
    baseline_path = os.path.join(baselines, f"{name}_qa.json")
    baseline: dict | None = None
    if os.path.exists(baseline_path):
        with open(baseline_path) as f:
            baseline = json.load(f)
        b_id = baseline.get("raw_identity")
        if not update and b_id and identity and b_id.get("sha256") != identity.get("sha256"):
            return {
                "target": name,
                "status": "skipped",
                "reason": (
                    f"raw stack differs from baseline (yours: {identity.get('frames')}x10s "
                    f"{identity.get('datetime')}, baseline: {b_id.get('frames')}x10s "
                    f"{b_id.get('datetime')}) -- not comparable"
                ),
            }

    out_dir = os.path.join(scratch, name)
    render = run_target(src, out_dir, cfg, profile="showcase", out_name="final")

    # A gate that silently skips is a defect (docs/TENETS.md tenet 7). `published`
    # absent/None means a legacy caller/fake never returned publication info --
    # treated as certified so old renders and existing fakes keep working
    # (tests/test_validate.py's `lambda *a, **k: {}`). But an EXPLICIT
    # `certified: False` means the reference render itself failed processing
    # (HeroOutcome.PROCESSING_FAILED, core/publication.py's decide_publication) --
    # its pixels/QA are not a certified artifact, so orientation/QA comparison
    # against it would validate against garbage. Hard-fail BEFORE any baseline or
    # orientation logic runs, and refuse to capture it as a baseline at all.
    published = (render or {}).get("published")
    if published is not None and published.get("certified") is False:
        if update:
            return {"target": name, "status": "refused_uncertified"}
        return {
            "target": name,
            "status": "fail",
            "reason": (
                "reference render was uncertified (processing_failed) -- orientation "
                "and QA comparison require a certified render"
            ),
        }

    # The render's own output filenames are derived from the FITS header/filename
    # (orchestrate.run_target), NEVER from the validation config key `name` -- e.g. an
    # "M8_noisy" entry may still render as "M8_final.jpg" if that capture's header
    # OBJECT reads "M 8". `render["target"]` carries the actual label; only the
    # baselines/ dir and status/reporting below stay keyed by the config name.
    label = (render or {}).get("target") or name
    fresh = qa_target(out_dir, cfg, label)
    if identity:
        fresh["raw_identity"] = identity

    if update:
        os.makedirs(baselines, exist_ok=True)
        with open(baseline_path, "w") as f:
            json.dump(fresh, f, indent=2)
        final_jpg = os.path.join(out_dir, f"{label}_final.jpg")
        if os.path.exists(final_jpg):
            shutil.copy2(final_jpg, os.path.join(baselines, f"{name}_final.jpg"))
        return {"target": name, "status": "baseline_updated", "baseline": baseline_path}
    if baseline is None:
        return {"target": name, "status": "no_baseline", "reason": f"missing {baseline_path} (run --update-baselines)"}
    # Both renders are read ONCE here, before the diff, because two comparisons need
    # the same two luma arrays: the orientation gate and the fixed-area brightness
    # metric. Reads only -- no writes were relocated by this ordering.
    extra: dict = {}
    om = None
    baseline_img = os.path.join(baselines, f"{name}_final.jpg")
    fresh_img = os.path.join(out_dir, f"{label}_final.jpg")
    if os.path.exists(baseline_img) and os.path.exists(fresh_img):
        from .qa import load_rgb_luma, orientation_match

        _, fresh_luma, _ = load_rgb_luma(fresh_img)
        _, base_luma, _ = load_rgb_luma(baseline_img)
        om = orientation_match(fresh_luma, base_luma)
        # Validation-only, deliberately NOT a field in the shipped {target}_qa.json.
        # The area fraction is pinned from the BASELINE's envelope, so a fresh render
        # whose mask grew cannot masquerade as a brightness change.
        pinned = (baseline.get("metrics") or {}).get("envelope_area_frac")
        b_val = fixed_area_p90(base_luma, pinned) if pinned else None
        f_val = fixed_area_p90(fresh_luma, pinned) if pinned else None
        extra["fixed_area_p90"] = {
            "baseline": round(b_val, 1) if b_val is not None else None,
            "fresh": round(f_val, 1) if f_val is not None else None,
            "kind": "level_abs",
            "area_frac": pinned,
        }

    tol = (cfg.get("validation") or {}).get("tolerances")
    diff = diff_qa(baseline, fresh, tol, extra_metrics=extra)
    result = {"target": name, "status": "pass" if diff["ok"] else "fail", "diff": diff}
    if om is not None:
        result["orientation"] = om
        if om["best"] != "identity":
            result["status"] = "fail"
            result["reason"] = f"orientation vs baseline: {om['best']}"
    return result


def replay_check(name: str, src: str, out_dir: str, cfg: dict, scratch: str) -> dict:
    """Re-render with the target's own emitted, unedited recipe and compare.

    The replay must produce identical recipe values and pixels within
    `config.validation.replay` tolerances on the same machine in the same
    validation run.
    """
    import seestar.run as run_module

    from .core.publication import sidecar_names

    # `out_dir`'s files are named by the render's own derived label (header/filename),
    # never the validation config key `name` -- see `_render_label`. `src` is the same
    # staged raw the original render used, so re-deriving from it here reproduces
    # exactly the label `run_target` picked (both the original render in `out_dir` and
    # the replay render below share the same raw file/header, hence the same label).
    label = _render_label(src, name)

    # `replay_check` never receives the original render's return value (it's called
    # from `run_validation` after the fact, keyed only by `out_dir`), so the
    # certified/uncertified namespace has to be read back from which manifest is on
    # disk. A certified original leaves `{label}_run.json`; an uncertified one leaves
    # only the `_uncertified` run manifest. Replaying reproducibility off an
    # uncertified original would compare against a failed render's artifacts --
    # a hard fail, never the ordinary "no recipe yet" `replay_skipped` (tenet 7: a
    # gate that silently skips is a defect).
    if not os.path.exists(os.path.join(out_dir, f"{label}_run.json")):
        uncertified_run = os.path.join(out_dir, sidecar_names(label, "final", False)["run"])
        if os.path.exists(uncertified_run):
            return {
                "target": name,
                "status": "fail",
                "reason": "original render was uncertified; replay reproducibility requires a certified render",
            }

    recipe = os.path.join(out_dir, f"{label}_recipe.json")
    if not os.path.exists(recipe):
        return {"target": name, "status": "replay_skipped", "reason": "no emitted recipe"}
    replay_dir = os.path.join(scratch, f"{name}_replay")
    run_module.run_target(src, replay_dir, cfg, profile="showcase", out_name="final", recipe_path=recipe)

    def _values(p):
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)
        return {e["adjustment"]: e["value"] for e in doc.get("adjustments", [])}

    v1 = _values(recipe)
    v2 = _values(os.path.join(replay_dir, f"{label}_recipe.json"))
    param_diffs = {k: {"original": v1.get(k), "replay": v2.get(k)} for k in set(v1) | set(v2) if v1.get(k) != v2.get(k)}

    tol = (cfg.get("validation") or {}).get("replay") or {}
    pixels = tiff_diff(os.path.join(out_dir, f"{label}_final.tif"), os.path.join(replay_dir, f"{label}_final.tif"))
    pixels_ok = (
        pixels.get("max_abs") is not None
        and pixels["max_abs"] <= tol.get("max_abs_ceiling", 0)
        and pixels["rmse"] <= tol.get("rmse_ceiling", 0.0)
    )
    ok = not param_diffs and pixels_ok
    return {
        "target": name,
        "status": "replay_pass" if ok else "replay_fail",
        "param_diffs": param_diffs,
        "pixels": pixels,
        "tolerances": tol,
    }


def _resolve_os_paths(vcfg: dict, platform: str) -> tuple[dict, str]:
    """Pick OS-specific validation raw paths + baselines dir. Reference raw data and
    baselines live at different absolute locations per machine (Windows render box vs
    Mac); on darwin the *_darwin overrides win when present, else the base keys -- so
    Windows/default behavior stays byte-identical. Baselines get their own dir per OS so
    a Mac run never clobbers the committed Windows baselines."""
    is_mac = platform == "darwin"
    tmap = (vcfg.get("targets_darwin") if is_mac else None) or vcfg.get("targets") or {}
    baselines = (vcfg.get("baselines_dir_darwin") if is_mac else None) or vcfg.get(
        "baselines_dir", "validation/baselines"
    )
    return tmap, baselines


def run_validation(
    cfg: dict, targets: list[str] | None = None, update: bool = False, scratch: str | None = None
) -> dict:
    """Sequential back-validation of every configured (or selected) target. Missing
    raw paths SKIP with a warning -- never an error. Summary `ok` is False only on a
    real regression (skips and no-baselines don't fail the run, they're reported)."""
    vcfg = cfg.get("validation") or {}
    tmap, baselines_rel = _resolve_os_paths(vcfg, sys.platform)  # per-OS raw paths + baselines
    # Absolute paths: Siril scripts `cd` into the render dir, and Siril's own
    # default CWD is not ours. A relative scratch dir can break the first render.
    scratch = os.path.abspath(scratch or vcfg.get("scratch_dir", "validation/scratch"))
    baselines = os.path.abspath(baselines_rel)
    os.makedirs(scratch, exist_ok=True)

    selected = {k: v for k, v in tmap.items() if targets is None or k in targets}
    results = []
    for name, raw in selected.items():
        results.append(validate_target(name, raw, cfg, scratch, baselines, update=update))

    # Replay reproducibility is checked on one target per run: the property
    # belongs to the code path, not to each image.
    for r, (name, raw) in zip(results, selected.items(), strict=False):
        if r["status"] in ("pass", "fail", "baseline_updated", "no_baseline"):
            src = stage_raw(name, raw, scratch)
            results.append(replay_check(name, src, os.path.join(scratch, name), cfg, scratch))
            break
    statuses = [r["status"] for r in results]
    return {
        "results": results,
        "counts": {s: statuses.count(s) for s in sorted(set(statuses))},
        "ok": not any(s in ("fail", "replay_fail", "refused_uncertified") for s in statuses),
    }
