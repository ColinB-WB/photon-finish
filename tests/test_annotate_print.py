"""Phase D: documentation annotation overlay (N/E from the CD matrix, scale bar).

The print-profile paper tuning (black lift + print-safe params) coverage moved to
tests/test_core_policy.py::test_print_profile_paper_tuning_vs_showcase (Task 23:
policy.py deleted; this file's compute_params-based check was unrelated to the
annotation overlay it otherwise tests)."""

import numpy as np
from PIL import Image

from seestar.adapters._annotate_impl import _scale_bar_len, annotate, ne_vectors_image

# NGC5128 CD matrix (validated: saved deliverable == raw array orientation -> N up, E left).
NGC5128_CD = {"CD1_1": -0.00109987, "CD1_2": -0.000144297, "CD2_1": 0.000145044, "CD2_2": -0.00110046}


def test_ne_vectors_north_up_east_left():
    n, e = ne_vectors_image(NGC5128_CD, flipud=True)
    assert n[1] < 0  # North points up (PIL y is down)
    assert e[0] < 0  # East points left
    # roughly perpendicular unit vectors
    assert abs(n[0] * e[0] + n[1] * e[1]) < 0.1
    assert abs((n[0] ** 2 + n[1] ** 2) - 1.0) < 1e-6


def test_ne_vectors_flip_inverts_y():
    n_up, _ = ne_vectors_image(NGC5128_CD, flipud=True)
    n_dn, _ = ne_vectors_image(NGC5128_CD, flipud=False)
    assert np.sign(n_up[1]) == -np.sign(n_dn[1])


def test_ne_vectors_missing_cd():
    assert ne_vectors_image({}, flipud=True) == (None, None)


def test_scale_bar_round_length():
    arcmin, bar_px = _scale_bar_len(3.99, 1048)  # Seestar scale, doc-render width
    assert arcmin in (1, 2, 5, 10, 15, 20, 30, 60)
    assert 0.10 * 1048 < bar_px < 0.25 * 1048  # ~12-22% of width


def test_annotate_writes_file(tmp_path):
    img = (np.random.default_rng(0).random((400, 220, 3)) * 40).astype("uint8")
    src = str(tmp_path / "in.jpg")
    Image.fromarray(img).save(src)
    out = str(tmp_path / "out.jpg")
    info = {
        "title": "M8 — Lagoon Nebula",
        "subtitle": "Nebula",
        "lines": ["51x10s = 8.5 min | LP filter", "2026-06-15 | ZWO Seestar S30"],
        "pixscale_arcsec": 3.99,
        "cd": NGC5128_CD,
    }
    annotate(src, out, info)
    assert Image.open(out).size == (220, 400)
