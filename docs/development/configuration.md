# Configuration

The repository has three different configuration surfaces. They serve different
audiences and must not be conflated.

## Engine configuration

`config.yaml` contains committed processing laws, defaults, solver bounds,
quality gates, and validation settings. It is part of the tested engine
contract.

`config.local.yaml`, when present beside it, is a gitignored deep-merge overlay
for machine-specific information such as private validation-capture paths. Dict
values merge recursively; non-dict values, including lists, replace the
committed value.

Do not put personal paths, signing identities, credentials, or other private
values into `config.yaml`.

Changes to engine configuration are behavior changes. They require tests,
back-validation, and the same review as Python policy changes.

## User preferences

`preferences.yaml` is the plain-language, supported way to change a look across
renders:

```text
<output root>/preferences.yaml           applies to all targets
<output root>/<target>/preferences.yaml  applies to one target
```

Target preferences override global preferences. Category sections are applied
only after classification. Unknown keys and invalid values fail with all
validation errors reported.

Generate a documented template:

```console
seestar config template --out preferences.yaml
```

Explain current values:

```console
seestar config explain
seestar config explain <preference-name>
```

Preferences are more advanced than selecting a GUI output style. They are for a
look a user wants repeatedly.

## Per-image recipes

`<target>_recipe.json` records the actual adjustments used for one render.
Editing `your_value` and explicitly replaying the recipe pins only those fields.
Recipes are never discovered or applied silently.

Recipes are generated records, not the global engine source of truth. Do not
copy a target's number into engine defaults without general evidence.

## Precedence

The typed decision layer applies settings in this order:

```text
engine defaults
  < output profile
  < preferences
  < recipe pins
  < explicit caller overrides
```

Later layers may override earlier values only within their validated schema.
Unconditional filter and measurement-authority rules still constrain what an
override can legitimately mean.

## External-tool paths

Tool discovery checks, in order:

1. `SEESTAR_<TOOL>` environment variable;
2. `tools.<tool>` in configuration;
3. the system `PATH`; and
4. known installation locations for the operating system.

Use `seestar doctor` to inspect the resolved result. Environment variables and
local overlays are preferable to committing a developer-specific path.

## Validation paths

Real reference captures are not repository data. Put OS-specific target paths
under the relevant `validation.targets_*` key in `config.local.yaml`. On macOS
the current key is `validation.targets_darwin`.

Never weaken a release gate because a private capture is absent. Frame-independent
tests must run everywhere; owner real-frame gates must run on the review machine
before approval.
