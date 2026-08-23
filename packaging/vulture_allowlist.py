"""Reviewed Vulture false positives (2026-07-28), used indirectly via telemetry/
serialization dicts or reflective access — Vulture can't see those paths.
Reproduce the clean baseline (AFTER Task 14 removes Adapter.available()):
    vulture seestar packaging tools tests packaging/vulture_allowlist.py \
        --min-confidence 60 --ignore-decorators "@pytest.fixture" \
        --ignore-names scratch_registry,pkg,cfg_arg,out_dir_arg
This is the FINAL command (Task 12 Step 5b trial run). At Task 12 the only
expected residue is the `Adapter.available()` findings (removed in Task 14b);
the definitive zero-output run is post-plan, after that removal.

`--ignore-names` covers callback-signature parameters that are unused inside
their own body by design (a pytest fixture consumed only for its side effect,
or a monkeypatched callable's positional args unused after the ones the test
actually asserts on): `scratch_registry` (tests/test_adapters_base.py),
`pkg` (tests/test_known_targets_load.py), `cfg_arg`/`out_dir_arg`
(tests/test_validate.py). Renaming any of these at their definition would
either break pytest's name-based fixture injection or make an already-narrow
lambda signature harder to read for no benefit, so they're recorded here
instead of touched in source.
"""

from seestar.core.types import FramingConfig, PriorObservation, RenderMetrics

PriorObservation.fov_fraction
RenderMetrics.envelope_area_frac
RenderMetrics.body_p95
RenderMetrics.core_value_median
RenderMetrics.global_black_frac
# Dataclass field written via constructor kwargs (io.py) but not yet read via
# attribute access anywhere: real, not junk. `subject_threshold_sigma` is the
# exact knob Task 13 wires into measure_subject. Vulture can't see that path.
FramingConfig.subject_threshold_sigma
# NOTE: Adapter.available() is REMOVED in Task 14b — intentionally NOT allowlisted here.
# NOTE: RecoveryDefaults.chroma_safety_margin is REMOVED in Task 14a — intentionally NOT allowlisted here.
# NOTE: PlanningState.trail_present is REMOVED in Task 14c — intentionally NOT allowlisted here.
