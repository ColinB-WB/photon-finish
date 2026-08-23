"""Frozen dataclasses and enums shared by the pure core.

Core values are immutable, explicit, and validated where useful. Enums include
UNKNOWN variants rather than using `None` for unknown measured facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import ClassVar


class Category(Enum):
    NEBULA = "nebula"
    GALAXY = "galaxy"
    STARFIELD = "starfield"
    UNKNOWN = "unknown"


class SNRClass(Enum):
    CLEAN = "clean"
    NOISY = "noisy"


class FilterKind(Enum):
    LP = "LP"
    IRCUT = "IRCUT"
    UNKNOWN = "unknown"

    @classmethod
    def from_raw(cls, raw: str | None) -> FilterKind:
        if raw is None:
            return cls.UNKNOWN
        s = str(raw).strip().upper()
        if s == "LP":
            return cls.LP
        if s == "IRCUT":
            return cls.IRCUT
        return cls.UNKNOWN


class CategorySource(Enum):
    FILTER_DEFAULT = "filter_default"  # provisional: filter alone decided
    E_MEASURE = "e_measure"  # measured extended-signal fraction decided
    CATALOG_PRIOR = "catalog_prior"  # WCS catalog prior decided


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ValueError(msg)


@dataclass(frozen=True)
class ChannelStats:
    """Per-channel statistics in the canonical unit-float space [0, 1]."""

    name: str
    median: float
    sigma: float
    max_value: float
    sat_frac: float

    def __post_init__(self) -> None:
        _require(0.0 <= self.median <= 1.0, f"median {self.median} outside [0,1] canonical space")
        _require(self.sigma >= 0.0, f"sigma {self.sigma} negative")
        _require(0.0 <= self.max_value <= 2.0, f"max_value {self.max_value} outside [0,2]")
        _require(0.0 <= self.sat_frac <= 1.0, f"sat_frac {self.sat_frac} outside [0,1]")


@dataclass(frozen=True)
class Measurements:
    """Linear-image statistics, ALWAYS in the one canonical space (unit-float).
    Normalization happens once, in seestar/measure.py at the io boundary; raw
    16-bit values never enter the core (the black-point scale-bug killer)."""

    channels: tuple[ChannelStats, ...]
    background: float
    bgnoise: float
    max_value: float
    dims: tuple[int, int]  # (width, height) in px; (0, 0) = unknown
    pixscale_arcsec: float | None
    dynamic_range: float | None

    def __post_init__(self) -> None:
        _require(0.0 <= self.background <= 1.0, f"background {self.background} outside [0,1]")
        _require(self.bgnoise >= 0.0, f"bgnoise {self.bgnoise} negative")
        _require(0.0 <= self.max_value <= 2.0, f"max_value {self.max_value} outside [0,2]")
        _require(len(self.dims) == 2 and all(d >= 0 for d in self.dims), f"bad dims {self.dims}")
        _require(
            self.pixscale_arcsec is None or self.pixscale_arcsec > 0,
            f"pixscale_arcsec {self.pixscale_arcsec} not positive",
        )


@dataclass(frozen=True)
class ClassifyThresholds:
    """Defaults mirror config.yaml engine.classify."""

    extended_signal_threshold: float = 0.03
    noisy_frames_below: int = 45
    noisy_bgnoise_above: float = 0.0025


@dataclass(frozen=True)
class Classification:
    category: Category
    snr_class: SNRClass
    filter_kind: FilterKind
    extended_fraction: float | None  # measured E; None = not yet measured
    extended_provisional: bool  # True while category rests on filter alone
    source: CategorySource

    def __post_init__(self) -> None:
        _require(
            self.extended_fraction is None or 0.0 <= self.extended_fraction <= 1.0,
            f"extended_fraction {self.extended_fraction} outside [0,1]",
        )


class TargetMetric(Enum):
    BODY_P90 = "body_p90"
    SKY_MEDIAN = "sky_median"


@dataclass(frozen=True)
class PerCategory:
    nebula: float
    galaxy: float
    starfield: float

    def for_category(self, c: Category) -> float:
        if c is Category.NEBULA:
            return self.nebula
        if c is Category.GALAXY:
            return self.galaxy
        if c is Category.STARFIELD:
            return self.starfield
        raise ValueError("no engine default for Category.UNKNOWN - refuse, never silently skip")


@dataclass(frozen=True)
class StretchTarget:
    metric: TargetMetric
    level: float
    sky_ceiling: float | None


@dataclass(frozen=True)
class StretchTargets:
    nebula: StretchTarget
    galaxy: StretchTarget
    starfield: StretchTarget

    def for_category(self, c: Category) -> StretchTarget:
        if c is Category.NEBULA:
            return self.nebula
        if c is Category.GALAXY:
            return self.galaxy
        if c is Category.STARFIELD:
            return self.starfield
        raise ValueError("no stretch target for Category.UNKNOWN - refuse, never silently skip")


@dataclass(frozen=True)
class SolverBounds:
    lo: float = 0.02
    hi: float = 0.25
    tol: float = 0.5
    max_iter: int = 10

    def __post_init__(self) -> None:
        _require(0.0 < self.lo < self.hi < 1.0, f"solver bounds lo={self.lo} hi={self.hi} invalid")
        _require(self.tol > 0 and self.max_iter > 0, "solver tol/max_iter must be positive")


@dataclass(frozen=True)
class GhsParams:
    d: float
    b: float
    sp: float
    lp: float
    hp: float
    unsharp_sigma: float = 0.0
    unsharp_amount: float = 0.0
    closeup: bool = False

    def __post_init__(self) -> None:
        _require(
            0.0 <= self.lp <= self.sp <= self.hp <= 1.0,
            f"GHS ordering LP<=SP<=HP violated: {self.lp}/{self.sp}/{self.hp}",
        )


@dataclass(frozen=True)
class ClusterConfig:
    concentration_min: float
    block: int


@dataclass(frozen=True)
class FramingConfig:
    enabled: bool
    fill_fraction: float
    k_extent: float
    subject_threshold_sigma: float
    trigger_max_frac: float


@dataclass(frozen=True)
class RecoveryDefaults:
    """Bounded chroma-recovery search (Phase 3B). `saturation_bracket` are relative
    multipliers on the DECIDED per-image saturation, applied highest-first, so the
    strongest candidate that still clears the pegged_chroma ceiling wins.
    Category-agnostic: the bracket means the same thing for nebula 0.60 / starfield
    0.55 / galaxy 0.60. len(bracket) is the attempt cap."""

    saturation_bracket: tuple[float, ...]
    body_saturation_ceiling: PerCategory | None = None


@dataclass(frozen=True)
class PccBounds:
    """Broad, deterministic acceptance bounds for a tool-reported PCC solution
    (Diagnosis 4, spec 2026-08-12). These reject explicit tool-flagged
    imprecision, too-few calibration stars, and implausible white-balance
    coefficients. Validated against a real healthy Siril 1.4.4 solution (S50
    broadband field, 330 stars, factors 0.471/0.713/1.000, captured 2026-08-14)
    and the rejected S50 M82 imprecise solution; never tuned to make a specific
    target look better."""

    min_stars: int = 25
    coeff_min: float = 0.2
    coeff_max: float = 5.0
    max_ratio: float = 4.0  # max/min applied coefficient ratio


@dataclass(frozen=True)
class EngineDefaults:
    """Typed mirror of config.yaml `engine:` (built once by io.engine_defaults_from_cfg)."""

    # None = measured per-edge automatic crop (the default). An explicit value is
    # the legacy forced symmetric crop -- honored for config/recipe compatibility.
    crop_border_frac: float | None
    # hard ceiling on any AUTOMATICALLY measured per-edge trim (fraction of that
    # dimension): a detector claiming more is refused, never silently applied.
    crop_max_auto_frac: float
    bge_smoothing: float
    bge_correction: str
    denoise: PerCategory
    denoise_noisy_bonus: float
    asinh_d: PerCategory
    blackpoint_factor: float
    mtf_mid: PerCategory
    noisy_mtf_delta: float
    noisy_level_scale: float
    stretch_targets: StretchTargets
    solve_stretch: bool
    solver: SolverBounds
    saturation: PerCategory
    galaxy_treatment: GhsParams
    nebula_treatment: GhsParams | None  # None = disabled
    cluster: ClusterConfig
    cluster_treatment: GhsParams | None  # None = disabled
    sharpening_undersampled_arcsec: float
    sharpening_unsharp_cap: float
    print_black_lift: float
    star_reduce_k: float
    conservative_star_hero: bool
    classify: ClassifyThresholds
    e_gate: bool
    catalog_prior: bool
    combine_sessions: bool
    framing: FramingConfig
    trail_removal_enabled: bool
    recovery: RecoveryDefaults
    # Diagnosis 4: acceptance bounds for tool-reported PCC evidence. Default keeps
    # the field order/old constructors stable (mirrors the SolveProvenance pattern).
    pcc: PccBounds = PccBounds()


@dataclass(frozen=True)
class SharpeningCap:
    pixscale_arcsec: float
    cap: float


@dataclass(frozen=True)
class Overrides:
    """Sparse typed overrides (preferences / recipe pins / caller), applied in
    precedence order: engine < profile < preferences < pins < caller."""

    profile: str | None = None
    saturation: float | None = None
    stretch_target_level: float | None = None
    denoise_strength: float | None = None
    star_reduce_k: float | None = None
    structure_enabled: bool | None = None
    star_forks: bool | None = None
    closeup_enabled: bool | None = None
    trail_removal_enabled: bool | None = None
    crop_border_frac: float | None = None
    bge_smoothing: float | None = None
    pcc: bool | None = None
    asinh_d: float | None = None
    blackpoint: float | None = None
    mtf_mid: float | None = None
    stretch_target_scale: float | None = None


@dataclass(frozen=True)
class Decisions:
    """Fully computed processing decisions for one target.

    `core.policy.compute_decisions` returns this value after applying engine
    defaults, profile overlays, preferences, recipe pins, and caller overrides.
    All fields are normalized and validated before they enter planning.
    """

    category: Category
    snr_class: SNRClass
    profile: str
    crop_border_frac: float | None  # None = measured per-edge auto crop (default)
    bge_smoothing: float
    bge_correction: str
    denoise_strength: float
    pcc: bool
    asinh_d: float
    blackpoint: float
    blackpoint_scale: float
    mtf_mid: float
    stretch_target_scale: float
    saturation: float
    star_forks: bool
    ghs_allowed: bool
    ght: GhsParams | None
    sky_solve: bool
    annotate: bool
    black_lift: float
    sharpening_capped: SharpeningCap | None
    # Fields below carry defaults so the field order stays stable. They are
    # user-controllable knobs that participate in the same override precedence
    # as the required decision fields above.
    stretch_target_level: float | None = None  # None = engine per-category level
    structure_enabled: bool = True  # False strips the GHS treatment (ght -> None)
    closeup_enabled: bool = True  # False suppresses the galaxy closeup fork
    trail_removal_enabled: bool = True  # False skips the trail scan
    star_reduce_k: float = 0.45  # screen-blend strength for the star-reduced hero
    # Phase 3A conservative star policy: when True (and star_forks is on), the
    # recommended hero is the stars-on render and star-reduced/starless are
    # emitted only as variants. False restores the legacy star-reduced hero.
    conservative_star_hero: bool = True

    def __post_init__(self) -> None:
        _require(0 < self.denoise_strength <= 1.5, f"denoise_strength {self.denoise_strength} not in (0, 1.5]")
        _require(0 <= self.blackpoint < 1, f"blackpoint {self.blackpoint} not in [0, 1)")
        _require(self.saturation >= 0, f"saturation {self.saturation} negative")


# ============================================================================
# Stage keys, artifact references, and the closed operation union
# ============================================================================


class StageKey(Enum):
    """Internal stage identifiers used for planning, execution, and progress."""

    ANALYZE = "analyze"
    PRIOR = "prior"
    PREPARE_SOURCE = "prepare_source"
    COMBINE_SESSIONS = "combine_sessions"
    CROP = "crop"
    TRAIL = "trail"
    BACKGROUND_EXTRACTION = "bge"
    EXTENDED_PROBE = "probe"
    DENOISE = "denoise"
    COLOR_CALIBRATION = "color"
    STRETCH_SOLVE = "solve_stretch"
    STRETCH = "stretch"
    STAR_SEPARATION = "starless"
    STAR_BLEND = "star_reduce"
    CLOSEUP = "closeup"
    EXPORT = "export"
    METADATA = "metadata"
    ANNOTATE = "annotate"


class ArtifactRole(Enum):
    """Artifact roles in the pipeline."""

    SOURCE = "source"
    PRODUCTION = "production"
    PROBE = "probe"
    QA = "qa"
    DELIVERABLE = "deliverable"
    CANDIDATE = "candidate"  # a deliverable-shaped export awaiting accept/reject;
    # scratch-routed like PRODUCTION/PROBE until promotion copies it to a real
    # DELIVERABLE path (see orchestrate.py's promotion step, Phase 3.0 Task 5;
    # Task 6 adds the atomic run-manifest commit on top of that promotion).


@dataclass(frozen=True)
class ArtifactRef:
    """Path-free reference to a pipeline artifact."""

    name: str
    role: ArtifactRole


@dataclass(frozen=True)
class PrepareSource:
    """Load a session into working memory."""

    session_index: int
    output: ArtifactRef
    stage: ClassVar[StageKey] = StageKey.PREPARE_SOURCE

    @property
    def inputs(self) -> tuple[ArtifactRef, ...]:
        return ()

    @property
    def outputs(self) -> tuple[ArtifactRef, ...]:
        return (self.output,)


@dataclass(frozen=True)
class CombineSessions:
    """Register and integrate multiple sessions."""

    session_indices: tuple[int, ...]
    output: ArtifactRef
    stage: ClassVar[StageKey] = StageKey.COMBINE_SESSIONS

    @property
    def inputs(self) -> tuple[ArtifactRef, ...]:
        return ()

    @property
    def outputs(self) -> tuple[ArtifactRef, ...]:
        return (self.output,)


@dataclass(frozen=True)
class Crop:
    """Crop measured-invalid borders and normalize orientation.

    The rectangle is in RAW FITS array coordinates: `x` counts columns from the
    file's column-0 edge, `y` counts rows from the file's row-0 edge (Diagnosis 1,
    spec 2026-08-12 -- per-edge measured crop, no forced symmetry). `mirror_x` is
    independent: a zero-crop run (x=y=0, full width/height) still normalizes
    orientation."""

    input: ArtifactRef
    output: ArtifactRef
    x: int
    y: int
    width: int
    height: int
    mirror_x: bool
    stage: ClassVar[StageKey] = StageKey.CROP

    @property
    def inputs(self) -> tuple[ArtifactRef, ...]:
        return (self.input,)

    @property
    def outputs(self) -> tuple[ArtifactRef, ...]:
        return (self.output,)


@dataclass(frozen=True)
class RemoveTrail:
    """Remove satellite/airplane trails."""

    input: ArtifactRef
    output: ArtifactRef
    stage: ClassVar[StageKey] = StageKey.TRAIL

    @property
    def inputs(self) -> tuple[ArtifactRef, ...]:
        return (self.input,)

    @property
    def outputs(self) -> tuple[ArtifactRef, ...]:
        return (self.output,)


@dataclass(frozen=True)
class BackgroundExtract:
    """GraXpert background extraction."""

    input: ArtifactRef
    output: ArtifactRef
    smoothing: float
    correction: str
    stage: ClassVar[StageKey] = StageKey.BACKGROUND_EXTRACTION

    @property
    def inputs(self) -> tuple[ArtifactRef, ...]:
        return (self.input,)

    @property
    def outputs(self) -> tuple[ArtifactRef, ...]:
        return (self.output,)


@dataclass(frozen=True)
class Denoise:
    """GraXpert denoising."""

    input: ArtifactRef
    output: ArtifactRef
    strength: float
    stage: ClassVar[StageKey] = StageKey.DENOISE

    @property
    def inputs(self) -> tuple[ArtifactRef, ...]:
        return (self.input,)

    @property
    def outputs(self) -> tuple[ArtifactRef, ...]:
        return (self.output,)


@dataclass(frozen=True)
class ColorCalibrate:
    """Color calibration (PCC) and background neutralization."""

    input: ArtifactRef
    output: ArtifactRef
    pcc: bool
    stage: ClassVar[StageKey] = StageKey.COLOR_CALIBRATION

    @property
    def inputs(self) -> tuple[ArtifactRef, ...]:
        return (self.input,)

    @property
    def outputs(self) -> tuple[ArtifactRef, ...]:
        return (self.output,)


@dataclass(frozen=True)
class StretchRender:
    """Stretch, saturation, and structural enhancements."""

    input: ArtifactRef
    output: ArtifactRef
    asinh_d: float
    blackpoint: float
    mtf_mid: float
    saturation: float
    ght: GhsParams | None
    black_lift: float
    stage: ClassVar[StageKey] = StageKey.STRETCH

    @property
    def inputs(self) -> tuple[ArtifactRef, ...]:
        return (self.input,)

    @property
    def outputs(self) -> tuple[ArtifactRef, ...]:
        return (self.output,)


@dataclass(frozen=True)
class SeparateStars:
    """StarNet2 star/starless separation."""

    input: ArtifactRef
    starless: ArtifactRef
    stars: ArtifactRef
    stage: ClassVar[StageKey] = StageKey.STAR_SEPARATION

    @property
    def inputs(self) -> tuple[ArtifactRef, ...]:
        return (self.input,)

    @property
    def outputs(self) -> tuple[ArtifactRef, ...]:
        return (self.starless, self.stars)

    @property
    def byproducts(self) -> tuple[ArtifactRef, ...]:
        """Outputs the tool produces whether or not the plan wants them: starnet2
        always emits the stars layer (`--unscreen`). A plan that never consumes it
        (cluster QA starless) is not wrong -- validate_plan's orphan check exempts
        byproducts. Ops without this property default to () (plan_checks getattr)."""
        return (self.stars,)


@dataclass(frozen=True)
class ScreenBlend:
    """Screen-blend reduced stars over starless."""

    starless: ArtifactRef
    stars: ArtifactRef
    output: ArtifactRef
    k: float
    stage: ClassVar[StageKey] = StageKey.STAR_BLEND

    @property
    def inputs(self) -> tuple[ArtifactRef, ...]:
        return (self.starless, self.stars)

    @property
    def outputs(self) -> tuple[ArtifactRef, ...]:
        return (self.output,)


@dataclass(frozen=True)
class CropTo:
    """Crop to bounding box (closeup framing)."""

    input: ArtifactRef
    output: ArtifactRef
    box: tuple[int, int, int, int]
    stage: ClassVar[StageKey] = StageKey.CLOSEUP

    @property
    def inputs(self) -> tuple[ArtifactRef, ...]:
        return (self.input,)

    @property
    def outputs(self) -> tuple[ArtifactRef, ...]:
        return (self.output,)


@dataclass(frozen=True)
class Export:
    """Export to image formats (16-bit TIFF, PNG, JPG)."""

    input: ArtifactRef
    output_base: str
    jpg_quality: int = 95
    formats: tuple[str, ...] = ("tif", "png", "jpg")
    role: ArtifactRole = ArtifactRole.DELIVERABLE
    stage: ClassVar[StageKey] = StageKey.EXPORT

    @property
    def inputs(self) -> tuple[ArtifactRef, ...]:
        return (self.input,)

    @property
    def outputs(self) -> tuple[ArtifactRef, ...]:
        return tuple(ArtifactRef(name=f"{self.output_base}.{f}", role=self.role) for f in self.formats)


@dataclass(frozen=True)
class MetadataCard:
    """EXIF/XMP metadata content (deterministic capture facts, no tool versions or timestamps)."""

    title: str
    object_name: str  # bare target label ("M8") for IPTC:ObjectName — title may append a common name
    description: str
    one_liner: str
    user_comment: str
    keywords: tuple[str, ...]
    datetime_original: str | None
    artist: str | None
    ra_deg: float | None
    dec_deg: float | None
    pixscale_arcsec: float | None


@dataclass(frozen=True)
class AnnotationCard:
    """Caption/annotation content for Annotate stage."""

    title: str
    subtitle: str
    lines: tuple[str, ...]
    pixscale_arcsec: float | None
    cd: tuple[float, float, float, float] | None


@dataclass(frozen=True)
class EmbedMetadata:
    """Embed metadata into exported files."""

    targets: tuple[ArtifactRef, ...]
    card: MetadataCard
    variant: str
    stage: ClassVar[StageKey] = StageKey.METADATA

    @property
    def inputs(self) -> tuple[ArtifactRef, ...]:
        return self.targets

    @property
    def outputs(self) -> tuple[ArtifactRef, ...]:
        return ()


@dataclass(frozen=True)
class Annotate:
    """Annotate image with labels and scale bar."""

    input: ArtifactRef
    output: ArtifactRef
    card: AnnotationCard
    stage: ClassVar[StageKey] = StageKey.ANNOTATE

    @property
    def inputs(self) -> tuple[ArtifactRef, ...]:
        return (self.input,)

    @property
    def outputs(self) -> tuple[ArtifactRef, ...]:
        return (self.output,)


# Closed operation union — the single source of truth for all pipeline operations
Operation = (
    PrepareSource
    | CombineSessions
    | Crop
    | RemoveTrail
    | BackgroundExtract
    | Denoise
    | ColorCalibrate
    | StretchRender
    | SeparateStars
    | ScreenBlend
    | CropTo
    | Export
    | EmbedMetadata
    | Annotate
)


@dataclass(frozen=True)
class ProbeRecord:
    """One stretch-solve probe iteration, oldest first. `phase` mirrors
    SolveState.phase ("mtf" while searching the inner mtf binary search,
    "bp" while nested inside an outer blackpoint search)."""

    phase: str
    mtf_mid: float
    blackpoint: float
    value: float
    sky: float
    target_level: float
    distance: float


@dataclass(frozen=True)
class SolveProvenance:
    """Record of the stretch solve computation."""

    metric: TargetMetric
    target_level: float
    achieved: float
    sky: float
    mtf_mid: float
    blackpoint: float
    converged: bool
    mtf_pinned: str | None
    bp_pinned: bool
    bp_iterations: int
    ght_in_probe: bool
    level_base: float
    level_scale: float
    asinh_d: float
    probes: tuple[ProbeRecord, ...]
    # Diagnosis 5 (defaults keep the field order/old constructors stable): which
    # frozen SOLVE REGION every probe of this solve measured against, and
    # whether the probe trace stayed monotonic. `nonmonotonic=True` means the
    # guard demoted a would-be-converged result to the conservative fallback.
    #
    # The solve region is NOT the structure support (owner ruling, 2026-08-14).
    # Target level 72 was calibrated against the object mask, so freezing a
    # DIFFERENT region silently redefines what the number means: the first
    # implementation solved on the starless sigma support and shipped M81/M100
    # at object-mask body_p90 33/42 against a 70/70 baseline, while over-lifting
    # M8/M17. The region is therefore the object mask, frozen once off the first
    # real-chain body probe -- except where the catalogue-corroborated
    # frame-filling recovery fired, whose broad support IS the honest subject.
    region_source: str | None = None  # "frozen_object_mask" | "recovered_structure_support" | None
    region_area_frac: float | None = None
    nonmonotonic: bool = False


@dataclass(frozen=True)
class PccEvidence:
    """Structured evidence parsed from the color-calibration tool's own output
    (Diagnosis 4). The ADAPTER parses and reports; it never decides whether the
    solution is acceptable — that judgment is `core.policy.evaluate_pcc_evidence`.
    Only produced when a PCC command actually ran to completion; a command that
    errored surfaces as a WorkFailed with an adapter-classified reason instead."""

    succeeded: bool  # tool printed its explicit success line
    imprecise: bool  # tool flagged "imprecise solution" (the M82 S50 case)
    few_stars_warning: bool  # tool flagged "did not rely on many stars"
    stars_used: int | None  # calibration stars the solution used, when reported
    coefficients: tuple[float, float, float] | None  # applied white-balance factors
    detail: str  # bounded machine excerpt of the evidence lines


@dataclass(frozen=True)
class PccVerdict:
    """Deterministic accept/reject of PCC evidence against PccBounds.

    `qualified` marks an acceptance the tool itself hedged (its advisory
    `imprecise` flag) while every other datum stayed in bounds -- accepted, but
    reported as uncertain on every surface rather than silently."""

    accepted: bool
    reason: str  # machine-stable, e.g. "imprecise_within_bounds" | "evidence_within_bounds"
    qualified: bool = False


@dataclass(frozen=True)
class ColorProvenance:
    """Which color path shipped, and why (Diagnosis 4). Recorded in the Plan
    sidecar, run notes, and EXIF so every surface agrees. `attempted=False,
    fallback=False` is the ordinary policy-off native path (LP / IRCUT
    starfield); `fallback=True` means a PCC attempt was rejected or unavailable
    and a FRESH native artifact replaced it."""

    attempted: bool
    accepted: bool
    fallback: bool
    reason: str
    evidence: PccEvidence | None
    # True when the calibration was accepted despite the tool's own advisory
    # flag (Owner Gate 3): shipped, but reported as uncertain everywhere.
    qualified: bool = False


@dataclass(frozen=True)
class Plan:
    """Fully computed execution plan — path-free, immutable, equality-testable."""

    steps: tuple[Operation, ...]
    deliverables: tuple[ArtifactRef, ...]
    solve: SolveProvenance | None
    # Diagnosis 4 (default keeps old constructors stable): the shipped color path.
    color: ColorProvenance | None = None


# ============================================================================
# Planner work requests, observations, and results
# ============================================================================


class MeasureKind(Enum):
    BORDER_SCAN = "border_scan"  # per-edge invalid border widths on the prepared source
    SUBJECT_SUPPORT = "subject_support"  # stable subject support from the linear artifact
    LINEAR_STATS = "linear_stats"  # background/bgnoise of an artifact (canonical space)
    EXTENDED_SIGNAL = "extended_signal"  # E via quick-stretch + StarNet probe chain
    CONCENTRATION = "concentration"  # cluster core detection on linear master
    RENDER_METRIC = "render_metric"  # probe JPG metric: (value, sky)
    SUBJECT_BOX = "subject_box"  # framing bbox from a starless artifact
    TRAIL_SCAN = "trail_scan"  # detect (and inpaint into output ref if present)
    INPUT_STARS = "input_stars"  # findstar on the linear master (QA attribution)


@dataclass(frozen=True)
class RunOps:
    """Execute a sequence of production operations, in order."""

    ops: tuple[Operation, ...]


@dataclass(frozen=True)
class Measure:
    """Take a measurement off an artifact (no pipeline mutation, unless TRAIL_SCAN
    writes the detrailed artifact into `output`, SUBJECT_SUPPORT writes the
    support raster into `output`, or a RENDER_METRIC probe FREEZES the solve
    region into `output`)."""

    kind: MeasureKind
    ref: ArtifactRef
    # TRAIL_SCAN detrailed artifact / SUBJECT_SUPPORT raster / RENDER_METRIC:
    # freeze this probe's object mask as the solve region and write it here
    output: ArtifactRef | None = None
    metric: TargetMetric | None = None  # RENDER_METRIC
    # RENDER_METRIC only, and deliberately two separate rasters (owner ruling,
    # 2026-08-14 -- the former "one support drives both" invariant is superseded):
    #   `region`            the frozen SOLVE REGION the body metric is measured
    #                       over, so the metric responds to mtf alone and never to
    #                       mask membership churn (Diagnosis 5). Absent on the
    #                       first probe, which freezes it into `output` instead.
    #   `structure_support` the broad subject support, used ONLY as the sky
    #                       complement for the two-point solve (Owner Gate 2: on a
    #                       frame-filling subject the global median sits inside the
    #                       nebulosity).
    region: ArtifactRef | None = None
    structure_support: ArtifactRef | None = None


@dataclass(frozen=True)
class LookupLocalTarget:
    """Always-on local catalog lookup for every WCS target."""


@dataclass(frozen=True)
class FetchNetworkPrior:
    """Cone-search the WCS field center in the catalog (shell already knows the center)."""


WorkRequest = RunOps | Measure | LookupLocalTarget | FetchNetworkPrior


@dataclass(frozen=True)
class WorkItem:
    id: str  # planner-assigned, unique within the session
    request: WorkRequest


# ---- observations (typed results) ----
@dataclass(frozen=True)
class PriorObservation:
    catalog_category: Category  # UNKNOWN when catalog had no dominant object
    fov_fraction: float | None


@dataclass(frozen=True)
class BorderObservation:
    """Measured per-edge invalid border widths (px), in RAW FITS array terms:
    `top` = the file's first rows, `bottom` = last rows, `left`/`right` =
    first/last columns. `confident=False` means the detector saw something it
    cannot demonstrate invalid (e.g. merely-dark lines) -- the core then crops
    zero and records the ambiguity (Diagnosis 1: uncertain pixels are kept)."""

    left: int
    top: int
    right: int
    bottom: int
    confident: bool
    detail: str  # machine reason string, e.g. "clean" or "top=blank:28"


@dataclass(frozen=True)
class CropDecision:
    """The core's deterministic per-edge crop decision (planning.decide_crop).
    `forced` marks the legacy explicit crop_border_frac override path; `warning`
    is non-None whenever measured evidence was refused or unavailable."""

    left: int
    top: int
    right: int
    bottom: int
    reason: str  # "measured" | "forced_border_frac" | "ambiguous" | "scan_failed"
    forced: bool
    warning: str | None


@dataclass(frozen=True)
class TrailObservation:
    present: bool


@dataclass(frozen=True)
class LinearStatsObservation:
    background: float
    bgnoise: float


@dataclass(frozen=True)
class EValueObservation:
    e_value: float


@dataclass(frozen=True)
class ConcentrationObservation:
    concentration: float


@dataclass(frozen=True)
class SolveRegionObservation:
    """Typed summary of the region one solve measures its body metric over.

    Pixel data stays SHELL-side (the raster the shell wrote to the freezing
    probe's `output` ref); the core reasons only about this summary. Distinct
    from `SubjectSupportObservation` on purpose: the support answers "what
    structure did the source contain", the region answers "where is the level
    that target 72 was calibrated against measured"."""

    area_frac: float  # region fraction of the frame [0, 1]
    dims: tuple[int, int]  # (width, height) of the raster, for geometry checks
    source: str  # "frozen_object_mask" | "recovered_structure_support" | "none"
    valid: bool
    detail: str  # machine reason string


@dataclass(frozen=True)
class ProbeMetricObservation:
    value: float
    sky: float
    # Set ONLY on the probe that was asked to freeze the solve region (the first
    # body_p90 probe of a solve). `valid=False` means the shell could not freeze
    # one and measured its recorded conservative global fallback instead -- the
    # planner records that and stops asking, so no later probe silently
    # re-derives a different region.
    region: SolveRegionObservation | None = None


@dataclass(frozen=True)
class SubjectSupportObservation:
    """Typed summary of the stable subject-support measurement (Diagnoses 5+3).
    Pixel data stays SHELL-side (the raster the shell wrote to the Measure's
    `output` ref); the core reasons only about this summary. `valid=False`
    means no usable support was found -- the solver then uses its conservative
    global fallback and faint-structure QA reports "not measured", both
    recorded, never silent."""

    area_frac: float  # supported fraction of the frame [0, 1]
    dims: tuple[int, int]  # (width, height) of the raster, for geometry checks
    source: str  # "starless_sigma" | "starless_low_sigma" | "starless_dark_anchored" | "none"
    valid: bool
    detail: str  # machine reason string
    # True when the catalogue-corroborated frame-filling recovery produced this
    # support (Owner Gate 2). A typed flag, not a `source` string comparison, so
    # the core never sniffs shell vocabulary: it is the one case where the broad
    # support IS the honest solve region, because the object mask is known to
    # have collapsed onto the brightest knots of a frame-filling subject.
    recovered: bool = False


@dataclass(frozen=True)
class SubjectBoxObservation:
    box: tuple[int, int, int, int] | None  # None = subject too large / unmeasurable


@dataclass(frozen=True)
class InputStarsObservation:
    star_count: int | None
    fwhm_px: float | None


@dataclass(frozen=True)
class OpsDone:
    """A RunOps batch completed successfully. `pcc_evidence` carries the parsed
    tool evidence when the batch ran a ColorCalibrate with pcc=True (Diagnosis 4);
    None otherwise — the planner treats a missing report on an attempted PCC as
    unverifiable and takes the recorded native fallback."""

    pcc_evidence: PccEvidence | None = None


Observation = (
    PriorObservation
    | BorderObservation
    | TrailObservation
    | LinearStatsObservation
    | EValueObservation
    | ConcentrationObservation
    | ProbeMetricObservation
    | SubjectSupportObservation
    | SubjectBoxObservation
    | InputStarsObservation
    | OpsDone
)


# ---- results ----
@dataclass(frozen=True)
class WorkSucceeded:
    id: str
    observation: Observation


@dataclass(frozen=True)
class WorkUnavailable:
    id: str
    reason: str


@dataclass(frozen=True)
class WorkFailed:
    id: str
    reason: str


WorkResult = WorkSucceeded | WorkUnavailable | WorkFailed


# ============================================================================
# QA gates as pure typed rules
# ============================================================================


@dataclass(frozen=True)
class RenderMetrics:
    """Typed mirror of qa.measure_render's output + star fields (qa.score adds
    star_count/fwhm after measure_render). Non-Optional fields are always
    populated by measure_render (it defaults to 0.0, never omits them);
    Optional fields are genuinely absent when the relevant artifact/reference
    is missing (e.g. no starless -> no faint_structure input for that pass).

    `star_count`/`fwhm` are `None` when the `findstar` measurement itself failed
    (tool unavailable, detection error) -- unlike every other Optional field here,
    `core.qa_rules.evaluate_gates` treats a `None` value here as a FAIL, not a
    SKIPPED verdict, whenever an ACTIVE `star_preservation`/`fwhm_ceiling`
    threshold needs it (M2, 2026-07-31: fail-closed, not silently absent)."""

    dimensions: tuple[int, int]
    envelope_area_frac: float
    body_p90: float | None
    body_p95: float | None
    body_rgb: tuple[float, float, float] | None
    background_median_outside_mask: float | None
    near_black_frac_outside_mask: float
    core_sat_frac: float
    pegged_chroma_frac_body: float
    body_saturation_median: float
    core_saturation_median: float
    core_value_median: float
    star_saturation_median: float
    global_black_frac: float
    star_count: int | None  # None -> FAIL (not SKIPPED) when star_preservation is active
    fwhm: float | None  # None -> FAIL (not SKIPPED) when fwhm_ceiling is active


@dataclass(frozen=True)
class GateThresholds:
    """Typed mirror of config.yaml qa.gates (built by io.gate_thresholds_from_cfg).
    Per-category fields are None when the whole threshold is absent from config
    (that gate is skipped for every category, never a false fail). body_rgb_max_ratio
    has no nebula field BY DESIGN -- LP Halpha naturally dominates red, so that
    check never applied to nebula (config.yaml qa.gates.body_rgb_max_ratio.nebula
    is null)."""

    enabled: bool
    reference_pixscale: float | None
    reference_dims: tuple[int, int] | None
    # star_preservation (Diagnosis 2, 2026-08): SOURCE-RELATIVE -- the output is
    # judged against the stars measured in THIS capture's linear master
    # (INPUT_STARS), never an absolute per-camera count. One cross-category
    # ratio + a small absolute catastrophe floor (capped at the input count).
    # Calibration: docs/assessments/2026-08-13-cross-camera-validation-coverage.md.
    star_preservation_min_retention: float | None
    star_preservation_catastrophe_floor: int | None
    fwhm_ceiling: PerCategory | None
    core_sat_ceiling: PerCategory | None
    # faint_structure (Diagnosis 3, 2026-08): SOURCE-RELATIVE -- the published
    # starless render is judged by how much of the run's OWN measured subject
    # support stays visible, never by an inverse-FOV frame fraction. One
    # cross-category retention bound + a small absolute catastrophe floor on
    # the output's global extended fraction.
    faint_retention_min: float | None
    faint_catastrophe_floor: float | None
    near_black_ceiling: PerCategory | None
    sky_median_ceiling: PerCategory | None
    pegged_chroma_ceiling: PerCategory | None
    core_saturation_ceiling: PerCategory | None
    star_saturation_ceiling: PerCategory | None
    body_rgb_max_ratio_starfield: float | None
    body_rgb_max_ratio_galaxy: float | None
    star_retention_warn: float


class Verdict(Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"
    WARN = "warn"


@dataclass(frozen=True)
class FaintEvidence:
    """Source-relative faint-structure evidence for one published starless
    render (Diagnosis 3). `retention` = fraction of the run's measured subject
    support still visible above the output's own background; `output_frac` =
    the output starless' global extended fraction (the absolute catastrophe
    arm). `support_state` is the honesty switch: "valid" judges; "not_measured"
    skips (a starfield without a cluster, a profile with no starless render, an
    older run); "missing_artifact" fails closed (the manifest promised a
    support raster that is gone or mismatched)."""

    retention: float | None
    output_frac: float | None
    support_state: str  # "valid" | "not_measured" | "missing_artifact"


@dataclass(frozen=True)
class StarPreservation:
    """The source-relative star-preservation comparison (Diagnosis 2), recorded
    alongside the gate verdict so QA JSON / notes carry the full evidence:
    both counts, the ratio, and the thresholds that judged them. `retention` is
    None when either count is missing or the input is zero. `effective_floor`
    = min(configured catastrophe floor, input count) -- an output is never
    required to exceed what its source contained."""

    input_count: int | None
    output_count: int | None
    retention: float | None
    min_retention: float | None
    catastrophe_floor: int | None
    effective_floor: int | None


@dataclass(frozen=True)
class GateResult:
    """One gate's verdict. `detail` is "" for PASS; for FAIL it is byte-identical
    to the string qa.absolute_gates produced for the same inputs (validate.py's
    _GATE_KIND regex parses it -- changing the wording breaks baseline diffing
    silently). SKIPPED/WARN carry a human-readable reason, never parsed."""

    gate: str
    verdict: Verdict
    detail: str


@dataclass(frozen=True)
class QAReport:
    """Category-aware QA gate verdicts for one render. UNKNOWN category is
    ALLOWED (unlike PerCategory.for_category, which refuses it) -- every gate
    is then a visible SKIPPED verdict, never silently absent (the M8 overcook
    lesson: a gate that silently skips is not a gate)."""

    category: Category
    gates: tuple[GateResult, ...]
    warnings: tuple[str, ...]
    passed: bool


class HeroOutcome(Enum):
    """The final, honest state of the artifact that ships under the recommended
    name -- never computed on an intermediate probe render."""

    PASS = "pass"
    SOURCE_LIMITED = "source_limited"
    PROCESSING_FAILED = "processing_failed"


@dataclass(frozen=True)
class HeroQualityReport:
    """PASS only if every hard gate on the ACTUAL final passes, the stretch
    solver converged (or was never run, or missed by no more than tolerance),
    and the final hero is still within `contract_delta` of what the solver
    targeted. SOURCE_LIMITED when the only failures are ones already attributed
    to a physical input limitation (e.g. input FWHM already exceeds the
    ceiling). PROCESSING_FAILED otherwise -- including when the solver
    converged but a LATER stage (saturation, star reduction) pulled the shipped
    artifact away from the solved intent, and including non-convergence beyond
    tolerance even if every hard gate happens to pass.

    `soft_fail_gates` (2026-08-01, the M8 false-positive finding) lists FAIL
    gates that are real but aesthetic-only misses on THIS shipped hero (default:
    fwhm) -- never blocking for the end-user-facing outcome the way
    `processing_failed_gates` does, but still an honest measured value a
    developer can inspect. A gate never counts as soft when its FAIL means the
    underlying measurement itself was unavailable (M2 fail-closed) -- a broken
    instrument stays a hard, visible failure, never a quiet aesthetic note."""

    outcome: HeroOutcome
    qa: QAReport
    solve_converged: bool | None  # None = solver was skipped/off, nothing to converge
    contract_delta: float | None  # final hero metric - solved achieved; None = no solve ran
    source_limited_gates: tuple[str, ...]
    processing_failed_gates: tuple[str, ...]
    reasons: tuple[str, ...]
    soft_fail_gates: tuple[str, ...] = ()


class PublicationNamespace(Enum):
    """Which parallel deliverable namespace a run publishes into (spec 2026-08-13)."""

    CERTIFIED = "certified"
    UNCERTIFIED = "uncertified"


@dataclass(frozen=True)
class PublicationIdentity:
    """The (out_name, namespace) pair that scopes replacement and cleanup.

    A run may replace only its own identity's prior set; a certified run's
    cross-namespace cleanup targets only the uncertified identity of the SAME
    out_name -- never another profile's peer output."""

    out_name: str
    namespace: PublicationNamespace


@dataclass(frozen=True)
class PublicationDecision:
    """Pure verdict of core/publication.py::decide_publication. certified derives
    from HeroOutcome (PASS/SOURCE_LIMITED -> True); `cleans` is None for a failed
    run -- nothing a failed run does removes certified files (one-way direction)."""

    certified: bool
    namespace: PublicationNamespace
    identity: PublicationIdentity
    cleans: PublicationIdentity | None
