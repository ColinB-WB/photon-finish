"""The repeatable no-history public-export tool. Never a git clone -- history
must not travel (docs/history/superpowers/specs/2026-08-02-photon-finish-public-release-design.md)."""

from __future__ import annotations

import os
import subprocess

from packaging.export_public_repo import (
    collect_files,
    export_tree,
    init_single_commit,
    main,
    read_version,
)


def _write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def test_collect_files_honors_exclude_list(tmp_path):
    root = tmp_path / "src"
    _write(str(root / "README.md"), "hello")
    _write(str(root / "dev-notes" / "secret.md"), "private")
    files = collect_files(str(root), ["dev-notes/"])
    assert files == ["README.md"]


def test_collect_files_excludes_nested_dot_git(tmp_path):
    root = tmp_path / "src"
    _write(str(root / "a.txt"), "x")
    _write(str(root / ".git" / "HEAD"), "ref: refs/heads/main")
    files = collect_files(str(root), [".git/"])
    assert files == ["a.txt"]


def test_export_tree_copies_only_included_files(tmp_path):
    root = tmp_path / "src"
    dest = tmp_path / "dest"
    _write(str(root / "README.md"), "hello")
    _write(str(root / "dev-notes" / "secret.md"), "private")
    copied = export_tree(str(root), str(dest), ["dev-notes/"])
    assert copied == ["README.md"]
    assert (dest / "README.md").read_text(encoding="utf-8") == "hello"
    assert not (dest / "dev-notes").exists()


def test_export_tree_refuses_to_overwrite_existing_dest(tmp_path):
    root = tmp_path / "src"
    dest = tmp_path / "dest"
    _write(str(root / "a.txt"), "x")
    dest.mkdir()
    try:
        export_tree(str(root), str(dest), [])
        raise AssertionError("expected SystemExit")
    except SystemExit:
        pass


def test_read_version_parses_dunder_version(tmp_path):
    root = tmp_path / "src"
    _write(str(root / "seestar" / "__init__.py"), '__version__ = "9.9.9"\n')
    assert read_version(str(root)) == "9.9.9"


def test_init_single_commit_creates_exactly_one_commit(tmp_path):
    dest = tmp_path / "dest"
    _write(str(dest / "README.md"), "hello")
    init_single_commit(str(dest), "9.9.9")
    branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=str(dest), capture_output=True, text=True, check=True
    ).stdout.strip()
    assert branch == "main"
    log = (
        subprocess.run(["git", "log", "--oneline"], cwd=str(dest), capture_output=True, text=True, check=True)
        .stdout.strip()
        .splitlines()
    )
    assert len(log) == 1
    assert (
        "Photon Finish v9.9.9"
        in subprocess.run(
            ["git", "log", "-1", "--format=%s"], cwd=str(dest), capture_output=True, text=True, check=True
        ).stdout
    )


def test_main_aborts_on_personal_info_hit(tmp_path):
    root = tmp_path / "src"
    dest = tmp_path / "dest"
    _write(str(root / "seestar" / "__init__.py"), '__version__ = "9.9.9"\n')
    _write(str(root / "leak.txt"), "path is /Users/ACME_LEAK_USER/secret\n")
    tokens_path = tmp_path / "tokens"
    _write(str(tokens_path), "ACME_LEAK_USER\n")
    exclude_path = tmp_path / "exclude"
    _write(str(exclude_path), "")

    rc = main(
        [
            "--root",
            str(root),
            "--dest",
            str(dest),
            "--exclude",
            str(exclude_path),
            "--tokens",
            str(tokens_path),
        ]
    )
    assert rc == 1
    assert not dest.exists()


def test_main_exports_clean_tree_successfully(tmp_path):
    root = tmp_path / "src"
    dest = tmp_path / "dest"
    _write(str(root / "seestar" / "__init__.py"), '__version__ = "9.9.9"\n')
    _write(str(root / "README.md"), "hello")
    exclude_path = tmp_path / "exclude"
    _write(str(exclude_path), "")
    tokens_path = tmp_path / "tokens"
    _write(str(tokens_path), "SOME_TOKEN\n")

    rc = main(
        [
            "--root",
            str(root),
            "--dest",
            str(dest),
            "--exclude",
            str(exclude_path),
            "--tokens",
            str(tokens_path),
        ]
    )
    assert rc == 0
    assert (dest / "README.md").exists()
    assert (dest / ".git").is_dir()


def test_main_fails_closed_with_no_tokens_and_no_skip_scan(tmp_path):
    """The plan's own highest-risk item (personal-info sanitization) must fail closed:
    with no .personal-tokens configured and --skip-scan not passed, refuse to export."""
    root = tmp_path / "src"
    dest = tmp_path / "dest"
    _write(str(root / "seestar" / "__init__.py"), '__version__ = "9.9.9"\n')
    _write(str(root / "README.md"), "hello")
    exclude_path = tmp_path / "exclude"
    _write(str(exclude_path), "")

    rc = main(
        [
            "--root",
            str(root),
            "--dest",
            str(dest),
            "--exclude",
            str(exclude_path),
            "--tokens",
            str(tmp_path / "does-not-exist-tokens"),
        ]
    )
    assert rc == 1
    assert not dest.exists()


def test_main_skip_scan_exports_with_no_tokens(tmp_path):
    """--skip-scan is the explicit override for exercising export mechanics without
    real tokens configured (e.g. this test suite)."""
    root = tmp_path / "src"
    dest = tmp_path / "dest"
    _write(str(root / "seestar" / "__init__.py"), '__version__ = "9.9.9"\n')
    _write(str(root / "README.md"), "hello")
    exclude_path = tmp_path / "exclude"
    _write(str(exclude_path), "")

    rc = main(
        [
            "--root",
            str(root),
            "--dest",
            str(dest),
            "--exclude",
            str(exclude_path),
            "--tokens",
            str(tmp_path / "does-not-exist-tokens"),
            "--skip-scan",
        ]
    )
    assert rc == 0
    assert (dest / "README.md").exists()
    assert (dest / ".git").is_dir()
