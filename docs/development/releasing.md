# Releasing

Releases are owner-controlled. A green unit-test suite is necessary but not
sufficient.

## Current v1 state

The processing and known-target work is complete through the owner-reviewed
value gate. Packaging verification, release re-freeze, clean-machine testing,
beta, public-copy review, and tagging remain separate gates.

The authoritative live checklist is `docs/RELEASE_READINESS.md`. It is excluded
from the public site while the gate is active. Do not infer approval from a
generated hash or a successful local build.

## Release sequence

1. **Prepare a clean candidate.** Confirm intended changes, clean status, single
   version source, dependency metadata, notices, and documentation.
2. **Run automated checks.** Focused and full tests, lint, pre-commit,
   documentation link check, and strict site build.
3. **Build distribution artifacts.** Build the wheel and install it in a fresh
   environment outside the checkout.
4. **Build the Mac application.** Use `packaging/build_app.sh`, then exercise the
   packaged binary's self-tests.
5. **Sign, notarize, and package.** Use the owner credentials described in
   `packaging/README.md`; never commit them.
6. **Run the clean-build checklist.** Test the actual DMG on a clean Mac and the
   packaged application on a clean Windows machine (`docs/CLEAN_BUILD_TEST.md`).
7. **Run representative real renders.** Nebula, galaxy, and cluster; verify
   output, QA, untouched sources, and failed-rerun no-clobber.
8. **Run the owner's own clean-build testing.** The blocking beta was dropped
   (`docs/BETA_PLAN.md`, 2026-08-02) — release readiness now rests on the
   clean-build checklist (step 6) plus the owner's own hands-on testing, not a
   separate external beta.
9. **Prepare the public repository copy.** Run `python -m packaging.export_public_repo
   --dest <path>` (the repeatable no-history export tool) to build the allowlisted,
   no-history copy — it excludes maintainer-private material per
   `packaging/public_export.exclude` and scans for personal information via
   `packaging/scan_personal_info.py`; review licenses separately.
10. **Re-freeze and record approvals.** Record approved generated-data hashes
    and release evidence.
11. **Tag only on explicit owner instruction.** Create the annotated `v1.0.0`
    tag; no plan or agent tags implicitly. Attach release artifacts with
    `python -m packaging.publish_release` (wraps `gh release create`).

## Pinned frozen-app build environment

The Mac app is built from one committed, hashed lock —
`packaging/requirements-build-macos.lock` (macOS arm64, CPython 3.12 only). It pins
every third-party dependency of the frozen app (runtime + gui + desktop extras +
PyInstaller, including setuptools). On every run `packaging/build_app.sh`:

1. **deletes and recreates `.venv-build`** — a pre-existing environment is never
   reused, so a stale or hand-modified venv cannot leak into a release build;
2. uses the **pip bundled with the recorded Python installation** (no unpinned
   `pip --upgrade`);
3. installs the lock with `--require-hashes`; and
4. installs the local project with `--no-deps --no-build-isolation`, so the editable
   build runs against the lock's setuptools instead of letting PEP 517 build
   isolation resolve a newer one.

Nothing can drift past the lock; the cost is re-installing the build venv each time.

This lock is a **maintainer release tool**, not a contributor requirement: ordinary
contributors keep the normal path (`pip install -e ".[dev,gui,docs]"`,
`pre-commit install`, `pytest`), and the dependency ranges in `pyproject.toml` remain
the supported public contract, tested through normal resolution.

Regenerate the lock only as a deliberate maintenance change, on the release platform
(macOS arm64, CPython 3.12), with the pinned generator **pip-tools 7.6.0** in a scratch
environment:

```console
python3 -m venv /tmp/pttool && /tmp/pttool/bin/pip install pip-tools==7.6.0
/tmp/pttool/bin/pip-compile --generate-hashes --allow-unsafe --extra gui --extra desktop \
  --output-file packaging/requirements-build-macos.lock \
  pyproject.toml packaging/requirements-build.in
```

(`--allow-unsafe` is required: PyInstaller depends on setuptools, which must be pinned
and hashed or `--require-hashes` rejects the install.)

**Reproducibility record (v1 scope).** For each release build, record: the source
commit; the build machine's macOS and Command Line Tools versions; the exact Python
version and provenance; the lock file content (committed); the exact build and signing
commands; and SHA-256 hashes of every artifact (wheel, sdist, `.app` zip, DMG).
Byte-identical wheel/sdist reproduction and `SOURCE_DATE_EPOCH` are deliberately
deferred past v1; signed artifacts are never expected to be byte-identical (signatures
and timestamps differ).

## Mac packaging

```console
bash packaging/build_app.sh
SEESTAR_SELFTEST=known-targets \
  "dist/Photon Finish.app/Contents/MacOS/Photon Finish"
bash packaging/sign_notarize_dmg.sh
```

The final command requires owner-supplied signing identity, team ID, and a
stored notarization profile. See `packaging/README.md`.

The known-target self-test must assert the approved record count and content
hash, not only that at least a few records load.

## Installed-package verification

Build a wheel, install it in a fresh temporary virtual environment, change to a
directory outside the checkout, and verify the package resource loads. This
catches accidental success from source-tree imports.

## Documentation publishing

The owner decided (2026-08-02) to publish the MkDocs site via GitHub Pages once
the repository is public. `.github/workflows/docs.yml` builds the site on every
push to `main` that touches `docs/**` or `mkdocs.yml`, but its `deploy` job is
guarded to only run on the public repository (`github.repository ==
'ColinB-WB/photon-finish'`), so it is a safe no-op on the private repo.

Enable Pages (source = "GitHub Actions") once the public repository exists
(Task 9 of the public-release plan). Publishing is a distribution action and
should use the same released documentation commit as the application.

## Release notes

Promote the `[Unreleased]` section of `CHANGELOG.md` to the final version and
date. Notes should state:

- supported telescope, input, and platforms;
- separately installed helper applications;
- local/privacy behavior;
- important capabilities and limits; and
- known issues that a user can act on.

Avoid internal task numbers and research labels in public release notes.
