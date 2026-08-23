"""Adapter registry package.

Importing this package populates `base.REGISTRY` by importing each concrete
adapter module for its side effect (`register(...)` calls) — explicit
imports, no discovery/plugin scanning.
"""

from . import builtin, exiftool, graxpert, siril, starnet  # noqa: F401
