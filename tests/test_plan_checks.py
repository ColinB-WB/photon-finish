from seestar.core import types as T
from seestar.core.plan_checks import validate_plan


def _ref(name, role=T.ArtifactRole.PRODUCTION):
    return T.ArtifactRef(name=name, role=role)


def _chain():
    src = _ref("work", T.ArtifactRole.SOURCE)
    lin = _ref("linear_master")
    stretched = _ref("stretched")
    prep = T.PrepareSource(session_index=0, output=src)
    color = T.ColorCalibrate(input=src, output=lin, pcc=False)
    stretch = T.StretchRender(
        input=lin, output=stretched, asinh_d=75, blackpoint=0.01, mtf_mid=0.08, saturation=0.6, ght=None, black_lift=0.0
    )
    exp = T.Export(input=stretched, output_base="X_final")
    return prep, color, stretch, exp


def test_valid_linear_plan():
    prep, color, stretch, exp = _chain()
    plan = T.Plan(steps=(prep, color, stretch, exp), deliverables=exp.outputs, solve=None)
    assert validate_plan(plan) == ()


def test_consumed_before_produced_fails():
    prep, color, stretch, exp = _chain()
    plan = T.Plan(steps=(color, prep, stretch, exp), deliverables=exp.outputs, solve=None)
    errs = validate_plan(plan)
    assert any("not produced" in e for e in errs)


def test_missing_deliverable_fails():
    prep, color, stretch, exp = _chain()
    ghost = _ref("ghost.jpg", T.ArtifactRole.DELIVERABLE)
    plan = T.Plan(steps=(prep, color, stretch, exp), deliverables=(*exp.outputs, ghost), solve=None)
    assert any("ghost.jpg" in e for e in validate_plan(plan))


def test_orphan_production_fails():
    prep, color, stretch, exp = _chain()
    orphan = T.Denoise(input=color.output, output=_ref("unused"), strength=0.5)
    plan = T.Plan(steps=(prep, color, orphan, stretch, exp), deliverables=exp.outputs, solve=None)
    assert any("orphan" in e for e in validate_plan(plan))


def test_probe_role_forbidden_in_final_plan():
    prep, color, stretch, exp = _chain()
    probe = T.StretchRender(
        input=color.output,
        output=_ref("p", T.ArtifactRole.PROBE),
        asinh_d=75,
        blackpoint=0.01,
        mtf_mid=0.1,
        saturation=0.6,
        ght=None,
        black_lift=0.0,
    )
    plan = T.Plan(steps=(prep, color, probe, stretch, exp), deliverables=exp.outputs, solve=None)
    assert any("probe" in e.lower() for e in validate_plan(plan))


def test_duplicate_production_fails():
    prep, color, stretch, exp = _chain()
    plan = T.Plan(steps=(prep, color, color, stretch, exp), deliverables=exp.outputs, solve=None)
    assert any("twice" in e for e in validate_plan(plan))


def test_byproduct_stars_exempt_from_orphan_check():
    # cluster QA path: SeparateStars' starless is QA (kept), the stars layer is
    # PRODUCTION scratch nobody consumes -- a byproduct, NOT an orphan.
    prep, color, stretch, exp = _chain()
    sep = T.SeparateStars(
        input=stretch.output,
        starless=_ref("X_starless", T.ArtifactRole.QA),
        stars=_ref("cluster_stars"),
    )
    plan = T.Plan(steps=(prep, color, stretch, exp, sep), deliverables=exp.outputs, solve=None)
    assert validate_plan(plan) == ()


def test_unconsumed_production_starless_still_orphan():
    # only the op's DECLARED byproducts are exempt: an unconsumed PRODUCTION
    # starless (not in SeparateStars.byproducts) is still an orphan.
    prep, color, stretch, exp = _chain()
    sep = T.SeparateStars(
        input=stretch.output,
        starless=_ref("starless"),
        stars=_ref("stars"),
    )
    plan = T.Plan(steps=(prep, color, stretch, exp, sep), deliverables=exp.outputs, solve=None)
    errs = validate_plan(plan)
    assert any("orphan" in e and "starless" in e for e in errs)
    assert not any("'stars'" in e for e in errs)  # the stars byproduct stays exempt
