#!/usr/bin/env python3
"""Attach the locally-built macOS DMG and Windows exe to a GitHub Release.

Both artifacts are built fully manually and locally (packaging/build_app.sh +
sign_notarize_dmg.sh on macOS, packaging/build_app_windows.ps1 on Windows) --
this script only wraps the `gh release create` upload so a two-artifact
release is one command. No CI, no stored secrets
(docs/development/releasing.md).

  python -m packaging.publish_release --tag v1.0.0 \\
    --dmg "dist/Photon Finish.dmg" \\
    --windows-exe "dist/Photon Finish.exe" \\
    --repo ColinB-WB/photon-finish

Run with `-m`, matching packaging/export_public_repo.py's invocation, so both
release tools in this directory are always launched the same way.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess


def extract_changelog_section(text: str, version: str) -> str:
    """Return the body of CHANGELOG.md's `## [<version>] - ...` section (up to
    the next `## [` heading or end of file), stripped of surrounding blank lines."""
    pattern = re.compile(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)", re.MULTILINE | re.DOTALL)
    m = pattern.search(text)
    if not m:
        raise SystemExit(f"CHANGELOG.md has no '## [{version}]' section -- promote [Unreleased] first.")
    return m.group(1).strip() + "\n"


def build_gh_argv(tag: str, repo: str, notes_path: str, assets: list[str]) -> list[str]:
    return ["gh", "release", "create", tag, "--repo", repo, "--notes-file", notes_path, *assets]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True, help='e.g. v1.0.0 (CHANGELOG.md must have "## [1.0.0]")')
    ap.add_argument("--dmg", required=True)
    ap.add_argument("--windows-exe", required=True)
    ap.add_argument("--repo", required=True, help="e.g. ColinB-WB/photon-finish")
    ap.add_argument("--changelog", default="CHANGELOG.md")
    args = ap.parse_args(argv)

    version = args.tag.lstrip("v")
    for path in (args.dmg, args.windows_exe):
        if not os.path.isfile(path):
            print(f"Artifact not found: {path}")
            return 1
    if shutil.which("gh") is None:
        print("gh (GitHub CLI) not found on PATH.")
        return 1

    changelog_text = open(args.changelog, encoding="utf-8").read()
    section = extract_changelog_section(changelog_text, version)

    notes_path = os.path.join(os.path.dirname(os.path.abspath(args.dmg)), f".release-notes-{version}.md")
    with open(notes_path, "w", encoding="utf-8") as f:
        f.write(section)

    argv_gh = build_gh_argv(args.tag, args.repo, notes_path, [args.dmg, args.windows_exe])
    print("Running:", " ".join(argv_gh))
    result = subprocess.run(argv_gh)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
