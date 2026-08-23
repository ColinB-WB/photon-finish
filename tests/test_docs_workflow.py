"""Sanity checks on the MkDocs -> GitHub Pages deploy workflow."""

from __future__ import annotations

from pathlib import Path

import yaml


def test_docs_workflow_exists_and_parses():
    path = Path(".github/workflows/docs.yml")
    assert path.exists()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data is not None


def test_docs_workflow_triggers_on_docs_changes_to_main():
    data = yaml.safe_load(Path(".github/workflows/docs.yml").read_text(encoding="utf-8"))
    push_cfg = data[True]["push"]  # YAML parses bare `on:` as boolean key True
    assert push_cfg["branches"] == ["main"]
    assert "docs/**" in push_cfg["paths"]
    assert "mkdocs.yml" in push_cfg["paths"]


def test_docs_workflow_builds_strict_and_deploys_pages():
    text = Path(".github/workflows/docs.yml").read_text(encoding="utf-8")
    assert "mkdocs build --strict" in text
    assert "actions/deploy-pages" in text
    assert "actions/upload-pages-artifact" in text


def test_docs_workflow_deploy_is_guarded_to_the_public_repo():
    """The private repo has no GitHub Pages enabled -- deploy must not run there."""
    data = yaml.safe_load(Path(".github/workflows/docs.yml").read_text(encoding="utf-8"))
    deploy = data["jobs"]["deploy"]
    assert deploy.get("if") == "github.repository == 'ColinB-WB/photon-finish'"
