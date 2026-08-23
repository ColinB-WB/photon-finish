import pathlib
import re

import packaging.scan_personal_info as s


def test_approved_bundle_id_is_exactly_the_current_one():
    # Exact-set: adding an id (or leaving the retired pre-rename bundle id behind)
    # must be a deliberate, reviewed change here.
    assert s.APPROVED_PUBLIC_IDENTIFIERS == {"com.workingbackwards.photon-finish"}


def test_unexpected_bundle_id_would_not_be_approved():
    assert "com.someoneelse.app" not in s.APPROVED_PUBLIC_IDENTIFIERS


# bundle-id ASSIGNMENTS only — not arbitrary reverse-DNS (avoids com.apple.security.* keys)
_ASSIGN = re.compile(
    r'(?:BUNDLE_ID\s*=\s*["\']?|--osx-bundle-identifier\s+["\']?|<key>CFBundleIdentifier</key>\s*<string>)'
    r"(?P<id>[A-Za-z0-9.-]+)"
)


def test_all_hardcoded_bundle_ids_are_approved():
    found = set()
    for p in pathlib.Path("packaging").rglob("*"):
        if p.is_file() and p.suffix in {".sh", ".py", ".plist"}:
            found |= {m.group("id") for m in _ASSIGN.finditer(p.read_text(encoding="utf-8", errors="ignore"))}
    unlisted = found - s.APPROVED_PUBLIC_IDENTIFIERS
    assert not unlisted, f"unapproved bundle identifier assignment(s) in packaging/: {sorted(unlisted)}"


def test_validation_doc_excluded_from_public_export():
    globs = s.load_exclude("packaging/public_export.exclude")
    assert "docs/VALIDATION.md" in globs
