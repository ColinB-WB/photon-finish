"""Pure Plan validation.

Checks that every consumed artifact was produced earlier, every deliverable is
produced, no artifact is produced twice, no scratch artifact is orphaned, and no
planning-only probe artifact appears in the final Plan.
"""

from __future__ import annotations

from .types import ArtifactRef, ArtifactRole, Crop, Plan


def validate_plan(plan: Plan) -> tuple[str, ...]:
    errors: list[str] = []
    produced: set[ArtifactRef] = set()
    consumed: set[ArtifactRef] = set()
    byproducts: set[ArtifactRef] = set()

    for i, op in enumerate(plan.steps):
        # a degenerate crop rectangle (negative origin or non-positive size) can
        # only come from a broken crop decision -- fail the plan loudly
        if isinstance(op, Crop) and (op.x < 0 or op.y < 0 or op.width <= 0 or op.height <= 0):
            errors.append(f"step {i} (Crop) has an invalid rectangle x={op.x} y={op.y} w={op.width} h={op.height}")
        for ref in op.inputs:
            if ref not in produced:
                errors.append(f"step {i} ({type(op).__name__}) consumes '{ref.name}' not produced by an earlier step")
            consumed.add(ref)
        # byproducts: outputs the tool emits whether or not the plan wants them
        # (e.g. starnet2's stars layer) -- exempt from the orphan check below.
        # Ops without the property default to no byproducts.
        byproducts.update(getattr(op, "byproducts", ()))
        for ref in op.outputs:
            if ref in produced:
                errors.append(f"'{ref.name}' produced twice")
            produced.add(ref)
            if ref.role is ArtifactRole.PROBE:
                errors.append(f"probe artifact '{ref.name}' in a final Plan (probes are planning-only)")

    for ref in plan.deliverables:
        if ref not in produced:
            errors.append(f"deliverable '{ref.name}' is never produced")

    for ref in produced - consumed - byproducts:
        if ref.role in (ArtifactRole.SOURCE, ArtifactRole.PRODUCTION, ArtifactRole.PROBE):
            errors.append(f"orphan artifact '{ref.name}' ({ref.role.value}): produced but never consumed")

    return tuple(errors)
