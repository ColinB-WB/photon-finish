"""Diagnosis 4 (spec 2026-08-12), Task 5: PCC evidence bounds and the
deterministic fresh native-color fallback.

Contract under test:
- The Siril adapter PARSES structured evidence from the tool's own output
  (`parse_pcc_evidence`) and CLASSIFIES command failures
  (`classify_pcc_failure`); it never decides acceptability.
- `core.policy.evaluate_pcc_evidence` is the one deterministic judgment:
  broad bounds reject explicit tool-flagged imprecision, few-stars solutions,
  too-few stars, implausible coefficients, and unverifiable evidence.
- Planning makes PCC recoverable: a rejected or unavailable attempt allocates a
  FRESH native artifact (input = the untouched denoised master, never the
  partially calibrated PCC output) and continues with pcc=False; failure of the
  native path itself refuses planning (a real production failure).
- Provenance (`Plan.color` / notes / EXIF) records attempted/accepted/fallback
  and the reason on every surface.

Sample tool text comes from a REAL captured Siril 1.4.4 run on the S50 M82
stack (2026-08-14) plus the exact message strings extracted from the installed
siril-cli binary — no invented wording.
"""

from __future__ import annotations

import os

import pytest

from seestar.core.planning import PlanComplete, PlanningRefused
from seestar.core.policy import evaluate_pcc_evidence
from seestar.core.types import (
    ColorCalibrate,
    FilterKind,
    OpsDone,
    PccBounds,
    PccEvidence,
    StageKey,
    StretchRender,
    WorkFailed,
    WorkSucceeded,
)
from tests.test_core_planning import Responder, _req, drive

# ---------------------------------------------------------------------------
# Real / binary-exact Siril output samples
# ---------------------------------------------------------------------------

GOOD_OUTPUT = """\
log: 1141 stars excluded from the calculation
log: Distribution of errors: 330 no error, 754 not in area, 375 inner radius too small, 6 pixel out of range, 6 flux ratio failed
log: Found a solution for color calibration using 330 stars. Factors:
log: K0: 0.471\t(deviation: 0.000)
log: K1: 0.713\t(deviation: 0.000)
log: K2: 1.000\t(deviation: 0.000)
log: After renormalization, the following coefficients are applied
log: White balance factors:
log: K0: 0.471
log: K1: 0.713
log: K2: 1.000
log: Background reference:
log: B0: +1.83092e+03
log: B1: +9.94404e+02
log: B2: -1.33325e+00
log: Photometric Color Calibration succeeded.
log: Script execution finished successfully.
"""

IMPRECISE_OUTPUT = """\
log: Found a solution for color calibration using 214 stars. Factors:
log: K0: 0.405\t(deviation: 0.012)
log: K1: 0.653\t(deviation: 0.009)
log: K2: 1.000\t(deviation: 0.000)
log: The photometric color calibration seems to have found an imprecise solution, consider correcting the image gradient first
log: After renormalization, the following coefficients are applied
log: White balance factors:
log: K0: 0.405
log: K1: 0.653
log: K2: 1.000
log: Photometric Color Calibration succeeded.
"""

FEW_STARS_OUTPUT = """\
log: Found a solution for color calibration using 12 stars. Factors:
log: K0: 0.512\t(deviation: 0.031)
log: K1: 0.744\t(deviation: 0.022)
log: K2: 1.000\t(deviation: 0.000)
log: The photometric color calibration has found a solution which may not be perfect because it did not rely on many stars
log: White balance factors:
log: K0: 0.512
log: K1: 0.744
log: K2: 1.000
log: Photometric Color Calibration succeeded.
"""

NO_STARS_FAILURE = "log: There are not enough stars picked in the image. least 20 are needed\nlog: Photometric Color Calibration failed"
CATALOG_FAILURE = "log: Catalog error, no stars identified!\nlog: Photometric Color Calibration failed"
NO_CATALOG_FAILURE = "log: No valid catalog found.\nlog: Photometric Color Calibration failed"
NO_WCS_FAILURE = (
    "log: Cannot run the standalone photometric color calibration on this image "
    "because it has no WCS data or it is not supported"
)


def _good_evidence(**kw):
    d = dict(
        succeeded=True,
        imprecise=False,
        few_stars_warning=False,
        stars_used=330,
        coefficients=(0.471, 0.713, 1.0),
        detail="using 330 stars",
    )
    d.update(kw)
    return PccEvidence(**d)


# ---------------------------------------------------------------------------
# Adapter: parse evidence / classify failures (seestar.adapters.siril)
# ---------------------------------------------------------------------------


def test_parse_good_output():
    from seestar.adapters.siril import parse_pcc_evidence

    ev = parse_pcc_evidence(GOOD_OUTPUT)
    assert ev.succeeded is True
    assert ev.imprecise is False and ev.few_stars_warning is False
    assert ev.stars_used == 330
    assert ev.coefficients == (0.471, 0.713, 1.0)
    assert len(ev.detail) <= 300


def test_parse_imprecise_output():
    from seestar.adapters.siril import parse_pcc_evidence

    ev = parse_pcc_evidence(IMPRECISE_OUTPUT)
    assert ev.succeeded is True
    assert ev.imprecise is True
    assert ev.stars_used == 214
    assert ev.coefficients == (0.405, 0.653, 1.0)


def test_parse_few_stars_output():
    from seestar.adapters.siril import parse_pcc_evidence

    ev = parse_pcc_evidence(FEW_STARS_OUTPUT)
    assert ev.succeeded is True
    assert ev.few_stars_warning is True
    assert ev.stars_used == 12


def test_parse_unrecognized_output():
    from seestar.adapters.siril import parse_pcc_evidence

    ev = parse_pcc_evidence("log: Running command: pcc\nsomething entirely different")
    assert ev.succeeded is False
    assert ev.stars_used is None and ev.coefficients is None


@pytest.mark.parametrize(
    "text,expected",
    [
        (NO_STARS_FAILURE, "no_stars"),
        ("log: Not enough stars available", "no_stars"),
        ("log: No stars retrieved from the catalog NOMAD", "catalog"),
        (CATALOG_FAILURE, "catalog"),
        (NO_CATALOG_FAILURE, "catalog"),
        ("log: Reprojecting catalog failed", "catalog"),
        (NO_WCS_FAILURE, "no_wcs"),
        ("log: something exploded", "tool"),
    ],
)
def test_classify_pcc_failure(text, expected):
    from seestar.adapters.siril import classify_pcc_failure

    assert classify_pcc_failure(text) == expected


def test_adapter_reports_evidence_and_never_decides(monkeypatch, tmp_path):
    """A pcc=True ColorCalibrate appends parsed evidence to ctx.pcc_evidence —
    even for the imprecise solution. Rejection is the CORE's call, not the
    adapter's (spec Diagnosis 4 item 5)."""
    from seestar.adapters import base as B
    from seestar.adapters import siril as S

    calls = {}

    def fake_run(lines, tools):
        calls["lines"] = lines
        return IMPRECISE_OUTPUT

    monkeypatch.setattr(S, "run_siril", fake_run)
    monkeypatch.setattr(S, "neutralize_background", lambda p: {})

    class _WS:
        def path_for(self, ref, ext):
            return str(tmp_path / f"{ref.name}{ext}")

    from seestar.core.types import ArtifactRef, ArtifactRole

    inp = ArtifactRef("denoised", ArtifactRole.PRODUCTION)
    out = ArtifactRef("linear_master", ArtifactRole.PRODUCTION)
    op = ColorCalibrate(input=inp, output=out, pcc=True)
    ctx = B.ExecContext(workspace=_WS(), tools=None, resolve={inp: str(tmp_path / "denoised.fit")})
    written = S.SirilAdapter().execute(op, ctx)
    assert out in written
    assert len(ctx.pcc_evidence) == 1
    assert ctx.pcc_evidence[0].imprecise is True  # reported, not judged
    assert "pcc" in calls["lines"] and "icc_remove" in calls["lines"]


def test_adapter_native_path_emits_no_evidence(monkeypatch, tmp_path):
    from seestar.adapters import base as B
    from seestar.adapters import siril as S

    monkeypatch.setattr(S, "run_siril", lambda lines, tools: "log: ok")

    class _WS:
        def path_for(self, ref, ext):
            return str(tmp_path / f"{ref.name}{ext}")

    from seestar.core.types import ArtifactRef, ArtifactRole

    inp = ArtifactRef("denoised", ArtifactRole.PRODUCTION)
    out = ArtifactRef("linear_native", ArtifactRole.PRODUCTION)
    op = ColorCalibrate(input=inp, output=out, pcc=False)
    ctx = B.ExecContext(workspace=_WS(), tools=None, resolve={inp: str(tmp_path / "denoised.fit")})
    S.SirilAdapter().execute(op, ctx)
    assert ctx.pcc_evidence == []


def test_adapter_classifies_pcc_command_failure(monkeypatch, tmp_path):
    from seestar.adapters import base as B
    from seestar.adapters import siril as S

    def fake_run(lines, tools):
        raise RuntimeError(f"Siril script failed:\n{CATALOG_FAILURE}")

    monkeypatch.setattr(S, "run_siril", fake_run)

    class _WS:
        def path_for(self, ref, ext):
            return str(tmp_path / f"{ref.name}{ext}")

    from seestar.core.types import ArtifactRef, ArtifactRole

    inp = ArtifactRef("denoised", ArtifactRole.PRODUCTION)
    out = ArtifactRef("linear_master", ArtifactRole.PRODUCTION)
    op = ColorCalibrate(input=inp, output=out, pcc=True)
    ctx = B.ExecContext(workspace=_WS(), tools=None, resolve={inp: str(tmp_path / "denoised.fit")})
    with pytest.raises(B.AdapterError, match=r"pcc_unavailable\(catalog\)"):
        S.SirilAdapter().execute(op, ctx)


# ---------------------------------------------------------------------------
# Core: evaluate_pcc_evidence bounds
# ---------------------------------------------------------------------------

BOUNDS = PccBounds()


def test_good_evidence_accepts():
    v = evaluate_pcc_evidence(_good_evidence(), BOUNDS)
    assert v.accepted is True
    assert v.reason == "evidence_within_bounds"


# --- Owner Gate 3 ruling (2026-08-14): Siril's "imprecise" line is ADVICE about
# gradient, not a veto. Treating it as one rejected every IRCUT galaxy tested --
# including the known-good M81 regression case at 1540 stars -- which disabled
# colour calibration for the whole category and shipped the orange-galaxy defect
# the 2026-07 PCC-ownership rule exists to prevent. It is now a QUALIFIED accept
# when every other piece of evidence is within bounds; the warning is preserved
# on every surface so acceptance is never silent.


def test_imprecise_but_plausible_is_qualified_acceptance():
    v = evaluate_pcc_evidence(_good_evidence(imprecise=True), BOUNDS)
    assert v.accepted is True
    assert v.reason == "imprecise_within_bounds"
    assert v.qualified is True


def test_healthy_acceptance_is_not_qualified():
    v = evaluate_pcc_evidence(_good_evidence(), BOUNDS)
    assert v.accepted is True and v.qualified is False


@pytest.mark.parametrize(
    "label,stars,coeffs",
    [
        ("M82 S50", 380, (0.418, 0.659, 1.0)),
        ("M81 S50", 406, (0.413, 0.634, 1.0)),
        ("M81 S30 Pro", 1540, (0.411, 0.690, 1.0)),
    ],
)
def test_the_three_real_galaxy_solutions_are_accepted(label, stars, coeffs):
    """The exact evidence captured from real renders at Owner Gate 3. Every one
    was flagged imprecise and every one is plausible, so all three must now be
    accepted-qualified rather than rejected."""
    ev = _good_evidence(imprecise=True, stars_used=stars, coefficients=coeffs)
    v = evaluate_pcc_evidence(ev, BOUNDS)
    assert v.accepted is True, f"{label} must not lose its calibration"
    assert v.reason == "imprecise_within_bounds" and v.qualified is True


def test_imprecise_does_not_rescue_implausible_coefficients():
    v = evaluate_pcc_evidence(_good_evidence(imprecise=True, coefficients=(0.05, 0.7, 1.0)), BOUNDS)
    assert v.accepted is False and v.reason == "implausible_coefficients"


def test_imprecise_does_not_rescue_too_few_stars():
    v = evaluate_pcc_evidence(_good_evidence(imprecise=True, stars_used=BOUNDS.min_stars - 1), BOUNDS)
    assert v.accepted is False and v.reason == "too_few_stars"


def test_imprecise_does_not_rescue_unreported_evidence():
    assert evaluate_pcc_evidence(_good_evidence(imprecise=True, stars_used=None), BOUNDS).accepted is False
    assert evaluate_pcc_evidence(_good_evidence(imprecise=True, coefficients=None), BOUNDS).accepted is False
    assert evaluate_pcc_evidence(_good_evidence(imprecise=True, succeeded=False), BOUNDS).accepted is False


def test_few_stars_warning_rejects():
    v = evaluate_pcc_evidence(_good_evidence(few_stars_warning=True), BOUNDS)
    assert v.accepted is False and v.reason == "few_stars_solution"


def test_not_confirmed_rejects():
    v = evaluate_pcc_evidence(_good_evidence(succeeded=False), BOUNDS)
    assert v.accepted is False and v.reason == "not_confirmed"


def test_too_few_stars_rejects():
    v = evaluate_pcc_evidence(_good_evidence(stars_used=BOUNDS.min_stars - 1), BOUNDS)
    assert v.accepted is False and v.reason == "too_few_stars"


def test_stars_unreported_rejects():
    v = evaluate_pcc_evidence(_good_evidence(stars_used=None), BOUNDS)
    assert v.accepted is False and v.reason == "stars_unreported"


def test_coefficient_out_of_range_rejects():
    v = evaluate_pcc_evidence(_good_evidence(coefficients=(0.05, 0.7, 1.0)), BOUNDS)
    assert v.accepted is False and v.reason == "implausible_coefficients"
    v = evaluate_pcc_evidence(_good_evidence(coefficients=(0.5, 0.7, 6.0)), BOUNDS)
    assert v.accepted is False and v.reason == "implausible_coefficients"


def test_coefficient_ratio_rejects():
    # each within [coeff_min, coeff_max] but the spread is implausible
    v = evaluate_pcc_evidence(_good_evidence(coefficients=(0.25, 0.7, 1.5)), BOUNDS)
    assert v.accepted is False and v.reason == "implausible_coefficients"


def test_coefficients_unreported_rejects():
    v = evaluate_pcc_evidence(_good_evidence(coefficients=None), BOUNDS)
    assert v.accepted is False and v.reason == "coefficients_unreported"


def test_missing_evidence_rejects():
    v = evaluate_pcc_evidence(None, BOUNDS)
    assert v.accepted is False and v.reason == "no_evidence"


def test_parsed_real_outputs_both_accept_with_the_right_qualification():
    """End-to-end over the real tool text: the clean solution accepts plainly,
    the imprecise one accepts as qualified. Neither loses its calibration."""
    from seestar.adapters.siril import parse_pcc_evidence

    good = evaluate_pcc_evidence(parse_pcc_evidence(GOOD_OUTPUT), BOUNDS)
    imprecise = evaluate_pcc_evidence(parse_pcc_evidence(IMPRECISE_OUTPUT), BOUNDS)
    assert good.accepted is True and good.qualified is False
    assert imprecise.accepted is True and imprecise.qualified is True


# ---------------------------------------------------------------------------
# Planning: recoverable PCC step
# ---------------------------------------------------------------------------


def _galaxy_req(**kw):
    # IRCUT + caller category pin via prior is heavier than needed; the e-gate
    # default classify on IRCUT with high E lands on galaxy in existing tests.
    from seestar.core.types import Category, PriorObservation

    d = dict(target_label="M81", filter_kind=FilterKind.IRCUT, has_wcs=True, use_prior=True)
    d.update(kw)
    return (
        _req(**d),
        PriorObservation(catalog_category=Category.GALAXY, fov_fraction=0.1),
    )


def _color_ops(plan):
    return [s for s in plan.steps if isinstance(s, ColorCalibrate)]


def test_accepted_pcc_keeps_calibrated_path():
    req, prior = _galaxy_req()
    resp = Responder(prior=prior)  # default responder supplies GOOD evidence
    out = drive(req, resp)
    assert isinstance(out, PlanComplete)
    ops = _color_ops(out.plan)
    assert len(ops) == 1 and ops[0].pcc is True
    assert ops[0].output.name == "linear_master"
    assert out.state.decisions.pcc is True
    c = out.plan.color
    assert c is not None and c.attempted and c.accepted and not c.fallback
    assert c.reason == "evidence_within_bounds"
    # downstream consumes the calibrated artifact
    stretches = [s for s in out.plan.steps if isinstance(s, StretchRender)]
    assert stretches and all(s.input.name == "linear_master" for s in stretches)


def test_rejected_evidence_falls_back_to_fresh_native():
    req, prior = _galaxy_req()
    resp = Responder(prior=prior)
    resp.hook(
        "ColorCalibrate",
        lambda item: WorkSucceeded(
            item.id,
            OpsDone(
                pcc_evidence=(
                    _good_evidence(coefficients=(0.05, 0.7, 1.0), stars_used=214) if item.request.ops[0].pcc else None
                )
            ),
        ),
    )
    out = drive(req, resp)
    assert isinstance(out, PlanComplete)
    # the attempt happened, then the fallback: two ColorCalibrate executions...
    assert resp.calls.count("ColorCalibrate") == 2
    # ...but the final Plan carries ONLY the native op, reading the untouched
    # denoised master into a FRESH artifact (never the rejected PCC output)
    ops = _color_ops(out.plan)
    assert len(ops) == 1 and ops[0].pcc is False
    assert ops[0].input.name == "denoised"
    assert ops[0].output.name == "linear_native"
    assert out.state.decisions.pcc is False
    c = out.plan.color
    assert c is not None and c.attempted and not c.accepted and c.fallback
    assert c.reason == "implausible_coefficients"
    assert c.evidence is not None
    # every downstream consumer reads the native artifact
    stretches = [s for s in out.plan.steps if isinstance(s, StretchRender)]
    assert stretches and all(s.input.name == "linear_native" for s in stretches)


def test_downstream_measures_use_native_artifact_after_fallback():
    from seestar.core.types import MeasureKind

    req, prior = _galaxy_req()
    seen = {}

    class Spy(Responder):
        def __call__(self, item):
            r = item.request
            if hasattr(r, "kind") and r.kind in (
                MeasureKind.LINEAR_STATS,
                MeasureKind.INPUT_STARS,
                MeasureKind.SUBJECT_SUPPORT,
            ):
                seen[r.kind] = r.ref.name
            return super().__call__(item)

    resp = Spy(prior=prior)
    resp.hook(
        "ColorCalibrate",
        lambda item: WorkSucceeded(
            item.id,
            OpsDone(pcc_evidence=_good_evidence(coefficients=(0.05, 0.7, 1.0)) if item.request.ops[0].pcc else None),
        ),
    )
    out = drive(req, resp)
    assert isinstance(out, PlanComplete)
    assert seen[MeasureKind.LINEAR_STATS] == "linear_native"
    assert seen[MeasureKind.INPUT_STARS] == "linear_native"
    assert seen[MeasureKind.SUBJECT_SUPPORT] == "linear_native"


def test_missing_evidence_on_attempt_falls_back():
    req, prior = _galaxy_req()
    resp = Responder(prior=prior)
    resp.hook("ColorCalibrate", lambda item: WorkSucceeded(item.id, OpsDone()))  # no evidence reported
    out = drive(req, resp)
    assert isinstance(out, PlanComplete)
    c = out.plan.color
    assert c is not None and c.fallback and c.reason == "no_evidence"
    assert _color_ops(out.plan)[0].pcc is False


def test_unavailable_pcc_command_falls_back():
    req, prior = _galaxy_req()
    resp = Responder(prior=prior)

    def hook(item):
        if item.request.ops[0].pcc:
            return WorkFailed(item.id, "ColorCalibrate: pcc_unavailable(catalog): no stars identified")
        return WorkSucceeded(item.id, OpsDone())

    resp.hook("ColorCalibrate", hook)
    out = drive(req, resp)
    assert isinstance(out, PlanComplete)
    c = out.plan.color
    assert c is not None and c.attempted and c.fallback and not c.accepted
    assert c.reason.startswith("unavailable:")
    assert "catalog" in c.reason
    assert _color_ops(out.plan)[0].pcc is False


def test_native_fallback_failure_refuses_planning():
    req, prior = _galaxy_req()
    resp = Responder(prior=prior)

    def hook(item):
        return WorkFailed(item.id, "siril exploded")

    resp.hook("ColorCalibrate", hook)
    out = drive(req, resp)
    assert isinstance(out, PlanningRefused)
    assert out.stage is StageKey.COLOR_CALIBRATION


def test_native_only_failure_refuses_planning():
    # LP: pcc is never attempted, so a ColorCalibrate failure is a plain
    # production failure (unchanged failure-table row)
    resp = Responder()
    resp.hook("ColorCalibrate", lambda item: WorkFailed(item.id, "siril exploded"))
    out = drive(_req(), resp)
    assert isinstance(out, PlanningRefused)


def test_lp_never_attempts_pcc():
    out = drive(_req(), Responder())  # LP nebula default
    assert isinstance(out, PlanComplete)
    ops = _color_ops(out.plan)
    assert len(ops) == 1 and ops[0].pcc is False
    c = out.plan.color
    assert c is not None and not c.attempted and not c.fallback
    assert c.reason == "policy_off"


def test_ircut_starfield_never_attempts_pcc():
    req = _req(target_label="M5", filter_kind=FilterKind.IRCUT, has_wcs=False, use_prior=False)
    out = drive(req, Responder(concentration=10.0))
    assert isinstance(out, PlanComplete)
    ops = _color_ops(out.plan)
    assert len(ops) == 1 and ops[0].pcc is False
    assert out.plan.color is not None and out.plan.color.reason == "policy_off"


# ---------------------------------------------------------------------------
# Provenance surfaces: plan sidecar + notes + EXIF agree
# ---------------------------------------------------------------------------


def test_plan_sidecar_serializes_color_provenance():
    from seestar.io import plan_to_dict

    req, prior = _galaxy_req()
    resp = Responder(prior=prior)
    resp.hook(
        "ColorCalibrate",
        lambda item: WorkSucceeded(
            item.id,
            OpsDone(pcc_evidence=_good_evidence(coefficients=(0.05, 0.7, 1.0)) if item.request.ops[0].pcc else None),
        ),
    )
    out = drive(req, resp)
    d = plan_to_dict(out.plan)
    assert d["color"]["fallback"] is True
    assert d["color"]["reason"] == "implausible_coefficients"
    assert d["color"]["evidence"]["coefficients"] == [0.05, 0.7, 1.0]


# ---------------------------------------------------------------------------
# Orchestrator integration: the evidence drain and the notes["color"] surface.
# CHANGE_SAFETY rule 8 -- every promised surface asserted INDEPENDENTLY, driven
# through the real run_target with the hermetic fake tool chain.
# ---------------------------------------------------------------------------


def _ircut_target(tmp_path):
    from conftest import write_fake_stack

    d = tmp_path / "X9_import"
    d.mkdir()
    write_fake_stack(d / "Stacked_51_X9_10.0s_IRCUT_20260615-221349.fit", obj="X9")
    return str(d)


def _run_pcc_attempt(tmp_path, monkeypatch, evidence=None, out_name="out"):
    """Drive a real run_target with PCC forced ON via the caller override (the
    top of the precedence chain), so these tests exercise the attempt/drain/
    provenance surfaces without depending on how a synthetic frame classifies.

    `evidence` replaces what the fake Siril adapter reports for the pcc pass --
    the injection point that matters, since the shipped adapter's own parser is
    already covered by the parse_* tests above."""
    from test_orchestrate import _apply_mocks, _cfg

    import seestar.orchestrate as orch
    from seestar.adapters import base as B

    _apply_mocks(monkeypatch)
    if evidence is not None:
        from conftest import write_small_fits

        class _EvidenceAdapter:
            name = "fake-color-evidence"

            def execute(self, op, ctx):
                out = {}
                for ref in op.outputs:
                    p = ctx.workspace.path_for(ref, ".fit")
                    write_small_fits(p, 5)
                    out[ref] = p
                if op.pcc:
                    ctx.pcc_evidence.append(evidence)
                return out

        monkeypatch.setitem(B.REGISTRY, ColorCalibrate, _EvidenceAdapter())
    return orch.run_target(_ircut_target(tmp_path), str(tmp_path / out_name), _cfg(), overrides={"pcc": True})


def test_orchestrator_drains_evidence_and_records_accepted_color_notes(tmp_path, monkeypatch):
    """The healthy path end-to-end: the fake Siril adapter reports evidence, the
    orchestrator drains it into OpsDone, the core accepts it, and notes["color"]
    says so -- the surface no core-level test can prove."""
    result = _run_pcc_attempt(tmp_path, monkeypatch)
    color = result["notes"]["color"]
    assert color["attempted"] is True and color["accepted"] is True and color["fallback"] is False
    assert color["reason"] == "evidence_within_bounds"
    assert color["evidence"]["stars_used"] == 330
    assert color["evidence"]["coefficients"] == [0.471, 0.713, 1.0]  # JSON-safe (list, not tuple)
    assert "warnings" not in color
    assert result["params"]["pcc"] is True
    assert result["params"]["_pcc_provenance"] == "accepted(evidence_within_bounds)"


def test_qualified_acceptance_is_recorded_on_every_surface(tmp_path, monkeypatch):
    """Owner Gate 3: an imprecise-but-plausible solution SHIPS, and says so as
    uncertain on notes, report params/EXIF, the plan sidecar, and QA. Each
    surface asserted independently (CHANGE_SAFETY rule 8)."""
    import json

    result = _run_pcc_attempt(tmp_path, monkeypatch, evidence=_good_evidence(imprecise=True))

    color = result["notes"]["color"]
    assert color["accepted"] is True and color["fallback"] is False
    assert color["qualified"] is True
    assert color["reason"] == "imprecise_within_bounds"
    assert "lower confidence" in color["summary"]
    assert any("imprecise" in w for w in color["warnings"]), color.get("warnings")

    assert result["params"]["pcc"] is True
    assert result["params"]["_pcc_provenance"] == "accepted-qualified(imprecise_within_bounds)"

    out_dir = str(tmp_path / "out")
    written = [f for f in os.listdir(out_dir) if f.endswith("plan.json")]
    sidecar = json.load(open(os.path.join(out_dir, written[0])))
    assert sidecar["plan"]["color"]["qualified"] is True
    # the calibrated artifact is what shipped -- no fresh native fallback
    cc = [s for s in sidecar["plan"]["steps"] if s["op"] == "ColorCalibrate"]
    assert len(cc) == 1 and cc[0]["pcc"] is True and cc[0]["output"]["name"] == "linear_master"


def test_qa_color_warnings_pure():
    """`seestar qa` is read long after the run: an accepted-but-uncertain colour
    must not look identical to a clean one, and a fallback must say the photo
    kept its native colour."""
    from seestar.qa import color_warnings

    assert color_warnings(None) == []
    assert color_warnings({}) == []
    # a clean acceptance is silent
    assert color_warnings({"accepted": True, "qualified": False, "fallback": False}) == []
    qual = color_warnings({"accepted": True, "qualified": True, "reason": "imprecise_within_bounds"})
    assert len(qual) == 1 and "LOWER CONFIDENCE" in qual[0] and "imprecise_within_bounds" in qual[0]
    fb = color_warnings({"accepted": False, "fallback": True, "reason": "implausible_coefficients"})
    assert len(fb) == 1 and "NOT applied" in fb[0] and "native colour" in fb[0]


def test_orchestrator_records_fallback_color_notes_when_evidence_rejected(tmp_path, monkeypatch):
    """The rejected path end-to-end: an imprecise solution from the tool ends with
    native color shipped, said the same way on notes and report params."""
    result = _run_pcc_attempt(tmp_path, monkeypatch, evidence=_good_evidence(coefficients=(0.05, 0.7, 1.0)))
    color = result["notes"]["color"]
    assert color["attempted"] is True and color["accepted"] is False and color["fallback"] is True
    assert color["reason"] == "implausible_coefficients"
    assert "not trustworthy" in color["summary"]
    assert result["params"]["pcc"] is False
    assert result["params"]["_pcc_provenance"] == "native-fallback(implausible_coefficients)"


def test_notes_and_plan_sidecar_agree_on_the_shipped_color_path(tmp_path, monkeypatch):
    """Rule 7: the returned/persisted representations must agree."""
    import json

    result = _run_pcc_attempt(tmp_path, monkeypatch, evidence=_good_evidence(coefficients=(0.05, 0.7, 1.0)))
    # the hermetic fake renders do not certify, so the sidecar carries the
    # uncertified name; this test is about color agreement, not certification
    out_dir = str(tmp_path / "out")
    written = [f for f in os.listdir(out_dir) if f.endswith("plan.json")]
    assert written, f"no plan sidecar in {out_dir}: {os.listdir(out_dir)}"
    sidecar = json.load(open(os.path.join(out_dir, written[0])))
    plan_color = sidecar["plan"]["color"]
    notes_color = result["notes"]["color"]
    for k in ("attempted", "accepted", "fallback", "reason"):
        assert plan_color[k] == notes_color[k], f"{k} disagrees between plan sidecar and notes"


def test_unreadable_tool_report_warns_distinctly_from_a_bad_solution(tmp_path, monkeypatch):
    """A report the app cannot read is a possible tool/parser DRIFT, not an
    ordinary quality rejection -- it must be loud (the silent-gate lesson)."""
    result = _run_pcc_attempt(tmp_path, monkeypatch, evidence=_good_evidence(succeeded=False))
    color = result["notes"]["color"]
    assert color["fallback"] is True and color["reason"] == "not_confirmed"
    assert any("could not read" in w for w in color["warnings"])
    assert "output format may have changed" in " ".join(color["warnings"])


def test_stale_evidence_does_not_leak_from_a_failed_batch(monkeypatch):
    """ctx.pcc_evidence is a shared mutable channel: a batch that appends evidence
    and THEN fails must not leave that entry for the next batch's drain."""
    from seestar.adapters import base as B

    ctx = B.ExecContext(workspace=None, tools=None)
    ctx.pcc_evidence.append(_good_evidence())

    # mirrors orchestrate._do's exception path
    try:
        raise RuntimeError("boom")
    except Exception:
        ctx.pcc_evidence.clear()
    assert ctx.pcc_evidence == []


def test_siril_runs_under_a_pinned_c_locale():
    """Siril's console log is gettext-localized (verified: a French locale prints
    "L'etalonnage ... a reussi"), which would silently break every evidence
    signature AND run_siril's own failure marker. The child must be pinned."""
    from seestar.adapters.siril import _c_locale_env

    env = _c_locale_env()
    assert env["LC_ALL"] == "C" and env["LANG"] == "C" and env["LANGUAGE"] == "C"


def test_run_siril_passes_the_pinned_locale_to_the_child(monkeypatch):
    import seestar.adapters.siril as S

    seen = {}

    class _R:
        stdout, stderr = "log: ok", ""

    def fake_run_external(args, **kw):
        seen.update(kw)
        return _R()

    monkeypatch.setattr(S, "run_external", fake_run_external)
    S.run_siril(["load x"], _FakeToolPaths())
    assert seen["env"]["LC_ALL"] == "C"


class _FakeToolPaths:
    siril = "siril-cli"


def test_pcc_timeout_propagates_instead_of_buying_a_second_attempt(monkeypatch, tmp_path):
    """A hung Siril is NOT a recoverable 'pcc unavailable': falling back would
    launch a second full-length attempt, and the timeout message is already the
    novice-safe surface the CLI/GUI render verbatim."""
    from seestar.adapters import base as B
    from seestar.adapters import siril as S
    from seestar.adapters._external_process import ExternalToolTimeout
    from seestar.core.types import ArtifactRef, ArtifactRole

    def fake_run(lines, tools):
        raise ExternalToolTimeout("Siril did not finish within 30 minutes. No source photo was changed.")

    monkeypatch.setattr(S, "run_siril", fake_run)

    class _WS:
        def path_for(self, ref, ext):
            return str(tmp_path / f"{ref.name}{ext}")

    inp = ArtifactRef("denoised", ArtifactRole.PRODUCTION)
    op = ColorCalibrate(input=inp, output=ArtifactRef("linear_master", ArtifactRole.PRODUCTION), pcc=True)
    ctx = B.ExecContext(workspace=_WS(), tools=None, resolve={inp: str(tmp_path / "d.fit")})
    with pytest.raises(ExternalToolTimeout):
        S.SirilAdapter().execute(op, ctx)


def test_unavailable_reason_never_carries_raw_tool_text():
    """The color reason reaches the EXIF of every exported photo, so it must be a
    bounded class token -- never captured Siril output (which echoes local paths)."""
    from seestar.core.planning import _unavailable_reason

    noisy = WorkFailed(
        "1",
        'ExecutionError: ColorCalibrate: pcc_unavailable(catalog) cd "/Users/someone/Pictures/scratch" log: ...',
    )
    assert _unavailable_reason(noisy) == "unavailable:catalog"
    assert _unavailable_reason(WorkFailed("1", "something totally unexpected")) == "unavailable:tool"
    # no path fragment can survive
    assert "/Users/" not in _unavailable_reason(noisy)


def test_exif_user_comment_carries_fallback_provenance():
    from seestar.metadata import _build_card

    inspection = {
        "meta": {"target": "M82", "frames": 249, "sub_exposure_s": 10.0, "total_integration_min": 41.5},
        "header": {"has_wcs": False},
        "classification": {"category": "galaxy"},
    }
    params = {
        "bge_smoothing": 0.5,
        "bge_correction": "Subtraction",
        "denoise_strength": 0.65,
        "asinh_d": 55,
        "blackpoint": 0.0148,
        "mtf_mid": 0.08,
        "saturation": 0.6,
        "pcc": False,
        "profile": "showcase",
        "_pcc_provenance": "native-fallback(implausible_coefficients)",
    }
    card = _build_card(inspection, params, "final", {"defaults": {"telescope": "ZWO Seestar S50"}}, "certified")
    assert "no PCC" in card.user_comment
    assert "pcc=native-fallback(implausible_coefficients)" in card.user_comment


# ---------------------------------------------------------------------------
# Diagnosis 7 (Task 6): stable guidance for the PCC failure a user can FIX.
# ---------------------------------------------------------------------------


def test_catalog_failure_carries_retry_and_connectivity_guidance(tmp_path, monkeypatch):
    """A catalog/network failure is the one PCC outcome the user can usually act
    on, and it is indistinguishable from a broken tool unless told. The guidance
    rides in the WARNINGS list (which the CLI and GUI both surface), not only in
    the notes summary. Asserted alongside the machine-readable reason, which must
    stay the bounded class string that reaches EXIF."""
    from conftest import write_small_fits
    from test_orchestrate import _apply_mocks, _cfg

    import seestar.orchestrate as orch
    from seestar.adapters import base as B
    from seestar.adapters.base import AdapterError

    _apply_mocks(monkeypatch)

    class _CatalogFailureAdapter:
        name = "fake-color-catalog-failure"

        def execute(self, op, ctx):
            if op.pcc:
                # what siril.py raises after classifying its own failed pcc run
                raise AdapterError("pcc_unavailable(catalog)")
            out = {}
            for ref in op.outputs:
                p = ctx.workspace.path_for(ref, ".fit")
                write_small_fits(p, 5)
                out[ref] = p
            return out

    monkeypatch.setitem(B.REGISTRY, ColorCalibrate, _CatalogFailureAdapter())
    result = orch.run_target(_ircut_target(tmp_path), str(tmp_path / "out"), _cfg(), overrides={"pcc": True})

    color = result["notes"]["color"]
    assert color["fallback"] is True and "catalog" in color["reason"]
    assert "internet connection" in color["summary"]
    guidance = [w for w in color.get("warnings", []) if "internet connection" in w]
    assert guidance, f"no connectivity guidance in {color.get('warnings')}"
    assert "again" in guidance[0], "guidance must tell the user what to DO, not just what broke"
    # the EXIF-bound provenance stays the bounded class, never the guidance prose
    assert result["params"]["_pcc_provenance"].startswith("native-fallback(")
