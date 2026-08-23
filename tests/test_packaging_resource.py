"""Clean-installed-build coverage for the known-targets catalog (Task 8).

CLAUDE.md sec0 / the known-targets prototype design (`docs/history/superpowers/specs/
2026-07-25-known-targets-prototype-design.md` sec1 "Packaging") require the bundled
`seestar/data/known_targets.json` to load via **package resources** from a clean
INSTALLED build (a fresh-venv `pip install` of the wheel, and the frozen PyInstaller
app) -- not merely from a source checkout where a source-relative path would happen
to resolve.

This test suite cannot itself build a wheel into a fresh venv or run PyInstaller (both
are slow and environment-heavy -- see `docs/CLEAN_BUILD_TEST.md` for the documented
manual check). Instead it proves the two things that make that clean build work:

1. `seestar.known_targets.load_catalog()` resolves the catalog purely through
   `importlib.resources.files("seestar.data")` (never a source-relative `open(...)`
   path) and returns real records -- this is the exact mechanism an installed wheel
   (no source tree on disk) depends on.
2. The packaging declarations that make that resource ship in the first place are
   actually in place: `pyproject.toml`'s `[tool.setuptools.package-data]` includes
   `seestar.data` -> `*.json`, and `packaging/build_app.sh`'s PyInstaller invocation
   explicitly bundles `known_targets.json` via `--add-data` AND collects it via
   `--collect-data seestar` (belt-and-suspenders -- presence in the bundle is
   necessary but not sufficient; `docs/CLEAN_BUILD_TEST.md` sec1a is the manual gate
   that actually exercises `load_catalog()` from inside the frozen app).
"""

from __future__ import annotations

import importlib.resources as ir
from pathlib import Path

import yaml

from seestar import known_targets as kt
from seestar.io import load_config

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_load_catalog_via_importlib_resources_loads_real_records():
    # No source-relative path anywhere in this test -- load_catalog() must resolve
    # the bundled resource purely through importlib.resources.files("seestar.data"),
    # exactly as it will from an installed wheel with no source checkout
    # on disk at all.
    records = kt.load_catalog()
    assert len(records) >= 4, records

    canonical_ids = {r.canonical_id for r in records}
    assert {"M8", "NGC7000", "NGC5128", "NGC5139"} <= canonical_ids


def test_pyproject_declares_known_targets_package_data():
    pyproject_path = REPO_ROOT / "pyproject.toml"
    text = pyproject_path.read_text(encoding="utf-8")

    try:
        import tomllib

        data = tomllib.loads(text)
        package_data = data["tool"]["setuptools"]["package-data"]
        assert "seestar.data" in package_data, package_data
        patterns = package_data["seestar.data"]
        assert any(p.endswith(".json") or p == "*.json" for p in patterns), patterns
    except ImportError:
        # stdlib tomllib is 3.11+; fall back to a robust string check for older
        # runtimes (requires-python is >=3.10, CLAUDE.md sec0).
        assert '"seestar.data"' in text
        assert "*.json" in text


def test_build_app_sh_bundles_known_targets_json():
    build_script = REPO_ROOT / "packaging" / "build_app.sh"
    text = build_script.read_text(encoding="utf-8")

    assert '--add-data "seestar/data/known_targets.json:seestar/data"' in text, (
        "packaging/build_app.sh must explicitly bundle known_targets.json via "
        "PyInstaller --add-data (src:dest, dest = the package dir) so "
        "importlib.resources.files('seestar.data') resolves in the frozen app."
    )
    # Defense-in-depth (owner review): --add-data alone proves the file is present in
    # the bundle, not that PyInstaller's collected-package machinery resolves it the
    # same way an installed wheel's package-data does. --collect-data seestar bundles
    # the whole seestar package's declared data files under their importable package
    # path, belt-and-suspenders with the explicit --add-data above.
    assert "--collect-data seestar" in text, (
        "packaging/build_app.sh must also pass PyInstaller --collect-data seestar so "
        "the package's data files are collected under the importable seestar.data "
        "package path, as a defense-in-depth complement to --add-data."
    )


def test_packaged_default_config_exists_and_loads():
    ref = ir.files("seestar.data") / "config.default.yaml"
    assert ref.is_file()
    assert isinstance(yaml.safe_load(ref.read_text()), dict)


def test_packaged_default_matches_repo_root_config():
    root = yaml.safe_load(Path("config.yaml").read_text())
    packaged = yaml.safe_load((ir.files("seestar.data") / "config.default.yaml").read_text())
    assert packaged == root, "seestar/data/config.default.yaml drifted from config.yaml — resync"


def test_load_config_falls_back_to_packaged(monkeypatch):
    import seestar.io as io

    monkeypatch.setattr(io, "_CFG_PATH", Path("/nonexistent/config.yaml"))
    cfg = load_config()  # must NOT raise FileNotFoundError
    assert isinstance(cfg, dict) and cfg
