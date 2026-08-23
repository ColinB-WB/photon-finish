"""Pure recovery-candidate lifecycle: dependency closure, ref remapping, the
accept/recover policy, and the chroma-backoff sub-plan builder (Phase 3B)."""

from seestar.core.recovery import (
    RecoveryAction,
    chroma_backoff_steps,
    decide_candidate,
    downstream_closure,
    remap_for_attempt,
)
from seestar.core.types import (
    ArtifactRef,
    ArtifactRole,
    Category,
    Export,
    GateResult,
    HeroOutcome,
    HeroQualityReport,
    QAReport,
    ScreenBlend,
    SeparateStars,
    StretchRender,
    Verdict,
)

_LINEAR = ArtifactRef("linear_master", ArtifactRole.PRODUCTION)
_STRETCHED = ArtifactRef("stretched", ArtifactRole.PRODUCTION)
_STARLESS = ArtifactRef("starless", ArtifactRole.PRODUCTION)
_STARS = ArtifactRef("stars", ArtifactRole.PRODUCTION)
_REDUCED = ArtifactRef("reduced", ArtifactRole.PRODUCTION)


def _stretch(saturation=0.60):
    return StretchRender(
        input=_LINEAR,
        output=_STRETCHED,
        asinh_d=75,
        blackpoint=0.01,
        mtf_mid=0.08,
        saturation=saturation,
        ght=None,
        black_lift=0.0,
    )


def _separate():
    return SeparateStars(input=_STRETCHED, starless=_STARLESS, stars=_STARS)


def _blend(k=0.45):
    return ScreenBlend(starless=_STARLESS, stars=_STARS, output=_REDUCED, k=k)


def _export():
    return Export(input=_REDUCED, output_base="T_final")


def test_downstream_closure_of_the_stretch_output_includes_everything_after_it():
    steps = (_stretch(), _separate(), _blend(), _export())
    closure = downstream_closure(steps, _STRETCHED)
    assert closure == frozenset(
        {_STRETCHED, _STARLESS, _STARS, _REDUCED} | {r for op in (_export(),) for r in op.outputs}
    )


def test_downstream_closure_of_the_blend_output_is_only_itself_and_export():
    steps = (_stretch(), _separate(), _blend(), _export())
    closure = downstream_closure(steps, _REDUCED)
    assert closure == frozenset({_REDUCED} | {r for op in (_export(),) for r in op.outputs})
    # crucially, does NOT include _STRETCHED/_STARLESS/_STARS -- a star_reduce_k-only
    # change must not force re-running the stretch or star separation.


def test_remap_for_attempt_gives_invalidated_refs_fresh_names_and_leaves_others_alone():
    steps = (_stretch(), _separate(), _blend(), _export())
    invalidated = downstream_closure(steps, _STRETCHED)
    remapped = remap_for_attempt(steps, invalidated, "r1")

    new_stretch, new_separate, new_blend, new_export = remapped
    assert new_stretch.output.name == "stretched_r1"
    assert new_separate.input.name == "stretched_r1"  # consumes the remapped stretch output
    assert new_separate.starless.name == "starless_r1"
    assert new_separate.stars.name == "stars_r1"
    assert new_blend.starless.name == "starless_r1"
    assert new_blend.stars.name == "stars_r1"
    assert new_blend.output.name == "reduced_r1"
    assert new_export.input.name == "reduced_r1"
    assert new_export.output_base == "T_final_r1"
    # saturation/params on the stretch itself are untouched by remapping --
    # remap_for_attempt only renames refs, the caller supplies the new params
    # via dataclasses.replace before calling this function.
    assert new_stretch.saturation == steps[0].saturation


def test_remap_for_attempt_leaves_unrelated_ops_completely_untouched():
    steps = (_stretch(), _separate(), _blend(), _export())
    invalidated = downstream_closure(steps, _REDUCED)  # blend-output-only change
    remapped = remap_for_attempt(steps, invalidated, "r1")

    new_stretch, new_separate, new_blend, new_export = remapped
    assert new_stretch == steps[0]  # untouched: identical dataclass, same repr
    assert new_separate == steps[1]  # untouched
    assert new_blend.output.name == "reduced_r1"
    assert new_export.input.name == "reduced_r1"
    assert new_export.output_base == "T_final_r1"


# ---- Phase 3B: decide_candidate policy + chroma-backoff sub-plan --------------
# (docs/3B-chroma-recovery-design.md, plan §3B/§6). Chroma recovery fires ONLY
# when the sole processing-failed gate is pegged_chroma on a converged solve --
# lowering saturation cannot fix non-convergence (3C) or any other failure, and
# must never mask an independent one (plan §7).


def _hero(outcome, processing_failed_gates=(), solve_converged=True):
    return HeroQualityReport(
        outcome=outcome,
        qa=QAReport(
            category=Category.NEBULA,
            gates=tuple(GateResult(g, Verdict.FAIL, "neon") for g in processing_failed_gates),
            warnings=(),
            passed=(outcome is HeroOutcome.PASS),
        ),
        solve_converged=solve_converged,
        contract_delta=0.0,
        source_limited_gates=(),
        processing_failed_gates=tuple(processing_failed_gates),
        reasons=(),
    )


def test_decide_accepts_pass_and_source_limited():
    for outcome in (HeroOutcome.PASS, HeroOutcome.SOURCE_LIMITED):
        d = decide_candidate(_hero(outcome), attempt=0, max_attempts=2)
        assert d.action is RecoveryAction.ACCEPT


def test_decide_recovers_chroma_only_failure_with_attempts_left():
    hero = _hero(HeroOutcome.PROCESSING_FAILED, processing_failed_gates=("pegged_chroma",))
    assert decide_candidate(hero, attempt=0, max_attempts=2).action is RecoveryAction.RECOVER
    assert decide_candidate(hero, attempt=1, max_attempts=2).action is RecoveryAction.RECOVER


def test_decide_accepts_when_bracket_exhausted():
    # attempt == max_attempts: no attempts remain -> ship best-effort + honest fail.
    hero = _hero(HeroOutcome.PROCESSING_FAILED, processing_failed_gates=("pegged_chroma",))
    assert decide_candidate(hero, attempt=2, max_attempts=2).action is RecoveryAction.ACCEPT


def test_decide_accepts_non_convergence_failure_not_chroma_recoverable():
    # NGC7000 shape: PROCESSING_FAILED via non-convergence, no failed gate. Lowering
    # saturation cannot help -> ACCEPT (honest fail), never a wasted rerender.
    hero = _hero(HeroOutcome.PROCESSING_FAILED, processing_failed_gates=(), solve_converged=False)
    assert decide_candidate(hero, attempt=0, max_attempts=2).action is RecoveryAction.ACCEPT


def test_decide_does_not_recover_chroma_alongside_an_independent_failure():
    # pegged_chroma is failing but so is another gate: chroma backoff must not be
    # used to mask an independent pipeline failure (plan §7).
    hero = _hero(HeroOutcome.PROCESSING_FAILED, processing_failed_gates=("pegged_chroma", "near_black"))
    assert decide_candidate(hero, attempt=0, max_attempts=2).action is RecoveryAction.ACCEPT


def test_decide_does_not_recover_chroma_when_solve_did_not_converge():
    # Even if pegged_chroma is the only failed gate, an unconverged solve is a
    # 3C-level failure lowering saturation can't fix -> ACCEPT.
    hero = _hero(HeroOutcome.PROCESSING_FAILED, processing_failed_gates=("pegged_chroma",), solve_converged=False)
    assert decide_candidate(hero, attempt=0, max_attempts=2).action is RecoveryAction.ACCEPT


def test_aesthetic_backoff_recovers_clean_pass_over_ceiling_with_attempts_left():
    # PASS (neon gate + everything clean) but measured body S over the ceiling -> RECOVER
    hero = _hero(HeroOutcome.PASS)
    d = decide_candidate(hero, attempt=0, max_attempts=2, body_saturation_median=0.82, saturation_ceiling=0.60)
    assert d.action is RecoveryAction.RECOVER
    d1 = decide_candidate(hero, attempt=1, max_attempts=2, body_saturation_median=0.70, saturation_ceiling=0.60)
    assert d1.action is RecoveryAction.RECOVER


def test_aesthetic_backoff_accepts_when_under_ceiling():
    hero = _hero(HeroOutcome.PASS)
    d = decide_candidate(hero, attempt=1, max_attempts=2, body_saturation_median=0.55, saturation_ceiling=0.60)
    assert d.action is RecoveryAction.ACCEPT


def test_aesthetic_backoff_accepts_on_exhaustion_still_pass():
    # bracket exhausted, still over ceiling -> ACCEPT (ships best-effort PASS; the hero
    # is PASS, so no PROCESSING_FAILED -- the terminal distinction is automatic)
    hero = _hero(HeroOutcome.PASS)
    d = decide_candidate(hero, attempt=2, max_attempts=2, body_saturation_median=0.70, saturation_ceiling=0.60)
    assert d.action is RecoveryAction.ACCEPT


def test_aesthetic_backoff_dormant_when_ceiling_none():
    # default saturation_ceiling=None -> aesthetic arm never fires (parity for every
    # render until brackets lock a ceiling)
    hero = _hero(HeroOutcome.PASS)
    d = decide_candidate(hero, attempt=0, max_attempts=2, body_saturation_median=0.99)
    assert d.action is RecoveryAction.ACCEPT


def test_aesthetic_backoff_recovers_source_limited_over_ceiling():
    # A soft capture (e.g. M8) ships SOURCE_LIMITED (fwhm excused), NOT PASS. It must
    # still be eligible for aesthetic saturation backoff when over the ceiling -- the
    # regression case would otherwise never trigger the arm. Lowering saturation does
    # not touch fwhm, so a re-render stays SOURCE_LIMITED (the input limit is not hidden).
    hero = _hero(HeroOutcome.SOURCE_LIMITED)
    d = decide_candidate(hero, attempt=0, max_attempts=2, body_saturation_median=0.82, saturation_ceiling=0.60)
    assert d.action is RecoveryAction.RECOVER


def test_aesthetic_backoff_accepts_source_limited_under_ceiling():
    hero = _hero(HeroOutcome.SOURCE_LIMITED)
    d = decide_candidate(hero, attempt=0, max_attempts=2, body_saturation_median=0.55, saturation_ceiling=0.60)
    assert d.action is RecoveryAction.ACCEPT


def test_aesthetic_backoff_never_chases_a_processing_failure():
    # a non-chroma PROCESSING_FAILED that is ALSO over the ceiling must NOT trigger
    # aesthetic backoff (arms are mutually exclusive by outcome; don't chase satu on a
    # render failing for another reason)
    hero = _hero(HeroOutcome.PROCESSING_FAILED, processing_failed_gates=("star_count",))
    d = decide_candidate(hero, attempt=0, max_attempts=2, body_saturation_median=0.82, saturation_ceiling=0.60)
    assert d.action is RecoveryAction.ACCEPT


def test_catastrophe_arm_unchanged_by_new_params():
    # the 3B pegged_chroma path still recovers, with the new params at defaults
    hero = _hero(HeroOutcome.PROCESSING_FAILED, processing_failed_gates=("pegged_chroma",))
    assert decide_candidate(hero, attempt=0, max_attempts=2).action is RecoveryAction.RECOVER


def test_chroma_backoff_steps_freshens_refs_and_lowers_saturation():
    steps = (_stretch(saturation=0.60), _separate(), _blend(), _export())
    out = chroma_backoff_steps(steps, saturation=0.51, suffix="satu1")

    new_stretch, new_separate, new_blend, new_export = out
    # the whole stretch-and-downstream chain is freshened (forces re-execution)
    assert new_stretch.output.name == "stretched_satu1"
    assert new_separate.input.name == "stretched_satu1"
    assert new_export.output_base == "T_final_satu1"
    # ...at the lowered saturation the caller asked for
    assert new_stretch.saturation == 0.51
    # upstream input (the reused linear master) is untouched -> Workspace reuses it
    assert new_stretch.input == _LINEAR


def test_chroma_backoff_lower_saturation_is_the_only_param_change():
    steps = (_stretch(saturation=0.60), _separate(), _blend(), _export())
    out = chroma_backoff_steps(steps, saturation=0.42, suffix="satu2")
    s = out[0]
    base = steps[0]
    # every stretch param except saturation (and the renamed output ref) is identical
    assert (s.asinh_d, s.blackpoint, s.mtf_mid, s.ght, s.black_lift) == (
        base.asinh_d,
        base.blackpoint,
        base.mtf_mid,
        base.ght,
        base.black_lift,
    )
    assert s.saturation == 0.42
