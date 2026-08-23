"""The README front door must carry the public promise and link the user manual."""

from __future__ import annotations

from pathlib import Path


def test_readme_carries_the_public_promise():
    text = Path("README.md").read_text(encoding="utf-8").lower()
    # Links the canonical public promise and honest scope.
    assert "docs/manual/capabilities-and-limits.md" in text
    assert "docs/manual/index.md" in text
    # offline + source-sacred guarantees
    assert "offline" in text
    assert ("never modifies" in text) or ("does not modify" in text) or ("read-only" in text)
    # both platforms + the honest windows caveat
    assert "dmg" in text
    assert "from source" in text
    # honest scope: best-effort frame-filling nebulae
    assert "best-effort" in text or "best effort" in text


def test_readme_names_required_tools():
    text = Path("README.md").read_text(encoding="utf-8")
    for tool in ("Siril", "GraXpert", "StarNet2", "ExifTool"):
        assert tool in text


def test_readme_scopes_doctor_to_source_installations():
    text = Path("README.md").read_text(encoding="utf-8").lower()
    assert "source installation" in text
    assert "packaged mac" in text
    assert "seestar doctor" in text
