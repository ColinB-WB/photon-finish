# Validation

Validation is layered because no single test can prove both deterministic policy
and photographic quality.

## Fast and full automated tests

Run focused tests while changing a component, then the complete suite:

```console
pytest tests/<focused-module>.py -q
pytest
```

The full suite covers pure-core decisions, boundary conversions, adapters,
source safety, orchestration, recipes, catalog behavior, packaging assumptions,
and regression cases.

Also run:

```console
ruff check .
pre-commit run --all-files
```

Tests should fail when a required invariant cannot be checked. Do not use a
module-wide skip that hides frame-independent guarantees.

## Back-validation

`seestar validate` re-renders configured private reference captures into a
scratch area, scores them, compares them with committed baselines, and performs
a replay-reproducibility check.

Configure machine-local inputs in `config.local.yaml`, not `config.yaml`:

```yaml
validation:
  targets_darwin:
    M8: /absolute/private/path/to/M8
```

Then run:

```console
seestar validate
seestar validate --targets M8,M81
```

Different photons are not interchangeable baselines. Before interpreting a
score difference, confirm the input capture identity.

### Quality references and regression baselines

These answer different questions:

- A **quality reference** asks whether a finished image is good, or better than
  another treatment such as the Seestar preview of the same capture.
- A **regression baseline** asks whether the engine's behavior changed when it
  reprocessed the same raw capture. It is a reviewed developer fixture, not a
  general example of an attractive photograph.

A regression baseline must represent the last **approved** result, not merely
the previous run. Comparing only with the immediately preceding run allows
small regressions to accumulate until the result has drifted far from anything
that was reviewed. Use before-and-after comparisons while iterating, then
compare every reference capture with its approved baseline before promoting a
new baseline.

## Updating baselines

`--update-baselines` changes the reviewed reference state. It is not a
convenient way to clear a failure.

Only update baselines when:

1. the input capture is the intended canonical reference;
2. the behavior change is understood and approved;
3. before/after images and metrics have been reviewed;
4. supported categories have been back-validated; and
5. the baseline change is isolated and documented.

```console
seestar validate --update-baselines
```

## Real-frame value gates

Some policies require private astronomical frames to establish that a benefit
is real and that authority boundaries hold. These tests may be conditionally
unavailable on general CI, but they must run—not skip—on the owner's gate
machine before release approval.

The known-target value gate, for example, proves both:

- a correct offline catalog match recovers the expected classification when
  network evidence is absent; and
- the catalog is inert where an LP filter rule or measurement is authoritative.

## Generated catalog checks

Catalog validation includes:

- hard source pin, license, schema, row-count, blob-ID, and SHA-256 checks;
- closed curation and output schemas;
- explicit resolution or abstention;
- entry and canonical identity checks;
- malformed geometry and association failures;
- deterministic byte-identical generation;
- WCS-confirmed runtime matching;
- review manifest and ambiguity report drift guards; and
- installed-wheel and frozen-app resource checks during release packaging.

See [Known-target catalog](known-target-catalog.md).

## Documentation checks

```console
mkdocs build --strict
python tools/check_docs.py
```

The strict build verifies the public navigation and Markdown rendering.
`check_docs.py` checks repository-local links on the current documentation
surface. Historical and implementation-record archives are excluded because
their old paths are part of the record they preserve.

## Operational release gates

Unit tests do not prove the stranger installation experience, notarization,
tool discovery, real external-tool rendering, beta comprehension, or a clean
public export.

Before a v1 tag, the owner completes:

- `docs/CLEAN_BUILD_TEST.md`;
- `docs/BETA_PLAN.md`;
- packaging and installed/frozen catalog checks;
- privacy and personal-information scan; and
- the explicit re-freeze and tag decision.

See [Releasing](releasing.md).
