"""Target discovery and the public run entry points.

This module understands the Seestar library layout: a target folder contains one
or more stacked FITS masters, and a library folder contains target folders. It
does not decide how a target should be processed. `run_target` delegates to
`seestar.orchestrate.run_target`, where the planner-driven render flow lives.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .inspect import file_meta, sanitize_label
from .paths import assert_output_disjoint_from_source, safe_output_dir
from .tools import ToolNotFoundError


@dataclass(frozen=True)
class TargetFailure:
    """One target's isolated ordinary-`Exception` failure within a `process_library`
    batch. `error_type` is the exception class name; `message` is a bounded plain
    string (`str(exc)`), never a full traceback (traceback surfacing would leak
    exec-internal detail into a novice-facing CLI report)."""

    target: str
    folder: str
    error_type: str
    message: str


@dataclass(frozen=True)
class BatchRunResult:
    """The typed partial-success report for `process_library` (owner-ratified
    2026-07-31 layering fix: this classification lives HERE, not only in a
    downstream CLI, so no future consumer of `process_library` can silently repeat
    the exit-0-on-QA-failure bug). Each discovered target lands in exactly one of
    three structurally-separate fields, in discovery order:

    - `successes`: `run_target` completed and its result `status` is `"pass"` or
      `"source_limited"` (a `source_limited` result ships a best-effort
      deliverable by design -- a documented DATA limitation, not a processing
      failure -- and counts as SUCCESS, CLAUDE.md's per-target QA contract).
    - `status_failures`: `run_target` completed WITHOUT raising, but its result
      `status` is neither `"pass"` nor `"source_limited"` (e.g. `"processing_failed"`
      -- an M2 fail-closed QA gate returns a result dict rather than raising, so
      exception-only isolation would silently miss this).
    - `failures`: `run_target` raised an ordinary `Exception`, isolated as a typed
      `TargetFailure`.

    `failures` stays exception-only and `status_failures` stays exception-free so
    the two failure kinds remain distinguishable; `ok` is the one true/false
    summary for a consumer that only cares whether the whole batch is clean."""

    successes: tuple[dict, ...]
    failures: tuple[TargetFailure, ...]
    status_failures: tuple[dict, ...]

    @property
    def ok(self) -> bool:
        """True iff every discovered target succeeded (`"pass"` or
        `"source_limited"`) -- no exception failures and no status failures."""
        return not self.failures and not self.status_failures


def _session_fits(folder: str) -> tuple[list[str], list[dict]]:
    """Return valid Seestar stacked FITS masters in `folder`.

    Discovery is based on FITS headers, not filenames: any `.fit`/`.fits` file
    with Seestar creator metadata, a 3-channel color stack, and `STACKCNT`
    qualifies. Non-qualifying FITS files are returned in `skipped` with a
    reason so callers can report them instead of silently ignoring them.
    """
    from .inspect import is_seestar_stack, read_header

    stacks: list[str] = []
    skipped: list[dict] = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return [], []
    for n in names:
        if not n.lower().endswith((".fit", ".fits")):
            continue
        p = os.path.join(folder, n)
        try:
            ok, reason = is_seestar_stack(read_header(p))
        except Exception as e:
            ok, reason = False, f"unreadable FITS ({e})"
        if ok:
            stacks.append(p)
        else:
            skipped.append({"path": p, "reason": reason})
    return stacks, skipped


def _find_raw(target_dir: str) -> list[str]:
    stacks, skipped = _session_fits(target_dir)
    if not stacks:
        why = "; ".join(f"{os.path.basename(s['path'])}: {s['reason']}" for s in skipped)
        raise FileNotFoundError(f"no Seestar stacked FITS in {target_dir}" + (f" (skipped: {why})" if why else ""))
    return stacks


def discover_targets(library_dir: str) -> dict:
    """Find renderable targets in a Seestar library or single-target folder.

    If `library_dir` itself contains stacked masters, it is treated as one
    target. Otherwise, each immediate subfolder that contains stacked masters is
    treated as a target. Target names come from the FITS `OBJECT` header when
    available, then the filename fallback, then the folder name.
    """

    def info(folder: str) -> tuple[dict | None, list[dict]]:
        stacks, skipped = _session_fits(folder)
        if not stacks:
            return None, skipped
        metas = {f: file_meta(f) for f in stacks}
        latest = max(stacks, key=lambda f: metas[f].get("datetime") or "")
        m = metas[latest]
        return {
            "target": m.get("target") or sanitize_label(os.path.basename(os.path.normpath(folder))),
            "folder": folder,
            "sessions": len(stacks),
            "latest": os.path.basename(latest),
            "latest_path": latest,
            "filter": m.get("filter"),
            "datetime": m.get("datetime"),
        }, skipped

    here, here_skipped = info(library_dir)
    if here:
        return {"targets": [here], "skipped": here_skipped}
    targets, skipped = [], list(here_skipped)
    for name in sorted(os.listdir(library_dir)):
        sub = os.path.join(library_dir, name)
        if os.path.isdir(sub):
            ti, sk = info(sub)
            skipped.extend(sk)
            if ti:
                targets.append(ti)
    return {"targets": targets, "skipped": skipped}


def run_target(
    target_dir: str,
    output_dir: str,
    cfg: dict,
    profile: str = "showcase",
    debug: bool = False,
    overrides: dict | None = None,
    keep_scratch: bool = False,
    catalog_prior: bool | None = None,
    out_name: str = "final",
    recipe_path: str | None = None,
    on_stage=None,
) -> dict:
    """Render the target whose Seestar stacked masters live in `target_dir`.

    The Seestar import is read-only. Working files, scratch files, sidecars, and
    deliverables are written under `output_dir`; `process_library` uses one
    output subfolder per discovered target.

    `out_name` names the single (non-fork) hero deliverable `{target}_{out_name}`.
    It must be "final" (the showcase mapping) or a non-showcase profile name
    (core.publication.allowed_out_names()); anything else raises ValueError --
    arbitrary names collide with fork and uncertified naming (publication spec
    2026-08-13 §1).

    This wrapper preserves the public `seestar.run.run_target` import path while
    delegating the actual render orchestration to `seestar.orchestrate`.
    """
    from . import orchestrate

    return orchestrate.run_target(
        target_dir,
        output_dir,
        cfg,
        profile=profile,
        debug=debug,
        overrides=overrides,
        keep_scratch=keep_scratch,
        catalog_prior=catalog_prior,
        out_name=out_name,
        recipe_path=recipe_path,
        on_stage=on_stage,
    )


def process_library(
    library_dir: str, output_root: str, cfg: dict, profile: str = "showcase", **kwargs
) -> BatchRunResult:
    """Discover every target under `library_dir` (S30 native layout) and render each into
    `<output_root>/<target>/`. The S30 import is never written to, so re-copying a target
    from the device can't clobber renders. `kwargs` pass through to `run_target`
    (debug, catalog_prior, out_name, keep_scratch, ...).

    An ordinary exception from one target's `run_target` call is isolated: it does
    not abort the batch, and later targets are still attempted. Discovery, the
    root-level source/output disjointness guard, and each target's output-dir
    containment check (`safe_output_dir`) stay OUTSIDE this per-target recovery
    boundary -- those are path-safety/global-prerequisite concerns, not per-target
    processing outcomes, and a violation aborts the whole batch. `ToolNotFoundError`
    is likewise a global prerequisite failure (a missing helper application, not a
    per-target problem) and is re-raised rather than isolated, so it is not repeated
    once per remaining target. `BaseException` subclasses other than ordinary
    `Exception` (`KeyboardInterrupt`, `SystemExit`, ...) are never caught here.

    A `run_target` call that completes WITHOUT raising is then classified by its
    result `status` (owner decision, 2026-07-31 layering fix -- classification
    lives here, not only in a downstream CLI): `"pass"`/`"source_limited"` land in
    `successes`, anything else (e.g. `"processing_failed"`) lands in the
    structurally separate `status_failures` field rather than being conflated with
    an exception `TargetFailure`.

    Returns a `BatchRunResult` with one entry (in discovery order) in exactly one
    of `successes` (the result dict `run_target` returned, status `"pass"` or
    `"source_limited"`), `status_failures` (the result dict, any other status), or
    `failures` (a typed `TargetFailure`, from an isolated exception) per
    discovered target."""
    assert_output_disjoint_from_source(library_dir, output_root)
    successes: list[dict] = []
    status_failures: list[dict] = []
    failures: list[TargetFailure] = []
    for t in discover_targets(library_dir)["targets"]:
        out_dir = safe_output_dir(output_root, t["target"])
        try:
            result = run_target(t["folder"], out_dir, cfg, profile=profile, **kwargs)
        except ToolNotFoundError:
            raise
        except Exception as exc:
            failures.append(
                TargetFailure(
                    target=t["target"],
                    folder=t["folder"],
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
            )
        else:
            if result.get("status") in ("pass", "source_limited"):
                successes.append(result)
            else:
                status_failures.append(result)
    return BatchRunResult(
        successes=tuple(successes),
        failures=tuple(failures),
        status_failures=tuple(status_failures),
    )
