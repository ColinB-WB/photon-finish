"""Classification + profile -> Decisions. Pure port of policy.compute_params,
profiles.apply_profile and policy._cap_sharpening. THE single owner of the PCC
decision (no overlay overrides it - 2026-07 M81 fix).

Design rules (locked from the M8 overcook regression, 2026-07):
- Brightness intent is `target_level_scale`, a multiplier on the engine's solved
  stretch target level -- NOT a fixed mtf delta. mtf direction was a historical bug
  source and fixed deltas defeat the per-image solver. `mtf_fallback_delta` exists
  only for the solver-off fallback path (solve_stretch: false).
- `ghs` gates the structure treatments (nebula envelope / galaxy dust lane /
  cluster halo GHS). Profiles that promise a gentle, honest tone (natural,
  documentation) render the base stretch only.
- showcase stays vivid -- it is guarded by the QA overcook gates (near-black
  background, pegged chroma, core clipping), not by taming these overlays.
- NO profile forces pcc (2026-07, M81 red-cast fix): the engine's filter/category
  rule is the single owner of the color-calibration decision. "Native color" is
  already what that rule produces wherever native color is correct; a profile
  override only ever flipped IRCUT galaxies, shipping uncalibrated red.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from .types import (
    Category,
    Classification,
    Decisions,
    EngineDefaults,
    FilterKind,
    GhsParams,
    Measurements,
    Overrides,
    PccBounds,
    PccEvidence,
    PccVerdict,
    SharpeningCap,
    SNRClass,
)


def evaluate_pcc_evidence(evidence: PccEvidence | None, bounds: PccBounds) -> PccVerdict:
    """Deterministic accept/reject of a tool-reported PCC solution (Diagnosis 4).

    The adapter reports evidence; THIS is the one place acceptability is decided
    (spec item 5). Order: every check that makes the solution UNUSABLE or
    UNVERIFIABLE runs first, so a rejection always names the strongest signal.
    Only then is the tool's advisory `imprecise` flag considered, and it
    downgrades the acceptance rather than vetoing it.

    Why `imprecise` is not a veto (Owner Gate 3, 2026-08-14). Siril's text is
    "found an imprecise solution, consider correcting the image gradient first"
    -- advice about gradient, not a claim the coefficients are wrong. Treated as
    a veto it rejected EVERY IRCUT galaxy measured, including the known-good M81
    regression case at 1540 stars, which disabled colour calibration for the
    whole category and shipped the uncalibrated orange galaxy that the 2026-07
    PCC-ownership rule exists to prevent. Native colour is not the demonstrably
    safe result here; it is merely the uncorrected one, and only PCC can fix a
    cast that lives in the star/body signal rather than the sky. So an imprecise
    solution whose every other datum is in bounds is ACCEPTED and marked
    `qualified` -- carried onto plan, notes, EXIF and QA so it is never a silent
    acceptance (Tenet 8: uncertainty is an honest outcome, stated not hidden).

    `few_stars_warning` deliberately still rejects: that gate produced no
    evidence about it, so it keeps its existing conservative behavior."""
    if evidence is None:
        return PccVerdict(accepted=False, reason="no_evidence")
    if not evidence.succeeded:
        return PccVerdict(accepted=False, reason="not_confirmed")
    if evidence.few_stars_warning:
        return PccVerdict(accepted=False, reason="few_stars_solution")
    if evidence.stars_used is None:
        return PccVerdict(accepted=False, reason="stars_unreported")
    if evidence.stars_used < bounds.min_stars:
        return PccVerdict(accepted=False, reason="too_few_stars")
    if evidence.coefficients is None:
        return PccVerdict(accepted=False, reason="coefficients_unreported")
    ks = evidence.coefficients
    if any(k < bounds.coeff_min or k > bounds.coeff_max for k in ks) or max(ks) / min(ks) > bounds.max_ratio:
        return PccVerdict(accepted=False, reason="implausible_coefficients")
    if evidence.imprecise:
        return PccVerdict(accepted=True, reason="imprecise_within_bounds", qualified=True)
    return PccVerdict(accepted=True, reason="evidence_within_bounds")


@dataclass(frozen=True)
class ProfileOverlay:
    pcc: bool | None  # None = keep the engine's filter/category decision
    saturation_scale: float  # multiplier on the per-category engine saturation
    saturation_abs: float | None  # absolute override (documentation); wins over scale
    target_level_scale: float  # multiplier on the solved stretch target level
    blackpoint_scale: float  # multiplier on the engine blackpoint factor (<1 = preserve more shadow)
    mtf_fallback_delta: float  # ONLY when solve_stretch is off (higher = dimmer)
    ghs: bool  # structure treatments (nebula/galaxy/cluster GHS) allowed
    sky_solve: bool  # two-point solve may raise the bp to hold the sky dark (gray-wash guard)
    star_forks: bool  # star-taming variant set allowed (extended targets)
    annotate: bool  # emit the annotated documentation JPG
    denoise_delta: float  # added to the engine denoise strength
    black_lift: bool  # apply engine print.black_lift (paper shadow floor)


PROFILE_OVERLAYS: dict[str, ProfileOverlay] = {
    # showcase: share/social/wow -- engine color decision, vivid but credible,
    # structure treatments on, stars tamed, some noise OK.
    "showcase": ProfileOverlay(
        # pcc=None: the engine's filter/category rule already keeps native color where
        # native color is right (LP Ha, IRCUT globulars). Forcing False here only
        # changed IRCUT galaxies -- the one case that NEEDS calibration (M81 S30 Pro
        # evidence, 2026-07: raw star signal R/G 1.29, shipped orange).
        pcc=None,
        saturation_scale=1.0,
        saturation_abs=None,
        target_level_scale=1.0,
        blackpoint_scale=1.0,
        mtf_fallback_delta=0.0,
        ghs=True,
        sky_solve=True,
        star_forks=True,
        annotate=False,
        denoise_delta=0.0,
        black_lift=False,
    ),
    # natural: honest/true-to-life -- calibrated color where the filter allows,
    # moderate saturation, softer contrast (base stretch only, no GHS),
    # stars-on untouched, preserved background.
    "natural": ProfileOverlay(
        pcc=None,
        saturation_scale=0.6,
        saturation_abs=None,
        # 0.95, bracketed on a solved LP-nebula chain (2026-07): visibly dimmer than
        # showcase (body p90 68 vs 72) with the sky preserved (near-black 0.8% vs the
        # 2% gate). 0.85 measured 5.5% near-black -- a dimmer TARGET dims the whole
        # lift, so "gentler" must not mean "much dimmer" or the background crushes.
        target_level_scale=0.95,
        blackpoint_scale=1.0,
        mtf_fallback_delta=0.04,
        ghs=False,
        sky_solve=True,
        star_forks=False,
        annotate=False,
        denoise_delta=0.0,
        black_lift=False,
    ),
    # documentation: record/science -- photometric color, low saturation, mild
    # stretch preserving full DR, nothing removed, + annotated JPG.
    "documentation": ProfileOverlay(
        pcc=None,
        saturation_scale=1.0,
        saturation_abs=0.1,
        target_level_scale=0.7,
        # full-DR promise: a mild (dim) lift with the standard black point crushes
        # 46% of the sky to near-black; bp x0.97 measured near-black 0.0% with a
        # moderate sky (14/255) -- nothing below the sky is clipped (2026-07 bracket).
        blackpoint_scale=0.97,
        mtf_fallback_delta=0.06,
        ghs=False,
        # full-DR promise: never raise the bp to darken the sky -- documentation
        # deliberately preserves everything above the (lowered) black point.
        sky_solve=False,
        star_forks=False,
        annotate=True,
        denoise_delta=0.0,
        black_lift=False,
    ),
    # print: showcase re-tuned for paper -- slightly lower output, print-safe gamut,
    # strong denoise, lifted black point so shadows survive reflective media.
    "print": ProfileOverlay(
        pcc=None,  # same reasoning as showcase: the engine rule decides
        saturation_scale=0.9,
        saturation_abs=None,
        target_level_scale=0.97,
        blackpoint_scale=1.0,
        mtf_fallback_delta=0.02,
        ghs=True,
        sky_solve=True,
        star_forks=True,
        annotate=False,
        denoise_delta=0.1,
        black_lift=True,
    ),
}


def _apply(d: Decisions, o: Overrides) -> Decisions:
    """Overlay one sparse Overrides onto Decisions (each non-None field replaces the
    same-named Decisions field). `star_forks` is special: an override can only UNDO
    the category rule (AND), never force forks onto a starfield. `profile` is resolved
    upstream (effective_profile) and is deliberately NOT applied here."""
    # A heterogeneous override bag, keyed by Decisions field name and applied
    # through `replace` below -- the value types are exactly the field types.
    ch: dict[str, Any] = {}
    if o.saturation is not None:
        ch["saturation"] = o.saturation
    if o.stretch_target_level is not None:
        ch["stretch_target_level"] = o.stretch_target_level
    if o.denoise_strength is not None:
        ch["denoise_strength"] = o.denoise_strength
    if o.star_reduce_k is not None:
        ch["star_reduce_k"] = o.star_reduce_k
    if o.structure_enabled is not None:
        ch["structure_enabled"] = o.structure_enabled
    if o.star_forks is not None:
        ch["star_forks"] = d.star_forks and o.star_forks  # AND: a pin can only un-do
    if o.closeup_enabled is not None:
        ch["closeup_enabled"] = o.closeup_enabled
    if o.trail_removal_enabled is not None:
        ch["trail_removal_enabled"] = o.trail_removal_enabled
    if o.crop_border_frac is not None:
        ch["crop_border_frac"] = o.crop_border_frac
    if o.bge_smoothing is not None:
        ch["bge_smoothing"] = o.bge_smoothing
    if o.pcc is not None:
        ch["pcc"] = o.pcc
    if o.asinh_d is not None:
        ch["asinh_d"] = o.asinh_d
    if o.blackpoint is not None:
        ch["blackpoint"] = o.blackpoint
    if o.mtf_mid is not None:
        ch["mtf_mid"] = o.mtf_mid
    if o.stretch_target_scale is not None:
        ch["stretch_target_scale"] = o.stretch_target_scale
    return dataclasses.replace(d, **ch) if ch else d


_NO_OVERRIDES = Overrides()  # frozen/immutable -> safe shared default (avoids B008)


def compute_decisions(
    classification: Classification,
    measurements: Measurements,
    engine: EngineDefaults,
    profile: str,
    prefs: Overrides = _NO_OVERRIDES,
    pins: Overrides = _NO_OVERRIDES,
    caller: Overrides = _NO_OVERRIDES,
) -> Decisions:
    # Resolve the effective profile FIRST so a prefs/pins/caller profile override
    # re-enters the overlay (precedence engine < profile < prefs < pins < caller).
    profile = caller.profile or pins.profile or prefs.profile or profile
    try:
        ov = PROFILE_OVERLAYS[profile]
    except KeyError:
        raise ValueError(f"Unknown profile '{profile}'. Available: {', '.join(PROFILE_OVERLAYS)}.") from None

    cat, snr = classification.category, classification.snr_class

    denoise = engine.denoise.for_category(cat)
    if snr is SNRClass.NOISY:
        denoise += engine.denoise_noisy_bonus

    asinh_d = engine.asinh_d.for_category(cat)
    # Provisional black point from the linear-master measurements. The authoritative
    # bp is recomputed on the LINEAR MASTER (post crop/BGE/denoise) just before the
    # stretch, because BGE Subtraction shifts the background median. Kept here so the
    # decision exists and operator overrides still apply.
    blackpoint = round(engine.blackpoint_factor * measurements.background, 6)
    mtf_mid = engine.mtf_mid.for_category(cat)
    # Noisy SNR class -> render dimmer to hide noise. On the solver path that is a
    # scale on the stretch target level (direction-safe); the mtf delta remains only
    # for the solver-off fallback where mtf_mid is used directly.
    target_scale = 1.0
    if snr is SNRClass.NOISY:
        mtf_mid = round(mtf_mid + engine.noisy_mtf_delta, 3)  # solver-off fallback only
        target_scale = engine.noisy_level_scale

    saturation = engine.saturation.for_category(cat)
    # PCC: never for LP (desaturates Ha); off for starfield even on IRCUT because
    # globulars are dominated by old red giants -- PCC calibrates to that population
    # and spreads the redness across the image as if it were a colour imbalance.
    # This rule is the single owner of the color decision -- no profile overrides it
    # (2026-07 M81 fix: showcase forcing pcc=False shipped an uncalibrated red cast
    # on the one case the rule turns PCC on, the IRCUT galaxy).
    pcc = (classification.filter_kind is FilterKind.IRCUT) and (cat is not Category.STARFIELD)

    # Structure treatments attach by CATEGORY; the profile overlay strips them when
    # the profile doesn't allow GHS (natural/documentation render the honest base
    # stretch only). Cluster-halo GHS attaches later in planning, gated by the
    # MEASURED concentration + the same ghs_allowed flag.
    # Galaxies get the GHS dust-lane treatment + a closeup; nebulae get a GHS
    # envelope treatment (no closeup -- full frame), SP set on the body so the lift
    # hits the nebula, not the sky.
    ght: GhsParams | None = None
    if cat is Category.GALAXY:
        ght = engine.galaxy_treatment
    elif cat is Category.NEBULA:
        ght = engine.nebula_treatment  # None when disabled

    # Pixel-scale prior (CLAUDE.md section 13 law). The Seestar is undersampled
    # (~4"/px, read from the CD matrix), so unsharp/deconvolution has no fine detail
    # to resolve and only amplifies noise -> clamp the treatment's unsharp_amount to
    # the configured cap when the MEASURED plate scale says undersampled.
    # Deterministic; prior proposes, measurement disposes.
    sharpening_capped: SharpeningCap | None = None
    px = measurements.pixscale_arcsec
    if ght is not None and px is not None and px >= engine.sharpening_undersampled_arcsec:
        cap = engine.sharpening_unsharp_cap
        if ght.unsharp_amount > cap:
            ght = dataclasses.replace(ght, unsharp_amount=cap)
            sharpening_capped = SharpeningCap(pixscale_arcsec=px, cap=cap)

    # ---- profile overlay (port of apply_profile, same order) ----
    if ov.pcc is not None:
        pcc = ov.pcc
    if ov.saturation_abs is not None:
        saturation = ov.saturation_abs
    elif ov.saturation_scale != 1.0:
        saturation = round(saturation * ov.saturation_scale, 3)
    # Brightness: scale the solved target level (composes with the engine's noisy
    # scale and the user's brightness preference). The mtf delta is dead weight on
    # the solver path (the solve overwrites mtf_mid) and only matters as the
    # solver-off fallback.
    target_scale = round(target_scale * ov.target_level_scale, 3)
    if ov.mtf_fallback_delta:
        mtf_mid = round(mtf_mid + ov.mtf_fallback_delta, 3)
    if not ov.ghs:
        ght = None
    star_forks = cat in (Category.NEBULA, Category.GALAXY) and ov.star_forks
    if ov.denoise_delta:
        denoise = round(denoise + ov.denoise_delta, 3)

    decisions = Decisions(
        category=cat,
        snr_class=snr,
        profile=profile,
        crop_border_frac=engine.crop_border_frac,
        bge_smoothing=engine.bge_smoothing,
        bge_correction=engine.bge_correction,
        denoise_strength=round(denoise, 3),
        pcc=pcc,
        asinh_d=asinh_d,
        blackpoint=blackpoint,
        blackpoint_scale=ov.blackpoint_scale,
        mtf_mid=mtf_mid,
        stretch_target_scale=target_scale,
        saturation=saturation,
        star_forks=star_forks,
        ghs_allowed=ov.ghs,
        ght=ght,
        sky_solve=ov.sky_solve,
        annotate=ov.annotate,
        black_lift=engine.print_black_lift if ov.black_lift else 0.0,
        sharpening_capped=sharpening_capped,
        stretch_target_level=None,
        structure_enabled=True,
        closeup_enabled=True,
        trail_removal_enabled=engine.trail_removal_enabled,
        star_reduce_k=engine.star_reduce_k,
        conservative_star_hero=engine.conservative_star_hero,
    )

    # Controller overlay (precedence engine < profile < prefs < pins < caller). The
    # profile was already resolved into the effective overlay above; the per-field
    # overrides land here.
    for overlay in (prefs, pins, caller):
        decisions = _apply(decisions, overlay)

    # structure_enabled=False strips the GHS treatment entirely (honest base stretch),
    # mirroring run.py's `if not structure_on: params.pop("ght")`. Applied AFTER the
    # overlays so an override that disables structure wins.
    if not decisions.structure_enabled and decisions.ght is not None:
        decisions = dataclasses.replace(decisions, ght=None)

    return decisions
