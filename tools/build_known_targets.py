"""Maintainer-only build tool: pinned OpenNGC snapshot + target_curation.yaml ->
seestar/data/known_targets.json (Task 7).

MAINTAINER TOOLING. This module lives under `tools/`, not `seestar/` -- it does NOT
run at processing time, is not bundled into the app, and does not import
`seestar.core` or any other runtime module. Users never run it; the SHELL app reads
only the generated JSON (docs/history/superpowers/specs/2026-07-25-known-targets-prototype-
design.md sec1).

Contract (spec sec4) -- FAIL-CLOSED, never warn-and-continue:
  - an `annotations` key matching no `selection`-resolved target;
  - a duplicate/degenerate/malformed search footprint;
  - a curation<->output schema-version incompatibility;
  - an unresolved alias/association, or a relationship pointing at an absent object;
  - an invalid `lookup_role` value (must be "primary" or "associated_only");
  - an orphaned `associated_only` record (referenced by no `primary` record's
    `associated[].canonical_id`), or one referenced inconsistently (a primary
    references it, but its own `associated` list doesn't reciprocate).
Every one of these raises `BuildError`. The build validates search footprints and
morphology even though nothing in v1 consumes them, so the data path is proven
end-to-end for D2b without a later rebuild (spec sec4/sec10).

Deterministic (Tenet "Trust requires bounded repeatability", docs/TENETS.md sec7):
canonicalized parsing (fixed-precision float rounding of OpenNGC-derived fields --
ra_deg/dec_deg/extent_*_arcmin) + canonical (sorted-key) JSON serialization for
hashing -> the same two inputs always produce the same `content_hash`, independent
of dict/YAML key order or process hash-seed. Curated `search_footprints` dims are
NOT rounded this way: they're static literals authored directly in
target_curation.yaml (not measured/derived), so they're passed through as
`float(...)`-cast YAML values with no precision reduction -- still deterministic
(same YAML in, same float out), just not part of the fixed-precision-rounding step.

Note on the "unresolved alias" fail-closed clause below: `aliases`/`equivalent_ids`
are plain display strings, not references into this catalog -- there is no
"resolve" step for them, hence no failure mode to test. Only `associated[].
canonical_id` is a real cross-record reference (another selected target's
canonical_id), and that IS validated (see the unresolved-association check in
`build()`).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re

import yaml

# --- schema versions ----------------------------------------------------------

# The curation-file schema version this build tool understands. A `target_curation
# .yaml` whose `schema_version` doesn't match is a fail-closed BuildError, not a
# best-effort parse. v2 introduces bulk Messier/Caldwell `selection` + a
# `resolution.abstain` block + exception-only `annotations`.
CURATION_SCHEMA_VERSION = 2

# The schema_version stamped into the generated seestar/data/known_targets.json
# header -- must match what seestar/known_targets.py's loader expects.
OUTPUT_SCHEMA_VERSION = 1


class BuildError(Exception):
    """Raised for every fail-closed condition in the build contract (spec sec4).
    Never caught/downgraded to a warning inside this module."""


# --- id normalization + the FROZEN entry-id rule --------------------------------


def normalize_id(raw: str) -> str:
    """Normalize an arbitrary id form (curation selection/annotation key, or an
    OpenNGC `Name` column value) to a comparable key: lowercase, punctuation and
    whitespace stripped. "NGC7000" / "ngc:7000" / "NGC 7000" all normalize equal.
    """
    return re.sub(r"[^a-z0-9]", "", str(raw).strip().lower())


def mint_entry_id(catalog_designation: str) -> str:
    """The catalog's `entry_id` for a resolved target: the lowercased, de-
    punctuated primary OpenNGC designation (e.g. "NGC 6523" / "NGC6523" ->
    "ngc6523"). Matches the existing hand-authored convention in
    seestar/data/known_targets.json (Task 1) exactly -- this build tool reproduces
    those ids, it does not invent a new minting scheme.

    frozen 2026-07-25; pre-registration (spec sec11) -- do NOT change this rule
    after the Task-10 benchmark evaluation.
    """
    return normalize_id(catalog_designation)


# --- lookup_role (P1 fix: explicit spatial-candidacy schema, not verb/extent
# inference) ----------------------------------------------------------------------
#
# "primary" -- a genuine top-level lookup target the app can match a frame to.
# "associated_only" -- relational metadata only (e.g. M8's embedded cluster
# NGC6530): loaded and reachable by id/association, but excluded from top-level
# spatial candidacy in `seestar/known_targets.py`'s `match_target` so it can never
# by itself make a frame "ambiguous". See target_curation.yaml's annotations
# comment block for the curation-facing description.
VALID_LOOKUP_ROLES = {"primary", "associated_only"}


# --- OpenNGC Type -> this app's 3-way family taxonomy ---------------------------

# OpenNGC's `Type` column (see NGC_guide.txt) uses ~17 codes; this app's
# `Classification.family` (seestar/known_targets.py's `_FAMILY_TO_CATEGORY`) has
# only nebula/galaxy/starfield/unknown. Every code an OpenNGC row can carry for a
# *selected* target must appear here, OR the curation entry must supply an explicit
# `classification_override` (with a `reason`) -- there is no silent default.
#
# FROZEN policy (2026-07-26): every code a selected row can carry maps here to
# nebula/galaxy/starfield/unknown; a code with no honest single-family reading maps
# to "unknown" rather than guessing -- a per-record curated `classification_override`
# (with a reason) still wins where a specific object needs a different call than its
# type code's default (e.g. NGC6530's "Cl+N" is the embedded cluster, curated to
# "starfield" -- see target_curation.yaml):
#   - "*"     (single star) / "**" (double star) -> starfield (point sources).
#   - "Cl+N"  (star cluster + nebula, a genuinely mixed code -- nebula dominates for
#             some selected objects, cluster for others) -> unknown; disambiguate per
#             object via a curated classification_override with a reason.
#   - "Other" (catch-all OpenNGC assigns to loose asterisms/uncertain types) ->
#             unknown; same rationale.
# Any code NOT in this table (including future/unanticipated OpenNGC codes) is fatal
# -- there is deliberately no catch-all default.
TYPE_TO_FAMILY: dict[str, str] = {
    "G": "galaxy",
    "GPair": "galaxy",
    "GTrpl": "galaxy",
    "GGroup": "galaxy",
    "GCl": "starfield",
    "OCl": "starfield",
    "*Ass": "starfield",
    "**": "starfield",
    "*": "starfield",
    "Neb": "nebula",
    "EmN": "nebula",
    "RfN": "nebula",
    "HII": "nebula",
    "DrkN": "nebula",
    "PN": "nebula",
    "SNR": "nebula",
    "Cl+N": "unknown",
    "Other": "unknown",
}


# --- curation loading ------------------------------------------------------------


# --- schema-v2 curation validation + Messier/Caldwell resolution -----------------
#
# v2 curation shape (fail-closed, closed key sets):
#   schema_version: 2
#   selection: { messier: all|<list[int]>, caldwell: all|<list[int]>, include: [...] }
#   resolution: { abstain: { M102: { reason: "..." } } }
#   annotations: { <norm-id>: { ... } }   # exception-only

_ALLOWED_TOP = {"schema_version", "selection", "resolution", "annotations"}
_ALLOWED_SEL = {"messier", "caldwell", "include"}
_CALDWELL_TOKEN = re.compile(r"^C\s*0*(\d+)$")


def _validate_curation_shape(cur: dict) -> None:
    if not isinstance(cur, dict):
        raise BuildError("target_curation.yaml must parse to a mapping")
    unknown = set(cur) - _ALLOWED_TOP
    if unknown:
        raise BuildError(f"unknown top-level keys: {sorted(unknown)}")
    if cur.get("schema_version") != CURATION_SCHEMA_VERSION:
        raise BuildError(f"curation schema_version {cur.get('schema_version')!r} != {CURATION_SCHEMA_VERSION}")
    sel = cur.get("selection")
    if not isinstance(sel, dict):
        raise BuildError("missing 'selection' mapping")
    unknown_sel = set(sel) - _ALLOWED_SEL
    if unknown_sel:
        raise BuildError(f"selection has unknown keys: {sorted(unknown_sel)}")
    for k, v in ((cur.get("resolution") or {}).get("abstain") or {}).items():
        if not isinstance(v, dict) or "reason" not in v:
            raise BuildError(f"resolution.abstain[{k!r}] must carry a 'reason'")
    for k, v in (cur.get("annotations") or {}).items():
        if not isinstance(v, dict):
            raise BuildError(f"annotations[{k!r}] must be a mapping")


def _range_selector(value, lo, hi, label):
    if value == "all":
        return list(range(lo, hi + 1))
    if value is None:
        return []
    if isinstance(value, list) and all(isinstance(v, int) for v in value):
        for v in value:
            if not (lo <= v <= hi):
                raise BuildError(f"{label} selector {v} out of [{lo},{hi}]")
        return value
    raise BuildError(f"{label} selector must be 'all' or a list of ints")


def _messier_index(idx):
    out = {}
    for norm, row in idx.items():
        m = (row.get("M") or "").strip()
        if m.isdigit():
            out.setdefault(int(m), []).append(norm)
    return out


def _caldwell_index(idx):
    out = {}
    for norm, row in idx.items():
        for tok in (row.get("Identifiers") or "").split(","):
            mm = _CALDWELL_TOKEN.match(tok.strip())
            if mm:
                out.setdefault(int(mm.group(1)), []).append(norm)
    return out


def resolve_designation(designation, source_index, abstain):
    """Deterministically resolve a Messier/Caldwell/NGC/IC designation to the
    normalized primary OpenNGC row key, or None if the designation is an
    explicitly-declared abstention. Fail-closed on no-primary/ambiguous."""
    if normalize_id(designation) in {normalize_id(k) for k in abstain}:
        return None
    d = designation.strip().upper()
    if d.startswith("M") and d[1:].isdigit():
        cands = _messier_index(source_index).get(int(d[1:]), [])
    elif d.startswith("C") and d[1:].isdigit():
        n = int(d[1:])
        cands = _caldwell_index(source_index).get(n, [])
        if not cands:
            # Owner-verified: 4 Caldwell objects (C9/C14/C41/C99) resolve via a
            # primary `Name`, not an `Identifiers` token -- and the OpenNGC addendum
            # stores those primary names zero-padded to 3 digits ("C009"/"C014"/
            # "C041"/"C099"). Try the as-typed form AND the zero-padded form so the
            # documented "4 via primary Name" case resolves (bare "C9" alone never
            # matches "C009"). Still fail-closed if neither exists.
            for form in (designation, f"C{n:03d}"):
                norm = normalize_id(form)
                if norm in source_index:
                    cands = [norm]
                    break
    else:
        norm = normalize_id(designation)
        cands = [norm] if norm in source_index else []
    cands = [c for c in cands if (source_index[c].get("Type") or "").strip() != "Dup"]
    if len(cands) == 0:
        raise BuildError(f"{designation!r} resolves to no primary OpenNGC row (declare an abstention?)")
    if len(cands) > 1:
        raise BuildError(f"{designation!r} is ambiguous -> {cands}; declare an explicit resolution/abstention")
    return cands[0]


def expand_selection(curation, source_index):
    """Expand a v2 `selection` into the ordered, de-duplicated list of normalized
    primary OpenNGC row keys (Messier first, then Caldwell, then explicit includes),
    plus the resolution block actually applied. Deterministic: the order is driven
    by the selection ranges, never by source-index iteration order."""
    _validate_curation_shape(curation)
    sel = curation["selection"]
    abstain = (curation.get("resolution") or {}).get("abstain") or {}
    selected, seen = [], set()
    for prefix, nums in (
        ("M", _range_selector(sel.get("messier"), 1, 110, "messier")),
        ("C", _range_selector(sel.get("caldwell"), 1, 109, "caldwell")),
    ):
        for n in nums:
            norm = resolve_designation(f"{prefix}{n}", source_index, abstain)
            if norm and norm not in seen:
                seen.add(norm)
                selected.append(norm)
    for raw in sel.get("include") or []:
        norm = normalize_id(raw)
        if norm not in source_index:
            raise BuildError(f"selection.include {raw!r} not in OpenNGC source")
        if norm not in seen:
            seen.add(norm)
            selected.append(norm)
    return selected, {"abstain": abstain}


def _load_curation(path: str) -> dict:
    """Load + fail-closed validate a schema-v2 target_curation.yaml."""
    with open(path, encoding="utf-8") as f:
        try:
            curation = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            raise BuildError(f"target_curation.yaml is not valid YAML: {exc}") from exc
    _validate_curation_shape(curation)
    return curation


# --- OpenNGC snapshot loading -----------------------------------------------------


def _git_blob_sha1(data: bytes) -> str:
    """Compute a file's git blob object id (sha1) in PURE PYTHON -- git's blob
    object is `sha1(b"blob <len>\\0" + data)`. Never shells out to git, so the build
    stays hermetic and dependency-free (no git required at build time)."""
    header = b"blob %d\0" % len(data)
    return hashlib.sha1(header + data).hexdigest()


# --- the INDEPENDENT, reviewed source pin (NOT read from SOURCE.json) -------------
#
# BLOCKING-defect fix (2026-07-27): source-pin verification must enforce against a
# hardcoded, code-reviewed constant, NOT against the mutable vendored SOURCE.json.
# Verifying the vendored files against values read from SOURCE.json is circular -- an
# attacker who edits a file AND updates SOURCE.json's own hashes (or deletes a file
# AND its SOURCE.json entries, or rewrites the commit) sails through. The load-bearing
# check below compares every vendored file's LIVE computed hashes to THIS constant.
# SOURCE.json is then held to the constant too (hygiene: it may not silently drift),
# but it is never itself the authority.
#
#   tag/commit: the pinned OpenNGC release (Global Constraint).
#   files: the EXACT four-file set that must be present. blob = git blob sha1
#     (commit-addressed identity), sha256 = file content hash. Values are the
#     already-reviewed pins, cross-checked against `git hash-object` / sha256 of the
#     live vendored files at authoring time.
EXPECTED_OPENNGC_PIN = {
    "tag": "v20260501",
    "commit": "36cb178a0f69dba8bfc03a99c10512831edf1c6b",
    "files": {
        "NGC.csv": {
            "blob": "308d80f93c2d4cc34d339125b374506230555936",
            "sha256": "840fe0c9ee1332e551b2e722a0e92726cd7b157914a3d2177602832aadd3aa9e",
        },
        "addendum.csv": {
            "blob": "564c669f441b9be35dc4c1ee3b6348acde3464c2",
            "sha256": "1d8f0914e643ada325a5a94d88d8fefad6a4937a2f77cc34f21483af22b11983",
        },
        "NGC_guide.txt": {
            "blob": "7e5e1447c3cccd54cd5cbebb29387e5b30b05cc1",
            "sha256": "b7fca022985daa0f41a8bc8dd7028513e4db9ca96a2cfc380b68ff4c2e812810",
        },
        "LICENSES/CC-BY-SA-4.0.txt": {
            "blob": "835a6836b385fa068fdd625685548845cda60c2f",
            "sha256": "cde7883b9050a1104f4ac19a1572aafd6e5d7323b68351aaf51fbf4beba54966",
        },
    },
}


def _verify_source_integrity(source_dir: str, manifest: dict) -> None:
    """Refuse to build unless every vendored file matches the INDEPENDENT reviewed
    pin `EXPECTED_OPENNGC_PIN` -- never SOURCE.json (SOURCE.json is a mutable vendored
    file; verifying against it is circular). Fail-closed (`BuildError`) on ANY of:

      1. SOURCE.json `tag`/`commit` != the constant. (kills a rewritten/zeroed commit)
      2. Any of the constant's four pinned files is absent. (kills a deleted file,
         e.g. the license, regardless of SOURCE.json edits)
      3. For each pinned file: its LIVE git blob sha1 AND LIVE sha256 (computed in
         pure Python) don't both equal the CONSTANT's values. (kills byte changes even
         when SOURCE.json's own hashes were updated to match)
      4. (hygiene) SOURCE.json's own recorded blob/sha256 for each pinned file must
         also equal the constant, so SOURCE.json cannot silently drift. This is a
         guard, not the load-bearing check -- (3) is.
    """
    pin = EXPECTED_OPENNGC_PIN
    # 1. tag + commit enforced against the constant, not SOURCE.json's self-report.
    if manifest.get("tag") != pin["tag"]:
        raise BuildError(f"source pin: SOURCE.json tag {manifest.get('tag')!r} != pinned {pin['tag']!r}")
    if manifest.get("commit") != pin["commit"]:
        raise BuildError(f"source pin: SOURCE.json commit {manifest.get('commit')!r} != pinned {pin['commit']!r}")

    sha_map = manifest.get("sha256") or {}
    blob_map = manifest.get("git_blob_ids") or {}
    # 2a. EXACT-SET contract: SOURCE.json's maps must contain EXACTLY the pinned four
    #     files -- an EXTRA entry (even one with valid self-declared hashes for a real
    #     file that was added to the dir) is out-of-contract and must fail closed. The
    #     per-file loop below only visits the four pinned files, so rejecting extras
    #     needs this set comparison. (Missing entries are also caught per-file below.)
    expected_files = set(pin["files"])
    extra = (set(sha_map) - expected_files) | (set(blob_map) - expected_files)
    if extra:
        raise BuildError(
            f"source pin: SOURCE.json has out-of-contract entries {sorted(extra)}; "
            f"the pinned set is exactly {sorted(expected_files)}"
        )
    for rel, expected in pin["files"].items():
        # 2. the pinned file MUST exist (the constant requires the exact four-file
        #    set; a deleted file fails here regardless of SOURCE.json's contents).
        path = os.path.join(source_dir, rel)
        if not os.path.exists(path):
            raise BuildError(f"pinned source file missing: {rel}")
        data = open(path, "rb").read()
        # 3. LOAD-BEARING: live hashes vs the CONSTANT (not SOURCE.json).
        actual_sha = hashlib.sha256(data).hexdigest()
        if actual_sha != expected["sha256"]:
            raise BuildError(f"source integrity failure: {rel} sha256 {actual_sha} != pinned {expected['sha256']}")
        actual_blob = _git_blob_sha1(data)
        if actual_blob != expected["blob"]:
            raise BuildError(f"source integrity failure: {rel} git blob id {actual_blob} != pinned {expected['blob']}")
        # 4. hygiene: SOURCE.json's own record for this file must also match the
        #    constant -- an entry missing from SOURCE.json is an incomplete pin; a
        #    recorded value that disagrees with the constant is a drift/integrity
        #    failure. (Neither is load-bearing; (3) already caught real tampering.)
        if rel not in sha_map:
            raise BuildError(f"source pin incomplete: {rel} missing sha256 in SOURCE.json")
        if rel not in blob_map:
            raise BuildError(f"source pin incomplete: {rel} missing git blob id in SOURCE.json")
        if sha_map[rel] != expected["sha256"]:
            raise BuildError(f"source integrity failure: SOURCE.json sha256 for {rel} != pinned {expected['sha256']}")
        if blob_map[rel] != expected["blob"]:
            raise BuildError(
                f"source integrity failure: SOURCE.json git blob id for {rel} != pinned {expected['blob']}"
            )


def load_openngc_source(source_dir: str) -> tuple[str, dict[str, dict]]:
    base = os.path.basename(source_dir.rstrip("/"))
    m = re.match(r"openngc-(?P<version>.+)$", base)
    if not m:
        raise BuildError(f"OpenNGC source dir must be named 'openngc-<version>', got {base!r}")
    version = m.group("version")
    manifest = json.load(open(os.path.join(source_dir, "SOURCE.json"), encoding="utf-8"))
    _verify_source_integrity(source_dir, manifest)
    if manifest.get("tag") != version:
        raise BuildError(f"SOURCE.json tag {manifest.get('tag')!r} != dir version {version!r}")
    index: dict[str, dict] = {}
    for fname in ("NGC.csv", "addendum.csv"):
        with open(os.path.join(source_dir, fname), encoding="utf-8") as f:
            for row in csv.DictReader(f, delimiter=";"):
                name = (row.get("Name") or "").strip()
                if not name:
                    continue
                norm = normalize_id(name)
                if norm in index:
                    raise BuildError(f"OpenNGC source has duplicate Name entries for {name!r}")
                index[norm] = row
    return version, index


def _parse_ra_deg(raw: str) -> float:
    hh, mm, ss = raw.strip().split(":")
    return (float(hh) + float(mm) / 60.0 + float(ss) / 3600.0) * 15.0


def _parse_dec_deg(raw: str) -> float:
    s = raw.strip()
    sign = -1.0 if s.startswith("-") else 1.0
    s = s.lstrip("+-")
    dd, mm, ss = s.split(":")
    return sign * (float(dd) + float(mm) / 60.0 + float(ss) / 3600.0)


def _parse_float_or_none(raw: str | None) -> float | None:
    if raw is None or not str(raw).strip():
        return None
    return float(raw)


def _parse_common_names(raw: str | None) -> list[str]:
    if not raw or not raw.strip():
        return []
    return [name.strip() for name in raw.split(",") if name.strip()]


# --- search-footprint validation (spec sec4: fail-closed, even though inert) -----


def _validate_and_normalize_footprints(raw_list: list, label: str) -> list[dict]:
    seen_keys: list[str] = []
    normalized: list[dict] = []
    for i, fp in enumerate(raw_list):
        if not isinstance(fp, dict) or "shape" not in fp:
            raise BuildError(f"{label}: malformed search_footprint at index {i}: {fp!r}")
        shape = fp["shape"]
        if shape == "ellipse":
            entry = _validate_ellipse_footprint(fp, label, i)
        elif shape == "circle":
            entry = _validate_circle_footprint(fp, label, i)
        else:
            raise BuildError(f"{label}: malformed search_footprint at index {i}: unknown shape {shape!r}")
        key = json.dumps(entry, sort_keys=True)
        if key in seen_keys:
            raise BuildError(f"{label}: duplicate search_footprint at index {i}: {entry!r}")
        seen_keys.append(key)
        normalized.append(entry)
    return normalized


def _validate_ellipse_footprint(fp: dict, label: str, i: int) -> dict:
    required = ("major_arcmin", "minor_arcmin", "pa_deg")
    for key in required:
        if key not in fp:
            raise BuildError(f"{label}: malformed ellipse search_footprint at index {i}: missing {key!r}")
    major, minor, pa = fp["major_arcmin"], fp["minor_arcmin"], fp["pa_deg"]
    if not all(isinstance(v, int | float) and not isinstance(v, bool) for v in (major, minor, pa)):
        raise BuildError(f"{label}: malformed ellipse search_footprint at index {i}: non-numeric field")
    if major <= 0 or minor <= 0:
        raise BuildError(f"{label}: degenerate ellipse search_footprint at index {i}: non-positive axis")
    if minor > major:
        raise BuildError(f"{label}: degenerate ellipse search_footprint at index {i}: minor_arcmin > major_arcmin")
    if not (0.0 <= pa < 360.0):
        raise BuildError(f"{label}: malformed ellipse search_footprint at index {i}: pa_deg out of [0,360)")
    return {"shape": "ellipse", "major_arcmin": float(major), "minor_arcmin": float(minor), "pa_deg": float(pa)}


def _validate_circle_footprint(fp: dict, label: str, i: int) -> dict:
    if "radius_arcmin" not in fp:
        raise BuildError(f"{label}: malformed circle search_footprint at index {i}: missing 'radius_arcmin'")
    radius = fp["radius_arcmin"]
    if not isinstance(radius, int | float) or isinstance(radius, bool):
        raise BuildError(f"{label}: malformed circle search_footprint at index {i}: non-numeric radius_arcmin")
    if radius <= 0:
        raise BuildError(f"{label}: degenerate circle search_footprint at index {i}: non-positive radius_arcmin")
    return {"shape": "circle", "radius_arcmin": float(radius)}


# --- footprint derivation (spec sec4/sec10: inert in v1, but the data path is
# proven end-to-end for D2b -- see module docstring). Two provenance families:
# DERIVED (from the OpenNGC row's own extent columns) and CURATED (an explicit,
# reasoned override). Malformed source geometry is a fail-closed BuildError, never
# a silent circle fallback -- a negative/non-numeric MajAx/MinAx/PosAng means the
# source data is broken, not that the object is round. --------------------------


def _num_or_malformed(raw, label):
    if raw is None or str(raw).strip() == "":
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise BuildError(f"malformed source geometry: {label}={raw!r} is non-numeric") from None
    if v < 0:
        raise BuildError(f"malformed source geometry: {label}={raw!r} is negative")
    return v


def derive_footprint(row):
    """Derive a search footprint from an OpenNGC row's own extent columns
    (MajAx/MinAx/PosAng). Returns (footprints, provenance) where provenance is one
    of "derived-ellipse" / "derived-circle" / "unavailable". An ABSENT extent
    (empty MajAx) is "unavailable" -- distinct from MALFORMED geometry (negative or
    non-numeric), which is fatal (`_num_or_malformed`), never silently circled."""
    major = _num_or_malformed(row.get("MajAx"), "MajAx")
    minor = _num_or_malformed(row.get("MinAx"), "MinAx")
    pa = _num_or_malformed(row.get("PosAng"), "PosAng")
    if major is None or major == 0:
        return [], "unavailable"
    if minor is not None and minor > 0 and pa is not None:
        return (
            [{"shape": "ellipse", "major_arcmin": major, "minor_arcmin": minor, "pa_deg": pa % 180.0}],
            "derived-ellipse",
        )
    return ([{"shape": "circle", "radius_arcmin": major / 2.0}], "derived-circle")


def _resolve_footprints(row, ann):
    """Resolve a record's search footprints: a closed curated
    `search_footprints_override: {reason, footprints}` annotation schema wins
    (requires a `reason` -- a curated footprint is a judgment call, not a catalog
    fact); otherwise derive from the row's own OpenNGC extent columns."""
    ov = ann.get("search_footprints_override")
    if ov is not None:
        if not isinstance(ov, dict) or "reason" not in ov or "footprints" not in ov:
            raise BuildError("search_footprints_override must be a mapping with 'reason' and 'footprints'")
        return _validate_and_normalize_footprints(ov["footprints"], "curated-override"), "curated"
    return derive_footprint(row)


# --- per-record resolution --------------------------------------------------------


def _resolve_classification(designation: str, row: dict, ann: dict) -> tuple[str, str]:
    """Returns (family, provenance_source_string)."""
    if "classification_override" in ann:
        override = ann["classification_override"]
        if not isinstance(override, dict) or "family" not in override or "reason" not in override:
            raise BuildError(f"{designation}: classification_override must be a mapping with 'family' and 'reason'")
        return override["family"], "curated"
    type_code = (row.get("Type") or "").strip()
    family = TYPE_TO_FAMILY.get(type_code)
    if family is None:
        raise BuildError(
            f"{designation}: unmapped OpenNGC Type code {type_code!r} -- add it to TYPE_TO_FAMILY or "
            "supply a curated classification_override with a reason in target_curation.yaml"
        )
    return family, "openngc"


def _resolve_record(row: dict, ann: dict, openngc_version: str) -> dict:
    designation = (row.get("Name") or "").strip()
    entry_id = mint_entry_id(designation)

    canonical_id = ann.get("canonical_id", designation)
    equivalent_ids: list[str] = []
    if canonical_id != designation:
        equivalent_ids.append(designation)
    equivalent_ids.extend(ann.get("equivalent_ids_add", []) or [])

    common_names = _parse_common_names(row.get("Common names"))
    common_name = common_names[0] if common_names else None
    aliases = list(common_names)
    for extra in ann.get("aliases_add", []) or []:
        if extra not in aliases:
            aliases.append(extra)

    ra_deg = _parse_ra_deg(row["RA"])
    dec_deg = _parse_dec_deg(row["Dec"])
    extent_major = _parse_float_or_none(row.get("MajAx"))
    extent_minor = _parse_float_or_none(row.get("MinAx"))

    family, classification_source_kind = _resolve_classification(designation, row, ann)
    classification_source = "curated" if classification_source_kind == "curated" else f"OpenNGC {openngc_version}"

    footprints, footprint_provenance = _resolve_footprints(row, ann)

    morphology_traits = list(ann.get("morphology_traits", []) or [])

    associated = []
    for a in ann.get("associated", []) or []:
        if not isinstance(a, dict) or "canonical_id" not in a or "relationship" not in a:
            raise BuildError(f"{designation}: malformed associated entry {a!r} (requires canonical_id + relationship)")
        associated.append({"canonical_id": a["canonical_id"], "relationship": a["relationship"]})

    identity_provenance = f"OpenNGC {openngc_version}"
    if ann.get("aliases_add"):
        identity_provenance += " + curated aliases"

    lookup_role = ann.get("lookup_role", "primary")

    record = {
        "entry_id": entry_id,
        "canonical_id": canonical_id,
        "common_name": common_name,
        "aliases": aliases,
        "equivalent_ids": equivalent_ids,
        "classification": {"family": family, "source": classification_source},
        "ra_deg": round(ra_deg, 4),
        "dec_deg": round(dec_deg, 4),
        "extent_major_arcmin": round(extent_major, 2) if extent_major is not None else None,
        "extent_minor_arcmin": round(extent_minor, 2) if extent_minor is not None else None,
        "search_footprints": footprints,
        "morphology_traits": morphology_traits,
        "associated": associated,
        "lookup_role": lookup_role,
        "provenance": {
            "identity": identity_provenance,
            "classification": classification_source,
            "coordinates": f"OpenNGC {openngc_version}",
            "footprint": footprint_provenance,
            # Morphology is curation-sourced: "curated" ONLY where this record
            # actually carries curated morphology_traits; absent traits means it was
            # never curated, so "not-curated" is the accurate provenance (not the
            # blanket "curated" every record used to claim).
            "morphology": "curated" if morphology_traits else "not-curated",
        },
    }
    return record


# --- determinism: canonical content hash ------------------------------------------


def _content_hash(records: list[dict]) -> str:
    """sha256 over the canonicalized (sorted-key, fixed-separator) JSON of the
    resolved records list. Floats are already fixed-precision-rounded by
    `_resolve_record`, so this reproduces byte-identically across runs/processes/
    architectures for the same two inputs (Tenet 7, bounded repeatability)."""
    canonical = json.dumps(records, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- atomic JSON write (mirrors seestar/io.py's write_plan_sidecar pattern) -------


def _write_json_atomic(path: str, payload: dict) -> None:
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except OSError:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


# --- top-level build ---------------------------------------------------------------


def _memberships(row):
    """Derive (preferred canonical id, sorted equivalent ids) for a row per the
    approved canonical-ID policy: prefer a Messier id; a Caldwell-only NGC/IC object
    keeps its primary OpenNGC `Name` (NGC7000, not C20). Caldwell/NGC/IC ids and the
    Messier object's NGC/IC name land in equivalent_ids."""
    m = (row.get("M") or "").strip()
    messier_id = f"M{int(m)}" if m.isdigit() else None
    name = (row.get("Name") or "").strip()
    equiv = []
    for tok in (row.get("Identifiers") or "").split(","):
        mm = _CALDWELL_TOKEN.match(tok.strip())
        if mm:
            equiv.append(f"C{int(mm.group(1))}")
    ngc, ic = (row.get("NGC") or "").strip(), (row.get("IC") or "").strip()
    if ngc:
        equiv.append(f"NGC{ngc}")
    if ic:
        equiv.append(f"IC{ic}")
    preferred = messier_id or name  # Caldwell-only -> keep NGC/IC primary name
    if messier_id:
        equiv.append(name)
    seen, out = set(), []
    for e in equiv:
        if e and e != preferred and e not in seen:
            seen.add(e)
            out.append(e)
    return preferred, sorted(out)


def _validate_associations_and_roles(recs: dict) -> None:
    """Fail-closed cross-record validation shared by the build path: association
    resolution, the closed lookup_role set, orphaned associated_only records, and
    primary<->associated_only reciprocity. `recs` maps normalized-id -> record.
    Extracted from the retired v1 `build()` so build_v2 reuses the exact same laws."""
    # 1. every association must point at another resolved/selected record.
    canonical_ids = {r["canonical_id"] for r in recs.values()}
    for record in recs.values():
        for assoc in record["associated"]:
            if assoc["canonical_id"] not in canonical_ids:
                raise BuildError(
                    f"unresolved association: {record['canonical_id']} -> {assoc['canonical_id']!r} is not "
                    "among the resolved/selected targets (auto-include or add it to selection.include)"
                )

    # 2. every record's lookup_role must be one of the closed VALID_LOOKUP_ROLES set.
    for record in recs.values():
        role = record["lookup_role"]
        if role not in VALID_LOOKUP_ROLES:
            raise BuildError(
                f"{record['canonical_id']}: invalid lookup_role {role!r} -- must be one of {sorted(VALID_LOOKUP_ROLES)}"
            )

    # 3. every associated_only record must be referenced by >=1 primary record's
    #    associated[].canonical_id -- an orphan is drifted relational metadata.
    referenced_norm_ids: set[str] = {
        normalize_id(assoc["canonical_id"])
        for record in recs.values()
        if record["lookup_role"] == "primary"
        for assoc in record["associated"]
    }
    for record in recs.values():
        if record["lookup_role"] != "associated_only":
            continue
        if normalize_id(record["canonical_id"]) not in referenced_norm_ids:
            raise BuildError(
                f"orphaned associated_only record: {record['canonical_id']!r} is not referenced by any "
                "primary record's associated[].canonical_id"
            )

    # 4. reciprocity ("inconsistently referenced"): if a primary P references an
    #    associated_only record X, X's own associated list must reference P back.
    records_by_canonical_norm = {normalize_id(r["canonical_id"]): r for r in recs.values()}
    for record in recs.values():
        if record["lookup_role"] != "primary":
            continue
        for assoc in record["associated"]:
            target = records_by_canonical_norm.get(normalize_id(assoc["canonical_id"]))
            if target is None or target["lookup_role"] != "associated_only":
                continue  # not an associated_only reference; nothing to reciprocate
            back_refs = {normalize_id(a["canonical_id"]) for a in target["associated"]}
            if normalize_id(record["canonical_id"]) not in back_refs:
                raise BuildError(
                    f"inconsistently referenced associated_only record: {record['canonical_id']} -> "
                    f"{target['canonical_id']!r} is not reciprocated by {target['canonical_id']!r}'s own "
                    "associated[].canonical_id"
                )


def build_v2(curation, source_index, version, out_path):
    """Reconcile a schema-v2 `curation` against an already-loaded OpenNGC
    `source_index` (from `load_openngc_source`) into the fully-resolved catalog
    dict, optionally writing it atomically to `out_path`. The OpenNGC `version` is
    threaded in (never hardcoded). Fail-closed on every contract condition."""
    selected, resolution = expand_selection(curation, source_index)
    annotations = curation.get("annotations") or {}
    selected_set = set(selected)
    # Annotation keys are matched by normalized id (Global Constraint: "NGC7000" /
    # "ngc:7000" / "NGC 7000" are the same target). Normalize once, fail-closed on a
    # key that resolves to no selected target and on a normalized-key collision.
    ann_by_norm: dict[str, dict] = {}
    for key, value in annotations.items():
        norm_key = normalize_id(key)
        if norm_key not in selected_set:
            raise BuildError(f"annotations key {key!r} does not match any selection-resolved target")
        if norm_key in ann_by_norm:
            raise BuildError(f"duplicate annotations key (after normalization): {key!r}")
        ann_by_norm[norm_key] = value or {}
    recs = {}
    for norm in selected:
        row = source_index[norm]
        ann = ann_by_norm.get(norm) or ann_by_norm.get(normalize_id(row.get("Name", ""))) or {}
        rec = _resolve_record(row, ann, version)
        if "canonical_id" not in ann:
            preferred, equiv = _memberships(row)
            rec["canonical_id"] = preferred
            rec["equivalent_ids"] = sorted(set(rec.get("equivalent_ids", [])) | set(equiv))
        recs[norm] = rec
    seen_ids = {}
    for norm, rec in recs.items():
        if rec["entry_id"] in seen_ids:
            raise BuildError(f"entry_id collision: {rec['entry_id']!r} ({seen_ids[rec['entry_id']]} & {norm})")
        seen_ids[rec["entry_id"]] = norm
    _validate_associations_and_roles(recs)
    ordered = [recs[n] for n in selected]
    payload = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "provenance": {
            "source": f"OpenNGC {version} (identity/coordinates), Messier+Caldwell selection",
            "license": "CC-BY-SA-4.0 (OpenNGC) -- see THIRD_PARTY_NOTICES.md",
            "generated": "tools/build_known_targets.py build_v2 (deterministic)",
            "content_hash": _content_hash(ordered),
            "curation_schema_version": curation["schema_version"],
            "abstentions": sorted(resolution["abstain"]),
            "note": "Coordinates/extent are OpenNGC facts; footprints are catalog-derived "
            "(curated only where a search_footprints_override was given); morphology is "
            "curated only where present, otherwise not-curated. Footprints/morphology INERT in v1.",
        },
        "records": ordered,
    }
    if out_path is not None:
        _write_json_atomic(out_path, payload)
    return payload


# --- data manifest (owner-review artifact; PURE DATA, no geometry/WCS, no seestar
# import). Everything here is a count, a table, a provenance string, or a hash --
# nothing computes footprint overlap or drives the matcher (that geometry-aware
# review is the SEPARATE tools/known_targets_review.py). ------------------------


def _resolution_summary(curation: dict, source_index: dict) -> dict:
    """Pure-data M/C identifier-resolution summary: how many Messier/Caldwell
    designations resolve, by which path, and which abstain."""
    abstain = (curation.get("resolution") or {}).get("abstain") or {}
    abstain_norm = {normalize_id(k) for k in abstain}
    cald_idx = _caldwell_index(source_index)
    messier_resolved, messier_abstained = [], []
    for n in range(1, 111):
        d = f"M{n}"
        if normalize_id(d) in abstain_norm:
            messier_abstained.append(d)
            continue
        if resolve_designation(d, source_index, abstain) is not None:
            messier_resolved.append(d)
    cald_resolved, cald_via_identifiers, cald_via_name, cald_abstained = [], [], [], []
    for n in range(1, 110):
        d = f"C{n}"
        if normalize_id(d) in abstain_norm:
            cald_abstained.append(d)
            continue
        if resolve_designation(d, source_index, abstain) is None:
            continue
        cald_resolved.append(d)
        if n in cald_idx:
            cald_via_identifiers.append(d)
        else:
            cald_via_name.append(d)
    return {
        "messier_resolved": messier_resolved,
        "messier_abstained": messier_abstained,
        "caldwell_resolved": cald_resolved,
        "caldwell_via_identifiers": cald_via_identifiers,
        "caldwell_via_name": cald_via_name,
        "caldwell_abstained": cald_abstained,
    }


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def render_manifest(
    curation: dict, source_index: dict, version: str, source_dir: str, payload: dict, out_json_path: str | None
) -> str:
    """Render the pure-DATA owner-review manifest markdown from already-built state.
    No geometry, no WCS, no seestar import -- counts, resolution table, type
    distribution, provenance, abstentions, curated overrides, associations, hashes."""
    records = payload["records"]
    src_manifest = json.load(open(os.path.join(source_dir, "SOURCE.json"), encoding="utf-8"))
    abstain = (curation.get("resolution") or {}).get("abstain") or {}
    res = _resolution_summary(curation, source_index)

    by_role: dict[str, int] = {}
    for r in records:
        by_role[r["lookup_role"]] = by_role.get(r["lookup_role"], 0) + 1
    n_primary = by_role.get("primary", 0)
    n_assoc = by_role.get("associated_only", 0)

    # type distribution + resulting family (row = OpenNGC Type -> family, counts)
    type_family_counts: dict[tuple[str, str], int] = {}
    fam_counts: dict[str, int] = {}
    unknown_records: list[dict] = []
    for r in records:
        row = source_index.get(r["entry_id"], {})
        tcode = (row.get("Type") or "").strip() or "(none)"
        fam = r["classification"]["family"]
        type_family_counts[(tcode, fam)] = type_family_counts.get((tcode, fam), 0) + 1
        fam_counts[fam] = fam_counts.get(fam, 0) + 1
        if fam == "unknown":
            unknown_records.append(r)

    # footprint / morphology provenance counts
    fp_prov: dict[str, int] = {}
    morph_prov: dict[str, int] = {}
    for r in records:
        fp_prov[r["provenance"]["footprint"]] = fp_prov.get(r["provenance"]["footprint"], 0) + 1
        morph_prov[r["provenance"]["morphology"]] = morph_prov.get(r["provenance"]["morphology"], 0) + 1

    missing_extent = [r for r in records if r["extent_major_arcmin"] is None]

    out_sha = None
    if out_json_path is not None and os.path.exists(out_json_path):
        out_sha = hashlib.sha256(open(out_json_path, "rb").read()).hexdigest()

    # The manifest is a committed, portable review artifact -- it must NOT embed an
    # absolute, machine-specific `--out` path. Render a STABLE repository-relative
    # path (forward-slashed) so the manifest is byte-stable across machines/checkouts.
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    out_display = None
    if out_json_path is not None:
        out_display = os.path.relpath(os.path.abspath(out_json_path), repo_root).replace(os.sep, "/")

    lines: list[str] = []
    A = lines.append
    A("# Known-targets catalog manifest (owner-review; pure data)")
    A("")
    A("> Generated by `tools/build_known_targets.py --manifest` (no geometry, no WCS, no")
    A("> `seestar` import). The geometry-aware ambiguity/WCS review is the separate")
    A("> `validation/known_targets_ambiguity_impact.md` (via `tools/known_targets_review.py`).")
    A("")

    A("## 1. Population counts (stop-condition contract)")
    A("")
    A(
        _md_table(
            ["metric", "value"],
            [
                ["primary records", n_primary],
                ["associated-only records", n_assoc],
                ["total records", len(records)],
                ["expected (contract)", "218 primary + 1 associated = 219"],
            ],
        )
    )
    A("")

    A("## 2. Source pin (independently verified)")
    A("")
    A(
        _md_table(
            ["field", "value"],
            [
                ["tag", src_manifest.get("tag")],
                ["commit", src_manifest.get("commit")],
                ["release_url", src_manifest.get("release_url")],
                ["retrieval_date", src_manifest.get("retrieval_date")],
                ["license", src_manifest.get("license")],
            ],
        )
    )
    A("")
    A("Vendored source files (git blob id / sha256 / row count):")
    A("")
    blob = src_manifest.get("git_blob_ids", {})
    sha = src_manifest.get("sha256", {})
    rc = src_manifest.get("row_counts", {})
    frows = []
    for f in sorted(set(blob) | set(sha)):
        frows.append([f, blob.get(f, "-"), sha.get(f, "-"), rc.get(f, "-")])
    A(_md_table(["file", "git blob id", "sha256", "rows"], frows))
    A("")

    A("## 3. Messier / Caldwell identifier resolution")
    A("")
    A(
        _md_table(
            ["set", "resolved", "abstained", "notes"],
            [
                [
                    "Messier M1-M110",
                    len(res["messier_resolved"]),
                    ", ".join(res["messier_abstained"]) or "-",
                    "abstentions carry a reason (sec 4)",
                ],
                [
                    "Caldwell C1-C109",
                    len(res["caldwell_resolved"]),
                    ", ".join(res["caldwell_abstained"]) or "-",
                    f"{len(res['caldwell_via_identifiers'])} via Identifiers token, "
                    f"{len(res['caldwell_via_name'])} via primary Name ({', '.join(res['caldwell_via_name'])})",
                ],
            ],
        )
    )
    A("")

    A("## 4. Abstentions")
    A("")
    for k in sorted(abstain):
        reason = " ".join(str(abstain[k].get("reason", "")).split())
        A(f"- **{k}** — {reason}")
    A("")

    A("## 5. Type distribution -> family mapping")
    A("")
    trows = []
    for tcode, fam in sorted(type_family_counts):
        trows.append([tcode, fam, type_family_counts[(tcode, fam)]])
    A(_md_table(["OpenNGC Type", "family", "count"], trows))
    A("")
    A("Family totals: " + ", ".join(f"{k}={v}" for k, v in sorted(fam_counts.items())) + ".")
    A("")

    A("## 6. OWNER DECISION — Cl+N / mixed objects accepted as UNKNOWN")
    A("")
    A("These records classify as **family=unknown** (their OpenNGC Type code — `Cl+N`")
    A("star-cluster-plus-nebula, or `Other` — has no honest single-family reading). They")
    A("are left at the FROZEN unknown default with **NO curated override**.")
    A("")
    A("> **OWNER DECISION (2026-07-27): accepted as UNKNOWN; no classification override**")
    A("> for {M42, M73, IC5146, NGC2239, IC2944}. The five records stand at the FROZEN")
    A("> unknown default with no per-object `classification_override`.")
    A("")
    urows = []
    for r in unknown_records:
        row = source_index.get(r["entry_id"], {})
        urows.append([r["canonical_id"], r["entry_id"], (row.get("Type") or "").strip(), r["common_name"] or "-"])
    A(_md_table(["canonical_id", "entry_id", "OpenNGC Type", "common name"], urows))
    A("")
    A("> Note (finding at regeneration): the population plan's brief predicted the")
    A("> UNKNOWN set as {M42, M43, IC5146, NGC2239, IC2944}. In this pinned OpenNGC")
    A("> snapshot **M43 is typed `HII` -> nebula** (not unknown), while **M73 is typed")
    A("> `Other`** (a 4-star asterism) -> unknown. The count is still 5; the membership")
    A("> is {M42, M73, IC5146, NGC2239, IC2944}. Owner-confirmed as UNKNOWN (2026-07-27).")
    A("")

    A("## 7. Curated overrides + reasons (exception-only annotations)")
    A("")
    for key, ann in (curation.get("annotations") or {}).items():
        ann = ann or {}
        A(f"### `{key}`")
        if "canonical_id" in ann:
            A(f"- canonical_id override: **{ann['canonical_id']}**")
        if ann.get("aliases_add"):
            A(f"- aliases added: {ann['aliases_add']}")
        if "classification_override" in ann:
            co = ann["classification_override"]
            A(f"- classification_override: **{co.get('family')}** — {' '.join(str(co.get('reason', '')).split())}")
        if ann.get("morphology_traits"):
            A(f"- morphology_traits (curated): {ann['morphology_traits']}")
        if "lookup_role" in ann:
            A(f"- lookup_role: **{ann['lookup_role']}**")
        if ann.get("associated"):
            A(f"- associated: {[(a['canonical_id'], a['relationship']) for a in ann['associated']]}")
        if "search_footprints_override" in ann:
            A("- **search_footprints_override present** (curated footprint)")
        else:
            A("- footprint: catalog-derived (from OpenNGC extent; no curated override for this record)")
        A("")

    A("## 8. Associations + lookup roles")
    A("")
    arows = []
    for r in records:
        if r["associated"] or r["lookup_role"] != "primary":
            assoc = "; ".join(f"{a['canonical_id']}({a['relationship']})" for a in r["associated"]) or "-"
            arows.append([r["canonical_id"], r["lookup_role"], assoc])
    A(_md_table(["canonical_id", "lookup_role", "associated"], arows))
    A("")

    A("## 9. Footprint + morphology provenance counts")
    A("")
    A(_md_table(["footprint provenance", "count"], sorted(fp_prov.items())))
    A("")
    A(_md_table(["morphology provenance", "count"], sorted(morph_prov.items())))
    A("")

    A("## 10. Missing-extent records (no catalogued MajAx -> footprint 'unavailable')")
    A("")
    if missing_extent:
        mrows = [[r["canonical_id"], r["entry_id"], r["provenance"]["footprint"]] for r in missing_extent]
        A(_md_table(["canonical_id", "entry_id", "footprint provenance"], mrows))
    else:
        A("_None — every selected record has a catalogued extent._")
    A("")

    A("## 11. Hashes (re-freeze contract)")
    A("")
    A(
        _md_table(
            ["artifact", "value"],
            [
                ["catalog content_hash", payload["provenance"]["content_hash"]],
                ["output file", out_display or "(not written)"],
                ["output file sha256", out_sha or "(not written)"],
            ],
        )
    )
    A("")

    A("## 12. Canonical + equivalent ids (all records)")
    A("")
    idrows = []
    for r in records:
        idrows.append([r["canonical_id"], r["entry_id"], ", ".join(r["equivalent_ids"]) or "-"])
    A(_md_table(["canonical_id", "entry_id", "equivalent_ids"], idrows))
    A("")

    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--curation",
        default=os.path.join(os.path.dirname(__file__), "..", "target_curation.yaml"),
        help="Path to target_curation.yaml (default: repo root)",
    )
    ap.add_argument(
        "--openngc",
        "--source-dir",
        dest="source_dir",
        default=os.path.join(os.path.dirname(__file__), "data", "openngc-v20260501"),
        help="Path to the pinned, integrity-verified OpenNGC source directory (default: tools/data/openngc-v20260501)",
    )
    ap.add_argument(
        "--out",
        default=os.path.join(os.path.dirname(__file__), "..", "seestar", "data", "known_targets.json"),
        help="Output path for the generated catalog JSON (default: seestar/data/known_targets.json)",
    )
    ap.add_argument(
        "--manifest",
        default=None,
        help="Optional path to write the pure-DATA owner-review manifest markdown "
        "(counts, resolution table, type distribution, provenance, abstentions, hashes). "
        "No geometry/WCS -- that review is tools/known_targets_review.py.",
    )
    args = ap.parse_args()
    try:
        curation = _load_curation(args.curation)
        version, index = load_openngc_source(args.source_dir)
        result = build_v2(curation, index, version, args.out)
        if args.manifest:
            manifest_md = render_manifest(curation, index, version, args.source_dir, result, args.out)
            with open(args.manifest, "w", encoding="utf-8") as f:
                f.write(manifest_md)
    except BuildError as exc:
        print(f"BuildError: {exc}")
        return 1
    print(f"wrote {len(result['records'])} records to {args.out}")
    print(f"content_hash={result['provenance']['content_hash']}")
    if args.manifest:
        print(f"wrote manifest to {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
