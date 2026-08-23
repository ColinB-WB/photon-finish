"""UserComment provenance: recipe pins are recorded like prefs provenance."""

from __future__ import annotations

import seestar.adapters.exiftool as ET
import seestar.metadata as M


def _fixtures():
    inspection = {
        "meta": {
            "target": "X1",
            "frames": 51,
            "sub_exposure_s": 10.0,
            "total_integration_min": 8.5,
            "filter": "LP",
            "datetime": "2026-06-15-221349",
        },
        "header": {"has_wcs": False},
        "classification": {"category": "nebula"},
    }
    params = {
        "bge_smoothing": 0.5,
        "bge_correction": "Subtraction",
        "denoise_strength": 0.5,
        "asinh_d": 75,
        "blackpoint": 0.0145,
        "mtf_mid": 0.08,
        "saturation": 0.6,
        "pcc": False,
        "profile": "showcase",
        "_recipe_provenance": {"denoise_strength": 0.7},
    }
    cfg = {
        "defaults": {"telescope": "scope", "artist": ""},
        "graxpert": {"bge_model": "1.0.1", "denoise_model": "3.0.2"},
    }
    return inspection, params, cfg


def test_usercomment_records_recipe_pins(monkeypatch, tmp_path):
    captured = {}

    def fake_run(args, **kw):
        captured["args"] = args

    monkeypatch.setattr(ET, "run_external", fake_run)
    inspection, params, cfg = _fixtures()

    class FakeTools:
        exiftool = "exiftool"

    M.embed(str(tmp_path / "x.jpg"), inspection, params, "final", cfg, FakeTools())
    uc = next(a for a in captured["args"] if a.startswith("-EXIF:UserComment="))
    assert "recipe-pins=denoise_strength:0.7" in uc


def test_usercomment_records_both_prefs_and_recipe_pins(monkeypatch, tmp_path):
    captured = {}

    def fake_run(args, **kw):
        captured["args"] = args

    monkeypatch.setattr(ET, "run_external", fake_run)
    inspection, params, cfg = _fixtures()
    params["_pref_provenance"] = {"brightness": "bright (1.1)"}

    class FakeTools:
        exiftool = "exiftool"

    M.embed(str(tmp_path / "x.jpg"), inspection, params, "final", cfg, FakeTools())
    uc = next(a for a in captured["args"] if a.startswith("-EXIF:UserComment="))
    assert "; prefs=" in uc
    assert "; recipe-pins=" in uc
    assert "brightness:bright (1.1)" in uc
    assert "recipe-pins=denoise_strength:0.7" in uc


def test_usercomment_without_pins_has_no_recipe_marker(monkeypatch, tmp_path):
    captured = {}

    def fake_run(args, **kw):
        captured["args"] = args

    monkeypatch.setattr(ET, "run_external", fake_run)
    inspection, params, cfg = _fixtures()
    del params["_recipe_provenance"]

    class FakeTools:
        exiftool = "exiftool"

    M.embed(str(tmp_path / "x.jpg"), inspection, params, "final", cfg, FakeTools())
    uc = next(a for a in captured["args"] if a.startswith("-EXIF:UserComment="))
    assert "recipe-pins" not in uc


def _capture_card(certification: str):
    """Build the MetadataCard directly for the given certification value, mirroring
    the fixture pattern above (inspection/params/cfg from `_fixtures()`)."""
    inspection, params, cfg = _fixtures()
    return M._build_card(inspection, params, "final", cfg, certification)


def test_embed_certification_lands_in_usercomment_only(monkeypatch, tmp_path):
    card_cert = _capture_card(certification="certified")
    card_unc = _capture_card(certification="uncertified")
    assert "certification=uncertified" in card_unc.user_comment
    assert "certification" not in card_cert.user_comment
    # no other field differs (MetadataCard is the frozen dataclass at core/types.py:621):
    import dataclasses

    a = dataclasses.asdict(card_unc)
    b = dataclasses.asdict(card_cert)
    a.pop("user_comment")
    b.pop("user_comment")
    assert a == b
