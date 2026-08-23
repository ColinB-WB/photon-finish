"""The signing Team ID / org must not be hard-coded in packaging scripts or docs;
they come from env vars at sign time. Forbidden tokens are loaded from the git-ignored
.personal-tokens (packaging/scan_personal_info.py itself carries no tokens at all now,
so no special exemption is needed, but we keep the scanner module exempt for safety)."""

from __future__ import annotations

import glob
import os

import pytest

from packaging.scan_personal_info import load_tokens

_EXEMPT = {os.path.join("packaging", "scan_personal_info.py")}


def test_no_hardcoded_signing_identity_in_packaging():
    tokens = load_tokens()
    if not tokens:
        pytest.skip("no .personal-tokens configured")
    offenders = []
    for path in glob.glob("packaging/**/*", recursive=True):
        if not os.path.isfile(path) or path in _EXEMPT:
            continue
        try:
            text = open(path, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        for tok in tokens:
            if tok in text:
                offenders.append((path, tok))
    assert not offenders, f"hard-coded signing identity found: {offenders}"
