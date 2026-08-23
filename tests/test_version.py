"""The runtime version and the installed package metadata must agree.

Recipes stamp ``seestar.__version__`` into ``engine_version`` — a drift between
``pyproject.toml`` and ``seestar/__init__.py`` makes every emitted recipe claim
the wrong engine (found by an external review: metadata said 3.0.0, runtime 2.0.0).
``pyproject.toml`` now derives its version from ``seestar.__version__``
(``[tool.setuptools.dynamic]``), so a mismatch here means the editable install is
stale — re-run ``pip install -e .`` after a version bump.
"""

from __future__ import annotations

import importlib.metadata

import seestar


def test_runtime_version_matches_package_metadata():
    assert importlib.metadata.version("photon-finish") == seestar.__version__


def test_license_is_spdx_mit():
    meta = importlib.metadata.metadata("photon-finish")
    val = meta.get("License-Expression") or meta.get("License")
    assert val and "MIT" in val
