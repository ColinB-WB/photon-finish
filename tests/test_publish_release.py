"""The gh-release-create wrapper. No real `gh` invocation in tests -- only the
pre-flight checks and pure argv/notes construction are exercised."""

from __future__ import annotations

from packaging.publish_release import build_gh_argv, extract_changelog_section, main

_CHANGELOG = """# Changelog

## [Unreleased]

### Preparing v1.0

- work in progress

## [1.0.0] - 2026-08-15

### Added

- First public release.

### Renamed for release

- The public project is Photon Finish.

## [0.9.0] - 2026-01-01

- older stuff
"""


def test_extract_changelog_section_returns_body_up_to_next_heading():
    section = extract_changelog_section(_CHANGELOG, "1.0.0")
    assert "First public release." in section
    assert "Renamed for release" in section
    assert "older stuff" not in section
    assert "Unreleased" not in section


def test_extract_changelog_section_missing_version_raises():
    try:
        extract_changelog_section(_CHANGELOG, "9.9.9")
        raise AssertionError("expected SystemExit")
    except SystemExit:
        pass


def test_build_gh_argv_includes_tag_repo_notes_and_assets():
    argv = build_gh_argv("v1.0.0", "ColinB-WB/photon-finish", "/tmp/notes.md", ["a.dmg", "b.exe"])
    assert argv == [
        "gh",
        "release",
        "create",
        "v1.0.0",
        "--repo",
        "ColinB-WB/photon-finish",
        "--notes-file",
        "/tmp/notes.md",
        "a.dmg",
        "b.exe",
    ]


def test_main_reports_missing_artifact(tmp_path, capsys):
    rc = main(
        [
            "--tag",
            "v1.0.0",
            "--dmg",
            str(tmp_path / "missing.dmg"),
            "--windows-exe",
            str(tmp_path / "missing.exe"),
            "--repo",
            "ColinB-WB/photon-finish",
        ]
    )
    assert rc == 1
    assert "not found" in capsys.readouterr().out


def test_main_reports_missing_gh_cli(tmp_path, monkeypatch, capsys):
    dmg = tmp_path / "a.dmg"
    winexe = tmp_path / "b.exe"
    dmg.write_text("x")
    winexe.write_text("x")
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(_CHANGELOG, encoding="utf-8")

    import packaging.publish_release as pr

    monkeypatch.setattr(pr.shutil, "which", lambda name: None)

    rc = main(
        [
            "--tag",
            "v1.0.0",
            "--dmg",
            str(dmg),
            "--windows-exe",
            str(winexe),
            "--repo",
            "ColinB-WB/photon-finish",
            "--changelog",
            str(changelog),
        ]
    )
    assert rc == 1
    assert "gh" in capsys.readouterr().out.lower()
