"""Plan/execution sidecar serialization (Task 19, step 1): round-trip a small Plan
through plan_to_dict/json, and prove write_plan_sidecar is atomic (no partial file
on an injected os.replace failure)."""

from __future__ import annotations

import json
import os

import pytest

from seestar import io as IO
from seestar.core.types import (
    ArtifactRef,
    ArtifactRole,
    Crop,
    Denoise,
    Export,
    Plan,
    SolveProvenance,
    StretchRender,
    TargetMetric,
)
from seestar.execute import ExecutionReport, OpRecord


def _small_plan() -> Plan:
    src = ArtifactRef("working_copy", ArtifactRole.SOURCE)
    cropped = ArtifactRef("cropped", ArtifactRole.PRODUCTION)
    denoised = ArtifactRef("denoised", ArtifactRole.PRODUCTION)
    stretched = ArtifactRef("stretched", ArtifactRole.PRODUCTION)
    steps = (
        Crop(input=src, output=cropped, x=1, y=1, width=62, height=62, mirror_x=True),
        Denoise(input=cropped, output=denoised, strength=0.6),
        StretchRender(
            input=denoised,
            output=stretched,
            asinh_d=75,
            blackpoint=0.014,
            mtf_mid=0.08,
            saturation=0.6,
            ght=None,
            black_lift=0.0,
        ),
        Export(input=stretched, output_base="X1_final"),
    )
    deliverables = steps[-1].outputs
    solve = SolveProvenance(
        metric=TargetMetric.BODY_P90,
        target_level=72.0,
        achieved=72.0,
        sky=9.0,
        mtf_mid=0.08,
        blackpoint=0.014,
        converged=True,
        mtf_pinned=None,
        bp_pinned=False,
        bp_iterations=0,
        ght_in_probe=False,
        level_base=72.0,
        level_scale=1.0,
        asinh_d=75,
        probes=(),
    )
    return Plan(steps=steps, deliverables=deliverables, solve=solve)


def test_plan_to_dict_round_trips_and_tags_ops():
    plan = _small_plan()
    d = IO.plan_to_dict(plan)
    text = json.dumps(d)  # must be JSON-serializable
    assert text

    ops = [s["op"] for s in d["steps"]]
    assert ops == ["Crop", "Denoise", "StretchRender", "Export"]

    # ArtifactRef -> {"name","role"}, role enum -> .value
    crop = d["steps"][0]
    assert crop["input"] == {"name": "working_copy", "role": "source"}
    assert crop["output"] == {"name": "cropped", "role": "production"}

    # enums serialized to their .value everywhere
    assert d["solve"]["metric"] == "body_p90"
    assert all(dv["role"] == "deliverable" for dv in d["deliverables"])

    # nested GhsParams|None stays None
    assert d["steps"][2]["ght"] is None


def test_write_plan_sidecar_atomic(tmp_path, monkeypatch):
    plan = _small_plan()
    report = ExecutionReport(
        records=[
            OpRecord(
                op_key="k1",
                stage="crop",
                adapter="fake",
                status="ok",
                duration_s=0.0,
                outputs=(("cropped", "/x/cropped.fit", 10),),
            ),
        ]
    )
    target = str(tmp_path / "X1_plan.json")

    # happy path writes the file with the documented shape
    IO.write_plan_sidecar(target, plan, report)
    doc = json.loads(open(target, encoding="utf-8").read())
    assert doc["plan_format"] == 1
    assert set(doc) == {"plan_format", "plan", "execution"}
    assert doc["execution"]["records"][0]["op_key"] == "k1"

    os.remove(target)

    # injected os.replace failure -> no partial target file, and the temp is cleaned
    def _boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(IO.os, "replace", _boom)
    with pytest.raises(OSError):
        IO.write_plan_sidecar(target, plan, report)
    assert not os.path.exists(target)
    assert not os.path.exists(target + ".tmp")


def _report_as_at_92_103() -> ExecutionReport:
    """Same ExecutionReport construction as test_write_plan_sidecar_atomic (:92-103)."""
    return ExecutionReport(
        records=[
            OpRecord(
                op_key="k1",
                stage="crop",
                adapter="fake",
                status="ok",
                duration_s=0.0,
                outputs=(("cropped", "/x/cropped.fit", 10),),
            ),
        ]
    )


def test_write_plan_sidecar_records_published_mapping(tmp_path):
    plan = _small_plan()
    report = _report_as_at_92_103()
    p = tmp_path / "M_uncertified_plan.json"
    IO.write_plan_sidecar(str(p), plan, report, published={"certified": False, "outputs": {"M_final": "M_uncertified"}})
    doc = json.loads(p.read_text())
    assert doc["execution"]["published"] == {"certified": False, "outputs": {"M_final": "M_uncertified"}}
    assert doc["plan_format"] == 1  # additive: no format bump


def test_write_plan_sidecar_without_published_is_unchanged(tmp_path):
    plan = _small_plan()
    report = _report_as_at_92_103()
    p = tmp_path / "M_plan.json"
    IO.write_plan_sidecar(str(p), plan, report)
    assert "published" not in json.loads(p.read_text())["execution"]


def test_write_run_manifest_atomic(tmp_path, monkeypatch):
    """write_run_manifest uses the same atomic pattern (temp file + os.replace)
    as write_plan_sidecar. Test that on an injected os.replace failure, no
    partial target file remains and the temp is cleaned up."""
    manifest = IO.run_manifest_to_dict(
        attempt=1,
        source_identity={"target": "test", "filter": "LP"},
        deliverables=[{"name": "test_final.jpg", "role": "deliverable", "bytes": 102400}],
        plan_sidecar="test_plan.json",
        notes_sidecar="test_notes.json",
        accepted_at="2026-07-15T12:00:00Z",
    )
    target = str(tmp_path / "test_run.json")

    # happy path writes the file with the documented shape
    IO.write_run_manifest(target, manifest)
    doc = json.loads(open(target, encoding="utf-8").read())
    assert doc["manifest_format"] == 1
    assert set(doc) == {
        "manifest_format",
        "attempt",
        "outcome",
        "source_identity",
        "deliverables",
        "qa_artifacts",
        "superseded",
        "plan_sidecar",
        "notes_sidecar",
        "accepted_at",
    }
    assert doc["attempt"] == 1
    assert doc["outcome"] == "pass"  # Phase 3C: defaults to pass when unspecified

    os.remove(target)

    # injected os.replace failure -> no partial target file, and the temp is cleaned
    def _boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(IO.os, "replace", _boom)
    with pytest.raises(OSError):
        IO.write_run_manifest(target, manifest)
    assert not os.path.exists(target)
    assert not os.path.exists(target + ".tmp")


def test_plan_to_dict_serializes_probe_history():
    from seestar.core.types import ProbeRecord

    plan = _small_plan()
    solve = plan.solve
    plan = Plan(
        steps=plan.steps,
        deliverables=plan.deliverables,
        solve=SolveProvenance(
            metric=solve.metric,
            target_level=solve.target_level,
            achieved=solve.achieved,
            sky=solve.sky,
            mtf_mid=solve.mtf_mid,
            blackpoint=solve.blackpoint,
            converged=solve.converged,
            mtf_pinned=solve.mtf_pinned,
            bp_pinned=solve.bp_pinned,
            bp_iterations=solve.bp_iterations,
            ght_in_probe=solve.ght_in_probe,
            level_base=solve.level_base,
            level_scale=solve.level_scale,
            asinh_d=solve.asinh_d,
            probes=(
                ProbeRecord(
                    phase="mtf",
                    mtf_mid=0.07,
                    blackpoint=0.015,
                    value=71.8,
                    sky=11.0,
                    target_level=72.0,
                    distance=0.2,
                ),
            ),
        ),
    )
    d = IO.plan_to_dict(plan)
    assert d["solve"]["probes"][0]["mtf_mid"] == 0.07
    assert d["solve"]["probes"][0]["phase"] == "mtf"
