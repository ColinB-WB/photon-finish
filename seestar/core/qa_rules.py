"""Pure QA gate rules.

`seestar.qa` measures render artifacts; this module judges those already-measured
numbers against typed thresholds. Keeping the rules here means gate behavior is
deterministic, unit-testable, and free of file I/O, image libraries, subprocesses,
or target-name conditionals.

Unknown categories are expected on old or malformed renders where EXIF category
metadata cannot be read. In that case, every category-aware gate returns an
explicit SKIPPED verdict plus a warning. The function must short-circuit on
`Category.UNKNOWN` before calling `PerCategory.for_category`, which intentionally
raises on UNKNOWN in the engine-decision path.
"""

from __future__ import annotations

from .types import (
    Category,
    FaintEvidence,
    GateResult,
    GateThresholds,
    HeroOutcome,
    HeroQualityReport,
    QAReport,
    RenderMetrics,
    StarPreservation,
    Verdict,
)

# Gate identifiers in stable report order. "core_saturation", "star_saturation", and
# "body_rgb" are WARN-only (never FAIL); see `body_rgb_warning`.
_GATE_NAMES: tuple[str, ...] = (
    "star_preservation",
    "fwhm",
    "core_sat",
    "faint_structure",
    "near_black",
    "sky_median",
    "pegged_chroma",
    "core_saturation",
    "star_saturation",
    "body_rgb",
)

_UNKNOWN_CATEGORY_WARNING = "category unreadable from EXIF -- category-aware QA gates were SKIPPED"


def scale_factors(
    thresholds: GateThresholds, pixscale: float | None, dims: tuple[int, int] | None
) -> tuple[float, float]:
    """Resolution adaptation for gate thresholds (S30 vs S30 Pro): (fwhm_scale, area_scale).

    Thresholds are locked on a reference instrument
    (`thresholds.reference_pixscale`/`reference_dims`). At gate time, FWHM ceilings
    scale by the pixel-scale ratio (same seeing -> more px at a finer scale) and
    area floors scale by the FOV-area ratio (the same object covers a smaller
    fraction of a wider field). Unknown pixscale, or no configured reference,
    returns identity scaling.
    """
    ref_px = thresholds.reference_pixscale
    ref_dims = thresholds.reference_dims
    if not (pixscale and ref_px):
        return (1.0, 1.0)
    fwhm_scale = ref_px / pixscale
    area_scale = 1.0
    if dims and ref_dims:
        w, h = dims
        rw, rh = ref_dims
        area = w * h * pixscale**2
        if area:
            area_scale = (rw * rh * ref_px**2) / area
    return (fwhm_scale, area_scale)


def star_preservation(
    input_count: int | None, output_count: int | None, thresholds: GateThresholds
) -> tuple[GateResult, StarPreservation]:
    """SOURCE-RELATIVE star preservation (Diagnosis 2, spec 2026-08-12): judge the
    output against the stars measured in THIS capture's linear master, never an
    absolute per-camera floor. Both counts use the same findstar convention
    (qa.star_count / qa.input_star_metrics).

    Fail-closed: with the gate configured, a missing input OR output measurement
    is a named FAIL, never a silent skip. The catastrophe floor is capped at the
    measured input count -- an output is never required to exceed its source.
    A zero-star input passes trivially (nothing to preserve).
    """
    min_ret = thresholds.star_preservation_min_retention
    cat_floor = thresholds.star_preservation_catastrophe_floor
    if min_ret is None and cat_floor is None:
        rec = StarPreservation(input_count, output_count, None, None, None, None)
        return GateResult("star_preservation", Verdict.SKIPPED, "threshold not configured"), rec

    if output_count is None:
        rec = StarPreservation(input_count, None, None, min_ret, cat_floor, None)
        return (
            GateResult(
                "star_preservation",
                Verdict.FAIL,
                "star_preservation unavailable: output findstar measurement failed",
            ),
            rec,
        )
    if input_count is None:
        rec = StarPreservation(None, output_count, None, min_ret, cat_floor, None)
        return (
            GateResult(
                "star_preservation",
                Verdict.FAIL,
                "star_preservation unavailable: input star measurement missing (linear-master findstar)",
            ),
            rec,
        )

    effective_floor = min(cat_floor, input_count) if cat_floor is not None else None
    retention = round(output_count / input_count, 3) if input_count > 0 else None
    rec = StarPreservation(input_count, output_count, retention, min_ret, cat_floor, effective_floor)

    if effective_floor is not None and output_count < effective_floor:
        return (
            GateResult(
                "star_preservation",
                Verdict.FAIL,
                f"star_preservation catastrophic loss: output {output_count} < floor {effective_floor} "
                f"(min of catastrophe {cat_floor} and input {input_count})",
            ),
            rec,
        )
    if min_ret is not None and retention is not None and retention < min_ret:
        return (
            GateResult(
                "star_preservation",
                Verdict.FAIL,
                f"star_preservation {output_count}/{input_count} = {retention} < min retention {min_ret} "
                f"(source-relative; input measured on the linear master)",
            ),
            rec,
        )
    return GateResult("star_preservation", Verdict.PASS, ""), rec


def _skipped_report(category: Category, warnings: tuple[str, ...]) -> QAReport:
    gates = tuple(GateResult(gate=g, verdict=Verdict.SKIPPED, detail="") for g in _GATE_NAMES)
    return QAReport(category=category, gates=gates, warnings=warnings, passed=True)


def _body_rgb_limit(category: Category, thresholds: GateThresholds) -> float | None:
    if category is Category.STARFIELD:
        return thresholds.body_rgb_max_ratio_starfield
    if category is Category.GALAXY:
        return thresholds.body_rgb_max_ratio_galaxy
    return None  # nebula/unknown: no threshold, by design (LP Halpha dominates red)


def evaluate_gates(
    metrics: RenderMetrics,
    faint: FaintEvidence | None,
    category: Category,
    thresholds: GateThresholds,
    pixscale: float | None,
    profile: str | None,
    input_star_count: int | None = None,
) -> QAReport:
    """Category-aware hard gates on absolute metrics, as a typed report.

    These are catastrophe catches, not tuning preferences. A missing per-category
    threshold, an UNKNOWN category, or a missing metric in a family with no ACTIVE
    threshold all produce SKIPPED verdicts, never a false fail and never a silently
    absent gate.

    `star_preservation`/`fwhm` are the one deliberate exception (M2, 2026-07-31):
    when their threshold IS active but a required `findstar` measurement is
    unavailable (`None` -- detection failed, not merely unconfigured), the gate
    FAILS closed instead of skipping. A hard gate that silently skips because
    the instrument that measures it fell over is not a gate, and the shipping
    hero must never report PASS on an un-measured star field. Every other
    metric family is unchanged: a missing metric there still SKIPS.

    `input_star_count` is the INPUT_STARS evidence (findstar on the linear
    master, persisted in the run notes so an immediate run and a later
    `seestar qa` judge from the same source measurement) -- see
    `star_preservation` for the source-relative law it feeds (Diagnosis 2).

    `pixscale` (arcsec/px of the capture) makes the px/frame-fraction gates scale-aware
    (see scale_factors); without it thresholds apply at the S30 reference scale.
    `profile` exempts the gray-wash ("sky_median") gate for documentation (its full-DR
    promise legitimately brightens the sky)."""
    if category is Category.UNKNOWN:
        return _skipped_report(category, (_UNKNOWN_CATEGORY_WARNING,))
    if not thresholds.enabled:
        return _skipped_report(category, ())

    fwhm_scale, _ = scale_factors(thresholds, pixscale, metrics.dimensions)
    cat = category.value
    gates: list[GateResult] = []

    # star preservation: source-relative (Diagnosis 2). Fail-closed (M2) on a
    # missing input OR output measurement when the gate is configured.
    star_gate, _ = star_preservation(input_star_count, metrics.star_count, thresholds)
    gates.append(star_gate)

    # fwhm ceiling (pixel-scale scaled). Same fail-closed exception as star_count.
    ref_ceil = thresholds.fwhm_ceiling.for_category(category) if thresholds.fwhm_ceiling else None
    fwhm = metrics.fwhm
    if ref_ceil is None:
        gates.append(GateResult("fwhm", Verdict.SKIPPED, f"threshold not configured for {cat}"))
    elif fwhm is None:
        gates.append(GateResult("fwhm", Verdict.FAIL, "fwhm unavailable: findstar measurement failed"))
    else:
        ceil = round(ref_ceil * fwhm_scale, 2)
        if fwhm > ceil:
            note = f" (ref {ref_ceil} scaled for pixel scale)" if ceil != ref_ceil else ""
            gates.append(GateResult("fwhm", Verdict.FAIL, f"fwhm {fwhm:.2f} > {cat} ceiling {ceil}{note}"))
        else:
            gates.append(GateResult("fwhm", Verdict.PASS, ""))

    # core_sat ceiling
    csc = thresholds.core_sat_ceiling.for_category(category) if thresholds.core_sat_ceiling else None
    cs = metrics.core_sat_frac
    if csc is None or cs is None:
        gates.append(GateResult("core_sat", Verdict.SKIPPED, ""))
    elif cs > csc:
        gates.append(GateResult("core_sat", Verdict.FAIL, f"core_sat {cs:.4%} > {cat} ceiling {csc:.1%}"))
    else:
        gates.append(GateResult("core_sat", Verdict.PASS, ""))

    # faint_structure: SOURCE-RELATIVE (Diagnosis 3) -- retained supported
    # structure vs the run's own measured reference, never an inverse-FOV frame
    # fraction (a larger field does not owe a larger fraction of nebulosity).
    # "not_measured" skips honestly; a manifest-promised support raster that is
    # missing/mismatched fails closed.
    ret_min = thresholds.faint_retention_min
    cat_floor_f = thresholds.faint_catastrophe_floor
    if category is Category.STARFIELD:
        # Owner ruling (2026-08-14): NOT APPLICABLE to star fields. A globular's
        # signal is primarily stars, and the starless artifact this gate reads
        # intentionally removes them -- M5's support measures bright in the final
        # (p50 44) and at background in its starless (9.4 vs 9.8 outside), so an
        # unchanged, healthy render failed a diffuse-retention test it can never
        # pass. Star fields are held to source-relative STAR preservation (above)
        # instead; halo retention stays reported as diagnostic evidence only and
        # never controls certification.
        gates.append(GateResult("faint_structure", Verdict.SKIPPED, "not applicable — stars are the subject"))
    elif ret_min is None and cat_floor_f is None:
        gates.append(GateResult("faint_structure", Verdict.SKIPPED, "threshold not configured"))
    elif faint is None or faint.support_state == "not_measured":
        gates.append(GateResult("faint_structure", Verdict.SKIPPED, "not measured (no subject support recorded)"))
    elif faint.support_state == "missing_artifact":
        gates.append(
            GateResult(
                "faint_structure",
                Verdict.FAIL,
                "faint_structure unavailable: manifest-referenced support raster missing or mismatched",
            )
        )
    elif cat_floor_f is not None and faint.output_frac is not None and faint.output_frac < cat_floor_f:
        gates.append(
            GateResult(
                "faint_structure",
                Verdict.FAIL,
                f"faint_structure catastrophic: output extended fraction {faint.output_frac} < floor {cat_floor_f}",
            )
        )
    elif ret_min is not None and faint.retention is not None and faint.retention < ret_min:
        gates.append(
            GateResult(
                "faint_structure",
                Verdict.FAIL,
                f"faint_structure retained {faint.retention} < min retention {ret_min} "
                f"(source-relative; vs the run's measured subject support)",
            )
        )
    elif ret_min is not None and faint.retention is None:
        gates.append(GateResult("faint_structure", Verdict.SKIPPED, "retention unmeasured (no starless render)"))
    else:
        gates.append(GateResult("faint_structure", Verdict.PASS, ""))

    # near_black ceiling ("background crushed" -- overcook gate)
    nbc = thresholds.near_black_ceiling.for_category(category) if thresholds.near_black_ceiling else None
    nb = metrics.near_black_frac_outside_mask
    if nbc is None or nb is None:
        gates.append(GateResult("near_black", Verdict.SKIPPED, ""))
    elif nb > nbc:
        gates.append(
            GateResult(
                "near_black",
                Verdict.FAIL,
                f"background crushed: near_black_frac_outside_mask {nb:.2%} > {cat} ceiling {nbc:.1%}",
            )
        )
    else:
        gates.append(GateResult("near_black", Verdict.PASS, ""))

    # sky_median ceiling ("gray wash" -- the too-BRIGHT cousin of near_black).
    # documentation's full-DR promise legitimately brightens the sky -> exempt.
    smc = thresholds.sky_median_ceiling.for_category(category) if thresholds.sky_median_ceiling else None
    sm = metrics.background_median_outside_mask
    if profile == "documentation":
        gates.append(GateResult("sky_median", Verdict.SKIPPED, "skipped: documentation profile (full-DR exempt)"))
    elif smc is None or sm is None:
        gates.append(GateResult("sky_median", Verdict.SKIPPED, ""))
    elif sm > smc:
        gates.append(
            GateResult(
                "sky_median", Verdict.FAIL, f"gray wash: background_median_outside_mask {sm:.1f} > {cat} ceiling {smc}"
            )
        )
    else:
        gates.append(GateResult("sky_median", Verdict.PASS, ""))

    # pegged_chroma ceiling ("neon color" -- overcook gate)
    pcc = thresholds.pegged_chroma_ceiling.for_category(category) if thresholds.pegged_chroma_ceiling else None
    pc = metrics.pegged_chroma_frac_body
    if pcc is None or pc is None:
        gates.append(GateResult("pegged_chroma", Verdict.SKIPPED, ""))
    elif pc > pcc:
        gates.append(
            GateResult(
                "pegged_chroma", Verdict.FAIL, f"neon color: pegged_chroma_frac_body {pc:.2%} > {cat} ceiling {pcc:.1%}"
            )
        )
    else:
        gates.append(GateResult("pegged_chroma", Verdict.PASS, ""))

    # core_saturation -- WARN only: a bright core should burn PALE (low S), not stay
    # saturated. null ceiling -> SKIPPED (bracket-TBD, like nebula body_rgb).
    core_lim = thresholds.core_saturation_ceiling.for_category(category) if thresholds.core_saturation_ceiling else None
    cs2 = metrics.core_saturation_median
    if core_lim is None:
        gates.append(GateResult("core_saturation", Verdict.SKIPPED, ""))
    elif cs2 > core_lim:
        gates.append(
            GateResult(
                "core_saturation",
                Verdict.WARN,
                f"core over-saturated: core_saturation_median {cs2:.2f} > {cat} ceiling {core_lim} "
                f"(bright core not burning pale)",
            )
        )
    else:
        gates.append(GateResult("core_saturation", Verdict.PASS, ""))

    # star_saturation -- WARN only: faint field stars picking up a body-colour tint.
    star_lim = thresholds.star_saturation_ceiling.for_category(category) if thresholds.star_saturation_ceiling else None
    sts = metrics.star_saturation_median
    if star_lim is None:
        gates.append(GateResult("star_saturation", Verdict.SKIPPED, ""))
    elif sts > star_lim:
        gates.append(
            GateResult(
                "star_saturation",
                Verdict.WARN,
                f"field stars tinted: star_saturation_median {sts:.2f} > {cat} ceiling {star_lim}",
            )
        )
    else:
        gates.append(GateResult("star_saturation", Verdict.PASS, ""))

    # body_rgb imbalance -- WARN only, never a hard fail (a user pref legitimately
    # shifts saturation; this flags it, it doesn't block the render).
    ratio_limit = _body_rgb_limit(category, thresholds)
    body_rgb = metrics.body_rgb
    if ratio_limit is None or not body_rgb or min(body_rgb) <= 0:
        gates.append(GateResult("body_rgb", Verdict.SKIPPED, ""))
    else:
        ratio = max(body_rgb) / min(body_rgb)
        if ratio > ratio_limit:
            r, g, b = body_rgb
            gates.append(
                GateResult(
                    "body_rgb",
                    Verdict.WARN,
                    f"body RGB imbalance {ratio:.2f} > {ratio_limit} "
                    f"(R={r:.1f} G={g:.1f} B={b:.1f}): saturation may be overcooking star colours",
                )
            )
        else:
            gates.append(GateResult("body_rgb", Verdict.PASS, ""))

    passed = not any(g.verdict is Verdict.FAIL for g in gates)
    return QAReport(category=category, gates=tuple(gates), warnings=(), passed=passed)


def compute_star_retention(final_count: int, stars_on_count: int) -> float:
    """final _stars_on ratio (showcase star-reduced `_final` vs the stars-on master),
    rounded exactly as qa.qa_target's inline computation was."""
    return round(final_count / stars_on_count, 3)


def star_retention_warning(retention: float, final_count: int, stars_on_count: int, warn_floor: float) -> str | None:
    """Pure port of the star-retention warn in qa.qa_target: showcase's star-reduced
    `_final` keeping under `warn_floor` of the stars-on master's count means the field
    was EMPTIED, not tamed. Soft warning only -- a user 'stars: minimal' preference
    legitimately drops retention. Not folded into evaluate_gates/QAReport because it
    is inherently a TWO-artifact comparison (final vs the stars-on master), not a
    property of one RenderMetrics; qa.py computes `retention` from the two star
    counts and calls this to render the message."""
    if retention >= warn_floor:
        return None
    return (
        f"star field emptied: final keeps {retention:.0%} of stars-on count "
        f"({final_count}/{stars_on_count}, warn floor {warn_floor:.0%})"
    )


# Gates whose FAIL verdict is an aesthetic-only miss on an otherwise-shippable
# hero, never by itself alarming for the end-user-facing outcome (2026-08-01,
# the M8 false-positive finding: a hobby drag-drop tool should never show a
# casual user a failure screen over stars that came out a bit fat -- reserve
# blocking status for output that is actually broken). `seestar validate`'s own
# regression detection never reads HeroOutcome (it diffs raw gate_fails/metrics
# against baselines directly), so softening this is invisible to that check --
# it only changes what a live `seestar run`/GUI/`process` shows a person.
DEFAULT_SOFT_FAIL_GATES: frozenset[str] = frozenset({"fwhm"})


def _soft_eligible(gate: GateResult, soft_fail_gates: frozenset[str]) -> bool:
    """A FAIL is soft only when it's a genuine measured-value miss. A fail-closed
    'the measurement itself broke' detail (M2, e.g. 'fwhm unavailable: findstar
    measurement failed') is never soft -- a broken instrument stays a hard,
    visible failure regardless of which gate it broke on."""
    return gate.gate in soft_fail_gates and "unavailable" not in gate.detail


def evaluate_hero(
    qa: QAReport,
    solve_converged: bool | None,
    contract_delta: float | None,
    contract_tolerance: float | None,
    source_limited_gates: frozenset[str] = frozenset(),
    soft_fail_gates: frozenset[str] = DEFAULT_SOFT_FAIL_GATES,
    solve_miss: float | None = None,
) -> HeroQualityReport:
    """Classify the ACTUAL shipped hero into PASS / SOURCE_LIMITED / PROCESSING_FAILED.

    Two independent failure modes this catches that no single existing check combines:
    - the solver never converged by more than `contract_tolerance`, even if every
      hard gate on the final happens to pass (the NGC7000 lesson -- a non-converged
      solve is never a silent PASS; but a razor-thin miss within the same tolerance
      already used for post-solve drift is not the catastrophe that lesson guards
      against -- 2026-08-01, the M8 finding: a 0.65-of-72 miss that exhausted the
      solver's iteration budget is not a quality problem);
    - the solver converged on its probe, but a LATER stage (saturation, star
      reduction) pulled the actual shipped artifact away from the solved intent by
      more than `contract_tolerance` (the M8 lesson -- convergence on an
      intermediate does not prove the final delivers on it).

    `source_limited_gates` excuses specific FAIL-verdict gate names from being
    "processing" failures (e.g. an fwhm fail already attributed to source-limited
    input) -- but excusing one gate never excuses another that also failed.
    `soft_fail_gates` (default `DEFAULT_SOFT_FAIL_GATES`) similarly keeps listed
    gates out of `processing_failed_gates` when their FAIL is a genuine measured
    miss (see `_soft_eligible`) -- distinct from source-limited: a soft gate is
    still the pipeline's own output, just not one worth alarming a user over.
    """
    fail_results = tuple(g for g in qa.gates if g.verdict is Verdict.FAIL)
    excused = tuple(g.gate for g in fail_results if g.gate in source_limited_gates)
    soft = tuple(
        g.gate for g in fail_results if g.gate not in source_limited_gates and _soft_eligible(g, soft_fail_gates)
    )
    processing_failed = tuple(g.gate for g in fail_results if g.gate not in source_limited_gates and g.gate not in soft)

    drifted = contract_delta is not None and contract_tolerance is not None and abs(contract_delta) > contract_tolerance
    near_miss = solve_miss is not None and contract_tolerance is not None and abs(solve_miss) <= contract_tolerance
    nonconvergence_blocking = solve_converged is False and not near_miss

    reasons: list[str] = []
    if processing_failed:
        reasons.append(f"hard gate(s) failed: {', '.join(processing_failed)}")
    if nonconvergence_blocking:
        reasons.append("stretch solver did not converge")
    elif solve_converged is False:
        # Reachable only via `near_miss` (the `nonconvergence_blocking` branch above
        # took every other non-converged case), and near_miss itself required a
        # measured solve_miss -- a two-hop invariant, stated here so it survives.
        assert solve_miss is not None
        reasons.append(
            f"stretch solver did not fully converge but landed within tolerance "
            f"({abs(solve_miss):.2f} <= {contract_tolerance})"
        )
    if drifted:
        reasons.append(
            f"final hero drifted {contract_delta:+.1f} from the solved intent (tolerance {contract_tolerance})"
        )

    if processing_failed or nonconvergence_blocking or drifted:
        outcome = HeroOutcome.PROCESSING_FAILED
    elif excused:
        outcome = HeroOutcome.SOURCE_LIMITED
        reasons.append(f"source-limited gate(s) excused: {', '.join(excused)}")
    else:
        outcome = HeroOutcome.PASS
    if soft:
        reasons.append(f"soft quality note (non-blocking): {', '.join(soft)}")

    return HeroQualityReport(
        outcome=outcome,
        qa=qa,
        solve_converged=solve_converged,
        contract_delta=contract_delta,
        source_limited_gates=excused,
        processing_failed_gates=processing_failed,
        reasons=tuple(reasons),
        soft_fail_gates=soft,
    )
