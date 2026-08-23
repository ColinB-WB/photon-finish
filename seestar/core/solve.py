"""Pure stretch-solve state machine.

The planner uses this module to choose the Siril `mtf` midtone value that makes a
probe render hit its target brightness. For extended targets with a sky ceiling,
the state machine also raises the black point through an outer search so the sky
lands at or below the ceiling without crossing the measured sky median.

This module never renders files. It emits `ProbeRequest` values; the shell renders
and measures those probes, then feeds the numeric observations back into
`observe_probe`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .types import ProbeRecord, SolverBounds, TargetMetric


@dataclass(frozen=True)
class SolveRequest:
    metric: TargetMetric
    target_level: float
    bounds: SolverBounds
    blackpoint: float
    linear_median: float  # bp search hard bound: 0.999 * this (M8 bp law)
    sky_ceiling: float | None  # None = single-point (starfield / pinned-bp / doc profile)
    sky_tol: float = 2.0
    max_outer: int = 3


@dataclass(frozen=True)
class ProbeRequest:
    mtf_mid: float
    blackpoint: float


@dataclass(frozen=True)
class SolveState:
    """Every field explicit; a frozen value per step. `phase` is descriptive only
    (both phases emit mtf probes -- "bp" just means the current inner mtf search is
    nested inside an outer bp iteration)."""

    request: SolveRequest
    phase: str  # "mtf" | "bp"
    lo: float
    hi: float
    iterations: int
    lo_moved: bool
    hi_moved: bool
    mid: float
    value: float | None
    sky: float | None
    bp: float
    bp_lo: float
    bp_hi: float
    outer_iterations: int
    bp_hi_moved: bool
    done: bool
    probes: tuple[ProbeRecord, ...]


@dataclass(frozen=True)
class SolveResult:
    mtf_mid: float
    achieved: float
    sky: float
    converged: bool
    mtf_pinned: str | None  # None | "lo" | "hi"
    blackpoint: float
    bp_pinned: bool
    bp_iterations: int
    probes: tuple[ProbeRecord, ...]


def _inner_mid(bounds: SolverBounds, lo: float, hi: float) -> float:
    # mid computed FRESH each iteration from the CURRENT lo/hi -- never cached.
    return round((lo + hi) / 2, 4)


def start_solve(req: SolveRequest) -> tuple[SolveState, ProbeRequest]:
    lo, hi = req.bounds.lo, req.bounds.hi
    mid = _inner_mid(req.bounds, lo, hi)
    bp = req.blackpoint
    state = SolveState(
        request=req,
        phase="mtf",
        lo=lo,
        hi=hi,
        iterations=0,
        lo_moved=False,
        hi_moved=False,
        mid=mid,
        value=None,
        sky=None,
        bp=bp,
        bp_lo=bp,
        bp_hi=0.999 * req.linear_median,
        outer_iterations=0,
        bp_hi_moved=False,
        done=False,
        probes=(),
    )
    return state, ProbeRequest(mtf_mid=mid, blackpoint=bp)


def _fresh_inner(state: SolveState, bp: float, phase: str) -> SolveState:
    """Reset the inner mtf search to its configured bounds for a brand-new bp (the
    two-point law: each outer iteration is a FULL fresh inner solve -- lo/hi,
    moved-flags, and iteration count all restart, only bp changes)."""
    bounds = state.request.bounds
    lo, hi = bounds.lo, bounds.hi
    mid = _inner_mid(bounds, lo, hi)
    return replace(
        state,
        phase=phase,
        lo=lo,
        hi=hi,
        iterations=0,
        lo_moved=False,
        hi_moved=False,
        mid=mid,
        value=None,
        sky=None,
        bp=bp,
    )


def observe_probe(state: SolveState, value: float, sky: float) -> tuple[SolveState, ProbeRequest | None]:
    req = state.request
    record = ProbeRecord(
        phase=state.phase,
        mtf_mid=state.mid,
        blackpoint=state.bp,
        value=value,
        sky=sky,
        target_level=req.target_level,
        distance=round(abs(value - req.target_level), 4),
    )
    state = replace(state, probes=state.probes + (record,))
    bounds = req.bounds
    iterations = state.iterations + 1
    converged_now = abs(value - req.target_level) <= bounds.tol

    lo, hi = state.lo, state.hi
    lo_moved, hi_moved = state.lo_moved, state.hi_moved
    if not converged_now:
        if value < req.target_level:  # too dim -> brighter -> lower mtf
            hi, hi_moved = state.mid, True
        else:  # too bright -> dimmer -> higher mtf
            lo, lo_moved = state.mid, True

    inner_done = converged_now or iterations >= bounds.max_iter

    if not inner_done:
        mid = _inner_mid(bounds, lo, hi)
        new_state = replace(
            state,
            lo=lo,
            hi=hi,
            lo_moved=lo_moved,
            hi_moved=hi_moved,
            iterations=iterations,
            mid=mid,
            value=value,
            sky=sky,
        )
        return new_state, ProbeRequest(mtf_mid=mid, blackpoint=state.bp)

    # Inner solve finished (converged, or exhausted max_iter without converging):
    # state.mid/value/sky hold the last-probed point -- these ARE mtf_mid/achieved/
    # sky for THIS inner solve (mid is not recomputed once the loop has broken).
    settled = replace(
        state,
        lo=lo,
        hi=hi,
        lo_moved=lo_moved,
        hi_moved=hi_moved,
        iterations=iterations,
        value=value,
        sky=sky,
    )

    if req.sky_ceiling is None:  # single-point solve (e.g. starfield sky_median target)
        return replace(settled, done=True), None

    if state.outer_iterations == 0:
        # This inner solve ran at the ENGINE bp. Only enter the outer bp search if
        # the resulting sky sits above ceiling + tol -- ONE-SIDED: an already-dark
        # sky keeps the engine bp untouched (every existing dark-sky render is
        # unchanged; documentation opts out upstream via sky_ceiling=None).
        if sky <= req.sky_ceiling + req.sky_tol:
            return replace(settled, done=True), None
        bp_lo = settled.bp  # the bp just probed (== req.blackpoint)
        bp_hi = settled.bp_hi  # 0.999 * linear_median, fixed at start_solve
        outer_bp = round((bp_lo + bp_hi) / 2, 6)
        new_state = _fresh_inner(replace(settled, bp_lo=bp_lo, bp_hi=bp_hi), outer_bp, phase="bp")
        new_state = replace(new_state, outer_iterations=1)
        return new_state, ProbeRequest(mtf_mid=new_state.mid, blackpoint=new_state.bp)

    # Inside the outer bp binary search: this inner solve ran at a CANDIDATE bp.
    if abs(sky - req.sky_ceiling) <= req.sky_tol:
        return replace(settled, done=True), None

    bp_lo, bp_hi, bp_hi_moved = settled.bp_lo, settled.bp_hi, settled.bp_hi_moved
    if sky > req.sky_ceiling:  # still too bright -> push bp up (higher bp = darker sky)
        bp_lo = settled.bp
    else:  # overshot dark -> this is the cap that binds (bp_pinned watches bp_hi_moved)
        bp_hi, bp_hi_moved = settled.bp, True

    if settled.outer_iterations >= req.max_outer:
        # Outer loop exhausted without landing within sky_tol of the ceiling.
        return replace(settled, bp_lo=bp_lo, bp_hi=bp_hi, bp_hi_moved=bp_hi_moved, done=True), None

    outer_bp = round((bp_lo + bp_hi) / 2, 6)
    new_state = _fresh_inner(
        replace(settled, bp_lo=bp_lo, bp_hi=bp_hi, bp_hi_moved=bp_hi_moved),
        outer_bp,
        phase="bp",
    )
    new_state = replace(new_state, outer_iterations=settled.outer_iterations + 1)
    return new_state, ProbeRequest(mtf_mid=new_state.mid, blackpoint=new_state.bp)


def solve_result(state: SolveState) -> SolveResult:
    req = state.request
    converged = state.value is not None and abs(state.value - req.target_level) <= req.bounds.tol
    # pinned: EVERY iteration of the LAST inner solve pushed toward one bound (the
    # other never moved) and the target was still not reached -- the bound is what
    # binds, not the data. Exact (direction-flag) detection; a mid-range tol
    # exhaustion moves both -> not pinned.
    pinned = None
    if not converged:
        if state.hi_moved and not state.lo_moved:
            pinned = "lo"  # always wanted brighter than lo allows
        elif state.lo_moved and not state.hi_moved:
            pinned = "hi"  # always wanted dimmer than hi allows
    # bp pinned: the outer search ran, ended out of tolerance, and the upper bound
    # (bp_hi) never moved -- the ceiling is unreachable without violating the
    # bp < sky-median invariant (M8 lesson).
    bp_pinned = (
        req.sky_ceiling is not None
        and bool(state.outer_iterations)
        # Pinning can only be judged against a MEASURED sky. It is None only between
        # outer iterations (`_fresh_inner` clears it), where the previous code raised
        # a TypeError mid-chain instead of reporting a diagnostic; "not pinned" is
        # the honest reading of "no sky measured to compare".
        and state.sky is not None
        and abs(state.sky - req.sky_ceiling) > req.sky_tol
        and not state.bp_hi_moved
    )

    # Phase 3C stretch recovery: a CONVERGED solve halts on a within-tol probe, so
    # its terminal state IS the best probe -- report it unchanged (byte-for-byte
    # parity with every committed baseline). A NON-converged solve instead reports
    # the BEST VALID probe it ever measured (closest to target, sky prerequisite
    # met), so the plan assembled from this result renders the best stretch found
    # rather than the last one the search happened to end on. `converged` stays
    # False either way (every probe in a non-converged run is > tol, since a
    # within-tol probe always halts the loop), so evaluate_hero still reports
    # PROCESSING_FAILED honestly. pinned/bp diagnostics describe the terminal
    # search and are left as computed above.
    mtf_mid, achieved, sky, blackpoint = state.mid, state.value, state.sky, state.bp
    if not converged:
        best = _best_valid_probe(state.probes, req)
        if best is not None:
            mtf_mid, achieved, sky, blackpoint = best.mtf_mid, best.value, best.sky, best.blackpoint

    # `value`/`sky` are None between outer bp iterations (`_fresh_inner` clears them
    # while the probe HISTORY survives), which is why the best-probe fallback above
    # runs first: it restores a measured point in exactly that case. Reaching here
    # with neither means the solve finished having measured nothing at all -- a
    # defect to surface, not a number to invent (tenet 1). The pre-assert code
    # crashed on `float(None)` here anyway; this just names the reason.
    assert achieved is not None and sky is not None, "solve finished with no usable probe"
    return SolveResult(
        mtf_mid=mtf_mid,
        achieved=float(achieved),
        sky=float(sky),
        converged=converged,
        mtf_pinned=pinned,
        blackpoint=blackpoint,
        bp_pinned=bp_pinned,
        bp_iterations=state.outer_iterations,
        probes=state.probes,
    )


# Diagnosis 5: how far a probe trace may deviate from the mtf law (lower mtf =
# brighter) before the solve's convergence claim is untrustworthy. Expressed as
# a multiple of the solver's own convergence tolerance -- a reversal within a
# few tolerances is measurement noise; the S50 M82 moving-mask trace reversed
# by ~13 levels between neighbouring mtf values.
MONO_TOL_FACTOR = 4.0


def monotonicity_violation(probes: tuple[ProbeRecord, ...]) -> float:
    """Largest reversal of the mtf brightness law in a probe trace, grouped by
    blackpoint (each bp is its own response curve; never compared across
    groups). For any two same-bp probes with mtf_a < mtf_b, the law expects
    value_a >= value_b; the violation is max(value_b - value_a, 0). Returns
    0.0 for a clean (or empty) trace. Pure diagnostic -- callers decide what
    magnitude is disqualifying (see MONO_TOL_FACTOR)."""
    worst = 0.0
    by_bp: dict[float, list[ProbeRecord]] = {}
    for p in probes:
        by_bp.setdefault(p.blackpoint, []).append(p)
    for group in by_bp.values():
        ordered = sorted(group, key=lambda p: p.mtf_mid)
        # ALL pairs, not only adjacent ones: the S50 M82 moving-mask trace hides
        # its worst reversal between non-neighbouring samples (75.8 @ .1566 vs
        # 81.8 @ .1615) while adjacent deltas look small.
        for i, a in enumerate(ordered):
            for b in ordered[i + 1 :]:
                if b.mtf_mid > a.mtf_mid:  # equal-mtf repeats carry no direction information
                    worst = max(worst, b.value - a.value)
    return round(worst, 4)


def apply_monotonicity_guard(result: SolveResult, req: SolveRequest) -> tuple[SolveResult, float]:
    """Diagnosis 5: never publish a converged result off a non-monotonic trace.

    Returns (possibly demoted result, violation magnitude). When the trace
    reverses beyond MONO_TOL_FACTOR x tol, a `converged=True` result is demoted
    to the conservative fallback: converged=False, reporting the best VALID
    probe (same rule a non-converged solve already uses). The full trace stays
    in `probes` for the record; the caller notes the reason."""
    violation = monotonicity_violation(result.probes)
    if violation <= MONO_TOL_FACTOR * req.bounds.tol or not result.converged:
        return result, violation
    demoted = replace(result, converged=False)
    best = _best_valid_probe(result.probes, req)
    if best is not None:
        demoted = replace(demoted, mtf_mid=best.mtf_mid, achieved=best.value, sky=best.sky, blackpoint=best.blackpoint)
    return demoted, violation


def _best_valid_probe(probes: tuple[ProbeRecord, ...], req: SolveRequest) -> ProbeRecord | None:
    """Closest-to-target probe whose sky satisfies the request prerequisite.

    Valid = single-point solve (sky_ceiling None) accepts every probe; a two-point
    solve requires sky <= sky_ceiling + sky_tol. Best = min `distance`, tie-broken
    by earliest probe index (probes are oldest-first, so the first min wins) --
    fully deterministic. None when no probe qualifies (caller keeps the terminal
    state, i.e. today's behavior).
    """
    best: ProbeRecord | None = None
    for p in probes:  # oldest-first: the earliest of any distance tie is seen first
        if req.sky_ceiling is not None and p.sky > req.sky_ceiling + req.sky_tol:
            continue
        if best is None or p.distance < best.distance:
            best = p
    return best
