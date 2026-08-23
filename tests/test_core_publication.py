"""Decision table + naming/grammar laws for seestar/core/publication.py (spec rev 6 §§1-3,5)."""

from __future__ import annotations

import pytest

from seestar.core.policy import PROFILE_OVERLAYS
from seestar.core.publication import (
    FORK_LABELS,
    RESERVED_TOKENS,
    allowed_out_names,
    annotated_name,
    decide_publication,
    published_base,
    published_fork_base,
    sidecar_names,
    validate_extra_basename,
    validate_member_basename,
    validate_out_name,
)
from seestar.core.types import HeroOutcome, PublicationIdentity, PublicationNamespace


@pytest.mark.parametrize("outcome", [HeroOutcome.PASS, HeroOutcome.SOURCE_LIMITED])
@pytest.mark.parametrize("out_name", ["final", "print"])
def test_certified_outcomes_publish_certified_and_clean_same_out_name(outcome, out_name):
    d = decide_publication(outcome, out_name)
    assert d.certified is True
    assert d.namespace is PublicationNamespace.CERTIFIED
    assert d.identity == PublicationIdentity(out_name, PublicationNamespace.CERTIFIED)
    assert d.cleans == PublicationIdentity(out_name, PublicationNamespace.UNCERTIFIED)


@pytest.mark.parametrize("out_name", ["final", "documentation"])
def test_processing_failed_publishes_uncertified_and_cleans_nothing_else(out_name):
    d = decide_publication(HeroOutcome.PROCESSING_FAILED, out_name)
    assert d.certified is False
    assert d.namespace is PublicationNamespace.UNCERTIFIED
    assert d.identity == PublicationIdentity(out_name, PublicationNamespace.UNCERTIFIED)
    assert d.cleans is None


def test_allowed_out_names_is_final_plus_non_showcase_profiles():
    assert allowed_out_names() == frozenset({"final"} | (set(PROFILE_OVERLAYS) - {"showcase"}))


@pytest.mark.parametrize("bad", ["starless", "uncertified", "showcase", "stars-on", "x/../y", ""])
def test_validate_out_name_rejects_unsafe_values(bad):
    assert validate_out_name(bad) != []


def test_validate_out_name_accepts_the_safe_set():
    for name in allowed_out_names():
        assert validate_out_name(name) == []


def test_allowlist_disjoint_from_fork_labels_and_reserved_tokens():
    assert allowed_out_names().isdisjoint(FORK_LABELS)
    assert allowed_out_names().isdisjoint(RESERVED_TOKENS)


def test_published_names_final_identity():
    assert published_base("M", "final", True) == "M_final"
    assert published_base("M", "final", False) == "M_uncertified"
    assert published_fork_base("M", "starless", False) == "M_starless_uncertified"
    assert sidecar_names("M", "final", False) == {
        "plan": "M_uncertified_plan.json",
        "notes": "M_uncertified_notes.json",
        "run": "M_uncertified_run.json",
        "recipe": "M_uncertified_recipe.json",
        "qa": "M_uncertified_qa.json",
    }
    assert sidecar_names("M", "final", True)["run"] == "M_run.json"
    assert sidecar_names("M", "final", True)["recipe"] == "M_recipe.json"


def test_published_names_non_final_identity():
    assert published_base("M", "print", False) == "M_print_uncertified"
    assert sidecar_names("M", "print", False)["run"] == "M_print_uncertified_run.json"
    assert annotated_name("M", "documentation", False) == "M_documentation_uncertified_annotated.jpg"
    assert annotated_name("M", "documentation", True) == "M_documentation_annotated.jpg"


@pytest.mark.parametrize(
    "good",
    [
        "M_uncertified.tif",
        "M_uncertified.jpg",
        "M_starless_uncertified.tif",
        "M_stars-on_uncertified.png",
        "M_closeup_uncertified.jpg",
        "M_uncertified_plan.json",
        "M_uncertified_notes.json",
        "M_uncertified_run.json",
    ],
)
def test_validator_accepts_final_identity_members(good):
    assert validate_member_basename(good, "M", "final") is True


@pytest.mark.parametrize(
    "bad",
    [
        "M_final.tif",  # certified name — lacks _uncertified token
        "M_print_uncertified.tif",  # another identity's hero (fork position abuse)
        "M_uncertified.fit",  # extension not in allowlist
        "M_notes_uncertified.json",  # token order — not an exact JSON name
        "M_evil_uncertified.tif",  # fork token outside the closed set
        "sub/M_uncertified.tif",  # path separator
        "M_uncertified.tif.prev_ab12cd34",  # .prev_ token
        "/abs/M_uncertified.tif",  # absolute
        "M_uncertified_qa.json",  # qa/recipe are extras, not manifest members
    ],
)
def test_validator_rejects_bad_members(bad):
    assert validate_member_basename(bad, "M", "final") is False


def test_fork_grammar_is_final_identity_only():
    """Red-team finding 15: planning emits forks/QA-starless only for canon (out_name
    'final'); a print-identity manifest must not be able to name the final identity's
    fork files."""
    assert validate_member_basename("M_starless_uncertified.tif", "M", "print") is False
    assert validate_member_basename("M_print_uncertified.tif", "M", "print") is True


def test_extras_clause_accepts_exactly_recipe_qa_annotated():
    assert validate_extra_basename("M_uncertified_recipe.json", "M", "final") is True
    assert validate_extra_basename("M_uncertified_qa.json", "M", "final") is True
    assert validate_extra_basename("M_documentation_uncertified_annotated.jpg", "M", "documentation") is True
    assert validate_extra_basename("M_uncertified.tif", "M", "final") is False
