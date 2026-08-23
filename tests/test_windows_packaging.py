"""The Windows build must produce a single portable .exe (PyInstaller
--onefile), matching the Mac DMG's one-file, no-install-step distribution --
not a zip of a folder."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_windows_build_uses_onefile_not_zip():
    text = Path("packaging/build_app_windows.ps1").read_text(encoding="utf-8")
    assert "--onefile" in text
    assert "Compress-Archive" not in text
    assert "-windows.zip" not in text


def test_windows_build_uses_new_gui_stack_and_static_assets():
    text = Path("packaging/build_app_windows.ps1").read_text(encoding="utf-8")
    assert '".[gui,desktop]"' in text
    assert "seestar\\gui\\static;seestar\\gui\\static" in text
    for removed in (
        "--collect-all gradio",
        "--collect-all gradio_client",
        "--collect-all safehttpx",
        "--collect-all groovy",
        "--copy-metadata gradio",
    ):
        assert removed not in text


@pytest.mark.skipif(
    not Path("docs/CLEAN_BUILD_TEST.md").exists(),
    reason="docs/CLEAN_BUILD_TEST.md is maintainer-only and excluded from the public export",
)
def test_clean_build_test_references_single_exe_not_zip():
    text = Path("docs/CLEAN_BUILD_TEST.md").read_text(encoding="utf-8")
    assert "-windows.zip" not in text
    assert "dist\\Photon Finish\\Photon Finish.exe" not in text
    assert "dist\\Photon Finish.exe" in text
