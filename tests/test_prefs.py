"""Tests for seestar/prefs.py — the user preferences layer."""

from __future__ import annotations

import pytest
import yaml

from seestar.prefs import (
    ALL_PRESETS,
    STYLE_DEFAULT,
    STYLE_MAP,
    explain,
    generate_template,
    load_for_run,
    load_preferences,
    merge_preferences,
    prefs_to_overrides,
    resolve_value,
    validate_preferences,
)

# ---------------------------------------------------------------------------
# resolve_value
# ---------------------------------------------------------------------------


def test_resolve_label_returns_preset_value():
    val, label = resolve_value("balanced", "noise_reduction")
    assert val == pytest.approx(0.6)
    assert label == "balanced"


def test_resolve_default_label():
    p = ALL_PRESETS["noise_reduction"]
    val, label = resolve_value(p.default, "noise_reduction")
    assert val == p.presets[p.default]


def test_resolve_float_passthrough():
    val, label = resolve_value(0.4, "noise_reduction")
    assert val == pytest.approx(0.4)
    assert "0.4" in label


def test_resolve_float_provenance_names_closest_label():
    # 0.4 is between light(0.3) and balanced(0.6); closest is balanced
    _, label = resolve_value(0.4, "noise_reduction")
    assert "balanced" in label or "light" in label  # closest is light (|0.4-0.3|=0.1 < |0.4-0.6|=0.2)


def test_resolve_invalid_label_raises():
    with pytest.raises(ValueError, match="aggressive"):
        resolve_value("aggressive", "noise_reduction")


def test_resolve_invalid_label_lists_valid_labels():
    with pytest.raises(ValueError, match="light"):
        resolve_value("wrong", "noise_reduction")


def test_resolve_unknown_param_raises():
    with pytest.raises(ValueError, match="unknown_param"):
        resolve_value("balanced", "unknown_param")


def test_resolve_style_valid():
    _, label = resolve_value("share", "style")
    assert label == "share"


def test_resolve_style_invalid():
    with pytest.raises(ValueError, match="showcase"):  # error lists valid options
        resolve_value("showcase", "style")  # "showcase" is the profile name, not the pref label


# ---------------------------------------------------------------------------
# validate_preferences
# ---------------------------------------------------------------------------


def test_validate_clean_prefs():
    assert validate_preferences({"color": "vivid", "brightness": "bright"}) == []


def test_validate_unknown_key():
    errs = validate_preferences({"unknown_key": "vivid"})
    assert any("unknown_key" in e for e in errs)


def test_validate_bad_value():
    errs = validate_preferences({"noise_reduction": "aggressive"})
    assert any("noise_reduction" in e for e in errs)


def test_validate_category_section():
    errs = validate_preferences({"nebula": {"color": "vivid"}})
    assert errs == []


def test_validate_bad_category_section_value():
    errs = validate_preferences({"nebula": {"color": "wrong"}})
    assert any("nebula.color" in e for e in errs)


# ---------------------------------------------------------------------------
# merge_preferences
# ---------------------------------------------------------------------------


def test_merge_global_bare():
    merged = merge_preferences({"color": "vivid"}, {}, "nebula")
    assert merged["color"] == "vivid"


def test_merge_category_overrides_global():
    merged = merge_preferences({"color": "subtle", "nebula": {"color": "vivid"}}, {}, "nebula")
    assert merged["color"] == "vivid"


def test_merge_category_section_not_applied_for_other_category():
    merged = merge_preferences({"color": "subtle", "nebula": {"color": "vivid"}}, {}, "galaxy")
    assert merged["color"] == "subtle"


def test_merge_target_wins():
    merged = merge_preferences(
        {"color": "subtle", "nebula": {"color": "vivid"}},
        {"color": "muted"},
        "nebula",
    )
    assert merged["color"] == "muted"


def test_merge_category_keys_not_in_output():
    merged = merge_preferences({"nebula": {"color": "vivid"}}, {}, "galaxy")
    assert "nebula" not in merged


# ---------------------------------------------------------------------------
# prefs_to_overrides
# ---------------------------------------------------------------------------

ENGINE_CFG = {
    "saturation": {"nebula": 0.60, "galaxy": 0.60, "starfield": 0.85},
    "stretch": {
        "target": {
            "nebula": {"metric": "body_p90", "level": 72},
            "galaxy": {"metric": "body_p90", "level": 72},
            "starfield": {"metric": "sky_median", "level": 10},
        }
    },
}


def test_color_relative_scaling_nebula():
    overrides, _ = prefs_to_overrides({"color": "subtle"}, "nebula", ENGINE_CFG)
    # subtle multiplier 0.6 × nebula base 0.60 = 0.36
    assert overrides["saturation"] == pytest.approx(0.36, abs=0.01)


def test_color_relative_scaling_starfield():
    overrides, _ = prefs_to_overrides({"color": "vivid"}, "starfield", ENGINE_CFG)
    # vivid multiplier 1.5 × starfield base 0.85 = 1.275
    assert overrides["saturation"] == pytest.approx(1.275, abs=0.01)


def test_brightness_relative_scaling():
    overrides, _ = prefs_to_overrides({"brightness": "bright"}, "nebula", ENGINE_CFG)
    # bright multiplier 1.25 × nebula level 72 = 90.0
    assert overrides["stretch_target_level"] == pytest.approx(90.0, abs=0.5)


def test_noise_reduction_absolute():
    overrides, _ = prefs_to_overrides({"noise_reduction": "light"}, "nebula", ENGINE_CFG)
    assert overrides["denoise_strength"] == pytest.approx(0.3)


def test_stars_scope_ignored_for_starfield():
    overrides, _ = prefs_to_overrides({"stars": "minimal"}, "starfield", ENGINE_CFG)
    assert "star_reduce_k" not in overrides


def test_stars_scope_applied_for_nebula():
    overrides, _ = prefs_to_overrides({"stars": "minimal"}, "nebula", ENGINE_CFG)
    assert overrides["star_reduce_k"] == pytest.approx(0.15)


def test_structure_off_sets_flag():
    overrides, _ = prefs_to_overrides({"structure": "standard"}, "nebula", ENGINE_CFG)
    assert overrides["structure_enabled"] is False


def test_versions_single_sets_star_forks_false():
    overrides, _ = prefs_to_overrides({"versions": "single"}, "nebula", ENGINE_CFG)
    assert overrides["star_forks"] is False


def test_versions_auto_no_override():
    overrides, _ = prefs_to_overrides({"versions": "auto"}, "nebula", ENGINE_CFG)
    assert "star_forks" not in overrides


def test_closeup_off_for_galaxy():
    overrides, _ = prefs_to_overrides({"closeup": "off"}, "galaxy", ENGINE_CFG)
    assert overrides["closeup_enabled"] is False


def test_closeup_scope_ignored_for_nebula():
    overrides, _ = prefs_to_overrides({"closeup": "off"}, "nebula", ENGINE_CFG)
    assert "closeup_enabled" not in overrides


def test_style_maps_to_profile():
    overrides, prov = prefs_to_overrides({"style": "true-to-life"}, "nebula", ENGINE_CFG)
    assert overrides["profile"] == "natural"
    assert "style" in prov


def test_provenance_records_label_and_value():
    _, prov = prefs_to_overrides({"noise_reduction": "light"}, "nebula", ENGINE_CFG)
    assert "noise_reduction" in prov
    assert "light" in prov["noise_reduction"]


def test_float_escape_hatch():
    overrides, prov = prefs_to_overrides({"noise_reduction": 0.4}, "nebula", ENGINE_CFG)
    assert overrides["denoise_strength"] == pytest.approx(0.4)
    assert "0.4" in prov["noise_reduction"]


# ---------------------------------------------------------------------------
# load_preferences
# ---------------------------------------------------------------------------


def test_load_missing_file_returns_empty():
    assert load_preferences("/nonexistent/path/preferences.yaml") == {}


def test_load_reads_yaml(tmp_path):
    f = tmp_path / "preferences.yaml"
    f.write_text("color: vivid\nbrightness: bright\n")
    assert load_preferences(str(f)) == {"color": "vivid", "brightness": "bright"}


# ---------------------------------------------------------------------------
# load_for_run (integration)
# ---------------------------------------------------------------------------


def test_load_for_run_no_files(tmp_path):
    target_dir = tmp_path / "M8"
    target_dir.mkdir()
    overrides, prov = load_for_run(str(target_dir), "nebula", ENGINE_CFG)
    assert overrides == {}
    assert prov == {}


def test_load_for_run_global_file(tmp_path):
    (tmp_path / "preferences.yaml").write_text("color: vivid\n")
    target_dir = tmp_path / "M8"
    target_dir.mkdir()
    overrides, _ = load_for_run(str(target_dir), "nebula", ENGINE_CFG)
    assert "saturation" in overrides


def test_load_for_run_target_file_wins(tmp_path):
    (tmp_path / "preferences.yaml").write_text("color: vivid\n")
    target_dir = tmp_path / "M8"
    target_dir.mkdir()
    (target_dir / "preferences.yaml").write_text("color: muted\n")
    overrides, _ = load_for_run(str(target_dir), "nebula", ENGINE_CFG)
    # muted (0.3) × 0.60 = 0.18; vivid (1.5) × 0.60 = 0.90
    assert overrides["saturation"] < 0.5  # muted won


def test_load_for_run_invalid_raises(tmp_path):
    (tmp_path / "preferences.yaml").write_text("color: wrong_label\n")
    target_dir = tmp_path / "M8"
    target_dir.mkdir()
    with pytest.raises(ValueError, match="wrong_label"):
        load_for_run(str(target_dir), "nebula", ENGINE_CFG)


# ---------------------------------------------------------------------------
# explain + generate_template
# ---------------------------------------------------------------------------


def test_explain_all_contains_all_params():
    out = explain()
    for name in ALL_PRESETS:
        assert name in out
    assert "style" in out


def test_explain_single_param():
    out = explain("noise_reduction")
    assert "balanced" in out
    assert "light" in out
    assert "strong" in out


def test_explain_unknown_param():
    out = explain("nonexistent")
    assert "Unknown" in out


def test_generate_template_is_valid_yaml():
    tmpl = generate_template()
    # strip comment lines; remaining non-empty lines should parse
    non_comment = "\n".join(ln for ln in tmpl.splitlines() if not ln.strip().startswith("#"))
    parsed = yaml.safe_load(non_comment)
    assert isinstance(parsed, dict)


def test_generate_template_contains_all_params():
    tmpl = generate_template()
    for name in ALL_PRESETS:
        assert name in tmpl
    assert "style" in tmpl


def test_presets_default_is_a_valid_label():
    for name, p in ALL_PRESETS.items():
        assert p.default in p.presets, f"{name}: default '{p.default}' not in presets"


def test_style_default_is_valid():
    assert STYLE_DEFAULT in STYLE_MAP
