"""Replay reproducibility pieces (spec §2 Layer 2): pixel diff metric family."""

from __future__ import annotations

import json
import os

import numpy as np
import tifffile

import seestar.run as run_module
import seestar.validate as V
from seestar.validate import tiff_diff


def _write(path, arr):
    tifffile.imwrite(str(path), arr)
    return str(path)


def test_tiff_diff_identical(tmp_path):
    a = (np.random.default_rng(0).random((8, 8, 3)) * 65535).astype("uint16")
    pa = _write(tmp_path / "a.tif", a)
    pb = _write(tmp_path / "b.tif", a.copy())
    assert tiff_diff(pa, pb) == {"max_abs": 0, "rmse": 0.0}


def test_tiff_diff_one_pixel(tmp_path):
    # The reason RMSE alone is insufficient (spec §2): one bad pixel vanishes in
    # RMSE over the frame but is fully visible in max_abs.
    a = np.zeros((8, 8, 3), dtype="uint16")
    b = a.copy()
    b[4, 4, 1] = 500
    d = tiff_diff(_write(tmp_path / "a.tif", a), _write(tmp_path / "b.tif", b))
    assert d["max_abs"] == 500
    assert 0 < d["rmse"] < 500 / 8  # tiny in aggregate — max_abs catches it


def test_tiff_diff_shape_mismatch(tmp_path):
    a = np.zeros((8, 8, 3), dtype="uint16")
    b = np.zeros((4, 4, 3), dtype="uint16")
    d = tiff_diff(_write(tmp_path / "a.tif", a), _write(tmp_path / "b.tif", b))
    assert d["max_abs"] is None and "error" in d


# --- replay_check --------------------------------------------------------------------


def _fake_run_target_factory(value=0.5, pixel=1000):
    """A fake run_target that writes a recipe + final tif — the boundary here is the
    multi-minute real render; the compared ARTIFACTS are real files."""

    def fake(src, out_dir, cfg, profile="showcase", out_name="final", recipe_path=None, **kw):
        os.makedirs(out_dir, exist_ok=True)
        name = os.path.basename(out_dir).replace("_replay", "")
        doc = {
            "recipe_format": 1,
            "engine_version": "test",
            "generated": "now",
            "target": name,
            "source_fits": "x.fit",
            "context": {},
            "adjustments": [
                {
                    "adjustment": "denoise",
                    "value": value,
                    "your_value": None,
                    "tool": "GraXpert",
                    "note": "",
                    "reason": "",
                    "symptoms_if_wrong": "",
                }
            ],
        }
        with open(os.path.join(out_dir, f"{name}_recipe.json"), "w", encoding="utf-8") as f:
            json.dump(doc, f)
        arr = np.full((8, 8, 3), pixel, dtype="uint16")
        tifffile.imwrite(os.path.join(out_dir, f"{name}_final.tif"), arr)
        return {
            "target": name,
            "published": {"hero_base": f"{name}_final", "sidecars": {}, "certified": True},
        }

    return fake


def _cfg_tol():
    return {"validation": {"replay": {"max_abs_ceiling": 0, "rmse_ceiling": 0.0}}}


def test_replay_check_pass(tmp_path, monkeypatch):
    out = tmp_path / "T1"
    _fake_run_target_factory()(None, str(out), {})  # the "original" render artifacts
    monkeypatch.setattr(run_module, "run_target", _fake_run_target_factory())
    r = V.replay_check("T1", "src", str(out), _cfg_tol(), str(tmp_path))
    assert r["status"] == "replay_pass"
    assert r["param_diffs"] == {}
    assert r["pixels"]["max_abs"] == 0


def test_replay_check_fails_on_param_drift(tmp_path, monkeypatch):
    out = tmp_path / "T1"
    _fake_run_target_factory(value=0.5)(None, str(out), {})
    monkeypatch.setattr(run_module, "run_target", _fake_run_target_factory(value=0.6))
    r = V.replay_check("T1", "src", str(out), _cfg_tol(), str(tmp_path))
    assert r["status"] == "replay_fail"
    assert "denoise" in r["param_diffs"]


def test_replay_check_fails_on_pixel_drift(tmp_path, monkeypatch):
    out = tmp_path / "T1"
    _fake_run_target_factory(pixel=1000)(None, str(out), {})
    monkeypatch.setattr(run_module, "run_target", _fake_run_target_factory(pixel=1001))
    r = V.replay_check("T1", "src", str(out), _cfg_tol(), str(tmp_path))
    assert r["status"] == "replay_fail"
    assert r["pixels"]["max_abs"] == 1


def test_replay_check_skips_without_recipe(tmp_path):
    out = tmp_path / "T1"
    os.makedirs(out)
    r = V.replay_check("T1", "src", str(out), _cfg_tol(), str(tmp_path))
    assert r["status"] == "replay_skipped"


def test_replay_check_hard_fails_when_original_was_uncertified(tmp_path):
    """docs/TENETS.md tenet 7: a gate that silently skips is a defect. If the
    ORIGINAL render was uncertified (only the `_uncertified` run manifest is on
    disk, no `{label}_run.json`), replaying it isn't "no recipe yet" -- it's not a
    certified artifact at all, so this must hard-fail, never `replay_skipped`."""
    out = tmp_path / "M"
    os.makedirs(out)
    (out / "M_uncertified_run.json").write_text("{}")
    # deliberately no M_run.json and no M_recipe.json
    r = V.replay_check("M", "src", str(out), _cfg_tol(), str(tmp_path))
    assert r["status"] == "fail"
    assert "uncertified" in r["reason"]
