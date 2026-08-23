"""The frozen-app selftest hooks, exercised unfrozen: the frozen run in the build
battery proves bundling; these prove the logic. fastapi/uvicorn required."""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

import packaging.app_main as app_main  # noqa: E402


def test_gui_static_selftest_passes_from_source_tree():
    assert app_main._selftest_gui_static() == 0


def test_gui_server_selftest_boots_and_stops():
    assert app_main._selftest_gui_server() == 0
