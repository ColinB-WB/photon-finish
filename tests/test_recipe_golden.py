"""Golden recipe fixture (spec §6): locks recipe STRUCTURE and vocabulary — never
the engine's chosen numbers, which legitimately change when laws are re-locked."""

from __future__ import annotations

import json
import os

import seestar.recipe as R

GOLDEN = os.path.join(os.path.dirname(__file__), "fixtures", "golden_M81_recipe.json")

ENTRY_FIELDS = {"adjustment", "value", "your_value", "tool", "note", "reason", "symptoms_if_wrong"}
TOP_KEYS = {"recipe_format", "engine_version", "generated", "target", "source_fits", "context", "adjustments"}


def _doc():
    with open(GOLDEN, encoding="utf-8") as f:
        return json.load(f)


def test_golden_top_level_shape():
    doc = _doc()
    assert set(doc) == TOP_KEYS
    assert doc["recipe_format"] == R.RECIPE_FORMAT


def test_golden_entries_complete():
    for e in _doc()["adjustments"]:
        assert set(e) == ENTRY_FIELDS, f"schema drift in '{e.get('adjustment')}'"
        assert e["your_value"] is None  # emitted fresh, no pins
        assert e["tool"], "every adjustment names the tool that applies it"
        assert e["note"] and e["reason"] and e["symptoms_if_wrong"]


def test_golden_validates_clean():
    assert R.validate(_doc()) == []


def test_golden_adjustment_names_match_registry():
    names = {e["adjustment"] for e in _doc()["adjustments"]}
    assert names == set(R.ADJUSTMENTS), (
        f"golden out of sync with registry: unknown={names - set(R.ADJUSTMENTS)} missing={set(R.ADJUSTMENTS) - names}"
    )
