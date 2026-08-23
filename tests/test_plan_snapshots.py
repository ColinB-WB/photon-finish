"""Plan snapshot tests (Task 20).

Locks the EXACT JSON shape of the Plan the planner produces for the three
reference archetypes (nebula LP showcase three-look, IRCUT galaxy via prior
with closeup, IRCUT concentrated cluster). Reuses the `drive()` helper and
`Responder` synthetic tool from test_core_planning.py -- no tools, no files,
purely-driven planning -- instead of duplicating them.

Why a snapshot, and why the env var is the review gate (spec §2): the Plan is
the pipeline's central artifact -- changing what steps/deliverables/solve
provenance it emits for a locked reference case is a real behavioral change,
not a numeric knob, and deserves the same scrutiny as any other regression. A
plain assertion would only catch what someone thought to assert; a full-plan
snapshot catches EVERYTHING, including changes nobody thought to check for.
So: a missing snapshot is written on first run (letting a new archetype be
added) but the run that wrote it still FAILS -- "snapshot written" must never
be silently mistaken for "test passed" in CI, which cannot set the update
env var and so can never manufacture a passing baseline for itself. To
intentionally change a Plan, a developer re-runs locally with
SEESTAR_UPDATE_SNAPSHOTS=1, inspects the diff (git diff on the regenerated
JSON), and commits the new file as a reviewed decision. Every other run
compares byte-for-byte and fails with a unified diff on any mismatch.
"""

from __future__ import annotations

import difflib
import json
import os
import re

import pytest
from test_core_planning import Responder, _req, drive

from seestar.core.types import Category, FilterKind, PriorObservation
from seestar.io import plan_to_dict

SNAP_DIR = os.path.join(os.path.dirname(__file__), "snapshots")

# name -> () -> (request, responder)
_ARCHETYPES = {
    "nebula_lp_showcase": lambda: (_req(), Responder()),
    "galaxy_ircut_prior_closeup": lambda: (
        _req(target_label="NGC5128", filter_kind=FilterKind.IRCUT, has_wcs=True, use_prior=True),
        Responder(prior=PriorObservation(catalog_category=Category.GALAXY, fov_fraction=0.1)),
    ),
    "cluster_ircut_concentrated": lambda: (
        _req(target_label="M5", filter_kind=FilterKind.IRCUT, has_wcs=False, use_prior=False),
        Responder(concentration=60.0),
    ),
}

# Determinism guards (spec: a driven Plan is a pure function of typed inputs --
# no path or timestamp may leak into its JSON serialization).
_ABS_PATH_RE = re.compile(r"[A-Za-z]:[\\/]|/(?:Users|home|tmp)/")
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def _snapshot_json(name):
    request, responder = _ARCHETYPES[name]()
    outcome = drive(request, responder)
    assert type(outcome).__name__ == "PlanComplete", f"{name}: planning did not complete: {outcome}"
    text = json.dumps(plan_to_dict(outcome.plan), indent=2, sort_keys=True)
    assert not _ABS_PATH_RE.search(text), f"{name}: absolute path leaked into Plan JSON:\n{text}"
    assert not _TIMESTAMP_RE.search(text), f"{name}: timestamp leaked into Plan JSON:\n{text}"
    return text + "\n"  # match pre-commit end-of-file-fixer's single trailing newline


def _compare_or_write(name, actual):
    path = os.path.join(SNAP_DIR, f"plan_{name}.json")
    existed = os.path.exists(path)
    update = os.environ.get("SEESTAR_UPDATE_SNAPSHOTS") == "1"
    if not existed or update:
        os.makedirs(SNAP_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(actual)
        if not existed:
            pytest.fail(f"snapshot missing -- wrote {path}; re-run to verify, then commit it")
        return  # intentional update (SEESTAR_UPDATE_SNAPSHOTS=1 with an existing file): done
    with open(path, encoding="utf-8") as f:
        expected = f.read()
    if actual.rstrip("\n") != expected.rstrip("\n"):
        diff = "\n".join(
            difflib.unified_diff(
                expected.splitlines(),
                actual.splitlines(),
                fromfile=f"{path} (committed)",
                tofile=f"{path} (actual)",
                lineterm="",
            )
        )
        pytest.fail(
            f"Plan snapshot for '{name}' changed. If this is intentional, re-run with "
            f"SEESTAR_UPDATE_SNAPSHOTS=1, review the diff, and commit the new snapshot "
            f"(spec §2 -- Plan changes are reviewed, never silent):\n{diff}"
        )


@pytest.mark.parametrize("name", sorted(_ARCHETYPES))
def test_plan_snapshot(name):
    _compare_or_write(name, _snapshot_json(name))
