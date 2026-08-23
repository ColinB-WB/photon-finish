"""Tests for seestar/recipe.py — the per-image adjustment recipe."""

from __future__ import annotations

import json

import pytest

from seestar.core.types import Category, Decisions, GhsParams, SNRClass
from seestar.io import engine_defaults_from_cfg
from seestar.recipe import (
    ADJUSTMENTS,
    RECIPE_FORMAT,
    RecipeError,
    build_doc,
    change_report,
    context_warnings,
    emit,
    load,
    pins,
    solve_mode,
    validate,
)

# ---------------------------------------------------------------------------
# Registry integrity
# ---------------------------------------------------------------------------

EXPECTED_NAMES = [
    "trail_removal",
    "crop_border_frac",
    "bge_smoothing",
    "denoise_strength",
    "color_calibration",
    "saturation",
    "contrast_shape",
    "brightness_target",
    "structure_boost",
    "star_prominence",
    "blackpoint",
    "mtf_mid",
]


def test_registry_names_and_order():
    assert list(ADJUSTMENTS) == EXPECTED_NAMES


def test_every_entry_is_fully_documented():
    for name, a in ADJUSTMENTS.items():
        assert a.note, f"{name}: empty note"
        assert a.direction, f"{name}: empty direction"
        assert a.symptoms, f"{name}: empty symptoms"
        assert a.kind in ("tunable", "solved"), name
        assert a.vtype in ("float", "bool"), name


def test_float_entries_have_ranges():
    for name, a in ADJUSTMENTS.items():
        if a.vtype == "float":
            assert a.lo is not None and a.hi is not None and a.lo < a.hi, name
        else:
            assert a.lo is None and a.hi is None, name


def test_solved_entries_are_blackpoint_and_mtf():
    solved = [n for n, a in ADJUSTMENTS.items() if a.kind == "solved"]
    assert solved == ["blackpoint", "mtf_mid"]


def test_params_keys_are_real():
    # every mapped params key must be one the pipeline actually consumes
    known = {
        "trail_removal_enabled",
        "crop_border_frac",
        "bge_smoothing",
        "denoise_strength",
        "pcc",
        "saturation",
        "asinh_d",
        "stretch_target_level",
        "structure_enabled",
        "star_reduce_k",
        "blackpoint",
        "mtf_mid",
    }
    for name, a in ADJUSTMENTS.items():
        assert a.param in known, f"{name} maps to unknown params key {a.param}"


def test_format_constant():
    assert RECIPE_FORMAT == 1
    assert issubclass(RecipeError, ValueError)


# ---------------------------------------------------------------------------
# load + validate
# ---------------------------------------------------------------------------


def _minimal_doc(**over):
    doc = {"recipe_format": RECIPE_FORMAT, "engine_version": "0.0.0", "adjustments": []}
    doc.update(over)
    return doc


def test_load_missing_file_raises_recipe_error(tmp_path):
    with pytest.raises(RecipeError, match="not found"):
        load(str(tmp_path / "nope.json"))


def test_load_bad_json_raises_with_position(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("{ oops", encoding="utf-8")
    with pytest.raises(RecipeError, match="line"):
        load(str(f))


def test_load_valid_json(tmp_path):
    f = tmp_path / "ok.json"
    f.write_text(json.dumps(_minimal_doc()), encoding="utf-8")
    assert load(str(f))["recipe_format"] == RECIPE_FORMAT


def test_load_directory_raises_recipe_error_not_oserror(tmp_path):
    # passing a directory as the recipe path must not leak a raw OSError/IsADirectoryError
    with pytest.raises(RecipeError):
        load(str(tmp_path))


def test_load_json_array_raises_recipe_error_mentions_json_object(tmp_path):
    f = tmp_path / "array.json"
    f.write_text(json.dumps([1, 2]), encoding="utf-8")
    with pytest.raises(RecipeError, match="JSON object"):
        load(str(f))


def test_load_bare_json_string_raises_recipe_error(tmp_path):
    f = tmp_path / "string.json"
    f.write_text(json.dumps("hello"), encoding="utf-8")
    with pytest.raises(RecipeError, match="JSON object"):
        load(str(f))


def test_validate_clean_doc():
    assert validate(_minimal_doc()) == []


def test_validate_newer_format_is_error():
    errs = validate(_minimal_doc(recipe_format=RECIPE_FORMAT + 1))
    assert any("newer" in e for e in errs)


def test_validate_missing_format_is_error():
    doc = _minimal_doc()
    del doc["recipe_format"]
    assert any("recipe_format" in e for e in validate(doc))


def test_validate_missing_adjustments_is_error():
    doc = _minimal_doc()
    del doc["adjustments"]
    errs = validate(doc)
    assert any("Missing 'adjustments' list." in e for e in errs)


def test_validate_adjustments_not_a_list_is_distinct_error():
    doc = _minimal_doc(adjustments={})
    errs = validate(doc)
    assert any("'adjustments' must be a list." in e for e in errs)
    assert not any("Missing 'adjustments' list." in e for e in errs)


def test_validate_recipe_format_true_is_error_like_non_int():
    doc = _minimal_doc(recipe_format=True)
    errs = validate(doc)
    assert any("Missing or non-integer" in e for e in errs)


def test_validate_recipe_format_false_is_error_like_non_int():
    doc = _minimal_doc(recipe_format=False)
    errs = validate(doc)
    assert any("Missing or non-integer" in e for e in errs)


def test_validate_unknown_adjustment_lists_valid_names():
    doc = _minimal_doc(adjustments=[{"adjustment": "sharpness", "your_value": 1}])
    errs = validate(doc)
    assert any("sharpness" in e and "denoise_strength" in e for e in errs)


def test_validate_duplicate_adjustment():
    e = {"adjustment": "saturation", "your_value": None}
    errs = validate(_minimal_doc(adjustments=[e, dict(e)]))
    assert any("duplicate" in e_.lower() for e_ in errs)


def test_validate_null_your_value_is_fine():
    doc = _minimal_doc(adjustments=[{"adjustment": "saturation", "your_value": None}])
    assert validate(doc) == []


def test_validate_out_of_range_states_range_and_direction():
    doc = _minimal_doc(adjustments=[{"adjustment": "saturation", "your_value": 5.0}])
    errs = validate(doc)
    assert any("0.0" in e and "2.0" in e and "more colorful" in e for e in errs)


def test_validate_mtf_exclusive_bounds():
    doc = _minimal_doc(adjustments=[{"adjustment": "mtf_mid", "your_value": 0.0}])
    assert validate(doc)  # 0.0 is out of the exclusive range
    doc = _minimal_doc(adjustments=[{"adjustment": "mtf_mid", "your_value": 0.08}])
    assert validate(doc) == []


def test_validate_bool_type_enforced():
    doc = _minimal_doc(adjustments=[{"adjustment": "color_calibration", "your_value": 0.5}])
    assert any("true or false" in e for e in validate(doc))


def test_validate_number_type_enforced():
    doc = _minimal_doc(adjustments=[{"adjustment": "saturation", "your_value": True}])
    assert any("number" in e for e in validate(doc))


def test_validate_collects_all_errors():
    doc = _minimal_doc(
        adjustments=[
            {"adjustment": "sharpness", "your_value": 1},
            {"adjustment": "saturation", "your_value": 5.0},
        ]
    )
    assert len(validate(doc)) == 2


# ---------------------------------------------------------------------------
# pins + solve_mode
# ---------------------------------------------------------------------------


def _doc_with(pins_dict):
    return _minimal_doc(adjustments=[{"adjustment": k, "your_value": v} for k, v in pins_dict.items()])


def test_all_null_recipe_is_mode_a():
    doc = _minimal_doc(adjustments=[{"adjustment": n, "your_value": None} for n in ADJUSTMENTS])
    assert pins(doc) == ({}, set(), {})


def test_pin_maps_to_params_key():
    overrides, pinned, prov = pins(_doc_with({"denoise_strength": 0.7}))
    assert overrides == {"denoise_strength": 0.7}
    assert pinned == {"denoise_strength"}
    assert prov == {"denoise_strength": 0.7}


def test_pin_bool_color_calibration():
    overrides, _, _ = pins(_doc_with({"color_calibration": False}))
    assert overrides == {"pcc": False}


def test_pin_structure_boost():
    overrides, _, _ = pins(_doc_with({"structure_boost": False}))
    assert overrides == {"structure_enabled": False}


def test_pin_brightness_target_neutralizes_scale():
    # the pinned value is the solver's FINAL target: profile/noisy scaling must not
    # re-multiply it, or the user would not get the number they wrote
    overrides, _, _ = pins(_doc_with({"brightness_target": 80}))
    assert overrides["stretch_target_level"] == 80.0
    assert overrides["stretch_target_scale"] == 1.0


def test_pin_trail_removal():
    overrides, _, _ = pins(_doc_with({"trail_removal": False}))
    assert overrides == {"trail_removal_enabled": False}


def test_pin_solved_values():
    overrides, pinned, _ = pins(_doc_with({"blackpoint": 0.012, "mtf_mid": 0.08}))
    assert overrides == {"blackpoint": 0.012, "mtf_mid": 0.08}
    assert pinned == {"blackpoint", "mtf_mid"}


def test_solve_mode_normal():
    assert solve_mode(set()) == "normal"
    assert solve_mode({"saturation"}) == "normal"


def test_solve_mode_bp_pinned():
    assert solve_mode({"blackpoint"}) == "mtf_only"


def test_solve_mode_mtf_pinned_skips():
    assert solve_mode({"mtf_mid"}) == "skip"
    assert solve_mode({"mtf_mid", "blackpoint"}) == "skip"


# ---------------------------------------------------------------------------
# build_doc + emit
# ---------------------------------------------------------------------------

ENGINE_CFG = {
    "trail_removal": {"enabled": True},
    "star_reduce_k": 0.45,
    "stretch": {
        "target": {"nebula": {"metric": "body_p90", "level": 72}, "starfield": {"metric": "sky_median", "level": 10}}
    },
}
ENGINE = engine_defaults_from_cfg(ENGINE_CFG)

_DEFAULT_GHT = GhsParams(d=1.5, b=5.0, sp=0.2, lp=0.05, hp=0.7)


def _decisions(**over) -> Decisions:
    base = dict(
        category=Category.NEBULA,
        snr_class=SNRClass.CLEAN,
        profile="showcase",
        crop_border_frac=0.015,
        bge_smoothing=0.5,
        bge_correction="Subtraction",
        denoise_strength=0.5,
        pcc=False,
        asinh_d=75,
        blackpoint=0.0145,
        blackpoint_scale=1.0,
        mtf_mid=0.081,
        stretch_target_scale=1.0,
        saturation=0.6,
        star_forks=True,
        ghs_allowed=True,
        ght=_DEFAULT_GHT,
        sky_solve=True,
        annotate=False,
        black_lift=0.0,
        sharpening_capped=None,
        stretch_target_level=None,
        structure_enabled=True,
        closeup_enabled=True,
        trail_removal_enabled=True,
        star_reduce_k=0.45,
    )
    base.update(over)
    return Decisions(**base)


def _notes():
    return {
        "blackpoint": {"factor": 0.987, "linear_background_median": 0.0147},
        "stretch_solve": {"metric": "body_p90", "target": 72.0, "achieved": 71.8, "solved_mtf": 0.081},
    }


def test_build_doc_header_and_context():
    doc = build_doc("X1", "Stacked_51_X1_10.0s_LP_20260615-221349.fit", "LP", _decisions(), _notes(), ENGINE)
    assert doc["recipe_format"] == RECIPE_FORMAT
    assert doc["engine_version"]
    assert doc["target"] == "X1"
    assert doc["source_fits"].startswith("Stacked_")
    assert doc["context"] == {"category": "nebula", "snr_class": "clean", "filter": "LP", "profile": "showcase"}


def test_build_doc_entry_shape():
    doc = build_doc("X1", "f.fit", "LP", _decisions(), _notes(), ENGINE)
    e = next(x for x in doc["adjustments"] if x["adjustment"] == "denoise_strength")
    assert e["value"] == 0.5
    assert e["your_value"] is None
    assert e["note"] and e["reason"] and e["symptoms_if_wrong"]


def test_build_doc_solved_values_from_run():
    doc = build_doc("X1", "f.fit", "LP", _decisions(), _notes(), ENGINE)
    by = {x["adjustment"]: x for x in doc["adjustments"]}
    assert by["blackpoint"]["value"] == 0.0145
    assert by["mtf_mid"]["value"] == 0.081
    assert "re-computes" in by["mtf_mid"]["note"]  # solved-kind suffix
    assert "solved" in by["mtf_mid"]["reason"]


def test_build_doc_brightness_from_solve_notes():
    doc = build_doc("X1", "f.fit", "LP", _decisions(), _notes(), ENGINE)
    by = {x["adjustment"]: x for x in doc["adjustments"]}
    assert by["brightness_target"]["value"] == 72.0


def test_build_doc_applicability_star_prominence():
    doc = build_doc(
        "X1", "f.fit", "IRCUT", _decisions(category=Category.STARFIELD, star_forks=False, ght=None), _notes(), ENGINE
    )
    names = [x["adjustment"] for x in doc["adjustments"]]
    assert "star_prominence" not in names


def test_build_doc_applicability_structure():
    doc = build_doc("X1", "f.fit", "LP", _decisions(ghs_allowed=False, ght=None), _notes(), ENGINE)
    names = [x["adjustment"] for x in doc["adjustments"]]
    assert "structure_boost" not in names


def test_build_doc_preserves_prior_your_value():
    prior = _minimal_doc(adjustments=[{"adjustment": "saturation", "your_value": 0.8}])
    doc = build_doc("X1", "f.fit", "LP", _decisions(), _notes(), ENGINE, prior=prior)
    by = {x["adjustment"]: x for x in doc["adjustments"]}
    assert by["saturation"]["your_value"] == 0.8
    assert by["denoise_strength"]["your_value"] is None


def test_build_doc_pinned_reason():
    doc = build_doc("X1", "f.fit", "LP", _decisions(), _notes(), ENGINE, pinned={"saturation"})
    by = {x["adjustment"]: x for x in doc["adjustments"]}
    assert "pinned" in by["saturation"]["reason"]


def test_emit_discards_corrupt_prior_on_disk_gracefully(tmp_path):
    path = tmp_path / "X1_recipe.json"
    path.write_text("not { valid json at all !!!", encoding="utf-8")
    # no explicit prior passed -> emit() must best-effort read the on-disk file itself;
    # a corrupt prior must be discarded, not raised, and every your_value comes back null
    p = emit(str(tmp_path), "X1", "f.fit", "LP", _decisions(), _notes(), ENGINE)
    doc = json.loads(open(p, encoding="utf-8").read())
    assert all(e["your_value"] is None for e in doc["adjustments"])


def test_emit_writes_file_and_preserves_edits_on_rewrite(tmp_path):
    p = emit(str(tmp_path), "X1", "f.fit", "LP", _decisions(), _notes(), ENGINE)
    assert p.endswith("X1_recipe.json")
    doc = json.loads(open(p, encoding="utf-8").read())
    # user edits the file on disk
    for e in doc["adjustments"]:
        if e["adjustment"] == "denoise_strength":
            e["your_value"] = 0.7
    with open(p, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    # engine re-render WITHOUT --recipe must not destroy the edit
    emit(str(tmp_path), "X1", "f.fit", "LP", _decisions(), _notes(), ENGINE)
    doc2 = json.loads(open(p, encoding="utf-8").read())
    by = {x["adjustment"]: x for x in doc2["adjustments"]}
    assert by["denoise_strength"]["your_value"] == 0.7


# ---------------------------------------------------------------------------
# change_report + context_warnings
# ---------------------------------------------------------------------------


def test_context_warnings_engine_version_mismatch():
    import seestar

    doc = _minimal_doc(engine_version="0.0.1", context={})
    w = context_warnings(doc, "nebula", "showcase", "LP")
    assert any("0.0.1" in x and seestar.__version__ in x for x in w)


def test_context_warnings_drift():
    import seestar

    doc = _minimal_doc(
        engine_version=seestar.__version__,
        context={"category": "galaxy", "profile": "showcase", "filter": "LP"},
    )
    w = context_warnings(doc, "nebula", "showcase", "LP")
    assert any("category" in x and "galaxy" in x for x in w)


def test_context_warnings_clean():
    import seestar

    doc = _minimal_doc(
        engine_version=seestar.__version__,
        context={"category": "nebula", "profile": "showcase", "filter": "LP"},
    )
    assert context_warnings(doc, "nebula", "showcase", "LP") == []


def test_change_report_sources_and_changed():
    prior = _minimal_doc(
        adjustments=[
            {"adjustment": "denoise_strength", "value": 0.5, "your_value": 0.7},
            {"adjustment": "mtf_mid", "value": 0.081, "your_value": None},
            {"adjustment": "saturation", "value": 0.6, "your_value": None},
        ]
    )
    decisions = _decisions(denoise_strength=0.7, mtf_mid=0.079)
    rep = change_report(prior, decisions, _notes(), ENGINE, pinned={"denoise_strength"}, warnings=[])
    by = {c["adjustment"]: c for c in rep["changes"]}
    assert by["denoise_strength"] == {
        "adjustment": "denoise_strength",
        "prior": 0.5,
        "now": 0.7,
        "source": "pinned",
        "changed": True,
    }
    assert by["mtf_mid"]["source"] == "re-derived"
    assert by["mtf_mid"]["changed"] is True
    assert by["saturation"]["changed"] is False
    assert rep["pinned"] == ["denoise_strength"]


def test_change_report_flags_inapplicable_pin():
    # pin star_prominence, but replay produced no star forks -> explicit warning
    decisions = _decisions(star_forks=False)
    rep = change_report(_minimal_doc(), decisions, _notes(), ENGINE, pinned={"star_prominence"}, warnings=[])
    assert any("star_prominence" in w for w in rep["warnings"])


# ---------------------------------------------------------------------------
# trail_removal reason reflects the detection OUTCOME (2026-07 external review:
# value=true means the stage is enabled, not that a trail was found)
# ---------------------------------------------------------------------------


def test_trail_reason_no_trail_found():
    notes = {**_notes(), "trail_removal": {"present": False}}
    doc = build_doc("X1", "f.fit", "LP", _decisions(), notes, ENGINE)
    by = {x["adjustment"]: x for x in doc["adjustments"]}
    assert "no trail" in by["trail_removal"]["reason"]


def test_trail_reason_trail_removed():
    notes = {**_notes(), "trail_removal": {"present": True}}
    doc = build_doc("X1", "f.fit", "LP", _decisions(), notes, ENGINE)
    by = {x["adjustment"]: x for x in doc["adjustments"]}
    assert "removed" in by["trail_removal"]["reason"]


def test_trail_reason_detection_did_not_run():
    doc = build_doc("X1", "f.fit", "LP", _decisions(), _notes(), ENGINE)
    by = {x["adjustment"]: x for x in doc["adjustments"]}
    assert "did not run" in by["trail_removal"]["reason"]


def test_denoise_copy_does_not_contradict_galaxy_default():
    # galaxy default is 0.65; the note must not read as a hard 0.6 cap for all
    # categories (the ~0.6 cap is the dense-core/starfield lesson)
    a = ADJUSTMENTS["denoise_strength"]
    assert "0.65" in a.note
    assert "up to about 0.6;" not in a.note


# ---------------------------------------------------------------------------
# tool field: each adjustment names the tool that applies it (so a user can
# open that tool and experiment) — app-owned decisions must say so, not point
# the user at Siril for a value the app solves
# ---------------------------------------------------------------------------


def test_every_adjustment_names_its_tool():
    for name, a in ADJUSTMENTS.items():
        assert a.tool, f"{name}: empty tool"
    assert "GraXpert" in ADJUSTMENTS["bge_smoothing"].tool
    assert "GraXpert" in ADJUSTMENTS["denoise_strength"].tool
    assert "Siril" in ADJUSTMENTS["saturation"].tool
    assert "StarNet2" in ADJUSTMENTS["star_prominence"].tool
    for app_owned in ("blackpoint", "mtf_mid", "brightness_target", "trail_removal"):
        assert "this app" in ADJUSTMENTS[app_owned].tool


def test_entries_carry_tool_field():
    doc = build_doc("X1", "f.fit", "LP", _decisions(), _notes(), ENGINE)
    assert all(e["tool"] for e in doc["adjustments"])


def test_emit_writes_readable_unicode(tmp_path):
    # novices read this file in a text editor: dashes must appear as real
    # characters, never as backslash-u escape codes
    emit(str(tmp_path), "X1", "f.fit", "LP", _decisions(), _notes(), ENGINE)
    text = open(str(tmp_path / "X1_recipe.json"), encoding="utf-8").read()
    backslash = chr(92)
    assert backslash + "u2013" not in text and backslash + "u2014" not in text
    assert chr(0x2013) in text  # the en dash itself is present (0.0-1.0 ranges)
