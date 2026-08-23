"""v1.0 must attribute the separately-installed specialist tools and declare their
licenses, and the README must point to the notices."""

from __future__ import annotations

import importlib.resources as ir
import os
from pathlib import Path


def test_third_party_notices_names_the_external_tools():
    assert os.path.exists("THIRD_PARTY_NOTICES.md")
    text = open("THIRD_PARTY_NOTICES.md", encoding="utf-8").read()
    for tool in ("Siril", "GraXpert", "StarNet", "ExifTool"):
        assert tool in text, f"{tool} not attributed"


def test_readme_links_notices_and_states_separate_install():
    text = open("README.md", encoding="utf-8").read()
    assert "THIRD_PARTY_NOTICES.md" in text
    # the promise: tools are installed separately under their own licenses
    assert "separately" in text.lower()


def test_third_party_notices_attributes_openngc_catalog_data():
    """The shipped known_targets.json catalog is derived from OpenNGC (CC-BY-SA-4.0)
    data, not a user-installed tool -- it needs its own bundled-data attribution,
    distinct from the "install separately" external-tools table above."""
    assert os.path.exists("THIRD_PARTY_NOTICES.md")
    text = open("THIRD_PARTY_NOTICES.md", encoding="utf-8").read()
    assert "OpenNGC" in text, "OpenNGC not attributed"
    assert "CC-BY-SA" in text, "CC-BY-SA license not named"


def test_third_party_notices_ship_in_package():
    # The runtime attribution pointer must resolve from installed package data.
    assert (ir.files("seestar.data") / "THIRD_PARTY_NOTICES.md").is_file()
    assert (ir.files("seestar.data") / "OpenNGC-CC-BY-SA-4.0.txt").is_file()


def test_known_targets_license_pointer_resolves():
    import json

    meta = json.loads((ir.files("seestar.data") / "known_targets.json").read_text())
    # the license pointer is nested under provenance (known_targets.json:3-5), NOT top-level
    assert "THIRD_PARTY_NOTICES" in meta["provenance"]["license"]


def test_packaged_notices_match_repo_root():
    assert Path("seestar/data/THIRD_PARTY_NOTICES.md").read_text() == Path("THIRD_PARTY_NOTICES.md").read_text()
