"""Built-in (no external process) adapters: source preparation, trail removal,
and the documentation-profile annotation overlay.

PrepareSource resolves its session index through `ctx.resolve["__session_paths__"]`,
a list of absolute paths the shell provides. CombineSessions follows the same
indexing convention in `siril.py`, where it shells to Siril to register and
integrate sessions. The Plan itself stays path-free.
"""

from __future__ import annotations

import os
import shutil

from seestar.core.types import Annotate, PrepareSource, RemoveTrail

from ._annotate_impl import annotate as _annotate_fn
from ._trail_impl import remove_trail_fits
from .base import AdapterError, ExecContext, register


def _copy_working(src: str, dst: str) -> str:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)
    return dst


class BuiltinAdapter:
    """Executes PrepareSource, RemoveTrail, and Annotate -- no external tool process."""

    operations = (PrepareSource, RemoveTrail, Annotate)
    name = "builtin"

    def execute(self, op, ctx: ExecContext) -> dict:
        if isinstance(op, PrepareSource):
            return self._prepare_source(op, ctx)
        if isinstance(op, RemoveTrail):
            return self._remove_trail(op, ctx)
        if isinstance(op, Annotate):
            return self._annotate(op, ctx)
        raise AdapterError(f"BuiltinAdapter cannot execute {type(op).__name__}")

    def _prepare_source(self, op: PrepareSource, ctx: ExecContext) -> dict:
        session_paths = ctx.resolve["__session_paths__"]
        raw_fit = session_paths[op.session_index]
        out_path = ctx.workspace.path_for(op.output, ".fit")
        return {op.output: _copy_working(raw_fit, out_path)}

    def _remove_trail(self, op: RemoveTrail, ctx: ExecContext) -> dict:
        in_path = ctx.resolve[op.input]
        out_path = ctx.workspace.path_for(op.output, ".fit")
        info = remove_trail_fits(in_path, out_path)
        if not info.get("present"):
            # remove_trail_fits writes nothing when no trail is found -- the caller
            # keeps the input (CLAUDE.md §6a: trail-free frames are untouched).
            return {op.output: in_path}
        return {op.output: out_path}

    def _annotate(self, op: Annotate, ctx: ExecContext) -> dict:
        in_path = ctx.resolve[op.input]
        out_path = ctx.workspace.path_for(op.output, ".jpg")
        card = op.card
        cd = None
        if card.cd:
            cd1_1, cd1_2, cd2_1, cd2_2 = card.cd
            cd = {"CD1_1": cd1_1, "CD1_2": cd1_2, "CD2_1": cd2_1, "CD2_2": cd2_2}
        info = {
            "title": card.title,
            "subtitle": card.subtitle,
            "lines": list(card.lines),
            "pixscale_arcsec": card.pixscale_arcsec,
            "cd": cd,
        }
        _annotate_fn(in_path, out_path, info, jpg_quality=ctx.jpg_quality)
        return {op.output: out_path}


register(BuiltinAdapter())
