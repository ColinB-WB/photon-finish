"""Path-containment helpers for the shell layer.

Shell module: os/path only, no core import. Enforces "source data is sacred"
(tenet 2) and output-root containment (audit B-08/B-09) at the join sites.
"""

from __future__ import annotations

import os


def safe_output_dir(output_root: str, label: str) -> str:
    """Return the canonical ``output_root/label``, guaranteed strictly inside
    ``output_root``. Raises ValueError on any label that escapes the root
    (``..``, absolute paths, drive/UNC, symlink games are all canonicalized away)."""
    root = os.path.realpath(output_root)
    candidate = os.path.realpath(os.path.join(root, label))
    if candidate != root and os.path.commonpath([root, candidate]) == root:
        return candidate
    raise ValueError(f"target label {label!r} escapes the output root {output_root!r}")


def assert_output_disjoint_from_source(source_dir: str, output_dir: str) -> None:
    """Enforce tenet 2 (source data is sacred): the output tree must not equal,
    sit beneath, or contain the read-only source import. Raises ValueError on
    any overlap after realpath canonicalization (symlinks resolved)."""
    src = os.path.realpath(source_dir)
    out = os.path.realpath(output_dir)
    if src == out:
        raise ValueError(f"output directory {output_dir!r} is the source directory")
    try:
        common = os.path.commonpath([src, out])
    except ValueError:
        # Windows raises ValueError when the two paths are on different drives
        # (e.g. a source on G:\ and output on C:\) -- there is no common path
        # because they can't possibly overlap, which is exactly "disjoint: OK".
        return
    if common == src:
        raise ValueError(f"output {output_dir!r} is inside the read-only source {source_dir!r}")
    if common == out:
        raise ValueError(f"source {source_dir!r} is inside the output {output_dir!r}")
