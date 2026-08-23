"""CLI wiring for `seestar run --recipe` (replay mode)."""

from __future__ import annotations

import seestar.cli as cli
import seestar.run
from seestar.recipe import RecipeError


def _fake_result(notes=None):
    return {
        "target": "X1",
        "profile": "showcase",
        "status": "pass",  # run_target always carries a status; the CLI exits 1 without a passing one
        "params": {},
        "inspection": {
            "classification": {"category": "nebula"},
            "meta": {"total_integration_min": 8.5, "filter": "LP"},
        },
        "outputs": [],
        "notes": notes or {},
    }


def test_run_passes_recipe_path(tmp_path, monkeypatch, capsys):
    seen = {}

    def fake_run_target(target_dir, out, cfg, **kw):
        seen.update(kw)
        return _fake_result()

    monkeypatch.setattr(seestar.run, "run_target", fake_run_target)
    rc = cli.main(["run", str(tmp_path), "--out", str(tmp_path / "out"), "--recipe", str(tmp_path / "X1_recipe.json")])
    assert rc == 0
    assert seen["recipe_path"] == str(tmp_path / "X1_recipe.json")


def test_run_without_recipe_passes_none(tmp_path, monkeypatch):
    seen = {}

    def fake_run_target(target_dir, out, cfg, **kw):
        seen.update(kw)
        return _fake_result()

    monkeypatch.setattr(seestar.run, "run_target", fake_run_target)
    assert cli.main(["run", str(tmp_path), "--out", str(tmp_path / "out")]) == 0
    assert seen["recipe_path"] is None


def test_recipe_error_prints_and_exits_2(tmp_path, monkeypatch, capsys):
    def fake_run_target(*a, **kw):
        raise RecipeError("Recipe errors:\n  • bad thing")

    monkeypatch.setattr(seestar.run, "run_target", fake_run_target)
    rc = cli.main(["run", str(tmp_path), "--out", str(tmp_path / "out"), "--recipe", "r.json"])
    assert rc == 2
    assert "bad thing" in capsys.readouterr().out


def test_replay_report_is_printed(tmp_path, monkeypatch, capsys):
    notes = {
        "recipe_replay": {
            "pinned": ["denoise_strength"],
            "warnings": ["recipe written by engine 1.0.0; running 2.0.0 — app values re-derived, pins honored"],
            "changes": [
                {"adjustment": "denoise_strength", "prior": 0.5, "now": 0.7, "source": "pinned", "changed": True},
                {"adjustment": "mtf_mid", "prior": 0.081, "now": 0.079, "source": "re-derived", "changed": True},
            ],
        }
    }
    monkeypatch.setattr(seestar.run, "run_target", lambda *a, **kw: _fake_result(notes))
    cli.main(["run", str(tmp_path), "--out", str(tmp_path / "out"), "--recipe", "r.json"])
    out = capsys.readouterr().out
    assert "denoise_strength" in out and "pinned" in out
    assert "re-derived" in out
    assert "warn" in out.lower()
