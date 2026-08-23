import dataclasses
import os

import pytest

from seestar.adapters import base as B
from seestar.core.types import ArtifactRef, ArtifactRole, Denoise, Plan
from seestar.execute import (
    ExecutionError,
    ExecutionReport,
    assert_no_duplicate_work,
    execute_ops,
    execute_plan,
)
from seestar.workspace import Workspace


class FakeAdapter:
    name = "fake"
    operations = (Denoise,)

    def __init__(self):
        self.calls = []

    def execute(self, op, ctx):
        self.calls.append(op)
        p = ctx.workspace.path_for(op.output, ".fit")
        open(p, "w").write("data")
        return {op.output: p}


@pytest.fixture
def fake_registry(monkeypatch):
    fake = FakeAdapter()
    monkeypatch.setattr(B, "REGISTRY", {Denoise: fake})
    return fake


def _op(i="a", o="b"):
    return Denoise(
        input=ArtifactRef(i, ArtifactRole.PRODUCTION), output=ArtifactRef(o, ArtifactRole.PRODUCTION), strength=0.6
    )


def test_executes_and_registers(tmp_path, fake_registry):
    ws = Workspace(str(tmp_path))
    src = ArtifactRef("a", ArtifactRole.PRODUCTION)
    p0 = ws.path_for(src, ".fit")
    open(p0, "w").write("x")
    ws.register("seed", src, p0)
    op = _op()
    rep = execute_ops((op,), B.ExecContext(workspace=ws, tools=None))
    assert [r.status for r in rep.records] == ["ok"]
    assert ws.lookup(repr(op), op.output) is not None


def test_reuse_skips_adapter(tmp_path, fake_registry):
    ws = Workspace(str(tmp_path))
    src = ArtifactRef("a", ArtifactRole.PRODUCTION)
    p0 = ws.path_for(src, ".fit")
    open(p0, "w").write("x")
    ws.register("seed", src, p0)
    op = _op()
    pre = ws.path_for(op.output, ".fit")
    open(pre, "w").write("planned")
    ws.register(repr(op), op.output, pre)  # produced during planning
    rep = execute_ops((op,), B.ExecContext(workspace=ws, tools=None))
    assert rep.records[0].status == "reused" and fake_registry.calls == []


def test_missing_input_fails(tmp_path, fake_registry):
    ws = Workspace(str(tmp_path))
    with pytest.raises(ExecutionError):
        execute_ops((_op(),), B.ExecContext(workspace=ws, tools=None))


def test_on_stage_announces_op_before_adapter_runs(tmp_path, fake_registry):
    """Progress honesty: the stage callback fires when an op STARTS, so the GUI
    label is on screen during the work — a completion-time event showed each
    stage only after it finished, making fast early steps invisible."""
    ws = Workspace(str(tmp_path))
    src = ArtifactRef("a", ArtifactRole.PRODUCTION)
    p0 = ws.path_for(src, ".fit")
    open(p0, "w").write("x")
    ws.register("seed", src, p0)
    events = []
    orig = fake_registry.execute

    def logged_execute(op, ctx):
        events.append(("exec", op.stage.value))
        return orig(op, ctx)

    fake_registry.execute = logged_execute
    op = _op()
    execute_ops((op,), B.ExecContext(workspace=ws, tools=None), on_stage=lambda s: events.append(("stage", s.value)))
    assert events == [("stage", op.stage.value), ("exec", op.stage.value)]


def test_on_stage_fires_for_failing_op(tmp_path, fake_registry):
    """A failing op still announces its stage first — the user sees WHICH stage
    was running when the failure surfaced, instead of a stale earlier label."""
    ws = Workspace(str(tmp_path))
    src = ArtifactRef("a", ArtifactRole.PRODUCTION)
    p0 = ws.path_for(src, ".fit")
    open(p0, "w").write("x")
    ws.register("seed", src, p0)
    events = []

    def failing_execute(op, ctx):
        raise B.AdapterError("tool exploded")

    fake_registry.execute = failing_execute
    with pytest.raises(ExecutionError):
        execute_ops((_op(),), B.ExecContext(workspace=ws, tools=None), on_stage=lambda s: events.append(s.value))
    assert events == [_op().stage.value]


def test_unrouted_op_records_failed_and_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "REGISTRY", {})  # nothing routed
    ws = Workspace(str(tmp_path))
    src = ArtifactRef("a", ArtifactRole.PRODUCTION)
    p0 = ws.path_for(src, ".fit")
    open(p0, "w").write("x")
    ws.register("seed", src, p0)
    rep = ExecutionReport()
    with pytest.raises(ExecutionError):
        execute_ops((_op(),), B.ExecContext(workspace=ws, tools=None), report=rep)
    assert [r.status for r in rep.records] == ["failed"]
    assert rep.records[0].adapter == "<unrouted>"
    assert rep.records[0].duration_s == 0.0


def test_execute_plan_validates_first(tmp_path, fake_registry):
    ws = Workspace(str(tmp_path))
    op = _op()  # consumes 'a' which no step produces -> invalid plan
    plan = Plan(steps=(op,), deliverables=(), solve=None)
    with pytest.raises(ExecutionError, match="invalid plan"):
        execute_plan(plan, B.ExecContext(workspace=ws, tools=None))
    assert fake_registry.calls == []  # validation aborted before any adapter ran


def test_three_op_chain_order_and_flow(tmp_path, fake_registry):
    ws = Workspace(str(tmp_path))
    src = ArtifactRef("a", ArtifactRole.PRODUCTION)
    p0 = ws.path_for(src, ".fit")
    open(p0, "w").write("x")
    ws.register("seed", src, p0)
    ops = (_op("a", "b"), _op("b", "c"), _op("c", "d"))
    ctx = B.ExecContext(workspace=ws, tools=None)
    rep = execute_ops(ops, ctx)
    assert [r.status for r in rep.records] == ["ok", "ok", "ok"]
    assert [r.op_key for r in rep.records] == [repr(op) for op in ops]  # record order = op order
    assert fake_registry.calls == list(ops)  # adapter saw them in order
    for op in ops:  # each op's output registered under its own key...
        assert ws.lookup(repr(op), op.output) is not None
    # ...and each downstream input resolved to its upstream producer's path
    assert ctx.resolve[ops[1].input] == ws.lookup(repr(ops[0]), ops[0].output)
    assert ctx.resolve[ops[2].input] == ws.lookup(repr(ops[1]), ops[1].output)


def test_no_duplicate_work_invariant(tmp_path, fake_registry):
    ws = Workspace(str(tmp_path))
    src = ArtifactRef("a", ArtifactRole.PRODUCTION)
    p0 = ws.path_for(src, ".fit")
    open(p0, "w").write("x")
    ws.register("seed", src, p0)
    op = _op()
    rep = execute_ops((op,), B.ExecContext(workspace=ws, tools=None))
    # simulate a bug: same op executed again into the same report
    os.remove(ws.lookup(repr(op), op.output))  # force re-execution, not reuse
    execute_ops((dataclasses.replace(op),), B.ExecContext(workspace=ws, tools=None), report=rep)
    with pytest.raises(AssertionError):
        assert_no_duplicate_work(rep)


def test_take_measurement_passes_configured_subject_sigma(tmp_path, monkeypatch):
    from seestar import execute as E
    from seestar import framing as F
    from seestar.adapters import base as B
    from seestar.core.types import ArtifactRef, ArtifactRole, Measure, MeasureKind
    from seestar.workspace import Workspace

    seen = {}
    monkeypatch.setattr(
        F,
        "measure_subject",
        lambda starless, threshold_sigma=4.0: (seen.__setitem__("sigma", threshold_sigma), None)[1],
    )

    starless = tmp_path / "s.fit"
    starless.write_bytes(b"x")
    ws = Workspace(str(tmp_path / "out"))
    ref = ArtifactRef("starless", ArtifactRole.PRODUCTION)
    ws.register("producer", ref, str(starless))  # _resolve -> ws.find_producer(ref)
    ctx = B.ExecContext(workspace=ws, tools=None)

    measure = Measure(kind=MeasureKind.SUBJECT_BOX, ref=ref)  # output/metric default None
    cfg = {"engine": {"framing": {"subject_threshold_sigma": 6.5}}}
    E.take_measurement(measure, ctx, cfg, tools=None)
    assert seen["sigma"] == 6.5
