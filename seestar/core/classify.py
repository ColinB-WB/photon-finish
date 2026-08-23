"""Turn measured FITS properties into a content classification.

The function is target-agnostic by construction: inputs include the filter,
frame count, measurements, thresholds, and optional extended-signal value, but no
object name.
"""

from __future__ import annotations

from .types import (
    Category,
    CategorySource,
    Classification,
    ClassifyThresholds,
    FilterKind,
    Measurements,
    SNRClass,
)


def classify(
    filter_kind: FilterKind,
    frames: int,
    measurements: Measurements,
    thresholds: ClassifyThresholds,
    e_value: float | None = None,
) -> Classification:
    noisy = frames < thresholds.noisy_frames_below or measurements.bgnoise > thresholds.noisy_bgnoise_above

    if e_value is not None:
        extended = e_value >= thresholds.extended_signal_threshold
    else:
        extended = filter_kind is FilterKind.LP  # provisional until StarNet E exists

    if filter_kind is FilterKind.LP:
        category = Category.NEBULA
    elif extended:
        category = Category.GALAXY
    else:
        category = Category.STARFIELD

    return Classification(
        category=category,
        snr_class=SNRClass.NOISY if noisy else SNRClass.CLEAN,
        filter_kind=filter_kind,
        extended_fraction=e_value,
        extended_provisional=e_value is None,
        source=CategorySource.E_MEASURE if e_value is not None else CategorySource.FILTER_DEFAULT,
    )
