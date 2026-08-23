from pathlib import Path


def test_fixtures_readme_states_ownership_and_redistribution():
    text = Path("tests/fixtures/README.md").read_text(encoding="utf-8")
    lowered = text.lower()
    assert "redistribut" in lowered, "fixtures README must state redistribution permission"
    assert "colin bryar" in lowered, "fixtures README must record the capturing owner"
    # The unresolvable pointer must be gone (a history-free public snapshot has no git log).
    assert "see git log" not in lowered
