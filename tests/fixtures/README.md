# Committed test fixtures (spec 2026-07-13 §6)

## Provenance & redistribution

These three crops are derived from original Seestar S30 / S30 Pro captures taken by
Colin Bryar (the repository author). The author owns the source data and grants
permission to redistribute these fixtures publicly under the repository's LICENSE
(MIT). They are 256×256 center crops with headers preserved verbatim.

Real 256x256 center crops of real Seestar captures — headers verbatim (CREATOR,
STACKCNT, FILTER, BZERO/BSCALE, WCS+SIP with CRPIX shifted to the crop). Regenerate
with `python tools/make_fixture.py <src> <dest>`.

| File | Model | Filter | Category | Source capture |
|---|---|---|---|---|
| M8_S30_LP_nebula.fit | S30 | LP | nebula | Stacked_51_M 8_10.0s_LP_20260615-221349 |
| M81_S30Pro_IRCUT_galaxy.fit | S30 Pro | IRCUT | galaxy | Stacked_60_M 81_10.0s_IRCUT_20260703-220445 |
| NGC5139_S30_IRCUT_starfield.fit | S30 | IRCUT | starfield | NGC 5139 — S30, IRCUT, 69×10 s (690 s total), DATE-OBS 2026-06-16T06:47:58 (header-verbatim) |

Filenames deliberately do NOT match `Stacked_*` — they double as renamed-file
evidence for content-first discovery. LIMITS: crops are wrong for whole-frame
properties (star-preservation gates, frame-fraction gates, end-to-end QA) — those live
in `seestar validate` with full local captures. `golden_M81_recipe.json` (Task 7)
locks recipe STRUCTURE, not engine values.
