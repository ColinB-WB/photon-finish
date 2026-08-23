"""Siril adapter: crop/orientation, color calibration, stretch/structure render,
star-reduced blend, closeup crop, multi-format export, and session combination.

Tool gotchas baked in:
  - Siril `load` breaks on spaces -> callers always work on space-free copies.
  - Siril CWD defaults to ~/Pictures -> every script `cd`s to an absolute path.
  - `pcc` puts the image into a color-managed state (ICC profile) -> every later
    `savejpg` sRGB-gamma-encodes the output unless `icc_remove` runs right after
    (the M81 gray-wash root cause) -- see ColorCalibrate / _color_calibrate.

SirilAdapter's execute() branches (Crop, ColorCalibrate, StretchRender, ScreenBlend,
CropTo, Export, CombineSessions) are the live path. Operations carry path-free
artifact references; `ExecContext` resolves them to real files in the run workspace.
`neutralize_background` and `stretch_chain_lines` remain module-level helpers because
both adapter methods and tests call them directly.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

from seestar.core.types import (
    ColorCalibrate,
    CombineSessions,
    Crop,
    CropTo,
    Export,
    GhsParams,
    PccEvidence,
    ScreenBlend,
    StretchRender,
)

from ._external_process import (
    ExternalToolFailure,
    ExternalToolTimeout,
    run_external,
    summarize_tool_output,
    write_diagnostic_log,
)
from .base import AdapterError, ExecContext, ToolPaths, register


# --- low-level runner ---------------------------------------------------------
def _c_locale_env() -> dict[str, str]:
    """Siril's console log is gettext-LOCALIZED: under a French locale it prints
    "L'etalonnage des couleurs par photometrie a reussi" instead of "Photometric
    Color Calibration succeeded." (verified against Siril 1.4.4, 2026-08-14).
    Every string this module matches on -- the PCC evidence signatures AND
    `run_siril`'s own "Script execution failed" marker -- would silently stop
    matching, turning a healthy calibration into a permanent native fallback and,
    worse, a FAILED script into an apparent success. Pin the child to the C
    locale so the machine-readable contract is the same in every environment
    (Tenet 7: user-visible quality and safety consistent across environments).

    `LC_ALL=C` alone already wins over a hostile `LANGUAGE=fr` (verified), but
    LANG/LANGUAGE are pinned too so the behavior does not depend on which gettext
    build resolves the precedence. Only the message catalog is affected -- this
    does not change Siril's numeric output (`LC_NUMERIC` under C is the '.'
    decimal point the parsers already expect)."""
    return {**os.environ, "LC_ALL": "C", "LANG": "C", "LANGUAGE": "C"}


def run_siril(script_lines: list[str], tools: ToolPaths) -> str:
    """Run a Siril command script; return combined stdout/stderr. Bounded by
    `tools.timeouts.siril` (H1 Task 4) -- a hung Siril call can no longer hang the
    whole pipeline run; `ExternalToolTimeout` propagates to every caller. The
    child runs under a pinned C locale (`_c_locale_env`) so every string this
    module parses stays in the one language it was written against."""
    body = "requires 1.0.0\n" + "\n".join(script_lines) + "\n"
    with tempfile.NamedTemporaryFile("w", suffix=".ssf", delete=False) as f:
        f.write(body)
        sp = f.name
    try:
        # explicit encoding: Windows' default cp1252 raises on Siril's UTF-8 output
        # (e.g. curly quotes), killing the reader thread -> stdout is None (M17 case)
        r = run_external(
            [tools.siril, "-s", sp],
            tool="siril",
            tools=tools,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_c_locale_env(),
        )
        out = (r.stdout or "") + (r.stderr or "")
        if "Script execution failed" in out:
            # Diagnosis 7: the whole (unbounded) output used to become the
            # exception message and then be cut to its first 200 characters --
            # Siril's banner. Keep the ACTIONABLE tail in the message and put the
            # full text in the run's bounded diagnostic log. A log that cannot be
            # written is reported alongside, never instead of, this failure.
            log_path, log_warning = write_diagnostic_log(
                getattr(tools, "diagnostics_dir", None), tool="siril", stage=None, text=out
            )
            raise ExternalToolFailure(
                tool="siril",
                failure_class="script_failed",
                summary=summarize_tool_output(out),
                exit_status=r.returncode,
                log_path=log_path,
                guidance=log_warning,
                full_output=out,
            )
        return out
    finally:
        os.unlink(sp)


# --- Stage 2: color -> linear calibrated --------------------------------------

# Siril 1.4.4 message signatures (extracted verbatim from the installed binary,
# 2026-08-14; a captured real run on the S50 M82 stack confirms the success
# shape). The parser REPORTS evidence; acceptability is decided by
# `core.policy.evaluate_pcc_evidence` only (Diagnosis 4 item 5).
_PCC_SUCCEEDED = "Photometric Color Calibration succeeded."
_PCC_IMPRECISE = "found an imprecise solution"
_PCC_FEW_STARS = "did not rely on many stars"
_PCC_STARS_RE = re.compile(r"Found a solution for color calibration using (\d+) stars")
# the renormalized/applied factors: bare "K<n>: <float>" lines (the first factor
# block carries a "(deviation: ...)" suffix and deliberately does not match)
_PCC_COEFF_RE = re.compile(r"K([012]):\s*([0-9]+(?:\.[0-9]+)?)\s*$", re.MULTILINE)


def parse_pcc_evidence(text: str) -> PccEvidence:
    """Structured PCC evidence from a completed Siril script's combined output."""
    stars = None
    m = _PCC_STARS_RE.search(text)
    if m:
        stars = int(m.group(1))
    coeffs: tuple[float, float, float] | None = None
    found = {int(k): float(v) for k, v in _PCC_COEFF_RE.findall(text)}
    if set(found) == {0, 1, 2}:
        coeffs = (found[0], found[1], found[2])
    detail_lines = [
        ln.strip()
        for ln in text.splitlines()
        if _PCC_STARS_RE.search(ln) or _PCC_IMPRECISE in ln or _PCC_FEW_STARS in ln
    ]
    return PccEvidence(
        succeeded=_PCC_SUCCEEDED in text,
        imprecise=_PCC_IMPRECISE in text,
        few_stars_warning=_PCC_FEW_STARS in text,
        stars_used=stars,
        coefficients=coeffs,
        detail="; ".join(detail_lines)[:300],
    )


def classify_pcc_failure(text: str) -> str:
    """Deterministic class for a FAILED pcc command, from Siril's own wording:
    'no_wcs' | 'no_stars' | 'catalog' (covers the online catalog fetch, so also
    network loss) | 'tool' (anything unrecognized)."""
    low = text.lower()
    if "no wcs data" in low:
        return "no_wcs"
    if "not enough stars" in low:  # the IMAGE lacks usable stars
        return "no_stars"
    if "catalog" in low or "catalogue" in low:  # catalog fetch/content problems (incl. network)
        return "catalog"
    return "tool"


def neutralize_background(fit_path: str) -> dict:
    """Light background neutralization (CLAUDE.md Stage 2): per-channel OFFSET so every
    channel's sky median equals their mean. PCC calibrates channel gains to catalog STAR
    colors and says nothing about the sky -- post-PCC the channel backgrounds diverge, and
    a scalar black point (0.987 x mean median) then sits below the brightest channel's sky,
    which renders as a tinted gray wash (the M81 sky-40 regression). Offsets only: star/
    body color from PCC is untouched. Returns the per-channel medians before/after."""
    import numpy as np
    from astropy.io import fits
    from astropy.stats import sigma_clipped_stats

    with fits.open(fit_path, mode="update") as hdul:
        data = hdul[0].data
        if data.ndim != 3:  # mono: nothing to neutralize
            return {"medians_before": None, "medians_after": None}
        if not np.issubdtype(data.dtype, np.floating):  # integer data would wrap on subtract
            data = data.astype(np.float32)
            hdul[0].data = data
        meds = [float(sigma_clipped_stats(data[i], sigma=3.0, maxiters=5)[1]) for i in range(data.shape[0])]
        mean_med = float(np.mean(meds))
        for i in range(data.shape[0]):
            data[i] -= meds[i] - mean_med
        hdul.flush()
    return {"medians_before": [round(m, 6) for m in meds], "medians_after": round(mean_med, 6)}


# --- Stage 3-4 + export: render from linear calibrated ------------------------
def _deliverable(workdir: str, out_base: str) -> dict:
    return {
        "tif": os.path.join(workdir, out_base + ".tif"),
        "png": os.path.join(workdir, out_base + ".png"),
        "jpg": os.path.join(workdir, out_base + ".jpg"),
    }


def stretch_chain_lines(
    stem: str,
    asinh_d: float,
    blackpoint: float,
    mtf_mid: float,
    saturation: float,
    ght: dict | None = None,
    save_pre_ght: str | None = None,
) -> list[str]:
    """The ONE stretch transform chain: asinh -> mtf -> (GHS) -> satu -> (unsharp).
    Both the mtf solver's probe and the final render build their Siril script from
    here, so the solver always measures the SAME chain the final render ships. This
    is the Option-A contract fix: before it, the GHS ran outside the solve and blew
    ~40% past the solved body_p90 target while crushing the background (M8 evidence:
    p90 75->123, near-black sky 0.01%->4.2%, 1475->1054 stars)."""
    lines = [f'load "{stem}"', f"asinh {asinh_d} {blackpoint}", f"mtf 0 {mtf_mid} 1"]
    if ght and save_pre_ght:
        lines.append(f"save {save_pre_ght}")
    if ght:
        lines.append(
            f"ght -D={ght['ght_d']} -B={ght['ght_b']} -SP={ght['ght_sp']} -LP={ght['ght_lp']} -HP={ght['ght_hp']}"
        )
    lines.append(f"satu {saturation} 0")
    if ght and ght.get("unsharp_amount", 0) > 0:
        lines.append(f"unsharp {ght['unsharp_sigma']} {ght['unsharp_amount']}")
    return lines


def render(
    linear_fit: str,
    workdir: str,
    out_base: str,
    asinh_d: float,
    blackpoint: float,
    mtf_mid: float,
    saturation: float,
    tools: ToolPaths,
    jpg_quality: int = 95,
    ght: dict | None = None,
    black_lift: float = 0.0,
    debug_pre_ght: str | None = None,
) -> dict:
    """Bright-but-controlled stretch: black point at the sky level (clean dark
    background) + asinh + a low midtone lift that brings the subject up BRIGHT so it
    fills the frame at full resolution. Export 16-bit TIFF (master) + PNG (lossless
    showcase) + quality-95 JPG. Locked from the M8 BC2 result.

    `ght` (galaxies, Phase 5) inserts a Generalized Hyperbolic Transform between the
    base stretch and saturation to reveal the dust lane/inner structure -- contrast
    expanded around the body (SP) with the sky (LP) and core (HP) protected -- plus an
    optional restrained unsharp.

    `debug_pre_ght` (stem, no extension): when set and ght is active, saves the
    post-asinh+mtf state as a FITS before GHS runs so the caller can snap it.

    The stretch itself comes from stretch_chain_lines -- the SAME chain solve_mtf
    probes, so the solved mtf is honored end-to-end (Option-A contract)."""
    lines = [f'cd "{workdir}"'] + stretch_chain_lines(
        Path(linear_fit).stem, asinh_d, blackpoint, mtf_mid, saturation, ght=ght, save_pre_ght=debug_pre_ght
    )
    if black_lift and black_lift > 0:
        # Paper black-point lift (print profile): out = bl + (1-bl)*in -> the darkest tone
        # maps to paper's darkest printable black so shadow detail survives reflective media.
        tmp = f"{out_base}_pretmp"
        lines += [f"save {tmp}", f'pm "${tmp}$ * {round(1 - black_lift, 4)} + {round(black_lift, 4)}"']
    lines += [f"savetif {out_base}", f"savepng {out_base}", f"savejpg {out_base} {jpg_quality}"]
    run_siril(lines, tools)
    return _deliverable(workdir, out_base)


# --- GhsParams -> the dict shape stretch_chain_lines wants --------------------
def _ght_dict(g: GhsParams) -> dict:
    d = {"ght_d": g.d, "ght_b": g.b, "ght_sp": g.sp, "ght_lp": g.lp, "ght_hp": g.hp}
    if g.unsharp_amount:
        d["unsharp_sigma"] = g.unsharp_sigma
        d["unsharp_amount"] = g.unsharp_amount
    return d


class SirilAdapter:
    """Executes Crop, ColorCalibrate, StretchRender, ScreenBlend, CropTo, Export, and
    CombineSessions using path-free operations resolved through ExecContext."""

    operations = (Crop, ColorCalibrate, StretchRender, ScreenBlend, CropTo, Export, CombineSessions)
    name = "siril"

    def execute(self, op, ctx: ExecContext) -> dict:
        if isinstance(op, Crop):
            return self._crop(op, ctx)
        if isinstance(op, ColorCalibrate):
            return self._color_calibrate(op, ctx)
        if isinstance(op, StretchRender):
            return self._stretch_render(op, ctx)
        if isinstance(op, ScreenBlend):
            return self._screen_blend(op, ctx)
        if isinstance(op, CropTo):
            return self._crop_to(op, ctx)
        if isinstance(op, Export):
            return self._export(op, ctx)
        if isinstance(op, CombineSessions):
            return self._combine_sessions(op, ctx)
        raise AdapterError(f"SirilAdapter cannot execute {type(op).__name__}")

    # -- Crop: measured per-edge border crop (rectangle in RAW array coords) -------
    def _crop(self, op: Crop, ctx: ExecContext) -> dict:
        input_path = ctx.resolve[op.input]
        out_path = ctx.workspace.path_for(op.output, ".fit")
        workdir = os.path.dirname(out_path)
        stem = Path(out_path).stem
        lines = [f'cd "{workdir}"', f'load "{Path(input_path).stem}"']
        # Coordinate convention, empirically pinned on Siril 1.4.4 (2026-08-13):
        # `crop x y w h` counts x from the file's column-0 edge but y from the
        # file's LAST-row edge. The op's rectangle is in raw array coords (y from
        # row 0), so after `mirrorx` (which flips rows) y passes through
        # unchanged; without mirrorx it must be converted via the frame height.
        if op.mirror_x:  # Seestar FITS omit ROWORDER; one mirrorx normalizes exports.
            lines.append("mirrorx")
            y = op.y
        else:
            from astropy.io import fits

            naxis2 = int(fits.getheader(input_path)["NAXIS2"])
            y = naxis2 - op.y - op.height
        lines.append(f"crop {op.x} {y} {op.width} {op.height}")
        lines.append(f"save {stem}")
        run_siril(lines, ctx.tools)
        return {op.output: out_path}

    # -- ColorCalibrate: optional pcc -> icc_remove -> neutralize -------------------
    def _color_calibrate(self, op: ColorCalibrate, ctx: ExecContext) -> dict:
        input_path = ctx.resolve[op.input]
        out_path = ctx.workspace.path_for(op.output, ".fit")
        workdir = os.path.dirname(out_path)
        stem = Path(out_path).stem
        lines = [f'cd "{workdir}"', f'load "{Path(input_path).stem}"']
        if op.pcc:
            lines += ["pcc", "icc_remove"]  # M81 gray-wash fix -- see neutralize_background
        lines.append(f"save {stem}")
        if op.pcc:
            # Diagnosis 4: report structured evidence from the tool's own output;
            # the CORE decides acceptability (evaluate_pcc_evidence), never here.
            # A failed pcc command aborts the script -> classify the failure so
            # the planner's recorded native fallback knows why.
            try:
                out_text = run_siril(lines, ctx.tools)
            except ExternalToolTimeout:
                # tier: NOT recoverable here. A hung Siril must not silently buy a
                # second full-length attempt, and its message is already the
                # novice-safe surface the CLI/GUI render verbatim -- propagate
                # unchanged (the planner then refuses, as for any hung tool).
                raise
            except RuntimeError as exc:  # tier: recoverable planning step -- the
                # planner falls back to native color on this named failure; a
                # genuine tool breakage resurfaces when the native attempt fails.
                # Only the failure CLASS travels in the message: this string
                # reaches ColorProvenance.reason and, from there, the EXIF of
                # every exported photo, so raw tool output (which echoes local
                # file paths) must never ride along -- the full text stays in
                # the local diagnostic surfaces.
                # classify from the tool's FULL output when we have it: the
                # message is deliberately a bounded summary, and the phrases this
                # classifier keys on ("not enough stars", "no WCS data") can fall
                # outside it.
                text = getattr(exc, "full_output", None) or str(exc)
                raise AdapterError(f"pcc_unavailable({classify_pcc_failure(text)})") from exc
            ctx.pcc_evidence.append(parse_pcc_evidence(out_text))
            neutralize_background(out_path)
            return {op.output: out_path}
        run_siril(lines, ctx.tools)
        return {op.output: out_path}

    # -- StretchRender: render() minus the savetif/png/jpg (Export's job now) ------
    def _stretch_render(self, op: StretchRender, ctx: ExecContext) -> dict:
        input_path = ctx.resolve[op.input]
        out_path = ctx.workspace.path_for(op.output, ".fit")
        workdir = os.path.dirname(out_path)
        stem_out = Path(out_path).stem
        ght = _ght_dict(op.ght) if op.ght else None
        # When debug mode asks for it and a GHS is active, save the post-asinh+mtf
        # state as a FITS before GHS runs so Debugger can snapshot that stage.
        pre_ght_stem = ctx.resolve.get("__debug_pre_ght__") if ght else None
        lines = [f'cd "{workdir}"'] + stretch_chain_lines(
            Path(input_path).stem,
            op.asinh_d,
            op.blackpoint,
            op.mtf_mid,
            op.saturation,
            ght=ght,
            save_pre_ght=pre_ght_stem,
        )
        if op.black_lift and op.black_lift > 0:
            tmp = f"{stem_out}_pretmp"
            lines += [f"save {tmp}", f'pm "${tmp}$ * {round(1 - op.black_lift, 4)} + {round(op.black_lift, 4)}"']
        lines.append(f"save {stem_out}")
        run_siril(lines, ctx.tools)
        return {op.output: out_path}

    # -- ScreenBlend: star-reduced = screen-blend stars back at strength k ----------
    def _screen_blend(self, op: ScreenBlend, ctx: ExecContext) -> dict:
        starless_path = ctx.resolve[op.starless]
        stars_path = ctx.resolve[op.stars]
        out_path = ctx.workspace.path_for(op.output, ".fit")
        workdir = os.path.dirname(out_path)
        stem_out = Path(out_path).stem
        sl, st = Path(starless_path).stem, Path(stars_path).stem
        lines = [
            f'cd "{workdir}"',
            f'load "{starless_path}"',
            f"save {sl}",
            f'load "{stars_path}"',
            f"save {st}",
            f'pm "1 - (1 - ${sl}$)*(1 - {op.k}*${st}$)"',
            f"save {stem_out}",
        ]
        run_siril(lines, ctx.tools)
        return {op.output: out_path}

    # -- CropTo: crop a rendered image to `op.box` (x, y, w, h) ---------------------
    def _crop_to(self, op: CropTo, ctx: ExecContext) -> dict:
        input_path = ctx.resolve[op.input]
        out_path = ctx.workspace.path_for(op.output, ".fit")
        workdir = os.path.dirname(out_path)
        stem = Path(out_path).stem
        x, y, w, h = op.box
        lines = [f'cd "{workdir}"', f'load "{input_path}"', f"crop {x} {y} {w} {h}", f"save {stem}"]
        run_siril(lines, ctx.tools)
        return {op.output: out_path}

    # -- Export: load the stretched artifact, savetif/savepng/savejpg into OUTPUT --
    def _export(self, op: Export, ctx: ExecContext) -> dict:
        input_path = ctx.resolve[op.input]
        out_paths: dict = {}
        workdir = None
        for ref in op.outputs:
            # ref.name already carries the extension ("<base>.tif") -- path_for gets
            # an empty ext so it isn't doubled (matches deliverable naming).
            p = ctx.workspace.path_for(ref, "")
            out_paths[ref] = p
            if workdir is None:
                workdir = os.path.dirname(p)
        lines = [f'cd "{workdir}"', f'load "{input_path}"']  # absolute quoted path: input is scratch, cwd is output
        fmt_cmd = {"tif": "savetif", "png": "savepng", "jpg": "savejpg"}
        for fmt in op.formats:
            if fmt == "jpg":
                lines.append(f"savejpg {op.output_base} {op.jpg_quality}")
            else:
                lines.append(f"{fmt_cmd[fmt]} {op.output_base}")
        run_siril(lines, ctx.tools)
        return out_paths

    # -- CombineSessions: register + integrate sessions, resolved by index ---------
    def _combine_sessions(self, op: CombineSessions, ctx: ExecContext) -> dict:
        session_paths = ctx.resolve["__session_paths__"]
        raws = [session_paths[i] for i in op.session_indices]
        out_path = ctx.workspace.path_for(op.output, ".fit")
        workdir = os.path.dirname(out_path)
        stem = Path(out_path).stem
        cdir = os.path.join(workdir, "_combine")
        if os.path.exists(cdir):
            shutil.rmtree(cdir)  # rmtree, not os.remove: Siril leaves a `cache` subdir
        os.makedirs(cdir, exist_ok=True)
        for i, f in enumerate(raws, 1):
            shutil.copyfile(f, os.path.join(cdir, f"sess{i}.fit"))
        run_siril(
            [
                f'cd "{cdir}"',
                "convert ses -out=.",
                "register ses_",
                f"stack r_ses_ rej 3 3 -norm=addscale -out={stem}_combined",
            ],
            ctx.tools,
        )
        produced = os.path.join(cdir, f"{stem}_combined.fit")
        if not os.path.exists(produced):
            raise AdapterError(f"session combine produced no output in {cdir}")
        shutil.copyfile(produced, out_path)
        shutil.rmtree(cdir)
        return {op.output: out_path}


register(SirilAdapter())
