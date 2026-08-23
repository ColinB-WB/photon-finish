# tests/test_pre_copy_gate.py
"""Pre-copy gate: the file set that would become public must carry no personal tokens.
Runs the scanner over the live repo minus the export exclude-list. This is the automated
backstop before the owner's manual scan + no-history copy. Tokens come from the git-ignored
.personal-tokens; with none configured the gate is honestly SKIPPED (not silently passed)."""

from __future__ import annotations

import os

import pytest

from packaging.scan_personal_info import load_exclude, load_tokens, scan


def test_included_public_set_has_no_personal_info():
    tokens = load_tokens()
    if not tokens:
        pytest.skip("no .personal-tokens configured — cannot run the pre-copy scan")
    exclude = load_exclude(os.path.join("packaging", "public_export.exclude"))
    hits = scan(".", exclude_globs=exclude, tokens=tokens)
    assert hits == [], "personal info in the would-be-public file set:\n" + "\n".join(
        f"  {rel}:{line}: {tok!r}" for rel, line, tok in hits
    )
