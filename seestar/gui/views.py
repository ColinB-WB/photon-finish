"""Server-side copy and structured view models for the GUI. No HTTP, no HTML.

Every novice-facing RUNTIME string lives here (spec risk R5); static shell copy
lives in static/. Builders return plain JSON-safe dicts the front end renders
with textContent — never trusted HTML. Ported copy-equivalent from gui.py @
f425414; markdown syntax (** ` ### <details>) is dropped, wording preserved —
structure moved into typed fields. Strings marked NEW COPY have no v1.0
equivalent and are collected for owner review (Task 10).
"""

from __future__ import annotations

import os

from ..paths import safe_output_dir

ALL_TARGETS = "(all targets)"

PROFILES = ("showcase", "natural", "documentation", "print")
PROFILE_NAMES = {
    "showcase": "Showcase",
    "natural": "Natural",
    "documentation": "Reference",
    "print": "Print",
}

# Scan-time copy (plain, no ⚠️ — parity with gui.scan()).
SCAN_CHOOSE_FOLDER = "Choose a folder that contains Seestar stacked photos."
# Render-validation copy (⚠️ prefixed — parity with _render_impl()).
MSG_CHOOSE_LIBRARY = "⚠️ Choose a folder that contains Seestar stacked photos."
MSG_CHOOSE_OUTPUT = "⚠️ Choose where the finished images should be saved."
MSG_CHOOSE_PHOTO = "⚠️ Choose one photo, or deliberately choose the all-photos batch."
MSG_NO_MATCH = "No matching photo is ready. Find photos again, then choose one."
MSG_RECIPE_SINGLE = "⚠️ Choose one photo before using saved adjustments."
# NEW COPY: v1.0 raised a bare ValueError here (an ugly toast); same guard, said plainly.
MSG_OUTPUT_OVERLAPS_SOURCE = (
    "⚠️ The save folder overlaps your Seestar photos folder. "
    "Choose a save folder outside it — your original captures are never written to."
)
# NEW COPY: quit refused while rendering (spec §4).
MSG_QUIT_BUSY = (
    "Photon Finish is still creating a photo, so it will stay open. "
    "Wait for the current photo to finish, then quit again."
)
# NEW COPY: single-instance refusal (spec §8; replaces the busy-port message).
MSG_ALREADY_RUNNING = (
    "Photon Finish is already open. Use the window that is already running (or quit it) instead of opening it twice."
)
# NEW COPY: native-dialog serialization (spec §5, no dialog stacking).
MSG_PICKER_BUSY = "A folder chooser is already open. Finish that one first."
MSG_PICKER_FAILED = (
    "The folder chooser could not open. Type or paste the folder path into the box instead — "
    "that works exactly the same."
)
# NEW COPY: the native window backend is unavailable (owner decision 2026-08-20 --
# the browser fallback was REMOVED; a step that silently degrades is a defect,
# TENETS 7). Must name the component and the fix: a --windowed build has no console,
# so the traceback is invisible and this dialog is the user's only information.
MSG_WEBVIEW_UNAVAILABLE = (
    "Photon Finish could not open its app window. On Windows this almost always means the "
    "Microsoft Edge WebView2 Runtime is missing — install it free from "
    "https://developer.microsoft.com/microsoft-edge/webview2/ and open Photon Finish again."
)
# NEW COPY: covers every refusal case — idle, an unknown job, a single-photo
# job (no batch boundary to stop at), or a click that lands after the last
# batch boundary (the button never shows in these states, but a click can
# race job completion) — so the copy must not claim nothing is running.
MSG_STOP_UNAVAILABLE = "There is nothing left to stop — any photo still being created will finish on its own."
MSG_SHUTTING_DOWN = "Photon Finish is shutting down. No new photo was started."
MSG_RESULT_CONTRACT = (
    "Photon Finish finished processing but did not report a viewable image from this run. "
    "Older images were left untouched and are not shown as new results."
)


def out_name(profile: str) -> str:
    return "final" if profile == "showcase" else profile


def recipe_path(output_root: str, target: str) -> str:
    """Where the engine writes a target's recipe: <output root>/<target>/<target>_recipe.json."""
    return os.path.join(safe_output_dir(output_root, target), f"{target}_recipe.json")


def fine_tune_available(which: str | None, output_root: str | None) -> bool:
    """Fine-tune shows only when a single target has an emitted recipe (v1 §12b)."""
    root = (output_root or "").strip()
    if not which or which == ALL_TARGETS or not root:
        return False
    try:
        return os.path.exists(recipe_path(root, which))
    except ValueError:
        return False  # label escapes the output root: not a real target, so no recipe


def msg_no_recipe(which: str) -> str:
    return f"⚠️ No adjustment file exists for {which} yet. Create the image once first."


def friendly_error(message: str) -> str:
    lower = message.lower()
    if "did not finish within" in lower and "no source photo was changed" in lower:
        # ExternalToolTimeout's own message is already novice-safe and actionable
        # (tool name, configured duration, retry/setup guidance, never argv or a
        # source path) — show it unchanged instead of the generic copy below.
        return message
    if "no source photo was changed" in lower:
        # Diagnosis 7 (ported from the retired seestar/gui.py at the main merge): a
        # typed external-tool failure's own message is already the novice-safe
        # surface — it names the tool, the stage it stopped in, what the tool said
        # (bounded), any guidance, and where the full log is. Show it unchanged
        # rather than replacing it with the generic copy below, the same contract
        # the timeout branch above has.
        return message
    if "not a directory" in lower or "errno 20" in lower:
        return "The selected save location is not a folder. Choose a different save folder and try again."
    if "permission denied" in lower:
        return (
            "Photon Finish could not write to the selected save folder. Choose a folder you can write to and try again."
        )
    if "no space left" in lower:
        return "The save drive is out of space. Free some space or choose another save folder."
    return "Processing stopped before a new image could be saved."


def friendly_warning(warning: str) -> str:
    """Translate common QA wording without hiding the diagnostic text."""
    lower = warning.lower()
    if "rgb imbalance" in lower or "star colour" in lower or "star color" in lower:
        return "Some star colors may look stronger than intended."
    if "saturation" in lower:
        return "A few bright areas may have lost color detail."
    return warning.replace("_", " ").capitalize()


def variant_label(variant: str) -> str:
    labels = {
        "final": "Recommended",
        "closeup": "Galaxy close-up",
        "stars-on": "Full star field",
        "star-reduced": "Reduced stars",
        "starless": "Starless version",
        "natural": "Natural style",
        "documentation": "Reference style",
        "documentation-annotated": "Annotated reference",
        "print": "Print style",
    }
    return labels.get(variant, variant.replace("-", " ").replace("_", " ").title())


def status_text(target, target_i, target_n, stage, tool, step_i, step_n, elapsed_s) -> str:
    """The live status line — same composition as gui._status @ f425414, minus markdown."""
    el = int(elapsed_s)
    elapsed = f"{el // 60}:{el % 60:02d}"
    tgt = f"{target} (target {target_i}/{target_n})" if target_n > 1 else target
    step = f"Step {step_i} of {step_n} · " if step_n and step_i else ""
    tool_s = f" ({tool})" if tool else ""
    return f"⏳ {tgt} · Latest update: {step}{stage or 'Starting…'}{tool_s} · {elapsed} elapsed"


def busy_message(target: str, elapsed_s: float) -> str:
    """Refusal shown to a second render attempt while one runs (single-flight)."""
    mins = int(elapsed_s / 60) if elapsed_s else 0
    ago = f" It started {mins} minute{'s' if mins != 1 else ''} ago." if mins else ""
    return (
        f"⏳ Photon Finish is still working on {target or 'another photo'} in the background.{ago} "
        "This page has reattached to that creation. Nothing new was started."
    )


def summary_line(done: list, failed: list, skipped: list, profile: str, output_root: str) -> str:
    style = PROFILE_NAMES.get(profile, str(profile).title())
    if failed and not done:
        line = f"❌ Couldn't create {', '.join(failed)}. No new image was saved."
    elif failed:
        line = f"⚠️ Finished {', '.join(done)}. Couldn't create {', '.join(failed)}."
    elif skipped:
        line = f"⚠️ Finished: {', '.join(done)} · {style} style. Saved in {output_root}"
    else:
        line = f"✅ Finished: {', '.join(done)} · {style} style. Saved in {output_root}"
    if skipped:
        # NEW COPY: batch-boundary graceful stop (spec §4, owner-approved 2026-08-12).
        line += f" Stopped before {', '.join(skipped)} at your request."
    return line


def job_failed_line(done: list) -> str:
    """Job-runner failure: preserve known completed results and avoid false save claims."""
    if done:
        return (
            f"❌ Finished {', '.join(done)}, then Photon Finish encountered an internal problem. "
            "Review the completed results and technical details below."
        )
    return "❌ Photon Finish encountered an internal problem. Review the technical details below before trying again."


def scan_view(discovery: dict) -> dict:
    """discover_targets() output -> the scan panel's view model (parity: gui.scan)."""
    targets = discovery["targets"]
    skipped = discovery["skipped"]
    rows = [[t["target"], t["filter"] or "?", t["sessions"], t["datetime"] or "?"] for t in targets]
    if not targets:
        detail = None
        if skipped:
            ex = skipped[0]
            detail = f"{len(skipped)} FITS file(s) skipped — for example {os.path.basename(ex['path'])}: {ex['reason']}"
        return {
            "rows": [],
            "choices": [],
            "value": None,
            "message": "No Seestar stacked photos were found in that folder.",
            "detail": detail,
        }
    names = [t["target"] for t in targets]
    choices = [{"label": f"All {len(targets)} photos — longer batch", "value": ALL_TARGETS}] if len(names) > 1 else []
    choices += [{"label": n, "value": n} for n in names]
    message = f"Found {len(targets)} photo{'s' if len(targets) != 1 else ''}. Choose one below."
    if skipped:
        message += f" {len(skipped)} non-stacked file(s) were ignored."
    return {
        "rows": rows,
        "choices": choices,
        "value": names[0] if len(names) == 1 else None,
        "message": message,
        "detail": None,
    }


def qa_unavailable_view(exc: BaseException) -> dict:
    return {
        "heading": "Result check unavailable",
        "explanation": "The image was created, but its automatic quality check could not finish.",
        "warnings": [],
        "details": [],
        "technical": str(exc),
    }


def qa_view(sc: dict) -> dict:
    """qa_target() scorecard -> view model (copy-equivalent to gui._qa_markdown)."""
    m, ag = sc["metrics"], sc.get("absolute_gates", {})
    hero = sc.get("hero") or {}
    outcome = hero.get("outcome")
    heading, explanation = {
        "pass": ("Result check: ✅ Ready to use", "The finished image passed its automatic quality check."),
        "source_limited": (
            "Result check: 🟡 Finished — capture-limited",
            "The image was created, but the original capture limits how much detail can be recovered.",
        ),
        "processing_failed": (
            "Result check: ❌ Needs attention",
            "The automatic check found a processing problem in the finished image.",
        ),
    }.get(
        outcome,
        (
            "Result check: ✅ Ready to use" if sc["passed"] else "Result check: ❌ Needs attention",
            "The automatic quality check has finished.",
        ),
    )
    category = str(sc.get("category", "image")).replace("_", " ").title()
    warnings = sc.get("warnings", [])
    details = [f"Check category: {category}", f"Stars detected: {m.get('star_count')}"]
    if m.get("fwhm"):
        details.append(f"FWHM: {m.get('fwhm'):.2f}")
    details.append(
        f"Body p90: {m.get('body_p90')} · Background: "
        f"{m.get('background_median_outside_mask')} · Core saturation: {m.get('core_sat_frac')}"
    )
    if ag.get("faint_structure_frac") is not None:
        details.append(f"Faint structure: {ag['faint_structure_frac']}")
    if not sc["passed"]:
        details.append("Failed checks: " + "; ".join(sc["hard_fails"]))
    if hero.get("reasons"):
        details.append("Processing outcome: " + "; ".join(hero["reasons"]))
    details.extend(f"Warning: {w}" for w in warnings)
    return {
        "heading": heading,
        "explanation": explanation,
        "warnings": [friendly_warning(w) for w in warnings],
        "details": details,
        "technical": None,
    }


def replay_view(notes: dict | None) -> dict | None:
    """The recipe-replay change report (parity: gui._replay_markdown), as data."""
    rep = (notes or {}).get("recipe_replay")
    if not rep:
        return None
    return {
        "changes": [
            {
                "adjustment": c["adjustment"],
                "prior": c["prior"],
                "now": c["now"],
                "source": c["source"],
                "changed": bool(c["changed"]),
            }
            for c in rep.get("changes", [])
        ],
        "warnings": list(rep.get("warnings", [])),
    }


def _hero_first(images: list[dict]) -> list[dict]:
    hero = [i for i in images if i["label"] in ("Recommended", "Galaxy close-up")]
    return hero + [i for i in images if i not in hero]


def images_from_result(result: dict | None, target: str) -> list[dict]:
    """Only images reported by THIS run (stale-style protection), hero first."""
    entries = (result or {}).get("outputs") or []
    current = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = entry.get("jpg")
        if path and os.path.exists(path):
            variant = entry.get("variant") or os.path.basename(path)[len(target) + 1 : -4]
            current.append({"path": path, "label": variant_label(str(variant)), "filename": os.path.basename(path)})
    return _hero_first(current)
