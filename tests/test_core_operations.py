import dataclasses

import pytest

from seestar.core import types as T


def _ref(name, role=T.ArtifactRole.PRODUCTION):
    return T.ArtifactRef(name=name, role=role)


def test_every_operation_is_frozen_with_stage_and_refs():
    src = _ref("working_copy", T.ArtifactRole.SOURCE)
    ops = [
        T.PrepareSource(session_index=0, output=src),
        T.CombineSessions(session_indices=(0, 1), output=src),
        T.Crop(
            input=src,
            output=_ref("cropped"),
            x=28,
            y=16,
            width=1864,
            height=1048,
            mirror_x=True,
        ),
        T.RemoveTrail(input=_ref("cropped"), output=_ref("detrailed")),
        T.BackgroundExtract(input=_ref("cropped"), output=_ref("bge"), smoothing=0.5, correction="Subtraction"),
        T.Denoise(input=_ref("bge"), output=_ref("denoised"), strength=0.6),
        T.ColorCalibrate(input=_ref("denoised"), output=_ref("linear_master"), pcc=False),
        T.StretchRender(
            input=_ref("linear_master"),
            output=_ref("stretched"),
            asinh_d=75,
            blackpoint=0.0148,
            mtf_mid=0.08,
            saturation=0.6,
            ght=None,
            black_lift=0.0,
        ),
        T.SeparateStars(input=_ref("stretched"), starless=_ref("starless"), stars=_ref("stars")),
        T.ScreenBlend(starless=_ref("starless"), stars=_ref("stars"), output=_ref("reduced"), k=0.45),
        T.CropTo(input=_ref("stretched"), output=_ref("closeup"), box=(10, 10, 640, 360)),
        T.Export(input=_ref("stretched"), output_base="M8_final"),
    ]
    for op in ops:
        assert isinstance(op.stage, T.StageKey)
        assert isinstance(op.inputs, tuple) and isinstance(op.outputs, tuple)
        with pytest.raises(dataclasses.FrozenInstanceError):
            op.stage = T.StageKey.EXPORT


def test_export_outputs_one_deliverable_per_format():
    e = T.Export(input=_ref("stretched"), output_base="M8_final")
    names = [r.name for r in e.outputs]
    assert names == ["M8_final.tif", "M8_final.png", "M8_final.jpg"]
    assert all(r.role is T.ArtifactRole.DELIVERABLE for r in e.outputs)


def test_plan_is_frozen_and_path_free():
    e = T.Export(input=_ref("stretched"), output_base="X_final")
    plan = T.Plan(steps=(e,), deliverables=e.outputs, solve=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.steps = ()
    # equality-testable intent (spec section 5)
    plan2 = T.Plan(
        steps=(T.Export(input=_ref("stretched"), output_base="X_final"),), deliverables=e.outputs, solve=None
    )
    assert plan == plan2


def test_stage_values_match_legacy_progress_keys():
    legacy = {
        "analyze",
        "prior",
        "crop",
        "trail",
        "bge",
        "probe",
        "denoise",
        "color",
        "solve_stretch",
        "stretch",
        "starless",
        "star_reduce",
        "closeup",
        "export",
    }
    assert legacy <= {k.value for k in T.StageKey}
