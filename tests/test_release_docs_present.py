# tests/test_release_docs_present.py
"""Current public docs and operational release gates must retain their commitments."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.skipif(
    not (Path("docs/CLEAN_BUILD_TEST.md").exists() and Path("docs/BETA_PLAN.md").exists()),
    reason="docs/CLEAN_BUILD_TEST.md and docs/BETA_PLAN.md are maintainer-only and excluded from the public export",
)
def test_release_docs_exist():
    for path in (
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "SUPPORT.md",
        "docs/index.md",
        "docs/manual/capabilities-and-limits.md",
        "docs/CLEAN_BUILD_TEST.md",
        "docs/BETA_PLAN.md",
    ):
        assert Path(path).exists(), f"missing {path}"


def test_reference_style_explains_documentation_filename():
    text = Path("docs/manual/understanding-results.md").read_text(encoding="utf-8").lower()
    assert "reference style" in text
    assert "_documentation" in text


@pytest.mark.skipif(
    not Path("docs/BETA_PLAN.md").exists(),
    reason="docs/BETA_PLAN.md is maintainer-only and excluded from the public export",
)
def test_beta_plan_is_blocking_with_four_questions():
    text = Path("docs/BETA_PLAN.md").read_text(encoding="utf-8").lower()
    assert "blocking" in text
    # the four questions' themes
    for theme in ("install", "render", "understand", "prefer"):
        assert theme in text


@pytest.mark.skipif(
    not Path("docs/CLEAN_BUILD_TEST.md").exists(),
    reason="docs/CLEAN_BUILD_TEST.md is maintainer-only and excluded from the public export",
)
def test_clean_build_covers_three_categories_and_source_safety():
    text = Path("docs/CLEAN_BUILD_TEST.md").read_text(encoding="utf-8").lower()
    for cat in ("nebula", "galaxy", "cluster"):
        assert cat in text
    assert "doctor" in text
    assert "clobber" in text or "untouched" in text


def test_windows_packaged_app_is_marked_coming_soon_not_available():
    """A packaged Windows .exe shipped briefly in the v1.0.0 release but was
    pulled after a real bug (the folder-picker dialog not reliably getting
    foreground focus). Until a proper fix ships (wrapping the GUI in a native
    desktop shell, e.g. pywebview), the docs must say "coming soon" and the
    Windows path must be source-only -- not claim a packaged app is available."""
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "coming soon" in readme.lower()
    assert "the release build is a self-contained packaged application" not in readme.lower()

    caps = Path("docs/manual/capabilities-and-limits.md").read_text(encoding="utf-8")
    assert "macOS and Windows packaged applications" not in caps
    assert "A packaged Windows application (coming soon" in caps

    install = Path("docs/manual/installation.md").read_text(encoding="utf-8")
    assert "Windows 11 release application" not in install
    assert "coming soon" in install.lower()

    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
    assert "run from source for now" in changelog.lower()


def test_security_md_has_no_stale_pre_publish_conditional():
    text = Path("SECURITY.md").read_text(encoding="utf-8")
    assert "Before v1.0 is published" not in text


def test_config_yaml_names_no_second_person():
    # Positive check: confirm the sanitized replacement text is present, rather than
    # asserting a name-containing string is absent (which would require re-embedding
    # the name literally in this test file -- and this file ships in the public repo).
    for path in ("config.yaml", "seestar/data/config.default.yaml"):
        text = Path(path).read_text(encoding="utf-8")
        assert "a noisy M8 capture" in text, f"{path} missing the sanitized regression note"


@pytest.mark.skipif(
    not Path("docs/BETA_PLAN.md").exists(),
    reason="docs/BETA_PLAN.md is maintainer-only and excluded from the public export",
)
def test_beta_plan_records_the_2026_08_02_drop_decision():
    text = Path("docs/BETA_PLAN.md").read_text(encoding="utf-8")
    assert "2026-08-02" in text
    assert "dropped" in text.lower()
