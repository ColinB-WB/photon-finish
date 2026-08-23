"""One committed identity, two evaluation surfaces, one verdict.

The immediate candidate evaluation (`orchestrate.run_target`, which DECIDES
publication) and a later `seestar qa` replay (`qa.qa_target`, which reports on
what shipped) must reach the same conclusion from the same committed files. They
did not: the immediate path passed `faint=None` into `evaluate_gates` while the
replay measured persisted faint evidence, so a render could be published as PASS
and then read back as a hard FAIL (owner ruling, 2026-08-14).

That inconsistency is a Tenet 7 defect in its own right -- a gate that silently
skips on the deciding surface is not a gate -- and exempting star fields from the
faint gate would have hidden its symptom without fixing it for extended targets.

Placeholder target name "M" (never a real astronomical object) per CLAUDE.md's
target-agnostic rule.
"""

from __future__ import annotations

import json
import os

import seestar.orchestrate as orch
import seestar.qa as qa_module
from seestar.core.publication import published_qa_base, sidecar_names
from seestar.io import load_config


class _FakeTools:
    siril = "siril-cli"
    graxpert = "GraXpert"
    starnet2 = "starnet2"
    exiftool = "exiftool"


def _cfg():
    cfg = load_config()
    cfg["engine"]["trail_removal"]["enabled"] = False
    cfg["engine"]["catalog_prior"] = False
    return cfg


def _make_target_dir(tmp_path):
    from conftest import write_fake_stack

    target_dir = tmp_path / "M_import"
    target_dir.mkdir()
    write_fake_stack(target_dir / "Stacked_51_M_10.0s_LP_20260615-221349.fit", obj="M")
    return str(target_dir)


def _install_fakes(monkeypatch):
    """The hermetic harness WITHOUT any hero/gate short-circuit: no `_force_pass`,
    no pinned metrics, no pinned faint evidence. Both surfaces must measure the
    same real (if synthetic) files and agree on their own."""
    from conftest import (
        fake_measure_subject_support,
        fake_probe_metric,
        install_fake_op_adapters,
        install_fake_run_siril,
    )

    import seestar.execute as exec_module
    import seestar.measure as measure_module

    monkeypatch.setattr(orch, "resolve_tools", lambda cfg: _FakeTools())
    monkeypatch.setattr(qa_module, "resolve_tools", lambda cfg: _FakeTools())
    monkeypatch.setattr(orch, "embed", lambda *a, **kw: None)
    install_fake_op_adapters(monkeypatch)
    install_fake_run_siril(monkeypatch)
    monkeypatch.setattr(exec_module, "_probe_metric", fake_probe_metric)
    monkeypatch.setattr(measure_module, "measure_subject_support", fake_measure_subject_support)
    # `embed` is a no-op in this harness, so the published JPG carries no EXIF
    # keywords for `seestar qa` to read the resolved category back from. Stand in
    # for the metadata the real exporter writes -- without it the replay reads
    # UNKNOWN, skips every category gate, and the two surfaces would be compared
    # on different gate sets (the comparison would pass vacuously).
    monkeypatch.setattr(qa_module, "read_xmp_subject", lambda *a, **kw: "nebula 3.99 arcsec/px showcase")


def test_immediate_run_and_later_qa_agree_on_hard_gates_and_hero_outcome(tmp_path, monkeypatch):
    """The named invariant (owner ruling, 2026-08-14): from ONE committed
    identity, the deciding surface and the reporting surface produce the same
    applicable hard-gate verdicts and the same hero outcome.

    Red before the fix: `run_target` evaluated the faint gate as
    not-measured (faint=None was passed unconditionally) while `qa_target`
    measured the published starless against the published support raster, so the
    two disagreed on both the gate set and, where that gate decided it, the hero
    outcome. Green command:
    `pytest tests/test_qa_gate_parity.py`."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")
    _install_fakes(monkeypatch)

    result = orch.run_target(target_dir, out_dir, _cfg())

    # the run committed an identity and recorded its own hero verdict
    hero_run = result["notes"]["hero_contract"]
    assert result["status"] == hero_run["outcome"]

    # ...and a later `seestar qa` over the same directory recomputes it
    sc = qa_module.qa_target(out_dir, _cfg(), "M")
    hero_qa = sc["hero"]

    assert hero_qa["outcome"] == hero_run["outcome"], (
        f"deciding surface said {hero_run['outcome']}, reporting surface said {hero_qa['outcome']}"
    )
    # every hard gate, verdict by verdict -- not just the summary outcome, which
    # could agree by luck while the two surfaces judged different evidence
    gates_qa = {g["gate"]: g["verdict"] for g in sc["gate_report"]["gates"]}
    assert gates_qa == hero_run["gates"]
    # ...and the gate that used to differ is genuinely APPLICABLE here, so the
    # comparison is not satisfied by both surfaces skipping everything
    assert gates_qa["faint_structure"] != "skipped"


def test_faint_evidence_is_measured_on_the_deciding_surface(tmp_path, monkeypatch):
    """The specific regression: the immediate evaluation must MEASURE faint
    evidence from this attempt's candidate starless and the structure-support
    raster, not pass None. Asserted at the seam (the call actually happens, with
    both rasters) so the parity above cannot be satisfied by both surfaces
    skipping the gate."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")
    _install_fakes(monkeypatch)

    seen: list[tuple] = []
    real = orch.faint_evidence

    def _spy(starless_render, support_png, support_recorded):
        seen.append((starless_render, support_png, support_recorded))
        return real(starless_render, support_png, support_recorded)

    monkeypatch.setattr(orch, "faint_evidence", _spy)
    orch.run_target(target_dir, out_dir, _cfg())

    assert seen, "the deciding surface never measured faint evidence"
    starless, support_png, recorded = seen[0]
    assert recorded is True, "the run measured a valid structure support but did not report it as recorded"
    assert support_png and os.path.basename(support_png).startswith("M_support")
    assert starless and starless.endswith(".tif"), "faint evidence must come from a starless raster"


def test_solve_region_provenance_survives_into_the_replay(tmp_path, monkeypatch):
    """The contract metric a replay re-measures must come from the run's OWN
    persisted region provenance -- never a fresh object mask of the final render
    that merely resembles the frozen region."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")
    _install_fakes(monkeypatch)

    result = orch.run_target(target_dir, out_dir, _cfg())
    certified = result["published"]["certified"]
    sn = sidecar_names("M", "final", certified)

    notes = json.loads(open(os.path.join(out_dir, sn["notes"]), encoding="utf-8").read())["notes"]
    assert notes["stretch_solve"]["region_source"] == "frozen_object_mask"
    assert notes["solve_region"]["artifact"] == "M_solve_region"
    man = json.loads(open(os.path.join(out_dir, sn["run"]), encoding="utf-8").read())
    assert published_qa_base("M", "solve_region", certified) + ".png" in man["qa_artifacts"]
