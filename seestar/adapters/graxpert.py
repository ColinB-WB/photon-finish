"""GraXpert adapter: AI background extraction and denoise.

GraXpert resolves `-output` relative to the input file's directory and appends
`.fits` itself. Pass an absolute input path and a bare output basename; passing a
full output path produces doubled-path failures.
"""

from __future__ import annotations

import os
from pathlib import Path

from seestar.core.types import BackgroundExtract, Denoise

from ._external_process import ExternalToolFailure, run_external, summarize_tool_output, write_diagnostic_log
from .base import AdapterError, ExecContext, ToolPaths, register

_GRAXPERT_GUIDANCE = "If this was GraXpert's first run, open GraXpert once to finish its model setup, then try again."


def _graxpert(cmd: str, input_abs: str, out_base: str, extra: list[str], tools: ToolPaths) -> str:
    """Bounded by `tools.timeouts.graxpert` (H1 Task 4) -- a hung GraXpert CLI call
    (e.g. first-run model download) can no longer hang the whole pipeline run;
    `ExternalToolTimeout` propagates to the adapter's caller."""
    in_dir = os.path.dirname(os.path.abspath(input_abs))
    r = run_external(
        [tools.graxpert, "-cli", "-cmd", cmd, os.path.abspath(input_abs), "-output", out_base, "-gpu", "true", *extra],
        tool="graxpert",
        tools=tools,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out_path = os.path.join(in_dir, out_base + ".fits")
    if not os.path.exists(out_path):
        # Diagnosis 7: bounded, actionable summary in the message; full stdout +
        # stderr in the run's diagnostic log. GraXpert's most common real failure
        # is an unfinished first-run model download, which prints at the END of a
        # long progress stream -- exactly what the old truncation discarded.
        text = f"{r.stdout or ''}\n{r.stderr or ''}"
        log_path, log_warning = write_diagnostic_log(
            getattr(tools, "diagnostics_dir", None), tool="graxpert", stage=cmd, text=text
        )
        raise ExternalToolFailure(
            tool="graxpert",
            failure_class="no_output",
            summary=summarize_tool_output(text),
            exit_status=r.returncode,
            log_path=log_path,
            guidance=(log_warning or _GRAXPERT_GUIDANCE),
        )
    return out_path


def bge(
    input_fit: str,
    tools: ToolPaths,
    smoothing: float = 0.5,
    correction: str = "Subtraction",
    out_base: str | None = None,
) -> str:
    out_base = out_base or (Path(input_fit).stem + "_bge")
    return _graxpert(
        "background-extraction", input_fit, out_base, ["-smoothing", str(smoothing), "-correction", correction], tools
    )


def denoise(input_fit: str, tools: ToolPaths, strength: float = 0.5, out_base: str | None = None) -> str:
    out_base = out_base or (Path(input_fit).stem + "_dn")
    return _graxpert("denoising", input_fit, out_base, ["-strength", str(strength)], tools)


class GraxpertAdapter:
    """Executes BackgroundExtract and Denoise by shelling to GraXpert -cli."""

    operations = (BackgroundExtract, Denoise)
    name = "graxpert"

    def execute(self, op, ctx: ExecContext) -> dict:
        input_path = ctx.resolve[op.input]
        # GraXpert's own gotcha (above): ask the workspace for a .fits path, then
        # pass GraXpert only the bare basename -- it writes beside the input, which
        # lives in the same (scratch) directory as this artifact's slot.
        out_path = ctx.workspace.path_for(op.output, ".fits")
        out_base = os.path.splitext(os.path.basename(out_path))[0]
        if isinstance(op, BackgroundExtract):
            produced = _graxpert(
                "background-extraction",
                input_path,
                out_base,
                ["-smoothing", str(op.smoothing), "-correction", op.correction],
                ctx.tools,
            )
        elif isinstance(op, Denoise):
            produced = _graxpert("denoising", input_path, out_base, ["-strength", str(op.strength)], ctx.tools)
        else:
            raise AdapterError(f"GraxpertAdapter cannot execute {type(op).__name__}")
        # Register the REAL path GraXpert produced (matches out_path when input and
        # output share a directory, which the workspace guarantees for scratch roles).
        return {op.output: produced}


register(GraxpertAdapter())
