from __future__ import annotations

from seestar import known_targets as kt
from seestar.core.types import Category


def test_load_catalog_parses_prototype_records():
    recs = kt.load_catalog()
    by_id = {r.canonical_id: r for r in recs}
    assert {"M8", "NGC7000", "NGC5128", "NGC5139"} <= set(by_id)
    ngc5128 = by_id["NGC5128"]
    assert ngc5128.category is Category.GALAXY
    assert isinstance(ngc5128.ra_deg, float) and isinstance(ngc5128.dec_deg, float)
    m8 = by_id["M8"]
    assert "NGC6523" in m8.equivalent_ids  # equivalence
    assert any(a["canonical_id"] == "NGC6530" for a in m8.associated)  # association, NOT alias
    assert "NGC6530" not in m8.aliases


def test_load_catalog_missing_resource_returns_empty(monkeypatch):
    # a corrupt/missing catalog must degrade, never raise
    monkeypatch.setattr(kt, "_read_catalog_text", lambda name: None)
    assert kt.load_catalog() == ()


def test_load_catalog_corrupt_json_returns_empty(monkeypatch):
    monkeypatch.setattr(kt, "_read_catalog_text", lambda name: "{not json")
    assert kt.load_catalog() == ()


def test_load_catalog_permission_error_returns_empty(monkeypatch):
    # a locked/quarantined bundled resource (plausible on the notarized Mac app or
    # Windows run-from-source) must degrade, never raise (spec sec5.7). Broader than
    # FileNotFoundError: PermissionError/IsADirectoryError are OSError subclasses too.
    def _raise(name):
        raise PermissionError("denied")

    monkeypatch.setattr(kt, "_read_catalog_text", _raise)
    assert kt.load_catalog() == ()


def test_read_catalog_text_permission_error_returns_none(tmp_path, monkeypatch):
    # exercise the real _read_catalog_text (not just load_catalog's caller-side
    # try/except) against an actual OSError from the filesystem: point it at a
    # directory, which raises IsADirectoryError on read_text.
    import importlib.resources

    (tmp_path / "known_targets.json").mkdir()

    class _FakeTraversable:
        def joinpath(self, name):
            return tmp_path / name

    monkeypatch.setattr(importlib.resources, "files", lambda pkg: _FakeTraversable())
    assert kt._read_catalog_text("known_targets.json") is None


def test_read_catalog_text_non_utf8_returns_none(tmp_path, monkeypatch):
    # a non-UTF-8 bundled resource must degrade like a missing one, not raise
    # UnicodeDecodeError out of load_catalog.
    import importlib.resources

    bad = tmp_path / "known_targets.json"
    bad.write_bytes(b"\xff\xfe\x00bad")

    class _FakeTraversable:
        def joinpath(self, name):
            return tmp_path / name

    monkeypatch.setattr(importlib.resources, "files", lambda pkg: _FakeTraversable())
    assert kt._read_catalog_text("known_targets.json") is None
    assert kt.load_catalog() == ()
