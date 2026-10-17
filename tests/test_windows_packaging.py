"""The Windows build must produce one installer (Inno Setup) wrapping a PyInstaller
--onedir folder -- never a --onefile exe (Defender flagged it as Wacatac.B!ml) and
never a zip of the folder (Explorer marks extracted files as downloaded, and .NET then
refuses to load pythonnet, so the window never opens). See packaging/photon_finish.iss."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_windows_build_is_onedir_wrapped_in_an_installer():
    text = Path("packaging/build_app_windows.ps1").read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "--onedir" in code
    assert "--onefile" not in code  # the comments still explain why it was dropped
    assert "packaging\\photon_finish.iss" in text
    assert "/DAppVersion=$Version" in text
    assert "Compress-Archive" not in text


def test_installer_packages_the_onedir_folder_per_user():
    text = Path("packaging/photon_finish.iss").read_text(encoding="utf-8")
    assert 'Source: "..\\dist\\{#AppName}\\*"' in text
    assert "OutputBaseFilename={#AppName} Setup" in text
    # Per-user: no UAC prompt for a hobby app.
    assert "PrivilegesRequired=lowest" in text
    # Changing AppId would make Windows treat every upgrade as a second, separate app.
    assert "AppId={{2C7FBF53-299B-46C7-AC82-ABF003D94400}" in text


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
def test_clean_build_test_references_the_installer():
    text = Path("docs/CLEAN_BUILD_TEST.md").read_text(encoding="utf-8")
    assert "-windows.zip" not in text
    assert "dist\\Photon Finish Setup.exe" in text
    assert "--onefile" not in text
