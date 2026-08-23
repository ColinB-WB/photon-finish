"""The GUI live-progress plan builder (pure; no engine run, no external tools).

Task 23: `plan_stages`/`_STAGE_ORDER`/`_ALWAYS` relocated from `seestar.run` (deleted,
the thin conductor now carries zero decision logic) into `seestar.orchestrate` --
estimating the progress step count is a shell/progress concern, not presentation.
Display copy (label, tool) is `seestar.presentation.STAGE_PRESENTATION`, the ONLY
place those strings live; they must stay identical to the legacy `_STAGE_INFO`
values (the GUI surface is unchanged)."""

from seestar.core.types import StageKey
from seestar.orchestrate import _ALWAYS, _STAGE_ORDER, plan_stages
from seestar.presentation import STAGE_PRESENTATION


def test_always_stages_present_minimal():
    keys = plan_stages(
        {"prior": False, "trail": False, "probe": False, "solve_stretch": False, "forks": False, "closeup": False}
    )
    assert keys == ["analyze", "crop", "bge", "denoise", "color", "stretch", "export"]


def test_starfield_default_flags():
    # defaults: trail on, solve_stretch on; no prior/probe/forks/closeup
    keys = plan_stages({})
    assert keys == ["analyze", "crop", "trail", "bge", "denoise", "color", "solve_stretch", "stretch", "export"]


def test_nebula_showcase_has_star_set_no_closeup():
    keys = plan_stages({"forks": True})
    assert "starless" in keys and "star_reduce" in keys
    assert "closeup" not in keys
    assert keys.index("starless") < keys.index("star_reduce") < keys.index("export")


def test_galaxy_full_chain_order():
    keys = plan_stages({"prior": True, "probe": True, "forks": True, "closeup": True})
    assert keys[0] == "analyze" and keys[1] == "prior"
    assert keys[-1] == "export"
    # canonical execution order is preserved
    assert keys == [k for k in _STAGE_ORDER if k in keys]
    assert keys.index("probe") < keys.index("denoise")
    assert keys.index("closeup") < keys.index("export")


def test_trail_toggle():
    assert "trail" in plan_stages({"trail": True})
    assert "trail" not in plan_stages({"trail": False})


def test_step_count_is_plan_length():
    keys = plan_stages({"forks": True})
    # a GUI "Step i/N" uses index+1 and len -> both well-defined for every planned key
    for i, _k in enumerate(keys, 1):
        assert 1 <= i <= len(keys)


def test_every_ordered_stage_has_label_and_tool():
    assert set(_ALWAYS).issubset(set(_STAGE_ORDER))
    for k in _STAGE_ORDER:
        info = STAGE_PRESENTATION.get(StageKey(k))
        assert info is not None, f"{k} missing a label"
        label, tool = info
        assert label and isinstance(tool, str)  # tool may be "" (built-in)


def test_job_snapshots_stream_stage_progress():
    """The registry's on_stage callback surfaces stage label + tool + step count in
    status_text (was: gui.render generator streaming). Engine faked -> no tools."""
    import threading

    from seestar.gui.jobs import EngineHooks, JobRegistry, JobRequest

    seen = threading.Event()
    proceed = threading.Event()
    statuses = []

    def run(target, request, cb):
        for key, label, tool, i, n in (
            ("analyze", "Analyzing image", "", 1, 9),
            ("bge", "Removing light-pollution gradient", "GraXpert", 4, 9),
            ("starless", "Removing stars", "StarNet2", 8, 9),
        ):
            cb(key, label, tool, i, n)
            seen.set()
            assert proceed.wait(timeout=10)
            proceed.clear()
        return {
            "target": target["target"],
            "ok": True,
            "images": [],
            "qa": None,
            "replay": None,
            "warnings": [],
            "error": None,
        }

    reg = JobRegistry(
        EngineHooks(
            discover=lambda d: {"targets": [{"target": "M8", "folder": "/lib/M8"}], "skipped": []},
            validate=lambda r: [],
            process_target=run,
        )
    )
    snap = reg.start(JobRequest("/lib", "M8", "showcase", "/out"))
    for _ in range(3):
        assert seen.wait(timeout=10)
        seen.clear()
        statuses.append(reg.snapshot(snap["id"])["status_text"])
        proceed.set()
    reg.join(timeout=10)
    blob = "\n".join(statuses)
    assert "Analyzing image" in blob
    assert "Removing light-pollution gradient (GraXpert)" in blob
    assert "Step 4 of 9" in blob
    assert "Removing stars (StarNet2)" in blob
    assert reg.snapshot(snap["id"])["summary"].startswith("✅ Finished: M8")
