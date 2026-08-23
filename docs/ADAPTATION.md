# How the engine adapts to the source

This maintainer reference explains how the engine measures each source, adapts
its processing, and verifies that changes remain safe across the validation set.

**The principle:** never assume one test case generalizes—measure the file,
react to the measurement, check the output, and back-validate every engine
change against the reference set. A limited source produces an honestly modest
result; a strong source receives the full treatment.

Every adaptive mechanism, its measurement, where its constants live, and how it is
kept honest:

## 1. Content category — from filter + measured signal, never the name

`inspect.classify` derives **nebula / galaxy / starfield** from the FITS filter
(`LP` dual-band vs `IRCUT` broadband) plus the measured extended-signal fraction
`E` (StarNet-starless pixels above background + 3σ). The WCS **catalog prior**
(`prior.py`, on by default) refines galaxy-vs-globular where `E` cannot.
Object names are *never* processing inputs (locked by
`tests/test_architecture.py::test_no_target_name_conditionals_in_processing_source`).
Thresholds: `config.yaml engine.classify` (threaded explicitly — this block exists
because its predecessor key sat in config unthreaded for months, a silent no-op).
**Failure mode guarded:** if the prior is unreachable, classification degrades to
filter-only — the run notes carry the actual error and `seestar qa` warns that the
IRCUT galaxy/starfield split was resolved without it (evidence: an SSL failure once
shipped M100, a face-on spiral, as a starfield with the cluster treatment).

## 2. SNR class — from frame count + measured noise

`noisy` (few frames or high `bgnoise`) vs `clean` drives a denoise bonus
(`engine.denoise.noisy_bonus`) and a dimmer solved-stretch target
(`noisy_level_scale`) — restraint scales with measured quality, not with the
object. Thresholds: `engine.classify.noisy_*`.

## 3. Per-image solved stretch — the levels are targets, not constants

The black point is measured on the linear master (0.987 × sky median). The mtf
midtone is **solved per image** by `core.solve`, driven through `core.planning`,
so the rendered output hits a per-category level (`engine.stretch.target`),
probing the *same chain the final ships* (GHS included — the session-8 contract).
Extended targets get a **two-point solve**: black point owns the background
(one-sided `sky_ceiling` guard), mtf owns the subject. Faintness is thereby handled
automatically — a dim source gets a stronger lift, a bright one a gentler lift,
to the same rendered level.

## 4. Bound-binding warnings — a bound that binds silently is a gate that silently skips

The solver's search bounds (`engine.stretch.solver`) are engineering limits, not
looks. If a solve **converges onto a bound without reaching its target** ("pinned":
every iteration pushed one direction), the run records it
(`notes.stretch_solve.pinned` → the `{target}_notes.json` sidecar), the CLI prints
a warning, and `seestar qa` ships it in the scorecard. Plain **non-convergence**
(no pin, target missed — e.g. a jumpy probe mask on a tiny subject like M100's 6′
disk) warns the same way (`qa.solver_warnings`). Evidence for why: the old in-code
`lo=0.04` floor silently clamped four M81 renders ~18% under their solved target
before being re-locked to 0.02 through the harness.

## 5. Resolution/FOV-scaled QA gates — S30 and S30 Pro from one threshold set

Gate thresholds are locked at the S30 reference scale (`qa.gates.reference`,
1080×1920 @ 3.99″/px). At QA time, `core.qa_rules.scale_factors` rescales from the
pixel scale embedded in the render's EXIF: **FWHM ceilings** by the pixel-scale
ratio (same seeing → more px on a finer scale). **Faint structure** is
source-relative (2026-08, Diagnosis 3): retained supported structure vs the run's
own measured subject support — the inverse-FOV frame-fraction scaling is gone (it
false-alarmed in both directions: M81 S30 Pro wide, M82 S50 narrow).
Star preservation is source-relative (2026-08, Diagnosis 2): the
output is judged against the stars measured in that capture's own linear master,
never an absolute per-camera floor — the S50's narrower field only ever removes
stars. Unreadable pixel scale → gates run unscaled *with an explicit warning*.

## 6. Instrument identity — header, not assumption

The FITS `CREATOR` ("ZWO Seestar S30" / "ZWO Seestar S30 Pro") names the
instrument in metadata/annotations; the *engine* adapts only from measured dims
and WCS pixel scale. `config defaults.telescope` is a fallback for headers that
omit it.

## 7. Structure treatments — gated by measurement

- **Galaxy GHS** (dust lane / faint disk): attaches by category; its SP/LP/D are
  bracket-locked (`engine.galaxy_treatment`, provenance in the comments).
- **Cluster halo GHS**: gated by *measured concentration*
  (`measure.measure_concentration` >= `engine.cluster.concentration_min`), never
  by a name. Sparse fields stay untreated.
- **Nebula GHS**: off — measurement showed it cannot beat the solved base stretch
  (see config comment).

## 8. Pixel-scale sharpening cap

At ~4″/px the data is undersampled; `core.policy.compute_decisions` caps unsharp automatically from
the measured WCS scale (`engine.sharpening`). A different (finer) instrument would
un-cap itself the same way.

## 9. Source-limited vs pipeline-limited QA attribution

At run time the engine measures the **input's own star quality** on the linear
master (`qa.input_star_metrics`; a fixed autostretch probe as fallback for faint
fields) into the notes sidecar. If the render then fails the scaled FWHM gate but
the *input* already exceeded that ceiling, QA re-attributes the fail as
**SOURCE-LIMITED** — a tagged warning instead of a hard fail (no processing can
sharpen seeing/focus; a permanently red QA trains people to ignore QA). If the
input was sharp and the render is not, the fail stays hard: the pipeline bloated
the stars, and that *is* a regression.

## 10. Locked constants — provenance or it didn't happen

Every locked constant carries, in its `config.yaml` comment: the evidence that set
it, the data range it was validated on, and (where applicable) the signal that
fires when an input leaves that envelope. In-code fallback defaults must mirror
config (locked by `tests/test_core_engine_types.py`).

## 11. Back-validation — `seestar validate`

The enforcement of all of the above: re-render every configured reference target
from raw, QA it, diff against the committed baseline
(`validation/baselines/<T>_qa.json`). **An engine change is done only when every
available reference is identical-or-better** — lost `passed`, any new gate-fail
kind, or metric drift beyond `validation.tolerances` is a regression (exit 2).
Machine-specific raw paths skip gracefully; see `validation/README.md`.

```
seestar validate                          # diff all available targets
seestar validate --targets M81,M100       # subset
seestar validate --update-baselines ...   # capture new baselines (owner-approved changes only)
```
