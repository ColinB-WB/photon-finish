# Developer guide

The public manual explains how to use the application. This section explains
how to maintain it without weakening its evidence, source-safety, or
repeatability boundaries.

## Start here

- [Architecture](architecture.md) — components, control flow, and dependency
  boundaries.
- [Configuration](configuration.md) — engine defaults, local overlays,
  preferences, recipes, and precedence.
- [Validation](validation.md) — test layers, real-frame gates, and baseline
  discipline.
- [Known-target catalog](known-target-catalog.md) — runtime contract,
  reproducible generation, provenance, and curation.
- [Releasing](releasing.md) — owner gates, packaging, beta, public-copy, and tag
  sequence.

Then read:

- [Project tenets](../TENETS.md)
- [Adaptation inventory](../ADAPTATION.md)
- [Extending the engine](../EXTENDING.md)
- `AGENTS.md` at the repository root, the detailed operator and engine
  contract (`CLAUDE.md` is a thin pointer into it for Claude Code)

## Records versus current guidance

Implementation plans, historical assessments, private validation inputs,
credentials, and personal worktree state remain in the private maintainer
repository. They are not part of the public project and do not override current
code, tenets, tests, or the canonical references above.
