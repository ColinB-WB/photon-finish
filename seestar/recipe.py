"""Per-image adjustment recipe (spec: docs/history/superpowers/specs/2026-07-11-adjustment-recipe-design.md).

Two modes, ONE code path (run.run_target):
  (a) every run EMITS <output_dir>/<TARGET>_recipe.json — each adjustment that applied,
      with the app's value, a plain-English note, the reason it was chosen, the symptoms
      if it's wrong, and "your_value": null (replace null with a number/boolean to pin).
  (b) `seestar run --recipe <file>` VALIDATES the edited file (hard error listing every
      problem), honors every non-null your_value as a PIN, re-derives all app-owned
      values from measurement, renders the same outputs, and re-emits the recipe with
      your_value preserved verbatim.

``ADJUSTMENTS`` is the single source of truth for the recipe surface (mirrors
prefs.ALL_PRESETS): emission, validation, error messages, and override translation all
derive from it. Two kinds: "tunable" (app default shown; a pin overrides it) and
"solved" (re-derived from measurement every run; a pin skips the solver for that
control). Target-agnostic: replay logic never reads the object's name.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime

from . import __version__
from .core.types import Decisions, EngineDefaults, Overrides
from .io import overrides_from_prefs_dict

RECIPE_FORMAT = 1


class RecipeError(ValueError):
    """A recipe problem the user must fix; the message is the full human-readable list."""


@dataclass(frozen=True)
class Adjustment:
    param: str  # params-dict key the pin maps to
    tool: str  # which tool applies this (so a user can open it and experiment)
    kind: str  # "tunable" | "solved"
    vtype: str  # "float" | "bool"
    lo: float | None  # inclusive lower bound (floats; None for bool)
    hi: float | None  # inclusive upper bound
    exclusive: bool  # bounds are exclusive (mtf_mid: 0 < m < 1)
    note: str  # plain-English what-this-is (emitted verbatim)
    direction: str  # the knob's direction, one line (also used in error messages)
    symptoms: str  # symptoms-if-wrong (emitted verbatim)


ADJUSTMENTS: dict[str, Adjustment] = {
    "trail_removal": Adjustment(
        param="trail_removal_enabled",
        tool="this app (built-in trail detection and repair)",
        kind="tunable",
        vtype="bool",
        lo=None,
        hi=None,
        exclusive=False,
        note="Whether to detect and remove a satellite/aircraft trail (a long thin straight "
        "bright line) before processing.",
        direction="true = remove a detected trail; false = leave the frame untouched",
        symptoms="Wrongly on: a real linear feature (e.g. a diffraction spike) gets painted over. "
        "Wrongly off: a bright streak crosses the finished image.",
    ),
    "crop_border_frac": Adjustment(
        param="crop_border_frac",
        tool="Siril (crop)",
        kind="tunable",
        vtype="float",
        lo=0.0,
        hi=0.1,
        exclusive=False,
        note="By default (null) the app measures each edge and trims only demonstrated-invalid "
        "borders -- a clean frame keeps every pixel. Setting a value here forces the legacy "
        "fixed symmetric crop of that fraction from every edge.",
        direction="null = measured per-edge automatic crop; higher = more edge removed (forced)",
        symptoms="Wrongly forced high: you lose usable sky area (real stars/nebulosity near the "
        "edges are cut). Wrongly forced low on a damaged frame: dark ragged edges survive into "
        "the finished image and skew the background model.",
    ),
    "bge_smoothing": Adjustment(
        param="bge_smoothing",
        tool="GraXpert (background extraction)",
        kind="tunable",
        vtype="float",
        lo=0.0,
        hi=1.0,
        exclusive=False,
        note="How smooth the modeled light-pollution gradient is before it is subtracted.",
        direction="higher = smoother, larger-scale gradient model",
        symptoms="Too low: the model chases real nebulosity and eats faint structure. "
        "Too high: strong local gradients survive subtraction.",
    ),
    "denoise_strength": Adjustment(
        param="denoise_strength",
        tool="GraXpert (denoising)",
        kind="tunable",
        vtype="float",
        lo=0.0,
        hi=1.0,
        exclusive=False,
        note="How much AI noise smoothing is applied (0.0–1.0). Counter-intuitively, more "
        "denoise reveals MORE faint stars (they rise above the detection threshold). Values "
        "around 0.6 are typical; faint galaxies use about 0.65, while dense stellar cores "
        "(clusters) can begin to over-smooth above roughly 0.6–0.7.",
        direction="higher = smoother background, more faint stars revealed (dense cores over-smooth past ~0.6–0.7)",
        symptoms="Too high: creamy/plastic look, dense star cores smear together. "
        "Too low: grainy background, faint stars missing.",
    ),
    "color_calibration": Adjustment(
        param="pcc",
        tool="Siril (photometric color calibration)",
        kind="tunable",
        vtype="bool",
        lo=None,
        hi=None,
        exclusive=False,
        note="Whether photometric color calibration (PCC) runs. The app decides from the "
        "filter and measured category: dual-band LP filters and star clusters keep native "
        "color; broadband galaxies get calibrated.",
        direction="true = calibrate star color against the catalog; false = keep native color",
        symptoms="Wrongly on (dual-band nebula): deep red emission washes to pink. "
        "Wrongly off (broadband galaxy): the body keeps an uncalibrated color cast.",
    ),
    "saturation": Adjustment(
        param="saturation",
        tool="Siril (color saturation)",
        kind="tunable",
        vtype="float",
        lo=0.0,
        hi=2.0,
        exclusive=False,
        note="Color saturation applied after the stretch (threshold 0, so faint signal is saturated too).",
        direction="higher = more colorful",
        symptoms="Too high: neon color, hues pinned to the rail (the QA pegged-chroma gate "
        "fails). Too low: gray, lifeless image.",
    ),
    "contrast_shape": Adjustment(
        param="asinh_d",
        tool="Siril (asinh stretch)",
        kind="tunable",
        vtype="float",
        lo=1.0,
        hi=500.0,
        exclusive=False,
        note="The asinh stretch depth — the contrast SHAPE of the lift (the brightness "
        "LEVEL is solved separately per image).",
        direction="higher = stronger lift of faint signal relative to bright",
        symptoms="Too high: the faint field lifts into visible noise and bright cores flatten. "
        "Too low: the subject looks small, dim and underwhelming.",
    ),
    "brightness_target": Adjustment(
        param="stretch_target_level",
        tool="this app (per-image solver) — applied via Siril's midtone lift",
        kind="tunable",
        vtype="float",
        lo=1.0,
        hi=200.0,
        exclusive=False,
        note="The output level the stretch solver aims for (0–255 scale): extended targets "
        "solve the subject's brightness (body p90), star fields solve the sky median. This "
        "is THE brightness knob.",
        direction="higher = brighter render",
        symptoms="Too high: gray washed-out sky, blown cores. Too low: dark, small-looking "
        "subject that does not fill the frame.",
    ),
    "structure_boost": Adjustment(
        param="structure_enabled",
        tool="Siril (generalized hyperbolic stretch)",
        kind="tunable",
        vtype="bool",
        lo=None,
        hi=None,
        exclusive=False,
        note="Whether the measured structure treatment (GHS) runs on top of the base stretch "
        "— nebula envelope, galaxy dust lane, or cluster halo. Pinning this true can only "
        "UN-DO an 'off' decision the profile or preferences made — it cannot invent a "
        "structure treatment for an image the engine decided has none.",
        direction="true = reveal faint structure aggressively; false = honest base stretch only",
        symptoms="Wrongly on: overcooked look on weak data. Wrongly off: the faint envelope/"
        "halo/dust lane the data supports stays invisible.",
    ),
    "star_prominence": Adjustment(
        param="star_reduce_k",
        tool="StarNet2 (star separation) + Siril (blend)",
        kind="tunable",
        vtype="float",
        lo=0.0,
        hi=1.0,
        exclusive=False,
        note="How strongly stars are blended back into the star-reduced hero image (screen blend strength k).",
        direction="higher = more prominent stars",
        symptoms="Too high: the star field competes with the subject. Too low: an eerie, "
        "nearly starless sky that reads as fake.",
    ),
    "blackpoint": Adjustment(
        param="blackpoint",
        tool="this app (measured from the image) — applied via Siril's asinh stretch",
        kind="solved",
        vtype="float",
        lo=0.0,
        hi=1.0,
        exclusive=False,
        note="The stretch black point, set just BELOW the measured sky level so the "
        "background is dark but the faint star field survives.",
        direction="higher = darker sky (and, past the sky median, clipped faint stars)",
        symptoms="Too high: faint stars vanish and the frame looks empty (a black point "
        "above the sky median once cut a star field from ~900 stars to 26). "
        "Too low: gray, washed-out background.",
    ),
    "mtf_mid": Adjustment(
        param="mtf_mid",
        tool="this app (per-image solver) — applied via Siril's midtone lift",
        kind="solved",
        vtype="float",
        lo=0.0,
        hi=1.0,
        exclusive=True,
        note="The midtone lift applied after asinh. The app solves this per image so the "
        "render hits brightness_target.",
        direction="LOWER = brighter (Siril mtf semantics)",
        symptoms="Too low: washed-out gray sky. Too high: dim, flat subject. If brightness "
        "is what you want to change, pin brightness_target instead — it survives re-solves.",
    ),
}

_SOLVED_SUFFIX = " The app re-computes this from the image each run; leave your_value null unless you want to force it."


# ---------------------------------------------------------------------------
# Load + validate (mode (b) entry gate)
# ---------------------------------------------------------------------------


def load(path: str) -> dict:
    """Parse a recipe JSON. Raises RecipeError with a human-readable message."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        raise RecipeError(f"Recipe file not found: {path}") from None
    except OSError as e:
        raise RecipeError(f"Cannot read recipe file ({path}): {e}") from None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise RecipeError(f"Recipe is not valid JSON ({path}): {e.msg} at line {e.lineno}, column {e.colno}") from None
    if not isinstance(parsed, dict):
        raise RecipeError(f"Recipe must be a JSON object ({path}): got {type(parsed).__name__}")
    return parsed


def validate(doc: dict) -> list[str]:
    """Validate an edited recipe. Returns ALL problems as human-readable strings
    (prefs.py pattern) — the caller refuses to render on any error."""
    errors: list[str] = []

    fmt = doc.get("recipe_format")
    if isinstance(fmt, bool) or not isinstance(fmt, int):
        errors.append("Missing or non-integer 'recipe_format'.")
    elif fmt > RECIPE_FORMAT:
        errors.append(
            f"This recipe was written by a newer seestar (recipe_format {fmt}; this app "
            f"reads {RECIPE_FORMAT}). Update the app or re-emit the recipe."
        )

    adjustments = doc.get("adjustments")
    if "adjustments" not in doc:
        errors.append("Missing 'adjustments' list.")
        return errors
    if not isinstance(adjustments, list):
        errors.append("'adjustments' must be a list.")
        return errors

    seen: set[str] = set()
    for i, entry in enumerate(adjustments):
        if not isinstance(entry, dict) or not entry.get("adjustment"):
            errors.append(f"adjustments[{i}]: each entry must be an object with an 'adjustment' name.")
            continue
        name = entry["adjustment"]
        if name not in ADJUSTMENTS:
            errors.append(f"adjustments[{i}]: unknown adjustment '{name}'. Valid names: {', '.join(ADJUSTMENTS)}.")
            continue
        if name in seen:
            errors.append(f"adjustments[{i}]: duplicate entry for '{name}'.")
            continue
        seen.add(name)
        yv = entry.get("your_value")
        if yv is not None:
            errors.extend(_check_value(name, yv, i))
    return errors


def _check_value(name: str, yv, i: int) -> list[str]:
    a = ADJUSTMENTS[name]
    where = f"adjustments[{i}] ({name})"
    if a.vtype == "bool":
        if not isinstance(yv, bool):
            return [f"{where}: your_value must be true or false. ({a.direction})"]
        return []
    if isinstance(yv, bool) or not isinstance(yv, int | float):
        return [f"{where}: your_value must be a number. ({a.direction})"]
    v = float(yv)
    in_range = (a.lo < v < a.hi) if a.exclusive else (a.lo <= v <= a.hi)
    if not in_range:
        bounds = f"{a.lo}–{a.hi}" + (", exclusive" if a.exclusive else "")
        return [f"{where}: your_value {yv} is out of range ({bounds}). ({a.direction})"]
    return []


# ---------------------------------------------------------------------------
# Pins -> engine overrides
# ---------------------------------------------------------------------------


def pins(doc: dict) -> tuple[dict, set[str], dict]:
    """Translate non-null your_value entries to `run_target` overrides.

    Returns (overrides, pinned adjustment names, provenance {name: value}).
    An all-null recipe returns ({}, set(), {}) — mode (a) IS mode (b) with no pins.
    """
    overrides: dict = {}
    pinned: set[str] = set()
    prov: dict = {}
    for entry in doc.get("adjustments") or []:
        name = entry.get("adjustment")
        yv = entry.get("your_value")
        if name not in ADJUSTMENTS or yv is None:
            continue
        a = ADJUSTMENTS[name]
        val = bool(yv) if a.vtype == "bool" else float(yv)
        overrides[a.param] = val
        if name == "brightness_target":
            # the pin is the solver's FINAL target level: neutralize the profile/noisy
            # scale so the user gets exactly the number they wrote
            overrides["stretch_target_scale"] = 1.0
        pinned.add(name)
        prov[name] = val
    return overrides, pinned, prov


def overrides_from_pins(doc: dict) -> tuple[Overrides, frozenset[str], dict]:
    """Typed twin of `pins()`: same translation, returned as a typed `Overrides`
    (via `io.overrides_from_prefs_dict` -- every ADJUSTMENTS `param` is a real
    Overrides field name, verified) plus a `frozenset` of pinned adjustment names
    (tighter than `pins()`'s `set` -- the pin set is never mutated after this point).

    Returns (overrides, pinned adjustment names, provenance {name: value}).
    An all-null recipe returns (Overrides(), frozenset(), {}) -- mode (a) IS mode (b)
    with no pins.
    """
    overrides, pinned, prov = pins(doc)
    return overrides_from_prefs_dict(overrides), frozenset(pinned), prov


def solve_mode(pinned: set[str]) -> str:
    """How the stretch solve stage runs given pinned solved values:
    - "skip":     mtf_mid pinned -> no solve at all (bp recompute is gated separately)
    - "mtf_only": blackpoint pinned -> keep the pinned bp, solve mtf against it,
                  never the two-point bp/sky search
    - "normal":   no solved value pinned
    """
    if "mtf_mid" in pinned:
        return "skip"
    if "blackpoint" in pinned:
        return "mtf_only"
    return "normal"


# ---------------------------------------------------------------------------
# Emission (mode (a) — every run)
# ---------------------------------------------------------------------------

# adjustment name -> preferences.yaml key, for provenance in reasons
_PREF_KEY = {
    "denoise_strength": "noise_reduction",
    "saturation": "color",
    "brightness_target": "brightness",
    "star_prominence": "stars",
    "structure_boost": "structure",
}


def _applicable(decisions: Decisions) -> list[str]:
    """The adjustments that applied to THIS run, in registry (pipeline) order."""
    names = []
    for name in ADJUSTMENTS:
        if name == "star_prominence" and not decisions.star_forks:
            continue
        if name == "structure_boost" and not decisions.ghs_allowed:
            continue
        names.append(name)
    return names


def _current_value(name: str, decisions: Decisions, notes: dict, engine: EngineDefaults):
    a = ADJUSTMENTS[name]
    if name == "trail_removal":
        return bool(decisions.trail_removal_enabled)
    if name == "brightness_target":
        solved = (notes.get("stretch_solve") or {}).get("target")
        if solved is not None:
            return solved
        base = engine.stretch_targets.for_category(decisions.category).level
        return round(base * decisions.stretch_target_scale, 1)
    if name == "structure_boost":
        return decisions.ght is not None
    if name == "star_prominence":
        return decisions.star_reduce_k
    if name == "color_calibration":
        return bool(decisions.pcc)
    return getattr(decisions, a.param)


def _reason(name: str, decisions: Decisions, notes: dict, pinned: set[str]) -> str:
    if name in pinned:
        return "pinned by you (your_value honored)"
    if name == "trail_removal":
        # value=true means the DETECTION STAGE is enabled, not that a trail was found;
        # the reason carries the actual outcome so the two aren't conflated.
        tr = notes.get("trail_removal")
        if tr is None:
            return "trail detection did not run (stage disabled)"
        if tr.get("present"):
            return "detection ran: a trail was found and removed"
        return "detection ran: no trail found"
    if name == "crop_border_frac":
        cr = notes.get("crop") or {}
        if cr.get("summary"):
            return f"measured per edge: {cr['summary']}"
        return "measured per-edge automatic crop"
    if name == "blackpoint":
        bn = notes.get("blackpoint") or {}
        r = f"measured: factor {bn.get('factor')} x linear sky median {bn.get('linear_background_median')}"
        if bn.get("solved_for_sky"):
            r += f"; raised by the two-point sky solve to hold the sky at {bn['solved_for_sky'].get('sky')}"
        return r
    if name == "mtf_mid":
        ss = notes.get("stretch_solve") or {}
        if ss.get("solved_mtf") is not None:
            return f"solved: {ss.get('metric')} hit {ss.get('achieved')} (target {ss.get('target')})"
        return "engine fallback value (stretch solver off)"
    base = f"{decisions.category.value} default ({decisions.snr_class.value} SNR), profile={decisions.profile}"
    prov = notes.get("preferences") or {}
    pref_key = _PREF_KEY.get(name)
    if pref_key and pref_key in prov:
        base += f"; set by preferences: {prov[pref_key]}"
    return base


def build_doc(
    target: str,
    source_fits: str,
    filt: str | None,
    decisions: Decisions,
    notes: dict,
    engine: EngineDefaults,
    prior: dict | None = None,
    pinned: set[str] | None = None,
) -> dict:
    """Assemble the recipe document from the run's FINAL decisions + notes.

    `prior` (an earlier recipe doc) contributes ONLY your_value preservation —
    app values always come from this run. `context` is informational/drift-warning
    only; replay never branches on it (target-agnostic law)."""
    pinned = pinned or set()
    prior_yv: dict = {}
    for e in (prior or {}).get("adjustments") or []:
        if isinstance(e, dict) and e.get("adjustment"):
            prior_yv[e["adjustment"]] = e.get("your_value")

    entries = []
    for name in _applicable(decisions):
        a = ADJUSTMENTS[name]
        entries.append(
            {
                "adjustment": name,
                "value": _current_value(name, decisions, notes, engine),
                "your_value": prior_yv.get(name),
                "tool": a.tool,
                "note": a.note + (_SOLVED_SUFFIX if a.kind == "solved" else ""),
                "reason": _reason(name, decisions, notes, pinned),
                "symptoms_if_wrong": a.symptoms,
            }
        )
    return {
        "recipe_format": RECIPE_FORMAT,
        "engine_version": __version__,
        "generated": datetime.now().isoformat(timespec="seconds"),
        "target": target,
        "source_fits": os.path.basename(source_fits),
        "context": {
            "category": decisions.category.value,
            "snr_class": decisions.snr_class.value,
            "filter": filt,
            "profile": decisions.profile,
        },
        "adjustments": entries,
    }


def emit(
    output_dir: str,
    target: str,
    source_fits: str,
    filt: str | None,
    decisions: Decisions,
    notes: dict,
    engine: EngineDefaults,
    prior: dict | None = None,
    pinned: set[str] | None = None,
    *,
    filename: str | None = None,
    seed_your_values_from: str | None = None,
) -> str:
    """Write <output_dir>/<filename> (default "<target>_recipe.json") and return its
    path.

    Without an explicit prior (a plain mode-(a) run), an existing on-disk recipe at
    that same path is read best-effort purely to PRESERVE your_value — never to
    honor it (pins apply only via --recipe; no auto-discovery). If that path doesn't
    exist but `seed_your_values_from` does (an uncertified-namespace emission
    seeding pins from the certified recipe -- spec rev 6 T6), that file is read
    instead, under the same best-effort semantics."""
    path = os.path.join(output_dir, filename or f"{target}_recipe.json")
    if prior is None:
        source_path = path if os.path.exists(path) else seed_your_values_from
        if source_path and os.path.exists(source_path):
            try:
                with open(source_path, encoding="utf-8") as f:
                    prior = json.load(f)
            except (OSError, json.JSONDecodeError):
                prior = None
    doc = build_doc(target, source_fits, filt, decisions, notes, engine, prior=prior, pinned=pinned)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
    return path


def recipe_is_unedited(path: str) -> bool:
    """True iff the recipe parses and carries no user pin (every your_value null).
    Missing or unparseable -> False (conservative: never certify deletability)."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    return all(e.get("your_value") is None for e in doc.get("adjustments") or [])


# ---------------------------------------------------------------------------
# Replay reporting (what changed vs the prior render)
# ---------------------------------------------------------------------------


def context_warnings(doc: dict, category: str, profile: str, filt: str | None) -> list[str]:
    """Warn-and-proceed checks: engine version mismatch and context drift.
    Measurement wins; a mismatch that silently skips is the QA silent-gate lesson."""
    w: list[str] = []
    ev = doc.get("engine_version")
    if ev and ev != __version__:
        w.append(f"recipe written by engine {ev}; running {__version__} — app values re-derived, pins honored")
    ctx = doc.get("context") or {}
    for key, now in (("category", category), ("profile", profile), ("filter", filt)):
        was = ctx.get(key)
        if was and now and was != now:
            w.append(f"recipe context {key} was '{was}', this run is '{now}' — measurement wins")
    return w


def change_report(
    prior: dict,
    decisions: Decisions,
    notes: dict,
    engine: EngineDefaults,
    pinned: set[str] | frozenset[str],
    warnings: list[str],
) -> dict:
    """Per-adjustment diff of this run vs the input recipe's recorded values.
    Every pinned adjustment that did not apply gets an EXPLICIT warning.

    `warnings` is both MUTATED IN PLACE (appended to) and returned inside the
    result dict — callers must pass a fresh list per call, not a shared/reused one."""
    prior_vals = {e.get("adjustment"): e.get("value") for e in (prior.get("adjustments") or []) if isinstance(e, dict)}
    changes = []
    applied = set()
    for name in _applicable(decisions):
        applied.add(name)
        now = _current_value(name, decisions, notes, engine)
        was = prior_vals.get(name)
        changes.append(
            {
                "adjustment": name,
                "prior": was,
                "now": now,
                "source": "pinned" if name in pinned else "re-derived",
                "changed": was != now,
            }
        )
    for name in sorted(pinned - applied):
        warnings.append(
            f"pinned adjustment '{name}' did not apply to this render "
            "(not part of this image's pipeline) — the pin had no effect"
        )
    return {"pinned": sorted(pinned), "changes": changes, "warnings": warnings}
