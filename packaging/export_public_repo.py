#!/usr/bin/env python3
"""Build a no-history public snapshot of this repo for the photon-finish public copy.

Repeatable release tool (run again for every future release, since the private
repo stays the permanent dev home -- docs/development/releasing.md). NEVER a
git clone: history must not travel (the pre-sanitization config.yaml lived
unsanitized through commit b5e3980).

Copies the working tree into a fresh directory, skipping
packaging/public_export.exclude, then `git init`s a single commit there.

  python -m packaging.export_public_repo --dest ../photon-finish

Run with `-m`, not as a bare script path: `packaging/` is not the repo root, so
`python packaging/export_public_repo.py` puts the wrong directory on sys.path[0]
and `import packaging.scan_personal_info` fails.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess

from packaging.scan_personal_info import _is_excluded, load_exclude, load_tokens, scan


def read_version(root: str) -> str:
    """Parse __version__ out of seestar/__init__.py without importing it."""
    path = os.path.join(root, "seestar", "__init__.py")
    text = open(path, encoding="utf-8").read()
    m = re.search(r'^__version__ = "(.*)"', text, re.MULTILINE)
    if not m:
        raise SystemExit(f"Could not read __version__ from {path}")
    return m.group(1)


def collect_files(root: str, exclude_globs: list[str]) -> list[str]:
    """Return repo-relative, POSIX-separated paths to copy, honoring the exclude list."""
    included: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        dirnames[:] = [d for d in dirnames if not _is_excluded(f"{rel_dir}/{d}" if rel_dir else d, exclude_globs)]
        for fn in filenames:
            rel = f"{rel_dir}/{fn}" if rel_dir else fn
            if not _is_excluded(rel, exclude_globs):
                included.append(rel)
    return sorted(included)


def export_tree(root: str, dest: str, exclude_globs: list[str]) -> list[str]:
    """Copy the included file set from `root` into a fresh `dest` directory.
    `dest` must not already exist. Returns the list of copied relative paths."""
    if os.path.exists(dest):
        raise SystemExit(f"Destination already exists, refusing to overwrite: {dest}")
    files = collect_files(root, exclude_globs)
    for rel in files:
        src_path = os.path.join(root, *rel.split("/"))
        dst_path = os.path.join(dest, *rel.split("/"))
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        shutil.copy2(src_path, dst_path)
    return files


def init_single_commit(dest: str, version: str) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=dest, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Photon Finish Release",
        "GIT_AUTHOR_EMAIL": "release@localhost",
        "GIT_COMMITTER_NAME": "Photon Finish Release",
        "GIT_COMMITTER_EMAIL": "release@localhost",
    }
    subprocess.run(
        ["git", "commit", "-q", "-m", f"Photon Finish v{version}"],
        cwd=dest,
        check=True,
        env=env,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--dest", required=True)
    ap.add_argument("--exclude", default=os.path.join("packaging", "public_export.exclude"))
    ap.add_argument("--tokens", default=".personal-tokens")
    ap.add_argument(
        "--skip-scan",
        action="store_true",
        help="Export even with no .personal-tokens configured. Only for exercising the export "
        "mechanics without real tokens set up -- never for an actual publish.",
    )
    args = ap.parse_args(argv)

    tokens = load_tokens(args.tokens)
    exclude_globs = load_exclude(args.exclude) if os.path.exists(args.exclude) else []
    if not tokens:
        if not args.skip_scan:
            print(
                f"ERROR: no .personal-tokens configured at {args.tokens!r} -- refusing to export "
                "without a personal-info scan. Copy .personal-tokens.example, fill in real "
                "usernames/emails/signing team ID, or pass --skip-scan to override (testing only)."
            )
            return 1
        print("WARNING: --skip-scan passed -- personal-info scan SKIPPED, not passed. Do not publish this export.")
    else:
        hits = scan(args.root, exclude_globs, tokens)
        if hits:
            print("PERSONAL INFO FOUND -- aborting export:")
            for rel, line, tok in hits:
                print(f"  {rel}:{line}: {tok!r}")
            return 1

    version = read_version(args.root)
    files = export_tree(args.root, args.dest, exclude_globs)
    init_single_commit(args.dest, version)

    print(f"Exported {len(files)} files to {args.dest} as a single commit (Photon Finish v{version}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
