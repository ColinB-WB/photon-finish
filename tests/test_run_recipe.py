"""End-to-end integration test: run_target through the recipe system, with the
external tools (Siril/GraXpert/StarNet2/exiftool) and heavy measurement stages
monkeypatched out. Exercises a real recipe emit + a real recipe replay (including
a real pinned value flowing all the way into real params), on the simplest
possible path: a "nebula" category, LP filter, star_forks disabled so no StarNet
stage is reached at all.

Placeholder target name "X1" (never a real astronomical object) per CLAUDE.md's
target-agnostic rule.
"""

from __future__ import annotations

import json
import os

import pytest

import seestar.execute as exec_module
import seestar.orchestrate as orch_module
import seestar.qa as qa_module
import seestar.recipe as R
import seestar.run as run_module
from seestar.io import load_config

FAKE_FIT_NAME = "Stacked_51_X1_10.0s_LP_20260615-221349.fit"


def _make_target_dir(tmp_path) -> str:
    from conftest import write_fake_stack

    target_dir = tmp_path / "X1_import"
    target_dir.mkdir()
    write_fake_stack(target_dir / FAKE_FIT_NAME, obj="X1")
    return str(target_dir)


def _cfg() -> dict:
    cfg = load_config()
    # Keep this hermetic + on the simplest path (F2 spec): no trail removal, no
    # catalog prior network lookup, e-gate already off in config.yaml.
    cfg["engine"]["trail_removal"]["enabled"] = False
    cfg["engine"]["catalog_prior"] = False
    return cfg


class _FakeTools:
    siril = "siril-cli"
    graxpert = "GraXpert"
    starnet2 = "starnet2"
    exiftool = "exiftool"


def _fake_resolve_tools(cfg):
    return _FakeTools()


def _fake_embed(*a, **kw) -> None:
    pass


def _fake_probe_metric(jpg_path, metric, mask_cfg=None, **kw) -> float:
    """The stretch solve runs through the core state machine now; fake the JPG probe
    metric so the search converges deterministically (body target 72, sky low)."""
    return 72.0 if metric == "body_p90" else 10.0


def _apply_mocks(monkeypatch) -> None:
    from conftest import fake_measure_subject_support, install_fake_op_adapters, install_fake_run_siril

    monkeypatch.setattr(orch_module, "resolve_tools", _fake_resolve_tools)
    monkeypatch.setattr(orch_module, "embed", _fake_embed)
    install_fake_op_adapters(monkeypatch)
    install_fake_run_siril(monkeypatch)
    monkeypatch.setattr(exec_module, "_probe_metric", _fake_probe_metric)
    import seestar.measure as measure_module

    monkeypatch.setattr(measure_module, "measure_subject_support", fake_measure_subject_support)
    monkeypatch.setattr(qa_module, "input_star_metrics", lambda *a, **kw: {})


def _force_pass(monkeypatch):
    """Make evaluate_hero classify the candidate PASS. REQUIRED for any certified-path
    test: the hermetic harness's fake renders fail star_count/fwhm/sky_median by
    default, so without this every end-to-end run is processing_failed. Every test in
    this file is a certified-namespace recipe round-trip (recipe emission is
    unconditional; the notes.json/recipe.json paths asserted here are the certified
    names), so `_run` applies this unconditionally."""
    from dataclasses import replace as dc_replace

    from seestar.core.types import HeroOutcome

    real = orch_module.evaluate_hero

    def _passed(*a, **kw):
        hero = real(*a, **kw)
        return dc_replace(
            hero, outcome=HeroOutcome.PASS, processing_failed_gates=(), source_limited_gates=(), reasons=()
        )

    monkeypatch.setattr(orch_module, "evaluate_hero", _passed)


def _run(monkeypatch, target_dir, out_dir, **kw):
    _apply_mocks(monkeypatch)
    _force_pass(monkeypatch)
    cfg = _cfg()
    return run_module.run_target(
        target_dir,
        out_dir,
        cfg,
        profile=kw.pop("profile", "showcase"),
        overrides=kw.pop("overrides", {"star_forks": False}),
        **kw,
    )


IGNORED_KEYS = {"_recipe_provenance", "_pref_provenance"}


def _strip(params: dict) -> dict:
    return {k: v for k, v in params.items() if k not in IGNORED_KEYS}


def test_plain_run_emits_valid_recipe(tmp_path, monkeypatch):
    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")
    result = _run(monkeypatch, target_dir, out_dir)

    recipe_path = os.path.join(out_dir, "X1_recipe.json")
    assert os.path.exists(recipe_path)
    doc = json.loads(open(recipe_path, encoding="utf-8").read())
    assert set(doc) == {
        "recipe_format",
        "engine_version",
        "generated",
        "target",
        "source_fits",
        "context",
        "adjustments",
    }
    assert doc["target"] == "X1"
    assert R.validate(doc) == []
    assert result["params"]["category"] == "nebula"


def test_replay_with_no_pins_reproduces_params(tmp_path, monkeypatch):
    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")
    result1 = _run(monkeypatch, target_dir, out_dir)
    recipe_path = os.path.join(out_dir, "X1_recipe.json")

    # every your_value is still null: mode (b) with no pins IS mode (a)
    doc = json.loads(open(recipe_path, encoding="utf-8").read())
    assert all(e["your_value"] is None for e in doc["adjustments"])

    result2 = _run(monkeypatch, target_dir, out_dir, recipe_path=recipe_path)

    assert _strip(result2["params"]) == _strip(result1["params"])


def test_replay_with_mtf_pin(tmp_path, monkeypatch):
    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")
    _run(monkeypatch, target_dir, out_dir)
    recipe_path = os.path.join(out_dir, "X1_recipe.json")

    doc = json.loads(open(recipe_path, encoding="utf-8").read())
    for e in doc["adjustments"]:
        if e["adjustment"] == "mtf_mid":
            e["your_value"] = 0.05
    with open(recipe_path, "w", encoding="utf-8") as f:
        json.dump(doc, f)

    result = _run(monkeypatch, target_dir, out_dir, recipe_path=recipe_path)

    assert result["notes"]["stretch_solve"] == {"skipped": "mtf_mid pinned via recipe", "mtf_mid": 0.05}
    assert result["params"]["mtf_mid"] == 0.05

    # the pin survives into the recipe re-emitted by the replay run itself
    doc2 = json.loads(open(recipe_path, encoding="utf-8").read())
    by = {e["adjustment"]: e for e in doc2["adjustments"]}
    assert by["mtf_mid"]["your_value"] == 0.05
    assert by["mtf_mid"]["value"] == 0.05


def test_replay_with_blackpoint_pin(tmp_path, monkeypatch):
    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")
    _run(monkeypatch, target_dir, out_dir)
    recipe_path = os.path.join(out_dir, "X1_recipe.json")

    doc = json.loads(open(recipe_path, encoding="utf-8").read())
    for e in doc["adjustments"]:
        if e["adjustment"] == "blackpoint":
            e["your_value"] = 0.011
    with open(recipe_path, "w", encoding="utf-8") as f:
        json.dump(doc, f)

    result = _run(monkeypatch, target_dir, out_dir, recipe_path=recipe_path)

    assert result["params"]["blackpoint"] == 0.011
    assert result["notes"]["blackpoint"]["pinned_via_recipe"] is True

    doc2 = json.loads(open(recipe_path, encoding="utf-8").read())
    by = {e["adjustment"]: e for e in doc2["adjustments"]}
    assert by["blackpoint"]["your_value"] == 0.011
    assert by["blackpoint"]["value"] == 0.011


def test_replay_change_report_all_rederived_zero_changes(tmp_path, monkeypatch):
    """Spec §2 Layer 1: an UNEDITED recipe replays to an empty change report —
    every adjustment re-derived, nothing changed, nothing pinned."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")
    result1 = _run(monkeypatch, target_dir, out_dir)
    recipe_path = os.path.join(out_dir, "X1_recipe.json")

    result2 = _run(monkeypatch, target_dir, out_dir, recipe_path=recipe_path)

    rep = result2["notes"]["recipe_replay"]
    assert rep["changes"], "change report must list every applicable adjustment"
    assert all(c["source"] == "re-derived" for c in rep["changes"])
    assert all(not c["changed"] for c in rep["changes"])
    assert rep.get("warnings", []) == []
    assert _strip(result2["params"]) == _strip(result1["params"])


def test_replay_change_report_persists_to_notes_sidecar(tmp_path, monkeypatch):
    """Owner-reproduced defect (Task 6 corrective): notes["recipe_replay"] used to be
    assembled AFTER the accepted-run transaction had already written and committed
    {target}_notes.json, so the RETURNED result dict carried recipe_replay but the
    persisted, manifest-referenced sidecar on disk did not -- they diverged. Read the
    sidecar off DISK (not the in-memory result) to pin that the committed notes file
    itself carries the change report."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")
    result1 = _run(monkeypatch, target_dir, out_dir)
    recipe_path = os.path.join(out_dir, "X1_recipe.json")

    result2 = _run(monkeypatch, target_dir, out_dir, recipe_path=recipe_path)

    notes_path = os.path.join(out_dir, "X1_notes.json")
    assert os.path.exists(notes_path)
    persisted = json.loads(open(notes_path, encoding="utf-8").read())
    assert "recipe_replay" in persisted["notes"], "persisted notes sidecar is missing recipe_replay"

    rep = persisted["notes"]["recipe_replay"]
    assert rep["changes"], "change report must list every applicable adjustment"
    assert all(c["source"] == "re-derived" for c in rep["changes"])
    assert all(not c["changed"] for c in rep["changes"])
    assert rep.get("warnings", []) == []
    # the persisted sidecar must match the in-memory report the run returned -- no divergence
    assert persisted["notes"]["recipe_replay"] == result2["notes"]["recipe_replay"]
    assert _strip(result2["params"]) == _strip(result1["params"])


def test_persisted_notes_equal_returned_notes_serialized_contract(tmp_path, monkeypatch):
    """docs/CHANGE_SAFETY.md rule 7 ("test persisted state, not only return values")
    and its named invariant test: after a recipe replay, the run's RETURNED
    `result["notes"]` must equal the `notes` object persisted in
    `{target}_notes.json` under the SERIALIZED CONTRACT -- round-tripped through the
    exact same serializer the sidecar write uses
    (`json.dump(..., indent=2, default=str)` in orchestrate.py's accepted-run
    transaction), so `default=str` coercions and wrapper shapes are honored rather
    than compared as live Python objects. This is a stronger sibling of
    test_replay_change_report_persists_to_notes_sidecar above, which only checked
    the `recipe_replay` sub-key; this compares the WHOLE notes object."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")
    _run(monkeypatch, target_dir, out_dir)
    recipe_path = os.path.join(out_dir, "X1_recipe.json")

    result2 = _run(monkeypatch, target_dir, out_dir, recipe_path=recipe_path)

    notes_path = os.path.join(out_dir, "X1_notes.json")
    assert os.path.exists(notes_path)
    persisted = json.loads(open(notes_path, encoding="utf-8").read())
    returned_roundtrip = json.loads(json.dumps(result2["notes"], default=str))
    assert returned_roundtrip == persisted["notes"]


def _force_processing_failed(monkeypatch):
    """Make evaluate_hero classify the candidate PROCESSING_FAILED regardless of
    gates. Replicated from tests/test_source_safety_rerun.py -- repo style is no
    cross-test-file imports."""
    from dataclasses import replace as dc_replace

    from seestar.core.types import HeroOutcome

    real = orch_module.evaluate_hero

    def _failed(*a, **kw):
        hero = real(*a, **kw)
        return dc_replace(
            hero,
            outcome=HeroOutcome.PROCESSING_FAILED,
            processing_failed_gates=("injected",),
            reasons=("injected failure",),
        )

    monkeypatch.setattr(orch_module, "evaluate_hero", _failed)


def test_failed_run_emits_uncertified_recipe_and_preserves_certified(tmp_path, monkeypatch):
    """Task 6 (spec rev 6, red-team finding 21): a failed run must not touch the
    certified recipe -- it emits an uncertified sidecar, seeded with your_value
    pins carried over from the certified recipe on disk."""
    target_dir = _make_target_dir(tmp_path)
    out_dir_path = tmp_path / "out"
    out_dir_path.mkdir()
    out_dir = str(out_dir_path)
    certified_recipe = out_dir_path / "X1_recipe.json"
    certified_recipe.write_text(
        json.dumps({"adjustments": [{"adjustment": "saturation", "value": 0.6, "your_value": 0.8}]})
    )
    before = certified_recipe.read_bytes()

    _apply_mocks(monkeypatch)
    _force_processing_failed(monkeypatch)
    result = run_module.run_target(target_dir, out_dir, _cfg(), overrides={"star_forks": False})

    assert result["status"] == "processing_failed"
    assert certified_recipe.read_bytes() == before  # byte-identical: untouched

    unc = json.loads((out_dir_path / "X1_uncertified_recipe.json").read_text())
    sat = next(e for e in unc["adjustments"] if e["adjustment"] == "saturation")
    assert sat["your_value"] == 0.8  # pins seeded across from the certified recipe


def test_recipe_is_unedited(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"adjustments": [{"adjustment": "a", "your_value": None}]}))
    assert R.recipe_is_unedited(str(p)) is True
    p.write_text(json.dumps({"adjustments": [{"adjustment": "a", "your_value": 1.5}]}))
    assert R.recipe_is_unedited(str(p)) is False
    p.write_text("{corrupt")
    assert R.recipe_is_unedited(str(p)) is False
    assert R.recipe_is_unedited(str(tmp_path / "missing.json")) is False


def test_invalid_recipe_raises_before_any_work(tmp_path, monkeypatch):
    """Coverage-matrix M18: an invalid recipe must refuse before any planning/tool
    work happens (orchestrate.py validates the recipe FIRST, raising RecipeError --
    note this is a raised exception, not a PlanningRefused typed outcome: recipe
    validation runs in the shell before start_planning is ever called)."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")
    bad_recipe_path = str(tmp_path / "bad_recipe.json")
    with open(bad_recipe_path, "w", encoding="utf-8") as f:
        json.dump({"recipe_format": R.RECIPE_FORMAT, "adjustments": "not-a-list"}, f)
    assert R.validate(json.load(open(bad_recipe_path, encoding="utf-8"))) != []

    with pytest.raises(R.RecipeError):
        _run(monkeypatch, target_dir, out_dir, recipe_path=bad_recipe_path)

    # no planning/tool work occurred: no scratch or deliverables in out_dir at all
    assert not os.path.exists(out_dir) or os.listdir(out_dir) == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
