"""Core registry models and accessors: every HDL building block, what it
is, what it depends on, which golden defines its semantics, and what it
costs.

The registry itself lives in dau-core's hydra config tree
(``dau_core/config/dau-core/cores.yaml``): one ``CoreDefinition`` entry per
core, composed into the ccflow ``ModelRegistry`` at ``/dau-core/<name>``
and discoverable on the shared search path via the ``hydra.lernaplugins``
entry point. Composition IS the registry — this module defines the models
and thin cached accessors, never a core list. Resource envelopes are produced
by the dau-build synthesize-cores task; ``measured=False`` marks estimates
awaiting a synthesis pass. New entries record resource measurement points by
part, clock, and module parameters; legacy unkeyed envelopes remain readable
during migration.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from fractions import Fraction
from importlib import import_module
from pathlib import Path
from typing import Literal

from ccflow import BaseModel
from pydantic import ConfigDict, Field, NonNegativeFloat, NonNegativeInt, StrictInt, field_validator, model_validator

from .design import UnitKind
from .stream import LogicalType, OperationCode


class CoreKind(Enum):
    OPERATOR = "operator"
    STREAM_INFRA = "stream-infra"
    SHELL_ADAPTER = "shell-adapter"
    PACKAGE = "package"
    SIM_MODEL = "sim-model"


class LoadImageKind(Enum):
    """What a load-capable tile's LOAD-phase input stream is (the two-phase
    load: cfg_load=1 streams the build image, cfg_load=0 probes). The
    composition validates image/length-grid rules per kind; a core without
    this marker has no cfg_load port and can never be phase-bound."""

    BITMAP_64 = "bitmap-64"  # KEY_SPACE/64 little-endian 64-bit words (membership bitmap)
    QUAD_ROWS = "quad-rows"  # 16-byte build rows (hash-join key + payload)


class ContractKind(Enum):
    """Which streaming interface contract an operator tile speaks. Contract
    tests select their validator from this tag; a new core declares its
    contract here rather than being special-cased in test code."""

    ROW_STREAM = "row-stream"  # the standard 64-bit stream + status tile contract
    ARROW_LITE_DESCRIPTOR = "arrow-lite-descriptor"  # descriptor-driven Arrow-lite protocol
    # the wide-lane contract: SLOTS whole 128-bit quad rows per beat, one
    # beat per cycle, the all-ones row as the pad (drop-by-pad, no
    # compaction). No slang validator yet — like arrow-lite, conformance
    # rides the cocotb benches until one lands.
    WIDE_ROW_STREAM = "wide-row-stream"


class ResourceEnvelope(BaseModel):
    """Legacy unkeyed post-synthesis footprint. ``measured=False`` marks an
    engineering estimate. New measurements use ``ResourceMeasurement``."""

    model_config = ConfigDict(frozen=True)

    lut: NonNegativeInt
    ff: NonNegativeInt
    bram36: NonNegativeFloat
    dsp: NonNegativeInt
    measured: bool = True


class TimingStatus(Enum):
    MET = "met"
    VIOLATED = "violated"


class ResourceMeasurement(BaseModel):
    """One exact OOC result. Coordinates are deliberately discrete: a
    measurement says nothing about another part, clock, or parameter tuple."""

    model_config = ConfigDict(frozen=True)

    part: str = Field(min_length=1)
    clock_ns: float = Field(gt=0)
    params: Mapping[str, StrictInt] = {}
    lut: NonNegativeInt
    ff: NonNegativeInt
    bram36: NonNegativeFloat
    dsp: NonNegativeInt
    timing: TimingStatus
    wns_ns: float

    @model_validator(mode="after")
    def _timing_matches_wns(self) -> ResourceMeasurement:
        met = self.wns_ns >= 0
        if met != (self.timing is TimingStatus.MET):
            raise ValueError(f"timing {self.timing.value!r} disagrees with WNS {self.wns_ns:+g} ns")
        return self

    def usable_envelope(self) -> ResourceEnvelope:
        """Return resource counts only when timing met its target."""
        if self.timing is TimingStatus.VIOLATED:
            raise TimingViolationError(
                f"resource measurement for part={self.part!r}, clock_ns={self.clock_ns:g}, "
                f"params={dict(self.params)!r} violated timing (WNS {self.wns_ns:+g} ns)"
            )
        return ResourceEnvelope(lut=self.lut, ff=self.ff, bram36=self.bram36, dsp=self.dsp)


class IngestRate(BaseModel):
    """A stream core's guaranteed sustained ingest rate: ``rows`` rows per
    ``per_cycles`` clock cycles, an exact rational (1 row per 2 cycles is
    the composed lane rate the silicon proved; the key-mask dispatcher
    ingests 1 per 1). This is PUBLIC INTERFACE, not a heuristic: the
    scheduler/pricing software rate-matches a core it cannot inspect — a
    user-provided custom core tied to a custom operator is opaque except
    for its registry declaration, so the declaration carries the rate.
    Declare the FLOOR a core sustains under continuous valid input,
    with RECORD/DRAIN EMISSION AMORTIZED over the batch: close-out
    records, top-k drains, and per-group records are batch-tail or
    per-group costs, not per-row costs, so the declaration describes the
    streaming regime (groups much smaller than rows, drains at close).
    Data-dependent worst cases — every group key distinct, adversarial
    key runs into one dispatcher lane — are planner-regime concerns the
    planner sees in the plan (group keys, sortedness), not floor
    violations. A core may burst faster, and a measured improvement (a
    retime, a wider port) lands here the same release the RTL proves it.
    Compositions over these floors yield the sustained rate the scheduler
    plans on UNDER those stated assumptions."""

    model_config = ConfigDict(frozen=True)

    rows: StrictInt
    per_cycles: StrictInt
    # when set, the declared rate is PER SLOT and the effective rate is
    # rows/per_cycles x the named module parameter (the wide-lane cores:
    # ingest 1/1 per slot, SLOTS slots per beat). The name must be a
    # declared parameter of the core — registry-test enforced.
    scales_with_param: str | None = None
    # when set, the core's row framing rides the named WIDTH parameter and
    # the effective rate is min(rows/per_cycles, param/128): a 64-bit
    # framing carries half a 128-bit quad row per beat, wider framings one
    # or more, capped by the declared beat rate (the dispatcher's
    # serializer ingests wide beats but forwards one row per cycle). At
    # most one of scales_with_param / frames_on_param may be set.
    frames_on_param: str | None = None

    @field_validator("rows", "per_cycles")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("ingest rate components must be >= 1")
        return value

    @model_validator(mode="after")
    def _one_scaling_shape(self) -> IngestRate:
        if self.scales_with_param is not None and self.frames_on_param is not None:
            raise ValueError("an ingest rate scales with at most one parameter shape")
        return self

    @property
    def rows_per_cycle(self) -> Fraction:
        return Fraction(self.rows, self.per_cycles)

    def effective(self, params: Mapping[str, int], declared: Mapping[str, ParameterSpec]) -> Fraction:
        """The rate at concrete parameter values — what the scheduler uses.
        ``params`` are the composition's overrides; ``declared`` the core's
        parameter specs (defaults fill unset overrides)."""

        def value_of(name: str) -> int:
            spec = declared.get(name)
            if spec is None:
                raise ValueError(f"ingest rate scales with undeclared parameter {name!r}")
            return int(params.get(name, spec.default))

        if self.scales_with_param is not None:
            return self.rows_per_cycle * value_of(self.scales_with_param)
        if self.frames_on_param is not None:
            return min(self.rows_per_cycle, Fraction(value_of(self.frames_on_param), 128))
        return self.rows_per_cycle


class ParameterSpec(BaseModel):
    """An overridable SystemVerilog module parameter: its HDL default and the
    constraint a composition override must satisfy — mirroring the RTL's own
    elaboration guards so an invalid override is rejected at composition time,
    never later as an HDL elaboration failure. ``multiple_of`` mirrors sizing
    (the membership bitmap is ``KEY_SPACE/64`` words); ``choices`` pins an
    exact legal set (the quad-stream widths are 64 or 128, nothing else);
    ``power_of_two`` mirrors mask/bit-select decodes (the key-mask dispatcher's
    ``NUM_PARTITIONS``); ``minimum`` mirrors a lower bound (the rolling-moment
    window is ``>= 2`` — a size-1 sample variance is undefined). The HDL is the
    source of truth; this mirrors it."""

    model_config = ConfigDict(frozen=True)

    default: int
    multiple_of: int = Field(default=1, gt=0)
    choices: tuple[int, ...] | None = None
    power_of_two: bool = False
    minimum: int = 1
    maximum: int | None = None

    def violation(self, value: int) -> str | None:
        """Why ``value`` is not a legal override, or ``None`` when it is.
        This is the ONE statement of the constraint -- composition wraps it
        with tile context, and the model's own coherence check runs the
        default through it, so the rule cannot drift between the two."""
        if self.choices is not None:
            if value not in self.choices:
                allowed = ", ".join(str(choice) for choice in self.choices)
                return f"must be one of {allowed}, got {value!r}"
            return None  # an enumerated choice set (which may include a 0 flag) is the whole constraint
        if value <= 0:
            return f"must be a positive int, got {value!r}"
        if value < self.minimum:
            return f"must be >= {self.minimum}, got {value!r}"
        if self.maximum is not None and value > self.maximum:
            return f"must be <= {self.maximum}, got {value!r}"
        if value % self.multiple_of != 0:
            return f"must be a positive multiple of {self.multiple_of}, got {value!r}"
        if self.power_of_two and value & (value - 1):
            return f"must be a power of two, got {value!r}"
        return None

    @model_validator(mode="after")
    def _constraints_cohere(self) -> ParameterSpec:
        """The spec mirrors RTL elaboration guards, so an incoherent spec is a
        registry bug that would reject every override -- catch it at
        registration, not at the first composition."""
        if self.maximum is not None and self.minimum > self.maximum:
            raise ValueError(f"minimum {self.minimum} exceeds maximum {self.maximum}")
        if self.choices is not None and not self.choices:
            raise ValueError("choices, when given, must be non-empty")
        reason = self.violation(self.default)
        if reason is not None:
            raise ValueError(f"default {self.default} violates the spec's own constraints: {reason}")
        return self


class StorageIntent(BaseModel):
    """A core's declared storage need (STORAGE_AND_NOC.md). ``owned`` = a
    private single-cycle arena; ``shared`` = handle-based tier transactions.
    Capacity prices against a declared module parameter so the planner can
    refuse what no tier serves — never discovered at synthesis."""

    model_config = ConfigDict(frozen=True)

    role: str = Field(min_length=1)
    access: Literal["owned", "shared"]
    bytes_per_unit: int = Field(gt=0)
    scale_parameter: str | None = None
    tier_hint: str | None = None


class CoreDefinition(BaseModel):
    """One HDL building block and its contract surround."""

    model_config = ConfigDict(frozen=True)

    name: str
    kind: CoreKind
    module: str
    source: str
    description: str
    depends: tuple[str, ...] = ()
    golden: str | None = None  # "module:callable" defining the semantics
    bench: str | None = None  # cocotb bench module
    resources: ResourceEnvelope | tuple[ResourceMeasurement, ...] | None = None
    count_port: str | None = None  # the tile's trailing status counter, if any
    contract_kind: ContractKind | None = None  # required for operators; None elsewhere (no declared stream contract)
    # the core's guaranteed sustained ingest rate — like contract_kind,
    # required for stream operators (registry-enforced): the
    # public fact the scheduler feeds an opaque core by
    ingest: IngestRate | None = None
    # Logical stream record shape, in 64-bit words. Exactly one input shape
    # is required for every operator. A parameterized shape names
    # the module parameter whose concrete/default value supplies the width.
    record_words: StrictInt | None = None
    record_words_param: str | None = None
    # Omit both when output preserves the input record shape. Fixed and
    # parameterized reshapers declare their emitted shape here. A few
    # field-mask aggregators have a genuinely runtime-variable output length;
    # consumers cannot safely infer a fixed adjacency for those streams.
    output_record_words: StrictInt | None = None
    output_record_words_param: str | None = None
    output_record_words_variable: bool = False
    # A generic elastic register-depth axis is valid only when computation is
    # combinational per beat. Multi-word framing/state machines must leave it
    # false even if a bespoke internal pipeline exists.
    supports_pipeline_depth: bool = False
    # capability facts (HDL truth, consumed by the composition-derived
    # capability contract): the descriptor-selectable OperationCodes a tile
    # executes in hardware and the logical types those opcodes accept.
    # AVERAGE is never declared here — it is not a hardware opcode (the host
    # divides the SUM/COUNT results at protocol level).
    opcodes: tuple[OperationCode, ...] = ()
    opcode_input_types: tuple[LogicalType, ...] = ()
    row_capacity: int | None = None  # batch row capacity for buffered tiles (bitonic sorter CAPACITY)
    unit_kind: UnitKind | None = None  # the design-inventory unit an instance of this tile contributes
    # HDL status truth: True when the tile emits a status close-out after
    # every successfully completed batch (terminal-tile behavior — the record
    # writer's done waits on it); False when the tile is silent on success
    # and raises status only for what the downstream cannot see (zero-rows
    # compaction, torn row, bad config — the chain-stage/partition contract,
    # exactly one close-out per lane per batch).
    success_close_out: bool = False
    # the two-phase load capability: what this tile's LOAD-phase input stream
    # is (None = no cfg_load port; the tile can never be phase-bound)
    load_image: LoadImageKind | None = None
    # overridable SystemVerilog module parameters and their HDL defaults (the
    # membership filter's KEY_SPACE, the broadcast hash join's KEY_SPACE, ...).
    # A composition may override a declared parameter to build a non-default
    # core size (the module-parameter channel); an override of an undeclared
    # name is rejected. The values here mirror the .sv parameter defaults —
    # the HDL is the source of truth, this is the machine-readable declaration
    # the composition validates against.
    parameters: Mapping[str, ParameterSpec] = {}
    # declared storage needs; the walker wires them, contracts advertise them
    storage_intent: tuple[StorageIntent, ...] = ()

    def storage_bytes(self, params: Mapping[str, int]) -> tuple[tuple[StorageIntent, int], ...]:
        """Each intent with its byte cost at the effective parameters
        (overrides win, declared defaults otherwise)."""
        priced = []
        for intent in self.storage_intent:
            units = 1
            if intent.scale_parameter is not None:
                spec = self.parameters[intent.scale_parameter]
                units = int(params.get(intent.scale_parameter, spec.default))
            priced.append((intent, intent.bytes_per_unit * units))
        return tuple(priced)

    def model_post_init(self, context) -> None:
        super().model_post_init(context)
        # the public interface is self-enforcing, not just registry-tested:
        # a user-provided operator core is invalid without its ingest rate
        # (the scheduler cannot feed a core whose rate it does not know)
        if self.kind == CoreKind.OPERATOR and self.ingest is None:
            raise ValueError(f"operator core {self.name!r} declares no ingest rate (required public interface)")
        if self.record_words is not None and self.record_words < 1:
            raise ValueError(f"core {self.name!r} record_words must be >= 1")
        if self.output_record_words is not None and self.output_record_words < 1:
            raise ValueError(f"core {self.name!r} output_record_words must be >= 1")
        if self.record_words is not None and self.record_words_param is not None:
            raise ValueError(f"core {self.name!r} declares both fixed and parameterized input record words")
        if self.output_record_words is not None and self.output_record_words_param is not None:
            raise ValueError(f"core {self.name!r} declares both fixed and parameterized output record words")
        if self.output_record_words_variable and (self.output_record_words is not None or self.output_record_words_param is not None):
            raise ValueError(f"core {self.name!r} declares both fixed/parameterized and runtime-variable output record words")
        if self.kind == CoreKind.OPERATOR and self.record_words is None and self.record_words_param is None:
            raise ValueError(f"operator core {self.name!r} declares no record_words (required public interface)")
        for role, name in (("input record words", self.record_words_param), ("output record words", self.output_record_words_param)):
            if name is not None and name not in self.parameters:
                raise ValueError(f"core {self.name!r} {role} follows undeclared parameter {name!r}")
        if self.supports_pipeline_depth and "PIPELINE_DEPTH" not in self.parameters:
            raise ValueError(f"core {self.name!r} supports pipeline depth but declares no PIPELINE_DEPTH parameter")
        for intent in self.storage_intent:
            if intent.scale_parameter is not None and intent.scale_parameter not in self.parameters:
                raise ValueError(
                    f"core {self.name!r} storage intent {intent.role!r} scales by "
                    f"scale_parameter {intent.scale_parameter!r}, which is not a declared parameter"
                )
        if isinstance(self.resources, tuple):
            expected_params = set(self.parameters)
            coordinates: set[tuple[str, float, tuple[tuple[str, int], ...]]] = set()
            for point in self.resources:
                if set(point.params) != expected_params:
                    raise ValueError(
                        f"core {self.name!r} resource measurement parameters {sorted(point.params)!r} "
                        f"do not match declared parameters {sorted(expected_params)!r}"
                    )
                coordinate = (point.part, point.clock_ns, tuple(sorted(point.params.items())))
                if coordinate in coordinates:
                    raise ValueError(f"core {self.name!r} has duplicate resource measurement coordinate {coordinate!r}")
                coordinates.add(coordinate)

    def _parameter_value(self, name: str, params: Mapping[str, int]) -> int:
        spec = self.parameters.get(name)
        if spec is None:
            raise ValueError(f"core {self.name!r} record shape follows undeclared parameter {name!r}")
        return int(params.get(name, spec.default))

    def effective_record_words(self, params: Mapping[str, int]) -> int:
        """Concrete input-record width after applying parameter overrides."""
        if self.record_words_param is not None:
            return self._parameter_value(self.record_words_param, params)
        if self.record_words is None:
            raise ValueError(f"core {self.name!r} declares no input record shape")
        return int(self.record_words)

    def effective_output_record_words(self, params: Mapping[str, int]) -> int | None:
        """Concrete emitted width, or ``None`` for runtime-variable output."""
        if self.output_record_words_variable:
            return None
        if self.output_record_words_param is not None:
            return self._parameter_value(self.output_record_words_param, params)
        if self.output_record_words is not None:
            return int(self.output_record_words)
        return self.effective_record_words(params)

    def resource_measurement(self, *, part: str, clock_ns: float, params: Mapping[str, int]) -> ResourceMeasurement:
        """Resolve one exact measurement, never a neighboring point.

        Composition overrides are expanded with declared defaults before
        comparison. Legacy scalar envelopes have no coordinates and therefore
        cannot answer this lookup safely.
        """
        requested = {name: int(params.get(name, spec.default)) for name, spec in self.parameters.items()}
        requested.update({name: int(value) for name, value in params.items() if name not in requested})
        if not isinstance(self.resources, tuple):
            raise UnmeasuredResourceError(
                f"core {self.name!r} is unmeasured for part={part!r}, clock_ns={clock_ns:g}, params={requested!r}; "
                "its legacy resource envelope has no measurement coordinates"
            )

        for point in self.resources:
            if point.part == part and point.clock_ns == clock_ns and dict(point.params) == requested:
                return point

        part_points = [point for point in self.resources if point.part == part]
        if not part_points:
            gap = f"part {part!r}"
        else:
            clock_points = [point for point in part_points if point.clock_ns == clock_ns]
            gap = f"clock_ns {clock_ns:g} on part {part!r}" if not clock_points else f"params {requested!r} at part={part!r}, clock_ns={clock_ns:g}"
        available = ", ".join(f"(part={point.part!r}, clock_ns={point.clock_ns:g}, params={dict(point.params)!r})" for point in self.resources)
        raise UnmeasuredResourceError(f"core {self.name!r} has unmeasured {gap}; measured points: {available or 'none'}")

    def source_path(self) -> Path:
        from . import hdl

        return Path(str(hdl.__path__[0])) / self.source

    def golden_callable(self):
        if self.golden is None:
            return None
        module_name, _, attr = self.golden.partition(":")
        return getattr(import_module(module_name), attr)


class UnknownCoreError(KeyError):
    pass


class UnmeasuredResourceError(LookupError):
    pass


class TimingViolationError(ValueError):
    pass


def loaded_cores() -> Mapping[str, CoreDefinition]:
    """The core registry, name -> definition in registry order: dau-core's
    hydra config tree (``dau-core/cores.yaml``) composed into the ccflow
    ``ModelRegistry`` at ``/dau-core/<core-name>``. Composition IS the
    registry — there is no Python-literal core list; these accessors are
    thin read-through views for in-process consumers (tests, dau shell
    walking). A ``/dau-core`` subregistry a consumer already composed
    (possibly with overrides) is the source of truth and is never reloaded
    or overwritten here; the packaged defaults load only when it is absent."""
    from ccflow import ModelRegistry

    subregistry = ModelRegistry.root().models.get("dau-core")
    if subregistry is None:
        from dau_core.config import load_config

        subregistry = load_config().models["dau-core"]
    return {name: model for name, model in subregistry.models.items() if isinstance(model, CoreDefinition)}


def core(name: str) -> CoreDefinition:
    try:
        return loaded_cores()[name]
    except KeyError:
        raise UnknownCoreError(f"unknown core {name!r}; known: {sorted(loaded_cores())}") from None


def core_for_module(module: str) -> CoreDefinition:
    for definition in loaded_cores().values():
        if definition.module == module:
            return definition
    raise UnknownCoreError(f"no core defines module {module!r}")


def cores_of_kind(kind: CoreKind) -> tuple[CoreDefinition, ...]:
    return tuple(definition for definition in loaded_cores().values() if definition.kind == kind)


def sources_for(*names: str) -> tuple[Path, ...]:
    """Dependency-closed, deduplicated source paths for a set of cores —
    packages and dependencies first, in registry order."""
    selected: dict[str, CoreDefinition] = {}

    def visit(name: str) -> None:
        definition = core(name)
        for dependency in definition.depends:
            visit(dependency)
        selected.setdefault(definition.name, definition)

    for name in names:
        visit(name)
    ordered = [definition for definition in loaded_cores().values() if definition.name in selected]
    return tuple(definition.source_path() for definition in ordered)
