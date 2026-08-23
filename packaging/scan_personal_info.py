#!/usr/bin/env python3
"""Scan a file tree for machine-specific personal info before a public copy.

Tokens are never hard-coded here — they live in a git-ignored `.personal-tokens` file
(one per line; copy `.personal-tokens.example` and fill in your own machine-specific
leaks: home-path usernames, private emails, signing Team ID, other people's names).
Does NOT flag the author's public byline "Colin Bryar" unless it happens to be listed
as a token (it should not be). Exit non-zero if anything is found. Respects
packaging/public_export.exclude.

  python packaging/scan_personal_info.py            # scan repo root minus the exclude-list
  python packaging/scan_personal_info.py --root .   # explicit root
"""

from __future__ import annotations

import argparse
import fnmatch
import os


def load_tokens(path: str = ".personal-tokens") -> list[str]:
    """Load one personal token per line from `path`. Blank lines and '#' comments
    are ignored. Returns [] if the file does not exist (honest no-op, not an error)."""
    if not os.path.exists(path):
        return []
    tokens: list[str] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                tokens.append(line)
    return tokens


# Files we never scan as text (binary / images).
_SKIP_EXT = {
    ".fit",
    ".fits",
    ".tif",
    ".tiff",
    ".jpg",
    ".jpeg",
    ".png",
    ".xisf",
    ".ser",
    ".cr2",
    ".raw",
    ".pyc",
    ".gz",
    ".zip",
    ".dmg",
    ".icns",
    ".so",
    ".dylib",
}

# Reverse-DNS identifiers that are INTENTIONAL public project identity, reviewed
# and approved by the owner (2026-07-28). The scanner treats these as expected;
# any OTHER hard-coded org-derived identifier remains something to surface.
APPROVED_PUBLIC_IDENTIFIERS = frozenset(
    {
        "com.workingbackwards.photon-finish",  # macOS bundle id, packaging/build_app.sh
    }
)


def load_exclude(path: str) -> list[str]:
    globs: list[str] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                globs.append(line)
    return globs


def _is_excluded(rel: str, exclude_globs: list[str]) -> bool:
    parts = rel.replace(os.sep, "/")
    for g in exclude_globs:
        gg = g.rstrip("/")
        if fnmatch.fnmatch(parts, g) or fnmatch.fnmatch(parts, gg):
            return True
        # directory prefix match: "dev-notes/" excludes "dev-notes/a/b.txt"
        if g.endswith("/") and (parts == gg or parts.startswith(gg + "/")):
            return True
        # bare glob like "*.egg-info/" against any path segment
        if any(fnmatch.fnmatch(seg, gg) for seg in parts.split("/")):
            return True
    return False


def scan(root: str, exclude_globs: list[str], tokens: list[str]) -> list[tuple[str, int, str]]:
    hits: list[tuple[str, int, str]] = []
    if not tokens:
        return hits
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir
        # prune excluded directories in-place for speed
        dirnames[:] = [
            d for d in dirnames if not _is_excluded(os.path.join(rel_dir, d) if rel_dir else d, exclude_globs)
        ]
        for fn in filenames:
            rel = os.path.join(rel_dir, fn) if rel_dir else fn
            if _is_excluded(rel, exclude_globs):
                continue
            if os.path.splitext(fn)[1].lower() in _SKIP_EXT:
                continue
            full = os.path.join(dirpath, fn)
            try:
                with open(full, encoding="utf-8", errors="strict") as f:
                    for i, line in enumerate(f, start=1):
                        for tok in tokens:
                            if tok in line:
                                hits.append((rel, i, tok))
            except (UnicodeDecodeError, OSError):
                continue  # binary or unreadable — skip
    return hits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--exclude", default=os.path.join("packaging", "public_export.exclude"))
    ap.add_argument("--tokens", default=".personal-tokens")
    args = ap.parse_args(argv)
    tokens = load_tokens(args.tokens)
    if not tokens:
        print("No .personal-tokens configured — copy .personal-tokens.example. Nothing to scan.")
        return 0
    exclude = load_exclude(args.exclude) if os.path.exists(args.exclude) else []
    hits = scan(args.root, exclude, tokens)
    if hits:
        print("PERSONAL INFO FOUND (must be neutralized before public copy):")
        for rel, line, tok in hits:
            print(f"  {rel}:{line}: {tok!r}")
        return 1
    print("Clean: no personal tokens in the included (public) file set.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
