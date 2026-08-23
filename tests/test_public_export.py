"""The personal-info scanner and the public-export exclude-list.

The scanner is the automated backstop before the owner's manual scan (docs/RELEASE_READINESS.md):
it flags machine-specific leaks (home-path usernames, private emails, signing Team ID, a second
person's capture folders) but NOT the author's public byline "Colin Bryar". Real tokens are never
hard-coded in this file — they live in a git-ignored `.personal-tokens` (see
`packaging/scan_personal_info.load_tokens`); these tests exercise the mechanism with synthetic
tokens instead.
"""

from __future__ import annotations

import os

from packaging.scan_personal_info import _is_excluded, load_exclude, load_tokens, scan


def test_scan_flags_a_known_token(tmp_path):
    (tmp_path / "leak.txt").write_text("path is /Users/ACME_LEAK_USER/secret\n")
    hits = scan(str(tmp_path), exclude_globs=[], tokens=["ACME_LEAK_USER"])
    assert any(tok == "ACME_LEAK_USER" for _, _, tok in hits)


def test_scan_ignores_excluded_paths(tmp_path):
    d = tmp_path / "dev-notes"
    d.mkdir()
    (d / "leak.txt").write_text("acme-leak@example.com\n")
    hits = scan(str(tmp_path), exclude_globs=["dev-notes/"], tokens=["acme-leak@example.com"])
    assert hits == []


def test_scan_allows_author_public_name(tmp_path):
    (tmp_path / "LICENSE").write_text("Copyright (c) 2026 Colin Bryar\n")
    # Token list deliberately does NOT include "Colin Bryar" — the public byline is never flagged.
    hits = scan(str(tmp_path), exclude_globs=[], tokens=["ACME_LEAK_USER"])
    assert hits == []


def test_scan_with_no_tokens_finds_nothing(tmp_path):
    (tmp_path / "leak.txt").write_text("path is /Users/ACME_LEAK_USER/secret\n")
    hits = scan(str(tmp_path), exclude_globs=[], tokens=[])
    assert hits == []


def test_exclude_list_loads_and_covers_maintainer_trees():
    globs = load_exclude(os.path.join("packaging", "public_export.exclude"))
    for required in (
        "dev-notes/",
        ".claude/",
        "validation/",
        "experiments/",
        "config.local.yaml",
        ".git/",
        ".superpowers/",
        ".personal-tokens",
        "docs/RELEASE_READINESS.md",
        "docs/BETA_PLAN.md",
        "docs/CLEAN_BUILD_TEST.md",
        "docs/assessments/",
        "docs/superpowers/",
        "docs/history/",
    ):
        assert required in globs


def test_exclude_list_covers_gitignored_build_and_cache_artifacts():
    """The export copies the working tree and never reads .gitignore, so every
    git-ignored artifact must be listed here or it ships. All six below reached a
    v1.1.0 dry-run export before this was added."""
    globs = load_exclude(os.path.join("packaging", "public_export.exclude"))
    for leaked in (
        ".DS_Store",
        "docs/.DS_Store",
        ".mypy_cache/CACHEDIR.TAG",
        ".pytest_cache/v/cache/lastfailed",
        ".ruff_cache/0.14.0/12345",
        "Photon Finish.spec",
        "Seestar S30.spec",
    ):
        assert _is_excluded(leaked, globs), leaked


def test_load_tokens_reads_one_per_line_ignoring_blanks_and_comments(tmp_path):
    p = tmp_path / "tokfile"
    p.write_text("\n# a comment\nfoo-token\n\nbar-token\n")
    assert load_tokens(str(p)) == ["foo-token", "bar-token"]


def test_load_tokens_missing_file_returns_empty_list():
    assert load_tokens(str(os.path.join(str(os.getcwd()), "does-not-exist-token-file"))) == []
    assert load_tokens("missing") == []


def test_future_only_doc_excluded_from_public_export():
    globs = load_exclude("packaging/public_export.exclude")
    assert _is_excluded("docs/future/input-scenarios.md", globs)
