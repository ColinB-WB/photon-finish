"""Config-promoted constants (2026-07): classify thresholds, solver bounds, and the
QA object-mask parameters live in config.yaml and are THREADED explicitly. The
in-code defaults must mirror config.yaml -- the bug class this locks out is the
silent config no-op (engine.extended_signal_threshold was in config for months but
never actually reached classify())."""

import inspect as pyinspect

from seestar.inspect import classify
from seestar.io import load_config
from seestar.qa import _mask_kw, object_mask

_CFG = load_config()


def _meta(frames=60, filt="IRCUT"):
    return {"filter": filt, "frames": frames}


def _stats(bgnoise=0.001):
    # background/max are required by stats_to_measurements (core.Measurements
    # bounds); these tests only exercise the noisy/E thresholds, so any valid
    # in-range values are fine.
    return {"bgnoise": bgnoise, "background": 0.01, "max": 0.02}


# --- classify honors classify_cfg -----------------------------------------------


def test_noisy_frames_boundary():
    cc = _CFG["engine"]["classify"]
    below = cc["noisy_frames_below"]
    assert classify(_meta(frames=below - 1), _stats(), classify_cfg=cc)["snr_class"] == "noisy"
    assert classify(_meta(frames=below), _stats(), classify_cfg=cc)["snr_class"] == "clean"


def test_noisy_bgnoise_boundary():
    cc = _CFG["engine"]["classify"]
    thr = cc["noisy_bgnoise_above"]
    assert classify(_meta(), _stats(bgnoise=thr * 1.01), classify_cfg=cc)["snr_class"] == "noisy"
    assert classify(_meta(), _stats(bgnoise=thr * 0.99), classify_cfg=cc)["snr_class"] == "clean"


def test_extended_signal_threshold_from_cfg():
    cc = {"extended_signal_threshold": 0.10}
    # E below the custom threshold -> starfield; above -> galaxy (IRCUT)
    assert classify(_meta(), _stats(), E=0.05, classify_cfg=cc)["category"] == "starfield"
    assert classify(_meta(), _stats(), E=0.15, classify_cfg=cc)["category"] == "galaxy"
    # same E against the (lower) default threshold -> galaxy
    assert classify(_meta(), _stats(), E=0.05)["category"] == "galaxy"


def test_cfgless_classify_matches_config_values():
    cc = _CFG["engine"]["classify"]
    for frames in (cc["noisy_frames_below"] - 1, cc["noisy_frames_below"]):
        for bg in (cc["noisy_bgnoise_above"] * 0.5, cc["noisy_bgnoise_above"] * 2):
            for E in (cc["extended_signal_threshold"] * 0.5, cc["extended_signal_threshold"] * 2):
                bare = classify(_meta(frames=frames), _stats(bg), E=E)
                cfgd = classify(_meta(frames=frames), _stats(bg), E=E, classify_cfg=cc)
                assert bare == cfgd, "in-code defaults must mirror config.yaml engine.classify"


# --- in-code defaults mirror config.yaml (solver, mask) --------------------------

# test_default_solver_mirrors_config (DEFAULT_SOLVER == config.yaml engine.stretch.solver)
# deleted Task 23 (pipeline.py's DEFAULT_SOLVER/solve_mtf/solve_stretch_two_point are
# dead in production -- the slice-5 planner drives core/solve.py's state machine
# directly). This exact property (config.yaml's solver block matching the typed
# default) is already covered by
# test_core_engine_types.py::test_engine_defaults_from_empty_cfg_matches_config_yaml.


def test_mask_defaults_mirror_config():
    mask_cfg = _CFG["qa"]["mask"]
    sig = pyinspect.signature(object_mask)
    assert sig.parameters["k"].default == mask_cfg["k"]
    assert sig.parameters["blur"].default == mask_cfg["blur"]
    assert _mask_kw(mask_cfg) == {"k": mask_cfg["k"], "blur": mask_cfg["blur"]}
    assert _mask_kw(None) == {}


def test_old_config_key_removed():
    # the silently-unthreaded key must not linger as a decoy
    assert "extended_signal_threshold" not in _CFG["engine"]
