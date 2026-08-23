"""Diagnosis 1 (spec 2026-08-12): measured per-edge border crop, zero as the safe
default. Three layers, tested in order:

- shell detector `measure.measure_border` -- per-edge invalid widths from the
  prepared source FITS; only DEMONSTRATED invalid signatures (non-finite, blank/
  zero, ragged partial-blank lines) count; darkness alone is ambiguous, never crop.
- pure core decision `planning.decide_crop` -- bounds, forced legacy override,
  ambiguity/failure => zero crop.
- Siril adapter `_crop` -- the empirically pinned coordinate convention (Siril
  1.4.4: crop x counts from the file's column-0 edge; crop y counts from the
  file's LAST-row edge, so with the adapter's mirrorx-before-crop order, y maps
  exactly to the raw array's row-0 edge trim).

Edge labels everywhere: `top` = the first rows of the FITS data array (numpy row
0), `bottom` = the last rows, `left`/`right` = first/last columns.
"""

from __future__ import annotations

import numpy as np
import pytest
from astropy.io import fits

from seestar.adapters.base import ExecContext
from seestar.adapters.siril import SirilAdapter
from seestar.core.planning import decide_crop
from seestar.core.types import (
    ArtifactRef,
    ArtifactRole,
    BorderObservation,
    Crop,
)
from seestar.measure import measure_border

RNG = np.random.default_rng(7)


def _frame(h=240, w=320, pedestal=3500.0, noise=40.0):
    """Raw-ADU-like stack frame (matches what the working copy actually is)."""
    return RNG.normal(pedestal, noise, (h, w)).astype(np.float32)


def _write(tmp_path, data, name="t.fit"):
    p = tmp_path / name
    fits.PrimaryHDU(data).writeto(p, overwrite=True)
    return str(p)


# ---------------------------------------------------------------------------
# shell detector: measure_border
# ---------------------------------------------------------------------------


def test_clean_frame_measures_zero_on_all_edges(tmp_path):
    d = measure_border(_write(tmp_path, _frame()))
    assert (d["left"], d["top"], d["right"], d["bottom"]) == (0, 0, 0, 0)
    assert d["confident"] is True


def test_clean_color_stack_measures_zero(tmp_path):
    color = np.stack([_frame(), _frame(), _frame()])  # (3, H, W) like a Seestar stack
    d = measure_border(_write(tmp_path, color))
    assert (d["left"], d["top"], d["right"], d["bottom"]) == (0, 0, 0, 0)
    assert d["confident"] is True


def test_real_content_touching_each_edge_is_not_cropped(tmp_path):
    f = _frame()
    f[0:6, 40:80] += 20000.0  # bright nebulosity/star flux on the top edge
    f[-6:, 100:140] += 20000.0  # ... bottom
    f[30:70, 0:6] += 20000.0  # ... left
    f[90:130, -6:] += 20000.0  # ... right
    d = measure_border(_write(tmp_path, f))
    assert (d["left"], d["top"], d["right"], d["bottom"]) == (0, 0, 0, 0)
    assert d["confident"] is True


@pytest.mark.parametrize(
    "edge,expected",
    [
        ("left", (12, 0, 0, 0)),
        ("top", (0, 12, 0, 0)),
        ("right", (0, 0, 12, 0)),
        ("bottom", (0, 0, 0, 12)),
    ],
)
def test_blank_margin_detected_only_on_the_damaged_edge(tmp_path, edge, expected):
    f = _frame()
    sl = {
        "left": (slice(None), slice(0, 12)),
        "top": (slice(0, 12), slice(None)),
        "right": (slice(None), slice(-12, None)),
        "bottom": (slice(-12, None), slice(None)),
    }[edge]
    f[sl] = 0.0
    d = measure_border(_write(tmp_path, f))
    assert (d["left"], d["top"], d["right"], d["bottom"]) == expected
    assert d["confident"] is True


def test_nonfinite_margin_detected(tmp_path):
    f = _frame()
    f[0:9, :] = np.nan
    d = measure_border(_write(tmp_path, f))
    assert (d["left"], d["top"], d["right"], d["bottom"]) == (0, 9, 0, 0)
    assert d["confident"] is True


def test_ragged_partial_blank_edge_detected(tmp_path):
    f = _frame()
    # ragged stacking border: roughly half of each of the first 8 rows is dead
    for i in range(8):
        f[i, : 160 + 10 * i] = 0.0
    d = measure_border(_write(tmp_path, f))
    assert d["top"] == 8
    assert (d["left"], d["right"], d["bottom"]) == (0, 0, 0)


def test_dark_gradient_is_ambiguous_not_cropped(tmp_path):
    f = _frame()
    # merely-dark rows (finite, nonzero) fading in from the top: NOT demonstrated
    # invalid -> zero widths, confidence withdrawn so the planner records a warning
    f[0:30, :] *= np.linspace(0.25, 1.0, 30)[:, None]
    d = measure_border(_write(tmp_path, f))
    assert (d["left"], d["top"], d["right"], d["bottom"]) == (0, 0, 0, 0)
    assert d["confident"] is False
    assert "top" in d["detail"]


def test_m82_shaped_clean_s50_frame_measures_zero(tmp_path):
    # regression stand-in for the real S50 M82 capture (1080x1920, pedestal ~3466
    # ADU, clean borders -- spec Diagnosis 1 evidence table): the measured decision
    # must be zero on all four edges, preserving all 1080x1920 pixels.
    f = RNG.normal(3466.0, 60.0, (1920, 1080)).astype(np.float32)
    d = measure_border(_write(tmp_path, f))
    assert (d["left"], d["top"], d["right"], d["bottom"]) == (0, 0, 0, 0)
    assert d["confident"] is True
    dec = decide_crop(
        (1080, 1920),
        BorderObservation(**{k: d[k] for k in ("left", "top", "right", "bottom")}, confident=True, detail=d["detail"]),
        max_auto_frac=0.05,
        forced_frac=None,
    )
    assert (dec.left, dec.top, dec.right, dec.bottom) == (0, 0, 0, 0)
    assert dec.warning is None


# ---------------------------------------------------------------------------
# core decision: decide_crop
# ---------------------------------------------------------------------------

_CLEAN = BorderObservation(left=0, top=0, right=0, bottom=0, confident=True, detail="clean")


def test_decide_clean_zero_crop_no_warning():
    d = decide_crop((1920, 1080), _CLEAN, max_auto_frac=0.05, forced_frac=None)
    assert (d.left, d.top, d.right, d.bottom) == (0, 0, 0, 0)
    assert d.forced is False and d.warning is None


def test_decide_measured_edge_applied_asymmetrically():
    obs = BorderObservation(left=0, top=12, right=5, bottom=0, confident=True, detail="top=blank:12;right=blank:5")
    d = decide_crop((1920, 1080), obs, max_auto_frac=0.05, forced_frac=None)
    assert (d.left, d.top, d.right, d.bottom) == (0, 12, 5, 0)
    assert d.warning is None


def test_decide_forced_legacy_frac_symmetric():
    d = decide_crop((1920, 1080), None, max_auto_frac=0.05, forced_frac=0.015)
    assert (d.left, d.top, d.right, d.bottom) == (28, 16, 28, 16)
    assert d.forced is True


def test_decide_ambiguous_zero_crop_with_warning():
    obs = BorderObservation(left=0, top=0, right=0, bottom=0, confident=False, detail="top=dark_ambiguous")
    d = decide_crop((1920, 1080), obs, max_auto_frac=0.05, forced_frac=None)
    assert (d.left, d.top, d.right, d.bottom) == (0, 0, 0, 0)
    assert d.warning is not None and "top=dark_ambiguous" in d.warning


def test_decide_oversized_edge_refused_never_silently_applied():
    # bound for a 1080-row frame at 0.05 = 54 px; a 300-row claim is refused
    obs = BorderObservation(left=0, top=300, right=4, bottom=0, confident=True, detail="top=blank:300;right=blank:4")
    d = decide_crop((1920, 1080), obs, max_auto_frac=0.05, forced_frac=None)
    assert d.top == 0  # refused, kept as valid data
    assert d.right == 4  # the in-bounds edge still crops
    assert d.warning is not None and "top" in d.warning


# ---------------------------------------------------------------------------
# Siril adapter: crop rectangle script (empirically pinned convention)
# ---------------------------------------------------------------------------


class _FakeWorkspace:
    def __init__(self, root):
        self.root = root

    def path_for(self, ref, ext):
        return str(self.root / f"{ref.name}{ext}")


def _crop_lines(tmp_path, monkeypatch, op, input_data):
    from seestar.adapters import siril as S

    in_path = _write(tmp_path, input_data, "in.fit")
    captured = {}

    def fake_run(lines, tools):
        captured["lines"] = lines
        return ""

    monkeypatch.setattr(S, "run_siril", fake_run)
    ctx = ExecContext(workspace=_FakeWorkspace(tmp_path), tools=None)
    ctx.resolve[op.input] = in_path
    SirilAdapter().execute(op, ctx)
    return captured["lines"]


def _crop_op(**kw):
    d = dict(
        input=ArtifactRef("working_copy", ArtifactRole.SOURCE),
        output=ArtifactRef("cropped", ArtifactRole.PRODUCTION),
        x=3,
        y=5,
        width=100,
        height=200,
        mirror_x=True,
    )
    d.update(kw)
    return Crop(**d)


def test_crop_script_mirrored_y_is_raw_top(tmp_path, monkeypatch):
    # with mirrorx before crop, Siril's y-from-last-row maps to the raw array's
    # row-0 edge -- the op's y passes through unchanged
    lines = _crop_lines(tmp_path, monkeypatch, _crop_op(), _frame(h=240, w=320))
    assert "mirrorx" in lines
    assert lines.index("mirrorx") < lines.index("crop 3 5 100 200")


def test_crop_script_unmirrored_y_converted_from_frame_height(tmp_path, monkeypatch):
    # without mirrorx, Siril's y counts from the file's LAST-row edge: converting
    # a raw row-0-edge y needs the frame height (240 - 5 - 200 = 35)
    lines = _crop_lines(tmp_path, monkeypatch, _crop_op(mirror_x=False), _frame(h=240, w=320))
    assert "mirrorx" not in lines
    assert "crop 3 35 100 200" in lines
