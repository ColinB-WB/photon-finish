"""Check repository-local Markdown links on the current documentation surface."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROOT_DOCS = (
    ROOT / "README.md",
    ROOT / "INSTALL.md",
    ROOT / "CHANGELOG.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    ROOT / "SUPPORT.md",
    ROOT / "THIRD_PARTY_NOTICES.md",
)
EXCLUDED_DOC_PARTS = {
    ("docs", "history", "v1.0"),
    # The implementation-record archive, moved from docs/superpowers/ on 2026-08-19.
    # Excluded for the same reason v1.0 is: archived plans and specs are not part of
    # the current documentation surface, and they carry stale relative paths and
    # code snippets that read as links.
    ("docs", "history", "superpowers"),
}
LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")


def _is_excluded(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    parts = rel.parts
    return any(parts[: len(prefix)] == prefix for prefix in EXCLUDED_DOC_PARTS)


def markdown_files() -> list[Path]:
    files = [path for path in ROOT_DOCS if path.exists()]
    files.extend(path for path in (ROOT / "docs").rglob("*.md") if not _is_excluded(path))
    return sorted(set(files))


def _link_path(raw: str) -> str | None:
    value = raw.strip()
    if value.startswith("<") and ">" in value:
        value = value[1 : value.index(">")]
    else:
        value = value.split(maxsplit=1)[0]
    value = value.split("#", 1)[0]
    if not value or value.startswith(("http://", "https://", "mailto:", "tel:", "data:")):
        return None
    return value


def find_broken_links() -> list[str]:
    broken: list[str] = []
    for source in markdown_files():
        text = source.read_text(encoding="utf-8")
        for match in LINK_RE.finditer(text):
            value = _link_path(match.group(1))
            if value is None:
                continue
            target = Path(value)
            if not target.is_absolute():
                target = source.parent / target
            if not target.resolve().exists():
                line = text.count("\n", 0, match.start()) + 1
                broken.append(f"{source.relative_to(ROOT)}:{line}: {match.group(1)}")
    return broken


def main() -> int:
    broken = find_broken_links()
    if broken:
        print("Broken local Markdown links:")
        for item in broken:
            print(f"- {item}")
        return 1
    print(f"OK: checked local links in {len(markdown_files())} current Markdown files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
