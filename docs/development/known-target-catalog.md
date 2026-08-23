# Known-target catalog

## Runtime purpose

The application ships a local catalog so many common Messier and Caldwell
targets can be identified from WCS coordinates without a network request.

The v1 release-candidate catalog contains:

- all deterministically resolvable, deduplicated Messier and Caldwell primaries;
- one associated-only record needed for reviewed relationships;
- 219 total records; and
- a generated content hash recorded in the review manifest.

This is deliberately not a bulk import of every OpenNGC object.

## Runtime boundary

Production code reads only:

```text
seestar/data/known_targets.json
```

It does not parse OpenNGC CSV files, load maintainer curation, or import the
builder. `importlib.resources` resolves the JSON in source, installed wheels, and
the frozen application.

Matching requires WCS position and a field-of-view estimate. Outcomes are hit,
miss, ambiguous, or spatially rejected. Ambiguous fields abstain; there is no
category-consensus runtime behavior in v1.

Only category may cross into classification, and only at the existing bounded
prior point. Measurement, filter rules, and QA remain authoritative.
`search_footprints` and `morphology_traits` are validated data-path fields but
are inert in v1 processing.

## Maintainer inputs

The generated JSON comes from:

- `tools/data/openngc-v20260501/` — the complete pinned OpenNGC release inputs
  used by the builder, including `NGC.csv`, `addendum.csv`, guide, and
  CC-BY-SA-4.0 license;
- `tools/data/openngc-v20260501/SOURCE.json` — human-readable provenance and
  independently reviewed hashes;
- hardcoded `EXPECTED_OPENNGC_PIN` values in the builder—the actual trust
  anchor; and
- `target_curation.yaml` — selection, explicit abstention, and exception-only
  annotations.

The full `NGC.csv` belongs in the maintainer repository because it makes the
generation reproducible from the exact reviewed source and allows the builder
to fail closed on source integrity. It is not loaded or bundled at runtime.
If repository size later becomes a material concern, move the complete pinned
source as one reviewed, checksummed artifact; do not silently substitute a
hand-trimmed CSV or make builds depend on an unpinned download.

## Build

Never edit `known_targets.json` by hand. Regenerate it:

```console
python tools/build_known_targets.py \
  --manifest validation/known_targets_manifest.md

python tools/known_targets_review.py
```

The builder is deterministic and fail-closed. It rejects an unreviewed source
tag or commit, extra or missing pinned files, bad blob IDs or hashes, schema
drift, unresolved or ambiguous identifiers, malformed geometry, unknown type
codes, invalid roles, broken associations, and orphaned annotations.

## Resolution and curation

Selection is catalog-level:

- Messier identifiers resolve through the OpenNGC `M` column.
- Caldwell identifiers resolve through structured `C NNN` identifier tokens,
  with the reviewed primary-name path for the four addendum objects.
- Duplicates are removed by resolved OpenNGC primary identity.
- Genuine uncertainty must be declared as a reasoned abstention.

OpenNGC type codes map through a frozen family policy. Mixed or catch-all types
become `unknown` unless an exception has an explicit family and reason.
Astronomical identity or curation that requires genuine judgment is an owner
stop, not an opportunity for a convenient guess.

## Review artifacts

- `validation/known_targets_manifest.md` records resolution, distributions,
  curated exceptions, provenance, and hashes.
- `validation/known_targets_ambiguity_impact.md` records the effect of the full
  catalog on spatial matching, including candidate IDs and categories.
- `validation/known_targets_prereopen_baseline.md` preserves the pre-expansion
  comparison point.

Generated artifacts have drift-guard tests. Fine-grained ambiguity counts are
review observations, not immutable behavior contracts.

## License and attribution

OpenNGC is CC-BY-SA-4.0. The generated catalog is distributed under
CC-BY-SA-4.0 rather than the application's MIT license. Preserve the source,
release tag, commit, hashes, upstream credit, derived-file provenance, and
license text in every distribution.

See `THIRD_PARTY_NOTICES.md` at the repository root.
