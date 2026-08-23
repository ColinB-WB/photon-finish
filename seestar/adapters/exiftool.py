"""exiftool adapter: embed provenance metadata into finished JPG/TIFF (CLAUDE.md §11).

Port of `metadata.embed`'s arg-list construction + subprocess call, reading its facts
from `op.card` (a MetadataCard) instead of raw inspection/params -- the SHELL
(seestar/metadata.py) builds the card (display strings, target-name lookups,
provenance formatting all stay there, never in seestar.core). EXIF:Software is
composed here from cfg-independent constants (ExecContext carries no cfg) that
mirror config.yaml's current graxpert model versions.
"""

from __future__ import annotations

from seestar.core.types import EmbedMetadata, MetadataCard

from ._external_process import run_external
from .base import AdapterError, ExecContext, register

_BGE_MODEL = "1.0.1"
_DENOISE_MODEL = "3.0.2"
_SIRIL_VERSION = "1.4.4"
_SOFTWARE = f"GraXpert bge{_BGE_MODEL}/dn{_DENOISE_MODEL} + Siril {_SIRIL_VERSION} + StarNet2"


def _build_args(card: MetadataCard, variant: str, exiftool: str) -> list[str]:
    args = [
        exiftool,
        "-overwrite_original",
        f"-EXIF:ImageDescription={card.one_liner}",
        f"-EXIF:Software={_SOFTWARE}",
        f"-EXIF:UserComment={card.user_comment}",
        f"-XMP:Title={card.title}",
        f"-XMP:Description={card.description}",
        f"-IPTC:ObjectName={card.object_name}",
    ]
    if card.artist:  # omit Artist when unset (don't stamp a blank/other name)
        args.append(f"-EXIF:Artist={card.artist}")
    if card.datetime_original:
        args.append(f"-EXIF:DateTimeOriginal={card.datetime_original}")
    if card.ra_deg is not None and card.dec_deg is not None:
        args += [f"-XMP:Subject+=RA {round(card.ra_deg, 4)}", f"-XMP:Subject+=Dec {round(card.dec_deg, 4)}"]
        if card.pixscale_arcsec:
            args.append(f"-XMP:Subject+={card.pixscale_arcsec} arcsec/px")
    for kw in card.keywords:
        if kw:
            args.append(f"-XMP:Subject+={kw}")
    args.append(f"-XMP:Subject+=variant:{variant}")
    return args


class ExiftoolAdapter:
    """Executes EmbedMetadata by shelling to exiftool, once per target artifact."""

    operations = (EmbedMetadata,)
    name = "exiftool"

    def execute(self, op, ctx: ExecContext) -> dict:
        if not isinstance(op, EmbedMetadata):
            raise AdapterError(f"ExiftoolAdapter cannot execute {type(op).__name__}")
        if ctx.tools is None:
            raise AdapterError("ExiftoolAdapter requires resolved tools")
        args = _build_args(op.card, op.variant, ctx.tools.exiftool)
        for ref in op.targets:
            path = ctx.resolve[ref]
            # Bounded by tools.timeouts.exiftool (H1 Task 4); ExternalToolTimeout
            # propagates to the caller (metadata.embed / orchestrate.py).
            run_external(
                [*args, path],
                tool="exiftool",
                tools=ctx.tools,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
            )
        return {}


register(ExiftoolAdapter())
