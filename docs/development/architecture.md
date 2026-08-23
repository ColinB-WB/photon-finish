# Architecture

## System shape

The application is a deterministic Python engine surrounded by an imperative
shell that reads files, runs specialist applications, performs optional catalog
queries, and writes deliverables.

```text
GUI / CLI
    |
discovery and run orchestration
    |
inspect -> classify -> plan -> render -> export -> QA
    |                    |
pure typed core          external-tool adapters
                         GraXpert / Siril / StarNet2 / ExifTool
```

The GUI and CLI are two interfaces over the same engine. The GUI does not have a
second processing implementation.

## Functional core

`seestar/core/` owns typed evidence, decisions, planning, probes, recovery rules,
and quality-gate evaluation. Core functions:

- accept explicit values;
- return new values;
- avoid file, process, network, environment, clock, and UI access; and
- remain deterministic for the same inputs.

This keeps scientific and processing policy independently testable. See
[`docs/TENETS.md`](../TENETS.md) and
`AGENTS.md` at the repository root for the governing boundaries.

## Imperative shell

The shell owns side effects:

- `seestar/gui/` — native app-window interface (a local job server plus a
  pywebview shell) and native folder selection;
- `seestar/cli.py` — command-line entry point;
- `seestar/run.py` and `seestar/orchestrate.py` — discovery, staged execution,
  atomic promotion, records, and cleanup;
- `seestar/inspect.py` and `seestar/measure.py` — FITS/header and image evidence;
- `seestar/adapters/` — external process invocation;
- `seestar/prior.py` and `seestar/known_targets.py` — network and bundled catalog
  evidence;
- `seestar/qa.py` and `seestar/validate.py` — result scoring and
  back-validation; and
- `seestar/io.py` — configuration and typed/serialized boundary conversions.

Effects should move inward only as explicit data. A core rule must never find a
file, read an environment variable, call a process, or perform a network query.

## Authority order

Processing decisions follow this boundary:

1. FITS filter laws that are unconditional for the capture path.
2. Measurements from the photograph.
3. Bounded catalog evidence where measurement cannot settle classification.
4. Safe fallback or explicit uncertainty.

Catalog identity may carry a category into classification, but it cannot supply
pixel measurements, brightness, morphology-derived operations, filter behavior,
or QA results. Footprint and morphology fields in the current runtime catalog
are loaded and validated but inert for processing.

## Source safety and atomic outputs

Discovery treats the Seestar library as read-only. A run creates work under its
owned output/workspace paths and promotes completed deliverables only after
their required checks succeed.

A failed rerun must not truncate or replace prior successful hero or fork
outputs. Cleanup may remove only scratch artifacts created and owned by the
current run.

## Data and control flow

1. Discovery finds candidate FITS files.
2. Qualification opens each candidate and verifies Seestar stacked-color
   evidence.
3. Inspection reads header evidence and image measurements.
4. Source-phase classification applies filter and measurement authority plus
   bounded priors.
5. The core produces typed decisions and a plan.
6. The shell executes the plan through tool adapters.
7. Candidate and recovery stages remain bounded by registered dependencies.
8. Successful outputs are promoted and metadata is embedded.
9. Recipe, notes, and QA records explain the run.

## Generated data

`seestar/data/known_targets.json` is a runtime resource, not maintainer-authored
source. `tools/build_known_targets.py` produces it deterministically from pinned
OpenNGC inputs and `target_curation.yaml`. Runtime code reads JSON only and does
not import the builder or parse the source catalog.

See [Known-target catalog](known-target-catalog.md).

## Extension rule

A target result may reveal a general problem, but production code must not
special-case that target. Demonstrate the need across evidence, define a
measurable predicate, implement a category/general rule, and back-validate every
supported class. The detailed checklist lives in
[Extending the engine](../EXTENDING.md).
