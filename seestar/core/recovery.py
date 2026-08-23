"""Pure recovery-candidate lifecycle: dependency-invalidation closure, ref
remapping for a fresh recovery attempt, and the accept/recover decision.

This module never touches the filesystem or a Workspace -- it only
reconstructs typed Operation sequences and classifies an already-measured
candidate. The shell (orchestrate.py) executes whatever this module returns
and feeds back typed observations; it never itself decides accept-vs-recover
or which refs are invalidated.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from .types import (
    ArtifactRef,
    Export,
    HeroOutcome,
    HeroQualityReport,
    Operation,
    ScreenBlend,
    SeparateStars,
    StretchRender,
)


def downstream_closure(steps: tuple[Operation, ...], changed: ArtifactRef) -> frozenset[ArtifactRef]:
    """Every ArtifactRef (including `changed` itself) that must be recomputed
    if the operation producing `changed` is replaced with different
    parameters. A pure graph closure over the Plan's own declared
    inputs/outputs -- no hand-maintained per-scenario invalidation table, so
    a star_reduce_k-only change (ScreenBlend's output) never drags in the
    stretch/star-separation refs it doesn't depend on."""
    consumers: dict[ArtifactRef, list[Operation]] = {}
    for op in steps:
        for ref in op.inputs:
            consumers.setdefault(ref, []).append(op)

    invalidated = {changed}
    frontier = [changed]
    while frontier:
        ref = frontier.pop()
        for op in consumers.get(ref, ()):
            for out in op.outputs:
                if out not in invalidated:
                    invalidated.add(out)
                    frontier.append(out)
    return frozenset(invalidated)


def _fresh(ref: ArtifactRef, suffix: str) -> ArtifactRef:
    return ArtifactRef(name=f"{ref.name}_{suffix}", role=ref.role)


def remap_for_attempt(
    steps: tuple[Operation, ...], invalidated: frozenset[ArtifactRef], suffix: str
) -> tuple[Operation, ...]:
    """Reconstruct `steps`, giving every ref in `invalidated` a fresh,
    attempt-suffixed name so its producing op's identity genuinely differs
    (forcing real re-execution rather than Workspace's op_key-keyed reuse),
    while every untouched ref keeps its original name (picked up by that same
    reuse mechanism for free). Workspace's single-producer invariant
    (workspace.py::register) is the structural backstop if a future op type
    is added here without being taught to remap correctly.

    Deliberately scoped to the closed set of operations that can appear
    downstream of a recovery-eligible stretch (StretchRender, SeparateStars,
    ScreenBlend, Export) -- matches this codebase's explicit, closed-union
    style rather than a generic reflection-based rewrite over every Operation
    type. Extend explicitly (never with a catch-all) if a new op type needs
    to participate in recovery.

    Renaming is tracked as an explicit `dict[ArtifactRef, ArtifactRef]`
    old-to-new mapping, built incrementally while `steps` is walked in
    order -- an op's inputs are resolved by looking up whatever an earlier
    op in this same pass already renamed its producer's output to. No
    string-based name-sniffing: a ref is only ever "the same ref" by
    dataclass equality, never by comparing rendered name strings.
    """
    rename: dict[ArtifactRef, ArtifactRef] = {}

    def resolve(ref: ArtifactRef) -> ArtifactRef:
        return rename.get(ref, ref)

    def rename_if_invalidated(ref: ArtifactRef) -> ArtifactRef:
        if ref in invalidated:
            new_ref = _fresh(ref, suffix)
            rename[ref] = new_ref
            return new_ref
        return ref

    remapped: list[Operation] = []
    for op in steps:
        if isinstance(op, StretchRender):
            new_input = resolve(op.input)
            new_output = rename_if_invalidated(op.output)
            if new_input != op.input or new_output != op.output:
                op = replace(op, input=new_input, output=new_output)
        elif isinstance(op, SeparateStars):
            new_input = resolve(op.input)
            new_starless = rename_if_invalidated(op.starless)
            new_stars = rename_if_invalidated(op.stars)
            if new_input != op.input or new_starless != op.starless or new_stars != op.stars:
                op = replace(op, input=new_input, starless=new_starless, stars=new_stars)
        elif isinstance(op, ScreenBlend):
            new_starless = resolve(op.starless)
            new_stars = resolve(op.stars)
            new_output = rename_if_invalidated(op.output)
            if new_starless != op.starless or new_stars != op.stars or new_output != op.output:
                op = replace(op, starless=new_starless, stars=new_stars, output=new_output)
        elif isinstance(op, Export):
            new_input = resolve(op.input)
            if new_input != op.input:
                op = replace(op, input=new_input, output_base=f"{op.output_base}_{suffix}")
        remapped.append(op)
    return tuple(remapped)


def chroma_backoff_steps(steps: tuple[Operation, ...], saturation: float, suffix: str) -> tuple[Operation, ...]:
    """Build a fresh recovery sub-plan that renders the SAME stretch chain at a
    lower `saturation` (Phase 3B chroma backoff).

    Because saturation is fused into the single StretchRender (the session-8
    "solve the chain you ship" law -- there is no standalone Saturate op),
    lowering it invalidates the stretch output and everything downstream. We
    (1) remap exactly that downstream closure to attempt-suffixed names so
    Workspace re-executes it (upstream BGE/denoise/color refs keep their names
    and are reused), then (2) set the lowered saturation on the remapped
    StretchRender. remap_for_attempt handles ONLY ref renaming; the parameter
    override is this function's job, per its contract.
    """
    stretch = next(op for op in steps if isinstance(op, StretchRender))
    invalidated = downstream_closure(steps, stretch.output)
    remapped = remap_for_attempt(steps, invalidated, suffix)
    return tuple(replace(op, saturation=saturation) if isinstance(op, StretchRender) else op for op in remapped)


class RecoveryAction(Enum):
    ACCEPT = "accept"
    RECOVER = "recover"


@dataclass(frozen=True)
class RecoveryDecision:
    action: RecoveryAction
    reason: str


def decide_candidate(
    hero: HeroQualityReport,
    attempt: int,
    max_attempts: int,
    body_saturation_median: float = 0.0,
    saturation_ceiling: float | None = None,
) -> RecoveryDecision:
    """Accept the candidate, or recover it by lowering saturation.

    TWO recovery arms, both lowering saturation through the same bounded bracket but
    with different triggers and DIFFERENT terminal semantics:

    - Catastrophe (Phase 3B): the shipped hero failed processing for exactly the
      `pegged_chroma` neon gate, on a converged solve. On exhaustion the hero stays
      PROCESSING_FAILED (the gate still fails) -- honest failure.
    - Aesthetic (Phase 4C): the hero is otherwise shippable (PASS, or SOURCE_LIMITED
      with the input limit already excused) but measured `body_saturation_median`
      exceeds the per-category `saturation_ceiling`. Over-saturation is an aesthetic
      target, NOT a catastrophe -- on exhaustion the hero keeps its shippable outcome
      (over-target is not a gate), shipping the best-effort image. SOURCE_LIMITED is
      eligible because a soft capture (e.g. the M8 regression case, fwhm excused) ships
      SOURCE_LIMITED, not PASS -- excluding it would leave the very case 4C targets
      unable to trigger. Lowering saturation does not touch fwhm, so a re-render stays
      SOURCE_LIMITED (the input limitation is preserved, never hidden).

    The arms are mutually exclusive by `hero.outcome` (catastrophe requires
    PROCESSING_FAILED; aesthetic requires PASS/SOURCE_LIMITED), so the terminal outcome
    distinction needs no extra machinery. `saturation_ceiling is None` (the default)
    keeps the aesthetic arm dormant -> exact 3B behavior.
    """
    chroma_recoverable = (
        hero.outcome is HeroOutcome.PROCESSING_FAILED
        and hero.processing_failed_gates == ("pegged_chroma",)
        and hero.solve_converged is not False
    )
    if chroma_recoverable and attempt < max_attempts:
        return RecoveryDecision(
            action=RecoveryAction.RECOVER,
            reason=f"pegged_chroma failed; lowering saturation (attempt {attempt + 1}/{max_attempts})",
        )

    aesthetic_recoverable = (
        hero.outcome in (HeroOutcome.PASS, HeroOutcome.SOURCE_LIMITED)
        and saturation_ceiling is not None
        and body_saturation_median > saturation_ceiling
    )
    if aesthetic_recoverable and attempt < max_attempts:
        return RecoveryDecision(
            action=RecoveryAction.RECOVER,
            reason=(
                f"body_saturation_median {body_saturation_median:.3f} > ceiling {saturation_ceiling}; "
                f"lowering saturation (attempt {attempt + 1}/{max_attempts})"
            ),
        )

    return RecoveryDecision(action=RecoveryAction.ACCEPT, reason=f"accepted at attempt {attempt}")
