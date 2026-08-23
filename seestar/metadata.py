"""Build and embed provenance metadata into finished JPG/TIFF files.

This shell module turns inspection data, render parameters, and config into a
`MetadataCard`, then hands that typed card to the exiftool adapter. Display text
and provenance formatting stay here rather than in `seestar.core`. The target's
common name is supplied BY THE CALLER from the offline catalog's spatial match --
this module knows no object names of its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .adapters.base import ExecContext
from .adapters.exiftool import ExiftoolAdapter
from .core.types import ArtifactRef, ArtifactRole, EmbedMetadata, MetadataCard

if TYPE_CHECKING:
    from .adapters.base import ToolPaths


def instrument(inspection: dict, cfg: dict) -> str:
    """The capture instrument, from the FITS header CREATOR ("ZWO Seestar S30",
    "ZWO Seestar S30 Pro", ...); config defaults.telescope is the fallback for
    headers that omit it."""
    return (inspection["header"].get("CREATOR") or "").strip() or cfg["defaults"]["telescope"]


def _build_card(
    inspection: dict,
    params: dict,
    variant: str,
    cfg: dict,
    certification: str = "certified",
    common_name: str | None = None,
) -> MetadataCard:
    """Deterministic capture facts + provenance, as a content-only MetadataCard --
    display strings belong in the shell (here), never in seestar.core (which the
    adapter is restricted to).

    `common_name` is cosmetic and comes from the caller's offline catalog match
    (by WCS footprint -- `known_targets.match_target`), never from a name table
    here: it decorates the title and adds a keyword, and its absence simply
    leaves the bare target label. It never influences a processing decision."""
    defaults = cfg["defaults"]
    scope = instrument(inspection, cfg)
    m = inspection["meta"]
    h = inspection["header"]
    cls = inspection["classification"]
    tgt = m.get("target") or "unknown"
    common = (common_name or "").strip()
    title = f"{tgt}" + (f" — {common}" if common else "")
    description = (
        f"{cls['category']}; {m.get('frames')}x{m.get('sub_exposure_s')}s = "
        f"{m.get('total_integration_min')} min; {m.get('filter')} filter"
    )
    one_liner = (
        f"{title}. {m.get('frames')}x{m.get('sub_exposure_s')}s = "
        f"{m.get('total_integration_min')} min, {m.get('filter')} filter, "
        f"{m.get('datetime', '')[:10]}. {scope}."
    )
    ght = params.get("ght") or {}
    ght_str = f"->GHS(D{ght['ght_d']},SP{ght['ght_sp']})" if ght.get("ght_d") else ""
    # Preferences provenance: record both label and resolved value so a future
    # recalibration of presets doesn't silently change what the log means.
    pref_str = ""
    if params.get("_pref_provenance"):
        pref_str = "; prefs=" + ",".join(f"{k}:{v}" for k, v in params["_pref_provenance"].items())
    if params.get("_recipe_provenance"):
        pref_str += "; recipe-pins=" + ",".join(f"{k}:{v}" for k, v in params["_recipe_provenance"].items())
    cert_str = "" if certification == "certified" else f"; certification={certification}"
    # Diagnosis 4: when a PCC attempt was judged (accepted or fell back to native),
    # stamp the provenance so the EXIF agrees with plan/notes on the shipped path.
    pcc_prov = f"; pcc={params['_pcc_provenance']}" if params.get("_pcc_provenance") else ""
    user_comment = (
        f"crop->BGE({params['bge_smoothing']},{params['bge_correction']})"
        f"->denoise({params['denoise_strength']})"
        f"->asinh({params['asinh_d']},bp{params['blackpoint']})"
        f"->mtf({params['mtf_mid']}){ght_str}->satu({params['saturation']})"
        f"{'+PCC' if params['pcc'] else '; no PCC'}{pcc_prov}"
        f"; profile={params['profile']}; variant={variant}{pref_str}{cert_str}"
    )
    keywords = tuple(
        kw for kw in (tgt, common, cls["category"], m.get("filter") or "", scope, f"profile:{params['profile']}") if kw
    )
    datetime_original = m["datetime"].replace("-", ":", 2) if m.get("datetime") else None
    artist = defaults.get("artist") or None
    ra_deg = dec_deg = pixscale_arcsec = None
    if h.get("has_wcs"):
        ra_deg, dec_deg = h.get("ra_deg"), h.get("dec_deg")
        pixscale_arcsec = h.get("pixscale_arcsec")
    return MetadataCard(
        title=title,
        object_name=tgt,  # bare target label for IPTC:ObjectName; title may append a common name
        description=description,
        one_liner=one_liner,
        user_comment=user_comment,
        keywords=keywords,
        datetime_original=datetime_original,
        artist=artist,
        ra_deg=ra_deg,
        dec_deg=dec_deg,
        pixscale_arcsec=pixscale_arcsec,
    )


def embed(
    path: str,
    inspection: dict,
    params: dict,
    variant: str,
    cfg: dict,
    tools: ToolPaths,
    *,
    certification: str = "certified",
    common_name: str | None = None,
) -> None:
    card = _build_card(inspection, params, variant, cfg, certification, common_name)
    ref = ArtifactRef(name="embed_target", role=ArtifactRole.DELIVERABLE)
    op = EmbedMetadata(targets=(ref,), card=card, variant=variant)
    ctx = ExecContext(workspace=None, tools=tools, resolve={ref: path})
    ExiftoolAdapter().execute(op, ctx)
