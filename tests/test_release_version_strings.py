"""No stale 3.x version strings remain in shipped, user-facing files, and the
packaging plist is derived from the single version source rather than a literal."""

from __future__ import annotations

import seestar


def test_runtime_version_matches_the_changelog_top_entry():
    """The release version is asserted against the CHANGELOG rather than a literal.

    Pinning the number here meant every bump broke a test that had nothing to say
    about the bump -- it was hard-coded to 1.0.0 and failed the moment 1.1.0
    landed. What actually matters is that the runtime version and the CHANGELOG's
    newest released heading agree, because `publish_release.py` refuses a tag
    whose heading is missing."""
    import re

    text = open("CHANGELOG.md", encoding="utf-8").read()
    headings = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", text, re.MULTILINE)
    assert headings, "CHANGELOG.md has no released version heading"
    assert seestar.__version__ == headings[0], (
        f"runtime {seestar.__version__} != newest CHANGELOG heading {headings[0]}"
    )


def test_readme_has_no_v3_version_claim():
    text = open("README.md", encoding="utf-8").read()
    assert "v3.x" not in text
    assert "Shipped since v3.0" not in text


def test_build_app_plist_uses_single_source_not_literal():
    text = open("packaging/build_app.sh", encoding="utf-8").read()
    # the plist version must not be a hard-coded 3.0.0 / 1.0.0 literal
    assert "CFBundleShortVersionString 3.0.0" not in text
    assert "CFBundleShortVersionString 1.0.0" not in text
    assert "CFBundleShortVersionString 1.1.0" not in text
    # it must derive from seestar/__init__.py
    assert "seestar/__init__.py" in text
