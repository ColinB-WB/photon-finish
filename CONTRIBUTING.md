# Contributing

Thank you for considering a contribution. This is a small hobby project with a
deliberately conservative processing philosophy: a reliable improvement across
many captures matters more than an exceptional result on one target.

## Before making a change

Read:

1. [Project tenets](docs/TENETS.md)
2. [Architecture](docs/development/architecture.md)
3. [Validation](docs/development/validation.md)
4. [Extending the engine](docs/EXTENDING.md) for processing changes

User documentation should describe the current implementation, use plain
language, and avoid diminishing the Seestar or the specialist applications this
project uses.

## Set up a development environment

```console
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,gui,docs]"
pre-commit install
```

On Windows, activate the environment from PowerShell and use `py -m pip` if
preferred.

## Make a focused change

- Keep the functional core pure and put file, process, network, and UI effects
  at the shell.
- Add or change tests before changing behavior.
- Do not add target-specific processing rules. Convert a useful observation
  into a measured, category-level rule and back-validate it.
- Do not modify source captures or write into a source library.
- Do not hand-edit generated catalog JSON.
- Preserve deterministic serialization, provenance, and fail-closed validation.
- Keep unrelated worktree changes out of the commit.

## Verify

At minimum:

```console
ruff check .
pytest
mkdocs build --strict
pre-commit run --all-files
```

Processing, packaging, catalog, and release changes have additional gates in
[Validation](docs/development/validation.md) and
[Releasing](docs/development/releasing.md). Tests that require private reference
captures must run, not silently skip, before an owner-approved release gate.

## Propose the change

Explain:

- the user or maintainer problem;
- the evidence for the change;
- which tenets constrain it;
- tests and validation performed; and
- known limitations or follow-up work.

Do not include private FITS data, personal paths, signing credentials, or other
people's photographs without explicit permission.
