"""Cluster-halo treatment: the measured concentration trigger (target-agnostic) and the
progress-plan wiring. Synthetic FITS only -- no engine run, no external tools."""

import numpy as np
from astropy.io import fits

from seestar.measure import measure_concentration
from seestar.orchestrate import plan_stages


def _write(path, data):
    fits.PrimaryHDU(data.astype("float32")).writeto(path, overwrite=True)


def test_concentration_separates_cluster_from_uniform_field(tmp_path):
    rng = np.random.default_rng(0)
    H = W = 512
    field = np.abs(rng.normal(10.0, 1.0, (H, W)))  # faint uniform field
    uni = tmp_path / "uniform.fit"
    _write(str(uni), field)
    c_uniform = measure_concentration(str(uni))["concentration"]

    yy, xx = np.mgrid[0:H, 0:W]  # add a concentrated bright core
    blob = 200.0 * np.exp(-(((xx - 256) ** 2 + (yy - 256) ** 2) / (2 * 40.0**2)))
    clu = tmp_path / "cluster.fit"
    _write(str(clu), field + blob)
    res = measure_concentration(str(clu))

    assert res["concentration"] > c_uniform  # cluster is more concentrated
    assert res["concentration"] >= 8.0  # clears the default trigger
    assert c_uniform < 8.0  # a uniform field does NOT trigger
    cx, cy = res["center_xy"]  # center localized on the core
    assert abs(cx - 256) < 48 and abs(cy - 256) < 48


def test_concentration_handles_color_cube(tmp_path):
    rng = np.random.default_rng(1)
    cube = np.abs(rng.normal(10.0, 1.0, (3, 256, 256)))  # (C,H,W) like the S30 master
    p = tmp_path / "rgb.fit"
    _write(str(p), cube)
    r = measure_concentration(str(p))
    assert "concentration" in r and r["block"] == 32


def test_plan_cluster_gets_starless_not_star_reduce():
    keys = plan_stages({"cluster": True})
    assert "starless" in keys  # QA halo artifact
    assert "star_reduce" not in keys  # clusters keep their stars; starless is QA-only


def test_plan_sparse_field_no_starless():
    assert "starless" not in plan_stages({})  # no forks, no cluster
