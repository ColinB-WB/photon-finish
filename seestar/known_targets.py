"""Local, offline identity + category prior catalog (SHELL layer).

Reads the bundled `seestar/data/known_targets.json` package resource and exposes it as
a tuple of `LocalTargetRecord`. Per CLAUDE.md sec0 this module is SHELL-side (not
`seestar.core`) and may import `seestar.core.*` only.

**v1 inertness (spec sec5.4):** the rich record here (footprint, morphology,
associations) is loaded, validated, and reported, but nothing in v1 wires it into a
decision path. Only `category` is ever reduced into the core-facing observation the
pure core is allowed to see — this module intentionally does NOT do that reduction;
that belongs to the consumer built in a later task (`prior.py`/`orchestrate.py`).

**Never raises (spec sec5.7):** a missing or corrupt bundled catalog resource must not
break a render. `load_catalog()` degrades to an empty catalog and logs a
`local_catalog_unavailable` note; callers fall back to the existing network/measured
path.

See docs/history/superpowers/specs/2026-07-25-known-targets-prototype-design.md for the full
design.
"""

from __future__ import annotations

import importlib.resources
import json
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass

from seestar.core.types import Category, PriorObservation

logger = logging.getLogger(__name__)

_DEFAULT_RESOURCE = "known_targets.json"

_FAMILY_TO_CATEGORY: dict[str, Category] = {
    "nebula": Category.NEBULA,
    "galaxy": Category.GALAXY,
    "starfield": Category.STARFIELD,
    "unknown": Category.UNKNOWN,
}


@dataclass(frozen=True)
class LocalTargetRecord:
    """One catalog entry: identity + category, plus (v1-inert) footprint/morphology.

    Deliberately lives shell-side, not in `seestar.core`: the pure core must never be
    able to see (let alone act on) `search_footprints`/`morphology_traits`/
    `associated` — see spec sec5.4. Everything except `category` here is
    loaded/validated/reported only.

    `lookup_role` is `"primary"` (a genuine top-level lookup target, e.g. M8) or
    `"associated_only"` (relational metadata that stays loaded/reachable by id and
    reportable as association metadata, but must never independently make a frame
    `ambiguous` — see `match_target`, e.g. M8's embedded cluster NGC6530).
    """

    entry_id: str
    canonical_id: str
    common_name: str | None
    aliases: tuple[str, ...]
    equivalent_ids: tuple[str, ...]
    category: Category
    ra_deg: float
    dec_deg: float
    extent_major_arcmin: float | None
    extent_minor_arcmin: float | None
    search_footprints: tuple[dict, ...]
    morphology_traits: tuple[str, ...]
    associated: tuple[dict, ...]
    provenance: dict
    lookup_role: str  # "primary" | "associated_only" -- see match_target's candidacy filter


def _read_catalog_text(name: str) -> str | None:
    """Read the raw text of a bundled catalog resource, or None on any read failure.

    Isolated from `load_catalog` so tests can monkeypatch this one function to
    simulate a missing, permission-denied, or corrupt bundled resource without
    touching the real installed package data (see tests/test_known_targets_load.py).
    """
    try:
        return importlib.resources.files("seestar.data").joinpath(name).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # OSError subsumes FileNotFoundError/PermissionError/IsADirectoryError (a
        # locked/quarantined/missing bundled resource, e.g. on the notarized Mac app
        # or Windows run-from-source); UnicodeDecodeError covers a non-UTF-8 resource.
        # Never raise (spec sec5.7) — degrade to "no local record" like a missing file.
        return None


def _record_from_dict(raw: dict) -> LocalTargetRecord:
    family = raw["classification"]["family"]
    category = _FAMILY_TO_CATEGORY.get(family, Category.UNKNOWN)
    extent_major = raw.get("extent_major_arcmin")
    extent_minor = raw.get("extent_minor_arcmin")
    return LocalTargetRecord(
        entry_id=raw["entry_id"],
        canonical_id=raw["canonical_id"],
        common_name=raw.get("common_name"),
        aliases=tuple(raw.get("aliases", ())),
        equivalent_ids=tuple(raw.get("equivalent_ids", ())),
        category=category,
        ra_deg=float(raw["ra_deg"]),
        dec_deg=float(raw["dec_deg"]),
        extent_major_arcmin=float(extent_major) if extent_major is not None else None,
        extent_minor_arcmin=float(extent_minor) if extent_minor is not None else None,
        search_footprints=tuple(raw.get("search_footprints", ())),
        morphology_traits=tuple(raw.get("morphology_traits", ())),
        associated=tuple(raw.get("associated", ())),
        provenance=dict(raw.get("provenance", {})),
        # Load-time is lenient (spec sec5.7's "never raise"): a missing lookup_role
        # defaults to "primary" rather than raising. Build-time (tools/
        # build_known_targets.py) is where the value is validated strictly against
        # the closed {"primary", "associated_only"} set -- see that module.
        lookup_role=raw.get("lookup_role", "primary"),
    )


def load_catalog(resource: str = _DEFAULT_RESOURCE) -> tuple[LocalTargetRecord, ...]:
    """Load the local known-targets catalog. Never raises (spec sec5.7).

    A missing or corrupt bundled resource degrades to an empty catalog — callers must
    treat that exactly like "no local record" and fall back to the network prior (IRCUT)
    or the generic measured path (LP), never crash a render.
    """
    try:
        text = _read_catalog_text(resource)
        if text is None:
            logger.warning("local_catalog_unavailable resource=%s reason=missing", resource)
            return ()
        payload = json.loads(text)
        records_raw = payload["records"]
        return tuple(_record_from_dict(raw) for raw in records_raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        # Defense in depth: `_read_catalog_text` already degrades OSError/
        # UnicodeDecodeError to None internally, but this call is wrapped too so a
        # caller-swapped/monkeypatched reader that raises directly still can't crash
        # a render (spec sec5.7 — MUST NEVER raise).
        logger.warning("local_catalog_unavailable resource=%s reason=%s", resource, exc)
        return ()


# --- WCS footprint-overlap matching + conflict classification (Task 2) -----------
#
# Pure geometry, no I/O: the shell (a later task, prior.py/orchestrate.py) is
# responsible for computing `frame_center`/`fov_arcmin` from the FITS WCS
# (`prior.field_center`/`prior._fov_long_arcmin`) and passing them in here.
#
# Matching is by IMAGE-FOOTPRINT OVERLAP, never centroid-to-frame-center distance
# alone (spec sec5.1/sec5.5, sec11): a catalog entry's angular extent is treated as a
# circle of radius `extent_major_arcmin / 2` around its centroid, the image's own
# footprint is treated as a circle of radius `fov_arcmin / 2` (or the caller-supplied
# `image_footprint` override, e.g. a more precise rectangular-diagonal radius) around
# `frame_center`, and the two circles are tested for overlap. This is what makes a
# valid OFF-CENTER composition -- a large object's centroid far from frame center but
# whose edge still falls in frame -- register as a hit (see
# `test_match_target_off_center_composition_still_hits`): a centroid-distance-only
# test would wrongly reject it.

# Minimum image-footprint overlap fraction for a candidate catalog entry to count as
# a spatial `hit` rather than `spatially_rejected`. The fraction is the realized
# overlap distance as a share of the two circles' COMBINED radius (catalog extent
# radius + image footprint radius): 0.0 means the circles are only just touching at
# the edge (no real overlap), 1.0 means concentric.
#
# Geometric first-principles choice (not tuned to any benchmark, per spec sec11's
# pre-registration requirement): a solved WCS field center
# (`prior.field_center`, full `pixel_to_world`, not the CRVAL fallback) is accurate
# to well under an arcmin, and combined radii for an S30 frame (~65' FOV radius) plus
# a catalogued deep-sky extent run from a few arcmin to well over a hundred -- so 5%
# of the combined radius is comfortably larger than realistic WCS/extent measurement
# noise (guards against a spurious hit from two circles that only just graze at the
# edge), while staying far short of requiring near-concentricity, so a genuine
# off-center composition (extent-driven overlap, small realized fraction) still
# registers as a hit.
#
# frozen 2026-07-25; pre-registration — do NOT tune after seeing Task-10 benchmark
# results.
MIN_OVERLAP_FRAC = 0.05


@dataclass(frozen=True)
class MatchResult:
    """Outcome of matching a frame's WCS footprint against the local catalog.

    `record` is the single matched entry on `status == "hit"`; `None` for every other
    status (there is no single entry to report on a miss/ambiguous/rejection).
    """

    record: LocalTargetRecord | None
    status: str  # "hit" | "miss" | "ambiguous" | "spatially_rejected"


def _angular_separation_arcmin(ra1_deg: float, dec1_deg: float, ra2_deg: float, dec2_deg: float) -> float:
    """Great-circle (haversine) separation between two RA/Dec points, in arcmin."""
    ra1, dec1, ra2, dec2 = (math.radians(v) for v in (ra1_deg, dec1_deg, ra2_deg, dec2_deg))
    dra = ra2 - ra1
    ddec = dec2 - dec1
    a = math.sin(ddec / 2.0) ** 2 + math.cos(dec1) * math.cos(dec2) * math.sin(dra / 2.0) ** 2
    sep_rad = 2.0 * math.asin(min(1.0, math.sqrt(max(0.0, a))))
    return math.degrees(sep_rad) * 60.0


def _fov_radius_arcmin(fov_arcmin: float, image_footprint: float | None) -> float:
    """The image's own circular footprint radius, in arcmin.

    `image_footprint`, when given (a positive value), is the caller's own -- possibly
    more precise, e.g. rectangular-diagonal-based -- footprint radius and takes
    precedence; otherwise fall back to the simple circular approximation
    `fov_arcmin / 2` (half the long-axis FOV, `prior._fov_long_arcmin`'s definition).
    """
    if image_footprint is not None and image_footprint > 0:
        return float(image_footprint)
    return max(0.0, fov_arcmin) / 2.0


def _overlap_fraction(
    record: LocalTargetRecord,
    frame_center: tuple[float, float],
    fov_arcmin: float,
    image_footprint: float | None,
) -> float:
    """Fraction (0..1) of the catalog-extent/image-FOV circles' combined radius that
    is realized overlap. 0 when the circles do not overlap at all."""
    catalog_radius = (record.extent_major_arcmin or 0.0) / 2.0
    fov_radius = _fov_radius_arcmin(fov_arcmin, image_footprint)
    combined_radius = catalog_radius + fov_radius
    if combined_radius <= 0.0:
        return 0.0
    separation = _angular_separation_arcmin(record.ra_deg, record.dec_deg, frame_center[0], frame_center[1])
    overlap = combined_radius - separation
    if overlap <= 0.0:
        return 0.0
    return min(1.0, overlap / combined_radius)


def match_target(
    records: Sequence[LocalTargetRecord],
    frame_center: tuple[float, float],
    fov_arcmin: float,
    image_footprint: float | None,
) -> MatchResult:
    """Match a frame's WCS footprint against candidate local catalog records.

    `records` is whatever candidate set the caller narrowed down (e.g. a coarse
    RA/Dec box query over the loaded catalog); this function does the overlap
    geometry and the hit/miss/ambiguous/spatially_rejected classification only --
    it does not itself search the full catalog.

    **Candidacy is `lookup_role == "primary"` only** (explicit schema, not verb/
    extent inference -- an `associated_only` record, e.g. M8's embedded cluster
    NGC6530, stays loaded and reachable by id/association metadata, but can never
    independently make a frame `ambiguous` or be returned as a `hit` here):

    - no PRIMARY records at all (`records` empty, or every record is
      `associated_only`) -> "miss" (nothing matchable; there was no candidate).
    - one or more primary records, none clearing `MIN_OVERLAP_FRAC` ->
      "spatially_rejected" (a candidate existed but its footprint cannot plausibly
      overlap the image).
    - exactly one primary record clears the threshold -> "hit".
    - more than one primary record clears the threshold -> "ambiguous" (no record
      returned; the caller cannot safely pick one -- genuinely different intended
      targets, unchanged by this filter).
    """
    primaries = [r for r in records if r.lookup_role == "primary"]
    if not primaries:
        return MatchResult(record=None, status="miss")
    hits = [r for r in primaries if _overlap_fraction(r, frame_center, fov_arcmin, image_footprint) >= MIN_OVERLAP_FRAC]
    if not hits:
        return MatchResult(record=None, status="spatially_rejected")
    if len(hits) > 1:
        return MatchResult(record=None, status="ambiguous")
    return MatchResult(record=hits[0], status="hit")


def classify_conflict(record: LocalTargetRecord, measured_category: Category) -> str:
    """Classify a catalog record's identity against the pipeline's own measurement.

    Only `ra_deg`/`dec_deg` (position) and `category` are consulted -- never
    `search_footprints`/`morphology_traits`/`associated` (spec sec5.4 inertness: the
    rich record must not drive a decision beyond identity/category in v1).

    - "spatially_impossible": the record's own stored position is outside the
      physically valid celestial range (declination beyond +/-90 deg, or right
      ascension outside [0, 360)) -- it cannot correspond to any real sky position,
      so it is impossible independent of any particular frame. (A record that is merely
      *far from this frame* is handled earlier, by `match_target`'s
      `spatially_rejected` status -- that check needs the frame's WCS, which this
      function deliberately does not take, per the frozen 2-argument interface.)
    - "type_disagreement": the position is plausible, but the catalog's category
      disagrees with what the pipeline measured (e.g. catalog says galaxy, the frame
      measured nebula) -- identity is kept, the disputed hint is dropped upstream.
    - "none": no conflict.
    """
    if not (-90.0 <= record.dec_deg <= 90.0) or not (0.0 <= record.ra_deg < 360.0):
        return "spatially_impossible"
    if record.category != measured_category:
        return "type_disagreement"
    return "none"


def to_observation(record: LocalTargetRecord) -> PriorObservation:
    """Reduce a rich local catalog record to the core-facing `PriorObservation`.

    This is the ONE reduction point where a local catalog hit is allowed to cross the
    core boundary (spec sec5.4): only `category` is carried across -- identity,
    aliases, extent, footprint, morphology, and associations all stay shell-side,
    exactly like the pure core's `PriorObservation` type already restricts the
    network path. Category *authority* still belongs to `core.planning`'s
    `_reconcile()`, not to this function or to any shell-side gate (spec sec5.5) --
    `to_observation` never withholds or adjusts `category` based on a measured-vs-
    catalog conflict; that adjudication is `reconcile()`'s job alone.

    `fov_fraction` is `None`: unlike the network path (which derives it from a
    catalogued angular size vs. the image FOV), the local match doesn't compute one in
    v1 -- `None` here means "no opinion", matching `PriorObservation.fov_fraction`'s
    existing meaning.
    """
    return PriorObservation(catalog_category=record.category, fov_fraction=None)
