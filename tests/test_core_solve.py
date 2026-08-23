"""Pure solve state machine contract (Task 17): start_solve/observe_probe/solve_result
must reproduce the exact probe sequence + outcomes of legacy pipeline.solve_mtf +
solve_stretch_two_point (tests/test_solver.py, unchanged, is the equivalence proof
against the Siril-facing drivers). These tests drive the state machine directly,
with no I/O -- pure math."""

from seestar.core.solve import SolveRequest, observe_probe, solve_result, start_solve
from seestar.core.types import SolverBounds, TargetMetric


def _drive(req, probe_fn, max_steps=100):
    state, probe = start_solve(req)
    for _ in range(max_steps):
        if probe is None:
            break
        val, sky = probe_fn(probe.mtf_mid, probe.blackpoint)
        state, probe = observe_probe(state, val, sky)
    return solve_result(state)


def _linear_probe(brightness_at_lo=200.0, brightness_at_hi=20.0, sky_frac=0.15):
    """Monotonic fake: brightness decreases as mtf rises (the mtf law)."""

    def fn(mid, bp):
        lo, hi = 0.02, 0.25
        t = (mid - lo) / (hi - lo)
        val = brightness_at_lo + t * (brightness_at_hi - brightness_at_lo)
        return val, val * sky_frac

    return fn


def _req(**kw):
    d = dict(
        metric=TargetMetric.BODY_P90,
        target_level=72.0,
        bounds=SolverBounds(),
        blackpoint=0.0148,
        linear_median=0.015,
        sky_ceiling=None,
    )
    d.update(kw)
    return SolveRequest(**d)


def test_converges_on_target():
    r = _drive(_req(), _linear_probe())
    assert r.converged and abs(r.achieved - 72.0) <= 0.5
    assert r.mtf_pinned is None and r.bp_pinned is False


def test_pinned_lo_when_target_unreachably_bright():
    r = _drive(_req(target_level=300.0), _linear_probe())
    assert not r.converged and r.mtf_pinned == "lo"


def test_pinned_hi_when_target_unreachably_dim():
    r = _drive(_req(target_level=1.0), _linear_probe())
    assert not r.converged and r.mtf_pinned == "hi"


def test_two_point_raises_bp_to_hold_sky():
    calls = []

    def fn(mid, bp):
        val = 72.0  # body always on target
        # higher bp -> darker sky. Slope picked so the ceiling sits inside the bp
        # window the M8 invariant allows (bp in (0.0148, 0.999*0.015]) -- with the
        # window this narrow (~1.9e-4), a shallower slope can never pull sky down
        # to 18 at all (verified: 20000.0 bottoms out at sky~36, permanently
        # bp-pinned), so this is calibrated to be reachable, not merely raised.
        sky = 40.0 - (bp - 0.0148) * 200000.0
        calls.append((mid, bp))
        return val, max(sky, 0.0)

    r = _drive(_req(sky_ceiling=18.0), fn)
    assert r.blackpoint > 0.0148  # bp was raised
    assert r.blackpoint <= 0.999 * 0.015 + 1e-9  # never past the sky median (M8 law)
    assert abs(r.sky - 18.0) <= 2.0
    assert r.bp_iterations >= 1


def test_bp_pinned_when_ceiling_unreachable():
    def fn(mid, bp):
        return 72.0, 50.0  # sky never comes down

    r = _drive(_req(sky_ceiling=18.0), fn)
    assert r.bp_pinned is True


def test_dark_sky_keeps_engine_bp():
    r = _drive(_req(sky_ceiling=18.0), _linear_probe(sky_frac=0.05))
    assert r.blackpoint == 0.0148 and r.bp_iterations == 0


def test_probe_count_bounded():
    n = {"c": 0}

    def fn(mid, bp):
        n["c"] += 1
        return 1000.0, 10.0  # never converges

    _drive(_req(sky_ceiling=None), fn)
    assert n["c"] <= SolverBounds().max_iter


# ---- ported from the deleted tests/test_solver.py (Task 23 coverage-loss fix:
# these two behaviors were only exercised through the Siril-facing solve_mtf
# driver, which test_core_solve.py's docstring assumed test_solver.py would keep
# proving equivalence for -- every case above uses SolverBounds() defaults, so
# neither a non-default search window nor the "bounces both ways, not pinned"
# branch had a typed-level test) ----------------------------------------------


def _direction_probe(mid, bp):
    """10/mid: the exact monotone fake the deleted tests/test_solver.py used
    (`_brightness`) -- lower mtf = brighter, the encoded direction law."""
    val = 10.0 / mid
    return val, val / 10.0


def test_custom_bounds_shift_search_window_and_pinning():
    """A non-default SolverBounds must actually shift where the search looks AND
    where pinned detection binds -- not just be accepted and ignored in favor of
    the module's implicit defaults."""
    bounds = SolverBounds(lo=0.10, hi=0.12)
    r = _drive(_req(target_level=300.0, bounds=bounds), _direction_probe)
    assert not r.converged
    assert r.mtf_pinned == "lo"
    assert 0.10 <= r.mtf_mid <= 0.12


def test_unreachable_between_probe_values_is_not_pinned():
    """A target reachable in principle sits BETWEEN two probed values, but an
    unrealistically tight tol exhausts max_iter before landing on it: the search
    bounces both directions (both lo_moved and hi_moved flip), so it must NOT
    report pinned -- a mid-range tol exhaustion is not a bound binding."""
    r = _drive(_req(target_level=100.0, bounds=SolverBounds(tol=1e-12, max_iter=4)), _direction_probe)
    assert r.converged is False
    assert r.mtf_pinned is None


def test_solve_result_retains_every_probe_in_order():
    req = _req(target_level=72.0)
    result = _drive(req, _linear_probe())
    assert len(result.probes) >= 2  # a binary search always takes more than one probe
    # first probe is the bounds midpoint; probes are in the order they were measured
    assert result.probes[0].mtf_mid == round((0.02 + 0.25) / 2, 4)
    # the FINAL probe's fields match the settled result exactly
    last = result.probes[-1]
    assert last.mtf_mid == result.mtf_mid
    assert last.value == result.achieved
    assert last.target_level == 72.0
    assert last.distance == round(abs(last.value - 72.0), 4)


# ---- Phase 3C: best-valid-probe recovery for NON-converged solves -------------
# (docs/3C-stretch-recovery-design.md, plan sec 3C/7). A converged solve halts on
# a within-tol probe, so best == last there and these do not touch it (parity).
# A non-converged solve must REPORT the closest valid probe it ever saw, not the
# terminal one -- so the plan built from `result` renders the best stretch found.


def _sequence_probe(values, sky_frac=0.1):
    """A fake whose returned brightness is a FIXED sequence indexed by probe count,
    independent of mtf -- deliberately non-monotonic so the terminal probe is not
    the closest one. sky = value * sky_frac."""
    n = {"i": 0}

    def fn(mid, bp):
        v = values[n["i"] % len(values)]
        n["i"] += 1
        return v, v * sky_frac

    return fn


def test_non_converged_reports_best_probe_not_terminal():
    # index 1 (value 71, target 72 -> distance 1.0) is uniquely closest; every
    # other probe is >170 away. tol=0.5 so the run never converges.
    values = [200.0, 71.0, 500.0, 400.0, 300.0, 250.0, 260.0, 255.0, 258.0, 256.0]
    r = _drive(_req(target_level=72.0, sky_ceiling=None), _sequence_probe(values))

    assert r.converged is False
    best = min(r.probes, key=lambda p: p.distance)  # min() keeps earliest on ties
    assert best.value == 71.0
    assert best is not r.probes[-1], "the earlier probe must beat the terminal one"
    # the settled result reports the BEST probe's params, not the terminal state's
    assert r.achieved == best.value
    assert r.mtf_mid == best.mtf_mid
    assert r.blackpoint == best.blackpoint


def test_non_converged_tie_break_prefers_earliest_probe():
    # probe 0 -> 70 (dist 2), every later probe -> 74 (dist 2): a distance tie.
    # earliest index wins => the result reports probe 0 (value 70).
    n = {"i": 0}

    def fn(mid, bp):
        v = 70.0 if n["i"] == 0 else 74.0
        n["i"] += 1
        return v, v * 0.1

    r = _drive(_req(target_level=72.0, sky_ceiling=None), fn)

    assert r.converged is False
    assert r.achieved == 70.0
    assert r.mtf_mid == r.probes[0].mtf_mid


def test_converged_solve_still_reports_terminal_probe_parity():
    # Parity guard: a converged solve must be UNCHANGED -- best == last == terminal.
    r = _drive(_req(target_level=72.0), _linear_probe())
    assert r.converged is True
    assert r.mtf_mid == r.probes[-1].mtf_mid
    assert r.achieved == r.probes[-1].value


def test_no_valid_probe_falls_back_to_terminal_state():
    # sky_ceiling=18 but every probe's sky is 100 (> ceiling+tol): NO valid probe.
    # Selection must fall back to the terminal state (current behavior), never pick
    # an invalid too-bright-sky probe.
    def fn(mid, bp):
        return 1000.0, 100.0  # body never near target, sky always above ceiling

    r = _drive(_req(target_level=72.0, sky_ceiling=18.0), fn)

    assert r.converged is False
    assert r.mtf_mid == r.probes[-1].mtf_mid
    assert r.achieved == r.probes[-1].value
    assert r.sky == r.probes[-1].sky


def test_arming_threshold_is_ceiling_plus_tolerance_not_the_ceiling():
    """Owner-ordered audit, 2026-08-14 (docs/assessments/
    2026-08-14-two-point-solve-threshold-audit.md): the black-point search arms
    above `sky_ceiling + sky_tol`, NOT above `sky_ceiling`. NGC5128 landing at
    sky 19.8 without a bp phase looked like a missed trigger during validation
    review; it is the designed band, because that same tolerance is the search's
    own stop condition -- a solve armed inside it would exit on its first probe
    having spent ~9 renders changing nothing.

    Both arms use a fake whose sky RESPONDS to the black point, so a
    non-arming result cannot be confused with an unreachable ceiling (that case
    is `test_bp_pinned_when_ceiling_unreachable`).

    CHANGE_SAFETY.md rule 10: CHARACTERIZATION, not TDD -- this pins behaviour
    that already holds, so the threshold cannot drift silently again. Green
    command: `pytest tests/test_core_solve.py -k arming_threshold`."""

    def _responsive(sky_at_engine_bp):
        def fn(mid, bp):
            return 72.0, max(sky_at_engine_bp - (bp - 0.0148) * 200000.0, 0.0)

        return fn

    # INSIDE the band (18 < 19 <= 20): must not arm, engine bp untouched
    inside = _drive(_req(sky_ceiling=18.0), _responsive(19.0))
    assert 18.0 < inside.sky <= 20.0, f"fixture must land in the band, got {inside.sky}"
    assert inside.bp_iterations == 0, "inside the tolerance band the search must NOT arm"
    assert inside.blackpoint == 0.0148, "...and the engine black point is untouched"

    # ABOVE the band: arms, raises bp, and pulls the sky back onto the ceiling
    above = _drive(_req(sky_ceiling=18.0), _responsive(40.0))
    assert above.bp_iterations >= 1, "above ceiling+tol the search must arm"
    assert above.blackpoint > 0.0148
    assert abs(above.sky - 18.0) <= 2.0
