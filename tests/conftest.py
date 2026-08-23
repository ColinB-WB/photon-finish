"""Shared test helpers: real tiny FITS writers (the repo mock guide — test doubles
that behave like the real thing; real files, real headers, minimal but real)."""

from __future__ import annotations

import os

import numpy as np
import pytest
from astropy.io import fits


def write_fake_stack(
    path,
    creator="ZWO Seestar S30",
    stackcnt=51,
    filt="LP",
    obj="X1",
    date_obs="2026-06-16T08:13:23.747145",
    exposure=10.0,
    shape=(3, 64, 64),
    seed=0,
):
    """A REAL small FITS with a Seestar-shaped header. Pass None for any header
    kwarg to OMIT that card (fallback tests). shape=(64, 64) makes a 2-D raw sub."""
    rng = np.random.default_rng(seed)
    data = rng.normal(1000.0, 30.0, size=shape).clip(0, 65535).astype("uint16")
    h = fits.Header()
    for key, val in [
        ("CREATOR", creator),
        ("STACKCNT", stackcnt),
        ("FILTER", filt),
        ("OBJECT", obj),
        ("DATE-OBS", date_obs),
        ("EXPOSURE", exposure),
    ]:
        if val is not None:
            h[key] = val
    fits.PrimaryHDU(data=data, header=h).writeto(str(path), overwrite=True)
    return str(path)


@pytest.fixture
def fake_stack():
    return write_fake_stack


def write_small_fits(path, seed=0, shape=(3, 32, 32)):
    """A REAL small linear-space FITS -- used by the op-adapter fakes below so
    `measure()`/`fits.open()` calls downstream of a faked stage see real, reproducible
    (seeded) pixel data instead of a stub."""
    rng = np.random.default_rng(seed)
    data = (rng.normal(0.015, 0.001, size=shape).clip(0, 1) * 65535).astype("uint16")
    fits.PrimaryHDU(data=data).writeto(str(path), overwrite=True)
    return str(path)


def _write_fake_jpg(path, seed=11, size=(64, 64)):
    """A tiny REAL RGB jpg (seeded noise + a brighter center blob) -- enough for
    qa.measure_render's PIL decode + object_mask threshold to produce a real
    (non-degenerate) body/background split, without shelling to any tool."""
    from PIL import Image

    rng = np.random.default_rng(seed)
    h, w = size
    arr = rng.normal(20.0, 5.0, size=(h, w, 3)).clip(0, 255)
    yy, xx = np.mgrid[0:h, 0:w]
    cy, cx = h / 2, w / 2
    blob = np.exp(-(((yy - cy) ** 2 + (xx - cx) ** 2) / (2 * (min(h, w) / 6) ** 2)))
    arr = (arr + blob[..., None] * 180.0).clip(0, 255).astype("uint8")
    Image.fromarray(arr, mode="RGB").save(str(path), format="JPEG")


class FakeTools:
    """Minimal fake tools object for mocking resolve_tools() in tests."""

    siril = "siril-cli"
    graxpert = "GraXpert"
    starnet2 = "starnet2"
    exiftool = "exiftool"


def install_fake_op_adapters(monkeypatch, seeds=None):
    """Install small real-file-writing fake adapters for every tool-shelling op type
    (Siril/GraXpert/StarNet2), so `run_target` can execute end-to-end through the
    executor with no external tool binary on PATH -- the same "fake at the subprocess
    boundary, real files" approach as `write_fake_stack`, applied one layer up (at the
    adapter registry instead of the subprocess boundary) so a full run_target/executor
    integration test stays hermetic. `seeds` (dict, op-name -> int) lets a caller pick
    the RNG seed per stage where numeric reproducibility across two runs matters (e.g.
    recipe replay); PrepareSource needs no fake (BuiltinAdapter is a real filesystem
    copy, no external tool)."""
    from seestar.adapters import base as B
    from seestar.core.types import (
        BackgroundExtract,
        ColorCalibrate,
        CombineSessions,
        Crop,
        CropTo,
        Denoise,
        Export,
        ScreenBlend,
        SeparateStars,
        StretchRender,
    )

    seeds = seeds or {}

    class _FitsAdapter:
        """Ignores real input; writes a fresh small real FITS for every declared
        output ref of the op (Crop/BackgroundExtract/Denoise/ColorCalibrate/
        StretchRender/ScreenBlend/CropTo/CombineSessions all have exactly one)."""

        def __init__(self, name, seed):
            self.name = name
            self._seed = seed

        def execute(self, op, ctx):
            out = {}
            for ref in op.outputs:
                p = ctx.workspace.path_for(ref, ".fit")
                write_small_fits(p, self._seed)
                out[ref] = p
            return out

    class _ColorAdapter(_FitsAdapter):
        """Mirrors the real Siril ColorCalibrate contract (Diagnosis 4): a pcc=True
        run REPORTS evidence into ctx.pcc_evidence for the core to judge. The fake
        chain models healthy tools, so it reports a healthy M81-style solution --
        without this, every faked IRCUT-galaxy run would take the no_evidence
        native fallback instead of the calibrated path real runs ship."""

        def execute(self, op, ctx):
            if op.pcc:
                from seestar.core.types import PccEvidence

                ctx.pcc_evidence.append(
                    PccEvidence(
                        succeeded=True,
                        imprecise=False,
                        few_stars_warning=False,
                        stars_used=330,
                        coefficients=(0.471, 0.713, 1.0),
                        detail="using 330 stars",
                    )
                )
            return super().execute(op, ctx)

    class _ExportAdapter:
        """Real render()/StarNet2 always wrote a real (if here, empty) file per
        format; downstream code only checks existence + embeds metadata, never
        pixel content, so empty real files were historically sufficient.

        Phase 3.0's candidate lifecycle (orchestrate.py::run_target) now reads
        REAL pixels from the exported jpg (qa.measure_render -> PIL Image.open)
        to measure the hero candidate before promoting it -- an empty file
        raises PIL.UnidentifiedImageError. The jpg format therefore gets a
        tiny real (seeded-noise) image; tif/png stay empty stubs since nothing
        reads their pixel content."""

        name = "fake-export"

        def execute(self, op, ctx):
            out = {}
            for ref in op.outputs:
                p = ctx.workspace.path_for(ref, "")  # ref.name already carries the extension
                if ref.name.endswith(".jpg"):
                    _write_fake_jpg(p)
                else:
                    with open(p, "wb"):
                        pass
                out[ref] = p
            return out

    class _SeparateStarsAdapter:
        name = "fake-starnet2"

        def execute(self, op, ctx):
            starless_p = ctx.workspace.path_for(op.starless, ".tif")
            stars_p = ctx.workspace.path_for(op.stars, ".tif")
            for p in (starless_p, stars_p):
                with open(p, "wb"):
                    pass
            return {op.starless: starless_p, op.stars: stars_p}

    monkeypatch.setitem(B.REGISTRY, Crop, _FitsAdapter("fake-crop", seeds.get("crop", 2)))
    monkeypatch.setitem(B.REGISTRY, BackgroundExtract, _FitsAdapter("fake-bge", seeds.get("bge", 3)))
    monkeypatch.setitem(B.REGISTRY, Denoise, _FitsAdapter("fake-denoise", seeds.get("denoise", 4)))
    monkeypatch.setitem(B.REGISTRY, ColorCalibrate, _ColorAdapter("fake-color", seeds.get("color", 5)))
    monkeypatch.setitem(B.REGISTRY, StretchRender, _FitsAdapter("fake-stretch", seeds.get("stretch", 6)))
    monkeypatch.setitem(B.REGISTRY, ScreenBlend, _FitsAdapter("fake-blend", seeds.get("blend", 7)))
    monkeypatch.setitem(B.REGISTRY, CropTo, _FitsAdapter("fake-cropto", seeds.get("cropto", 8)))
    monkeypatch.setitem(B.REGISTRY, CombineSessions, _FitsAdapter("fake-combine", seeds.get("combine", 9)))
    monkeypatch.setitem(B.REGISTRY, Export, _ExportAdapter())
    monkeypatch.setitem(B.REGISTRY, SeparateStars, _SeparateStarsAdapter())


def install_fake_run_siril(monkeypatch, seed=42):
    """Fake `adapters.siril.run_siril` itself, for the few DIRECT (non-adapter) call
    sites that stay legacy (e.g. run_target's starless-FITS persistence after
    SeparateStars, and the E-gate/render-metric probes). Parses the tiny subset of
    script lines those call sites emit (`cd`/`save`/`savetif`/`savepng`/`savejpg`)
    and writes small real files instead of shelling to Siril."""
    import seestar.adapters.siril as siril_module

    def _fake(lines, tools):
        cwd = "."
        for raw in lines:
            ln = raw.strip()
            if ln.startswith("cd "):
                cwd = ln.split(" ", 1)[1].strip().strip('"')
            elif ln.startswith("save "):
                write_small_fits(os.path.join(cwd, ln.split(" ", 1)[1].strip() + ".fit"), seed)
            elif ln.startswith("savetif "):
                open(os.path.join(cwd, ln.split(" ", 1)[1].strip() + ".tif"), "wb").close()
            elif ln.startswith("savepng "):
                open(os.path.join(cwd, ln.split(" ", 1)[1].strip() + ".png"), "wb").close()
            elif ln.startswith("savejpg "):
                name = ln.split(" ")[1]
                open(os.path.join(cwd, name + ".jpg"), "wb").close()
        return ""

    monkeypatch.setattr(siril_module, "run_siril", _fake)


def fake_probe_metric(jpg_path, metric, mask_cfg=None, **kw):
    """Hermetic stand-in for `execute._probe_metric` -- the ONE seam that reads a
    probe JPG (a second reader once bypassed these stubs and broke 105 tests).

    Honors `freeze_to` (owner ruling, 2026-08-14) by persisting a REAL raster, so
    the solve-region freeze, its registration, its publication as a qa_artifact,
    and a later `seestar qa` replay all exercise a file that exists -- a stub that
    silently skipped the write would leave every one of those paths untested.
    dims match `write_fake_stack`'s 64x64 frame, like
    `fake_measure_subject_support`."""
    freeze_to = kw.get("freeze_to")
    if freeze_to:
        import numpy as np
        from PIL import Image

        mask = np.zeros((64, 64), dtype=bool)
        mask[16:48, 16:48] = True
        Image.fromarray(mask.astype("uint8") * 255).save(freeze_to)
    return 72.0 if metric == "body_p90" else 10.0


def fake_measure_subject_support(linear_fit, workdir, target, tools, support_png, expected_frac=None):
    """Hermetic stand-in for the StarNet-backed subject-support measurement
    (Diagnoses 5+3): writes a REAL small PNG raster (so registration and
    publication paths exercise a real file) and returns a valid compact-subject
    summary. dims match `write_fake_stack`'s default 64x64 frame with the
    zero-crop decision the clean fake stack measures -- the planner's support
    geometry check compares against the post-crop frame.

    `expected_frac` mirrors the real signature (Owner Gate 2's catalogued-extent
    corroboration) so this fake keeps failing loudly if the real call site drifts."""
    import numpy as np
    from PIL import Image

    mask = np.zeros((64, 64), dtype=bool)
    mask[16:48, 16:48] = True
    Image.fromarray(mask.astype("uint8") * 255).save(support_png)
    return mask, {"area_frac": 0.25, "dims": (64, 64), "source": "starless_sigma", "valid": True, "detail": "fake"}
