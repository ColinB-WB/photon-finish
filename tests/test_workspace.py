import os

import pytest

from seestar.core.types import ArtifactRef, ArtifactRole
from seestar.workspace import Workspace


def _touch(p):
    open(p, "w").write("x")
    return p


def test_scratch_is_unique_and_run_scoped(tmp_path):
    a, b = Workspace(str(tmp_path)), Workspace(str(tmp_path))
    assert a.scratch_dir != b.scratch_dir
    assert os.path.isdir(a.scratch_dir) and a.scratch_dir.startswith(str(tmp_path))


def test_path_for_routes_by_role(tmp_path):
    ws = Workspace(str(tmp_path))
    d = ws.path_for(ArtifactRef("M8_final.jpg", ArtifactRole.DELIVERABLE), "")
    s = ws.path_for(ArtifactRef("linear_master", ArtifactRole.PRODUCTION), ".fit")
    assert os.path.dirname(d) == ws.output_dir
    assert os.path.dirname(s) == ws.scratch_dir
    assert " " not in os.path.basename(s)


def test_qa_role_routes_to_scratch_not_output(tmp_path):
    """Spec rev 4: QA-role artifacts execute into scratch and reach the output dir
    only via the publication transaction (the M13 starless clobber class)."""
    ws = Workspace(str(tmp_path))
    p = ws.path_for(ArtifactRef("M_starless", ArtifactRole.QA), ".tif")
    assert p.startswith(ws.scratch_dir)


def test_lookup_only_returns_same_run_same_op(tmp_path):
    ws = Workspace(str(tmp_path))
    ref = ArtifactRef("linear_master", ArtifactRole.PRODUCTION)
    p = _touch(ws.path_for(ref, ".fit"))
    ws.register("op-A", ref, p)
    assert ws.lookup("op-A", ref) == p
    assert ws.lookup("op-B", ref) is None  # different producing op -> no adoption
    fresh = Workspace(str(tmp_path))  # new run, empty workspace
    assert fresh.lookup("op-A", ref) is None  # cross-run adoption structurally impossible


def test_same_name_different_roles_register_independently(tmp_path):
    ws = Workspace(str(tmp_path))
    qa_ref = ArtifactRef("M8_starless.tif", ArtifactRole.QA)
    probe_ref = ArtifactRef("M8_starless.tif", ArtifactRole.PROBE)
    qa_path = _touch(ws.path_for(qa_ref, ""))
    probe_path = _touch(os.path.join(ws.scratch_dir, "probe_M8_starless.tif"))
    ws.register("op-A", qa_ref, qa_path)
    ws.register("op-A", probe_ref, probe_path)
    assert ws.lookup("op-A", qa_ref) == qa_path
    assert ws.lookup("op-A", probe_ref) == probe_path
    ws.cleanup()
    # Spec rev 4: QA now routes to scratch (like PROBE), so both registrations
    # are removed by cleanup() -- promotion to output_dir is the publication
    # transaction's job, not the Workspace's.
    assert not os.path.exists(qa_path)


def test_path_for_rejects_unsafe_names(tmp_path):
    ws = Workspace(str(tmp_path))
    with pytest.raises(ValueError, match="unsafe artifact name"):
        ws.path_for(ArtifactRef("evil:stream", ArtifactRole.PRODUCTION), ".fit")
    with pytest.raises(ValueError, match="unsafe artifact name"):
        ws.path_for(ArtifactRef("sub/dir", ArtifactRole.PRODUCTION), ".fit")


def test_lookup_requires_file_to_exist(tmp_path):
    ws = Workspace(str(tmp_path))
    ref = ArtifactRef("x", ArtifactRole.PRODUCTION)
    p = ws.path_for(ref, ".fit")
    ws.register("op-A", ref, p)  # registered but never written
    assert ws.lookup("op-A", ref) is None


def test_find_producer_returns_path_regardless_of_op_key(tmp_path):
    ws = Workspace(str(tmp_path))
    ref = ArtifactRef("linear_master", ArtifactRole.PRODUCTION)
    p = _touch(ws.path_for(ref, ".fit"))
    ws.register("op-A", ref, p)
    assert ws.find_producer(ref) == p
    assert ws.find_producer(ArtifactRef("nope", ArtifactRole.PRODUCTION)) is None


def test_find_producer_ignores_missing_files(tmp_path):
    ws = Workspace(str(tmp_path))
    ref = ArtifactRef("x", ArtifactRole.PRODUCTION)
    p = ws.path_for(ref, ".fit")
    ws.register("op-A", ref, p)  # registered but never written
    assert ws.find_producer(ref) is None


def test_cleanup_removes_scratch_and_probes_only(tmp_path):
    ws = Workspace(str(tmp_path))
    keep = _touch(ws.path_for(ArtifactRef("M8_final.jpg", ArtifactRole.DELIVERABLE), ""))
    qa = _touch(ws.path_for(ArtifactRef("M8_starless.tif", ArtifactRole.QA), ""))
    probe = _touch(ws.path_for(ArtifactRef("probe.jpg", ArtifactRole.PROBE), ""))
    scratch = _touch(ws.path_for(ArtifactRef("linear_master", ArtifactRole.PRODUCTION), ".fit"))
    unrelated = _touch(os.path.join(str(tmp_path), "users_own_file.txt"))  # NOT owned
    for key, ref, p in [
        ("a", ArtifactRef("M8_final.jpg", ArtifactRole.DELIVERABLE), keep),
        ("b", ArtifactRef("M8_starless.tif", ArtifactRole.QA), qa),
        ("c", ArtifactRef("probe.jpg", ArtifactRole.PROBE), probe),
        ("d", ArtifactRef("linear_master", ArtifactRole.PRODUCTION), scratch),
    ]:
        ws.register(key, ref, p)
    ws.cleanup()
    assert os.path.exists(keep) and os.path.exists(unrelated)
    # Spec rev 4: QA now executes in scratch and is REMOVED by cleanup() like
    # PRODUCTION -- only the publication transaction promotes it to output_dir.
    assert not os.path.exists(qa)
    assert not os.path.exists(probe) and not os.path.exists(scratch)
    assert not os.path.isdir(ws.scratch_dir)


def test_register_rejects_a_second_producer_for_the_same_ref(tmp_path):
    from seestar.core.types import ArtifactRef, ArtifactRole
    from seestar.workspace import Workspace

    ws = Workspace(str(tmp_path))
    ref = ArtifactRef("stretched", ArtifactRole.PRODUCTION)
    path_a = str(tmp_path / "a.fit")
    path_b = str(tmp_path / "b.fit")
    open(path_a, "w").close()
    open(path_b, "w").close()

    ws.register("op_key_a", ref, path_a)
    with pytest.raises(ValueError, match="single-producer invariant"):
        ws.register("op_key_b", ref, path_b)


def test_register_allows_the_same_producer_to_re_register_its_own_ref(tmp_path):
    """Re-registering under the SAME op_key (e.g. re-running an unchanged op
    after a cleanup/retry) is not a violation -- only a DIFFERENT op_key
    claiming the same ref is."""
    from seestar.core.types import ArtifactRef, ArtifactRole
    from seestar.workspace import Workspace

    ws = Workspace(str(tmp_path))
    ref = ArtifactRef("stretched", ArtifactRole.PRODUCTION)
    path = str(tmp_path / "a.fit")
    open(path, "w").close()

    ws.register("op_key_a", ref, path)
    ws.register("op_key_a", ref, path)  # must not raise
    assert ws.lookup("op_key_a", ref) == path


def test_reusing_output_ref_after_upstream_param_change_now_raises(tmp_path, monkeypatch):
    """Task 2 turns the Task-1-documented silent-stale-reuse bug into a hard
    error: attempt 2's StretchRender reusing the SAME output ref as attempt 1,
    with different params, is now rejected by the single-producer invariant
    at the point attempt 2 tries to register `stretched` again."""
    import seestar.execute as execute_module
    from seestar.adapters.base import ExecContext
    from seestar.core.types import ArtifactRef, ArtifactRole, StretchRender
    from seestar.workspace import Workspace

    class _FakeStretchAdapter:
        operations = (StretchRender,)
        name = "fake_stretch"

        def execute(self, op, ctx):
            path = ctx.workspace.path_for(op.output, ".fit")
            with open(path, "w") as f:
                f.write(f"saturation={op.saturation}")
            return {op.output: path}

    monkeypatch.setattr(execute_module.adapters_base, "REGISTRY", {StretchRender: _FakeStretchAdapter()})

    ws = Workspace(str(tmp_path))
    ctx = ExecContext(workspace=ws, tools=None, resolve={})
    linear = ArtifactRef("linear_master", ArtifactRole.PRODUCTION)
    stretched = ArtifactRef("stretched", ArtifactRole.PRODUCTION)
    ws.register("seed", linear, str(tmp_path / "seed.fit"))
    open(tmp_path / "seed.fit", "w").close()

    op1 = StretchRender(
        input=linear,
        output=stretched,
        asinh_d=75,
        blackpoint=0.01,
        mtf_mid=0.08,
        saturation=0.60,
        ght=None,
        black_lift=0.0,
    )
    execute_module.execute_ops([op1], ctx)

    op2 = StretchRender(
        input=linear,
        output=stretched,
        asinh_d=75,
        blackpoint=0.01,
        mtf_mid=0.08,
        saturation=0.40,
        ght=None,
        black_lift=0.0,
    )
    with pytest.raises(ValueError, match="single-producer invariant"):
        execute_module.execute_ops([op2], ctx)


def test_candidate_role_routes_to_scratch_not_output_dir(tmp_path):
    ws = Workspace(str(tmp_path))
    ref = ArtifactRef("T_final_candidate.jpg", ArtifactRole.CANDIDATE)
    path = ws.path_for(ref, "")
    assert path.startswith(ws.scratch_dir)
    assert not path.startswith(ws.output_dir) or ws.scratch_dir.startswith(ws.output_dir)
    # (scratch_dir lives INSIDE output_dir today, per Workspace.__init__ -- the
    # real assertion is that it's under scratch_dir specifically, not a bare
    # output_dir path.)
    assert os.path.dirname(path) == ws.scratch_dir
