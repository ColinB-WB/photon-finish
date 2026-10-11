# Change-Safety Rules

This is the **canonical, tool-neutral** home of the project's change-safety rules. Agent entry
points (`AGENTS.md`, `CLAUDE.md`) require reading this file before design or implementation work;
they must link here, never duplicate the rules (duplicates drift).

These rules operationalize the project tenets — chiefly *Trust requires bounded repeatability*
(a step that silently changes, skips, or degrades is a defect) and *Source data is sacred* — as
enforceable development mechanics. [`docs/TENETS.md`](./TENETS.md) remains the sole definition of
the tenets themselves.

**Preferred enforcement ladder: invariant tests → project rules → independent review.** Encode
known failure classes in invariant tests, govern broader behavior with project rules, and reserve
independent review for novel or high-risk judgment. Tests enforce rules; they do not supersede the
tenets, and a test that encodes the wrong invariant must be corrected. (Provenance: the 2026-07
public-release remediation, where a four-round-reviewed plan still leaked a relocated-write
ordering defect that only owner review caught.)

Apply these rules proportionally. They are mandatory for source safety, destructive behavior,
persistence, transactions, security boundaries, public interfaces, and release packaging. For
documentation, test-only maintenance, and mechanical changes, apply the relevant rules without
creating ceremonial evidence for risks that are absent.

## The rules

1. **Inspect in-repository callers and documented public interfaces before changing an
   interface.** Before changing an existing function, helper, return value, or test fixture,
   search for and inspect every in-repository caller and every documented public interface.
   Preserve the interface or list every required caller update. Handle callers that cannot be
   enumerated through compatibility, versioning, or an explicitly documented breaking change.

2. **Audit execution order when relocating a write or side effect.** Trace the complete execution
   order from the old location through function return. Identify every later mutation of the
   serialized object before moving the write.

3. **Multi-file publication must define its transaction explicitly:** the complete transaction
   set; the commit point; the rollback behavior; first-run behavior; prior-run behavior; and the
   hard-crash limitation.

4. **Fully assemble state before serializing it.** After a sidecar or manifest commits, do not
   mutate objects represented by that committed file.

5. **Failure handling is tiered.** Classify every new or modified `except` during implementation
   and review; add a code comment only when the tier or behavior is not obvious:
   - **Mandatory writes** (anything a committed manifest references) must fail loudly: propagate
     and roll back. Never `except OSError: pass` on a publication write.
   - **Rollback failures** must be reported while preserving the original exception — collected
     and chained (e.g. via `raise ... from` or an exception group / appended context), never
     silently ignored. A concealed incomplete rollback is worse than a loud one.
   - **Post-commit cleanup failures** (e.g. removing this run's backups) must produce a warning —
     leftover backups affect later runs.
   - Only **truly incidental cleanup** (scratch files with no effect on later runs) may be
     silently best-effort.

6. **Creating or changing transaction behavior requires the full transaction test matrix:**
   every failure boundary; a first run with no backups; replacement of a prior accepted run;
   multi-variant/fork output; successful publication; and preservation of unexplained crash
   backups.

7. **Test persisted state, not only return values.** If a function returns information also
   written to disk, assert that the returned and persisted representations agree — comparing the
   serialized contract (round-trip through the same serializer, so `default=str` coercions and
   wrapper shapes are honored).

8. **Assert every promised surface independently.** When a requirement promises multiple surfaces
   (e.g. CLI stderr AND a GUI-visible field), assert each on its own. Never `A or B` when both A
   and B are required.

9. **No "confirm at implementation" for discoverable facts.** A finalized plan must not defer
   facts discoverable from the repository. Inspect the actual signature, caller, consumer, or GUI
   route before approving the plan. Implementation may mechanically re-verify facts that could
   have changed because of earlier planned tasks; reopen the design only if the result changes
   behavior, scope, or safety. Deferrals are acceptable only for facts that genuinely cannot exist
   yet — and then the plan must say what to build, not assume what exists.

10. **Every new TDD behavior test appears in the documented red and green commands.** The red run
    must fail for the intended missing behavior — not a collection failure, unless the new module
    itself is the feature. Tests added for behavior the code already satisfies are **regression /
    characterization tests**, not TDD: document their green command, label them accurately, and
    identify the defect or mutation they guard against instead of manufacturing an artificial red
    run.

11. **Tasks sharing production or test files run sequentially in the same worktree.** One clean,
    explicit commit per task (`git add` with named files; never `git add -A` in a worktree with
    untracked maintainer files).

12. **Review and evidence are proportional to risk.** Source safety, destructive operations,
    persistence, transactions, security boundaries, public interfaces, and release packaging
    require independent review before dependent work continues. Documentation, comments,
    test-only maintenance, metadata, and mechanical changes may be batch-reviewed and need only
    the validation relevant to their actual risk.

13. **Review the complete diff before every commit.** Compare `git diff` against the intended
    change list; only intended hunks may be present. Editing tools can operate on stale file
    snapshots — the diff against the real tree is the ground truth that catches it.

## Workflow

- Keep independent checkpoints for source safety, transaction logic, security, and packaging.
- Batch mechanical cleanups, documentation changes, and already-decided dead-code removals.
- Every high-risk task report includes: the commit, changed files, relevant validation evidence,
  deviations, and any new behavior. Mechanical task reports may omit inapplicable red/green
  evidence.
- Stop only for contradictions, unexpected behavior changes, or new owner decisions.
- **A push is not finished until CI is green.** Watch the run to completion after every push
  (`gh run watch <id> --exit-status`; get the id from `gh run list --branch main --limit 1
  --workflow tests`), and fix or revert a red `main` before starting anything else. Never
  report a push as done on the strength of a local suite alone.
- **A green local suite does not predict CI.** Tests install `.[dev,gui]`; the `desktop` extra
  (pywebview) is deliberately excluded because it drags pyobjc on macOS — but a dev venv
  usually *has* it. A test that reaches an optional import therefore passes locally and fails
  everywhere else: on 2026-08-31 exactly this turned all five jobs red while the suite was
  green on the author's machine. When a change touches code with an optional import, re-run
  the suite with that module made unimportable (a `sys.meta_path` hook that raises for it)
  before pushing.

## Invariant tests

Rules whose violation is machine-checkable are enforced as tests (see rule 7 and rule 6):

- **Persisted-vs-returned notes:** after a recipe replay, the returned notes must equal the
  `notes` object persisted in `{target}_notes.json` (serialized-contract comparison) —
  `tests/test_run_recipe.py`.
- **Manifest referential integrity:** every path referenced by a committed manifest must exist,
  and — per publication identity — its deliverables ∪ qa_artifacts ∪ named extras must match
  that identity's on-disk transactional output set — `tests/test_source_safety_rerun.py`.
