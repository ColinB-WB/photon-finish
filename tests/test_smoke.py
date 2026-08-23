"""Smoke tests. Real tests will assert stat ranges on a sample FITS once the
engine stages are implemented."""

import seestar


def test_version():
    assert seestar.__version__


def test_profiles_listed():
    # profiles.py deleted (Task 23); core.policy.PROFILE_OVERLAYS is the production
    # single source of truth for the profile name set.
    from seestar.core.policy import PROFILE_OVERLAYS

    assert {"showcase", "natural", "documentation", "print"} <= set(PROFILE_OVERLAYS)
