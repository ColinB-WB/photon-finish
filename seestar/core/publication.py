"""Pure publication policy: certification, naming, and the deletion-authorizing
member grammar (spec docs/history/superpowers/specs/2026-08-13-honest-publication-semantics-design.md).

No I/O and no filesystem knowledge: every function here builds or checks
BASENAMES; the shell (orchestrate.py) maps them to paths. This module is the
single owner of the `_uncertified` naming contract and of what a cleanup
inventory may name -- an edited or corrupt manifest must never authorize
deleting a certified file (every valid member basename contains the
`_uncertified` token)."""

from __future__ import annotations

from .policy import PROFILE_OVERLAYS
from .types import HeroOutcome, PublicationDecision, PublicationIdentity, PublicationNamespace

# The closed fork-label set the planner emits (core/planning.py). Forks (and the
# QA starless) exist only for the canonical out_name "final" -- the grammar below
# accepts fork forms only for that identity, so no other identity's manifest can
# name them (a free fork token would let e.g. a print-identity manifest reach
# M_starless_uncertified.tif).
FORK_LABELS = ("stars-on", "star-reduced", "starless", "closeup")

# QA-artifact labels (manifest `qa_artifacts` members with published names, not
# user deliverables): the structure-support raster (Diagnoses 5+3) and the frozen
# SOLVE REGION (owner ruling, 2026-08-14 -- two rasters answering two different
# questions, see `solve_region_artifact_name`). Like the cluster QA starless,
# published for the canonical ("final") identity only.
QA_ARTIFACT_LABELS = ("support", "solve_region")

# Tokens no out_name may ever equal (collision with the namespace/sidecar grammar).
RESERVED_TOKENS = ("uncertified", "annotated", "plan", "notes", "run", "recipe", "qa")

_RASTER_EXTS = ("tif", "png", "jpg")


def allowed_out_names() -> frozenset[str]:
    """{"final"} plus every non-showcase profile name (showcase maps to "final")."""
    return frozenset({"final"} | (set(PROFILE_OVERLAYS) - {"showcase"}))


def validate_out_name(out_name: str) -> list[str]:
    """Empty list = valid. Names are not injective across identities, so anything
    outside the derived safe set is a hard error (spec rev 5, owner round 3)."""
    if out_name in allowed_out_names():
        return []
    return [f"out_name {out_name!r} is not a supported output name; allowed: {sorted(allowed_out_names())}"]


def decide_publication(outcome: HeroOutcome, out_name: str) -> PublicationDecision:
    """PASS/SOURCE_LIMITED -> certified publication that cleans the same-out_name
    uncertified set; PROCESSING_FAILED -> uncertified publication, cleans nothing."""
    certified = outcome is not HeroOutcome.PROCESSING_FAILED
    ns = PublicationNamespace.CERTIFIED if certified else PublicationNamespace.UNCERTIFIED
    return PublicationDecision(
        certified=certified,
        namespace=ns,
        identity=PublicationIdentity(out_name, ns),
        cleans=PublicationIdentity(out_name, PublicationNamespace.UNCERTIFIED) if certified else None,
    )


def _uncertified_stem(target: str, out_name: str) -> str:
    """The _uncertified token SUBSTITUTES for the "final" token; other out_names keep theirs."""
    if out_name == "final":
        return f"{target}_uncertified"
    return f"{target}_{out_name}_uncertified"


def published_base(target: str, out_name: str, certified: bool) -> str:
    if certified:
        return f"{target}_{out_name}"
    return _uncertified_stem(target, out_name)


def published_fork_base(target: str, fork: str, certified: bool) -> str:
    return f"{target}_{fork}" if certified else f"{target}_{fork}_uncertified"


def solve_region_artifact_name(region_source: str | None, target: str) -> str | None:
    """Which QA raster a solve with this `region_source` measured its body metric
    over (unpublished stem, before the certified/uncertified grammar), or None
    for a mask-free solve that froze no region.

    ONE mapping, shared by the immediate hero contract (`orchestrate.py`) and a
    later `seestar qa` (which reads `region_source` back from the run's persisted
    provenance), so the two surfaces can never drift onto different rasters. The
    frame-filling recovery case reuses the structure-support raster rather than
    publishing the same pixels twice."""
    if region_source is None:
        return None
    if region_source == "recovered_structure_support":
        return f"{target}_support"
    return f"{target}_solve_region"


def published_qa_base(target: str, label: str, certified: bool) -> str:
    """Published basename stem for a QA-artifact label (same _uncertified token
    grammar as forks -- one naming convention, one cleanup path)."""
    return f"{target}_{label}" if certified else f"{target}_{label}_uncertified"


def sidecar_names(target: str, out_name: str, certified: bool) -> dict[str, str]:
    """plan/notes/run/recipe/qa basenames for one identity. NOTE: certified sidecars
    are the legacy shared names (no out_name -- spec §1 caveat); uncertified sidecars
    are fully identity-keyed."""
    stem = target if certified else _uncertified_stem(target, out_name)
    return {k: f"{stem}_{k}.json" for k in ("plan", "notes", "run", "recipe", "qa")}


def annotated_name(target: str, out_name: str, certified: bool) -> str:
    if certified:
        return f"{target}_{out_name}_annotated.jpg"
    return f"{_uncertified_stem(target, out_name)}_annotated.jpg"


def _is_clean_basename(name: str) -> bool:
    return bool(name) and "/" not in name and "\\" not in name and ".." not in name and ".prev_" not in name


def validate_member_basename(name: str, target: str, out_name: str) -> bool:
    """spec §5 grammar, exactly two forms:
    - generic raster: the identity's hero base + raster ext; fork bases ONLY for the
      "final" identity (planning emits forks/QA-starless only when canon)
    - exact JSON: the identity's uncertified plan/notes/run sidecar names
    Everything else (including recipe/qa/annotated -- extras, not members) is invalid."""
    if not _is_clean_basename(name):
        return False
    hero = published_base(target, out_name, False)
    for ext in _RASTER_EXTS:
        if name == f"{hero}.{ext}":
            return True
        if out_name == "final":
            for fork in FORK_LABELS:
                if name == f"{published_fork_base(target, fork, False)}.{ext}":
                    return True
            for label in QA_ARTIFACT_LABELS:
                if name == f"{published_qa_base(target, label, False)}.{ext}":
                    return True
    sn = sidecar_names(target, out_name, False)
    return name in (sn["plan"], sn["notes"], sn["run"])


def validate_extra_basename(name: str, target: str, out_name: str) -> bool:
    """The identity's exact expected non-member extras: recipe, qa scorecard,
    annotated JPG (spec §5 step 1 / rev 5 delta -- the annotated name's
    _uncertified token is non-terminal, so it can never pass the generic form)."""
    if not _is_clean_basename(name):
        return False
    sn = sidecar_names(target, out_name, False)
    return name in (sn["recipe"], sn["qa"], annotated_name(target, out_name, False))
