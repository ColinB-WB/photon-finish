"""M7 (`docs/assessments/2026-07-30-code-quality-assessment.md`): direct tests for
`seestar/tools.py`'s resolution chain.

These are REGRESSION / CHARACTERIZATION tests, not TDD: the chain already behaves
as documented. They exist because it was the one substantial module with no direct
test at all — every other suite monkeypatches `resolve_tools` (or `_resolve_one`)
away, so the exact code the macOS/Windows CI matrix exists to protect was never
exercised. Each test names the mutation it guards against.

Hermetic by construction: no astronomy tool is installed, invoked, or required.
`shutil.which`, the platform-default table, and `os.path.isfile` are stubbed, and
every `SEESTAR_*` variable is cleared first — otherwise this file would pass or
fail depending on what the developer happens to have on PATH.
"""

from __future__ import annotations

import os

import pytest

from seestar import tools as T

_TOOLS = ("siril", "graxpert", "starnet2", "exiftool")
_EMPTY_CFG: dict = {}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Step 1 of the chain reads real environment variables; a developer with
    SEESTAR_SIRIL exported must not change what these tests measure."""
    for var in T._ENV.values():
        monkeypatch.delenv(var, raising=False)


def _no_path_tools(monkeypatch):
    """Steps 3 and 4 find nothing: PATH is empty of every candidate and no
    platform default exists."""
    monkeypatch.setattr(T.shutil, "which", lambda name: None)
    monkeypatch.setattr(T, "_platform_defaults", lambda tool: [])


# ---------------------------------------------------------------------------
# Step 1 -- environment variable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", _TOOLS)
def test_env_var_wins_over_every_later_step(monkeypatch, tool):
    """Guards against the env tier being dropped or ordered after config: an
    explicitly exported path is the user's override of last resort."""
    monkeypatch.setenv(T._ENV[tool], f"/from/env/{tool}")
    monkeypatch.setattr(T.shutil, "which", lambda name: "/from/path/wrong")
    monkeypatch.setattr(T, "_platform_defaults", lambda t: ["/from/platform/wrong"])
    cfg = {"tools": {tool: "/from/config/wrong"}}

    path, source, tried = T._resolve_one(tool, cfg)

    assert (path, source) == (f"/from/env/{tool}", "env")
    assert any("<- used" in line and "env" in line for line in tried)


def test_blank_env_var_falls_through_to_config(monkeypatch):
    """`SEESTAR_SIRIL="   "` is an unset variable, not a path. Guards against
    dropping the `.strip()` — an empty override would otherwise resolve to "",
    which fails much later with a far worse message."""
    monkeypatch.setenv("SEESTAR_SIRIL", "   ")
    _no_path_tools(monkeypatch)

    path, source, _ = T._resolve_one("siril", {"tools": {"siril": "/from/config/siril"}})

    assert (path, source) == ("/from/config/siril", "config")


# ---------------------------------------------------------------------------
# Step 2 -- config.yaml tools.<tool>
# ---------------------------------------------------------------------------


def test_config_value_wins_over_path_and_platform_default(monkeypatch):
    monkeypatch.setattr(T.shutil, "which", lambda name: "/from/path/wrong")
    monkeypatch.setattr(T, "_platform_defaults", lambda t: ["/from/platform/wrong"])

    path, source, _ = T._resolve_one("graxpert", {"tools": {"graxpert": "/from/config/graxpert"}})

    assert (path, source) == ("/from/config/graxpert", "config")


def test_empty_config_value_falls_through_to_path(monkeypatch):
    """`tools.*` ships EMPTY in config.yaml by default (§5), so the empty string
    MUST mean "not set". Guards against a truthiness change that would resolve
    every tool to ""."""
    monkeypatch.setattr(T.shutil, "which", lambda name: "/from/path/siril" if name == "siril-cli" else None)
    monkeypatch.setattr(T, "_platform_defaults", lambda t: [])

    path, source, _ = T._resolve_one("siril", {"tools": {"siril": "  "}})

    assert (path, source) == ("/from/path/siril", "auto-detect")


# ---------------------------------------------------------------------------
# Step 3 -- shutil.which, in candidate order
# ---------------------------------------------------------------------------


def test_which_tries_candidate_names_in_declared_order(monkeypatch):
    """`siril-cli` before `siril` matters: the plain `siril` binary opens the GUI,
    while the pipeline needs the CLI (§5). Guards against a reordered or
    set-ified candidate list."""
    asked: list[str] = []

    def fake_which(name):
        asked.append(name)
        return "/usr/bin/siril" if name == "siril" else None

    monkeypatch.setattr(T.shutil, "which", fake_which)
    monkeypatch.setattr(T, "_platform_defaults", lambda t: [])

    path, source, _ = T._resolve_one("siril", _EMPTY_CFG)

    assert asked == ["siril-cli", "siril"]
    assert (path, source) == ("/usr/bin/siril", "auto-detect")


def test_which_hit_wins_over_platform_default(monkeypatch):
    monkeypatch.setattr(T.shutil, "which", lambda name: "/from/path/exiftool")
    monkeypatch.setattr(T, "_platform_defaults", lambda t: ["/from/platform/exiftool"])

    path, source, _ = T._resolve_one("exiftool", _EMPTY_CFG)

    assert (path, source) == ("/from/path/exiftool", "auto-detect")


# ---------------------------------------------------------------------------
# Step 4 -- platform defaults, by file existence
# ---------------------------------------------------------------------------


def test_platform_default_is_used_only_when_the_file_exists(monkeypatch):
    """macOS app bundles never join PATH, so this tier is what makes a plain
    drag-to-Applications install work. Guards against probing by name instead of
    by existence: the first candidate must be REJECTED because it isn't there."""
    monkeypatch.setattr(T.shutil, "which", lambda name: None)
    monkeypatch.setattr(T, "_platform_defaults", lambda t: ["/missing/GraXpert", "/present/GraXpert"])
    monkeypatch.setattr(T.os.path, "isfile", lambda p: p == "/present/GraXpert")

    path, source, tried = T._resolve_one("graxpert", _EMPTY_CFG)

    assert (path, source) == ("/present/GraXpert", "platform-default")
    assert any("/missing/GraXpert" in line and "not found" in line for line in tried)


def test_macos_platform_defaults_probe_the_app_bundle_paths(monkeypatch):
    monkeypatch.setattr(T.sys, "platform", "darwin")

    assert T._platform_defaults("siril") == ["/Applications/Siril.app/Contents/MacOS/siril-cli"]
    assert T._platform_defaults("graxpert") == ["/Applications/GraXpert.app/Contents/MacOS/GraXpert"]


def test_windows_platform_defaults_follow_the_real_install_roots(monkeypatch):
    """Windows is the platform this resolution chain is least exercised on (no
    packaged build yet, run-from-source only), and its defaults are built from
    environment variables rather than literals — so they need a test that a
    macOS machine can still run."""
    monkeypatch.setattr(T.sys, "platform", "win32")
    monkeypatch.setenv("PROGRAMFILES", r"P:\Progs")
    monkeypatch.setenv("LOCALAPPDATA", r"L:\Local")

    siril = T._platform_defaults("siril")
    graxpert = T._platform_defaults("graxpert")

    assert siril[0] == os.path.join(r"P:\Progs", "Siril", "siril-cli.exe")
    assert os.path.join(r"L:\Local", "Programs", "GraXpert", "GraXpert.exe") in graxpert


def test_unknown_tool_name_has_no_platform_defaults(monkeypatch):
    monkeypatch.setattr(T.sys, "platform", "darwin")

    assert T._platform_defaults("not-a-tool") == []


# ---------------------------------------------------------------------------
# Step 5 -- fail fast, with every path that was tried
# ---------------------------------------------------------------------------


def test_missing_tool_reports_not_found_with_the_full_trail(monkeypatch):
    _no_path_tools(monkeypatch)

    path, source, tried = T._resolve_one("starnet2", _EMPTY_CFG)

    assert path is None
    assert source == "not-found"
    assert any("SEESTAR_STARNET2" in line for line in tried)
    assert any("config tools.starnet2" in line for line in tried)


def test_resolve_tools_raises_naming_every_missing_tool_and_all_three_remedies(monkeypatch):
    """The error a novice actually hits when nothing is installed. Guards against
    it degenerating into a bare "tool not found": it must name each missing tool,
    and for each, all three ways to fix it (PATH, config key, env var)."""
    _no_path_tools(monkeypatch)

    with pytest.raises(T.ToolNotFoundError) as exc:
        T.resolve_tools(_EMPTY_CFG)

    msg = str(exc.value)
    assert "4 required tool(s) not found" in msg
    for tool in _TOOLS:
        assert f"  {tool}:" in msg
        assert f"tools.{tool} in config.yaml" in msg
        assert T._ENV[tool] in msg


def test_resolve_tools_reports_only_the_tools_that_are_missing(monkeypatch):
    """A partially-installed machine must be told exactly what to install --
    guards against an all-or-nothing message that names healthy tools too."""
    monkeypatch.setenv("SEESTAR_SIRIL", "/from/env/siril")
    monkeypatch.setenv("SEESTAR_EXIFTOOL", "/from/env/exiftool")
    _no_path_tools(monkeypatch)

    with pytest.raises(T.ToolNotFoundError) as exc:
        T.resolve_tools(_EMPTY_CFG)

    msg = str(exc.value)
    assert "2 required tool(s) not found" in msg
    assert "  graxpert:" in msg and "  starnet2:" in msg
    assert "\n  siril:" not in msg and "\n  exiftool:" not in msg


# ---------------------------------------------------------------------------
# resolve_tools -- the assembled ToolPaths
# ---------------------------------------------------------------------------


def test_resolve_tools_assigns_each_resolved_path_to_its_own_field(monkeypatch):
    """Guards against cross-wiring in the dict->ToolPaths splat (e.g. every field
    getting siril's path), which no other test would notice: the fakes elsewhere
    in the suite hand back a stub object rather than exercising this construction."""
    for tool in _TOOLS:
        monkeypatch.setenv(T._ENV[tool], f"/from/env/{tool}")

    paths = T.resolve_tools(_EMPTY_CFG)

    assert paths.siril == "/from/env/siril"
    assert paths.graxpert == "/from/env/graxpert"
    assert paths.starnet2 == "/from/env/starnet2"
    assert paths.exiftool == "/from/env/exiftool"


def test_resolve_tools_resolution_is_independent_per_tool(monkeypatch):
    """Each tool runs its own chain: one may come from the env while another
    comes from PATH and a third from a platform default."""
    monkeypatch.setenv("SEESTAR_SIRIL", "/from/env/siril")
    monkeypatch.setattr(T.shutil, "which", lambda name: "/from/path/exiftool" if name == "exiftool" else None)
    monkeypatch.setattr(
        T, "_platform_defaults", lambda t: [f"/from/platform/{t}"] if t in ("graxpert", "starnet2") else []
    )
    monkeypatch.setattr(T.os.path, "isfile", lambda p: p.startswith("/from/platform/"))

    paths = T.resolve_tools({"tools": {"graxpert": "/from/config/graxpert"}})

    assert paths.siril == "/from/env/siril"
    assert paths.graxpert == "/from/config/graxpert"
    assert paths.starnet2 == "/from/platform/starnet2"
    assert paths.exiftool == "/from/path/exiftool"


# ---------------------------------------------------------------------------
# doctor -- the user-facing surface over the same chain
# ---------------------------------------------------------------------------


def test_doctor_names_the_source_tier_each_tool_resolved_through(monkeypatch):
    """`seestar doctor` is how a user finds out WHICH path won, so the source
    label is the output's whole point (§5)."""
    monkeypatch.setenv("SEESTAR_SIRIL", "/from/env/siril")
    monkeypatch.setattr(T.shutil, "which", lambda name: "/from/path/tool")
    monkeypatch.setattr(T, "_platform_defaults", lambda t: [])

    out = T.doctor(_EMPTY_CFG)

    assert "siril" in out and "env" in out and "/from/env/siril" in out
    assert "auto-detect" in out
    assert "[ok]  All tools found." in out


def test_doctor_reports_missing_tools_instead_of_raising(monkeypatch):
    """doctor must DIAGNOSE a broken install, so unlike resolve_tools it reports
    rather than raises -- otherwise the one command that explains the problem is
    the one command that cannot run."""
    _no_path_tools(monkeypatch)

    out = T.doctor(_EMPTY_CFG)

    assert "[MISSING]" in out
    assert "Missing tool details:" in out
    assert "SEESTAR_GRAXPERT" in out
