"""User preferences — plain-English tuning layer on top of the engine.

``ALL_PRESETS`` is the single source of truth for every user-facing parameter.
The template generator, validator, error messages, and provenance log all derive
from it — nothing else hardcodes preset labels or their numeric equivalents.

Two scale modes
---------------
relative  the preset value is a *multiplier* on the engine's per-category default.
          Used for color and brightness, where categories have different baselines
          (nebula saturation 0.60 vs starfield 0.85 — "subtle" should feel subdued
          relative to normal for *that* type, not a fixed absolute).
absolute  the preset value is used directly.

Scoping
-------
A preferences.yaml can live in two places:

    <output_root>/preferences.yaml          global  — all targets
    <output_root>/<target>/preferences.yaml target  — one target only

Within the global file, a ``nebula:``, ``galaxy:``, or ``starfield:`` section
overrides bare global settings for that category only. Target-level settings
win over everything. Priority (last wins):

    global bare  <  global category section  <  target-level
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import yaml

from .core.types import Category, EngineDefaults, Overrides

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Preset:
    presets: dict[str, float]  # label -> numeric value (multiplier or absolute)
    default: str  # which label is the default
    direction: str  # "higher = X" — one-liner for the template header
    description: str  # plain-English one-liner
    scope: str  # "all" | "extended" | "galaxy"
    scale: str  # "relative" | "absolute"


# style maps to profile name strings — handled separately from numeric presets.
STYLE_MAP: dict[str, str] = {
    "share": "showcase",
    "true-to-life": "natural",
    "record": "documentation",
    "print": "print",
}
STYLE_DEFAULT = "share"


ALL_PRESETS: dict[str, Preset] = {
    "color": Preset(
        presets={"vivid": 1.5, "standard": 1.0, "subtle": 0.6, "muted": 0.3},
        default="standard",
        direction="higher = more colorful",
        description="How colorful / saturated the image is",
        scope="all",
        scale="relative",
    ),
    "brightness": Preset(
        presets={"bright": 1.25, "medium": 1.0, "dim": 0.75},
        default="medium",
        direction="higher = brighter subject",
        description="How bright the subject appears overall",
        scope="all",
        scale="relative",
    ),
    "noise_reduction": Preset(
        presets={"light": 0.3, "balanced": 0.6, "strong": 0.8},
        default="balanced",
        direction="higher = more smoothing",
        description="How much noise reduction is applied",
        scope="all",
        scale="absolute",
    ),
    "stars": Preset(
        presets={"prominent": 0.8, "reduced": 0.45, "minimal": 0.15},
        default="reduced",
        direction="higher = more stars visible",
        description="How prominent stars are relative to the subject",
        scope="extended",  # nebula + galaxy only; ignored for star fields / clusters
        scale="absolute",
    ),
    "structure": Preset(
        presets={"enhanced": 1.0, "standard": 0.0},
        default="enhanced",
        direction="enhanced reveals faint detail; standard uses the base stretch only",
        description="How aggressively faint structure is revealed (nebula envelope, galaxy dust lane, cluster halo)",
        scope="all",
        scale="absolute",
    ),
    "versions": Preset(
        presets={"auto": 1.0, "single": 0.0},
        default="auto",
        direction="auto lets the app decide; single always produces one image",
        description="How many versions of the image to produce",
        scope="all",
        scale="absolute",
    ),
    "closeup": Preset(
        presets={"auto": 1.0, "off": 0.0},
        default="auto",
        direction="auto produces a closeup for small galaxies; off skips it",
        description="Whether to also produce a zoomed-in version (galaxies only)",
        scope="galaxy",
        scale="absolute",
    ),
}

# Ordered for template generation (most commonly changed first)
_TEMPLATE_ORDER = ["style", "color", "brightness", "noise_reduction", "stars", "structure", "versions", "closeup"]


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


def load_preferences(path: str) -> dict:
    """Read a preferences.yaml. Returns {} if the file does not exist."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def merge_preferences(
    global_prefs: dict,
    target_prefs: dict,
    category: str,
) -> dict:
    """Merge global and target preferences, applying category sections.

    Priority (highest last wins):
        global bare  <  global category section  <  target-level bare
    """
    merged: dict = {}
    _CATS = {"nebula", "galaxy", "starfield"}

    # 1. global bare settings
    for k, v in global_prefs.items():
        if k not in _CATS:
            merged[k] = v

    # 2. global category section (e.g. ``nebula: { color: vivid }``)
    cat_section = global_prefs.get(category) or {}
    merged.update(cat_section)

    # 3. target-level bare settings (no category sections at target level)
    for k, v in target_prefs.items():
        if k not in _CATS:
            merged[k] = v

    return merged


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------


def resolve_value(raw: str | float | int, param: str) -> tuple[float, str]:
    """Resolve a raw preference value to ``(numeric_value, provenance_label)``.

    Accepts a label (``'balanced'``) or a number (``0.4``).

    For *relative* params (color, brightness) the numeric value is a multiplier
    on the engine's per-category default. For *absolute* params it is used directly.

    Raises ``ValueError`` with a human-readable message on bad input.
    """
    if param == "style":
        label = str(raw)
        if label not in STYLE_MAP:
            valid = ", ".join(STYLE_MAP)
            raise ValueError(f"Invalid style '{raw}'. Valid options: {valid}.")
        return 0.0, label  # float unused for style; label carries the value

    if param not in ALL_PRESETS:
        raise ValueError(f"Unknown preference '{param}'.")

    p = ALL_PRESETS[param]

    if isinstance(raw, str):
        if raw not in p.presets:
            valid = "  ".join(f"{k} ({v})" for k, v in p.presets.items())
            raise ValueError(f"Invalid value '{raw}' for {param}. Valid labels: {valid}. Or use a number.")
        return p.presets[raw], raw

    # numeric escape hatch
    try:
        val = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid value '{raw}' for {param}. Expected a label or a number.") from exc
    # find closest label for provenance
    closest = min(p.presets, key=lambda k: abs(p.presets[k] - val))
    return val, f"{val} (~{closest})"


def validate_preferences(raw_prefs: dict) -> list[str]:
    """Validate all keys in a raw preferences dict. Returns a list of error strings."""
    errors: list[str] = []
    _CATS = {"nebula", "galaxy", "starfield"}
    known = set(ALL_PRESETS) | {"style"} | _CATS

    for k, v in raw_prefs.items():
        if k in _CATS:
            # validate nested category section
            for ck, cv in (v or {}).items():
                try:
                    resolve_value(cv, ck)
                except ValueError as e:
                    errors.append(f"{k}.{ck}: {e}")
        elif k not in known:
            errors.append(f"Unknown preference '{k}'. Known preferences: {', '.join(sorted(known - _CATS))}.")
        else:
            try:
                resolve_value(v, k)
            except ValueError as e:
                errors.append(f"{k}: {e}")

    return errors


# ---------------------------------------------------------------------------
# Translate to engine overrides
# ---------------------------------------------------------------------------


def prefs_to_overrides(
    raw_prefs: dict,
    category: str,
    engine_cfg: dict,
) -> tuple[dict, dict]:
    """Translate merged raw preferences to engine parameter overrides.

    Returns ``(overrides, provenance)`` where:
    - ``overrides``   dict to pass into ``run_target``'s existing overrides mechanism
    - ``provenance``  dict of ``"param: label (value)"`` strings for logging
    """
    overrides: dict = {}
    provenance: dict = {}
    _CATS = {"nebula", "galaxy", "starfield"}

    for param, raw in raw_prefs.items():
        if param in _CATS:
            continue  # already merged by merge_preferences

        # --- style ---
        if param == "style":
            _, label = resolve_value(raw, "style")
            overrides["profile"] = STYLE_MAP[label]
            provenance["style"] = f"{label} -> profile={STYLE_MAP[label]}"
            continue

        if param not in ALL_PRESETS:
            continue  # already caught by validate_preferences

        p = ALL_PRESETS[param]
        val, label = resolve_value(raw, param)

        # scope gate
        if p.scope == "extended" and category not in ("nebula", "galaxy"):
            continue
        if p.scope == "galaxy" and category != "galaxy":
            continue

        # --- color ---
        if param == "color":
            base = (engine_cfg.get("saturation") or {}).get(category, 0.6)
            overrides["saturation"] = round(base * val, 3)

        # --- brightness ---
        elif param == "brightness":
            tgt = (engine_cfg.get("stretch") or {}).get("target", {}).get(category, {})
            base_level = tgt.get("level", 72)
            overrides["stretch_target_level"] = round(base_level * val, 1)

        # --- noise_reduction ---
        elif param == "noise_reduction":
            overrides["denoise_strength"] = val

        # --- stars ---
        elif param == "stars":
            overrides["star_reduce_k"] = val

        # --- structure ---
        elif param == "structure":
            overrides["structure_enabled"] = val >= 0.5

        # --- versions ---
        elif param == "versions":
            if val < 0.5:  # "single"
                overrides["star_forks"] = False

        # --- closeup ---
        elif param == "closeup":
            if val < 0.5:  # "off"
                overrides["closeup_enabled"] = False

        provenance[param] = f"{label} ({val})" if not str(label).startswith(str(val)) else str(label)

    return overrides, provenance


# ---------------------------------------------------------------------------
# Discover and load for a run
# ---------------------------------------------------------------------------


def _discover_merge_and_validate(output_dir: str, category: str) -> dict:
    """Discover <output_dir>/../preferences.yaml (global) and <output_dir>/preferences.yaml
    (per-target), validate both, and merge them for `category`. Raises ``ValueError``
    (with every problem listed) if either file contains invalid preferences.

    Shared by the dict-based `load_for_run` and the typed `overrides_for_run` so both
    stay byte-identical on file discovery/validation/merge -- only the final
    translation step (`prefs_to_overrides` vs `prefs_to_overrides_typed`) differs.
    """
    output_root = os.path.dirname(os.path.abspath(output_dir))
    global_path = os.path.join(output_root, "preferences.yaml")
    target_path = os.path.join(output_dir, "preferences.yaml")

    global_prefs = load_preferences(global_path)
    target_prefs = load_preferences(target_path)

    # validate both files before merging
    errors: list[str] = []
    for label, prefs, path in (
        ("global", global_prefs, global_path),
        ("target", target_prefs, target_path),
    ):
        errs = validate_preferences(prefs)
        for e in errs:
            errors.append(f"{label} preferences ({path}): {e}")

    if errors:
        raise ValueError("Preferences errors:\n" + "\n".join(f"  • {e}" for e in errors))

    return merge_preferences(global_prefs, target_prefs, category)


def load_for_run(output_dir: str, category: str, engine_cfg: dict) -> tuple[dict, dict]:
    """Auto-discover, merge, validate, and translate preferences for one run.

    Looks for:
        <output_dir>/../preferences.yaml   (global)
        <output_dir>/preferences.yaml      (per-target)

    Returns ``(overrides, provenance)``. Raises ``ValueError`` if a file contains
    invalid preferences.
    """
    merged = _discover_merge_and_validate(output_dir, category)
    return prefs_to_overrides(merged, category, engine_cfg)


# ---------------------------------------------------------------------------
# Typed preference translation used by the planner path.
# ---------------------------------------------------------------------------


def prefs_to_overrides_typed(
    raw_prefs: dict,
    category: Category,
    engine: EngineDefaults,
) -> tuple[Overrides, dict]:
    """Typed twin of `prefs_to_overrides`: same per-preset logic, but reads engine
    defaults from a typed `EngineDefaults` and returns a typed `Overrides` directly
    (no params-dict key ever exists). `prefs_to_overrides` itself stays as the
    dict-based boundary `tests/test_prefs.py` exercises directly -- keep both in
    sync by hand if a preset's translation logic ever changes.
    """
    kwargs: dict = {}
    provenance: dict = {}
    _CATS = {"nebula", "galaxy", "starfield"}

    for param, raw in raw_prefs.items():
        if param in _CATS:
            continue  # already merged by merge_preferences

        # --- style ---
        if param == "style":
            _, label = resolve_value(raw, "style")
            kwargs["profile"] = STYLE_MAP[label]
            provenance["style"] = f"{label} -> profile={STYLE_MAP[label]}"
            continue

        if param not in ALL_PRESETS:
            continue  # already caught by validate_preferences

        p = ALL_PRESETS[param]
        val, label = resolve_value(raw, param)

        # scope gate
        if p.scope == "extended" and category not in (Category.NEBULA, Category.GALAXY):
            continue
        if p.scope == "galaxy" and category is not Category.GALAXY:
            continue

        # --- color ---
        if param == "color":
            base = engine.saturation.for_category(category)
            kwargs["saturation"] = round(base * val, 3)

        # --- brightness ---
        elif param == "brightness":
            base_level = engine.stretch_targets.for_category(category).level
            kwargs["stretch_target_level"] = round(base_level * val, 1)

        # --- noise_reduction ---
        elif param == "noise_reduction":
            kwargs["denoise_strength"] = val

        # --- stars ---
        elif param == "stars":
            kwargs["star_reduce_k"] = val

        # --- structure ---
        elif param == "structure":
            kwargs["structure_enabled"] = val >= 0.5

        # --- versions ---
        elif param == "versions":
            if val < 0.5:  # "single"
                kwargs["star_forks"] = False

        # --- closeup ---
        elif param == "closeup":
            if val < 0.5:  # "off"
                kwargs["closeup_enabled"] = False

        provenance[param] = f"{label} ({val})" if not str(label).startswith(str(val)) else str(label)

    return Overrides(**kwargs), provenance


def overrides_for_run(output_dir: str, category: Category, engine: EngineDefaults) -> tuple[Overrides, dict]:
    """Typed twin of `load_for_run`: discovers/merges/validates preferences.yaml
    exactly like `load_for_run` (shared helper), then translates directly to a
    typed `Overrides` -- ONE call for orchestrate.py's prefs layer, no
    dict-override intermediate. Returns ``(overrides, provenance)``; raises
    ``ValueError`` on invalid preferences, exactly like `load_for_run`.
    """
    merged = _discover_merge_and_validate(output_dir, category.value)
    return prefs_to_overrides_typed(merged, category, engine)


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def explain(param: str | None = None) -> str:
    """Return a human-readable explanation of one or all preferences.
    Always current — generated from ALL_PRESETS, never hand-written.
    """
    lines: list[str] = []

    def _explain_one(name: str, p: Preset) -> None:
        lines.append(f"\n{name}")
        lines.append(f"  {p.description}")
        if p.direction:
            lines.append(f"  ({p.direction})")
        lines.append(f"  Scope: {p.scope}")
        lines.append(f"  Presets (default: {p.default}):")
        for label, val in p.presets.items():
            marker = " ← default" if label == p.default else ""
            lines.append(f"    {label:<14} {val}{marker}")
        lines.append(
            f"  Numeric escape hatch: {name}: <number>  "
            f"({'multiplier on engine default' if p.scale == 'relative' else '0.0–1.0'})"
        )

    if param == "style":
        lines.append("\nstyle")
        lines.append("  What the image is for")
        lines.append(f"  Options (default: {STYLE_DEFAULT}):")
        for label, profile in STYLE_MAP.items():
            marker = " ← default" if label == STYLE_DEFAULT else ""
            lines.append(f"    {label:<16} -> profile={profile}{marker}")
        return "\n".join(lines)

    if param is not None:
        if param not in ALL_PRESETS:
            return f"Unknown preference '{param}'. Known: {', '.join(_TEMPLATE_ORDER)}."
        _explain_one(param, ALL_PRESETS[param])
        return "\n".join(lines)

    # all
    lines.append("Seestar preferences — current preset values")
    lines.append("=" * 44)
    _explain_one(
        "style",
        Preset(  # synthesize a Preset-like entry for style
            presets={k: 0.0 for k in STYLE_MAP},
            default=STYLE_DEFAULT,
            direction="",
            description="What the image is for",
            scope="all",
            scale="absolute",
        ),
    )
    lines[-1] = ""  # remove last numeric line for style
    lines.append("  Presets:")
    for label, profile in STYLE_MAP.items():
        marker = " ← default" if label == STYLE_DEFAULT else ""
        lines.append(f"    {label:<16} -> profile={profile}{marker}")

    for name in _TEMPLATE_ORDER:
        if name == "style":
            continue
        _explain_one(name, ALL_PRESETS[name])

    return "\n".join(lines)


def generate_template() -> str:
    """Generate a fully commented preferences.yaml from ALL_PRESETS.
    The comments (including numeric values) are generated — never hand-written.
    """
    lines: list[str] = []
    lines += [
        "# ─────────────────────────────────────────────────────────────",
        "# SEESTAR PREFERENCES",
        "# ─────────────────────────────────────────────────────────────",
        "# Place this file in:",
        "#   <output folder>/preferences.yaml          → applies to all photos",
        "#   <output folder>/<target>/preferences.yaml → applies to one target only",
        "#",
        "# For per-category settings, add a nebula:, galaxy:, or starfield: section",
        "# (see the example at the bottom of this file).",
        "#",
        "# All settings are optional — omit anything you're happy with.",
        "# ─────────────────────────────────────────────────────────────",
        "",
    ]

    # style
    lines += [
        "# PURPOSE — what is this image for?",
    ]
    for label, profile in STYLE_MAP.items():
        marker = "  (default)" if label == STYLE_DEFAULT else ""
        lines.append(f"#   {label:<16} → {profile}{marker}")
    lines.append(f"style: {STYLE_DEFAULT}")
    lines.append("")

    # numeric presets
    for name in _TEMPLATE_ORDER:
        if name == "style":
            continue
        p = ALL_PRESETS[name]
        lines.append(f"# {p.description.upper()}")
        if p.direction:
            lines.append(f"# {p.direction}")
        if p.scale == "relative":
            lines.append("# Numeric escape hatch: a multiplier on the engine's default for this category.")
        else:
            lines.append("# Numeric escape hatch: a number from 0.0 to 1.0.")
        for label, val in p.presets.items():
            marker = "  (default)" if label == p.default else ""
            lines.append(f"#   {label:<12} ({val}){marker}")
        lines.append(f"{name}: {p.default}")
        lines.append("")

    # category section example
    lines += [
        "",
        "# ─────────────────────────────────────────────────────────────",
        "# PER-CATEGORY OVERRIDES (optional)",
        "# Settings here apply only to that type of target, on top of",
        "# the global settings above.",
        "# ─────────────────────────────────────────────────────────────",
        "# nebula:",
        "#   color: vivid",
        "#   stars: minimal",
        "#",
        "# galaxy:",
        "#   brightness: bright",
        "#   noise_reduction: strong",
        "#",
        "# starfield:",
        "#   color: vivid",
    ]

    return "\n".join(lines) + "\n"
