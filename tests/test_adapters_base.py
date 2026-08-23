"""Adapter protocol + static registry (Task 12)."""

import typing

import pytest

from seestar.adapters import base as B
from seestar.core.types import ArtifactRef, ArtifactRole, Crop, Operation, PrepareSource


def _ref(name, role=ArtifactRole.PRODUCTION):
    return ArtifactRef(name=name, role=role)


class _FakeAdapter:
    """Minimal stand-in satisfying the Adapter protocol for one op type."""

    def __init__(self, name, op_type):
        self.name = name
        self.operations = (op_type,)

    def execute(self, op, ctx):
        return {}


@pytest.fixture
def scratch_registry(monkeypatch):
    """Give each test a fresh REGISTRY dict so tests don't pollute each other
    (or the real module-level REGISTRY that adapter modules populate)."""
    fresh: dict[type, B.Adapter] = {}
    monkeypatch.setattr(B, "REGISTRY", fresh)
    return fresh


def test_register_two_adapters_for_same_op_type_raises(scratch_registry):
    first = _FakeAdapter("adapter-one", Crop)
    second = _FakeAdapter("adapter-two", Crop)
    B.register(first)
    with pytest.raises(ValueError, match="Crop"):
        B.register(second)


def test_adapter_for_unregistered_op_raises_adapter_error(scratch_registry):
    op = PrepareSource(session_index=0, output=_ref("working_copy", ArtifactRole.SOURCE))
    with pytest.raises(B.AdapterError, match="PrepareSource"):
        B.adapter_for(op)


def test_adapter_for_returns_registered_adapter(scratch_registry):
    adapter = _FakeAdapter("crop-adapter", Crop)
    B.register(adapter)
    op = Crop(
        input=_ref("working_copy", ArtifactRole.SOURCE),
        output=_ref("cropped"),
        x=28,
        y=16,
        width=1864,
        height=1048,
        mirror_x=True,
    )
    assert B.adapter_for(op) is adapter


def test_registry_covers_every_operation_type():
    """Once seestar.adapters is imported, every type in the closed Operation
    union has exactly one adapter registered (Task 13 populated
    seestar/adapters/__init__.py with the five concrete adapter modules)."""
    import seestar.adapters  # noqa: F401 - import for side effect (populates REGISTRY)

    op_types = typing.get_args(Operation)
    missing = [t.__name__ for t in op_types if t not in B.REGISTRY]
    assert not missing, f"operation types with no adapter: {missing}"
