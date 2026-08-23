"""Maintainer-only WCS/ambiguity REVIEW tool. Unlike tools/build_known_targets.py
(which imports no seestar module), this tool DOES import seestar.known_targets to run
the real, unchanged matcher over frames. It never changes runtime behavior; it only
reports the consequence of catalog expansion on the fixed four-outcome matcher."""

from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
S30_FOV_ARCMIN = 130.0


def _bkt():
    spec = importlib.util.spec_from_file_location("build_known_targets", REPO_ROOT / "tools" / "build_known_targets.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["build_known_targets"] = m
    spec.loader.exec_module(m)
    return m


def build_records(curation: dict, source_dir: str):
    import seestar.known_targets as kt

    bkt = _bkt()
    version, index = bkt.load_openngc_source(source_dir)
    payload = bkt.build_v2(curation, index, version, None)
    return tuple(kt._record_from_dict(r) for r in payload["records"])


def ambiguity_histogram(records, fov_arcmin: float = S30_FOV_ARCMIN) -> dict:
    import seestar.known_targets as kt

    hist = Counter()
    for rec in records:
        if rec.lookup_role != "primary":
            continue
        res = kt.match_target(records, (rec.ra_deg, rec.dec_deg), fov_arcmin, None)
        hist[res.status] += 1
    return dict(hist)


def ambiguous_detail(records, fov_arcmin: float = S30_FOV_ARCMIN) -> list[dict]:
    """Reporting-only enumeration of every ambiguous centered-on-primary field.

    For each primary record whose OWN centered frame (`match_target` over the real,
    unchanged matcher/`MIN_OVERLAP_FRAC`) returns "ambiguous", report the full
    candidate set -- every primary record that itself clears `MIN_OVERLAP_FRAC` for
    that frame -- as `{"canonical_id": ..., "category": ...}` entries, plus whether
    those candidate categories are:

    - **"UNANIMOUS"**: every candidate shares one non-`unknown` category family, or
    - **"CONFLICTING"**: categories differ across candidates, or any candidate is
      `unknown`.

    This is READ-ONLY reporting: it imports `seestar.known_targets`'s internals
    (`_overlap_fraction`, `MIN_OVERLAP_FRAC`, `match_target`) to recompute the exact
    same candidate set the real matcher already used to decide "ambiguous", and
    introduces no new matching behavior, no category-consensus resolution, and no
    change to `seestar/known_targets.py`.
    """
    from seestar.core.types import Category
    from seestar.known_targets import MIN_OVERLAP_FRAC, _overlap_fraction, match_target

    primaries = [r for r in records if r.lookup_role == "primary"]
    out: list[dict] = []
    for rec in primaries:
        center = (rec.ra_deg, rec.dec_deg)
        res = match_target(records, center, fov_arcmin, None)
        if res.status != "ambiguous":
            continue
        candidates = sorted(
            (r for r in primaries if _overlap_fraction(r, center, fov_arcmin, None) >= MIN_OVERLAP_FRAC),
            key=lambda r: r.canonical_id,
        )
        categories = {c.category for c in candidates}
        unanimous = len(categories) == 1 and Category.UNKNOWN not in categories
        out.append(
            {
                "canonical_id": rec.canonical_id,
                "candidates": [{"canonical_id": c.canonical_id, "category": c.category.value} for c in candidates],
                "unanimous": unanimous,
                "verdict": "UNANIMOUS" if unanimous else "CONFLICTING",
            }
        )
    return out


# --- Expected real-frame identity: static, frame-independent (FIX-3) -----------
# The three mounted reference frames the prototype was locked against (CLAUDE.md
# sec4's reference data set). This is NOT measured here -- it is enforced by the
# strict, frame-gated assertions in `tests/test_known_targets_ambiguity_impact.py`'s
# `test_real_reference_frames_strict_hit_on_own_identity` and the per-test-gated
# NGC5128/NGC7000 tests in `tests/test_known_targets_value_gate.py` (both SKIP,
# never fail, when a frame isn't mounted on this machine). Listed here only as a
# static expectation table so the report stays fully deterministic and
# frame-independent (no machine-specific file names/paths/measured FOV embedded).
EXPECTED_REAL_FRAME_IDENTITY = (
    ("M8", "M8"),
    ("NGC5128", "NGC5128"),
    ("NGC7000", "NGC7000"),
)


def render_ambiguity_report(records, fov_arcmin: float = S30_FOV_ARCMIN) -> str:
    """Render the FULL deterministic, frame-independent Markdown review report.

    Deterministic in the sense that the exact same `records` (built from the pinned
    OpenNGC source + `target_curation.yaml`) and `fov_arcmin` always produce
    byte-identical output -- no machine-specific values (no mounted-frame file names,
    paths, or measured FOV) are embedded. `tests/test_known_targets_ambiguity_impact.py`'s
    `test_ambiguity_report_matches_committed` asserts the committed
    `validation/known_targets_ambiguity_impact.md` equals this function's output,
    as a drift guard that runs on every machine (including CI, with no frames
    mounted at all).
    """
    hist = ambiguity_histogram(records, fov_arcmin)
    detail = ambiguous_detail(records, fov_arcmin)
    n_primaries = sum(1 for r in records if r.lookup_role == "primary")
    n_ambiguous = hist.get("ambiguous", 0)
    n_unanimous = sum(1 for e in detail if e["unanimous"])
    n_conflicting = len(detail) - n_unanimous

    lines: list[str] = []
    lines.append("# Known-targets ambiguity-impact (matching unchanged; 4 outcomes)")
    lines.append("")
    lines.append(
        "Maintainer-only review artifact, generated by `tools/known_targets_review.py` "
        "(`build_records`/`ambiguity_histogram`/`ambiguous_detail`/`render_ambiguity_report`) "
        f"run over the full Messier+Caldwell population ({len(records)} records: {n_primaries} "
        "primary + associated-only NGC6530) with the S30 reference FOV "
        f"({fov_arcmin:g}'). **This is reporting only** -- it imports "
        "`seestar.known_targets`'s real, unmodified `match_target`/`_overlap_fraction`/"
        "`MIN_OVERLAP_FRAC` to recompute what the matcher already decided; it introduces "
        "no `category_consensus` outcome, no new `MatchResult` field, and no change to "
        "`seestar/known_targets.py`'s four-outcome matcher (`hit | miss | ambiguous | "
        "spatially_rejected`; `ambiguous` -> abstain, unchanged)."
    )
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(
        f"Synthetic centered-on-primary sweep (S30 FOV {fov_arcmin:g}'), one frame "
        f"centered on each of the {n_primaries} primary records: **{dict(hist)!r}**."
    )
    lines.append("")
    lines.append(
        f"Of the {n_ambiguous} ambiguous fields: **{n_unanimous} of {n_ambiguous} are "
        f"category-UNANIMOUS**, and **{n_conflicting} of {n_ambiguous} are "
        "category-CONFLICTING** (candidate categories differ across the ambiguous "
        "candidate set, or at least one candidate is `unknown`)."
    )
    lines.append("")
    lines.append(
        "Prototype targets M8 / NGC7000 / NGC5128 / NGC5139 all still single-hit on "
        "their own synthetic centered frame (`test_no_prototype_target_regresses_after_expansion`). "
        "Newly-ambiguous fields degrade to the existing `ambiguous -> abstain` path (the "
        "pipeline's IRCUT-network / LP-measured fallback), exactly as an out-of-catalog "
        "frame already does -- no runtime behavior changed by this review."
    )
    lines.append("")
    lines.append(f"## All {n_ambiguous} ambiguous fields (full enumeration)")
    lines.append("")
    lines.append(
        "Each row is one primary record's OWN centered synthetic frame. \"candidate "
        'ids" / "candidate categories" is the full set of primary records that '
        "themselves clear `MIN_OVERLAP_FRAC` for that frame (this is why `match_target` "
        "returned `ambiguous` -- more than one candidate cleared the threshold, so the "
        'matcher could not safely pick one). "verdict" is UNANIMOUS only when every '
        "candidate shares one non-`unknown` category; otherwise CONFLICTING."
    )
    lines.append("")
    lines.append("| centered target | candidate ids | candidate categories | verdict |")
    lines.append("|---|---|---|---|")
    for entry in detail:
        ids = ", ".join(c["canonical_id"] for c in entry["candidates"])
        cats = ", ".join(c["category"] for c in entry["candidates"])
        lines.append(f"| {entry['canonical_id']} | {ids} | {cats} | {entry['verdict']} |")
    lines.append("")
    lines.append(f"**{n_unanimous} of {n_ambiguous} unanimous / {n_conflicting} of {n_ambiguous} conflicting.**")
    lines.append("")
    lines.append("## Expected real-frame identity (enforced by the strict value-gate test)")
    lines.append("")
    lines.append(
        "Matching is unchanged; the real, mounted reference frames must still resolve "
        "unambiguously to their own canonical identity. This static expectation is "
        "enforced by the strict, per-test frame-gated assertions in "
        "`tests/test_known_targets_ambiguity_impact.py::test_real_reference_frames_strict_hit_on_own_identity` "
        "and the NGC5128/NGC7000 tests in `tests/test_known_targets_value_gate.py` "
        "(each SKIPs, never fails, when its frame is not mounted on this machine) -- "
        "not embedded here as measured values, since this report is frame-independent:"
    )
    lines.append("")
    lines.append("| reference frame | expected canonical id |")
    lines.append("|---|---|")
    for frame, expected_cid in EXPECTED_REAL_FRAME_IDENTITY:
        lines.append(f"| {frame} | {expected_cid} |")
    lines.append("")
    return "\n".join(lines)
