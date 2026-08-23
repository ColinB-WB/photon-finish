# tests/test_config_overlay.py
"""load_config deep-merges a sibling config.local.yaml overlay, and the committed
config.yaml carries no machine-specific personal paths (they live in the git-ignored overlay)."""

from __future__ import annotations

import textwrap

import pytest

from packaging.scan_personal_info import load_tokens
from seestar import io


def test_deep_merge_overlays_nested_leaves():
    base = {"validation": {"targets": {}, "tolerances": {"a": 1}}, "engine": {"x": 1}}
    overlay = {"validation": {"targets": {"M8": "/p/M8"}}}
    merged = io._deep_merge(base, overlay)
    assert merged["validation"]["targets"] == {"M8": "/p/M8"}
    assert merged["validation"]["tolerances"] == {"a": 1}  # untouched
    assert merged["engine"] == {"x": 1}


def test_load_config_merges_local_overlay(tmp_path):
    (tmp_path / "config.yaml").write_text(
        textwrap.dedent("""
        validation:
          targets: {}
          targets_darwin: {}
        engine:
          keep: 1
    """)
    )
    (tmp_path / "config.local.yaml").write_text(
        textwrap.dedent("""
        validation:
          targets_darwin:
            M8: "/some/local/M8"
    """)
    )
    cfg = io.load_config(tmp_path / "config.yaml")
    assert cfg["validation"]["targets_darwin"]["M8"] == "/some/local/M8"
    assert cfg["engine"]["keep"] == 1


def test_load_config_without_overlay_is_just_the_base(tmp_path):
    (tmp_path / "config.yaml").write_text("engine: {a: 1}\n")
    cfg = io.load_config(tmp_path / "config.yaml")
    assert cfg == {"engine": {"a": 1}}


def test_committed_config_yaml_has_no_personal_paths():
    tokens = load_tokens()
    if not tokens:
        pytest.skip("no .personal-tokens configured")
    with open("config.yaml", encoding="utf-8") as f:
        text = f.read()
    for tok in tokens:
        assert tok not in text, f"config.yaml still contains {tok!r}"
