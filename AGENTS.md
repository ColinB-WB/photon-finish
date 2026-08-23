# Seestar S30 / S30 Pro Astrophotography Processing — Agent Prompt

> **Audience: developers / the AI coding agent (Claude Code, Codex, or similar) that builds and
> tunes the engine.** This is the development spec, **not** documentation for people who use the
> app. End users download the notarized Mac app and never see this — they need only
> [`INSTALL.md`](./INSTALL.md). See the README's "For users" section.

You process astrophotography FITS files from a **ZWO Seestar S30 or S30 Pro** into
finished, share-worthy images. Verify things rather than assuming, and ask before
anything destructive. Work one target at a time and report after each.

## SCOPE BAR — read before proposing work (owner, 2026-08-14)

This is a **hobby project, given away free to Seestar enthusiasts to save them time.** It is
explicitly not state-of-the-art software and does not need to be. The engineering rigor below is
here because getting the *photo* wrong wastes a user's night — not because the codebase is an end
in itself.

- **Separate user-visible quality from maintainer instrumentation, and say which you are doing.**
  A crop that discards real field, a gate that fails a healthy capture, a galaxy shipping at half
  its calibrated brightness, an error message showing a banner instead of the reason — those change
  what an enthusiast receives and are worth real effort. Validation metrics, outcome matrices, and
  documentation sweeps do not; keep them minimal.
- **Prefer stopping over polishing.** Offer the smaller option explicitly, including skipping a
  step, and say what it costs.
- **The reference set is ~10 objects.** That is enough to catch a catastrophe and far too few to
  tune a threshold finely. When analysis starts arguing for more analysis, it is usually arguing
  for stopping. Instrument so a real regression is *visible*; do not chase beautiful numbers.
- **An honest uncertain outcome is finished work, not a defect to chase** (*Uncertainty is an
  honest outcome*, `docs/TENETS.md`).
- Scale the ceremony to the risk. `docs/CHANGE_SAFETY.md` already says to apply its rules
  proportionally and not to manufacture evidence for absent risks — that instruction is load-bearing
  here, not boilerplate.

---

## TENETS — read before you design, review, or write code

**[`docs/TENETS.md`](./docs/TENETS.md) is the SOLE definition of this project's tenets** — the
beliefs we use to resolve product and engineering tradeoffs. Do not restate, summarize, or fork
them anywhere else; always link to that file.

**Before you design, review, or write code in this repo, read `docs/TENETS.md`.** When a decision
involves a tradeoff, name the governing tenet(s). When two conflict, the earlier-listed tenet
governs — and *The photons are the authority* and *Source data is sacred* are inviolable, never
traded for a better-looking result. A change that violates a tenet is a defect, not a matter of
taste.

**Every design and review document includes a short "Tenet check" section:**

> **Tenet check:** Which tenets affect this decision? Are any in tension? Which one governs, and why?

This puts the tenets where consequential judgment happens (design specs, review write-ups) without
injecting reminders into routine edits. Keep it short — a few sentences that name the tenets by name
and state the resolution.

When you dispatch a subagent to design, review, or write code, tell it to read `docs/TENETS.md`
first — do not assume it inherits this file.

## CHANGE-SAFETY RULES — read before implementation work

**[`docs/CHANGE_SAFETY.md`](./docs/CHANGE_SAFETY.md) is the canonical, tool-neutral home of the
change-safety rules** (interface changes, relocated writes, transaction definitions, tiered
failure handling, testing and review requirements). Read it before changing code, and tell every
implementation subagent to read it alongside `docs/TENETS.md`. Do not restate the rules here or
anywhere else — link to that file (duplicates drift).

---

## 0. ARCHITECTURE

The engine is a **functional core / imperative shell** split,
three layers with a one-way dependency rule enforced by `tests/test_architecture.py`:

- **`seestar/core/`** — the PURE functional core. Import-allowlisted to stdlib
  data/math + intra-core only (`test_core_imports_only_allowlisted_stdlib`); no I/O,
  no subprocess, no tool calls. Holds: `types.py` (every frozen dataclass/enum —
  `Measurements`, `Classification`, `Decisions`, the closed 14-op `Operation` union,
  `Plan`, QA report types, …), `classify.py` (`Measurements -> Classification`),
  `policy.py` (`Classification + Measurements + Overrides -> Decisions` via
  `compute_decisions`, precedence engine < profile < prefs < pins < caller),
  `solve.py` (the pure mtf / two-point black-point solve state machine), `planning.py`
  (the planning state machine — `start_planning`/`advance`, the `NeedWork` loop, the
  **failure-policy table**, final `Plan` assembly), `plan_checks.py`
  (`validate_plan(Plan) -> errors`), and `qa_rules.py` (typed QA gate verdicts —
  `evaluate_gates`/`scale_factors`).
- **`seestar/adapters/`** — the SHELL layer that executes typed operations against
  real tools, one module per tool (`siril.py`, `graxpert.py`, `starnet.py`,
  `exiftool.py`, plus `builtin.py`/`_trail_impl.py`/`_annotate_impl.py`). May import
  `seestar.core.*` only, never other shell modules
  (`test_adapters_import_core_only`) — see `docs/EXTENDING.md` for the adapter
  contract and how to add one. `base.py` holds the `Adapter` protocol and the static
  `REGISTRY: dict[type[Operation], Adapter]`, populated by explicit `register()`
  calls; exactly one adapter per operation type
  (`tests/test_adapters_base.py`'s completeness sweep over
  `typing.get_args(Operation)`).
- **The rest of `seestar/`** — the SHELL: everything touching the world
  (filesystem, config, CLI, GUI). `orchestrate.py` is the conductor — builds the
  `PlanningRequest`, drives the `NeedWork` loop (measurement dispatch, `RunOps`
  execution), then `execute.py`'s `execute_plan`, then metadata/annotate/recipe/
  notes emission; `run_target()` lives here. `run.py` is now THIN: discovery +
  `process_library` + a one-line `run_target` delegate to `orchestrate.py`, zero
  decision logic. `workspace.py` is the run-scoped artifact store (owned handles,
  path routing, cleanup). `measure.py` turns FITS into `Measurements`. `io.py` is
  the ONE dict\<->typed boundary (`engine_defaults_from_cfg`, `gate_thresholds_from_cfg`,
  `overrides_from_prefs_dict`, `decisions_to_params`, `plan_to_dict`/
  `write_plan_sidecar`, `load_config`). `presentation.py` maps a `StageKey` to
  (novice label, tool name). `qa.py` does render measurement + assembles
  `{target}_qa.json` from `core/qa_rules.py`'s typed gates. `validate.py` is
  `seestar validate`. `known_targets.py` is the **offline local catalog** shell module
  (2026-07: `LocalTargetRecord`, package-resource `load_catalog()` that degrades on
  missing/corrupt, WCS footprint-overlap `match_target` with `lookup_role`
  primary-only candidacy, `to_observation` → the reduced core `PriorObservation`); it
  imports `seestar.core.*` only, and footprint/morphology never cross to the core (v1
  changes ONLY category). Built from `seestar/data/known_targets.json` (committed
  lockfile, generated by the maintainer-only `tools/build_known_targets.py` from a
  pinned OpenNGC snapshot + `target_curation.yaml`). See the spec
  `docs/history/superpowers/specs/2026-07-25-known-targets-prototype-design.md`.
  `prior.py`/`framing.py`/`debug.py`/`tools.py`/`cli.py`
  are largely unchanged shell modules (WCS prior + SIMBAD network fetch, auto-crop,
  debug snapshots, tool resolution, CLI). `seestar/gui/` (2026-08, replaced the single
  `gui.py`) is a native-window local GUI over the same engine entry points as the CLI:
  `__init__.py` is the composition root (`main()`/`start_server()` — startup order,
  bounded readiness barrier, shutdown unwind), `jobs.py` holds `JobRegistry` (single-
  flight render admission, reattach snapshots, the batch-boundary graceful-stop
  protocol), `instance.py` is the OS-held single-instance lock + ephemeral loopback
  socket bind, `server.py` is the FastAPI app (token/cookie auth, CSRF header, no-store/
  CSP headers, the HTTP<->JobRegistry translation), `static/` is the vanilla-JS front
  end (pure reducer + serial polling, `textContent`-only rendering), `window.py` is the
  pywebview shell (native window, quit-veto while a render is active), `pickers.py`
  wraps the native/subprocess folder-picker paths, and `views.py` is every novice-facing
  runtime string plus the typed view-model builders (scan/QA/replay/status copy).

**Where decisions live:** `core/policy.py`'s `compute_decisions` is the one function
that turns classification + measurements + overrides into `Decisions`; it's called
once, from `core/planning.py`'s internal `_compute`, itself driven by
`start_planning`/`advance`. **Where the failure table lives:** `core/planning.py`'s
`advance()` state machine handles each `WorkResult` per a fixed failure-policy table
— e.g. prior-fetch failure degrades to measured classification, combine-sessions
failure falls back to the latest session with a fresh artifact ref, a production op
failure refuses planning outright (`PlanningRefused`).

**Plan sidecar:** every render writes `<TARGET>_plan.json`
(`{"plan_format": 1, "plan": {...}, "execution": {...}}`, written atomically via
temp-file + `os.replace` in `io.py::write_plan_sidecar`) — additive and
machine-facing, never a replay input (recipes, §12b, are the only user-editable
replay surface).

**Correctness instruments** (no render-parity oracle; behavior is verified
independently): (1) the full test suite pins
DECISIONS-level behavior; (2) `seestar validate` re-renders configured reference
targets and diffs QA scorecards against committed baselines within tolerance,
including an orientation-vs-baseline hard gate; (3) the QA gates in
`core/qa_rules.py` are the engine's laws as hard checks; (4) visual review at render
milestones by the project owner — this caught one real regression (a
vertical flip in the StarNet FITS→TIFF boundary that every rotation-invariant metric
missed).

**Deleted modules** (do not follow references to these as if current):
`seestar/policy.py`, `seestar/profiles.py`, `seestar/pipeline.py`, `seestar/trail.py`,
`seestar/annotate.py` — superseded by the modules above. Historical narrative below
(the M8/M81/etc. regression stories) is preserved for its lessons; module-path
citations inside it have been updated to where that logic lives now.

---

## 1. GOAL

Take a Seestar S30 stacked FITS (raw, linear, unstretched) and **enhance it into
a great finished image** — rich believable color, full faint structure revealed,
dark-but-not-crushed background, protected highlights (cores/bright stars not
blown to white), tight controlled stars, low noise for the signal, and real
small-scale detail.

**Design principle (important):** the workflow must be **target-agnostic**.
**Target names are never processing inputs; object category and measured properties
are valid processing inputs.** Drive every decision from what is *measured* in the
file and read from its *header* (filter, exposure, background level, noise, star
density, whether extended nebulosity exists). Do **not** hard-code behavior from
knowing the object's name (e.g. "M8 is red"). The filter in the header is real
metadata and may be used; the target's identity may not be assumed. Named objects
(M8, M5, …) appear in this spec only as **regression examples** — evidence a rule
was locked against, never a special case in code (enforced by
`tests/test_architecture.py::test_no_target_name_conditionals_in_processing_source`).

---

## 2. REQUIRED INPUTS

- Raw **.fit** files from a Seestar, already stacked, plate-solved (WCS in
  header), linear, unstretched, 16-bit, 3-layer color. **Two capture models**, both
  supported (2026-07); the header `CREATOR` says which, and the engine adapts from
  the **measured** dims + WCS pixel scale, never from the model name:
  - **S30**: 1080×1920, GRBG Bayer, 150 mm f/5, 2.9 µm pixels, ~3.99″/px.
  - **S30 Pro**: 2160×3840 (IMX585), 160 mm, 2.9 µm pixels, ~3.74″/px — near-equal
    *sampling* but ~3.5× the *sky area*. Pixel-unit and frame-fraction laws locked on
    the S30 therefore scale, not break: QA gates carry an S30 reference scale
    (`qa.gates.reference`) and `core.qa_rules.scale_factors` rescales the FWHM ceiling
    (pixel-scale ratio) at gate time. Faint structure is SOURCE-RELATIVE (Diagnosis 3,
    2026-08): retained supported structure vs the run's own measured STRUCTURE SUPPORT,
    never an inverse-FOV frame fraction; not applicable to starfields (§13).
    Star preservation is SOURCE-RELATIVE (Diagnosis 2, 2026-08): the output is
    judged against the stars measured in that capture's own linear master, never
    an absolute per-camera count (the S50's narrower field broke the old
    absolute floors).
- **Identity is content-first, from the header, not the filename** (v1 lockdown,
  2026-07-13): `inspect.is_seestar_stack(header)` decides whether a `.fit`/`.fits` file
  is a Seestar stacked master — `CREATOR` contains "Seestar", `NAXIS3 == 3` (a debayered
  color stack; raw sub-frames are 2-D), `STACKCNT` present — regardless of its name.
  `file_meta()` then reads target/frames/filter/exposure/date **header-first**
  (`OBJECT`/`STACKCNT`/`FILTER`/`EXPOSURE`/`DATE-OBS`), falling back per-field to a
  filename parse only where the header lacks that field. The historical
  `Stacked_<frames>_<target>_10.0s_<filter>_<YYYYMMDD-HHMMSS>.fit` pattern (spaces
  inside the name like `M 8` are harmless to parse but **break Siril's `load`** — see
  Tooling) is now a **hint/fallback, not a requirement** — earlier versions required
  that exact name to discover a target at all; any qualifying `.fit`/`.fits`, any name,
  is discovered now (§4).
- **Filter meaning (a hint, not a target assumption):**
  - `LP` = light-pollution / **dual-band** filter → used on emission nebulae.
    **Strong Hα; PCC will desaturate it — see Color rule.**
  - `IRCUT` = **broadband** → used on clusters and galaxies.
- Read the **.fit straight from each target folder** (the S30 native layout — see §4).
  Use **ONLY the .fit**; ignore the sibling `.jpg` and `_thn.jpg` (Seestar previews).

## 3. REQUIRED OUTPUTS

For each target, write to that target's **output directory** (`<output root>/<target>/`,
separate from the read-only S30 import — see §4):
- A **16-bit TIFF** and a **viewable JPG** of each finished result.
- Clear names: `M8_final.jpg` / `M8_final.tif`, plus clearly named variants
  (e.g. `M8_stars_on`, `M8_star_reduced`, `M8_starless`).
- When finished with a target, delete bulky scratch intermediates, leaving only
  the deliverables.
- A short per-target report: target, total integration & filter, whether sessions
  were combined, what was done, and whether one image or variants (and why).

---

## 4. DIRECTORY STRUCTURE — S30 native layout in, separate output root out

The input is the folder you copied off the Seestar, **untouched**. Each target is a subfolder
holding the device trio(s) directly — **there is no `raw/`**:

```
<Seestar library>/                 <- the copy off the device — READ-ONLY, never written to
├── M8/  M17/  M5/  ...            <- one folder per astro target
│   ├── Stacked_51_M 8_10.0s_LP_20260615-221349.fit   <- stacked master (what we process)
│   ├── Stacked_51_M 8_..._.jpg                        <- device preview  (ignored)
│   └── Stacked_51_M 8_..._thn.jpg                     <- device thumbnail (ignored)
└── (several Stacked_* trios in one target folder = multiple sessions)
```

The `Stacked_*` name above is what the device actually writes, not a requirement the
pipeline enforces: identity is content-first, from the header (§2) — any `.fit`/`.fits`
file that measures as a Seestar stacked master is picked up under any name, and anything
that doesn't is skipped with a reported reason, never silently.

Renders go to a **separate output root**, one subfolder per target:

```
<output root>/
├── M8/   <- M8_final.{jpg,png,tif}, M8_stars-on*, M8_starless*, M8_qa.json, forks, ...
└── ...
```

**Discovery (one rule):** a folder that itself holds Seestar stacked masters (by header,
`inspect.is_seestar_stack` — §2; any `.fit`/`.fits` name qualifies) is a single target;
otherwise each immediate subfolder is one. `discover_targets` returns
`{"targets": [...], "skipped": [{"path", "reason"}, ...]}` — every file that doesn't
qualify is reported, never dropped silently. Point the tool at the whole library *or* one
target folder (`seestar discover <dir>` / `seestar process <library> --out <root>` /
`seestar run <target> --out <dir>`). `discover_targets` / `run_target(target_dir, output_dir, cfg, …)`
/ `process_library` in `seestar/run.py`. **Config is threaded explicitly** — `cfg` (from
`io.load_config()`) and a resolved `ToolPaths` are passed in, no global singleton.
Full CLI: `inspect · discover · run · process · prior · config · doctor · qa · validate ·
benchmark` (`seestar doctor` checks tools, §5; `seestar config` explains/generates
`preferences.yaml`, §12a; `seestar validate` back-validates against reference targets,
§13, including a per-run replay-reproducibility check).

**Multiple sessions:** the latest by capture datetime is used (combine is opt-in/deferred, §7).

**Never write into the S30 import.** The whole import tree is read-only; outputs *and* scratch
go to the output root, so re-copying a target from the device can't clobber renders. The
pipeline copies each raw `.fit`/`.fits` (whatever it's named — typically `Stacked_*.fit`,
the device's own name) into the output dir under a space-free working name (Siril's
`load` chokes on spaces like `M 8`), so the original is never modified.

Reference data set used to build/validate the engine (10 s subs): M81 (Bode's Galaxy,
IRCUT, galaxy), M8 (Lagoon, LP, nebula), M17 (Omega, LP, nebula), M5 (IRCUT,
globular), M13 (IRCUT, globular, 2 sessions -> use latest), NGC5128 (Cen A,
IRCUT, galaxy), NGC5139 (Omega Cen, IRCUT, globular).

---

## 5. REQUIRED SOFTWARE (paths AUTO-RESOLVED; flags verified)

**Tool paths are no longer hardcoded.** `seestar/tools.py resolve_tools(cfg)` locates each tool
(siril / graxpert / starnet2 / exiftool) by, in order: env var `SEESTAR_SIRIL` /
`SEESTAR_GRAXPERT` / `SEESTAR_STARNET2` / `SEESTAR_EXIFTOOL` → a non-empty `config.yaml tools.*`
(empty by default) → `shutil.which` → known platform-default install paths → else
`ToolNotFoundError`. **Cross-platform (macOS + Windows).** Entry points call `resolve_tools(cfg)`
once and thread the resulting `ToolPaths` through the pipeline (no global lookups). Run
**`seestar doctor`** to print exactly which path resolved for each tool. The macOS locations below
are the platform defaults / reference invocations — set an env var or `config.yaml tools.*` only if
auto-detect can't reach your install.

**GraXpert** — AI background extraction + denoise.
macOS default `/Applications/GraXpert.app/Contents/MacOS/GraXpert`
- BGE:   `GraXpert -cli -cmd background-extraction "<ABS_INPUT.fit>" -output <BASENAME> -smoothing 0.5 -correction Subtraction -gpu true`
- Denoise: `GraXpert -cli -cmd denoising "<ABS_INPUT.fits>" -output <BASENAME> -strength 0.5 -gpu true`
- **Gotcha:** `-output` is resolved **relative to the input file's directory**, and
  GraXpert **appends `.fits`**. Pass input as an **absolute path** and output as a
  **bare basename** (no directory), else you get a doubled path error.
- Models are downloaded & cached: BGE `1.0.1`, denoise `3.0.2`. Uses CoreML (GPU).

**Siril** — `/Applications/Siril.app/Contents/MacOS/siril-cli`  (v1.4.4)
- Run a script: `siril-cli -s script.ssf`
- CWD defaults to `~/Pictures`; **always `cd "<absolute path>"`** inside the script.
- Useful commands: `load`, `save`, `savetif` (16-bit), `savejpg <name> <qual>`,
  `crop x y w h`, `stat`, `asinh <stretch> [blackpoint]`, `mtf <lo> <mid> <hi>`,
  `satu <amount> [bg_threshold] [hue]`, `pcc`, `pm "<expr>"` (pixel math; reference
  images as `$name$`), `register`/`stack` (for combining sessions).
- `ls` is not allowed in scripts. The `ERROR: Python validation failed` lines are
  **harmless** (optional Python env we don't use).
- **`pcc` switches the image to a color-managed state** (ICC profile, persisted into
  saved FITS as an extra HDU): every later `savejpg` then sRGB-gamma-encodes values,
  unlike the unmanaged path which exports raw. The engine is calibrated on raw-value
  exports → **always `icc_remove` right after `pcc`** (session-9 gray-wash root cause).

**StarNet2** — `/usr/local/bin/starnet2`  (weights at `/usr/local/lib/starnet2/`)
- `starnet2 --input <stretched16bit.tif> --output <starless.tif> --unscreen <stars.tif> [--mask <m.tif>] [--stride N] [--upsample]`
- Runs on a **stretched** image (after the Siril stretch), 16-bit TIFF/PNG.
- `--unscreen` gives the star-only layer for screen-blending stars back.

Before processing in a fresh session, re-run each tool's `--help`/`--version` —
flags drift between versions.

---

## 6. THE WORKFLOW (corrected & target-agnostic)

Order: **inspect → crop ragged edges → GraXpert BGE → GraXpert denoise → color →
linked highlight-protecting stretch → saturation → (optional star treatment) →
export**. Always start from the raw linear FITS; never feed stretched data or JPGs
to GraXpert. StarNet is the exception — it wants the stretched image.

**Stage 0 — Inspect (measure, don't assume):** read header (filter, exposure,
WCS); `stat` for per-channel median/sigma/max & noise; count stars (findstar) for
star-density; optionally a quick StarNet pass to measure leftover (extended)
signal → tells you if real nebulosity/galaxy exists (→ star-treatment variants
make sense) vs a pure star field (→ never remove stars).

**Stage 1 — Clean:** auto/small crop of ragged border → BGE (smoothing 0.5,
Subtraction) → denoise (strength scaled to measured noise; ~0.4–0.6 typical;
clusters lighter to avoid smearing stars, faint galaxy heavier).

**Stage 2 — Color — THE KEY LESSON:**
- **Do NOT use PCC by default.** On the dual-band **LP** filter, PCC neutralizes
  the strong Hα to make broadband stars "correct" and turns **deep red into washed
  pink**. The S30's native balance is already near-neutral (backgrounds ~equal
  RGB), so keep native color + light background neutralization, and let the
  saturation stage reveal the real color (red Hα, blue reflection/OIII, star/galaxy
  color).
- PCC on the broadband **IRCUT** filter: **ON for galaxies** (accurate star color
  matters), but **OFF for star fields / globulars** — globulars are red-giant-dominated,
  and PCC + saturation pushed the body PINK (the color fix; `core/policy.py`'s
  `compute_decisions` disables PCC for the IRCUT starfield category). Decide from
  filter + measured category, not the target name.
- **This engine rule is the single owner of the PCC decision — no profile overrides it**
  (2026-07 M81 fix: showcase/print used to force PCC off for "native punchy color",
  which shipped an orange M81). And "native balance is near-neutral" is only true of
  frames whose *sky signal* dominates: on a dark-sky stack the equal-RGB background is
  just the bias pedestal, and the raw *star/body* signal can carry a real cast (M81
  S30 Pro measured R/G 1.29 sky-subtracted). Background neutralization can't fix that;
  only PCC can — which is exactly why the IRCUT-galaxy case must keep it.
- **PCC leaves two kinds of state behind — clean up both** (`to_linear_calibrated`):
  (1) **`icc_remove` immediately after `pcc`** — pcc puts the image into a
  color-managed state (an ICC profile saved into the master as an extra HDU), after
  which every `savejpg` — solver probes AND deliverables — sRGB-gamma-encodes the
  output while the no-PCC path exports raw working values. This was the TRUE root
  cause of the M81 "gray wash": the shipped jpg sky of 40 was the gamma encoding of
  a healthy linear 5.4, and the solver was chasing targets in gamma space. All
  engine targets/gates/references are locked in raw-value space.
  (2) **`adapters/siril.py`'s `neutralize_background`** — per-channel *offsets* re-aligning the
  channel sky medians (star/body color above the sky untouched); PCC calibrates
  gains to catalog STAR colors and promises nothing about the sky.

**Stage 3 — Stretch (locked from M8 "BC2") — BRIGHT BUT CONTROLLED:** the goal is a
subject bright enough to FILL THE FRAME at full resolution (like the S30's own
output), with a clean dark background. Recipe: **black point at the sky level +
asinh + a LOW mtf midtone lift.** `asinh <D> <bp>` then `mtf 0 <mid> 1`. The black
point (`bp ≈ 0.987 × background median`, just BELOW it) keeps the sky dark while
PRESERVING the faint star field; the asinh + low mtf lift the subject up bright.
**CRITICAL black-point lesson:** a bp ABOVE the median (to make an "inky" background)
clips the faint stars to black with the sky — M8 went from **916 stars to 26**, and
the frame looked empty/small/dull. Keep bp just below the median: "dark but NOT
crushed." Hard-won lessons: (1) a *uniform bright lift* ("BR1":
linked autostretch to a bright target) grays the background and flattens the core —
avoid; (2) holding everything *dark* ("SYN_B": gentle asinh + high mtf) makes the
subject look small/underwhelming; (3) **BC2 does both** — bright subject + clean
background — by separating the black point (controls background) from the lift
(controls subject). Avoid **unlinked** auto-stretch (it desaturates).
**Step 2 update ("BC3"):** the M8 bracket re-locked the nebula to a HIGHER asinh (75)
+ a LOWER mtf mid (0.08) — body p90 ~doubled and the bright core climbs high enough to
desaturate toward pink/white (the dust-lane structure), while the star field GREW
(837→952). **Direction matters:** in Siril `mtf 0 m 1`, a *lower* m is *brighter* — the
earlier 0.10 under-lifted; sweeping m *up* (a tempting misread) only dims. The bp is
**measured on the linear master** (post-BGE), scale-aware (raw 16-bit vs float master).

**Session-5 generalization — TARGET-DRIVEN ADAPTIVE LIFT (no per-target numbers).**
Fixed per-category mtf did NOT generalize: M17 (faint nebula) and M5 (star field) both
came out badly under-lifted with the locked values. The fix: **`asinh_d` is the
per-category contrast SHAPE; mtf is SOLVED per image** (`core/solve.py`'s solve state
machine, driven by `core/planning.py`) so the rendered output hits a target level — extended → masked body
p90 ≈ 72, starfield → sky median ≈ 10. Faintness is then handled automatically. Back-
validated on M8/M17/M5: reproduces hand-tuned quality, makes the two nebulae *consistent*
(body ~72 vs the inconsistent hand 65/83), no core clipping, beats the S30 on stars
(M5 726 vs 674, M8 895 vs 750). The target levels are the single tuning knob; raise them
to brighten everything, lower to darken. NO more per-target overrides.

**Session-8 contract fix — SOLVE THE CHAIN YOU SHIP (the M8 overcook regression).**
The solver used to probe asinh+mtf+satu only, while the render then applied the GHS
structure treatment *after* the solve — so the shipped output blew past the solved
target (M8 showcase: body p90 75→123, sky near-black 0.01%→4.2%, 1475→1054 stars,
32% of the body pinned to the chroma rail; QA passed it because the category read
failed and every gate silently skipped). Fix: `adapters/siril.py`'s `stretch_chain_lines` is the
ONE stretch chain (asinh → mtf → GHS → satu); **both the solve probe and the final
render build from it**, and any GHS is attached (incl. the measured cluster halo)
*before* the solve. The print `black_lift` stays outside the solve — it is a paper/
device mapping on top of the solved look. Profile/noisy brightness intent composes
as a *scale on the target level*, not a post-solve mtf offset.

**Session-9 — THE GRAY-WASH REGRESSION (M81 +PCC) and its three-layer fix.** The +PCC
M81 shipped a gray sky (jpg 40 vs the passing finals' 9–13) with the faint envelope
melted into it — the galaxies *looked* smaller (envelope frac halved). Forensics
(replicate the linear stages, trace the chain stage-by-stage, then diff FITS vs JPG of
the same render) found the true cause NOT in the stretch: **pcc had put the image into
a color-managed state, and every `savejpg` after it — the solver's probes and the
deliverables — applied an sRGB gamma while the no-PCC path exports raw values.** The
jpg sky 40 was gamma of a healthy linear 5.4; the solver chased body/sky targets in
gamma space; a first "fix" that solved the sky down to 14-in-gamma crushed the linear
data (4% near-black, envelope lost). Layers of the durable fix:
1. **Root cause:** `icc_remove` right after `pcc` (`to_linear_calibrated`) — one
   export convention for both color paths (§5 Siril gotchas).
2. **Structural guard:** for extended targets `core/solve.py`'s two-point solve
   (the black-point search phase of the solve state machine, driven by `core/planning.py`)
   solves **bp for the background** and **mtf for the body** (separation-of-controls
   law) — if the body-solved render leaves the sky above `target.<cat>.sky_ceiling`
   (~13), bp is raised (binary search, hard-bounded BELOW the sky median per the M8 bp
   lesson) and mtf re-solved. **One-sided:** an already-dark sky keeps the plain
   0.987 × median bp. documentation opts out (`core/policy.py`'s
   `PROFILE_OVERLAYS["documentation"].sky_solve = False`, full-DR).
3. **Gate:** QA `sky_median_ceiling` — the too-bright cousin of `near_black_ceiling`
   (skipped for documentation via the embedded profile keyword).

**Stage 4 — Enhance:** `satu <amt> 0` (threshold 0 so faint nebulosity is saturated
too), **per category**: nebula 0.60 · starfield 0.55 · galaxy 0.60 (§6a;
`config.engine.saturation`, overrideable via `preferences.yaml`). Optional local
contrast/sharpening on non-star structure.

**Stage 5 — Star treatment (only if extended signal exists):** StarNet → starless
+ stars layer. Star-reduced = screen-blend stars back at reduced strength via
`pm "1 - (1 - $starless$)*(1 - <k>*$stars$)"` (k≈0.45). For an extended target under a
star-taming profile (**showcase**/print), the default deliverable set is **all three**:
`{T}_stars-on`, `{T}_starless`, and the showcase `{T}_final` = **STAR-REDUCED** (the
showcase's "tamed/reduced" stars, §8a — decided with the owner). `natural`/
`documentation` profiles stay **stars-on** (untouched). **Never remove stars from
clusters — the stars are the subject** (single stars-on `{T}_final`).

**Stage 6 — Frame — FULL FRAME, no crop by default.** Hard lesson: "the subject
looks small" is almost always a **brightness** problem, NOT framing. The Seestar
field is wide, but its own output fills the frame by **stretching the faint envelope
brighter at full resolution** — not by cropping. Cropping to enlarge the subject
throws away ~70% of the pixels (≈2 MP → ≈0.6 MP) to fake what Stage 3's bright-but-
controlled stretch achieves for free. So: keep the full frame, get the stretch
bright (BC2). An auto feature-crop exists (`framing.py`, measures the subject from
the starless and crops to fill) but is **OFF by default** (`engine.framing.enabled:
false`) — reserve it for genuinely tiny subjects, and never for pure star fields.

**Stage 7 — Export:** 16-bit TIFF + JPG per variant; clean scratch. Embed metadata
(section 11) in every exported file.

### 6a. CONCRETE DECISION RULES (derive category from measurement + filter, never name)

From Stage 0 compute: filter `F`; frame count `N`; per-channel normalized median
`m`, sigma `s`, max, `bgnoise`; star count `S`; extended-signal fraction `E` =
fraction of StarNet-starless pixels above `m + 3s`. Then:
- **starfield** (cluster): `E` low (≲0.03) and `S` high.
- **extended**: `E` ≳0.03. Split by filter: `LP` → emission nebula; `IRCUT` → galaxy/broadband.
- **SNR class**: "clean" if `N` high & `bgnoise` low → push to the punchy end of each
  range; "noisy" if `N`≲45 or high `bgnoise` → restrained end + a bit more denoise.

| Parameter | Rule |
|---|---|
| Crop | **measured per edge; ZERO by default** (Diagnosis 1, 2026-08). Only a demonstrated invalid border signature (blank/non-finite rows, a sustained coverage discontinuity) trims pixels, and only on the edge that shows it; ambiguity or a failed measurement crops NOTHING and records why. Astronomical content, gradients, and merely dark sky are valid data. The old fixed ~1.5% border is gone — it silently discarded real field on every clean frame; an explicit legacy `crop_border_frac` pin still forces the symmetric crop, and `engine.crop_max_auto_frac` bounds any automatic trim. |
| GraXpert BGE | smoothing 0.5, Subtraction (always) |
| Denoise strength | base 0.5; **starfield 0.6** (session-6 measured); **galaxy/faint 0.65**; `+0.1` if noisy. **Denoise is the star-count LEVER, not stretch:** lighter denoise leaves faint stars buried below findstar's threshold, so the old "starfield 0.4 to protect stars" was BACKWARDS — it COST stars. A sweep showed count rises monotonically + FWHM drops (tighter, not fatter) with denoise (M5 620→734, M13 674→810 at 0.4→0.6, all clearing the S30). **0.6 is the CAP:** 0.7–0.8 keep scoring higher but over-smooth (dense cores cream over) — the proxy decouples from quality; pick by eye, not number. |
| Color / PCC | **`LP` → PCC OFF** (desaturates Hα); **`IRCUT` galaxy → PCC ON** (accurate star color); **`IRCUT` starfield/globular → PCC OFF** (the color fix — globulars are red-giant-dominated; PCC + high satu pushed them pink, `core/policy.py`'s `compute_decisions`). Always light bg-neutralize. |
| Stretch | **bright-but-controlled: black point at the sky level + asinh + LOW mtf midtone lift** (`asinh <D> <bp>` then `mtf 0 <mid> 1`). bp clips the sky dark/clean; asinh+low-mtf bring the subject up BRIGHT so it fills the frame at full res. Nebula re-locked from the M8 Step 2 bracket ("BC3"). |
| `asinh` D | **nebula 75** · galaxy 55 · **starfield 65** (`config.engine.stretch.asinh_d`; was 25 — too dim; crushed the faint field/halo) |
| `asinh` blackpoint | `0.987 × background median`, **measured on the LINEAR MASTER** (post crop/BGE/denoise), not raw — BGE shifts the median; and `measure()` is **scale-aware** (raw is 16-bit, the master is float [0,1]). bp just BELOW the median; a bp ABOVE it clips the faint STAR FIELD to black (M8: 26 stars vs 916/952). Dark but NOT crushed. |
| `mtf` mid | **SOLVED per image, not fixed.** `core/solve.py`'s solve state machine (driven by `core/planning.py`) binary-searches mtf so the rendered output hits a per-category target: **extended → masked body p90 ≈ 72; starfield → sky median ≈ 10** (`config.engine.stretch.target`). The probe renders the **same chain the final ships** (incl. any GHS treatment — `adapters/siril.py`'s `stretch_chain_lines`, session-8 contract). Profile/noisy brightness intent scales the *target level* (`stretch_target_scale`), never a post-solve mtf offset. (Config mtf values are only the search seed / solver-off fallback.) **Extended targets are a TWO-POINT solve** (session-9): if the body-solved sky exceeds `sky_ceiling` (18, re-locked from 13), bp is raised (never past the sky median) and mtf re-solved — bp owns the background, mtf owns the subject. One-sided; documentation opts out — and it arms above `sky_ceiling + sky_tol` (18+2), not at the ceiling itself (2026-08-14 audit). **Every probe of one solve measures body_p90 over ONE FROZEN SOLVE REGION** (owner ruling, 2026-08-14): the object mask — the region level 72 was calibrated against — taken once off the first real-chain body probe, or the recovered broad support where the catalogue-corroborated frame-filling recovery fired. It is published as `{T}_solve_region.png` so a later `seestar qa` re-measures the contract over the same pixels. Deliberately NOT the structure support: solving on that shipped M81/M100 at half their calibrated brightness. |
| Saturation `satu _ 0` | **nebula 0.60** (LP Hα red-dominant; embrace native red) · **starfield 0.55** (NOT the intuitive 0.85 — old globulars are red-giant-dominated ~4000K, R/G~1.3 by Planck; 0.85 amplified that to a PINK body, M5 evidence; 0.55 keeps a natural warm orange) · galaxy 0.60 (limit chroma noise). `config.engine.saturation`; overrideable via `preferences.yaml`. |
| Orientation | Seestar FITS omit `ROWORDER` → raster export comes out vertically flipped vs the S30's own JPG. **`mirrorx` once in `crop_linear`** (first Siril touch) normalizes every output. Fixed convention, target-agnostic. |
| Output | 16-bit TIFF (master) + **PNG** (lossless showcase) + quality-95 JPG |
| Star treatment | **extended (showcase/print) → default SET: `{T}_stars-on` + `{T}_starless` + showcase `{T}_final` = STAR-REDUCED** (§8a) · natural/documentation → stars-on · **starfield → single stars-on `{T}_final`, never remove** |
| Framing | full-frame; add a close-up only if signal bbox < ~25% of frame |
| Trail removal | inspect; remove any long thin straight bright line (objective, no fork) |

> **The nebula numbers above (asinh 75 / mtf 0.08 / satu 0.60) are validated on M8
> against its reference.** They are nebula-category starting points, not universal —
> re-bracket galaxy/globular against *their own* references before trusting values.

Pick the exact value within each range from the SNR class. These are starting
points — always render, **view the JPG, and adjust** before exporting.

---

## 7. MULTIPLE SESSIONS (e.g. M13 has 2 .fit)

**v2.0 default: use the LATEST capture** (by filename datetime). Combining sessions
shot far apart proved unreliable when their calibration scales differ (M13: 2025
background 0.0009 vs 2026 0.015, 17x) — the register+integrate path works
(the session-combine strategy in `core/planning.py`'s `start_planning`, `engine.combine_sessions: true`) but is **opt-in and
deferred** for refinement (per-session normalization, metadata merge). Default takes
the newest, best-calibrated single session.

## 8. PER-TARGET HINTS (filter is a hint, confirm by measurement)

- **Emission nebulae (LP — M8, M17):** Hα red-dominant. Light BGE, moderate
  denoise, **bright-but-controlled stretch (asinh D~75 + black point at sky level +
  low mtf ~0.08)** so the nebula fills the frame at full res with a clean dark
  background (not held dark, not a uniform bright wash). Full frame, no crop. **No
  PCC.** Star-treatment variants apply.
- **Globular clusters (IRCUT — M5, M13, NGC 5139):** LIFT enough to show the faint
  halo + surrounding star field (the cluster's gradual falloff is most of its beauty) —
  NOT a "gentle/dark" stretch (that crushed M5's field to 98% black, ~half the S30's
  stars; fixed to asinh 65). Keep stars resolved; **denoise at 0.6, NOT
  lighter** — counter-intuitively, lighter denoise (the old 0.4) loses faint stars (they
  stay buried in noise below findstar's threshold), and MORE denoise reveals them while
  TIGHTENING them (FWHM drops). Cap at 0.6: past ~0.7 the dense core over-smooths/creams.
  (M5 measured CLEAN, not noisiest — drive denoise from measured bgnoise, not the label.)
  **Color:** PCC **OFF** + saturation **0.55** (NOT a "boost" — globulars are red-giant-
  dominated; PCC + high satu pushed the body PINK; lower satu keeps a natural warm orange).
  Protect the core from clipping. "Subject looks small" on a cluster = under-lifted halo,
  NOT angular size — do NOT crop. **Never remove stars.**
  **Cluster halo treatment (implemented; historical plan at
  `docs/history/v1.0/plans/CLUSTER_HALO_PLAN.md`):** the diffuse halo is
  now lifted automatically by a measured GHS — `engine.cluster_treatment` (SP=0.20 on the
  halo, sibling of galaxy/nebula treatments). TARGET-AGNOSTIC: triggered by a MEASURED
  concentration `C` (`measure.py`'s `measure_concentration`; block-mean downsample → packed core
  stands many σ above the field; `C ≥ engine.cluster.concentration_min`), never the name.
  Showcase/print only; emits a `{T}_starless.tif` so the faint_structure QA gate measures the
  halo glow. Locked from an M13 SP bracket (halo +53%, background flat). Sparse fields (low `C`)
  are untreated.
- **Galaxy / hard (IRCUT — NGC 5128 Cen A):** faint, shot low. Heavy gradient
  removal + denoise, careful stretch for the dust lane, detect & remove any
  satellite/plane trail, crop in to feature the small galaxy. Weakest data — do
  your best and say so. **Phase 5 (implemented):** the dust lane is revealed by a
  **Generalized Hyperbolic Transform** (Siril `ght`, `engine.galaxy_treatment`)
  applied after the base asinh+mtf — expand contrast around the symmetry point
  **SP** with **LP** (shadow) protecting the sky and **HP** (highlight) protecting the
  core. **SP re-locked 0.25 → 0.15 (session-9 M81 faint-disk bracket):** the Cen A
  lock put SP at the *bright body* because a low SP lifted envelope AND sky together —
  but that sky-protection duty now belongs to the **two-point solve** (bp holds the
  background), freeing SP to sit on the *faint disk*. On M81 (solved per variant,
  Option-A) SP 0.15 grew the visible disk +27% (envelope 8.1%→10.3%) while pegged
  chroma nearly halved and the sky stayed dark — the disk the S30's own JPG showed is
  back. Re-validate the dust-lane case on Cen A when that data is next available
  (validate with a controlled GHS-on/off StarNet compare, not by eye alone). Classify the galaxy with the **WCS catalog prior**
  (`--prior`; the E-gate can't), and emit a **closeup** fork (galaxies only) cropped to
  the starless subject bbox. Keep sharpening/deconvolution restrained — 4″/px is
  undersampled, heavy sharpening amplifies noise.

## 8a. OUTPUT PROFILES (purpose presets — the render's "voice")

Three layers, kept separate: the **engine** adapts to *content* (nebula/cluster/
galaxy, §6a); a **profile** adapts to *purpose/audience*; **forks** (§9) are
*composition* options inside a profile. Default profile = **showcase**. A profile is
a small parameter overlay on top of the content-adaptive defaults, so e.g.
showcase-on-a-globular still gets cluster-appropriate gentle stretch, just punchy.

**Single source of truth: `seestar/core/policy.py`** (`PROFILE_OVERLAYS` +
`compute_decisions`, called once from `core/planning.py`; nothing else adjusts
decisions by profile; locked by `tests/test_core_policy.py`). **Profile meanings:** *showcase* = **vivid but
credible** — rich color and an aggressive reveal are the point, but neon color,
crushed backgrounds, emptied star fields, and featureless cores are defects (the QA
overcook gates enforce this, §below); *natural* = **believable and restrained** —
visibly gentler than showcase; *documentation* = conservative record; *print* =
showcase re-tuned for paper. **Profile brightness rides the solved stretch as
`target_level_scale`** (a multiplier on the per-category target level) — never a
fixed mtf delta on the solver path (mtf direction was a historical bug source;
`mtf_fallback_delta` exists only for `solve_stretch: false`).

| Knob | **showcase** (default) | **natural** | **documentation** | *print* (opt) |
|---|---|---|---|---|
| Purpose | share / social / wow | honest, believable | record / compare / photometry | wall art on paper |
| Color | engine decision (no profile touches PCC — 2026-07 M81 fix; the filter/category rule in `core/policy.py`'s `compute_decisions` already keeps native color where that's correct) | engine decision | engine decision | engine decision |
| Saturation | engine per-category ×1.0 | ×0.6 | absolute 0.1 | ×0.9 (print-safe) |
| Brightness (target level) | ×1.0 | ×0.95 (bracketed: 0.85 crushed the sky to 5.5% near-black; 0.95 = 0.8%) | ×0.7 mild + **bp ×0.97** (full DR: standard bp crushed 46% of sky; ×0.97 = 0%) | ×0.97 (lower output) |
| Structure GHS (nebula/galaxy/cluster) | **on** | **off** (honest base stretch) | off | on |
| Noise | engine | engine | engine | denoise +0.1 |
| Stars | tamed/reduced (fork set) | stars-on, untouched | stars-on, untouched | tamed |
| Background | dark, **not crushed** (near-black gate) | dark, natural | **preserve faint** | black point lifted for paper |
| Output | 16-bit TIFF + PNG + sRGB JPG | same | + **annotated** JPG (label, scale bar, N/E, date) | + black_lift |

`print` is showcase re-tuned for reflective media → keep optional, not a peer.
Starless and wide/close are **forks** (§9), not profiles — they compose within any
profile. Profiles are target-agnostic. CLI: `--profile showcase|natural|documentation|print`.

## 9. VARIANTS, FORKS & LABELING

**Make the obvious taste calls yourself** — don't ask. Default to **ONE** finished
image, `<TARGET>_final.jpg/.tif` — **except** that an **extended target** (nebula/galaxy)
under the **showcase**/print profile auto-emits the star-treatment set (`stars-on`,
`starless`, and `_final` = **star-reduced**), because those are inherently different
deliverables the viewer should choose among (§8a, Stage 5).

**Fork into multiple labeled images ONLY when the choices are genuinely
incompatible** (no single image can satisfy both). When you fork, **you invent a
short, descriptive, lowercase-hyphenated label that names the trade-off** (not
generic "v1/v2"). Save all, embed metadata, give a one-line pros/cons each. Genuine
forks and example labels:

- **Star treatment** (extended/nebula targets): `<T>_stars-on`, `<T>_star-reduced`,
  `<T>_starless` — inherently different deliverables.
- **Aesthetic identity** when vivid and natural can't coexist: e.g.
  `<T>_vivid` vs `<T>_natural`, or where the axis is accuracy,
  `<T>_crowd-pleaser` vs `<T>_technically-correct` (e.g. punchy native color vs
  PCC-calibrated star color on a cluster). Pick labels that fit the actual fork.
- **Framing** when a small subject wants both: `<T>_widefield` vs `<T>_closeup`.

Everything **objective** — trail removal, gradient/background, obvious noise,
combining sessions, and the per-SNR auto-tune of stretch/saturation — is just done.
No fork, no asking.

## 9a. DEBUG MODE (per-stage snapshots — off by default)

When `DEBUG` is on, save a small viewable JPG after **every** stage so each
component's effect can be inspected (this is what would have caught the PCC washout
at a glance). Output to `<output dir>/<TARGET>_debug/`, numbered:
`00_raw · 01_crop · 02_bge · 03_denoise · 04_color · 05_stretch · 06_saturation ·
07_starless · 08_stars · 09_star-reduced · 10_final`.

Rules:
- **Linear stages (00–04) get a preview-only stretch** (a fixed autostretch applied
  to the snapshot ONLY, identical across these stages so they're comparable) — never
  alter the pipeline data. Stretched stages (05+) are saved as-is.
- Also append per-stage `stat` (per-channel median/sigma/bgnoise) and the exact
  command + params to `<output dir>/<TARGET>_debug/debug_log.txt`. The numeric trail is
  what pinpoints regressions (e.g. saturation/color collapse).
- Small JPGs only (fast, low disk); no extra heavy FITS unless requested.
- DEBUG folders are **kept** (not cleaned). Normal runs skip debug output entirely
  and clean scratch as usual.

## 10. PROCESS & REPORT

Work one target at a time. After each: target, total integration & filter, whether
combined, what you did, one image or which forks (and the incompatibility that
justified each). If a step fails or a tool behaves unexpectedly, **stop and tell the
user** rather than guessing. Ask before installing, deleting, or any destructive
action. End with a summary of all targets and outputs.

---

## 11. EMBED METADATA IN EVERY EXPORT (exiftool, auto-resolved — §5; `metadata.embed(…, cfg, tools)`)

After exporting each JPG **and** TIFF, write metadata with
`exiftool -overwrite_original`. Pull values from the FITS header + filename +
measurements; never hard-code the object identity beyond what's parsed/derived.

Fields to set:
- `EXIF:ImageDescription` — human one-liner, e.g.
  `"M8 (Lagoon Nebula) — emission nebula. 51×10s = 8.5 min, LP dual-band, 2026-06-15. ZWO Seestar S30."`
- `EXIF:Artist` = the configured artist (`defaults.artist`); omitted when unset
- `EXIF:Software` = tool versions, e.g. `GraXpert bge1.0.1/dn3.0.2 + Siril 1.4.4 + StarNet2`
- `EXIF:DateTimeOriginal` = capture datetime parsed from the filename
- `EXIF:UserComment` = pipeline + key params + variant, e.g.
  `"crop->BGE(0.5,Sub)->denoise(0.5)->asinh(75,bp0.0145)->mtf(0.08)->satu(0.6); no PCC; profile=showcase; variant=final"`
- `XMP:Title` = target + common name; `XMP:Description` = type; subs; total integration; filter
- `XMP:Subject` / `IPTC:ObjectName` = keywords incl. target, common name, object type, filter, "Seestar S30"
- **The common name comes from the offline catalog match, not from a name table** (2026-08-20):
  `orchestrate._catalog_common_name` reads the `common_name` of the `LocalTargetRecord` this run
  already matched by WCS footprint (`known_targets.match_target`) and passes it to `metadata.embed`.
  Cosmetic only — title decoration and one keyword; never a processing input. A miss, an ambiguous
  field, a `spatially_impossible` record, or an entry with no name all ship the bare target label.
  `metadata.py` therefore contains no object-name literals and is now inside the §13
  target-agnostic source sweep.
- Astro keys where available: filter, frame count, total integration, RA/DEC (from
  WCS), pixel scale, telescope, and the **variant label**.

Verified working on this Mac (EXIF+XMP+IPTC embed and read back).

---

## 12. SETTLED DEFAULTS (decided with the user — apply, don't re-ask)

- **Aesthetic:** auto-tune per image from measured SNR (clean→punchy, noisy→
  restrained). Only split into `vivid`/`natural`-style forks when genuinely incompatible.
- **Framing:** full-frame by default; add a `closeup` fork only when the subject is
  small in frame.
- **Taste calls:** the agent makes obvious ones without asking; forks only for true
  incompatibilities, with self-authored descriptive labels (section 9).
- **Metadata:** always embedded in finished JPGs (and TIFFs) per section 11.
- Bring photographic expertise on what "great" means; show results and let the user
  react rather than over-asking.

## 12a. CONFIGURATION MODEL — `config.yaml` (internal) vs `preferences.yaml` (user)

Two layers, kept separate:
- **`config.yaml` = INTERNAL engine defaults** (the measured, locked values — asinh shapes, GHS
  treatments, QA gates, tool paths). Not user-facing; loaded by `io.load_config()` and
  threaded as `cfg`. Edit it only when re-locking an engine law against evidence.
- **`preferences.yaml` = the USER tuning layer** (`seestar/prefs.py`; plain-English presets).
  `ALL_PRESETS` is the single source of truth for every user-facing knob (color, brightness,
  stars, …). Two scale modes: **relative** (a multiplier on the per-category default — so
  "subtle" feels subdued relative to *that* category's baseline, e.g. nebula satu 0.60 vs
  starfield 0.55) and **absolute**. **Scoping:** `<out_root>/preferences.yaml` (global, with
  optional `nebula:`/`galaxy:`/`starfield:` category sections) and
  `<out_root>/<target>/preferences.yaml` (one target); priority global-bare < global-category <
  target. `seestar config` explains the presets and generates a template. Provenance (label +
  resolved value) is logged into the EXIF UserComment (`params["_pref_provenance"]`).

---

## 12b. ADJUSTMENT RECIPES — per-image round-trip tuning (`seestar/recipe.py`)

Every run emits `<output dir>/<TARGET>_recipe.json` — each adjustment that applied,
with the app's value, the tool that applies it (GraXpert/Siril/StarNet2, or "this app"
for solved/measured values — never point users at Siril for an app-owned decision), a
plain-English note, the reason, the symptoms-if-wrong, and `"your_value": null`. The
file is written `ensure_ascii=False` — users read it in a plain text editor. Replace
any `null` with a number/boolean and replay it — **CLI**: `seestar run <target> --out
<dir> --recipe <file>` (any path); **GUI** (v1): select the single target and use the
"Fine-tune (optional)" panel's Re-render button (bullet below) — either way the same
code path renders again, honoring every non-null `your_value` as a PIN and re-deriving
everything else from measurement (mode (a) IS mode (b) with no pins). Design spec:
`docs/history/superpowers/specs/2026-07-11-adjustment-recipe-design.md` (v0.5, CLI-only at the
time it was written; the GUI flow shipped in the v1 lockdown, see
`docs/history/superpowers/specs/2026-07-13-v1-lockdown-design.md` §3).

- **Registry:** `recipe.ADJUSTMENTS` is the single source of truth (mirrors
  `prefs.ALL_PRESETS`). Two kinds: `tunable` (pin overrides the app default) and
  `solved` (`blackpoint`, `mtf_mid` — re-derived every run; a pin short-circuits the
  solver: bp pinned → mtf-only solve, never the two-point bp/sky search; mtf pinned →
  no solve at all).
- **Precedence:** engine < profile < preferences.yaml < recipe pins < caller overrides.
  Pins survive category recomputes (prior/e-gate) **by construction**: every recompute goes
  through `core/planning.py`'s `_compute`, which calls `compute_decisions` with the request's
  own `pins` — there is no re-application step to forget (the old `run_target` `_reapply`
  shim is gone).
- **Never interpreted, never transferred:** recipes are per-image; cross-target taste
  stays in preferences.yaml. No auto-discovery — `--recipe` is explicit (a re-render
  without it still PRESERVES `your_value` fields when rewriting the file).
- **Two replay interfaces, different scope (v1):** the **CLI** `--recipe <file>` takes
  **any path** — replaying a recipe you moved, shared, or hand-edited elsewhere is
  supported. The **GUI**'s "Fine-tune (optional)" panel (`seestar/gui/`) is
  **emitted-recipe-only** — it appears once a single already-rendered target has a
  recipe under the chosen output root, "Open recipe" launches the OS text editor on
  that exact file, and "Re-render with my edits" always replays that same file (no
  file picker, no cross-target discovery). Plain **Render** never reads a recipe
  either way.
- **Invalid recipe = hard error before rendering**, every problem listed
  (prefs-style). Version/context drift = warn + proceed, recorded. Inapplicable pins
  warn explicitly (the silent-gate lesson). Replay QA is identical by construction
  (same code path; `seestar qa` unchanged).
- Replay prints and persists a change report (`notes["recipe_replay"]`): per
  adjustment prior value → new value, pinned vs re-derived. Pins are stamped into the
  EXIF UserComment (`recipe-pins=...`), like prefs provenance.
- Target-agnostic (enforced: `recipe.py` is in the §13 sweep test).

---

## 13. PRINCIPLES & THE CODE / OPERATOR SPLIT (derived from the M8 work)

Every decision in this pipeline belongs to one of three layers. Putting a decision in
the wrong layer is what caused every M8 bug. See `docs/ROADMAP.md` for sequencing, and
**`docs/ADAPTATION.md` for the full inventory of detect-and-react mechanisms** (the
generalization law: never assume one test case is universal; measure → react → gate →
back-validate with `seestar validate` before locking any engine change).

**Laws — deterministic CODE. Fixed functions of measurement, instrument, or tool
semantics. An LLM is *unreliable* here (it once proposed sweeping `mtf` the wrong way):**
- **Measure at the stage and in the units you'll act on.** The black-point bug
  (measured on raw, applied on the linear master) and the scale bug (16-bit vs float
  [0,1]) were both this. Parameters derived from the wrong stage/scale are silently wrong.
- **Every knob has a known direction — encode it.** Siril `mtf 0 m 1`: lower m = brighter.
- **Separate background control (black point) from subject control (asinh+mtf).**
- **Instrument conventions are unconditional** (the ROWORDER vertical flip).
- **Category from filter + measurement, never the target's name.**
- **Scale to measured SNR** (`bgnoise`, frame count) — not to the object.

**Guardrails — deterministic QA (`seestar/qa.py`, implemented + category-aware gates):**
- **Benchmark against a reference and quantify the gap.** Star count found the bp bug;
  masked p90/p95 found the under-lift; the star asterism found the flip. *None* were
  found by staring at one image.
- **Global metrics lie — mask the object** (the "0.1% near-black = gray wash" false alarm).
- **Regression-gate the catastrophes:** source-relative star preservation (output
  vs the input's own measured count), dimensions, orientation,
  core clipping, **crushed background** (`near_black_frac_outside_mask` — the masked
  cousin of the old false alarm), **gray-wash background** (`sky_median_ceiling` —
  the too-*bright* direction near_black can't see; the M81 +PCC sky-40 regression;
  documentation exempt via the embedded profile keyword), and **neon color** (`pegged_chroma_frac_body`:
  fraction of body pixels with HSV saturation on the rail, S ≥ 0.90 — channel-agnostic,
  so honest single-band dominance like LP Hα passes while satu-to-the-rail fails;
  a plain R/G ratio would wrongly punish real emission color). Plus a soft
  star-retention warn (final vs stars-on). Calibrated: every passing final clears by
  ≥2×; the overcooked M8 fails by >2×.
- **A gate that silently skips is not a gate.** The overcooked M8 "passed" QA because
  the EXIF category read failed (unresolved exiftool path) and every category gate was
  skipped without a word. Unknown category now emits an explicit warning.
- **Gate at the instrument's scale — or source-relative.** Pixel-unit thresholds are
  locked on the S30 reference (`qa.gates.reference`) and rescaled at gate time from
  the pixel scale embedded in the render's EXIF: FWHM ceiling by the pixel-scale
  ratio (`core.qa_rules.scale_factors`). Frame-fraction laws proved NOT to scale
  (the M81 S30 Pro false alarm, then the S50 M82 false alarm in the opposite
  direction): faint structure is now judged source-relative (Diagnosis 3, 2026-08) —
  retained supported structure vs the run's own measured STRUCTURE SUPPORT — with no
  FOV scaling at all. Unreadable pixel scale → the FWHM gate runs unscaled plus an
  explicit warning (see the skip lesson above). **Not applicable to STARFIELD**
  (owner ruling, 2026-08-14): a globular's signal is primarily stars and the starless
  artifact this gate reads intentionally removes them, so an unchanged, healthy M5
  hard-failed a test it can never pass. The verdict is now an explicit
  `SKIPPED: not applicable — stars are the subject`; star fields are held to
  source-relative star preservation instead, and halo retention stays reported as a
  diagnostic that never controls certification.

**Judgment — OPERATOR / LLM. Perception and taste, where numbers run out:**
- **Validate by looking, not only by numbers** (the flip passed every numeric check).
- **Intrinsic vs fixable:** deep-red Hα from an LP filter can't be saturated into
  broadband pink — accept it; fork only when two goals are genuinely incompatible.
- **Spot anomalies metrics don't model** (trails, gradients, odd artifacts), and decide
  "done".

**Prior proposes; measurement disposes.** The FITS header carries *acquisition* priors
(filter, integration, gain, pixel scale) and, via the **WCS** (not the `OBJECT` name),
the key to fetch *astrophysical* priors (type, size, surface brightness from a catalog).
Use priors to set expectations, pick the reference/treatment intent, and **flag
disagreement** (catalog says bright nebula, data measures an empty field → mispointing/
clouds/wrong file). Priors **never inject structure the photons don't support**, and
where measurement already captures something (e.g. `bgnoise` subsumes gain/temp), the
measurement wins.

**The operator runs measured controls; it does not guess control values.** Adapt per
target by *choosing among a correctly-bounded candidate sweep the engine renders and
scores*, the way the M8 stretch was locked — not by emitting raw numbers from memory.
