"""Reference-anchored QA for finished renders (CLAUDE.md §13 guardrails).

The lesson from M8: judge the OBJECT and the BACKGROUND separately, and benchmark
against the target's own reference. Global histogram checks lie (the "0.1% near-black
= gray wash" false alarm was actually the nebula envelope filling the frame).

Layers, kept distinct:
  - HARD gates  -> catastrophes: source-relative star preservation, dimensions, blown core, ORIENTATION
                   (the flip passed every numeric check; only the star pattern caught it).
  - SOFT scores -> aesthetic distance vs the reference (masked body p90/p95, envelope,
                   background outside the mask). Never fail on these alone.
  - WARN only   -> global black fraction (misleading; mask the object instead).

Pure functions (object_mask, orientation_match, masked metrics) need only numpy/PIL
and are unit-tested. star_count shells to Siril findstar and is skipped when absent.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re

import numpy as np
from astropy.stats import sigma_clipped_stats

try:
    from PIL import Image, ImageFilter
except ImportError:  # pillow is a declared dep; guard for clarity
    Image = None

from .adapters._external_process import ExternalToolTimeout
from .adapters.base import ToolPaths
from .core.publication import (
    allowed_out_names,
    published_base,
    published_fork_base,
    sidecar_names,
    solve_region_artifact_name,
)
from .core.qa_rules import (
    compute_star_retention,
    evaluate_gates,
    evaluate_hero,
    star_preservation,
    star_retention_warning,
)
from .core.qa_rules import scale_factors as qa_rules_scale_factors
from .core.types import Category, FaintEvidence, Verdict
from .io import gate_thresholds_from_cfg, hero_report_to_dict, qa_report_to_dict, render_metrics_from_dict
from .tools import resolve_tools


# --- image IO + features ------------------------------------------------------
def load_rgb_luma(path: str):
    """Return (rgb float HxWx3 0..255, luma HxW 0..255, (w,h))."""
    im = Image.open(path).convert("RGB")
    a = np.asarray(im).astype(np.float64)
    luma = 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]
    return a, luma, im.size


def _mask_kw(mask_cfg: dict | None) -> dict:
    """object_mask kwargs from config qa.mask; {} keeps the in-code defaults
    (which mirror config.yaml)."""
    if not mask_cfg:
        return {}
    return {"k": mask_cfg.get("k", 4.0), "blur": mask_cfg.get("blur", 6.0)}


def object_mask(luma: np.ndarray, k: float = 4.0, blur: float = 6.0):
    """Mask of the extended subject: smooth to suppress point stars, then threshold
    above the (robust) background. Returns (mask, bg_median, bg_std).
    Defaults mirror config qa.mask (thread via _mask_kw); shared by QA and the
    stretch solver's probe -- one mask, or the solve and the gates diverge."""
    sm = np.asarray(
        Image.fromarray(np.clip(luma, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(blur))
    ).astype(np.float64)
    _, med, std = sigma_clipped_stats(sm, sigma=3.0, maxiters=5)
    return sm > med + k * std, float(med), float(std)


def _feat(luma: np.ndarray, size=(180, 320)) -> np.ndarray:
    """Downsampled, high-passed, zero-mean unit-norm feature for star-pattern
    correlation. High-pass isolates point sources so processing/brightness
    differences between an output and its reference don't dominate."""
    im = Image.fromarray(np.clip(luma, 0, 255).astype(np.uint8)).resize(size)
    base = np.asarray(im).astype(np.float64)
    blur = np.asarray(im.filter(ImageFilter.GaussianBlur(3))).astype(np.float64)
    hp = np.clip(base - blur, 0, None)
    hp -= hp.mean()
    n = np.linalg.norm(hp)
    return hp / n if n else hp


def orientation_match(out_luma: np.ndarray, ref_luma: np.ndarray, margin: float = 0.03):
    """Cross-correlate the star pattern against the reference under 4 orientations.
    `ok` unless a flipped/rotated orientation beats identity by more than `margin`
    (would have caught the ROWORDER vertical flip)."""
    a = _feat(out_luma)
    b = _feat(ref_luma)
    variants = {
        "identity": lambda x: x,
        "flipud": np.flipud,
        "fliplr": np.fliplr,
        "rot180": lambda x: np.flipud(np.fliplr(x)),
    }
    cc = {name: float((a * f(b)).sum()) for name, f in variants.items()}
    best = max(cc, key=cc.get)
    ok = cc["identity"] >= max(cc.values()) - margin
    return {"best": best, "ok": bool(ok), "correlations": {n: round(v, 3) for n, v in cc.items()}}


def star_count(image_path: str, tools: ToolPaths | None = None):
    """(count, fwhm) via Siril findstar; (None, None) if tools unavailable."""
    if tools is None:
        return None, None
    from .adapters.siril import run_siril

    d = os.path.dirname(os.path.abspath(image_path))
    n = os.path.basename(image_path)
    try:
        out = run_siril([f'cd "{d}"', f'load "{n}"', "findstar"], tools)
    except ExternalToolTimeout:
        # M2/H1: a bounded-runner timeout is a real production failure, never a
        # silent "measurement unavailable" -- it must propagate so the caller
        # (score/qa_target) surfaces it as an actionable target failure, not a
        # fail-closed gate on a measurement that simply never ran.
        raise
    except Exception:
        return None, None
    m = re.findall(r"Found (\d+) Gaussian profile stars.*?FWHM ([\d.]+)", out)
    return (int(m[-1][0]), float(m[-1][1])) if m else (None, None)


# --- masked metrics -----------------------------------------------------------
def _pct(a, p):
    return float(np.percentile(a, p)) if a.size else None


def pegged_chroma(rgb: np.ndarray, mask: np.ndarray) -> tuple[float, float]:
    """(pegged_frac, saturation_median) of the masked body: HSV-style saturation
    (max-min)/max per pixel, over body pixels bright enough to matter (V >= 0.2).

    The target-agnostic "neon color" rail metric: a pixel with one channel near
    ceiling and the others near floor has S ~ 1 regardless of WHICH channel leads,
    so honest single-band dominance (LP Halpha red, OIII teal) with real hue texture
    passes, while saturation cranked to the rail fails. M8 overcook evidence:
    healthy pre-GHS body 0% pegged / S median 0.47; overcooked final 32% / 0.89."""
    body = rgb[mask]
    if not body.size:
        return 0.0, 0.0
    mx = body.max(axis=1)
    mn = body.min(axis=1)
    sat = (mx - mn) / np.maximum(mx, 1e-9)
    vis = mx >= 0.2 * 255  # ignore near-black body pixels (S is noise there)
    if not vis.any():
        return 0.0, 0.0
    return float((sat[vis] >= 0.90).mean()), float(np.median(sat[vis]))


def core_star_saturation(
    rgb: np.ndarray, luma: np.ndarray, mask: np.ndarray, core_pct: float = 95.0, star_vmin: float = 0.2
) -> tuple[float, float, float]:
    """(core_saturation_median, star_saturation_median, core_value_median).

    core = the brightest `core_pct`-percentile of body-mask pixels by luma. We report
    BOTH its chroma and its luminance, because low saturation ALONE cannot tell a pale
    LUMINOUS core (high value, low S -- what we want) from a DEAD GRAY one (low value,
    low S): the desired result is high luminance with controlled chroma. `core_value_
    median` is the median luma (0-255) over the core region; `core_saturation_median`
    is its HSV-style S=(max-min)/max. (Both telemetry -- WARN-only, never a hard gate.)

    stars = pixels OUTSIDE the body mask bright enough to be field stars (per-pixel
    max channel >= star_vmin). The body mask blurs point sources away by design, so
    star colour needs this distinct region. Catches faint stars tinted by the
    nebula's saturation.
    """

    def _median_s(px: np.ndarray) -> float:
        if not px.size:
            return 0.0
        mx = px.max(axis=1).astype(np.float64)
        mn = px.min(axis=1).astype(np.float64)
        return float(np.median((mx - mn) / np.maximum(mx, 1e-9)))

    core_sat, core_val = 0.0, 0.0
    if mask.any():
        thr = float(np.percentile(luma[mask], core_pct))
        core_sel = mask & (luma >= thr)
        core_sat = _median_s(rgb[core_sel])
        core_lum = luma[core_sel]
        core_val = float(np.median(core_lum)) if core_lum.size else 0.0
    star_med = _median_s(rgb[(~mask) & (rgb.max(axis=-1) >= star_vmin * 255)])
    return round(core_sat, 3), round(star_med, 3), round(core_val, 1)


def measure_render(path: str, mask_cfg: dict | None = None, core_pct: float = 95.0, star_vmin: float = 0.2) -> dict:
    rgb, luma, size = load_rgb_luma(path)
    mask, bgmed, _ = object_mask(luma, **_mask_kw(mask_cfg))
    body, bg = luma[mask], luma[~mask]
    pegged, sat_med = pegged_chroma(rgb, mask)
    core_sat_med, star_sat_med, core_val_med = core_star_saturation(rgb, luma, mask, core_pct, star_vmin)
    return {
        "dimensions": [size[0], size[1]],
        "envelope_area_frac": round(float(mask.mean()), 4),
        "body_p90": round(_pct(body, 90), 1) if body.size else None,
        "body_p95": round(_pct(body, 95), 1) if body.size else None,
        "body_rgb": [round(float(rgb[..., i][mask].mean()), 1) for i in range(3)] if body.size else None,
        "background_median_outside_mask": round(float(np.median(bg)), 2) if bg.size else None,
        # background-crush metric: sky pixels at/near pure black OUTSIDE the subject
        # mask (the masked version of the old global_black_frac false-alarm lesson)
        "near_black_frac_outside_mask": round(float((bg <= 5).mean()), 4) if bg.size else 0.0,
        "core_sat_frac": round(float((body >= 254).mean()), 5) if body.size else 0.0,
        "pegged_chroma_frac_body": round(pegged, 4),
        "body_saturation_median": round(sat_med, 3),
        "core_saturation_median": core_sat_med,
        "core_value_median": core_val_med,
        "star_saturation_median": star_sat_med,
        "global_black_frac": round(float((luma <= 5).mean()), 4),
    }


# --- scoring ------------------------------------------------------------------
def score(
    render: str,
    ref_default: str | None = None,
    ref_aesthetic: str | None = None,
    star_floor: int | None = None,
    expected_dims=None,
    core_clip_max: float = 0.01,
    star_source: str | None = None,
    tools: ToolPaths | None = None,
    mask_cfg: dict | None = None,
) -> dict:
    """Full scorecard: metrics + hard gates + soft scores + warnings. Tone/orientation
    are read from `render` (the JPG); star count is read from `star_source` if given
    (the 16-bit TIFF master gives the truthful count -- the lossy JPG undercounts)."""
    m = measure_render(render, mask_cfg=mask_cfg)
    _, luma, _ = load_rgb_luma(render)
    hard, warn, scores = [], [], {}

    cnt, fwhm = star_count(star_source if star_source and os.path.exists(star_source) else render, tools)
    m["star_count"], m["fwhm"] = cnt, fwhm
    # Fail-closed (M2): an explicit ACTIVE star_floor with no measurement (findstar
    # failed) is a hard failure, never a silent pass -- mirrors core.qa_rules.
    # evaluate_gates' star_count policy for the same reason.
    if star_floor is not None and cnt is None:
        hard.append("star_count unavailable: findstar measurement failed")
    elif star_floor is not None and cnt < star_floor:
        hard.append(f"star_count {cnt} < floor {star_floor}")

    if expected_dims is not None and m["dimensions"] != list(expected_dims):
        hard.append(f"dimensions {m['dimensions']} != expected {list(expected_dims)}")

    if m["core_sat_frac"] > core_clip_max:
        hard.append(f"core blown: {m['core_sat_frac']:.3%} of body saturated (> {core_clip_max:.1%})")

    if ref_default:
        _, refl, _ = load_rgb_luma(ref_default)
        om = orientation_match(luma, refl)
        m["orientation"] = om
        if not om["ok"]:
            hard.append(f"orientation '{om['best']}' vs reference (flipped/rotated): {om['correlations']}")
        rmask, _, _ = object_mask(refl, **_mask_kw(mask_cfg))
        ref_area = float(rmask.mean())
        scores["envelope_area_ratio"] = round(m["envelope_area_frac"] / ref_area, 3) if ref_area else None

    if ref_aesthetic:
        _, refa, _ = load_rgb_luma(ref_aesthetic)
        amask, _, _ = object_mask(refa, **_mask_kw(mask_cfg))
        abody = refa[amask]
        ap90, ap95 = _pct(abody, 90), _pct(abody, 95)
        m["aesthetic_body_p90"], m["aesthetic_body_p95"] = round(ap90, 1), round(ap95, 1)
        if m["body_p90"] and ap90:
            scores["body_p90_ratio"] = round(m["body_p90"] / ap90, 3)
        if m["body_p95"] and ap95:
            scores["body_p95_ratio"] = round(m["body_p95"] / ap95, 3)

    if m["global_black_frac"] > 0.6:
        warn.append(
            f"global black fraction {m['global_black_frac']:.0%} "
            "(WARN ONLY -- mask the object; this is often the envelope)"
        )

    return {
        "render": os.path.basename(render),
        "metrics": m,
        "scores": scores,
        "hard_fails": hard,
        "warnings": warn,
        "passed": not hard,
    }


def input_star_metrics(linear_fit: str, workdir: str, tools: ToolPaths | None = None) -> dict:
    """The INPUT's intrinsic star quality, measured on the linear master -- the bound
    on what any stretch can deliver (source-limited vs pipeline-limited attribution).
    findstar is Siril's registration star detector and is designed for linear frames,
    so try it directly; if the result is degenerate (a faint field buried at linear
    levels), re-probe on a FIXED autostretch snapshot (deterministic, snapshot-only,
    never touches the pipeline data). {} when nothing could be measured."""
    from .adapters.siril import run_siril

    cnt, fwhm = star_count(linear_fit, tools)
    if cnt and cnt >= 20:
        return {"star_count": cnt, "fwhm_px": fwhm, "measured_on": "linear"}
    stem = os.path.splitext(os.path.basename(linear_fit))[0]
    probe = f"{stem}_fsprobe"
    try:
        run_siril([f'cd "{workdir}"', f'load "{stem}"', "autostretch", f"save {probe}"], tools)
        cnt, fwhm = star_count(os.path.join(workdir, probe + ".fit"), tools)
    except ExternalToolTimeout:
        # Same re-raise reasoning as star_count above -- a timeout from either the
        # direct findstar probe (star_count call, above, unguarded) or this
        # autostretch probe must not be swallowed into an empty {} measurement.
        raise
    except Exception:
        return {}
    finally:
        try:
            os.remove(os.path.join(workdir, probe + ".fit"))
        except OSError:
            pass  # best-effort: incidental scratch cleanup (CHANGE_SAFETY rule 5)
    if cnt is None:
        return {}
    return {"star_count": cnt, "fwhm_px": fwhm, "measured_on": "autostretch_probe"}


def attribute_fwhm(render_fwhm: float | None, input_fwhm: float | None, scaled_ceiling: float | None) -> str | None:
    """Attribute a failing FWHM gate (CLAUDE.md §13 / the generalization principle:
    output quality should track input quality). None when the gate isn't failing or
    the input measurement is missing; 'source-limited' when the INPUT already exceeds
    the (pixel-scale-scaled) ceiling -- no processing can sharpen seeing/focus;
    'pipeline-limited' when the input was under the ceiling but the render is not
    (the pipeline bloated the stars)."""
    if render_fwhm is None or scaled_ceiling is None or render_fwhm <= scaled_ceiling:
        return None
    if input_fwhm is None:
        return None
    return "source-limited" if input_fwhm >= scaled_ceiling else "pipeline-limited"


def apply_fwhm_attribution(
    gate_fails: list[str], render_fwhm: float | None, input_fwhm: float | None, scaled_ceiling: float | None
) -> tuple[list[str], str | None, str | None]:
    """PURE: (filtered_gate_fails, warning_or_None, attribution_or_None). A
    SOURCE-limited fwhm fail moves from the hard fails to a tagged warning -- a
    permanently red QA on soft source data trains people to ignore QA, while the
    warning preserves the fact. Pipeline-limited stays a hard fail."""
    attr = attribute_fwhm(render_fwhm, input_fwhm, scaled_ceiling)
    if attr != "source-limited":
        return gate_fails, None, attr
    kept = [f for f in gate_fails if not f.startswith("fwhm ")]
    moved = [f for f in gate_fails if f.startswith("fwhm ")]
    warning = None
    if moved:
        warning = (
            f"{moved[0]} (SOURCE-LIMITED: input FWHM {input_fwhm:.2f}px already exceeds "
            f"the scaled ceiling {scaled_ceiling} -- data, not pipeline)"
        )
    return kept, warning, attr


# --- absolute (reference-free) metrics ---------------------------------------
def absolute_metrics(
    render_tif: str,
    linear_fit: str | None = None,
    starless_fit: str | None = None,
    tools: ToolPaths | None = None,
    mask_cfg: dict | None = None,
) -> dict:
    """Reference-FREE physical quality, measured where each is meaningful (ROADMAP
    "North star"). READ-ONLY baseline -- no gates/thresholds yet (set those once we
    have a nebula + globular + galaxy to calibrate against; one nebula would overfit).
    Each value is tagged with its source. Missing artifacts -> None, not an error."""
    from .inspect import measure

    out: dict = {}

    # sharpness / stars -- from the 16-bit TIFF master (findstar). Label with the
    # ACTUAL file: qa_target passes the stars-on master for showcase renders, and a
    # hardcoded "final.tif" here misattributed that depth measurement.
    cnt, fwhm = star_count(render_tif, tools)
    src = os.path.basename(render_tif)
    out["star_count"] = {"value": cnt, "source": src, "better": "higher"}
    out["fwhm_px"] = {"value": fwhm, "source": src, "better": "lower"}

    # noise floor / dynamic range / clipping -- from the LINEAR master (physical,
    # pre-stretch). bg/noise are scale-aware normalized [0,1].
    if linear_fit and os.path.exists(linear_fit):
        lm = measure(linear_fit)
        sat = max(c["sat_frac"] for c in lm["per_channel"].values())
        out["noise_floor"] = {"value": lm["bgnoise"], "source": "linear master", "better": "lower"}
        out["dynamic_range"] = {"value": lm["dynamic_range"], "source": "linear master", "better": "higher"}
        out["clip_frac"] = {"value": round(sat, 6), "source": "linear master", "better": "lower"}
        # object SNR: (object median - sky) / sky noise, mask from the stretched render
        try:
            _, luma, _ = load_rgb_luma(render_tif)
            mask, _, _ = object_mask(luma, **_mask_kw(mask_cfg))
            import numpy as np
            from astropy.io import fits

            data = fits.open(linear_fit)[0].data.astype("float64")
            from .measure import channels as _channels
            from .measure import norm_factor as _norm_factor

            nf = _norm_factor(data)
            ll = np.mean([ch for _, ch in _channels(data)], axis=0) / nf
            if ll.shape == mask.shape and lm["bgnoise"]:
                snr = (float(np.median(ll[mask])) - lm["background"]) / lm["bgnoise"]
                out["object_snr"] = {"value": round(snr, 1), "source": "linear+mask", "better": "higher"}
        except Exception:
            pass

    # faint extended structure recovered -- from the starless FITS (depth)
    if starless_fit and os.path.exists(starless_fit):
        from .measure import measure_extended_signal

        try:
            E = measure_extended_signal(starless_fit)
            out["faint_structure_frac"] = {
                "value": round(E, 4),
                "source": "starless",
                "better": "higher (for extended targets)",
            }
        except Exception:
            pass

    return out


_CATEGORIES = ("nebula", "galaxy", "starfield")


def read_xmp_subject(render: str, cfg: dict, tools: ToolPaths | None = None) -> str:
    """The final's embedded XMP:Subject keywords, "" if unreadable. QA's offline source
    for the engine's RESOLVED category and the capture pixel scale (no re-classify,
    no network).

    Uses the RESOLVED exiftool path when given -- the raw config value is empty by
    default (auto-detect), and running "" silently returned None, which skipped every
    category gate (the overcooked M8 passed QA this way)."""
    import subprocess
    import sys

    exif = (tools.exiftool if tools else None) or (cfg.get("tools") or {}).get("exiftool") or "exiftool"
    # CREATE_NO_WINDOW: without it, a --windowed frozen app (no console of its own) gets a
    # flashing conhost.exe window for this call -- same reasoning as adapters/_external_process.py.
    win_kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform.startswith("win") else {}
    try:
        return subprocess.run(
            [exif, "-s3", "-XMP:Subject", render],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            **win_kwargs,
        ).stdout
    except Exception:
        return ""


def category_from_subject(subject: str) -> str | None:
    """The engine's resolved category (incl. the catalog prior's galaxy call) from the
    embedded keywords. None if absent/ambiguous."""
    found = [c for c in _CATEGORIES if c in subject]
    return found[0] if len(found) == 1 else None


def pixscale_from_subject(subject: str) -> float | None:
    """The capture pixel scale from the embedded "<x> arcsec/px" keyword (written from
    the raw FITS WCS at export). None when the render predates the keyword or had no WCS."""
    m = re.search(r"([\d.]+)\s*arcsec/px", subject)
    return float(m.group(1)) if m else None


def solver_warnings(stretch_solve: dict | None) -> list[str]:
    """PURE: human warnings from the run's solve telemetry. Pinned bounds, the
    bp-invariant bound, and plain non-convergence (a small/jumpy subject mask can
    make the probe overshoot -- M100's 6' disk achieved 88 vs target 72) are all
    'never silent' signals; the render is still gated normally."""
    ss = stretch_solve or {}
    pin = ss.get("pinned") or {}
    out = []
    if pin.get("mtf"):
        out.append(
            f"stretch solver PINNED at mtf {pin['mtf']} bound -- target "
            f"{ss.get('metric')} {ss.get('target')} unreachable within "
            f"engine.stretch.solver bounds (achieved {ss.get('achieved')})"
        )
    elif pin.get("mtf_converged") is False:
        out.append(
            f"stretch solver did NOT converge: {ss.get('metric')} achieved "
            f"{ss.get('achieved')} vs target {ss.get('target')}"
        )
    if ss.get("nonmonotonic"):
        out.append(
            "stretch solver probe trace was NON-MONOTONIC (metric not a stable function "
            "of mtf) -- the conservative best-valid-probe fallback shipped instead of a "
            "converged claim (Diagnosis 5)"
        )
    if pin.get("bp"):
        out.append(
            "two-point solve PINNED at the bp < sky-median bound -- sky "
            f"{ss.get('sky')} above ceiling and cannot go darker without clipping the star field"
        )
    return out


def color_warnings(color_note: dict | None) -> list[str]:
    """Scorecard warnings for the shipped colour path (Diagnosis 4 + Owner Gate 3).

    PURE, like `solver_warnings`. Two states must never read as a clean pass:
    a QUALIFIED acceptance shipped a calibration the tool itself hedged, and a
    fallback shipped no calibration at all. `seestar qa` is read long after the
    render, so an accepted-but-uncertain colour has to look different from a
    confident one (the silent-gate lesson)."""
    if not color_note:
        return []
    reason = color_note.get("reason")
    if color_note.get("qualified"):
        return [
            f"colour calibration accepted with LOWER CONFIDENCE ({reason}): the tool flagged its "
            "own solution as imprecise; every other measure was within bounds"
        ]
    if color_note.get("fallback"):
        return [
            f"colour calibration was NOT applied ({reason}): this photo keeps its native colour, "
            "which can leave a cast the calibration would have corrected"
        ]
    return []


def read_run_notes(proc_dir: str, target: str, notes_name: str | None = None) -> dict | None:
    """The run's sidecar notes ({target}_notes.json by default, written by run_target):
    solve telemetry (pinned bounds), input quality, treatment decisions. None when absent
    (renders that predate the sidecar) -- QA then simply skips the notes-driven checks.

    `notes_name` lets a namespace-aware caller (`qa_target`, via `_resolve_namespace`)
    point this at the CERTIFIED or UNCERTIFIED identity's own notes sidecar; the
    default preserves the pre-namespace legacy filename for every existing caller."""
    name = notes_name if notes_name is not None else f"{target}_notes.json"
    p = os.path.join(proc_dir, name)
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def profile_from_subject(subject: str) -> str | None:
    """The render's profile from the embedded "profile:<name>" keyword. Lets QA apply
    profile-conditional gates offline (the sky gray-wash gate is skipped for
    documentation, whose full-DR promise legitimately brightens the sky)."""
    m = re.search(r"profile:([a-z]+)", subject)
    return m.group(1) if m else None


def gate_scale_factors(gates_cfg: dict, pixscale: float | None, dims=None) -> dict:
    """Resolution adaptation for gate thresholds (S30 vs S30 Pro). Thin DEPRECATED
    dict-in/dict-out wrapper over the typed `core.qa_rules.scale_factors`.

    New code should call `core.qa_rules.scale_factors` on a `GateThresholds`
    value built with `io.gate_thresholds_from_cfg`.
    """
    fwhm, area = qa_rules_scale_factors(gate_thresholds_from_cfg(gates_cfg), pixscale, dims)
    return {"fwhm": fwhm, "area": area}


def faint_structure_from_render(starless_render: str) -> tuple[float | None, str | None]:
    """(faint_structure_frac, warning) from a STARLESS raster deliverable (tif/png):
    fraction of luma above background+3sigma. The starless FITS is cleaned, but the
    starless TIFF is kept -- this lets QA GATE faint structure for extended targets.
    Tracks the FITS measure closely (M17 0.080 tif vs 0.082 fits).

    This input is OPTIONAL (a non-extended target never has one, and an older render
    may predate it): a MISSING file degrades to `(None, None)` silently -- "not
    measured" is the normal, unremarkable case for e.g. a starfield. A PRESENT file
    that cannot be read (e.g. a zero-byte/corrupt stub) is a different, more alarming
    state -- measurement was attempted and failed -- so it degrades to `(None,
    <warning>)`: still never a manufactured failure, but never silent either (spec
    rev 6 T11 fix round 1, item 3)."""
    if not starless_render or not os.path.exists(starless_render):
        return None, None
    try:
        _, luma, _ = load_rgb_luma(starless_render)
    except OSError:
        return None, (
            "starless raster present but unreadable -- faint_structure not measured "
            f"({os.path.basename(starless_render)})"
        )
    _, med, std = sigma_clipped_stats(luma, sigma=3.0, maxiters=5)
    return round(float((luma > med + 3 * std).mean()), 4), None


def support_body_p90(render: str, support_png: str) -> float | None:
    """body_p90 of a render measured over the FROZEN support raster (Diagnosis 5)
    -- the same convention the solver's probes and the run-side hero contract
    used, so a `seestar qa` replay compares like with like. None when the raster
    cannot be applied (unreadable, geometry mismatch, too small); the caller
    then falls back to the object-mask metric it already has."""
    try:
        mask = np.asarray(Image.open(support_png).convert("L")) > 127
        _, luma, _ = load_rgb_luma(render)
    except OSError:
        return None
    if mask.shape != luma.shape or mask.sum() < 50:
        return None
    return float(np.percentile(luma[mask], 90))


def faint_evidence(
    starless_render: str, support_png: str | None, support_recorded: bool
) -> tuple[FaintEvidence | None, str | None]:
    """Compose the SOURCE-RELATIVE faint-structure evidence (Diagnosis 3):
    retention = fraction of the run's measured subject support still visible
    above the output starless' own outside-support background (+3 sigma);
    output_frac = the output's global extended fraction (catastrophe arm).

    Honesty states: `support_recorded=False` (starfield without a cluster, an
    older run) -> (None, ...) and the gate SKIPS "not measured".
    `support_png=None` with support recorded (identity that never publishes the
    raster, e.g. non-canonical out_names) -> "not_measured" plus a warning.
    A promised raster that is unreadable or geometry-mismatched ->
    "missing_artifact" (the gate fails closed; a missing FILE is already a
    hard manifest-referential failure upstream)."""
    if not support_recorded:
        return None, None
    out_frac, warning = faint_structure_from_render(starless_render)
    if not starless_render or not os.path.exists(starless_render):
        # no starless render for this run (e.g. a profile that ships stars-on
        # only): nothing to judge -- honest not-measured, never a fabricated fail
        return FaintEvidence(None, None, "not_measured"), warning
    if support_png is None:
        return (
            FaintEvidence(None, out_frac, "not_measured"),
            "subject support was measured by the run but no durable raster is published for this identity",
        )
    if not os.path.exists(support_png):
        return (
            FaintEvidence(None, out_frac, "missing_artifact"),
            f"support raster missing: {os.path.basename(support_png)}",
        )
    try:
        mask = np.asarray(Image.open(support_png).convert("L")) > 127
        _, luma, _ = load_rgb_luma(starless_render)
    except OSError as e:
        return FaintEvidence(None, out_frac, "missing_artifact"), f"support/starless unreadable: {e}"
    if mask.shape != luma.shape:
        return (
            FaintEvidence(None, out_frac, "missing_artifact"),
            f"support raster geometry {mask.shape} does not match starless {luma.shape}",
        )
    if not mask.any() or mask.all():
        return FaintEvidence(None, out_frac, "missing_artifact"), "support raster degenerate (empty or full-frame)"
    outside = luma[~mask]
    _, med, std = sigma_clipped_stats(outside, sigma=3.0, maxiters=5)
    retention = round(float((luma[mask] > med + 3 * std).mean()), 4)
    return FaintEvidence(retention, out_frac, "valid"), warning


def absolute_gates(
    metrics: dict,
    faint: FaintEvidence | None,
    category: str | None,
    gates_cfg: dict,
    pixscale: float | None = None,
    profile: str | None = None,
    input_star_count: int | None = None,
) -> list[str]:
    """Category-aware hard gates on absolute metrics; returns fail strings.

    Thin DEPRECATED dict-in/list-out wrapper over the typed
    `core.qa_rules.evaluate_gates`. `qa_target` builds the typed report itself.
    Unknown category or a missing metric skips that gate instead of false-failing
    -- EXCEPT `star_preservation`/`fwhm`, which fail closed when their threshold
    is active but a required measurement is unavailable (M2, 2026-07-31): see
    `core.qa_rules.evaluate_gates`. `input_star_count` is the INPUT_STARS
    evidence the source-relative star gate compares against (Diagnosis 2).
    """
    cat = Category(category) if category is not None else Category.UNKNOWN
    report = evaluate_gates(
        render_metrics_from_dict(metrics),
        faint,
        cat,
        gate_thresholds_from_cfg(gates_cfg),
        pixscale,
        profile,
        input_star_count=input_star_count,
    )
    return [g.detail for g in report.gates if g.verdict is Verdict.FAIL]


def _resolve_stars_on_tif(proc_dir: str, target: str, final_tif: str, manifest: dict | None = None) -> str:
    """Which TIFF holds the full star-depth master to measure star_count/fwhm from.

    Prefers the run manifest's own "stars-on" deliverable mapping (authoritative for
    THIS run) over guessing by filename -- a folder can carry a stale `_stars-on.tif`
    left over from an older run/naming convention. (2026-08-01: qa_target silently
    re-measured a 2026-07-15 leftover file this way and reported its stale numbers
    -- a real fwhm "failure" that didn't reproduce at all against the actual fresh
    render -- as if they belonged to a brand-new run, purely because a same-named
    `_stars-on.tif` happened to still exist on disk.) Falls back to the old filename
    guess only when no manifest exists to consult: a pre-manifest legacy render, or
    a standalone `seestar qa` scan of a directory with no prior `seestar run` in it.

    `manifest`, when given (a namespace-aware caller like `qa_target` already parsed
    the CERTIFIED or UNCERTIFIED manifest via `_resolve_namespace`), is used AS-IS --
    no re-read. When omitted (every pre-namespace caller/test), this reads the legacy
    `{target}_run.json` itself, exactly as before."""
    if manifest is None:
        manifest_path = os.path.join(proc_dir, f"{target}_run.json")
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, encoding="utf-8") as f:
                    manifest = json.load(f)
            except (OSError, json.JSONDecodeError):
                manifest = None
    if manifest is not None:
        for d in manifest.get("deliverables") or []:
            if d.get("variant") == "stars-on" and d.get("tif"):
                return d["tif"]
        return final_tif  # this run's manifest is authoritative and has no stars-on variant
    stars_on_guess = os.path.join(proc_dir, f"{target}_stars-on.tif")
    return stars_on_guess if os.path.exists(stars_on_guess) else final_tif


def _hero_out_name(manifest: dict, target: str, certified: bool) -> str:
    """Which `out_name` this manifest's HERO deliverable actually belongs to --
    found by matching a deliverable's own basename against every allowed
    out_name's published hero base (spec rev 6 T11 fix round 1, item 5), never
    assumed to be "final". Fixes the certified non-final out_name case (`seestar
    run --profile natural` then `seestar qa`): the old hardcoded "final"
    manufactured two false `publication-referenced file missing: M_final.*`
    hard fails against a manifest that actually published `M_natural.*`.
    "final" is tried first (today's overwhelmingly common case, and the only one
    that can collide with a fork label via the canon star-reduced/stars-on hero
    variant -- see `orchestrate.py::_variant_for`). A manifest with no matching
    deliverable (empty/legacy/malformed) falls BACK to "final", exactly
    `published_base`'s prior unconditional default."""
    basenames: set[str] = set()
    for d in manifest.get("deliverables") or []:
        if not isinstance(d, dict):
            continue
        for ext in ("tif", "png", "jpg"):
            val = d.get(ext)
            if val:
                basenames.add(os.path.basename(val))
    for candidate in sorted(allowed_out_names(), key=lambda n: (n != "final", n)):
        hero_base = published_base(target, candidate, certified)
        if any(f"{hero_base}.{ext}" in basenames for ext in ("tif", "png", "jpg")):
            return candidate
    return "final"


def _resolve_namespace(proc_dir: str, target: str) -> dict:
    """Which publication namespace (certified/uncertified/legacy) this directory's
    siblings belong to, plus every sibling filename that follows from that
    resolution (spec `2026-08-13-honest-publication-semantics-design.md` §7).

    Tries the CERTIFIED run manifest first (today's shared `{target}_run.json`
    name -- `sidecar_names(target, "final", True)["run"]`), then the UNCERTIFIED
    one (`sidecar_names(target, "final", False)["run"]`). Whichever parses first
    wins. A manifest file that EXISTS but fails to parse (or isn't a JSON
    object) does NOT abort resolution to legacy -- it is skipped, with a warning
    accumulated, and the NEXT candidate (certified first, then uncertified) is
    tried: a corrupt certified manifest must not hide a perfectly valid
    uncertified one (fix round 1, item 1). The pre-namespace legacy defaults are
    reached ONLY when no candidate manifest parses at all -- that branch's
    behavior (and callers reading a single `warning` string) predates this
    accumulation; `warnings` is a list precisely so a directory with BOTH a
    corrupt certified AND a corrupt uncertified manifest reports both, never
    just the first (docs/TENETS.md: "a step that silently changes, skips, or
    degrades is a defect"). No manifest at all (a pre-namespace legacy render,
    or a bare `seestar qa` scan) is the ordinary, unwarned legacy case:
    `certified=None`, today's unnamespaced names, unchanged.

    Return keys: `certified` (bool | None), `manifest` (dict | None), `render_jpg`,
    `final_tif`, `notes_name`, `qa_name`, `starless_tif` (all BASENAMES -- the
    caller joins with `proc_dir`), and `warnings` (list[str], possibly empty)."""
    certified_run = sidecar_names(target, "final", True)["run"]
    uncertified_run = sidecar_names(target, "final", False)["run"]
    certified: bool | None = None
    manifest: dict | None = None
    warnings: list[str] = []

    for run_name, is_certified in ((certified_run, True), (uncertified_run, False)):
        path = os.path.join(proc_dir, run_name)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                parsed = json.load(f)
        except (OSError, json.JSONDecodeError):
            parsed = None
        if not isinstance(parsed, dict):
            warnings.append(
                f"{run_name} exists but could not be parsed as a manifest -- skipping to the next candidate"
            )
            continue
        certified, manifest = is_certified, parsed
        break

    if certified is None:
        return {
            "certified": None,
            "manifest": None,
            "render_jpg": f"{target}_final.jpg",
            "final_tif": f"{target}_final.tif",
            "notes_name": f"{target}_notes.json",
            "qa_name": f"{target}_qa.json",
            "starless_tif": f"{target}_starless.tif",
            "warnings": warnings,
        }

    out_name = _hero_out_name(manifest, target, certified)
    hero_base = published_base(target, out_name, certified)
    sn = sidecar_names(target, out_name, certified)
    starless_base = published_fork_base(target, "starless", certified)
    return {
        "certified": certified,
        "manifest": manifest,
        "render_jpg": f"{hero_base}.jpg",
        "final_tif": f"{hero_base}.tif",
        "notes_name": sn["notes"],
        "qa_name": sn["qa"],
        "starless_tif": f"{starless_base}.tif",
        "warnings": warnings,
    }


def _publication_inconsistency(mo, ro, evaluable: bool, reason: str) -> tuple[list[str], list[str]]:
    """(hard_fail_additions, warning_additions) for an UNCERTIFIED evaluation's
    manifest-vs-recomputed outcome comparison (spec §7). Agreement adds nothing.
    A genuine disagreement is a hard inconsistency ONLY when the recomputation
    could actually evaluate quality; when it could not (category unreadable, or
    -- the missing-hero branch -- no recomputation ran at all, `ro=None`), a
    mismatch is qualified as a warning instead, never asserted as a hard
    inconsistency and never silently reconciled either way (fix round 1, item 4;
    parity with the missing-hero branch, item 6)."""
    if mo == ro:
        return [], []
    if not evaluable:
        return [], [f"publication inconsistency not asserted: recomputation could not evaluate ({reason})"]
    return [f"publication inconsistency: manifest recorded {mo} but current evaluation computes {ro}"], []


def qa_target(proc_dir: str, cfg: dict, target: str | None = None) -> dict:
    """Score the deliverables in `proc_dir` (a target's output directory), pulling references
    from config. Applies the category-aware absolute GATES (Phase C) on top of the
    reference-anchored scorecard. `target` defaults to the directory name (the library driver
    writes `<output_root>/<target>/`); pass it explicitly if the folder is named otherwise.

    Namespace-aware (spec rev 6 T11): resolves every sibling filename through
    `_resolve_namespace`, honestly reports a dual (manifest vs recomputed) outcome
    for an UNCERTIFIED evaluation, and fails closed -- named, never a crash or a
    silent skip -- on any file a resolved manifest references but that is missing."""
    tools = resolve_tools(cfg)
    if target is None:
        target = os.path.basename(os.path.normpath(proc_dir))
    proc = proc_dir
    ns = _resolve_namespace(proc, target)
    manifest = ns["manifest"]
    pre_warnings: list[str] = list(ns.get("warnings") or [])
    if ns["certified"] is True:
        # Both namespaces present: the certified set is always the one reported
        # (spec §7) -- but its uncertified sibling's presence is surfaced, never
        # silently ignored.
        unc_run_name = sidecar_names(target, "final", False)["run"]
        if os.path.exists(os.path.join(proc, unc_run_name)):
            pre_warnings.append(
                f"an uncertified publication set is also present ({unc_run_name}) alongside "
                "the certified set in this directory; only the certified set was evaluated"
            )

    render = os.path.join(proc, ns["render_jpg"])
    final_tif = os.path.join(proc, ns["final_tif"])

    # Fail-closed manifest referential integrity (spec §7 / T11): every file a
    # resolved manifest promises -- deliverable members, qa_artifacts, the plan
    # and notes sidecars -- plus this identity's own hero raster (derived from
    # the resolution above, not necessarily spelled out under this exact name in
    # the manifest) must exist. A tampered or partially-promoted directory fails
    # NAMED and CLOSED. Optional durable inputs (`{target}_lin.fit`,
    # `{target}_starless.fit`, and a non-extended target's starless tif) are
    # never in this set -- their absence is normal and stays "not measured".
    missing_refs: list[str] = []
    if manifest is not None:
        referenced: list[str] = [ns["render_jpg"], ns["final_tif"]]
        for d in manifest.get("deliverables") or []:
            if not isinstance(d, dict):
                continue
            for ext in ("tif", "png", "jpg"):
                val = d.get(ext)
                if val:
                    referenced.append(os.path.basename(val))
        for p in manifest.get("qa_artifacts") or []:
            if isinstance(p, str):
                referenced.append(os.path.basename(p))
        for key in ("plan_sidecar", "notes_sidecar"):
            val = manifest.get(key)
            if isinstance(val, str):
                referenced.append(os.path.basename(val))
        seen: set[str] = set()
        for name in referenced:
            if name in seen:
                continue
            seen.add(name)
            if not os.path.exists(os.path.join(proc, name)):
                missing_refs.append(name)

    if manifest is not None and ns["render_jpg"] in missing_refs:
        # The hero raster itself is gone: nothing downstream can be measured
        # (score() would crash trying to open it) -- fail closed instead, named,
        # with everything else this resolution already knows.
        sc: dict = {
            "render": ns["render_jpg"],
            "metrics": {},
            "scores": {},
            "hard_fails": [f"publication-referenced file missing: {name}" for name in missing_refs],
            "warnings": pre_warnings,
            "passed": False,
        }
        if ns["certified"] is False:
            mo = manifest.get("outcome")
            # No recomputation ran at all (the hero raster is gone) -- ro is a
            # genuine None, never a fabricated "processing_failed" constant
            # (fix round 1, item 6). Parity with the main path (item 4): a
            # mismatch here is qualified, not asserted as a hard inconsistency
            # (this branch is already a hard fail/passed=False on the missing
            # file alone, so the qualification only affects the warnings/
            # hard_fails content, never the overall `passed` verdict).
            hf, warn = _publication_inconsistency(
                mo, None, evaluable=False, reason="hero raster missing, no recomputation ran"
            )
            sc["hard_fails"] = list(sc["hard_fails"]) + hf
            sc["warnings"] = list(sc["warnings"]) + warn
            sc["publication"] = {
                "certified": False,
                "manifest_outcome": mo,
                "recomputed_outcome": None,
            }
        with open(os.path.join(proc, ns["qa_name"]), "w") as f:
            json.dump(sc, f, indent=2)
        return sc

    # Star count is the data-depth metric -> read it from the stars-on master when the
    # showcase _final is star-reduced (else the tamed stars undercount the real depth).
    tif = _resolve_stars_on_tif(proc, target, final_tif, manifest=manifest)
    refs = (cfg.get("references") or {}).get(target, {})

    subject = read_xmp_subject(render, cfg, tools)
    cat = category_from_subject(subject)
    pixscale = pixscale_from_subject(subject)
    profile = profile_from_subject(subject)
    qa_cfg = cfg.get("qa") or {}
    gates_cfg = qa_cfg.get("gates") or {}
    mask_cfg = qa_cfg.get("mask")
    core_ceiling = (gates_cfg.get("core_sat_ceiling") or {}).get(cat) if cat else None
    sc = score(
        render,
        ref_default=refs.get("content"),
        ref_aesthetic=refs.get("aesthetic"),
        star_source=tif,
        core_clip_max=core_ceiling if core_ceiling is not None else 0.01,
        tools=tools,
        mask_cfg=mask_cfg,
    )
    sc["warnings"] = pre_warnings + sc["warnings"]
    if missing_refs:
        # The hero raster exists (checked above) but some OTHER manifest-referenced
        # file (a fork, a sidecar, a qa_artifact) is gone -- named, closed, but not
        # fatal to scoring what IS present.
        sc["hard_fails"] = list(sc["hard_fails"]) + [
            f"publication-referenced file missing: {name}" for name in missing_refs
        ]
        sc["passed"] = False
    # Solve telemetry from the run's sidecar notes: a bound that binds silently is a
    # gate that silently skips, so a PINNED solve (search collapsed onto a configured
    # bound without reaching its target) is surfaced here even though the render may
    # still pass every gate.
    run_notes = read_run_notes(proc, target, notes_name=ns["notes_name"])
    if run_notes:
        ss = (run_notes.get("notes") or {}).get("stretch_solve") or {}
        sc["warnings"].extend(solver_warnings(ss))
        # The IRCUT galaxy/starfield split needs the catalog prior (the E-gate is
        # disabled); a prior that failed leaves filter-only classification, which
        # is exactly how a faint galaxy ships as a starfield (M100, 2026-07:
        # prior "offline" from an SSL failure -> no PCC, cluster GHS on a spiral).
        cp = (run_notes.get("notes") or {}).get("catalog_prior") or {}
        if cp and not cp.get("used") and not cp.get("available", True) and cat in ("galaxy", "starfield"):
            sc["warnings"].append(
                "category resolved WITHOUT the catalog prior "
                f"(source={cp.get('source')}, error={cp.get('error', 'n/a')}) -- "
                "the IRCUT galaxy/starfield split is unreliable; re-run when the prior is reachable"
            )
        col = (run_notes.get("notes") or {}).get("color") or {}
        sc["warnings"].extend(color_warnings(col))
        sc["run_notes"] = {"stretch_solve": ss} if ss else {}
        if cp:
            sc["run_notes"]["catalog_prior"] = {k: cp.get(k) for k in ("available", "used", "source", "error")}
        if col:
            sc["run_notes"]["color"] = {
                k: col.get(k) for k in ("attempted", "accepted", "fallback", "qualified", "reason")
            }

    # Star retention: the star-reduced showcase _final vs the stars-on master. The data
    # depth gate reads stars-on; this catches the LOOK being emptied (GHS + reduction
    # compounding). Soft warning, not a hard gate -- a user 'stars: minimal' preference
    # legitimately drops retention. Only meaningful when stars-on resolved to a file
    # DIFFERENT from final (conservative_star_hero ships stars-on AS final, in which
    # case there's nothing distinct to compare).
    if tif != final_tif and os.path.exists(final_tif):
        son_cnt = sc["metrics"].get("star_count")
        fin_cnt, _ = star_count(final_tif, tools)
        if son_cnt and fin_cnt is not None:
            retention = compute_star_retention(fin_cnt, son_cnt)
            sc["metrics"]["star_retention_final_vs_stars_on"] = retention
            warn_floor = gates_cfg.get("star_retention_warn", 0.35)
            warning = star_retention_warning(retention, fin_cnt, son_cnt, warn_floor)
            if warning:
                sc["warnings"].append(warning)

    # read-only absolute baseline (physical metrics) from durable artifacts if present
    lin = os.path.join(proc, f"{target}_lin.fit")
    starless = os.path.join(proc, f"{target}_starless.fit")
    sc["absolute"] = absolute_metrics(tif, linear_fit=lin, starless_fit=starless, tools=tools, mask_cfg=mask_cfg)

    # Phase C: category-aware HARD gates, now the typed core.qa_rules.evaluate_gates
    # (measurement stays here in qa.py; verdict logic is pure/typed). faint_structure
    # comes from the kept starless TIFF (extended targets only; star fields have no
    # starless / no extended signal). Body-RGB balance (per-channel imbalance in the
    # masked cluster/galaxy body -- satu amplifying the inherent red-giant colour of
    # old globulars into a visible pink cast, e.g. M5's body_rgb [40.8, 29.8, 30.2],
    # R/G 1.37) is a WARN-verdict gate in the same typed report, not a separate check.
    # Source-relative faint structure (Diagnosis 3): judged against the run's
    # OWN measured subject support (published as a manifest qa_artifact), never
    # an inverse-FOV frame fraction. `support_recorded` comes from the run's
    # persisted notes; the raster name from the manifest's qa_artifacts.
    support_note = ((run_notes or {}).get("notes") or {}).get("subject_support") or {}
    support_recorded = bool(support_note.get("valid"))
    support_name = next(
        (
            os.path.basename(a)
            for a in (manifest or {}).get("qa_artifacts") or []
            if isinstance(a, str) and os.path.basename(a).endswith(".png") and "_support" in os.path.basename(a)
        ),
        None,
    )
    support_png = os.path.join(proc, support_name) if support_name else None
    faint, faint_warning = faint_evidence(os.path.join(proc, ns["starless_tif"]), support_png, support_recorded)
    # The SOLVE REGION is a separate raster from the structure support above
    # (owner ruling, 2026-08-14). Resolve it through the same mapping the run
    # used, from the run's OWN persisted provenance -- never by re-deriving an
    # object mask from the final render, which would compare the shipped image
    # against a region the solve never drove.
    solve_note = ((run_notes or {}).get("notes") or {}).get("stretch_solve") or {}
    region_source = solve_note.get("region_source")
    if region_source is None and solve_note.get("support_source"):
        # Compatibility: a run rendered BEFORE the region/support split solved its
        # probes over the structure-support raster and recorded it under the old
        # key. Re-QA'ing that output directory must still compare like with like,
        # so map the legacy provenance onto the raster it actually used.
        region_source = "recovered_structure_support"
    region_name = solve_region_artifact_name(region_source, target)
    region_png = None
    if region_name:
        region_png = next(
            (
                os.path.join(proc, os.path.basename(a))
                for a in (manifest or {}).get("qa_artifacts") or []
                if isinstance(a, str) and os.path.basename(a).startswith(region_name)
            ),
            None,
        )
    if faint_warning:
        sc["warnings"].append(faint_warning)
    cat_enum = Category(cat) if cat else Category.UNKNOWN
    thresholds = gate_thresholds_from_cfg(gates_cfg)
    # Source-relative star preservation (Diagnosis 2): the input evidence is the
    # run's OWN persisted INPUT_STARS measurement (notes.input_quality) -- the
    # same historical source evidence the immediate run QA judged against, so a
    # later `seestar qa` replay reaches the same verdict. Absent notes/count =
    # measurement unavailable = the gate fails closed (never re-measured here:
    # the linear master is scratch and may be gone).
    input_q_early = ((run_notes or {}).get("notes") or {}).get("input_quality") or {}
    input_star_count = input_q_early.get("star_count")
    report = evaluate_gates(
        render_metrics_from_dict(sc["metrics"]),
        faint,
        cat_enum,
        thresholds,
        pixscale,
        profile,
        input_star_count=input_star_count,
    )
    _, sp_record = star_preservation(input_star_count, sc["metrics"].get("star_count"), thresholds)
    sc["star_preservation"] = dataclasses.asdict(sp_record)
    gate_fails = [g.detail for g in report.gates if g.verdict is Verdict.FAIL]
    # never fail silently: an UNKNOWN category (or a disabled gate set) means every
    # category gate was SKIPPED -- surfaced as a warning, never a silent no-op.
    sc["warnings"].extend(report.warnings)
    # body_rgb (and any future WARN-verdict gate) surfaces the same way a WARN always
    # did: a warning string, never a hard fail.
    sc["warnings"].extend(g.detail for g in report.gates if g.verdict is Verdict.WARN)
    if pixscale is None and (gates_cfg.get("reference") or {}).get("pixscale_arcsec"):
        # never scale silently the wrong way: gates ran at the S30 reference scale
        sc["warnings"].append("pixel scale unreadable from EXIF -- QA gates applied UNSCALED (S30 reference)")

    # Source-limited vs pipeline-limited: when the INPUT's own stars already exceed
    # the scaled FWHM ceiling, the fail is the data's, not the pipeline's -- it moves
    # to a tagged warning (the render can never pass, and a permanently red QA
    # trains people to ignore QA). Pipeline-limited bloat stays a hard fail.
    input_q = ((run_notes or {}).get("notes") or {}).get("input_quality") or {}
    ref_ceil = (gates_cfg.get("fwhm_ceiling") or {}).get(cat) if cat else None
    fwhm_scale, area_scale = qa_rules_scale_factors(thresholds, pixscale, sc["metrics"].get("dimensions"))
    scales = {"fwhm": fwhm_scale, "area": area_scale}
    scaled_ceil = round(ref_ceil * scales["fwhm"], 2) if ref_ceil is not None else None
    gate_fails, attr_warning, attr = apply_fwhm_attribution(
        gate_fails, sc["metrics"].get("fwhm"), input_q.get("fwhm_px"), scaled_ceil
    )
    if attr_warning:
        sc["warnings"].append(attr_warning)
    if attr:
        sc["attribution"] = {
            "fwhm": attr,
            "input_fwhm_px": input_q.get("fwhm_px"),
            "render_fwhm_px": sc["metrics"].get("fwhm"),
            "scaled_ceiling": scaled_ceil,
            "input_measured_on": input_q.get("measured_on"),
        }
    sc["category"] = cat
    sc["absolute_gates"] = {
        "category": cat,
        "profile": profile,
        "pixscale_arcsec": pixscale,
        "gate_scales": scales,
        "faint_retention": faint.retention if faint is not None else None,
        "faint_output_frac": faint.output_frac if faint is not None else None,
        "faint_support_state": faint.support_state if faint is not None else "not_measured",
        "thresholds": gates_cfg,
        "fails": gate_fails,
    }
    # Additive typed report alongside the existing scorecard fields.
    sc["gate_report"] = qa_report_to_dict(report)

    # Hero contract (Phase 2): classify the ACTUAL shipped artifact, not the solve
    # probe. solve_converged=None means the solver was skipped/pinned (nothing to
    # converge); contract_delta is None when no solve ran at all. A source-limited
    # fwhm fail is excused here the same way it's excused from gate_fails above --
    # one attribution, reused, never two independent notions of "source-limited".
    ss = (run_notes or {}).get("notes", {}).get("stretch_solve") or {}
    solve_converged = ss.get("pinned", {}).get("mtf_converged") if ss.get("pinned") else None
    contract_delta = None
    if ss.get("achieved") is not None and ss.get("metric"):
        if ss["metric"] == "body_p90":
            # Diagnosis 5: a region-frozen solve measured its probes over the
            # solve-region raster -- the replay's contract metric must be
            # measured over the SAME pixels, or the comparison manufactures false
            # drift. `region_png` is resolved above from the manifest's
            # qa_artifacts via the ONE name mapping the run itself used.
            final_val = None
            if region_png and os.path.exists(region_png):
                final_val = support_body_p90(render, region_png)
            if final_val is None:
                final_val = sc["metrics"].get("body_p90")
        else:
            final_val = sc["metrics"].get("background_median_outside_mask")
        if final_val is not None:
            contract_delta = round(final_val - ss["achieved"], 1)
    # How far the solver's OWN final probe landed from its target (distinct from
    # contract_delta's later-stage drift) -- lets evaluate_hero treat a razor-thin
    # non-convergence as a near miss rather than a blocking failure (2026-08-01).
    solve_miss = (
        abs(ss["achieved"] - ss["target"]) if ss.get("achieved") is not None and ss.get("target") is not None else None
    )
    tol_cfg = (gates_cfg.get("hero_contract_tolerance") or {}).get(cat) if cat else None
    source_limited = frozenset({"fwhm"}) if attr == "source-limited" else frozenset()
    hero = evaluate_hero(
        report, solve_converged, contract_delta, tol_cfg, source_limited_gates=source_limited, solve_miss=solve_miss
    )
    sc["hero"] = hero_report_to_dict(hero)

    # An UNCERTIFIED evaluation reports BOTH outcomes honestly: the manifest's own
    # (HISTORICAL, recorded at commit time) and this pass's RECOMPUTED one -- never
    # asserted equal (spec §7). Disagreement is a hard inconsistency ONLY when the
    # recomputation could actually judge quality. Two distinct ways it might not:
    # an UNKNOWN category (every gate SKIPPED with the unknown-category warning),
    # or a KNOWN category whose gate set is disabled/all-skipped
    # (`core/qa_rules.py`'s `not thresholds.enabled` short-circuit -- `cat is not
    # None` alone missed this, silently asserting the exact hard inconsistency the
    # qualified path exists to prevent). `evaluable` is therefore derived from the
    # gate report itself -- any non-SKIPPED verdict means the recomputation really
    # judged something -- not from category presence alone (fix round 2).
    if ns["certified"] is False:
        mo = manifest.get("outcome") if manifest else None
        ro = sc["hero"]["outcome"]
        sc["publication"] = {"certified": False, "manifest_outcome": mo, "recomputed_outcome": ro}
        evaluable = any(g.verdict is not Verdict.SKIPPED for g in report.gates)
        inconsistency_reason = (
            "category unknown" if cat_enum is Category.UNKNOWN else "category gates disabled or all skipped"
        )
        hf, warn = _publication_inconsistency(mo, ro, evaluable=evaluable, reason=inconsistency_reason)
        if hf:
            sc["hard_fails"] = list(sc["hard_fails"]) + hf
            sc["passed"] = False
        if warn:
            sc["warnings"] = list(sc["warnings"]) + warn

    if gate_fails:
        sc["hard_fails"] = list(sc["hard_fails"]) + gate_fails
        sc["passed"] = False

    with open(os.path.join(proc, ns["qa_name"]), "w") as f:
        json.dump(sc, f, indent=2)
    return sc
