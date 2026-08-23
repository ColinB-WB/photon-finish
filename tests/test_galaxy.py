"""Phase-5 galaxy treatment wiring: render() inserts the `ght` command between the
base stretch and saturation. (Policy's ght-attachment-by-category coverage moved to
tests/test_core_policy.py -- test_galaxy_gets_pcc_and_ghs_and_closeup,
test_starfield_pcc_off_no_ghs, test_natural_strips_nebula_ghs_when_enabled -- Task 23,
policy.py/profiles.py deleted.)"""

from seestar.adapters import siril as _siril_adapter
from seestar.adapters.base import ToolPaths
from seestar.adapters.siril import stretch_chain_lines

# Dummy paths: run_siril is monkeypatched in every test that uses _TOOLS, so the
# actual executables are never invoked -- any non-empty string is fine here.
_TOOLS = ToolPaths(siril="siril", graxpert="graxpert", starnet2="starnet2", exiftool="exiftool")


def test_render_inserts_ght_between_stretch_and_satu(monkeypatch):
    captured = {}
    monkeypatch.setattr(_siril_adapter, "run_siril", lambda lines, tools: captured.setdefault("lines", lines))
    ght = {"ght_d": 1.5, "ght_b": 5.0, "ght_sp": 0.25, "ght_lp": 0.06, "ght_hp": 0.65, "unsharp_amount": 0.0}
    _siril_adapter.render(
        "/tmp/x_lin.fit",
        "/tmp",
        "x_final",
        asinh_d=55,
        blackpoint=0.01,
        mtf_mid=0.04,
        saturation=0.6,
        tools=_TOOLS,
        ght=ght,
    )
    lines = captured["lines"]
    ght_line = next((line for line in lines if line.startswith("ght ")), None)
    assert ght_line is not None
    assert "-D=1.5" in ght_line and "-SP=0.25" in ght_line
    assert "-LP=0.06" in ght_line and "-HP=0.65" in ght_line
    # ordering: asinh -> mtf -> ght -> satu
    idx = {k: i for i, line in enumerate(lines) for k in ("asinh", "mtf 0", "ght ", "satu") if line.startswith(k)}
    assert idx["asinh"] < idx["mtf 0"] < idx["ght "] < idx["satu"]


def test_render_no_ght_when_absent(monkeypatch):
    captured = {}
    monkeypatch.setattr(_siril_adapter, "run_siril", lambda lines, tools: captured.setdefault("lines", lines))
    _siril_adapter.render(
        "/tmp/x_lin.fit",
        "/tmp",
        "x_final",
        asinh_d=75,
        blackpoint=0.01,
        mtf_mid=0.08,
        saturation=0.6,
        tools=_TOOLS,
        ght=None,
    )
    assert not any(line.startswith("ght ") for line in captured["lines"])


def test_solver_probe_chain_matches_render_chain():
    """Option A (session-8 contract): the solver probes the SAME chain the final
    render ships (asinh -> mtf -> ght -> satu). Ported from tests/test_profiles.py
    (Task 23: unrelated to policy.py/profiles.py, this only exercises
    adapters.siril.stretch_chain_lines -- misplaced there)."""
    ght = {"ght_d": 1.5, "ght_b": 5.0, "ght_sp": 0.25, "ght_lp": 0.05, "ght_hp": 0.70}
    chain = stretch_chain_lines("lin", 75, 0.0145, 0.08, 0.6, ght=ght)
    assert any(line.startswith("ght ") for line in chain), "GHS must be inside the solved chain"
    ops = [line.split()[0] for line in chain]
    assert ops.index("asinh") < ops.index("mtf") < ops.index("ght") < ops.index("satu")
    # and without a treatment, the same chain minus the GHS
    base = stretch_chain_lines("lin", 75, 0.0145, 0.08, 0.6)
    assert [line for line in chain if not line.startswith(("ght", "save"))] == base
