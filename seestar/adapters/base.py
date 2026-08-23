"""Adapter protocol and static registry.

Adapters are the imperative shell around external tools (Siril, GraXpert,
StarNet2, exiftool). This module is the seam between the pure planner
(`seestar.core`) and tool-calling code: adapters may import from
`seestar.core.*` only, never from workspace/orchestration modules
(`tests/test_architecture.py::test_adapters_import_core_only`).
That is why `ExecContext.workspace` is duck-typed `object`: the concrete
Workspace type lives in the shell and would be a forbidden import here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from seestar.core.types import ArtifactRef, Operation  # core-only import (sweep law)

# The four external tools the pipeline shells to (H1 foundation, Task 3). Kept as a
# module-level tuple so ToolTimeouts.__post_init__ and .for_tool() share one
# enumeration instead of drifting.
_TIMEOUT_FIELDS = ("siril", "graxpert", "starnet2", "exiftool")


def _validate_timeout(field_name: str, value: object) -> None:
    """Enforce the settled invariant for every `tools.timeout_s.<tool>` value: a
    positive, finite, non-Boolean number. `bool` is a subclass of `int` in Python,
    so it must be excluded explicitly before the numeric check -- True/False would
    otherwise silently pass as 1/0. Named after the config key (not the bare field
    name) so the same message is correct whether the value came from YAML (via
    `seestar.tools`'s parser) or a direct `ToolTimeouts(...)` construction."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"tools.timeout_s.{field_name} must be a positive finite number, got {value!r}")


@dataclass(frozen=True)
class ToolTimeouts:
    """Wall-clock deadlines (seconds) for each external tool invocation (settled
    decision 3: siril 1800s, graxpert/starnet2 3600s, exiftool 120s). Validated on
    every construction -- direct programmatic use and config-driven parsing both go
    through `__post_init__`, so the invariant can never be bypassed."""

    siril: float = 1800.0
    graxpert: float = 3600.0
    starnet2: float = 3600.0
    exiftool: float = 120.0

    def __post_init__(self) -> None:
        for name in _TIMEOUT_FIELDS:
            _validate_timeout(name, getattr(self, name))

    def for_tool(self, name: str) -> float:
        """Exhaustive lookup -- a typo'd or unknown tool name is a loud ValueError,
        never a silent default or AttributeError."""
        mapping = {f: getattr(self, f) for f in _TIMEOUT_FIELDS}
        try:
            return mapping[name]
        except KeyError:
            raise ValueError(f"unknown tool {name!r}; expected one of {_TIMEOUT_FIELDS}") from None


@dataclass(frozen=True)
class ToolPaths:
    """Resolved executable paths for the external tools the pipeline shells to."""

    siril: str
    graxpert: str
    starnet2: str
    exiftool: str
    timeouts: ToolTimeouts = field(default_factory=ToolTimeouts)
    # Diagnosis 7: where a failing tool's FULL output is written. Set once per
    # run by the shell (the run's output dir); None for call sites with no run
    # directory, which then report the bounded summary alone. It rides on
    # ToolPaths because `tools` is already threaded to every tool wrapper --
    # adding it here beats a new parameter on fifteen call sites, and read via
    # getattr so the duck-typed fakes in the test suite keep working.
    diagnostics_dir: str | None = None


@dataclass
class ExecContext:
    """Everything an adapter may touch, injected by the executor: no globals,
    no shell imports inside adapters."""

    workspace: object  # duck-typed Workspace (path_for/register/lookup)
    tools: ToolPaths | None
    jpg_quality: int = 95
    resolve: dict = field(default_factory=dict)  # ArtifactRef -> concrete path (inputs)
    # Diagnosis 4: tool-evidence channel. An adapter APPENDS parsed PccEvidence
    # here when it runs a pcc=True ColorCalibrate; the orchestrator drains it
    # into the RunOps result so the core can judge it. Adapters never decide.
    pcc_evidence: list = field(default_factory=list)
    # Owner Gate 2: the catalogued angular extent of the intended subject as a
    # fraction of THIS frame, or None when no trustworthy catalogue hit exists.
    # Shell-side only -- it corroborates the subject-support MEASUREMENT and
    # never reaches the core, which still sees only the support summary (so the
    # v1 rule that footprint/morphology never cross the core boundary holds).
    subject_extent_frac: float | None = None


@runtime_checkable
class Adapter(Protocol):
    operations: tuple[type, ...]  # which op types it executes
    name: str

    def execute(self, op: Operation, ctx: ExecContext) -> dict[ArtifactRef, str]: ...

    # returns {output_ref: written_path}; raises AdapterError on tool failure


class AdapterError(RuntimeError):
    pass


REGISTRY: dict[type, Adapter] = {}  # populated by register() calls in each adapter module


def register(adapter: Adapter) -> None:
    for op_type in adapter.operations:
        if op_type in REGISTRY:
            raise ValueError(
                f"{op_type.__name__} already routed to {REGISTRY[op_type].name}; "
                "routing is static - one adapter per operation type"
            )
        REGISTRY[op_type] = adapter


def adapter_for(op: Operation) -> Adapter:
    try:
        return REGISTRY[type(op)]
    except KeyError:
        raise AdapterError(f"no adapter registered for {type(op).__name__}") from None
