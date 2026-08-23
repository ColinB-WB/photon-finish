"""Inspect one Seestar FITS file and derive content metadata.

Parses the Seestar filename, reads the FITS header (astropy), computes per-channel
statistics (numpy / astropy sigma-clipping), and derives a content category and an
SNR class that drive downstream processing.

The extended-signal fraction E (StarNet-based) and an accurate star count are
computed during the run where StarNet/Siril are already invoked; `classify()`
accepts them as optional refinements.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

from astropy.io import fits

from .core.classify import classify as _core_classify
from .core.types import ChannelStats, ClassifyThresholds, FilterKind, Measurements

# Stacked_<frames>_<target>_<exp>s_<filter>_<YYYYMMDD-HHMMSS>.fit   (target may contain spaces)
_FNAME = re.compile(
    r"Stacked_(?P<frames>\d+)_(?P<target>.+?)_(?P<exp>[\d.]+)s_"
    r"(?P<filter>[A-Za-z]+)_(?P<date>\d{8}-\d{6})",
    re.IGNORECASE,
)

_TRAILING_PAREN = re.compile(r"\s*\([^)]*\)\s*$")
_FS_UNSAFE = re.compile(r'[\\/:*?"<>|]')


def sanitize_label(name: str | None) -> str | None:
    """Filesystem-safe target label.

    This is I/O safety, not content identity: the label
    becomes folder/file names but is never a processing input, so there is NO
    catalog-alias canonicalization). Strips whitespace, a trailing parenthetical,
    and filesystem-unsafe characters; PRESERVES case. Empty/None -> None."""
    if not name:
        return None
    s = _TRAILING_PAREN.sub("", str(name))
    s = _FS_UNSAFE.sub("", s)
    s = re.sub(r"\s+", "", s)
    s = s.lstrip(".")  # a label must never begin a dot segment (., .., .hidden)
    if s in ("", ".", ".."):  # nothing usable / pure traversal
        return None
    return s


def parse_filename(path: str) -> dict:
    import os

    name = os.path.basename(path)
    m = _FNAME.search(name)
    if not m:
        return {"target": None, "frames": None, "filter": None, "sub_exposure_s": None, "datetime": None}
    g = m.groupdict()
    target = sanitize_label(g["target"])
    d = g["date"]
    dt = f"{d[0:4]}-{d[4:6]}-{d[6:8]} {d[9:11]}:{d[11:13]}:{d[13:15]}"
    return {
        "target": target,
        "frames": int(g["frames"]),
        "filter": g["filter"].upper(),
        "sub_exposure_s": float(g["exp"]),
        "datetime": dt,
    }


def read_header(path: str) -> dict:
    with fits.open(path) as hdul:
        h = hdul[0].header
    out = {
        k: h.get(k)
        for k in (
            "NAXIS1",
            "NAXIS2",
            "NAXIS3",
            "BITPIX",
            "CREATOR",  # instrument identity, e.g. "ZWO Seestar S30" / "ZWO Seestar S30 Pro"
            "FOCALLEN",
            "XPIXSZ",
            "FILTER",
            "DATE-OBS",
            "OBJECT",
            "CTYPE1",
            "CRVAL1",
            "CRVAL2",
            "CD1_1",
            "CD1_2",
            "CD2_1",
            "CD2_2",
            "STACKCNT",
            "EXPOSURE",
            "IMAGETYP",
            "TOTALEXP",
        )
        if k in h
    }
    out["has_wcs"] = "CTYPE1" in h and "CRVAL1" in h
    if out["has_wcs"]:
        out["ra_deg"] = h.get("CRVAL1")
        out["dec_deg"] = h.get("CRVAL2")
        if all(k in h for k in ("CD1_1", "CD1_2")):
            scale = (h["CD1_1"] ** 2 + h["CD1_2"] ** 2) ** 0.5 * 3600.0
            out["pixscale_arcsec"] = round(scale, 3)
    return out


def is_seestar_stack(header: dict) -> tuple[bool, str | None]:
    """Return whether a FITS header describes a Seestar stacked color master.

    The filename is irrelevant. A qualifying master has a Seestar `CREATOR`, a
    3-channel debayered color stack (`NAXIS3 == 3`), and `STACKCNT`. Raw
    sub-frames are 2-D and intentionally do not qualify.
    """
    creator = str(header.get("CREATOR") or "")
    if "seestar" not in creator.lower():
        return False, f"CREATOR is not a Seestar ({creator or 'missing'!r})"
    if header.get("NAXIS3") != 3:
        return False, "not a 3-channel color stack (raw sub-frame or mono)"
    if header.get("STACKCNT") is None:
        return False, "no STACKCNT (not a stacked master)"
    return True, None


def file_meta(path: str, header: dict | None = None) -> dict:
    """Capture metadata, HEADER-FIRST with the filename as per-field fallback
    (content over name). Same shape as parse_filename()."""
    h = header if header is not None else read_header(path)
    fn = parse_filename(path)
    dobs = h.get("DATE-OBS")
    dt = str(dobs)[:19].replace("T", " ") if dobs else fn["datetime"]
    filt = h.get("FILTER")
    exp = h.get("EXPOSURE")
    return {
        "target": sanitize_label(h.get("OBJECT")) or fn["target"],
        "frames": int(h["STACKCNT"]) if h.get("STACKCNT") is not None else fn["frames"],
        "filter": str(filt).strip().upper() if filt else fn["filter"],
        "sub_exposure_s": float(exp) if exp is not None else fn["sub_exposure_s"],
        "datetime": dt,
    }


def measurements_to_stats(m: Measurements) -> dict:
    """Typed Measurements -> public stats dict used by CLI/tests/reporting."""
    per = {
        c.name: {"median": c.median, "sigma": c.sigma, "max": c.max_value, "sat_frac": c.sat_frac} for c in m.channels
    }
    return {
        "per_channel": per,
        "background": m.background,
        "bgnoise": m.bgnoise,
        "max": m.max_value,
        "dynamic_range": m.dynamic_range,
        "dims": list(m.dims),
    }


def stats_to_measurements(stats: dict) -> Measurements:
    """Public stats dict -> typed Measurements."""
    per = tuple(
        ChannelStats(name=n, median=c["median"], sigma=c["sigma"], max_value=c["max"], sat_frac=c["sat_frac"])
        for n, c in (stats.get("per_channel") or {}).items()
    )
    dims = stats.get("dims") or [0, 0]
    return Measurements(
        channels=per,
        background=stats["background"],
        bgnoise=stats["bgnoise"],
        max_value=stats["max"],
        dims=(int(dims[0]), int(dims[1])),
        pixscale_arcsec=None,
        dynamic_range=stats.get("dynamic_range"),
    )


def _thresholds(classify_cfg: dict | None) -> ClassifyThresholds:
    cc = classify_cfg or {}
    return ClassifyThresholds(
        extended_signal_threshold=cc.get("extended_signal_threshold", 0.03),
        noisy_frames_below=cc.get("noisy_frames_below", 45),
        noisy_bgnoise_above=cc.get("noisy_bgnoise_above", 0.0025),
    )


def measure(path: str) -> dict:
    from .measure import measure_fits

    return measurements_to_stats(measure_fits(path))


def classify(
    meta: dict,
    stats: dict,
    E: float | None = None,
    star_count: int | None = None,
    classify_cfg: dict | None = None,
) -> dict:
    filt = meta.get("filter")
    c = _core_classify(
        FilterKind.from_raw(filt),
        meta.get("frames") or 0,
        stats_to_measurements(stats),
        _thresholds(classify_cfg),
        e_value=E,
    )
    return {
        "category": c.category.value,
        "snr_class": c.snr_class.value,
        "pcc": filt == "IRCUT",  # compatibility hint; core policy owns the real decision
        "E": E,
        "star_count": star_count,
        "E_provisional": c.extended_provisional,
    }


@dataclass
class Inspection:
    path: str
    meta: dict = field(default_factory=dict)
    header: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    classification: dict = field(default_factory=dict)


def inspect(path: str, E: float | None = None, star_count: int | None = None, classify_cfg: dict | None = None) -> dict:
    header = read_header(path)
    meta = file_meta(path, header)
    stats = measure(path)
    cls = classify(meta, stats, E=E, star_count=star_count, classify_cfg=classify_cfg)
    total = (meta["frames"] or 0) * (meta["sub_exposure_s"] or 0)
    meta["total_integration_s"] = total
    meta["total_integration_min"] = round(total / 60.0, 1)
    return asdict(Inspection(path=path, meta=meta, header=header, stats=stats, classification=cls))


def main(path: str) -> int:
    print(json.dumps(inspect(path), indent=2))
    return 0
