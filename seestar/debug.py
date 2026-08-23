"""Debug mode (CLAUDE.md §9a): save a viewable JPG after each stage.

Linear stages (pre-stretch) get a preview-only autostretch applied to the snapshot
ONLY (never the pipeline data), identical across them so they're comparable.
Stretched stages are saved as-is. Also appends per-stage `stat` to debug_log.txt.
"""

from __future__ import annotations

import os

from .adapters.base import ToolPaths
from .adapters.siril import run_siril


class Debugger:
    def __init__(self, debug_dir: str, tools: ToolPaths, enabled: bool = True):
        self.dir = debug_dir
        self.tools = tools
        self.enabled = enabled
        self.n = 0
        if enabled:
            os.makedirs(debug_dir, exist_ok=True)
            self.log = os.path.join(debug_dir, "debug_log.txt")
            open(self.log, "w").close()

    def snap(self, fits_path: str, name: str, linear: bool = True, note: str = "") -> None:
        if not self.enabled:
            return
        idx = f"{self.n:02d}"
        out = f"{idx}_{name}"
        lines = [f'cd "{self.dir}"', f'load "{fits_path}"']
        if linear:
            lines.append("autostretch")  # preview only
        lines += [f"savejpg {out} 88", f'load "{fits_path}"', "stat"]
        try:
            log = run_siril(lines, self.tools)
        except Exception as e:
            # Intentional broad catch (incl. H1's ExternalToolTimeout) — debug
            # snapshots are best-effort by design (CHANGE_SAFETY rule 5: truly
            # incidental cleanup/telemetry); a slow/hung snapshot must never fail
            # or delay the production render, only record its own failure.
            log = f"(snapshot failed: {e})"
        with open(self.log, "a") as f:
            f.write(f"\n=== {out} ({'linear' if linear else 'stretched'}) {note} ===\n")
            for ln in log.splitlines():
                if "layer:" in ln.lower() or "Mean:" in ln:
                    f.write(ln.strip() + "\n")
        self.n += 1
