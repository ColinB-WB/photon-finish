"""Copy + view models: the single home of novice-facing runtime strings (spec R5).

Mostly characterization ports from tests/test_gui.py @ f425414 (markdown syntax
dropped, wording preserved); the structured qa/replay/scan shapes are new TDD."""

import os

from seestar.gui import views


def _touch(p):
    open(p, "w").close()


# --- ported characterization (green once views.py lands) ---------------------


def test_out_name_maps_profile_to_fork_label():
    assert views.out_name("showcase") == "final"
    assert views.out_name("natural") == "natural"
    assert views.out_name("print") == "print"


def test_recipe_path(tmp_path):
    root = str(tmp_path)
    assert views.recipe_path(root, "M81") == os.path.join(os.path.realpath(root), "M81", "M81_recipe.json")


def test_fine_tune_available_rules(tmp_path):
    (tmp_path / "M81").mkdir()
    (tmp_path / "M81" / "M81_recipe.json").write_text("{}")
    assert views.fine_tune_available("M81", str(tmp_path)) is True
    assert views.fine_tune_available("M8", str(tmp_path)) is False
    assert views.fine_tune_available(views.ALL_TARGETS, str(tmp_path)) is False
    assert views.fine_tune_available(None, str(tmp_path)) is False
    assert views.fine_tune_available("M81", "") is False
    assert views.fine_tune_available("../evil", str(tmp_path)) is False  # label escape -> unavailable, not an exception


def test_friendly_error_preserves_plain_language_action():
    assert "not a folder" in views.friendly_error("[Errno 20] Not a directory: '/dev/null'").lower()
    assert "write" in views.friendly_error("[Errno 13] Permission denied").lower()


def test_friendly_error_preserves_actionable_graxpert_timeout_message():
    from seestar.adapters._external_process import _timeout_message

    out = views.friendly_error(_timeout_message("graxpert", 3600.0))
    assert "GraXpert" in out and "60 minutes" in out and "try again" in out.lower()
    assert "/" not in out and "--" not in out and ".fit" not in out


def test_friendly_error_preserves_generic_timeout_message_for_non_graxpert_tools():
    from seestar.adapters._external_process import _timeout_message

    out = views.friendly_error(_timeout_message("siril", 1800.0))
    assert "Siril" in out and "30 minutes" in out and "try again" in out.lower()
    assert "model setup" not in out.lower()
    assert "/" not in out and "--" not in out


def test_friendly_error_preserves_an_actionable_tool_failure_message():
    """Diagnosis 7: a typed external-tool failure's own message is the novice-safe
    surface -- tool, stage, what the tool said (bounded), guidance, and where the
    bounded full log is. `friendly_error` must show it UNCHANGED rather than
    replacing it with the generic 'Processing stopped' copy, the same contract the
    timeout branch already has. Built from the real failure type so this stays
    honest about production wording. Ported from the retired seestar/gui.py at the
    main merge, where it guarded the same behaviour."""
    from seestar.adapters._external_process import ExternalToolFailure

    message = str(
        ExternalToolFailure(
            tool="graxpert",
            failure_class="no_output",
            summary="Error: could not download the denoise model",
            stage="reducing noise",
            exit_status=1,
            log_path="/out/A/seestar_diagnostics.log",
            guidance="Open GraXpert once to finish its model setup, then try again.",
        )
    )
    out = views.friendly_error(message)

    assert out == message, "the actionable message must not be replaced by generic copy"
    assert "GraXpert" in out and "reducing noise" in out
    assert "could not download the denoise model" in out
    assert "seestar_diagnostics.log" in out
    assert "try again" in out.lower()


def test_friendly_error_still_generalizes_an_unrecognized_error():
    # the generic branch must survive: an error with none of the actionable
    # markers is still translated into plain language, not leaked raw
    assert views.friendly_error("KeyError: 'probe_7'") == "Processing stopped before a new image could be saved."


def test_missing_current_outputs_never_falls_back_to_stale_files(tmp_path):
    _touch(tmp_path / "M8_final.jpg")  # prior-run file must never appear as current
    assert views.images_from_result(None, "M8") == []


def test_images_from_result_excludes_stale_images(tmp_path):
    current = tmp_path / "M8_final.jpg"
    _touch(current)
    _touch(tmp_path / "M8_natural.jpg")  # stale, not in this run's outputs
    images = views.images_from_result({"outputs": [{"jpg": str(current), "variant": "final"}]}, "M8")
    assert [(i["path"], i["label"]) for i in images] == [(str(current), "Recommended")]


def test_images_from_result_orders_hero_first(tmp_path):
    """Parity item 'Recommended first': outputs order is not trusted."""
    final = tmp_path / "M8_final.jpg"
    starless = tmp_path / "M8_starless.jpg"
    _touch(final)
    _touch(starless)
    images = views.images_from_result(
        {"outputs": [{"jpg": str(starless), "variant": "starless"}, {"jpg": str(final), "variant": "final"}]},
        "M8",
    )
    assert images[0]["label"] == "Recommended"


# --- structured view models (new shapes; TDD) --------------------------------


def test_status_text_composition():
    s = views.status_text("M8", 2, 3, "Removing stars", "StarNet2", 8, 9, 125)
    assert "M8 (target 2/3)" in s
    assert "Step 8 of 9" in s and "Removing stars (StarNet2)" in s
    assert "2:05 elapsed" in s
    single = views.status_text("M8", 1, 1, "Starting…", "", 0, 0, 0)
    assert "(target" not in single and "Step" not in single and "Starting…" in single


def test_busy_message_names_target_and_age():
    m = views.busy_message("M8", 130)
    assert "M8" in m and "2 minutes ago" in m and "Nothing new was started." in m
    assert "another photo" in views.busy_message("", 10)


def test_summary_lines():
    ok = views.summary_line(["M8"], [], [], "showcase", "/out")
    assert ok.startswith("✅ Finished: M8") and "Showcase style" in ok and "/out" in ok
    part = views.summary_line(["M8"], ["M17"], [], "showcase", "/out")
    assert part.startswith("⚠️ Finished M8.") and "Couldn't create M17." in part
    bad = views.summary_line([], ["M8"], [], "showcase", "/out")
    assert bad.startswith("❌ Couldn't create M8.") and "No new image was saved." in bad
    stopped = views.summary_line(["M8"], [], ["M17", "M20"], "showcase", "/out")
    assert stopped.startswith("⚠️") and "Stopped before M17, M20 at your request." in stopped
    partial_internal = views.job_failed_line(["M8"])
    assert "Finished M8" in partial_internal and "No new image" not in partial_internal
    assert "technical details" in views.job_failed_line([]).lower()


def test_scan_view_multi_and_single(tmp_path, fake_stack):
    from seestar.run import discover_targets

    for name, filt in (("M5", "IRCUT"), ("M8", "LP")):
        d = tmp_path / name
        d.mkdir()
        fake_stack(d / f"Stacked_51_{name}_10.0s_{filt}_20260615-221349.fit", obj=name, filt=filt, stackcnt=51)
    v = views.scan_view(discover_targets(str(tmp_path)))
    assert v["message"].startswith("Found 2 photos")
    assert v["choices"][0] == {"label": "All 2 photos — longer batch", "value": views.ALL_TARGETS}
    assert {c["value"] for c in v["choices"][1:]} == {"M5", "M8"}
    assert v["value"] is None  # multi-target batches require an explicit choice
    single = views.scan_view(discover_targets(str(tmp_path / "M5")))
    assert [c["value"] for c in single["choices"]] == ["M5"] and single["value"] == "M5"


def test_scan_view_reports_skipped(tmp_path, fake_stack):
    from seestar.run import discover_targets

    fake_stack(tmp_path / "sub.fit", shape=(64, 64))  # 2-D raw sub: skipped
    v = views.scan_view(discover_targets(str(tmp_path)))
    assert v["rows"] == [] and v["choices"] == []
    assert v["message"] == "No Seestar stacked photos were found in that folder."
    assert "sub.fit" in v["detail"] and "3-channel" in v["detail"]  # the real reason, surfaced


def test_qa_view_surfaces_warnings_even_when_gates_pass():
    sc = {
        "passed": True,
        "category": "nebula",
        "metrics": {
            "star_count": 5258,
            "fwhm": 2.73,
            "body_p90": 64.8,
            "background_median_outside_mask": 11.8,
            "core_sat_frac": 0.0,
        },
        "hard_fails": [],
        "warnings": ["stretch solver did NOT converge: body_p90 achieved 64.8 vs target 72.0"],
        "absolute_gates": {"faint_structure_frac": 0.02},
    }
    v = views.qa_view(sc)
    assert v["heading"] == "Result check: ✅ Ready to use"
    assert any("did NOT converge" in d for d in v["details"])
    assert v["warnings"]  # friendly form shown above details too


def test_qa_view_processing_failed_presentation():
    sc = {
        "passed": False,
        "category": "nebula",
        "metrics": {
            "star_count": 2583,
            "fwhm": 5.90,
            "body_p90": 57.2,
            "background_median_outside_mask": 9.2,
            "core_sat_frac": 0.0,
        },
        "hard_fails": ["neon color: pegged_chroma_frac_body 26.07% > 20.0%"],
        "warnings": [],
        "absolute_gates": {"faint_structure_frac": 0.02},
        "hero": {"outcome": "processing_failed", "reasons": ["hard gate(s) failed: pegged_chroma"]},
    }
    v = views.qa_view(sc)
    assert "Needs attention" in v["heading"]
    assert any(d.startswith("Processing outcome:") for d in v["details"])
    assert any(d.startswith("Failed checks:") for d in v["details"])


def test_qa_view_source_limited_presentation():
    sc = {
        "passed": False,
        "category": "nebula",
        "metrics": {},
        "hard_fails": ["fwhm"],
        "warnings": [],
        "absolute_gates": {},
        "hero": {"outcome": "source_limited", "reasons": ["fwhm attributed to capture"]},
    }
    v = views.qa_view(sc)
    assert "capture-limited" in v["heading"]


def test_qa_unavailable_view_keeps_technical_detail():
    v = views.qa_unavailable_view(RuntimeError("boom <tag>"))
    assert v["heading"] == "Result check unavailable"
    assert v["technical"] == "boom <tag>"  # raw text; front end renders with textContent


def test_replay_view():
    notes = {
        "recipe_replay": {
            "warnings": ["pinned adjustment 'x' did not apply to this render — the pin had no effect"],
            "changes": [
                {"adjustment": "denoise", "prior": 0.65, "now": 0.7, "source": "pinned", "changed": True},
                {"adjustment": "saturation", "prior": 0.6, "now": 0.6, "source": "re-derived", "changed": False},
            ],
        }
    }
    v = views.replay_view(notes)
    assert v["changes"][0] == {"adjustment": "denoise", "prior": 0.65, "now": 0.7, "source": "pinned", "changed": True}
    assert "did not apply" in v["warnings"][0]
    assert views.replay_view({}) is None and views.replay_view(None) is None
