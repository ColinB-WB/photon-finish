"""Task 13: concrete adapters wired behind the registry.

Registry completeness (every op type in the closed Operation union has exactly one
adapter), a BuiltinAdapter happy path (real filesystem, no external tool), and one
outcome-asserting test per tool-shelling adapter (GraXpert/StarNet2/Siril/exiftool) --
each fakes the external tool at the subprocess boundary with a small real executable
(a .bat launching a Python script) that writes real minimal output files, so the test
exercises the adapter's actual path plumbing rather than mocking the adapter itself.
"""

from __future__ import annotations

import os
import shlex
import sys
import textwrap
import typing

import pytest

from seestar.adapters import base as B
from seestar.adapters._external_process import ExternalToolTimeout
from seestar.adapters.builtin import BuiltinAdapter
from seestar.adapters.exiftool import ExiftoolAdapter
from seestar.adapters.graxpert import GraxpertAdapter
from seestar.adapters.siril import SirilAdapter
from seestar.adapters.starnet import StarNetAdapter
from seestar.core.types import (
    ArtifactRef,
    ArtifactRole,
    BackgroundExtract,
    ColorCalibrate,
    EmbedMetadata,
    MetadataCard,
    Operation,
    PrepareSource,
    SeparateStars,
)
from tests.conftest import write_fake_stack


def _ref(name, role=ArtifactRole.PRODUCTION):
    return ArtifactRef(name=name, role=role)


def _write_fake_tool(tmp_path, name: str, body: str) -> str:
    """A REAL small executable (a Python script behind an OS-appropriate launcher)
    standing in for an external tool -- the same "fake at the subprocess boundary,
    real files" approach the repo already uses for FITS fixtures (tests/conftest.py),
    applied to tools. Cross-platform: a .bat wrapper on Windows, a /bin/sh wrapper on
    POSIX (a plain shebang would break if sys.executable's path contains spaces)."""
    py_path = tmp_path / f"{name}.py"
    py_path.write_text(textwrap.dedent(body), encoding="utf-8")
    if os.name == "nt":
        bat_path = tmp_path / f"{name}.bat"
        bat_path.write_text(
            f'@echo off\r\n"{sys.executable}" "{py_path}" %*\r\nexit /b %errorlevel%\r\n', encoding="utf-8"
        )
        return str(bat_path)
    sh_path = tmp_path / name
    sh_path.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(py_path))} "$@"\n', encoding="utf-8"
    )
    sh_path.chmod(0o755)
    return str(sh_path)


class _FakeTools:
    def __init__(self, **kw):
        self.siril = kw.get("siril", "siril")
        self.graxpert = kw.get("graxpert", "graxpert")
        self.starnet2 = kw.get("starnet2", "starnet2")
        self.exiftool = kw.get("exiftool", "exiftool")
        # H1 Task 4: every adapter now routes through run_external, which reads
        # tools.timeouts.for_tool(...) before it ever spawns the fake tool -- a
        # ToolPaths-shaped fake without this attribute raises AttributeError
        # before the behavior under test is reached. Defaults to the settled
        # ToolTimeouts() so the existing successful-path tests below are
        # unaffected (their fake tools return almost instantly, well inside any
        # of the settled 120s-3600s defaults).
        self.timeouts = kw.get("timeouts") or B.ToolTimeouts()


def _write_sleeper_tool(tmp_path, name: str, seconds: float = 2.0) -> str:
    """A REAL small executable that only sleeps -- stands in for a hung/slow
    external tool so the timeout tests exercise the real run_external deadline
    and process-tree cleanup, not a mocked subprocess call."""
    return _write_fake_tool(tmp_path, name, f"import time\ntime.sleep({seconds})\n")


def test_registry_complete():
    import seestar.adapters  # noqa: F401  populates REGISTRY

    missing = [t.__name__ for t in typing.get_args(Operation) if t not in B.REGISTRY]
    assert not missing, f"unrouted operations: {missing}"


def test_builtin_prepare_source(tmp_path):
    from seestar.workspace import Workspace

    (tmp_path / "in").mkdir()
    src = write_fake_stack(tmp_path / "in" / "Stacked_51_X 1_10.0s_LP_20260615-221349.fit")
    ws = Workspace(str(tmp_path / "out"))
    ref = ArtifactRef("working_copy", ArtifactRole.SOURCE)
    op = PrepareSource(session_index=0, output=ref)
    ctx = B.ExecContext(workspace=ws, tools=None, resolve={"__session_paths__": [src]})
    out = BuiltinAdapter().execute(op, ctx)
    path = out[ref]
    assert os.path.exists(path) and " " not in os.path.basename(path)


def test_graxpert_adapter_writes_registered_output(tmp_path):
    from astropy.io import fits

    from seestar.workspace import Workspace

    fake = _write_fake_tool(
        tmp_path,
        "fake_graxpert",
        """
        import os
        import sys

        import numpy as np
        from astropy.io import fits

        argv = sys.argv[1:]
        out_base = argv[argv.index("-output") + 1]
        input_path = next(a for a in argv if os.path.isfile(a))
        in_dir = os.path.dirname(os.path.abspath(input_path))
        fits.PrimaryHDU(data=np.zeros((3, 4, 4), dtype="float32")).writeto(
            os.path.join(in_dir, out_base + ".fits"), overwrite=True
        )
        """,
    )
    ws = Workspace(str(tmp_path / "out"))
    input_ref = _ref("linear_master")
    input_path = ws.path_for(input_ref, ".fit")
    fits.PrimaryHDU(data=None).writeto(input_path)  # any real file GraXpert can "see"
    output_ref = _ref("bge")
    ctx = B.ExecContext(workspace=ws, tools=_FakeTools(graxpert=fake), resolve={input_ref: input_path})
    op = BackgroundExtract(input=input_ref, output=output_ref, smoothing=0.5, correction="Subtraction")

    out = GraxpertAdapter().execute(op, ctx)

    produced = out[output_ref]
    assert produced.endswith(".fits") and os.path.exists(produced)
    assert os.path.dirname(produced) == os.path.dirname(input_path)  # GraXpert's own-dir gotcha


def test_starnet_adapter_writes_starless_and_stars(tmp_path):
    from PIL import Image

    from seestar.workspace import Workspace

    fake = _write_fake_tool(
        tmp_path,
        "fake_starnet2",
        """
        import sys

        import numpy as np
        from PIL import Image

        argv = sys.argv[1:]
        out = argv[argv.index("--output") + 1]
        unscreen = argv[argv.index("--unscreen") + 1]
        Image.fromarray(np.zeros((4, 4, 3), dtype="uint8")).save(out)
        Image.fromarray(np.zeros((4, 4, 3), dtype="uint8")).save(unscreen)
        """,
    )
    import numpy as np

    ws = Workspace(str(tmp_path / "out"))
    input_ref = _ref("stretched")
    input_path = ws.path_for(input_ref, ".tif")
    Image.fromarray(np.zeros((4, 4, 3), dtype="uint8")).save(input_path)
    starless_ref, stars_ref = _ref("starless"), _ref("stars")
    ctx = B.ExecContext(workspace=ws, tools=_FakeTools(starnet2=fake), resolve={input_ref: input_path})
    op = SeparateStars(input=input_ref, starless=starless_ref, stars=stars_ref)

    out = StarNetAdapter().execute(op, ctx)

    assert os.path.exists(out[starless_ref]) and os.path.exists(out[stars_ref])


def test_starnet_adapter_converts_fits_input_to_16bit_tif(tmp_path):
    """A Plan routes SeparateStars at the `stretched` PRODUCTION artifact, which the
    SirilAdapter writes as .fit -- starnet2 only reads TIFF/PNG, so the adapter must
    convert at the tool boundary, quantizing exactly like Siril savetif."""
    import numpy as np
    import tifffile
    from astropy.io import fits

    from seestar.workspace import Workspace

    fake = _write_fake_tool(
        tmp_path,
        "fake_starnet2",
        """
        import sys

        import numpy as np
        from PIL import Image

        argv = sys.argv[1:]
        inp = argv[argv.index("--input") + 1]
        assert inp.lower().endswith(".tif"), f"starnet2 got a non-TIFF input: {inp}"
        out = argv[argv.index("--output") + 1]
        unscreen = argv[argv.index("--unscreen") + 1]
        Image.fromarray(np.zeros((4, 5, 3), dtype="uint8")).save(out)
        Image.fromarray(np.zeros((4, 5, 3), dtype="uint8")).save(unscreen)
        """,
    )
    ws = Workspace(str(tmp_path / "out"))
    input_ref = _ref("stretched")
    input_path = ws.path_for(input_ref, ".fit")
    # channel-first (3,H,W) float, like a Siril-written stretched master; includes
    # out-of-range values (clip) and a rounding-boundary value (half away from zero).
    # float64 so the boundary value x65535 lands EXACTLY on 2.5 (float32 storage
    # perturbs it below the half and the rounding rule would be untestable).
    data = np.zeros((3, 4, 5), dtype="float64")
    data[0, 0, 0] = 0.5
    data[1, 1, 1] = 1.5  # clips to 1.0 -> 65535
    data[2, 2, 2] = -0.25  # clips to 0.0 -> 0
    data[0, 3, 4] = 2.5 / 65535.0  # x65535 = 2.5 exactly -> away-from-zero gives 3 (banker's would give 2)
    fits.PrimaryHDU(data=data).writeto(input_path)
    starless_ref, stars_ref = _ref("starless"), _ref("stars")
    ctx = B.ExecContext(workspace=ws, tools=_FakeTools(starnet2=fake), resolve={input_ref: input_path})
    op = SeparateStars(input=input_ref, starless=starless_ref, stars=stars_ref)

    out = StarNetAdapter().execute(op, ctx)

    assert os.path.exists(out[starless_ref]) and os.path.exists(out[stars_ref])
    tif_path = os.path.splitext(input_path)[0] + ".tif"
    assert os.path.exists(tif_path)
    img = tifffile.imread(tif_path)
    assert img.dtype == np.uint16 and img.shape == (4, 5, 3)  # (H,W,3), transposed
    # savetif parity is BOTH quantization AND row order: no ROWORDER header ->
    # BOTTOM-UP per the FITS standard -> the raster is the FITS rows flipped.
    expected = np.floor(np.clip(np.transpose(data, (1, 2, 0)), 0.0, 1.0) * 65535.0 + 0.5)[::-1]
    assert np.array_equal(img, expected.astype("uint16"))
    assert img[3, 0, 0] == 32768 and img[2, 1, 1] == 65535 and img[1, 2, 2] == 0  # FITS row r -> TIF row H-1-r
    assert img[0, 4, 0] == 3  # round half AWAY FROM ZERO (banker's rounding would give 2)


def test_fits_to_starnet_tif_row_order_convention(tmp_path):
    """Milestone regression lock (owner-caught vertical flip): FITS row 0 is the
    BOTTOM scanline, a TIFF's row 0 the TOP. With no ROWORDER header (BOTTOM-UP per
    the FITS standard) the TIF's row 0 must equal the LAST FITS row; an explicit
    ROWORDER=TOP-DOWN FITS is already raster order and is written as-is."""
    import numpy as np
    import tifffile
    from astropy.io import fits

    from seestar.adapters.starnet import _fits_to_starnet_tif

    # asymmetric pattern: each row's value = its FITS row index (rows distinguishable)
    h, w = 4, 5
    data = np.zeros((3, h, w), dtype="float64")
    for r in range(h):
        data[:, r, :] = (r + 1) / 10.0

    no_roworder = str(tmp_path / "no_roworder.fit")
    fits.PrimaryHDU(data=data).writeto(no_roworder)
    img = tifffile.imread(_fits_to_starnet_tif(no_roworder))
    expected_last_fits_row = np.floor(data[0, h - 1, 0] * 65535.0 + 0.5)
    assert (img[0] == expected_last_fits_row).all(), "TIF row 0 must equal the LAST FITS row (BOTTOM-UP default)"
    assert (img[h - 1] == np.floor(data[0, 0, 0] * 65535.0 + 0.5)).all()

    bottom_up = str(tmp_path / "bottom_up.fit")
    hdu = fits.PrimaryHDU(data=data)
    hdu.header["ROWORDER"] = "BOTTOM-UP"
    hdu.writeto(bottom_up)
    assert np.array_equal(tifffile.imread(_fits_to_starnet_tif(bottom_up)), img)  # explicit == default

    top_down = str(tmp_path / "top_down.fit")
    hdu = fits.PrimaryHDU(data=data)
    hdu.header["ROWORDER"] = "TOP-DOWN"
    hdu.writeto(top_down)
    img_td = tifffile.imread(_fits_to_starnet_tif(top_down))
    assert (img_td[0] == np.floor(data[0, 0, 0] * 65535.0 + 0.5)).all(), "TOP-DOWN data is already raster order"


def test_subject_box_row_convention_parity_with_legacy(tmp_path):
    """Closeup geometry lock: legacy measured the subject on a FITS produced by
    Siril savetif -> StarNet -> Siril load+save (baseline-approved pairing with the
    Siril `crop` that follows). Model Siril's conventions (savetif writes the raster
    top-down; save writes FITS bottom-up), run the NEW conversion
    (_fits_to_starnet_tif), and assert the measured subject centroid lands at the
    SAME array row as measuring the source FITS directly -- i.e. box coordinates
    stay in the source-FITS array frame. Pre-fix (no flip) this mirrors cy and the
    closeup crop is vertically mis-centered."""
    import numpy as np
    import tifffile
    from astropy.io import fits

    from seestar.adapters.starnet import _fits_to_starnet_tif
    from seestar.framing import measure_subject

    h, w = 64, 48
    rng = np.random.default_rng(3)
    a = rng.normal(0.02, 0.001, size=(h, w))
    a[8:16, 20:28] = 0.9  # asymmetric subject near array row ~12 (far from center)
    src = str(tmp_path / "stretched.fit")
    fits.PrimaryHDU(data=a).writeto(src)  # no ROWORDER -> BOTTOM-UP default

    ref = measure_subject(src)  # what legacy effectively measured (see docstring)
    assert ref is not None and abs(ref["cy"] - 11.5) < 2.0

    tif = _fits_to_starnet_tif(src)  # NEW conversion (savetif parity)
    starless_tif = tifffile.imread(tif)  # StarNet modeled as identity
    # Siril `load <tif>; save <fits>`: top-down raster in, bottom-up FITS out.
    round_trip = str(tmp_path / "subject_starless.fit")
    fits.PrimaryHDU(data=np.asarray(starless_tif, dtype="float64")[::-1] / 65535.0).writeto(round_trip)

    got = measure_subject(round_trip)
    assert got is not None
    assert abs(got["cy"] - ref["cy"]) < 1.0, f"subject row moved: legacy cy={ref['cy']}, new cy={got['cy']}"
    assert abs(got["cx"] - ref["cx"]) < 1.0


def test_siril_adapter_color_calibrate_writes_fit(tmp_path):
    from astropy.io import fits

    from seestar.workspace import Workspace

    fake = _write_fake_tool(
        tmp_path,
        "fake_siril",
        """
        import os
        import sys

        import numpy as np
        from astropy.io import fits

        script_path = sys.argv[sys.argv.index("-s") + 1]
        cwd = "."
        for raw in open(script_path, encoding="utf-8").read().splitlines():
            ln = raw.strip()
            if ln.startswith("cd "):
                cwd = ln.split(" ", 1)[1].strip('"')
            elif ln.startswith("save "):
                name = ln.split(" ", 1)[1].strip()
                fits.PrimaryHDU(data=np.zeros((3, 4, 4), dtype="float32")).writeto(
                    os.path.join(cwd, name + ".fit"), overwrite=True
                )
        """,
    )
    ws = Workspace(str(tmp_path / "out"))
    input_ref = _ref("denoised")
    input_path = ws.path_for(input_ref, ".fit")
    fits.PrimaryHDU(data=None).writeto(input_path)
    output_ref = _ref("linear_master")
    ctx = B.ExecContext(workspace=ws, tools=_FakeTools(siril=fake), resolve={input_ref: input_path})
    op = ColorCalibrate(input=input_ref, output=output_ref, pcc=False)

    out = SirilAdapter().execute(op, ctx)

    assert os.path.exists(out[output_ref])


def test_exiftool_adapter_invokes_tool_on_each_target(tmp_path):
    from seestar.workspace import Workspace

    fake = _write_fake_tool(
        tmp_path,
        "fake_exiftool",
        """
        import sys

        target = sys.argv[-1]
        with open(target + ".exif_called", "w", encoding="utf-8") as f:
            f.write(" ".join(sys.argv[1:]))
        """,
    )
    ws = Workspace(str(tmp_path / "out"))
    target_ref = _ref("M8_final.jpg", ArtifactRole.DELIVERABLE)
    target_path = ws.path_for(target_ref, "")
    with open(target_path, "w") as f:
        f.write("not a real jpg, exiftool is faked")
    card = MetadataCard(
        title="X1",
        object_name="X1",
        description="nebula; 51x10.0s = 8.5 min; LP filter",
        one_liner="X1. 51x10.0s = 8.5 min, LP filter, 2026-06-15. ZWO Seestar S30.",
        user_comment="crop->BGE(0.5,Subtraction)->denoise(0.5); profile=showcase; variant=final",
        keywords=("X1", "nebula", "LP"),
        datetime_original=None,
        artist=None,
        ra_deg=None,
        dec_deg=None,
        pixscale_arcsec=None,
    )
    op = EmbedMetadata(targets=(target_ref,), card=card, variant="final")
    ctx = B.ExecContext(workspace=ws, tools=_FakeTools(exiftool=fake), resolve={target_ref: target_path})

    ExiftoolAdapter().execute(op, ctx)

    marker = target_path + ".exif_called"
    assert os.path.exists(marker)
    recorded = open(marker, encoding="utf-8").read()
    assert "-EXIF:UserComment=" in recorded and target_path in recorded


# ---------------------------------------------------------------------------
# H1 Task 4: every processing-tool adapter is bounded through run_external's
# deadline. Each test below uses a REAL fake executable that sleeps past a
# very small per-tool timeout (seconds, never production minutes) and asserts
# (a) ExternalToolTimeout names the right tool, and (b) no expected output was
# registered/published -- the adapter must raise before returning a result.
# ---------------------------------------------------------------------------


def test_siril_adapter_times_out_and_publishes_no_output(tmp_path):
    from astropy.io import fits

    from seestar.workspace import Workspace

    fake = _write_sleeper_tool(tmp_path, "slow_siril")
    ws = Workspace(str(tmp_path / "out"))
    input_ref = _ref("denoised")
    input_path = ws.path_for(input_ref, ".fit")
    fits.PrimaryHDU(data=None).writeto(input_path)
    output_ref = _ref("linear_master")
    tools = _FakeTools(siril=fake, timeouts=B.ToolTimeouts(siril=0.2))
    ctx = B.ExecContext(workspace=ws, tools=tools, resolve={input_ref: input_path})
    op = ColorCalibrate(input=input_ref, output=output_ref, pcc=False)

    with pytest.raises(ExternalToolTimeout, match="Siril"):
        SirilAdapter().execute(op, ctx)

    assert not os.path.exists(ws.path_for(output_ref, ".fit"))


def test_graxpert_adapter_times_out_and_publishes_no_output(tmp_path):
    from astropy.io import fits

    from seestar.workspace import Workspace

    fake = _write_sleeper_tool(tmp_path, "slow_graxpert")
    ws = Workspace(str(tmp_path / "out"))
    input_ref = _ref("linear_master")
    input_path = ws.path_for(input_ref, ".fit")
    fits.PrimaryHDU(data=None).writeto(input_path)
    output_ref = _ref("bge")
    expected_out = ws.path_for(output_ref, ".fits")
    tools = _FakeTools(graxpert=fake, timeouts=B.ToolTimeouts(graxpert=0.2))
    ctx = B.ExecContext(workspace=ws, tools=tools, resolve={input_ref: input_path})
    op = BackgroundExtract(input=input_ref, output=output_ref, smoothing=0.5, correction="Subtraction")

    with pytest.raises(ExternalToolTimeout, match="GraXpert"):
        GraxpertAdapter().execute(op, ctx)

    assert not os.path.exists(expected_out)


def test_starnet_adapter_times_out_and_publishes_no_output(tmp_path):
    import numpy as np
    from PIL import Image

    from seestar.workspace import Workspace

    fake = _write_sleeper_tool(tmp_path, "slow_starnet2")
    ws = Workspace(str(tmp_path / "out"))
    input_ref = _ref("stretched")
    input_path = ws.path_for(input_ref, ".tif")
    Image.fromarray(np.zeros((4, 4, 3), dtype="uint8")).save(input_path)
    starless_ref, stars_ref = _ref("starless"), _ref("stars")
    tools = _FakeTools(starnet2=fake, timeouts=B.ToolTimeouts(starnet2=0.2))
    ctx = B.ExecContext(workspace=ws, tools=tools, resolve={input_ref: input_path})
    op = SeparateStars(input=input_ref, starless=starless_ref, stars=stars_ref)

    with pytest.raises(ExternalToolTimeout, match="StarNet2"):
        StarNetAdapter().execute(op, ctx)

    assert not os.path.exists(ws.path_for(starless_ref, ".tif"))
    assert not os.path.exists(ws.path_for(stars_ref, ".tif"))


def test_starnet_module_helper_times_out(tmp_path):
    """The module-level `starnet()` helper (legacy direct callers, not the
    registry adapter) is bounded the same way -- both select "starnet2"."""
    from seestar.adapters.starnet import starnet

    fake = _write_sleeper_tool(tmp_path, "slow_starnet2_direct")
    tools = _FakeTools(starnet2=fake, timeouts=B.ToolTimeouts(starnet2=0.2))

    with pytest.raises(ExternalToolTimeout, match="StarNet2"):
        starnet(str(tmp_path / "in.tif"), str(tmp_path), "base", tools)

    assert not os.path.exists(os.path.join(str(tmp_path), "base_starless.tif"))
    assert not os.path.exists(os.path.join(str(tmp_path), "base_stars.tif"))


def test_exiftool_adapter_times_out_and_publishes_no_output(tmp_path):
    from seestar.workspace import Workspace

    fake = _write_sleeper_tool(tmp_path, "slow_exiftool")
    ws = Workspace(str(tmp_path / "out"))
    target_ref = _ref("M8_final.jpg", ArtifactRole.DELIVERABLE)
    target_path = ws.path_for(target_ref, "")
    with open(target_path, "w") as f:
        f.write("not a real jpg, exiftool is faked")
    card = MetadataCard(
        title="X1",
        object_name="X1",
        description="nebula; 51x10.0s = 8.5 min; LP filter",
        one_liner="X1. 51x10.0s = 8.5 min, LP filter, 2026-06-15. ZWO Seestar S30.",
        user_comment="crop->BGE(0.5,Subtraction)->denoise(0.5); profile=showcase; variant=final",
        keywords=("X1", "nebula", "LP"),
        datetime_original=None,
        artist=None,
        ra_deg=None,
        dec_deg=None,
        pixscale_arcsec=None,
    )
    op = EmbedMetadata(targets=(target_ref,), card=card, variant="final")
    tools = _FakeTools(exiftool=fake, timeouts=B.ToolTimeouts(exiftool=0.2))
    ctx = B.ExecContext(workspace=ws, tools=tools, resolve={target_ref: target_path})

    with pytest.raises(ExternalToolTimeout, match="exiftool"):
        ExiftoolAdapter().execute(op, ctx)

    assert not os.path.exists(target_path + ".exif_called")
