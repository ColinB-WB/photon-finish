"""User-facing progress labels for pipeline stages.

Core planning and execution use `StageKey`; the CLI/GUI call `present()` to turn
that key into a novice-friendly label and the tool name shown beside it. An empty
tool string means the stage is built into this app.
"""

from __future__ import annotations

from .core.types import StageKey

STAGE_PRESENTATION: dict[StageKey, tuple[str, str]] = {
    StageKey.ANALYZE: ("Analyzing image", ""),
    StageKey.PRIOR: ("Identifying object (catalog)", "SIMBAD (online)"),
    StageKey.PREPARE_SOURCE: ("Preparing working copy", ""),
    StageKey.COMBINE_SESSIONS: ("Combining sessions", "Siril"),
    StageKey.CROP: ("Cropping edges", "Siril"),
    StageKey.TRAIL: ("Checking for satellite trails", ""),
    StageKey.BACKGROUND_EXTRACTION: ("Removing light-pollution gradient", "GraXpert"),
    StageKey.EXTENDED_PROBE: ("Classifying galaxy vs cluster", "StarNet2"),
    StageKey.DENOISE: ("Reducing noise", "GraXpert"),
    StageKey.COLOR_CALIBRATION: ("Balancing color", "Siril"),
    StageKey.STRETCH_SOLVE: ("Calibrating brightness", "Siril"),
    StageKey.STRETCH: ("Stretching & saturating", "Siril"),
    StageKey.STAR_SEPARATION: ("Removing stars", "StarNet2"),
    StageKey.STAR_BLEND: ("Blending star-reduced image", "Siril"),
    StageKey.CLOSEUP: ("Making galaxy close-up", "StarNet2"),
    StageKey.EXPORT: ("Saving images & metadata", "Siril + exiftool"),
    StageKey.METADATA: ("Saving images & metadata", "exiftool"),
    StageKey.ANNOTATE: ("Annotating documentation copy", ""),
}


def present(key: StageKey) -> tuple[str, str]:
    return STAGE_PRESENTATION[key]
