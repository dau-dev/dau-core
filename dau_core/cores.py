"""Core registry models and accessors: every HDL building block, what it
is, what it depends on, which golden defines its semantics, and what it
costs.

The registry itself lives in dau-core's hydra config tree
(``dau_core/config/dau-core/cores.yaml``): one ``CoreDefinition`` entry per
core, composed into the ccflow ``ModelRegistry`` at ``/dau-core/<name>``
and discoverable on the shared search path via the ``hydra.lernaplugins``
entry point. Composition IS the registry — this module defines the models
and thin cached accessors, never a core list. Resource measurements are
produced by the dau-build synthesize-cores task and recorded per part, clock
and module parameters; there is no other shape.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from enum import Enum
from fractions import Fraction
from importlib import import_module
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, Field, NonNegativeFloat, NonNegativeInt, PlainSerializer, StrictInt, field_validator, model_validator

from .design import UnitKind
from .element_types import ElementTypeError, decode_element_type, element_code, element_type
from .generators import CoreGenerator
from .models import CheckedModel, FrozenMapping, ReadOnly
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


class CoreRole(Enum):
    """What a core is FOR where a composition has to know, declared by the
    registry rather than recognized by its name.

    Two composition rules cannot be read off a tile's ports: that the core in
    the optional front-stage feed-transform slot is one that widens packed
    rows, and that a key-routing partitioner's lane count satisfies the decode
    its ``MODE`` selects. Both were exact-name comparisons, which is the one
    thing "composition IS the registry" forbids -- a second unpacker or a
    second router could not be written without editing this package, and a
    tile rename silently turned the check into dead code. A core says here
    what job it does; the composition asks.

    ``KEY_DISPATCH`` is a contract as well as a label: a core declaring it
    routes whole records to lanes by a decode over the record's key, and
    carries ``MODE`` (a :class:`dau_core.operators.DispatchMode`) and
    ``NUM_PARTITIONS`` parameters with those meanings. A range partitioner
    routes by ordered splitters instead and is deliberately not one.
    """

    FRONT_UNPACK = "front-unpack"  # widens packed rows into the quad row the operator tiles read
    KEY_DISPATCH = "key-dispatch"  # routes whole records to lanes by a decode over the key
    # The shell infrastructure a composition sits in. Each of these is a job
    # exactly one registered core fills, and the composition asks for the
    # job rather than naming the core: the DDR scan shell's burst reader,
    # its lane broadcast, its per-lane record writer and its write mux; the
    # identity/capability register block every shell carries; the stream-job
    # tops that hand one tile chain its rows from staging; and the front
    # gearbox that reframes a wide feed into the 64-bit row stream.
    SCAN_READER = "scan-reader"
    SCAN_BROADCAST = "scan-broadcast"
    SCAN_RECORD_WRITER = "scan-record-writer"
    SCAN_WRITE_MUX = "scan-write-mux"
    SHELL_REGISTERS = "shell-registers"
    STREAM_JOB_SHELL = "stream-job-shell"
    FRONT_GEARBOX = "front-gearbox"
    # The operator jobs a planner fills when it lowers a query, asked for by
    # job rather than by tile name. A job with a narrow and a wide tile
    # (filter, map, field sum) is told apart by the tile's contract_kind.
    ROW_FILTER = "row-filter"  # passes the rows a predicate tree accepts
    ROW_MAP = "row-map"  # derives a field from two fields with an ALU op
    FIELD_SUM = "field-sum"  # sums selected fields and counts rows: the ungrouped terminal
    MEMBERSHIP_FILTER = "membership-filter"  # passes rows whose key is in a loaded set
    HASH_JOIN = "hash-join"  # joins each probe row to a loaded build table
    LANE_PARTITION = "lane-partition"  # keeps the rows of one lane's key partition
    FIXED_POINT_DIVIDE = "fixed-point-divide"  # divides two fields on a fixed-point grid
    GROUPED_SUM = "grouped-sum"  # per-group field sums over a dense key range
    HASH_GROUPED = "hash-grouped"  # per-group field sums over hashed keys
    GROUP_TABLE = "group-table"  # per-group sums in a table over packed wide rows
    GROUPED_STATS = "grouped-stats"  # per-group first/last/min/max/sum
    ROLLING_MOMENTS = "rolling-moments"  # windowed moments per symbol
    TOP_K = "top-k"  # the k best rows by a key
    SORTER = "sorter"  # sorts a batch of rows


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


class ResourceEnvelope(CheckedModel):
    """Resource counts: one LUT/FF/BRAM/DSP tuple. The usable face of a
    measurement point, or the sum a composition estimate adds up to. Never
    a registry entry on its own: the registry records measurement points,
    which carry the coordinates a count is meaningless without."""

    lut: NonNegativeInt
    ff: NonNegativeInt
    bram36: NonNegativeFloat
    dsp: NonNegativeInt


class TimingStatus(Enum):
    MET = "met"
    VIOLATED = "violated"


class ResourceMeasurement(CheckedModel):
    """One exact OOC result. Coordinates are deliberately discrete: a
    measurement says nothing about another part, clock, or parameter tuple.

    A combinational core has no clock: ``clock_ns`` is ``None``, no timing
    was reported and none is recorded, and the point answers for any clock
    because the logic costs the same at every one. A clocked point carries
    the clock it was constrained to, whether it met, and by how much."""

    part: str = Field(min_length=1)
    clock_ns: float | None = None
    params: Annotated[Mapping[str, StrictInt], *ReadOnly] = Field(default_factory=FrozenMapping)
    lut: NonNegativeInt
    ff: NonNegativeInt
    bram36: NonNegativeFloat
    dsp: NonNegativeInt
    timing: TimingStatus | None = None
    wns_ns: float | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_type_spellings(cls, data):
        """A coordinate may name an element type where the synthesis run took
        one ("FIELD_WIDTH: int32"), and is stored as the encoding, so it
        compares equal to the same point written as a number. No parameter of
        an SV module has a legal non-numeric value other than a type, so a
        string here needs no further disambiguation."""
        if not isinstance(data, Mapping):
            return data
        params = data.get("params")
        if not isinstance(params, Mapping) or not any(isinstance(value, str) for value in params.values()):
            return data
        resolved = {name: element_code(value) if isinstance(value, str) else value for name, value in params.items()}
        return {**data, "params": resolved}

    @model_validator(mode="after")
    def _timing_matches_wns(self) -> ResourceMeasurement:
        if self.clock_ns is None:
            if self.timing is not None or self.wns_ns is not None:
                raise ValueError("an unclocked (combinational) measurement records no timing status or slack")
            return self
        if self.clock_ns <= 0:
            raise ValueError(f"clock_ns must be positive, got {self.clock_ns}")
        if self.timing is None or self.wns_ns is None:
            raise ValueError(f"a measurement at clock_ns={self.clock_ns:g} must record timing and wns_ns")
        met = self.wns_ns >= 0
        if met != (self.timing is TimingStatus.MET):
            raise ValueError(f"timing {self.timing.value!r} disagrees with WNS {self.wns_ns:+g} ns")
        return self

    def answers_clock(self, clock_ns: float) -> bool:
        """Whether this point prices a build at ``clock_ns``: an unclocked
        point prices any clock, a clocked one only its own."""
        return self.clock_ns is None or _same_clock(self.clock_ns, clock_ns)

    def usable_envelope(self) -> ResourceEnvelope:
        """Return resource counts only when timing met its target (or there
        was no timing to meet)."""
        if self.timing is TimingStatus.VIOLATED:
            raise TimingViolationError(
                f"resource measurement for part={self.part!r}, clock_ns={self.clock_ns:g}, "
                f"params={dict(self.params)!r} violated timing (WNS {self.wns_ns:+g} ns)"
            )
        return ResourceEnvelope(lut=self.lut, ff=self.ff, bram36=self.bram36, dsp=self.dsp)


class IngestRate(CheckedModel):
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


class ParameterSpec(CheckedModel):
    """An overridable SystemVerilog module parameter: its HDL default and the
    constraint a composition override must satisfy — mirroring the RTL's own
    elaboration guards so an invalid override is rejected at composition time,
    never later as an HDL elaboration failure. ``multiple_of`` mirrors sizing
    (the membership bitmap is ``KEY_SPACE/64`` words); ``choices`` pins an
    exact legal set (the quad-stream widths are 64 or 128, nothing else);
    ``power_of_two`` mirrors mask/bit-select decodes (the key-mask dispatcher's
    ``NUM_PARTITIONS``); ``minimum`` mirrors a lower bound (the rolling-moment
    window is ``>= 2`` — a size-1 sample variance is undefined). The HDL is the
    source of truth; this mirrors it.

    ``element_type`` marks the parameter as naming WHAT AN ELEMENT IS rather
    than how big something is, and changes three things:

    * ``default`` and ``choices`` may be written as type spellings
      (``int32``, ``q16.16``, ``float64``) as well as integers, and are stored
      as the :mod:`dau_core.element_types` encoding either way. A plain signed
      integer encodes as its own width, so ``32`` and ``int32`` are the same
      declaration and no existing entry changes meaning.
    * ``choices`` are matched on the whole type. ``int32`` and ``q16.16`` are
      both 32 bits wide and are NOT interchangeable, which is the distinction
      a width could not draw.
    * ``minimum``/``maximum``/``multiple_of``/``power_of_two`` constrain the
      type's WIDTH, because that is what the RTL guard they mirror is about
      ("FIELD_WIDTH must divide 128 and lie in [8, 64]" is a statement about
      the field's bits, not about the number that names its type).

    Set it only for a parameter that genuinely selects an element type. Bus
    and beat widths (``IN_WIDTH``, ``OUT_WIDTH``, ``DATA_WIDTH``) count bits
    per handshake and are not types; declaring one here would let a design ask
    for a fixed-point AXI bus."""

    default: int
    multiple_of: int = Field(default=1, gt=0)
    choices: tuple[int, ...] | None = None
    power_of_two: bool = False
    minimum: int = 1
    maximum: int | None = None
    element_type: bool = False

    @model_validator(mode="before")
    @classmethod
    def _accept_type_spellings(cls, data):
        """Resolve declared type spellings to their encoding before the int
        fields see them, so everything downstream -- the constraint checks,
        the ``-generic`` a synthesis run passes, the canonical spec hash --
        works on one integer form and never has to know how it was written."""
        if not isinstance(data, Mapping) or not data.get("element_type"):
            return data
        resolved = dict(data)
        if "default" in resolved:
            resolved["default"] = element_code(resolved["default"])
        choices = resolved.get("choices")
        if choices is not None:
            resolved["choices"] = tuple(element_code(choice) for choice in choices)
        return resolved

    def violation(self, value: int | str) -> str | None:
        """Why ``value`` is not a legal override, or ``None`` when it is.
        This is the ONE statement of the constraint -- composition wraps it
        with tile context, and the model's own coherence check runs the
        default through it, so the rule cannot drift between the two."""
        if self.element_type:
            try:
                element = element_type(value)
            except ElementTypeError as error:
                return str(error)
            return self._violation(element.code, element.width)
        if isinstance(value, str):
            return f"must be an int, got {value!r}; only a parameter declared element_type accepts a type spelling"
        return self._violation(value, value)

    def normalize(self, value: int | str) -> int:
        """The integer the RTL elaborates with, however the value was written.

        A consumer holding a raw override -- a build task assembling
        ``-generic`` arguments, say -- has to turn a spelling into its code
        before it reaches a tool, and cannot import this package to do it:
        dau-build is public and the encoding lives here. Asking the spec that
        declared the parameter keeps the vocabulary in one place.
        """
        if self.element_type:
            return element_code(value)
        if isinstance(value, str):
            # the same rule violation() states: a spelling is only for element types
            raise TypeError(f"must be an int, got {value!r}; only a parameter declared element_type accepts a type spelling")
        return value

    def _violation(self, value: int, magnitude: int) -> str | None:
        """``value`` is what the RTL elaborates with; ``magnitude`` is what the
        bounds constrain -- the same number for a size parameter, and the
        element type's width for a type parameter."""
        if self.choices is not None:
            if value not in self.choices:
                allowed = ", ".join(self._spell(choice) for choice in self.choices)
                return f"must be one of {allowed}, got {self._spell(value)}"
            return None  # an enumerated choice set (which may include a 0 flag) is the whole constraint
        if magnitude <= 0 and self.minimum > 0:
            # a spec that declares minimum 0 mirrors an HDL guard that ADMITS
            # zero (the fixed-point tiles' FRACTIONAL_BITS is [0, 31]); the
            # blanket positive-int rule would make that bound unreachable
            return f"must be a positive int, got {self._spell(value)}"
        if magnitude < self.minimum:
            return f"must be >= {self.minimum}, got {self._spell(value)}"
        if self.maximum is not None and magnitude > self.maximum:
            return f"must be <= {self.maximum}, got {self._spell(value)}"
        if magnitude % self.multiple_of != 0:
            return f"must be a positive multiple of {self.multiple_of}, got {self._spell(value)}"
        if self.power_of_two and magnitude & (magnitude - 1):
            return f"must be a power of two, got {self._spell(value)}"
        return None

    def _spell(self, value: int) -> str:
        """How a value appears in a message: a type parameter's values are
        named, because '4128' tells a reader nothing and 'q16.16' does."""
        if not self.element_type:
            return repr(value)
        try:
            return decode_element_type(value).name
        except ElementTypeError:
            return repr(value)

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
            raise ValueError(f"default {self._spell(self.default)} violates the spec's own constraints: {reason}")
        return self


class StorageIntent(CheckedModel):
    """A core's declared storage need (STORAGE_AND_NOC.md). ``owned`` = a
    private single-cycle arena; ``shared`` = handle-based tier transactions.
    Capacity prices against a declared module parameter so the planner can
    refuse what no tier serves — never discovered at synthesis."""

    role: str = Field(min_length=1)
    access: Literal["owned", "shared"]
    bytes_per_unit: int = Field(gt=0)
    scale_parameter: str | None = None
    tier_hint: str | None = None


def _column_names(node: Any) -> list[str]:
    """Every column a serialized polars expression names, in tree order."""
    if isinstance(node, dict):
        if set(node) == {"Column"} and isinstance(node["Column"], str):
            return [node["Column"]]
        return [name for value in node.values() for name in _column_names(value)]
    if isinstance(node, list):
        return [name for value in node for name in _column_names(value)]
    return []


def _json_size(node: Any) -> int:
    if isinstance(node, dict):
        return 1 + sum(_json_size(value) for value in node.values())
    if isinstance(node, list):
        return sum(_json_size(value) for value in node)
    return 0


def _freeze_json(node: Any) -> Any:
    """A JSON tree with every object a ``FrozenMapping`` and every array a
    tuple: read-only, and hashable."""
    if isinstance(node, Mapping):
        return FrozenMapping({key: _freeze_json(value) for key, value in node.items()})
    if isinstance(node, (list, tuple)):
        return tuple(_freeze_json(value) for value in node)
    return node


def _thaw_json(node: Any) -> Any:
    """A fresh plain-JSON copy of a tree :func:`_freeze_json` built."""
    if isinstance(node, Mapping):
        return {key: _thaw_json(value) for key, value in node.items()}
    if isinstance(node, tuple):
        return [_thaw_json(value) for value in node]
    return node


class ColumnBinding(CheckedModel):
    """Where a tile reads a column: the config port selecting its quad-row
    field and, for a column packed into a field with others (a key
    component), the config port taking its bit offset and the module
    parameter fixing its width. The group code of an aggregate is read the
    same way; its field is the key's, so it names no field port."""

    field: str | None = None
    shift: str | None = None
    width: str | None = None

    def ports(self) -> tuple[str, ...]:
        return tuple(port for port in (self.field, self.shift, self.width) if port is not None)


class ExpressionLowering(CheckedModel):
    """The polars expression a row tile computes, declared so a query
    compiler can reach the tile without being changed for it.

    ``expression`` is a polars expression over placeholder columns, given as
    source text (``'pl.col("value").clip(pl.col("lower"), pl.col("upper"))'``,
    evaluated through ccflow's ``PolarsExpression`` where polars is
    installed) or as a polars expression, which is kept as polars' own JSON.
    Text is the portable form: polars' JSON can change between versions, and
    a pattern only matches a query serialized by the same version.

    Each column the expression names is a placeholder. A compiler matches a
    query expression against it node for node: a placeholder listed in
    ``literals`` matches a literal in the query, any other placeholder
    matches a column or a sub-expression the compiler computes first, and
    every other node must be the same. ``bind`` names the config port each
    placeholder is wired to: a value port for a literal, and for a column a
    field select, or a :class:`ColumnBinding` for a column packed with
    others into one field.

    A row expression is computed in place: the tile writes
    ``destination_port``'s field of each quad row, ``row[dst] =
    golden(**args)``, the core's ``golden`` called with the placeholders by
    name. An aggregation (``pl.col("value").n_unique()``) makes the tile a
    lane terminal: it reduces each group's rows, reading the group code
    where ``group`` says, and emits one record per group.

    A compiler lowers the expression only when it can show the tile computes
    what polars would. Every bound column must hold values (not dictionary
    codes) at one shared fixed-point scale, and every literal must encode
    exactly at that scale within the field. A nonzero scale also needs
    ``scale_invariant``, meaning ``f(k*x, k*s...) == k*f(x, s...)`` for
    k > 0: true of clip, false of a product or a hash.
    """

    # polars' JSON is stored frozen and serialized back as plain JSON
    expression: Annotated[str | Mapping[str, Any], AfterValidator(_freeze_json), PlainSerializer(_thaw_json)]
    literals: tuple[str, ...] = ()
    bind: Annotated[Mapping[str, str | ColumnBinding], *ReadOnly]
    destination_port: str | None = None
    group: ColumnBinding | None = None
    scale_invariant: bool = False

    @field_validator("expression", mode="before")
    @classmethod
    def _expression_form(cls, value: Any) -> Any:
        # a polars expression (or a ccflow PolarsExpression) is kept as the
        # JSON polars serializes it to, so this module never imports polars
        meta = getattr(value, "meta", None)
        if meta is not None and hasattr(meta, "serialize"):
            import json

            return json.loads(meta.serialize(format="json"))
        return value

    def expression_json(self) -> dict[str, Any]:
        """The expression as polars serializes it, as a new dict on every
        call. Text is evaluated through ccflow's ``PolarsExpression``, which
        needs polars installed."""
        if not isinstance(self.expression, str):
            return _thaw_json(self.expression)
        import json

        from ccflow.exttypes.polars import PolarsExpression
        from pydantic import TypeAdapter

        expression = TypeAdapter(PolarsExpression).validate_python(self.expression)
        return json.loads(expression.meta.serialize(format="json"))

    def placeholders(self) -> tuple[str, ...]:
        """Every placeholder, in the order a match binds them."""
        return tuple(dict.fromkeys(_column_names(self.expression_json())))

    def columns(self) -> tuple[str, ...]:
        """The placeholders that stand for columns, in match order."""
        return tuple(name for name in self.placeholders() if name not in self.literals)

    def size(self) -> int:
        """Nodes in the expression: a compiler tries larger patterns first."""
        return _json_size(self.expression_json())

    def is_aggregate(self) -> bool:
        """Whether the expression is an aggregation, which makes the tile a
        lane terminal rather than a stage that rewrites a field."""
        return "Agg" in self.expression_json()

    def binding(self, placeholder: str) -> ColumnBinding:
        """Where the tile reads a column placeholder."""
        bound = self.bind[placeholder]
        return bound if isinstance(bound, ColumnBinding) else ColumnBinding(field=bound)

    @model_validator(mode="after")
    def _bound_once(self) -> ExpressionLowering:
        ports = [port for bound in self.bind.values() for port in ((bound,) if isinstance(bound, str) else bound.ports())]
        ports += [*(self.group.ports() if self.group is not None else ()), *((self.destination_port,) if self.destination_port else ())]
        if len(set(ports)) != len(ports):
            raise ValueError(f"a config port is bound twice: {tuple(ports)!r}")
        for placeholder in self.literals:
            if not isinstance(self.bind.get(placeholder, ""), str):
                raise ValueError(f"literal placeholder {placeholder!r} binds to one value port, not a column binding")  # noqa: TRY004  # pydantic reports ValueError
        try:
            placeholders = self.placeholders()
        except ImportError:
            # text without polars installed: the compiler, which has polars,
            # checks the placeholders when it reads the lowering
            return self
        except Exception as error:
            raise ValueError(f"the lowering's expression is not a polars expression: {error}") from error
        if set(self.bind) != set(placeholders):
            raise ValueError(f"bind must name every placeholder exactly: the expression has {sorted(placeholders)}, bind has {sorted(self.bind)}")
        if not set(self.literals) <= set(placeholders):
            raise ValueError(f"literals {sorted(set(self.literals) - set(placeholders))} are not placeholders of the expression")
        if not self.columns():
            raise ValueError("the expression needs at least one column placeholder")
        for placeholder in self.columns():
            if self.binding(placeholder).field is None:
                raise ValueError(f"column placeholder {placeholder!r} names no field port")
        if self.is_aggregate():
            if self.group is None or self.destination_port is not None:
                raise ValueError("an aggregation is a lane terminal: it declares where it reads the group code and writes no field")
        elif self.destination_port is None or self.group is not None:
            raise ValueError("a row expression writes destination_port's field and reads no group code")
        return self


def _same_clock(a: float, b: float) -> bool:
    """Two clock periods naming the same physical clock.

    The unit is NANOSECONDS, so the tolerance is a picosecond: 6.66667 against
    a derived 6.666666666666667 differs by 3.3e-6 ns. Clock tiers in this
    registry are more than a nanosecond apart (8.0 vs 6.667 vs 10.0), so a
    picosecond is a thousand times too small to reach a neighbour.
    """
    return abs(a - b) <= 1e-3


# Prefix on every `measured_from` digest, for the reason
# SPEC_CANONICAL_VERSION carries one: a deliberate change to HOW the closure
# is digested must be distinguishable from a change to the RTL it digests.
# Bumping it invalidates every stamp at once, which is correct -- no stamp
# written under the old rule means what a reader of the new rule would think.
HDL_CLOSURE_DIGEST_VERSION = "1"

# `include "types.svh" -- the only include form the palette uses, and the only
# one the toolchain is given a search path for.
_HDL_INCLUDE = re.compile(r'^\s*`include\s+"([^"]+)"', re.MULTILINE)


class CoreDefinition(CheckedModel):
    """One HDL building block and its contract surround."""

    name: str
    kind: CoreKind
    module: str
    source: str
    # The package `source` is resolved inside. Declared, not assumed: the
    # registry is a hydra search path, so a package that joins it through
    # the `hydra.lernaplugins` entry point contributes cores whose HDL
    # lives in ITS tree, not this one. Resolving every core's source under
    # dau_core.hdl meant a tile could be registered from anywhere but its
    # Verilog had to be vendored here.
    source_package: str = "dau_core.hdl"
    # Set when the HDL is RENDERED rather than checked in, because its
    # content is a function of configuration -- a transcendental table
    # cannot be computed at elaboration. `source` then names the file it
    # renders TO, so staging, dependency closure and synthesis reach it
    # through the same seam as every hand-written tile.
    generator: CoreGenerator | None = None
    description: str
    depends: tuple[str, ...] = ()
    golden: str | None = None  # "module:callable" defining the semantics
    bench: str | None = None  # cocotb bench module
    resources: tuple[ResourceMeasurement, ...] | None = None
    # The HDL closure digest (:meth:`hdl_closure_digest`) the numbers in
    # `resources` describe. It is what makes a recorded envelope falsifiable
    # without Vivado: the numbers cannot say which RTL produced them, so a
    # commit that edits a tile and leaves its envelope alone is invisible
    # until someone next runs synthesis. Stamping the RTL beside the numbers
    # turns that into a test failure naming the tile.
    #
    # None means UNSTAMPED -- the state every point recorded before this field
    # existed is in. It is permitted, and pinned as an exact set by
    # `test_measured_from_matches_the_hdl` so the population can only shrink.
    measured_from: str | None = None
    count_port: str | None = None  # the tile's trailing status counter, if any
    contract_kind: ContractKind | None = None  # required for operators; None elsewhere (no declared stream contract)
    # the composition job this core fills, when a composition rule turns on it
    # (None = no rule does; most cores)
    role: CoreRole | None = None
    # For a tile whose golden is a mathematical reference rather than an
    # integer identity (the transcendentals): the worst deviation from the
    # correctly rounded answer, in ULP, over an exhaustive sweep of the
    # domain. 0 means correctly rounded. A fact of the operating point,
    # measured and recorded here beside the resources, and pinned by the
    # suite so a datapath change that moves it is a change to an
    # advertised fact.
    accuracy_ulp: int | None = None
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
    parameters: Annotated[Mapping[str, ParameterSpec], *ReadOnly] = Field(default_factory=FrozenMapping)
    # declared storage needs; the walker wires them, contracts advertise them
    storage_intent: tuple[StorageIntent, ...] = ()
    # the polars expression this tile computes, for a compiler to discover
    lowering: ExpressionLowering | None = None

    def storage_bytes(self, params: Mapping[str, int]) -> tuple[tuple[StorageIntent, int], ...]:
        """Each intent with its byte cost at the effective parameters
        (overrides win, declared defaults otherwise)."""
        priced = []
        for intent in self.storage_intent:
            units = 1
            if intent.scale_parameter is not None:
                spec = self.parameters[intent.scale_parameter]
                override = params.get(intent.scale_parameter, spec.default)
                if isinstance(override, bool) or not isinstance(override, int):
                    raise ValueError(
                        f"core {self.name!r} storage intent {intent.role!r}: parameter {intent.scale_parameter!r} override {override!r} is not an int"
                    )
                # the spec owns the constraint; pricing an illegal override
                # would produce a byte cost no buildable design can have
                reason = spec.violation(override)
                if reason is not None:
                    raise ValueError(f"core {self.name!r} storage intent {intent.role!r}: parameter {intent.scale_parameter!r} {reason}")
                units = override
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
        if self.lowering is not None:
            try:
                aggregate = self.lowering.is_aggregate()
            except ImportError:
                # text without polars installed: the compiler checks the shape
                aggregate = None
            if aggregate is False and (self.contract_kind != ContractKind.ROW_STREAM or self.record_words != 2):
                raise ValueError(
                    f"core {self.name!r} declares a lowering, which rewrites a quad row in place; it must be a row-stream tile of 2 words"
                )
            if aggregate and self.contract_kind not in (ContractKind.ROW_STREAM, ContractKind.WIDE_ROW_STREAM):
                raise ValueError(
                    f"core {self.name!r} declares an aggregation, which terminates a lane; it must be a row-stream or wide-row-stream tile"
                )
            if self.golden is None:
                raise ValueError(f"core {self.name!r} declares a lowering but no golden element function")
        if self.supports_pipeline_depth and "PIPELINE_DEPTH" not in self.parameters:
            raise ValueError(f"core {self.name!r} supports pipeline depth but declares no PIPELINE_DEPTH parameter")
        for intent in self.storage_intent:
            if intent.scale_parameter is not None and intent.scale_parameter not in self.parameters:
                raise ValueError(
                    f"core {self.name!r} storage intent {intent.role!r} scales by "
                    f"scale_parameter {intent.scale_parameter!r}, which is not a declared parameter"
                )
        if self.measured_from is not None and self.resources is None:
            raise ValueError(
                f"core {self.name!r} stamps measured_from but records no resources; the stamp says which RTL a "
                "measurement describes and there is no measurement"
            )
        if self.resources is not None:
            expected_params = set(self.parameters)
            coordinates: set[tuple[str, float | None, tuple[tuple[str, int], ...]]] = set()
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
        comparison. An unclocked (combinational) point answers for any clock.

        Clock periods are DERIVED (1000 / job_clock_mhz), so they are compared
        to the picosecond rather than by float equality: 150 MHz yields
        6.666666666666667 while a registry writes 6.66667, and exact equality
        rejected a point while printing it in the same breath. Real clock tiers
        are more than a nanosecond apart, so this cannot reach a neighbour.
        """
        requested = {name: int(params.get(name, spec.default)) for name, spec in self.parameters.items()}
        requested.update({name: int(value) for name, value in params.items() if name not in requested})
        if not self.resources:
            raise UnmeasuredResourceError(
                f"core {self.name!r} is unmeasured for part={part!r}, clock_ns={clock_ns:g}, params={requested!r}; it records no measurement points"
            )

        for point in self.resources:
            if point.part == part and point.answers_clock(clock_ns) and dict(point.params) == requested:
                return point

        part_points = [point for point in self.resources if point.part == part]
        if not part_points:
            gap = f"part {part!r}"
        else:
            clock_points = [point for point in part_points if point.answers_clock(clock_ns)]
            gap = f"clock_ns {clock_ns:g} on part {part!r}" if not clock_points else f"params {requested!r} at part={part!r}, clock_ns={clock_ns:g}"
        available = ", ".join(
            f"(part={point.part!r}, clock_ns={'unclocked' if point.clock_ns is None else format(point.clock_ns, 'g')}, params={dict(point.params)!r})"
            for point in self.resources
        )
        raise UnmeasuredResourceError(f"core {self.name!r} has unmeasured {gap}; measured points: {available or 'none'}")

    def source_path(self, *, rendered_into: Path | None = None) -> Path:
        """The HDL this core synthesizes from.

        A GENERATED core (one declaring a ``generator``) has no checked-in
        file, so it refuses until it has been rendered rather than rendering
        implicitly on first read. What sits on disk is what gets synthesized;
        rendering behind a read would hide which parameters produced it, and
        the whole reason this core is generated is that its constants are a
        function of those parameters.

        ``rendered_into`` is the directory a generated core was rendered to
        (``render(into=)``); it defaults to :func:`render_root`. A build tree
        renders into its own directory and passes it here, so two builds never
        share one file.
        """
        if self.generator is not None:
            rendered = (rendered_into if rendered_into is not None else render_root(self.source_package)) / self.source
            if not rendered.is_file():
                raise UnrenderedCoreError(
                    f"core {self.name!r} is generated and has not been rendered to {rendered}. "
                    "Render it first: dau-build task=tasks/build/render-cores"
                )
            return rendered
        checked_in = package_root(self.source_package) / self.source
        if not checked_in.is_file():
            # An out-of-tree tile whose HDL directory is not an importable
            # package resolves to a MultiplexedPath that PRINTS correctly and
            # does not exist, so the first symptom is a synthesis run against a
            # missing file rather than a declaration error. Say it here.
            raise UnknownCoreSourceError(
                f"core {self.name!r} declares source {self.source!r} in package {self.source_package!r}, which does not "
                f"resolve to a file ({checked_in}). An HDL directory must be an importable package: add an __init__.py "
                "beside the .sv files so importlib can traverse it"
            )
        return checked_in

    def hdl_closure(self, resolve) -> tuple[tuple[str, str], ...]:
        """Every HDL file this core synthesizes from, as ``(name, sha256)``
        pairs sorted by name.

        WHAT IS IN THE CLOSURE is the whole question, and the answer is: the
        files a synthesis run of THIS core reads. That is its own source, the
        source of every core reachable through ``depends`` (the packages it
        imports), and every header those files `` `include ``, resolved the
        way the toolchain resolves them -- beside the including file first,
        then along the other source directories, which is exactly the
        ``-include_dirs`` list the synthesis task builds. A header nothing
        includes is NOT in the closure, so adding a second one to the palette
        does not re-measure every tile that ignores it; and a package a tile
        imports IS, so an edit there reaches every tile that reads it.

        ``resolve`` maps a core name to its definition. It is a parameter
        rather than a registry import because the build package reaches these
        models through the composed ccflow registry and must not import this
        one.

        A GENERATED core is hashed from its RENDERED TEXT, not from a file on
        disk: its RTL is a function of the generator and of the constants in
        its registry entry, so an edit to either changes what synthesis reads
        while every checked-in file stays byte-identical.
        """
        models: list[CoreDefinition] = []
        seen: set[str] = set()

        def visit(name: str) -> None:
            if name in seen:
                return
            seen.add(name)
            model = resolve(name)
            if model is None:
                raise UnknownCoreSourceError(
                    f"core {self.name!r} depends on {name!r}, which the registry does not resolve; its HDL closure cannot be digested"
                )
            for dependency in model.depends:
                visit(dependency)
            models.append(model)

        visit(self.name)

        pending: list[tuple[str, str, Path, bytes]] = []
        directories: dict[Path, str] = {}
        for model in models:
            if model.generator is not None:
                directory = render_root(model.source_package)
                data = model.generator.render().text.encode("utf-8")
            else:
                path = model.source_path()
                directory = path.parent
                data = path.read_bytes()
            directories.setdefault(directory, model.source_package)
            pending.append((model.source_package, model.source, directory, data))

        entries: dict[str, str] = {}
        while pending:
            package, name, directory, data = pending.pop(0)
            key = f"{package}/{name}"
            if key in entries:
                continue
            entries[key] = hashlib.sha256(data).hexdigest()
            for included in _HDL_INCLUDE.findall(data.decode("utf-8")):
                for candidate_dir in (directory, *directories):
                    candidate = candidate_dir / included
                    if candidate.is_file():
                        pending.append((directories.get(candidate_dir, package), included, candidate_dir, candidate.read_bytes()))
                        break
                else:
                    raise UnknownCoreSourceError(
                        f"core {self.name!r} reads {name!r}, which includes {included!r}; that header resolves in none of "
                        f"{sorted(str(d) for d in directories)}, so the HDL closure is not closed"
                    )
        return tuple(sorted(entries.items()))

    def hdl_closure_digest(self, resolve) -> str:
        """The digest a measured envelope records as ``measured_from``.

        Over BYTES, deliberately. A digest cannot tell a retimed adder from a
        renamed module, and pretending otherwise would mean teaching it which
        edits are allowed to be free -- which is the judgement the guard
        exists to force someone to make and write down. So a rename moves the
        digest too, and re-stamping it costs an argument in the pull request.
        """
        payload = "".join(f"{name}\0{digest}\n" for name, digest in self.hdl_closure(resolve))
        return f"{HDL_CLOSURE_DIGEST_VERSION}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"

    def render(self, *, into: Path | None = None) -> Path:
        """Render this core's HDL, returning the written path."""
        if self.generator is None:
            raise ValueError(f"core {self.name!r} is not generated; its source is checked in at {self.source_path()}")
        return self.generator.render().write(into if into is not None else render_root())

    def golden_callable(self):
        if self.golden is None:
            return None
        module_name, _, attr = self.golden.partition(":")
        return getattr(import_module(module_name), attr)


class UnknownCoreSourceError(RuntimeError):
    """A core names an HDL package that is not installed."""


class UnrenderedCoreError(RuntimeError):
    """A generated core was asked for its source before it was rendered."""


class UnmeasuredResourceError(LookupError):
    pass


class TimingViolationError(ValueError):
    pass


def package_root(package: str) -> Path:
    """The on-disk directory of an HDL package.

    Resolved through importlib rather than a relative path from this file,
    so a core contributed by another distribution finds its own tree.
    """
    from importlib.resources import files

    try:
        return Path(str(files(package)))
    except ModuleNotFoundError as error:
        raise UnknownCoreSourceError(
            f"HDL package {package!r} is not importable. A core declaring source_package must ship it: "
            "register the package on the hydra search path (the `hydra.lernaplugins` entry point) and install it"
        ) from error


def render_root(package: str = "dau_core.hdl") -> Path:
    """Where generated cores are materialized.

    Beside the declaring package, so a checkout behaves like one with the
    files committed. A build tree renders elsewhere and passes that directory
    to :meth:`CoreDefinition.source_path`.
    """
    return package_root(package) / "generated"
