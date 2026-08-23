"""Run-scoped owned artifact store.

Shell module: touches os/shutil/uuid. A Workspace owns exactly the files it
creates in its own scratch directory, plus the DELIVERABLE files it is told
about via register(). Reuse across stages happens ONLY through lookup(op_key,
ref) — a handle registered by the SAME producing operation in THIS run. There
is no adoption of files by path or name, so stale artifacts from a prior run
can never be picked up: the registry lives in memory and dies with the
Workspace instance.
"""

from __future__ import annotations

import os
import re
import shutil
import uuid

from seestar.core.types import ArtifactRef, ArtifactRole

_OUTPUT_ROLES = frozenset({ArtifactRole.DELIVERABLE})


_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|]')


def _sanitize(name: str) -> str:
    """Strip whitespace from a filename — Siril's `load` chokes on spaces.

    Artifact names are internally constructed, so anything that could escape
    the target directory (path separators, `..`) or hit Windows filename
    hazards (a colon writes an NTFS alternate data stream) is a bug upstream:
    fail loudly rather than accommodate it."""
    cleaned = re.sub(r"\s", "", name)
    if _UNSAFE_CHARS.search(cleaned) or cleaned.startswith(".."):
        raise ValueError(f"unsafe artifact name {name!r}")
    return cleaned


class Workspace:
    """Owns one run's scratch directory and tracks which files it produced."""

    def __init__(self, output_dir: str) -> None:
        self.output_dir = os.path.abspath(output_dir)
        self.scratch_dir = os.path.join(self.output_dir, f"_work_{uuid.uuid4().hex[:8]}")
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.scratch_dir, exist_ok=True)
        self._owned: dict[tuple[str, ArtifactRef], str] = {}
        self._producer_of: dict[ArtifactRef, str] = {}

    def path_for(self, ref: ArtifactRef, ext: str) -> str:
        """Where an adapter must write this artifact.

        DELIVERABLE routes to output_dir (it is the thing a user should
        find); QA artifacts execute in scratch and are promoted by the
        publication transaction; everything else (SOURCE, PRODUCTION, PROBE,
        CANDIDATE) is scratch, cleaned up at the end of the run.
        """
        base_dir = self.output_dir if ref.role in _OUTPUT_ROLES else self.scratch_dir
        filename = _sanitize(ref.name) + ext
        return os.path.join(base_dir, filename)

    def register(self, op_key: str, ref: ArtifactRef, path: str) -> None:
        """Record that `op_key` produced `ref` at `path`.

        Enforces a single-producer invariant PER REF (not per (op_key, ref)
        pair as before): once any op_key has claimed `ref`, no DIFFERENT
        op_key may claim it in this Workspace's lifetime. This is what makes
        recovery's "mint a fresh ref for anything invalidated" rule a
        structural guarantee rather than a convention -- a recovery
        implementation that forgets to remap a ref raises here instead of
        silently serving stale content (the exact bug in
        tests/test_workspace.py::test_reusing_output_ref_after_upstream_param_change...).
        Re-registering under the SAME op_key (e.g. a retried, unchanged op)
        is not a violation."""
        owner = self._producer_of.get(ref)
        if owner is not None and owner != op_key:
            raise ValueError(
                f"artifact ref {ref.name!r} ({ref.role.value}) already produced by "
                f"{owner!r}; {op_key!r} cannot also produce it (single-producer "
                "invariant) -- mint a fresh ArtifactRef name for anything a "
                "recovery attempt recomputes, never reuse the original ref"
            )
        self._producer_of[ref] = op_key
        self._owned[(op_key, ref)] = path

    def lookup(self, op_key: str, ref: ArtifactRef) -> str | None:
        """Return the path iff registered in THIS run by that exact op
        identity and the file still exists on disk. Never discovers files by
        name alone — this is the stale-adoption killer."""
        path = self._owned.get((op_key, ref))
        if path is None or not os.path.exists(path):
            return None
        return path

    def find_producer(self, ref: ArtifactRef) -> str | None:
        """Return the registered path for `ref` regardless of which op_key
        produced it. Input resolution ONLY — a Plan already guarantees a
        unique producer per ref (validate_plan), so op_key doesn't matter
        here. Reuse decisions must still go through `lookup`, which is keyed
        on the consuming op's own identity."""
        for (_op_key, owned_ref), path in self._owned.items():
            if owned_ref == ref and os.path.exists(path):
                return path
        return None

    def cleanup(self) -> None:
        """Remove everything this Workspace owns: the whole scratch dir
        (everything there is owned by construction — nothing else is ever
        written there), plus any registered output_dir file whose ref role is
        PROBE. Never touches anything else — no suffix inference, ever."""
        if os.path.isdir(self.scratch_dir):
            shutil.rmtree(self.scratch_dir)
        for (_op_key, ref), path in self._owned.items():
            if ref.role is ArtifactRole.PROBE and os.path.exists(path):
                os.remove(path)
