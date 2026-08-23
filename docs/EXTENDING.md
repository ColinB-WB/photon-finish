# Extending the engine — the adapter contract

> Audience: a contributor (human or agent) adding support for a new tool. If you
> are trying to understand the pipeline's overall architecture first, read
> AGENTS.md's "Architecture" section, then come back here.

The current architecture splits the engine into a pure functional core
(`seestar/core/`) that decides *what* to do, and an imperative shell of
**adapters** (`seestar/adapters/`) that know *how* to do it by shelling out to
a specific tool (Siril, GraXpert, StarNet2, exiftool). This document is the
contract for adding a new adapter — routing an existing operation to a
different tool — and for recognizing when what you actually want is a core
change instead.

## The Adapter protocol + static registry

Every tool integration implements the `Adapter` protocol
(`seestar/adapters/base.py`):

```python
@runtime_checkable
class Adapter(Protocol):
    operations: tuple[type, ...]  # which op types it executes
    name: str

    def execute(self, op: Operation, ctx: ExecContext) -> dict[ArtifactRef, str]: ...
    # returns {output_ref: written_path}; raises AdapterError on tool failure
```

`Operation` is the closed 14-member union defined in `seestar/core/types.py`
(`PrepareSource`, `CombineSessions`, `Crop`, `RemoveTrail`, `BackgroundExtract`,
`Denoise`, `ColorCalibrate`, `StretchRender`, `SeparateStars`, `ScreenBlend`,
`CropTo`, `Export`, `EmbedMetadata`, `Annotate`). All 14 are registered and
executable through the adapter protocol below — but `EmbedMetadata`/`Annotate`
are never *emitted by the planner* into a `Plan`. That's deliberate, not a gap:
both are terminal side effects (`outputs == ()`, nothing downstream consumes
them) with no reuse or determinism benefit from Plan-tracking, so
`seestar/orchestrate.py` calls them directly as shell post-processing on the
Plan's finished deliverables instead (see
`docs/history/superpowers/plans/2026-07-13-fp-core-decisions.md` for the full
rationale). Don't "fix" this by wiring them into `core/planning.py` without
re-reading that decision first. Adapters are registered into a
single static `REGISTRY: dict[type[Operation], Adapter]` by explicit
`register()` calls made at import time from each adapter module — there is
**no dynamic discovery, no plugin scanning, no drop-in directory**. The
registry is populated once, when `seestar/adapters/__init__.py` imports each
concrete adapter module for its side effect:

```python
# seestar/adapters/__init__.py
from . import builtin, exiftool, graxpert, siril, starnet  # noqa: F401
```

Routing is **static and exclusive**: exactly one adapter is registered per
operation type. `register()` raises `ValueError` if a second adapter tries to
claim an operation type already routed elsewhere, and
`tests/test_adapters_base.py` asserts *completeness* — every member of
`typing.get_args(Operation)` must have a registered adapter, using the real
`Operation` union as the source of truth so the test can't silently drift out
of sync with `core/types.py`.

Adapters may only import from `seestar.core.*` — never from
`seestar.workspace`, `seestar.orchestrate`, or any other shell module
(`tests/test_architecture.py::test_adapters_import_core_only` enforces this).
That's why `ExecContext.workspace` is typed as plain `object`: the concrete
`Workspace` type lives in the shell and importing it here would violate the
seam.

## Worked example: routing `Denoise` to a hypothetical PixInsight adapter

Say you want denoise to run through PixInsight's CLI instead of GraXpert. The
`Denoise` operation type already exists (`seestar/core/types.py`) with fields
`input: ArtifactRef`, `output: ArtifactRef`, `strength: float`. You do not
touch the union, planning, or QA — you write one new adapter module modeled on
the real shape of `seestar/adapters/graxpert.py`'s `GraxpertAdapter`:

```python
# seestar/adapters/pixinsight.py
"""PixInsight denoise adapter: routes the existing Denoise operation to
PixInsight's CLI instead of GraXpert.
"""

from __future__ import annotations

import os

from seestar.core.types import Denoise

from ._external_process import run_external
from .base import AdapterError, ExecContext, ToolPaths, register


def _pixinsight_denoise(input_path: str, out_path: str, strength: float, tools: ToolPaths) -> str:
    r = run_external(
        [
            tools.pixinsight,
            "--denoise",
            "--strength", str(strength),
            "--input", os.path.abspath(input_path),
            "--output", os.path.abspath(out_path),
        ],
        tool="pixinsight",
        tools=tools,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if not os.path.exists(out_path):
        raise RuntimeError(f"PixInsight denoise produced no output:\n{r.stdout}\n{r.stderr}")
    return out_path


class PixInsightAdapter:
    """Executes Denoise by shelling to PixInsight's CLI."""

    operations = (Denoise,)
    name = "pixinsight"

    def execute(self, op, ctx: ExecContext) -> dict:
        if not isinstance(op, Denoise):
            raise AdapterError(f"PixInsightAdapter cannot execute {type(op).__name__}")
        input_path = ctx.resolve[op.input]
        out_path = ctx.workspace.path_for(op.output, ".fits")
        produced = _pixinsight_denoise(input_path, out_path, op.strength, ctx.tools)
        return {op.output: produced}


register(PixInsightAdapter())
```

To actually route `Denoise` here instead of to GraXpert, you'd need to change
which module `seestar/adapters/__init__.py` imports for that operation (today
it's an all-or-nothing import of `graxpert`; splitting `Denoise` off to a
different tool while keeping `BackgroundExtract` on GraXpert means either
`graxpert.py` drops `Denoise` from its `operations` tuple, or you add a
selection mechanism — see below). That routing decision, and extending
`ToolPaths` with a `pixinsight` field and wiring `resolve_tools`, are
mechanical shell changes outside the scope of this worked example.

**Every adapter call must go through `run_external`, never a bare
`subprocess.run`/`subprocess.Popen`.** `seestar/adapters/_external_process.py`
is the ONE place in the adapter layer allowed to own a subprocess call — it
enforces a bounded wall-clock deadline (`tools.timeouts.for_tool(tool)`) and
kills the whole process tree it launched on timeout, so a hung or slow tool
call can never hang the rest of the pipeline. `run_external`'s signature is a
drop-in `subprocess.run` replacement (`capture_output`, `text`, `encoding`,
`errors`, `check` all behave identically); the two additions are the required
`tool=` keyword (the tool name key from `ToolTimeouts`, so the runner can look
up its deadline and name it in a timeout error) and `tools=` (the resolved
`ToolPaths`). A new tool needs a **reviewed timeout default**: add a field to
`ToolTimeouts` (`seestar/adapters/base.py`) alongside `siril`/`graxpert`/
`starnet2`/`exiftool`, with a value chosen the same deliberate way those four
were (how long the real tool can legitimately take, not an arbitrary guess).
Direct subprocess ownership in an adapter is an **architecture violation**,
caught by `tests/test_architecture.py`'s AST scan of the four concrete adapter
modules (`test_processing_adapters_own_subprocess_only_through_the_bounded_runner`)
— a new adapter module isn't in that scan's hard-coded list yet, so add it
there when you add the adapter, the same way the sweep tests in that file ask
every new processing module to opt in explicitly.

## The scope law

This document only covers one kind of change. The rule, verbatim:

> implementing an existing operation's semantics with a different tool; an
> operation with genuinely new semantics is a core change — union, planning
> policy, QA evidence, possibly recipe surface — by design, not by omission.

In other words: if PixInsight denoise produces the *same kind of thing* a
`Denoise` operation already promises (a denoised artifact, same inputs/outputs,
same place in the plan), a new adapter is the whole change. But if you want to
add something the engine has never modeled — a new *kind* of operation (say, a
deconvolution stage, or a multi-frame drizzle step) — that is **not** an
adapter-only change, even if it's implemented by shelling out to a tool in
exactly the same style as the example above. A genuinely new operation needs:

- a new member in the `Operation` union (`seestar/core/types.py`), with its
  own frozen dataclass and `StageKey`;
- a planning decision for when/whether it appears in a `Plan`
  (`seestar/core/planning.py` — and its failure-policy entry in `advance()`
  if it can fail);
- QA evidence if the operation changes anything a gate should measure
  (`seestar/core/qa_rules.py`);
- possibly a recipe-surface entry if a user should be able to pin it
  (`seestar/recipe.py`'s `ADJUSTMENTS` registry).

Do not smuggle new semantics into an adapter by, e.g., having a `Denoise`
adapter secretly also sharpen, or having a `Crop` adapter also remove a trail.
The adapter's job is to execute the operation exactly as the core defined it;
anything an adapter does beyond that is invisible to planning, QA, and the
recipe surface, and defeats the entire point of the split.

## The evidence bar

An adapter is not "done" because it runs without raising. Before routing a new
adapter in for real use (i.e. before it becomes the live route for any
operation type on any target):

1. Run **`seestar validate`** with your adapter routed in. `validate.py`
   re-renders the reference targets and diffs the resulting QA scorecards
   against the committed baselines within tolerance — including the
   orientation-vs-baseline hard gate that has already caught one real
   regression (a vertical flip in a FITS→TIFF boundary) that every
   rotation-invariant metric missed. A new adapter must clear this the same
   way any core change must.
2. **Present reference renders before any routing change** — actual images
   from the reference target set (§4 of AGENTS.md: M81, M8, M17, M5, M13,
   NGC 5128, NGC 5139), not just passing numbers. QA gates catch
   catastrophes; they do not certify that a differently-toned denoise or a
   differently-behaved background extraction still looks *right* to a human.
   Numeric parity with the old adapter is not the bar — the bar is "does this
   still produce a great image," judged the same way every other engine
   change in this project is judged (AGENTS.md §13's judgment layer).

Do not flip the static registry to point at a new adapter for real use until
both of these have been done and reported.

## Future: adapter selection (not built)

Today, routing is exclusive and static — one adapter per operation type,
chosen at import time, with no way for a user to pick between two adapters
that both implement the same operation. This is deliberate: with only one real
adapter per operation, a selection mechanism would be speculative plumbing
with nothing to select between.

The recorded intent, to build **only when a second real adapter exists for
the same operation type** (e.g. a working PixInsight `Denoise` adapter
alongside GraXpert's): a word-valued adjustment (in the same family as the
existing preferences/recipe vocabulary, not a raw tool-path string) selecting
which adapter handles that operation, with the same precedence chain the rest
of the engine already uses — **engine default < `preferences.yaml` <
recipe pin**. Until that second adapter exists, this is a note for the future,
not a feature to build ahead of need.
