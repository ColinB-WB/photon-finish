"""External tool path resolution: env -> config -> auto-detect -> platform defaults -> fail fast.

Resolution order for each tool (siril, graxpert, starnet2, exiftool):
  1. Environment variable: SEESTAR_SIRIL / SEESTAR_GRAXPERT / SEESTAR_STARNET2 / SEESTAR_EXIFTOOL
  2. Non-empty value in config.yaml tools.*
  3. shutil.which() with multiple candidate names per tool
  4. Known platform default install paths (existence check -- for app bundles / non-PATH installs)
  5. Raise ToolNotFoundError listing every path that was tried

Entry points call resolve_tools(cfg) once at startup and thread the resulting ToolPaths
through the pipeline. doctor(cfg) returns a human-readable resolution table (seestar doctor).
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .adapters.base import ToolPaths


class ToolNotFoundError(RuntimeError):
    """One or more required external tools could not be located."""


# Step 1 -- environment variable names, one per tool.
_ENV: dict[str, str] = {
    "siril": "SEESTAR_SIRIL",
    "graxpert": "SEESTAR_GRAXPERT",
    "starnet2": "SEESTAR_STARNET2",
    "exiftool": "SEESTAR_EXIFTOOL",
}

# Step 3 -- shutil.which() candidate names per tool.
# On Windows, which() appends PATHEXT (.exe, .cmd, …) automatically, so bare names
# already find .exe binaries -- no need to list foo.exe alongside foo.
_WHICH: dict[str, list[str]] = {
    "siril": ["siril-cli", "siril"],
    "graxpert": ["GraXpert", "graxpert"],
    "starnet2": ["starnet2", "StarNet2"],
    "exiftool": ["exiftool"],
}


def _platform_defaults(tool: str) -> list[str]:
    """Step 4 -- known install paths for the current platform, checked by file existence.

    macOS app bundles don't add themselves to PATH, so we probe them directly.
    Windows paths cover the most common installer locations.
    """
    if sys.platform == "darwin":
        p: dict[str, list[str]] = {
            "siril": ["/Applications/Siril.app/Contents/MacOS/siril-cli"],
            "graxpert": ["/Applications/GraXpert.app/Contents/MacOS/GraXpert"],
            "starnet2": ["/usr/local/bin/starnet2"],
            "exiftool": ["/opt/homebrew/bin/exiftool", "/usr/local/bin/exiftool"],
        }
    elif sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA", r"C:\Users\Default\AppData\Local")
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        p = {
            "siril": [
                os.path.join(pf, "Siril", "siril-cli.exe"),
                os.path.join(pf, "Siril", "bin", "siril-cli.exe"),
            ],
            "graxpert": [
                os.path.join(pf, "GraXpert", "GraXpert.exe"),
                os.path.join(local, "Programs", "GraXpert", "GraXpert.exe"),
            ],
            "starnet2": [
                os.path.join(pf, "StarNet2", "bin", "starnet2.exe"),
                os.path.join(pf, "StarNet2", "starnet2.exe"),
                os.path.join(local, "Programs", "StarNet2", "bin", "starnet2.exe"),
                os.path.join(local, "Programs", "StarNet2", "starnet2.exe"),
            ],
            "exiftool": [
                r"C:\Windows\exiftool.exe",
                r"C:\tools\exiftool.exe",
                os.path.join(local, "Programs", "exiftool", "exiftool.exe"),
            ],
        }
    else:
        p = {
            "siril": ["/usr/bin/siril-cli", "/usr/local/bin/siril-cli"],
            "graxpert": ["/usr/local/bin/GraXpert", "/usr/local/bin/graxpert"],
            "starnet2": ["/usr/local/bin/starnet2"],
            "exiftool": ["/usr/bin/exiftool", "/usr/local/bin/exiftool"],
        }
    return p.get(tool, [])


def _resolve_one(tool: str, cfg: dict) -> tuple[str | None, str, list[str]]:
    """Run the 5-step chain for a single tool.

    Returns (resolved_path | None, source_label, tried_list).
    """
    tried: list[str] = []

    # Step 1: environment variable
    env_var = _ENV[tool]
    env_val = os.environ.get(env_var, "").strip()
    if env_val:
        tried.append(f"env {env_var} = {env_val!r}  <- used")
        return env_val, "env", tried
    tried.append(f"env {env_var}: not set")

    # Step 2: config.yaml tools.<tool>
    cfg_val = ((cfg.get("tools") or {}).get(tool) or "").strip()
    if cfg_val:
        tried.append(f"config tools.{tool} = {cfg_val!r}  <- used")
        return cfg_val, "config", tried
    tried.append(f"config tools.{tool}: not set")

    # Step 3: shutil.which() with candidate names
    for name in _WHICH.get(tool, []):
        found = shutil.which(name)
        if found:
            tried.append(f"PATH {name!r} -> {found}  <- used")
            return found, "auto-detect", tried
        tried.append(f"PATH {name!r}: not found")

    # Step 4: known platform default paths (file existence)
    for path in _platform_defaults(tool):
        if os.path.isfile(path):
            tried.append(f"platform default {path!r}  <- used")
            return path, "platform-default", tried
        tried.append(f"platform default {path!r}: not found")

    return None, "not-found", tried


def parse_timeouts(cfg: dict):
    """Parse `tools.timeout_s.<tool>` from `cfg` into a `ToolTimeouts`.

    The ONE validation/conversion helper for timeout config, shared by
    `resolve_tools` and `doctor` -- there is no second in-code defaults mapping
    here: a present key is passed straight through to `ToolTimeouts`, and a
    missing key falls back to `ToolTimeouts()`'s own default for that field, so
    the code defaults live in exactly one place (`seestar.adapters.base.ToolTimeouts`).
    An invalid present value raises `ValueError` from `ToolTimeouts.__post_init__`,
    naming the offending `tools.timeout_s.<tool>` key."""
    from .adapters.base import ToolTimeouts

    _TOOLS = ("siril", "graxpert", "starnet2", "exiftool")
    raw = (cfg.get("tools") or {}).get("timeout_s") or {}
    defaults = ToolTimeouts()
    kwargs = {t: (raw[t] if t in raw else getattr(defaults, t)) for t in _TOOLS}
    return ToolTimeouts(**kwargs)


def resolve_tools(cfg: dict) -> ToolPaths:
    """Resolve all tool paths using the 5-step chain, plus validated timeouts.

    Call once at startup and pass the resulting ToolPaths through the pipeline.
    Raises ToolNotFoundError listing every missing tool and every path tried,
    or ValueError if a configured `tools.timeout_s.<tool>` value is invalid.
    """
    from .adapters.base import ToolPaths

    _TOOLS = ("siril", "graxpert", "starnet2", "exiftool")
    timeouts = parse_timeouts(cfg)
    results: dict[str, tuple[str | None, str, list[str]]] = {t: _resolve_one(t, cfg) for t in _TOOLS}

    missing = [t for t in _TOOLS if not results[t][0]]
    if missing:
        parts = [f"Seestar: {len(missing)} required tool(s) not found.\n"]
        for t in missing:
            _, _, tried = results[t]
            parts.append(f"  {t}:")
            for step in tried:
                parts.append(f"    {step}")
            parts.append(
                f"    -> Install {t} and add it to your PATH, "
                f"or set tools.{t} in config.yaml, "
                f"or set the {_ENV[t]} environment variable.\n"
            )
        raise ToolNotFoundError("\n".join(parts))

    return ToolPaths(**{t: results[t][0] for t in _TOOLS}, timeouts=timeouts)


def doctor(cfg: dict) -> str:
    """Return a human-readable tool-resolution report (used by `seestar doctor`)."""
    _TOOLS = ("siril", "graxpert", "starnet2", "exiftool")
    timeouts = parse_timeouts(cfg)
    rows: list[tuple[str, str, str, str]] = []
    detail_blocks: list[str] = []

    for tool in _TOOLS:
        path, source, tried = _resolve_one(tool, cfg)
        status = "[ok]" if path else "[MISSING]"
        rows.append((tool, source if path else "not-found", path or "", status))
        if not path:
            lines = [f"\n  {tool} -- searched:"]
            for s in tried:
                lines.append(f"    {s}")
            lines.append(
                f"    -> Install {tool} and add it to your PATH, "
                f"or set tools.{tool} in config.yaml, "
                f"or set {_ENV[tool]}."
            )
            detail_blocks.append("\n".join(lines))

    out = ["Tool path resolution:\n", f"  {'Tool':<12} {'Source':<18} Path"]
    out.append(f"  {'-' * 12} {'-' * 18} {'-' * 55}")
    for tool, source, path, status in rows:
        out.append(f"  {tool:<12} {source:<18} {path or '(not found)'}  {status}")

    if detail_blocks:
        out.append("\nMissing tool details:")
        out.extend(detail_blocks)
        out.append("\n[MISSING]  One or more tools are missing. Processing will fail.")
    else:
        out.append("\n[ok]  All tools found.")

    out.append("\nEffective timeouts (tools.timeout_s, seconds):")
    for tool in _TOOLS:
        out.append(f"  {tool:<12} {timeouts.for_tool(tool):g}")

    return "\n".join(out)
