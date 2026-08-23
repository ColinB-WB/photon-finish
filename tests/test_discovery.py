"""Library ingestion (content-first, spec §4): discover_targets' one-rule discovery
by HEADER CONTENT (any filename), _find_raw reading Seestar stacks straight from a
target folder, and process_library mapping each target to <output_root>/<target>/."""

import os

import seestar.run as R
from seestar.io import load_config

_CFG = load_config()


def _session(fake_stack, folder, frames, target, filt, dt):
    """Create an S30 trio (real fit + jpg + thumbnail); only the .fit should be
    discovered (identified by header content, not the sibling previews)."""
    base = f"Stacked_{frames}_{target}_10.0s_{filt}_{dt}"
    date_obs = f"{dt[0:4]}-{dt[4:6]}-{dt[6:8]}T{dt[9:11]}:{dt[11:13]}:{dt[13:15]}"
    fake_stack(
        os.path.join(folder, base + ".fit"),
        stackcnt=frames,
        filt=filt,
        obj=target,
        date_obs=date_obs,
    )
    open(os.path.join(folder, base + ".jpg"), "w").close()  # device preview, ignored
    open(os.path.join(folder, base + "_thn.jpg"), "w").close()  # device thumbnail, ignored


def test_single_target_folder(tmp_path, fake_stack):
    f = tmp_path / "M5"
    f.mkdir()
    _session(fake_stack, str(f), 75, "M5", "IRCUT", "20260615-010101")
    out = R.discover_targets(str(f))
    assert len(out["targets"]) == 1
    assert out["targets"][0]["target"] == "M5" and out["targets"][0]["filter"] == "IRCUT"
    assert out["targets"][0]["sessions"] == 1
    assert out["skipped"] == []


def test_library_root_discovers_all_sorted(tmp_path, fake_stack):
    for name, tgt, filt in [("M5", "M5", "IRCUT"), ("Lagoon", "M 8", "LP")]:
        d = tmp_path / name
        d.mkdir()
        _session(fake_stack, str(d), 50, tgt, filt, "20260615-020202")
    (tmp_path / "empty").mkdir()  # no fit -> skipped
    (tmp_path / "loose.txt").write_text("not fits")  # non-dir/non-fits -> ignored
    out = R.discover_targets(str(tmp_path))
    assert [t["target"] for t in out["targets"]] == ["M8", "M5"]  # by folder name (Lagoon < M5)
    # target identity comes from the header (OBJECT), not the folder name
    assert out["targets"][0]["folder"].endswith("Lagoon") and out["targets"][0]["target"] == "M8"


def test_multiple_sessions_picks_latest(tmp_path, fake_stack):
    d = tmp_path / "M13"
    d.mkdir()
    _session(fake_stack, str(d), 52, "M13", "IRCUT", "20250101-120000")  # older
    _session(fake_stack, str(d), 68, "M13", "IRCUT", "20260615-220000")  # newer
    out = R.discover_targets(str(d))
    t = out["targets"][0]
    assert t["sessions"] == 2
    assert t["latest"].startswith("Stacked_68_M13") and "20260615" in t["latest"]


def test_find_raw_reads_stacked_from_folder(tmp_path, fake_stack):
    d = tmp_path / "M17"
    d.mkdir()
    _session(fake_stack, str(d), 67, "M17", "LP", "20260615-220101")
    hits = R._find_raw(str(d))
    assert len(hits) == 1 and hits[0].endswith(".fit")  # only the fit, not the jpgs
    import pytest

    with pytest.raises(FileNotFoundError):
        R._find_raw(str(tmp_path / "M17" / "nope"))


def test_process_library_maps_each_target_to_output_subdir(tmp_path, monkeypatch, fake_stack):
    lib = tmp_path / "S30"
    lib.mkdir()
    for name, tgt in [("M5", "M5"), ("M17", "M 17")]:
        d = lib / name
        d.mkdir()
        _session(fake_stack, str(d), 60, tgt, "IRCUT", "20260615-030303")
    out_root = tmp_path / "Processed"
    calls = []
    monkeypatch.setattr(
        R, "run_target", lambda folder, output_dir, cfg, **kw: calls.append((folder, output_dir, kw)) or {}
    )
    R.process_library(str(lib), str(out_root), _CFG, profile="natural", catalog_prior=True)
    # each discovered target -> <output_root>/<target>; S30 import folder passed as input
    got = {os.path.basename(o): (f, kw) for f, o, kw in calls}
    assert set(got) == {"M5", "M17"}
    assert got["M17"][0].endswith(os.path.join("S30", "M17"))  # input is the device folder
    assert got["M5"][1]["profile"] == "natural" and got["M5"][1]["catalog_prior"] is True


def test_process_library_isolates_per_target_failures(tmp_path, monkeypatch, fake_stack):
    """M4: an ordinary exception from one target's run_target must not abort the
    batch -- later targets are still attempted, an earlier target's real planted
    output is untouched, and the failure is reported as a typed TargetFailure
    rather than propagating."""
    lib = tmp_path / "S30"
    lib.mkdir()
    for name in ("A", "B", "C"):
        d = lib / name
        d.mkdir()
        _session(fake_stack, str(d), 50, name, "IRCUT", "20260615-040404")
    out_root = tmp_path / "Processed"

    calls = []

    def fake_run_target(folder, output_dir, cfg, **kw):
        target = os.path.basename(os.path.normpath(folder))
        calls.append(target)
        if target == "B":
            raise RuntimeError("kaboom")
        os.makedirs(output_dir, exist_ok=True)
        with open(os.path.join(output_dir, "marker.txt"), "w") as f:
            f.write(target)
        return {"target": target, "status": "pass"}

    monkeypatch.setattr(R, "run_target", fake_run_target)
    batch = R.process_library(str(lib), str(out_root), _CFG)

    assert calls == ["A", "B", "C"]  # all three attempted, in discovery order
    assert [s["target"] for s in batch.successes] == ["A", "C"]
    assert batch.status_failures == ()
    assert len(batch.failures) == 1
    fail = batch.failures[0]
    assert fail.target == "B"
    assert fail.error_type == "RuntimeError"
    assert "kaboom" in fail.message
    assert fail.folder == str(lib / "B")

    # target A's real planted output survives target B's later failure untouched
    assert (out_root / "A" / "marker.txt").read_text() == "A"


def test_process_library_status_failure_lands_in_status_failures(tmp_path, monkeypatch, fake_stack):
    """Owner-ratified 2026-07-31 layering fix: a target whose run_target call
    completes WITHOUT raising (e.g. an M2 fail-closed QA gate returning a
    `processing_failed` result dict, orchestrate.py:906) is a FAILURE for the
    typed batch report too -- classification must live in BatchRunResult itself,
    not only in a downstream CLI, so any future consumer of process_library can't
    silently repeat the exit-0-on-QA-failure bug. It lands in the structurally
    separate `status_failures` field: NOT in `successes` (that stays pass +
    source_limited only) and NOT in `failures` (that stays exception-only, so the
    two failure kinds remain distinguishable)."""
    lib = tmp_path / "S30"
    lib.mkdir()
    for name in ("A", "B", "C"):
        d = lib / name
        d.mkdir()
        _session(fake_stack, str(d), 50, name, "IRCUT", "20260615-080808")
    out_root = tmp_path / "Processed"

    def fake_run_target(folder, output_dir, cfg, **kw):
        target = os.path.basename(os.path.normpath(folder))
        status = "processing_failed" if target == "B" else "pass"
        return {"target": target, "status": status}

    monkeypatch.setattr(R, "run_target", fake_run_target)
    batch = R.process_library(str(lib), str(out_root), _CFG)

    assert [s["target"] for s in batch.successes] == ["A", "C"]
    assert batch.failures == ()  # no exception was raised -- not an exception-failure
    assert [s["target"] for s in batch.status_failures] == ["B"]
    assert batch.ok is False


def test_process_library_source_limited_only_batch_is_ok(tmp_path, monkeypatch, fake_stack):
    """Reviewer minor: source_limited ships a best-effort deliverable by design
    (CLAUDE.md's per-target QA contract) and counts as SUCCESS -- a batch of only
    source_limited results has no failures of either kind, so `.ok` is True."""
    lib = tmp_path / "S30"
    lib.mkdir()
    for name in ("A", "B"):
        d = lib / name
        d.mkdir()
        _session(fake_stack, str(d), 50, name, "IRCUT", "20260615-090909")
    out_root = tmp_path / "Processed"

    monkeypatch.setattr(
        R,
        "run_target",
        lambda folder, output_dir, cfg, **kw: {
            "target": os.path.basename(os.path.normpath(folder)),
            "status": "source_limited",
        },
    )
    batch = R.process_library(str(lib), str(out_root), _CFG)

    assert [s["target"] for s in batch.successes] == ["A", "B"]
    assert batch.failures == ()
    assert batch.status_failures == ()
    assert batch.ok is True


def test_process_library_reraises_tool_not_found_and_stops(tmp_path, monkeypatch, fake_stack):
    """A ToolNotFoundError is a global prerequisite failure (missing GraXpert/Siril/
    etc.), not a per-target problem -- it must propagate immediately, unretried and
    unrepeated, so later targets are never attempted."""
    from seestar.tools import ToolNotFoundError

    lib = tmp_path / "S30"
    lib.mkdir()
    for name in ("A", "B"):
        d = lib / name
        d.mkdir()
        _session(fake_stack, str(d), 50, name, "IRCUT", "20260615-050505")
    out_root = tmp_path / "Processed"
    calls = []

    def fake_run_target(folder, output_dir, cfg, **kw):
        calls.append(os.path.basename(os.path.normpath(folder)))
        raise ToolNotFoundError("siril not found")

    monkeypatch.setattr(R, "run_target", fake_run_target)
    import pytest

    with pytest.raises(ToolNotFoundError):
        R.process_library(str(lib), str(out_root), _CFG)
    assert calls == ["A"]  # second target never attempted


def test_process_library_does_not_capture_keyboard_interrupt(tmp_path, monkeypatch, fake_stack):
    """Only ordinary Exception is isolated per target; KeyboardInterrupt (and other
    BaseException) must stop the batch immediately."""
    lib = tmp_path / "S30"
    lib.mkdir()
    d = lib / "A"
    d.mkdir()
    _session(fake_stack, str(d), 50, "A", "IRCUT", "20260615-060606")
    out_root = tmp_path / "Processed"

    def fake_run_target(folder, output_dir, cfg, **kw):
        raise KeyboardInterrupt()

    monkeypatch.setattr(R, "run_target", fake_run_target)
    import pytest

    with pytest.raises(KeyboardInterrupt):
        R.process_library(str(lib), str(out_root), _CFG)


def test_cli_process_reports_skipped_fits(tmp_path, monkeypatch, fake_stack, capsys):
    """`seestar process` must surface non-stack FITS the same way `discover` does
    (the silent-gate lesson) -- not just leave them out of the target list."""
    import seestar.cli as cli

    lib = tmp_path / "S30"
    lib.mkdir()
    d = lib / "M5"
    d.mkdir()
    _session(fake_stack, str(d), 60, "M5", "IRCUT", "20260615-030303")
    fake_stack(str(d / "alien.fits"), creator="MaxIm DL")  # non-Seestar -> skipped

    fake_result = {
        "target": "M5",
        "profile": "showcase",
        "status": "pass",
        "inspection": {
            "classification": {"category": "starfield"},
            "meta": {"total_integration_min": 10.0, "filter": "IRCUT"},
        },
        "notes": {},
        "outputs": [],
    }
    monkeypatch.setattr(R, "run_target", lambda folder, output_dir, cfg, **kw: fake_result)
    rc = cli.main(["process", str(lib), "--out", str(tmp_path / "Processed")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "skipped:" in out and "alien.fits" in out and "Seestar" in out
