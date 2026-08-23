"""WCS catalog prior — recognition from the SKY, not the LLM, not the OBJECT name.

Every Seestar frame is plate-solved (WCS in the header). A cone search at the
frame's true field center returns the catalogued object and its type. This is the
discriminator the measured **E-gate could not be**: E magnitude ranks a dense
globular ABOVE a faint galaxy (StarNet can't strip a packed core), so no threshold
separates IRCUT galaxy from IRCUT globular. The catalog knows what each object is.

Discipline (CLAUDE.md §13 / ROADMAP §6):
- **Prior proposes; measurement disposes.** The prior sets the category for the one
  ambiguous case (IRCUT galaxy vs star cluster) and flags disagreement; it NEVER
  injects structure into pixels.
- **Authority is scoped.** The LP filter is an unconditional law (dual-band emission
  -> nebula); the catalog may DISAGREE (e.g. M8 resolves to its embedded open cluster
  NGC 6530) but does not override the filter. See `reconcile()`.
- **Recognition from the sky.** Identity comes from the WCS field center via a cone
  search, never from the FITS `OBJECT` keyword (which is an acquisition label we
  treat as untrusted) and never from a vision model (it would hallucinate).
- **Offline-degradable & cached.** Network failure falls back to a local cache, then
  to "unavailable" — the engine keeps its measured default. A run never crashes here.

Stdlib only (urllib + csv): no astroquery dependency, matching the thin-wrapper
engine architecture.
"""

from __future__ import annotations

import csv
import io
import json
import os
import urllib.parse
import urllib.request
import warnings

# --- otype -> engine-category families -------------------------------------------
# SIMBAD short otype codes. We map to the engine's three content categories. The key
# decision this module exists for is galaxy vs star cluster on IRCUT; nebula is mostly
# a cross-check (the LP filter already forces nebula in reconcile()).
#
# Gotchas baked in from the de-risk:
#   - Cen A's primary otype is "BLL" (its blazar nucleus), not a galaxy code -> the
#     active-galactic-nucleus codes MUST live in the galaxy family.
#   - "ClG" is a cluster of GALAXIES (galaxy family), NOT a star cluster.
#   - "Cl*"/"GlC"/"OpC" are star clusters -> starfield (the cluster IS the subject).

_GALAXY_CODES = {
    "G",
    "GiG",
    "GiC",
    "GiP",
    "IG",
    "PaG",
    "GrG",
    "ClG",
    "SCG",
    "EmG",
    "SBG",
    "H2G",
    "bCG",
    "LSB",
    "rG",
    "AGN",
    "SyG",
    "Sy1",
    "Sy2",
    "Sy?",
    "LIN",
    "QSO",
    "Q?",
    "Bla",
    "BLL",
    "BiC",
    "LeG",
    "LeI",
    "LeQ",
    "PoG",
    "gG",
    "AG?",
    "rG?",
    "G?",
}
_CLUSTER_CODES = {
    "GlC",
    "OpC",
    "Cl*",
    "GlC?",
    "OpC?",
    "Cl?",
    "As*",
    "C?*",
    "*Cl",
    "GlCl",
}
_NEBULA_CODES = {
    "HII",
    "EmN",
    "RNe",
    "PN",
    "PN?",
    "SNR",
    "SNR?",
    "GNe",
    "DNe",
    "Cld",
    "ISM",
    "MoC",
    "SFR",
    "HH",
    "bub",
    "sh",
    "glb",
    "cor",
    " refl",
}

# Human labels for the report (only the ones we actually surface need to be pretty).
_OTYPE_LABEL = {
    "GlC": "globular cluster",
    "OpC": "open cluster",
    "Cl*": "star cluster",
    "G": "galaxy",
    "BLL": "BL Lac (galaxy)",
    "AGN": "active galaxy nucleus",
    "rG": "radio galaxy",
    "Sy2": "Seyfert galaxy",
    "HII": "HII region",
    "EmN": "emission nebula",
    "RNe": "reflection nebula",
    "PN": "planetary nebula",
    "SNR": "supernova remnant",
    "PoG": "part of a galaxy",
}


def classify_otype(otype: str, otype_txt: str | None = None) -> str | None:
    """Map a SIMBAD otype code to nebula | galaxy | starfield, or None if it is not a
    deep-sky object we recognise (a star, an X-ray/radio source, etc.)."""
    code = (otype or "").strip()
    if code in _GALAXY_CODES:
        return "galaxy"
    if code in _CLUSTER_CODES:
        return "starfield"
    if code in _NEBULA_CODES:
        return "nebula"
    # Substring fallback on the (sometimes long) text form, for codes not enumerated.
    txt = (otype_txt or code or "").lower()
    if "cluster of galaxies" in txt or "galaxy" in txt or "galax" in txt:
        return "galaxy"
    if "globular" in txt or "open cluster" in txt or "cluster of stars" in txt:
        return "starfield"
    if any(k in txt for k in ("nebula", "h ii", "hii region", "supernova remnant", "planetary")):
        return "nebula"
    return None


def otype_label(otype: str) -> str:
    return _OTYPE_LABEL.get((otype or "").strip(), (otype or "").strip())


# --- field center from full WCS (not CRVAL) --------------------------------------


def field_center(fits_path: str) -> tuple[float, float] | None:
    """True (RA, Dec) of the image center from the full WCS. CRVAL alone is the
    plate-solve reference pixel and can sit ~0.3 deg off the framed target; the
    Seestar centers the subject, so pixel_to_world at the image center is the target.
    Returns None if there is no usable WCS."""
    from astropy.io import fits
    from astropy.wcs import WCS

    try:
        with fits.open(fits_path) as hdul:
            hdr = hdul[0].header
            n1, n2 = hdr.get("NAXIS1"), hdr.get("NAXIS2")
            if not (n1 and n2):
                return None
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                w = WCS(hdr, naxis=2)  # naxis=2: the FITS is a 3D color cube
                sky = w.pixel_to_world((n1 + 1) / 2.0, (n2 + 1) / 2.0)
            return float(sky.ra.deg), float(sky.dec.deg)
    except Exception:
        # Fallback: CRVAL (better than nothing; the cone radius tolerates the offset).
        try:
            with fits.open(fits_path) as hdul:
                hdr = hdul[0].header
                if "CRVAL1" in hdr and "CRVAL2" in hdr:
                    return float(hdr["CRVAL1"]), float(hdr["CRVAL2"])
        except Exception:
            pass
    return None


# --- SIMBAD cone search (stdlib) -------------------------------------------------

_TAP_DEFAULT = "https://simbad.u-strasbg.fr/simbad/sim-tap/sync"


def query_simbad(
    ra: float,
    dec: float,
    radius_deg: float = 0.2,
    timeout_s: float = 15.0,
    tap_url: str = _TAP_DEFAULT,
    top: int = 30,
    sized_only: bool = True,
) -> list[dict]:
    """Cone search the SIMBAD `basic` table for the framed subject. The subject
    (galaxy/globular) is a large catalogued object, but it is queried at the FIELD
    CENTER -- offset ~1-2' from its catalog centroid, so dozens of member stars rank
    closer by distance. So `sized_only` (default) restricts to objects WITH a
    catalogued angular size and orders by size DESC: the member stars (no size) are
    excluded entirely and the dominant subject is row 1, immune to truncation. The
    caller retries with sized_only=False (nearest-first) only if that finds no deep-sky
    object -- e.g. a bare open cluster with no catalogued galdim. Raises on network
    error (the caller degrades to cache / unavailable). Each row: main_id, otype,
    otype_txt, ra, dec, galdim_majaxis, galdim_minaxis, dist (deg). (ADQL has no
    NULLS LAST, hence the IS NOT NULL filter rather than a null-tolerant sort.)"""
    where = f"CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', {ra}, {dec}, {radius_deg})) = 1"
    if sized_only:
        where += " AND galdim_majaxis IS NOT NULL"
        order = "galdim_majaxis DESC"
    else:
        order = "dist ASC"
    adql = (
        f"SELECT TOP {int(top)} main_id, otype, otype_txt, ra, dec, "
        f"galdim_majaxis, galdim_minaxis, "
        f"DISTANCE(POINT('ICRS', ra, dec), POINT('ICRS', {ra}, {dec})) AS dist "
        f"FROM basic WHERE {where} ORDER BY {order}"
    )
    data = urllib.parse.urlencode(
        {
            "request": "doQuery",
            "lang": "adql",
            "format": "csv",
            "query": adql,
        }
    ).encode()
    import ssl

    # One context that works on every machine we've met (generalization law):
    # the SYSTEM store (Windows: CryptoAPI -- carries any AV/proxy TLS-interception
    # root the machine's browsers trust) PLUS certifi's bundle (macOS/venvs without
    # a usable system store). Python 3.13+ enables VERIFY_X509_STRICT by default,
    # and interception roots commonly violate the strict profile ("Basic
    # Constraints of CA cert not marked critical") -- relax ONLY that profile bit;
    # chain verification itself stays on. (The M100 baseline capture failed
    # against both certifi-only and strict-system contexts, 2026-07.)
    ctx = ssl.create_default_context()
    try:
        import certifi

        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    req = urllib.request.Request(tap_url, data=data, headers={"User-Agent": "photon-finish/2.0"})
    with urllib.request.urlopen(req, timeout=timeout_s, context=ctx) as r:
        text = r.read().decode("utf-8", "replace")
    return parse_simbad_csv(text)


def parse_simbad_csv(text: str) -> list[dict]:
    """Parse a SIMBAD TAP CSV response into row dicts (network-free; unit-testable)."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    header = [h.strip() for h in rows[0]]

    def fnum(v):
        v = (v or "").strip()
        try:
            return float(v)
        except ValueError:
            return None

    out = []
    for raw in rows[1:]:
        if not raw or len(raw) < len(header):
            continue
        d = dict(zip(header, raw, strict=False))
        out.append(
            {
                "main_id": (d.get("main_id") or "").strip(),
                "otype": (d.get("otype") or "").strip(),
                "otype_txt": (d.get("otype_txt") or "").strip(),
                "ra": fnum(d.get("ra")),
                "dec": fnum(d.get("dec")),
                "galdim_majaxis": fnum(d.get("galdim_majaxis")),
                "galdim_minaxis": fnum(d.get("galdim_minaxis")),
                "dist": fnum(d.get("dist")),
            }
        )
    return out


def dominant_object(
    candidates: list[dict], fov_long_arcmin: float | None = None, max_fov_frac: float = 1.1
) -> dict | None:
    """The framed subject among the cone hits. Of the candidates that classify as a
    deep-sky object (galaxy / cluster / nebula -- foreground stars and X-ray/radio
    point sources are skipped), pick the one with the LARGEST angular size; that is the
    dominant subject the Seestar was pointed at. Two guards:
      - A framed subject cannot be LARGER than the field of view, so when `fov_long_arcmin`
        is known, reject catalogued sizes above `max_fov_frac` x FOV (rejects spurious
        oversized halo/association entries -- e.g. a bogus 184' "open cluster" sitting on
        top of M5 that would otherwise outrank the real 17' cluster).
      - Fall back to nearest-to-center when no candidate carries a size (e.g. a bare open
        cluster with no catalogued galdim).
    Order-independent."""
    dso = [
        {**c, "catalog_category": cat}
        for c in candidates
        if (cat := classify_otype(c.get("otype", ""), c.get("otype_txt")))
    ]
    if not dso:
        return None
    sized = [c for c in dso if c.get("galdim_majaxis")]
    if fov_long_arcmin:
        sized = [c for c in sized if c["galdim_majaxis"] <= fov_long_arcmin * max_fov_frac]
    if sized:
        return max(sized, key=lambda c: c["galdim_majaxis"])
    return min(dso, key=lambda c: (c.get("dist") if c.get("dist") is not None else 1e9))


# --- cache -----------------------------------------------------------------------


def _cache_path(cache_dir: str, ra: float, dec: float, radius_deg: float) -> str:
    d = os.path.expanduser(cache_dir)
    key = f"{ra:.4f}_{dec:.4f}_r{radius_deg:g}.json"
    return os.path.join(d, key)


def _cache_read(path: str) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _cache_write(path: str, payload: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(payload, f)
    except OSError:
        pass  # best-effort: network-prior cache write, not manifest-referenced; a miss just re-fetches next time (CHANGE_SAFETY rule 5)


# --- top-level: build the prior for a file ---------------------------------------


def prior_for(fits_path: str, header: dict, cfg: dict) -> dict:
    """Resolve the catalog prior for one frame. Always returns a dict (never raises);
    `available` is False when there is no WCS or the lookup could not be satisfied
    live or from cache. `source` is one of: live | cache | offline | no-wcs."""
    pcfg = cfg.get("prior", {}) if cfg else {}
    radius = float(pcfg.get("radius_deg", 0.12))
    timeout = float(pcfg.get("timeout_s", 15.0))
    tap_url = pcfg.get("tap_url", _TAP_DEFAULT)
    cache_dir = pcfg.get("cache_dir", "~/.cache/seestar/prior")

    base = {
        "enabled": True,
        "available": False,
        "source": None,
        "field_center": None,
        "object": None,
        "otype": None,
        "otype_label": None,
        "catalog_category": None,
        "angular_size_arcmin": None,
        "fov_fraction": None,
        "n_candidates": 0,
    }

    center = field_center(fits_path)
    if center is None:
        return {**base, "available": False, "source": "no-wcs"}
    ra, dec = center
    base["field_center"] = [round(ra, 5), round(dec, 5)]

    fov_long = _fov_long_arcmin(header)
    cpath = _cache_path(cache_dir, ra, dec, radius)
    candidates, source, dom = None, None, None
    try:
        # Sized-first (subject by angular size); fall back to nearest-DSO only if that
        # finds nothing with a catalogued size (e.g. a bare open cluster).
        candidates = query_simbad(ra, dec, radius_deg=radius, timeout_s=timeout, tap_url=tap_url, sized_only=True)
        dom = dominant_object(candidates, fov_long_arcmin=fov_long)
        if dom is None:
            nearest = query_simbad(ra, dec, radius_deg=radius, timeout_s=timeout, tap_url=tap_url, sized_only=False)
            candidates = candidates + nearest
            dom = dominant_object(nearest, fov_long_arcmin=fov_long)
        source = "live"
        _cache_write(cpath, {"center": center, "radius": radius, "candidates": candidates})
    except Exception as e:
        cached = _cache_read(cpath)
        if cached and "candidates" in cached:
            candidates, source = cached["candidates"], "cache"
            dom = dominant_object(candidates, fov_long_arcmin=fov_long)
        else:
            # carry the actual error: "offline" hid an SSL failure for a whole
            # session (M100 misclassified starfield -> wrong treatment chain)
            return {**base, "available": False, "source": "offline", "error": f"{type(e).__name__}: {str(e)[:120]}"}

    base["n_candidates"] = len(candidates)
    if dom is None:
        # Reached the catalog but found no deep-sky object near center -> a genuine
        # signal (mispointing / empty field / star-only frame). Report it.
        return {**base, "available": True, "source": source, "catalog_category": None, "object": None}

    size = dom.get("galdim_majaxis")
    fov_frac = _fov_fraction(size, header)
    return {
        **base,
        "available": True,
        "source": source,
        "object": dom["main_id"],
        "otype": dom["otype"],
        "otype_label": otype_label(dom["otype"]),
        "catalog_category": dom["catalog_category"],
        "angular_size_arcmin": size,
        "fov_fraction": fov_frac,
    }


def _fov_long_arcmin(header: dict):
    """FOV long axis in arcmin from the WCS pixel scale and image dimensions."""
    if not header:
        return None
    px = header.get("pixscale_arcsec")
    n1, n2 = header.get("NAXIS1"), header.get("NAXIS2")
    if not (px and n1 and n2):
        return None
    fov = max(n1, n2) * px / 60.0
    return fov if fov > 0 else None


# Any of these header cards, if present, define SOME pixel-scale transform (CD
# matrix, PC+CDELT, or bare CDELT). Their total absence means astropy's WCS would
# silently fall back to a degenerate 1 deg/pixel identity scale (a real but useless
# WCS object -- has_celestial is still True) rather than raising, so this is checked
# explicitly before trusting `proj_plane_pixel_scales`.
_WCS_SCALE_KEYS = (
    "CD1_1",
    "CD1_2",
    "CD2_1",
    "CD2_2",
    "PC1_1",
    "PC1_2",
    "PC2_1",
    "PC2_2",
    "CDELT1",
    "CDELT2",
)


def _fov_long_arcmin_from_wcs(fits_path: str):
    """FOV long axis in arcmin, derived from the FULL WCS (CD matrix, PC+CDELT, or
    bare CDELT convention -- whichever the header actually carries), not from the
    header's convenience `pixscale_arcsec` field.

    `inspect.read_header` only populates `pixscale_arcsec` when the header has an
    explicit CD1_1/CD1_2 matrix (CLAUDE.md's Seestar convention); a header
    plate-solved into a PC+CDELT (or bare CDELT) convention instead has a perfectly
    usable WCS but no `pixscale_arcsec`, and `_fov_long_arcmin` above returns None
    for it. This is the fallback for exactly that case: mirrors `field_center`'s
    open-and-build-WCS pattern, then uses `astropy.wcs.utils.proj_plane_pixel_scales`
    (correct for CD, PC, and CDELT conventions alike) to get the pixel scale, and
    multiplies by the LONG axis's own NAXIS to get the long-axis FOV in arcmin.

    Returns None if there is genuinely no usable WCS -- a missing/mismatched
    NAXIS1/NAXIS2, no CD/PC/CDELT convention present at all (see `_WCS_SCALE_KEYS`),
    a WCS that fails to construct, or a non-celestial WCS -- never raises.
    """
    from astropy.io import fits
    from astropy.wcs import WCS
    from astropy.wcs.utils import proj_plane_pixel_scales

    try:
        with fits.open(fits_path) as hdul:
            hdr = hdul[0].header
            n1, n2 = hdr.get("NAXIS1"), hdr.get("NAXIS2")
            if not (n1 and n2):
                return None
            if not any(k in hdr for k in _WCS_SCALE_KEYS):
                return None
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                w = WCS(hdr, naxis=2)
                if not w.has_celestial:
                    return None
                scales_deg = proj_plane_pixel_scales(w)
        if scales_deg is None or len(scales_deg) < 2:
            return None
        # match each axis's own pixel scale, not an average -- the long axis picks
        # the scale computed for that same axis.
        scale_deg = scales_deg[0] if n1 >= n2 else scales_deg[1]
        scale_arcsec = float(scale_deg) * 3600.0
        if scale_arcsec <= 0:
            return None
        fov = max(n1, n2) * scale_arcsec / 60.0
        return fov if fov > 0 else None
    except Exception:
        return None


def _fov_fraction(size_arcmin, header: dict):
    """Subject angular size as a fraction of the FOV long axis -> a quantitative input
    to the closeup decision (galaxies only; informational here, not acted on)."""
    fov = _fov_long_arcmin(header)
    if not size_arcmin or not fov:
        return None
    return round(size_arcmin / fov, 3)


# --- reconcile prior with the filter law + measured category ---------------------


def reconcile(filt: str | None, measured_category: str, prior: dict | None) -> tuple:
    """Decide the final category, honouring the authority order:
        filter law (LP -> nebula, unconditional)  >  catalog (IRCUT galaxy/starfield)
        > measured default.
    Returns (final_category, note). `note['used']` is True only when the catalog
    actually changed/confirmed the category. Always records disagreement for the log
    (prior proposes; measurement/filter disposes)."""
    if not prior or not prior.get("available") or not prior.get("catalog_category"):
        return measured_category, {
            "used": False,
            "reason": (prior or {}).get("source") or "no-prior",
        }
    cat = prior["catalog_category"]
    obj, ot = prior.get("object"), prior.get("otype")

    if filt == "LP":
        # Unconditional instrument law: dual-band -> emission nebula. The catalog may
        # legitimately disagree (M8 -> embedded open cluster NGC 6530); record it.
        return measured_category, {
            "used": False,
            "authority": "filter-law (LP->nebula)",
            "catalog_category": cat,
            "catalog_object": obj,
            "catalog_otype": ot,
            "disagreement": cat != "nebula",
            "message": (
                f"catalog '{obj}' otype={ot} -> {cat}; LP filter law keeps category=nebula"
                if cat != "nebula"
                else f"catalog confirms nebula ('{obj}')"
            ),
        }

    # IRCUT (broadband): this is exactly the case measurement can't settle.
    if cat in ("galaxy", "starfield"):
        changed = cat != measured_category
        return cat, {
            "used": True,
            "authority": "catalog",
            "from": measured_category,
            "to": cat,
            "changed": changed,
            "catalog_object": obj,
            "catalog_otype": ot,
            "message": (
                f"catalog '{obj}' otype={ot} -> category={cat}" + (f" (was {measured_category})" if changed else "")
            ),
        }
    # IRCUT but the catalog says nebula (unusual for a broadband pointing) -> trust the
    # catalog enough to flag it, but don't silently flip; keep measured + warn.
    return measured_category, {
        "used": False,
        "authority": "measured (catalog said nebula on IRCUT)",
        "catalog_category": cat,
        "catalog_object": obj,
        "catalog_otype": ot,
        "disagreement": True,
        "message": f"IRCUT but catalog '{obj}' otype={ot} -> nebula; left as {measured_category}, review",
    }
