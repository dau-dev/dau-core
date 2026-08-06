from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import IntEnum

from ccflow import BaseModel
from pydantic import ConfigDict

from .stream import LogicalType, OperationCode

FILTER_CONFIG_VERSION = "0.1"


class PredicateOp(IntEnum):
    """Comparison opcodes for the predicate filter tile. Zero is reserved so an
    unconfigured tile fails explicitly."""

    EQ = 1
    NE = 2
    LT = 3
    LE = 4
    GT = 5
    GE = 6


class PredicateCombine(IntEnum):
    SINGLE = 0
    AND = 1
    OR = 2


class FilterErrorCode(IntEnum):
    CONFIG = 1
    STREAM = 2


class FilterPredicate(BaseModel):
    """Predicate configuration: ``value <op0> constant0`` optionally combined
    with ``value <op1> constant1`` by AND/OR. Matches the tile's config ports."""

    model_config = ConfigDict(frozen=True)

    op0: PredicateOp
    constant0: int
    combine: PredicateCombine = PredicateCombine.SINGLE
    op1: PredicateOp = PredicateOp.EQ
    constant1: int = 0

    def model_post_init(self, context) -> None:
        super().model_post_init(context)
        for name in ("constant0", "constant1"):
            value = getattr(self, name)
            if not -(1 << 31) <= value <= (1 << 31) - 1:
                raise ValueError(f"{name} must fit in int32")

    def matches(self, value: int) -> bool:
        first = _compare(self.op0, value, self.constant0)
        if self.combine == PredicateCombine.SINGLE:
            return first
        second = _compare(self.op1, value, self.constant1)
        return (first and second) if self.combine == PredicateCombine.AND else (first or second)


def filter_values(values: Iterable[int], predicate: FilterPredicate) -> list[int]:
    """Golden semantics for the compacting predicate filter tile: emit the
    matching values in input order."""
    return [value for value in values if predicate.matches(value)]


def _compare(op: PredicateOp, value: int, constant: int) -> bool:
    if op == PredicateOp.EQ:
        return value == constant
    if op == PredicateOp.NE:
        return value != constant
    if op == PredicateOp.LT:
        return value < constant
    if op == PredicateOp.LE:
        return value <= constant
    if op == PredicateOp.GT:
        return value > constant
    if op == PredicateOp.GE:
        return value >= constant
    raise ValueError(f"unsupported predicate op {op!r}")


MAP_CONFIG_VERSION = "0.1"

_INT32_MASK = 0xFFFF_FFFF


class MapOp(IntEnum):
    """ALU opcodes for the map tile. Zero is reserved so an unconfigured tile
    fails explicitly."""

    ADD = 1
    SUB = 2
    MUL = 3
    MULSHR = 4  # (value * constant) >> shift: multiply-shift floor division / bucket derivation
    SHL = 5
    AND = 6
    OR = 7


class MapErrorCode(IntEnum):
    CONFIG = 1
    STREAM = 2


def _apply_map_op(op: MapOp, value: int, constant: int, shift: int) -> int:
    """The tile ALU: shared by the scalar and row map configs (a plain
    function so per-row golden loops never construct a model)."""
    if op == MapOp.ADD:
        return _wrap_int32(value + constant)
    if op == MapOp.SUB:
        return _wrap_int32(value - constant)
    if op == MapOp.MUL:
        return _wrap_int32(value * constant)
    if op == MapOp.MULSHR:
        # 64-bit signed product, arithmetic shift right
        return _wrap_int32((value * constant) >> shift)
    if op == MapOp.SHL:
        return _wrap_int32(value << shift)
    if op == MapOp.AND:
        return _wrap_int32((value & _INT32_MASK) & (constant & _INT32_MASK))
    if op == MapOp.OR:
        return _wrap_int32((value & _INT32_MASK) | (constant & _INT32_MASK))
    raise ValueError(f"unsupported map op {op!r}")


class MapConfig(BaseModel):
    """Per-row ALU configuration: ``value <op> constant`` (MULSHR/SHL also use
    ``shift``). All arithmetic is INT32 two's-complement wraparound — the
    golden and the tile agree bit-for-bit. Matches the tile's config ports."""

    model_config = ConfigDict(frozen=True)

    op: MapOp
    constant: int = 0
    shift: int = 0

    def model_post_init(self, context) -> None:
        super().model_post_init(context)
        if not -(1 << 31) <= self.constant <= (1 << 31) - 1:
            raise ValueError("constant must fit in int32")
        if not 0 <= self.shift <= 31:
            raise ValueError("shift must be in [0, 31]")

    def apply(self, value: int) -> int:
        return _apply_map_op(self.op, value, self.constant, self.shift)


def map_values(values: Iterable[int], config: MapConfig) -> list[int]:
    """Golden semantics for the map/ALU tile: apply the configured ALU
    operation to every value, preserving order and count."""
    return [config.apply(value) for value in values]


def _wrap_int32(value: int) -> int:
    value &= _INT32_MASK
    return value - (1 << 32) if value >= (1 << 31) else value


def filter_rows(rows: Iterable[tuple[int, int]], predicate: FilterPredicate, *, on_key: bool = False) -> list[tuple[int, int]]:
    """Golden semantics for the keyed predicate-filter tile: the predicate is
    evaluated on the value lane (or the key lane with ``on_key``) and whole
    ``(key, value)`` rows pass or drop together — compaction never misaligns
    the lanes."""
    return [(key, value) for key, value in rows if predicate.matches(key if on_key else value)]


def map_rows(rows: Iterable[tuple[int, int]], config: MapConfig, *, on_key: bool = False) -> list[tuple[int, int]]:
    """Golden semantics for the keyed map/ALU tile: the ALU operation applies
    to the value lane (or the key lane with ``on_key`` — e.g. time→bucket
    derivation); the other lane passes through unchanged, order and count
    preserved."""
    if on_key:
        return [(config.apply(key), value) for key, value in rows]
    return [(key, config.apply(value)) for key, value in rows]


class RowField(IntEnum):
    """Field selectors for 16-byte quad rows (two 64-bit words,
    w0 = {value, key}, w1 = {aux1, aux0}): field i is the i-th 32-bit lane
    in row order (key, value, aux0, aux1)."""

    KEY = 0
    VALUE = 1
    AUX0 = 2
    AUX1 = 3


class RowPredicateTerm(BaseModel):
    """One comparison term of a row predicate: ``field <op> constant``
    (signed 32-bit). Matches one term's config-port triple on the tile."""

    model_config = ConfigDict(frozen=True)

    field: RowField
    op: PredicateOp
    constant: int

    def model_post_init(self, context) -> None:
        super().model_post_init(context)
        if not -(1 << 31) <= self.constant <= (1 << 31) - 1:
            raise ValueError("constant must fit in int32")

    def matches(self, row: tuple[int, int, int, int]) -> bool:
        return _compare(self.op, _wrap_int32(row[self.field]), self.constant)


class RowPredicateConfig(BaseModel):
    """Compound row predicate: up to three terms combined by the two-level
    tree ``(term0 <combine01> term1) <combine2> term2``. ``SINGLE`` at a
    level ignores the right-hand term (the 2-term filter's combine,
    extended to three terms). Matches the tile's config ports."""

    model_config = ConfigDict(frozen=True)

    term0: RowPredicateTerm
    combine01: PredicateCombine = PredicateCombine.SINGLE
    term1: RowPredicateTerm | None = None
    combine2: PredicateCombine = PredicateCombine.SINGLE
    term2: RowPredicateTerm | None = None

    def model_post_init(self, context) -> None:
        super().model_post_init(context)
        if self.combine01 != PredicateCombine.SINGLE and self.term1 is None:
            raise ValueError("combine01 requires term1")
        if self.combine2 != PredicateCombine.SINGLE and self.term2 is None:
            raise ValueError("combine2 requires term2")

    def matches(self, row: tuple[int, int, int, int]) -> bool:
        result = self.term0.matches(row)
        if self.combine01 != PredicateCombine.SINGLE:
            second = self.term1.matches(row)
            result = (result and second) if self.combine01 == PredicateCombine.AND else (result or second)
        if self.combine2 != PredicateCombine.SINGLE:
            third = self.term2.matches(row)
            result = (result and third) if self.combine2 == PredicateCombine.AND else (result or third)
        return result


def filter_rows_by_predicate(rows: Iterable[tuple[int, int, int, int]], config: RowPredicateConfig) -> list[tuple[int, int, int, int]]:
    """Golden semantics for the row predicate filter tile: whole
    (key, value, aux0, aux1) rows pass when the compound predicate over the
    selected fields matches (signed 32-bit compares), order preserved."""
    return [tuple(row) for row in rows if config.matches(tuple(row))]


class RowMapConfig(BaseModel):
    """Per-row field ALU configuration: ``dst = src_a <op> src_b`` over the
    four row fields (MULSHR/SHL also use ``shift``; SHL ignores ``src_b``).
    All arithmetic is INT32 two's-complement wraparound — the golden and the
    tile agree bit-for-bit. Matches the tile's config ports."""

    model_config = ConfigDict(frozen=True)

    op: MapOp
    dst: RowField
    src_a: RowField
    src_b: RowField
    shift: int = 0

    def model_post_init(self, context) -> None:
        super().model_post_init(context)
        if not 0 <= self.shift <= 31:
            raise ValueError("shift must be in [0, 31]")

    def apply(self, row: tuple[int, int, int, int]) -> int:
        a = _wrap_int32(row[self.src_a])
        b = _wrap_int32(row[self.src_b])
        return _apply_map_op(self.op, a, b, self.shift)


def map_row_fields(rows: Iterable[tuple[int, int, int, int]], config: RowMapConfig) -> list[tuple[int, int, int, int]]:
    """Golden semantics for the row map/ALU tile: the configured destination
    field is rewritten from the two source-field operands; the other fields
    pass through unchanged, order and count preserved."""
    out: list[tuple[int, int, int, int]] = []
    for row in rows:
        mapped = list(row)
        mapped[config.dst] = config.apply(tuple(row))
        out.append(tuple(mapped))
    return out


UNPACK_CONFIG_VERSION = "0.1"


class UnpackErrorCode(IntEnum):
    CONFIG = 1
    # bypass mode consumes 16-byte quad rows (two words each); input_last on a
    # w0 is a torn row, invalidating the batch — the quad-row-tile convention
    STREAM = 2


class UnpackFieldSpec(BaseModel):
    """One output field's slice of a packed row: the ``width``-bit field at bit
    ``offset`` of the (<=64-bit) packed word, sign-extended (``signed``) or
    zero-extended to int32. Matches the tile's
    ``cfg_field{i}_{offset,width,signed}`` config ports."""

    model_config = ConfigDict(frozen=True)

    offset: int
    width: int
    signed: bool = False

    def model_post_init(self, context) -> None:
        super().model_post_init(context)
        if not 0 <= self.offset <= 63:
            raise ValueError("offset must be in [0, 63]")
        if not 1 <= self.width <= 32:
            raise ValueError("width must be in [1, 32]")
        if self.offset + self.width > 64:
            raise ValueError("offset + width must not exceed 64")

    def extract(self, packed: int) -> int:
        """Extract this field from ``packed`` as an int32-domain signed value
        (the exact 32-bit pattern the tile emits, read back as signed int32)."""
        raw = (packed >> self.offset) & ((1 << self.width) - 1)
        if self.signed and (raw >> (self.width - 1)) & 1:
            raw -= 1 << self.width
        return _wrap_int32(raw)


def unpack_rows(
    rows: Iterable[int] | Iterable[tuple[int, int, int, int]],
    field_specs: Sequence[UnpackFieldSpec] = (),
    *,
    bypass: bool = False,
) -> list[tuple[int, int, int, int]]:
    """Golden semantics for the row-unpack front-end feed tile.

    UNPACK (``bypass=False``, the default): widen each packed input word (one
    <=64-bit packed row per word in ``rows``) into a 16-byte int32 quad row.
    Output field i is ``packed[offset_i +: width_i]`` sign/zero extended to
    int32; the four fields map to (key, value, aux0, aux1) — the exact quad-row
    layout downstream row tiles consume (field 0 = key = w0.lo). One packed word
    in, one quad row out, order and count preserved.

    BYPASS (``bypass=True``): the input is already 16-byte int32 quad rows
    (``rows`` is an iterable of (key, value, aux0, aux1) tuples); the tile
    forwards them byte-identically, so the golden is the identity. Field
    descriptors are unused in this mode (not required, not validated)."""
    if bypass:
        return [tuple(row) for row in rows]
    specs = tuple(field_specs)
    if len(specs) != 4:
        raise ValueError("row-unpack needs exactly four field specs (key, value, aux0, aux1)")
    return [tuple(spec.extract(word) for spec in specs) for word in rows]


class FieldSumErrorCode(IntEnum):
    CONFIG = 1
    STREAM = 2


@dataclass(frozen=True)
class FieldSumAggregate:
    """The one record the field-sum aggregation tile emits per batch: exact
    INT64 sums of the mask-selected fields (ascending field order) plus the
    row count. Record format on the wire: one 64-bit word per selected
    field (two's-complement sum, ascending field index), then the count
    word, ``output_last`` on the count word."""

    field_mask: int
    sums: tuple[int, ...]
    count: int

    def record_words(self) -> list[int]:
        """The record as unsigned 64-bit stream words (sums re-encoded to
        two's complement), count last."""
        return [sum_value & 0xFFFF_FFFF_FFFF_FFFF for sum_value in self.sums] + [self.count]


def field_sum_rows(rows: Iterable[tuple[int, int, int, int]], *, field_mask: int) -> FieldSumAggregate:
    """Golden semantics for the batch field-sum aggregation tile: one pass
    over (key, value, aux0, aux1) rows accumulating an INT64 sum per
    mask-selected field plus the row count. ``field_mask`` bit i selects
    field i; at least one field must be selected (a zero mask is an
    unconfigured tile and reports CONFIG on hardware)."""
    if not 1 <= field_mask <= 15:
        raise ValueError("field_mask must select at least one of the four fields")
    selected = [i for i in range(4) if field_mask & (1 << i)]
    sums = [0] * len(selected)
    count = 0
    for row in rows:
        for slot, field in enumerate(selected):
            sums[slot] += _wrap_int32(row[field])
        count += 1
    return FieldSumAggregate(field_mask=field_mask, sums=tuple(sums), count=count)


class GroupedFieldSumErrorCode(IntEnum):
    CONFIG = 1
    STREAM = 2


@dataclass(frozen=True)
class GroupedFieldSumAggregate:
    """One closed group from the ordered grouped field-sum aggregation
    tile: INT64 sums of the mask-selected fields (ascending field order)
    plus the group's row count — exact for every feasible batch (an INT64
    accumulator of int32 addends cannot overflow below 2**32 rows in one
    group, far past any stageable batch). Record format on the wire: the
    group key sign-extended to one 64-bit word, one 64-bit word per
    selected field (two's-complement sum, ascending field index), then the
    count word. ``output_last`` is set only on the final group's count
    word — the batch's group records form one record stream and the tile
    closes out once per batch, after the last group (the composition
    invariant: exactly one success close-out per lane per batch)."""

    key: int
    field_mask: int
    sums: tuple[int, ...]
    count: int

    def record_words(self) -> list[int]:
        """The group record as unsigned 64-bit stream words: sign-extended
        key, sums re-encoded to two's complement, count last."""
        mask64 = 0xFFFF_FFFF_FFFF_FFFF
        return [self.key & mask64] + [sum_value & mask64 for sum_value in self.sums] + [self.count]


def grouped_field_sum_rows(rows: Iterable[tuple[int, int, int, int]], *, field_mask: int) -> list[GroupedFieldSumAggregate]:
    """Golden semantics for the ordered grouped field-sum aggregation tile
    (close-out mode): consecutive (key, value, aux0, aux1) rows with equal
    signed 32-bit keys form a group; a group closes when the key changes or
    input ends, emitting an INT64 sum per mask-selected field plus the row
    count. A key that reappears later opens a NEW group — partials merge on
    the host, never in the tile (the partial-bar contract). ``field_mask``
    bit i selects field i; at least one field must be selected (a zero mask
    is an unconfigured tile and reports CONFIG on hardware)."""
    if not 1 <= field_mask <= 15:
        raise ValueError("field_mask must select at least one of the four fields")
    selected = [i for i in range(4) if field_mask & (1 << i)]
    results: list[GroupedFieldSumAggregate] = []
    current_key: int | None = None
    sums: list[int] = []
    count = 0
    for row in rows:
        key = _wrap_int32(row[0])
        if current_key is None or key != current_key:
            if current_key is not None:
                results.append(GroupedFieldSumAggregate(key=current_key, field_mask=field_mask, sums=tuple(sums), count=count))
            current_key = key
            sums = [0] * len(selected)
            count = 0
        for slot, field in enumerate(selected):
            sums[slot] += _wrap_int32(row[field])
        count += 1
    if current_key is not None:
        results.append(GroupedFieldSumAggregate(key=current_key, field_mask=field_mask, sums=tuple(sums), count=count))
    return results


MEMBERSHIP_CONFIG_VERSION = "0.1"


class MembershipKind(IntEnum):
    """Membership-structure kind for the key-set membership filter tile. Zero
    is reserved so an unconfigured tile fails explicitly. v0 implements only
    ``EXACT_BITMAP`` — a direct-mapped bitmap over the configured dictionary-id
    key space, with no false positives. ``BLOOM`` is a reserved future config (a
    hashed bitmap trading exactness for an unbounded key space, its false
    positives verified host-side); the v0 tile rejects it with a config
    error."""

    EXACT_BITMAP = 1
    BLOOM = 2  # reserved; not built in v0 (rejected as ERR_CONFIG)


class MembershipMode(IntEnum):
    """Probe-phase disposition — one config bit: keep members (semi/IN) or keep
    non-members (anti/NOT IN)."""

    SEMI = 0  # keep rows whose key is a member — semi-join / IN / distinct probe
    ANTI = 1  # keep rows whose key is NOT a member — anti-join / NOT IN


class MembershipErrorCode(IntEnum):
    CONFIG = 1  # unconfigured / unsupported membership kind (bloom is not built in v0)
    STREAM = 2  # torn probe row (input_last on a w0)
    LOAD = 3  # build-set image length != key_space // 64


@dataclass(frozen=True)
class MembershipBuildSet:
    """The membership memory: a direct-mapped EXACT bitmap over the configured
    dictionary-id key space ``[0, key_space)``. Key ``k`` is a member iff
    ``0 <= k < key_space`` and bit ``k`` is set.

    Addressing decision (and its exactness). The IN-list / semi-join key column
    is dictionary-encoded host-side (dau-polars) into dense ids ``[0, N)``, and
    ``key_space`` is that dictionary's cardinality rounded up to a multiple of
    64. Because every id maps to exactly one bit, membership is EXACT — no false
    positives AND no false negatives — over the id space. A full ``2**32``
    bitmap would be 512 MB, impossible; the bounded dictionary space is the
    natural fit and keeps the whole memory BRAM-resident. A probe key outside
    ``[0, key_space)`` is definitionally a non-member: the build set only ever
    holds in-range dictionary ids, so an out-of-range probe key (signed int32,
    so negatives and ids ``>= key_space`` included) cannot be in it. This is the
    honest, verifiable v0; ``MembershipKind.BLOOM`` is the future inexact
    alternative for unbounded key spaces (host-verified false positives).

    The bitmap loads as ``key_space // 64`` little-endian 64-bit image words:
    word ``w`` bit ``b`` is id ``64*w + b``."""

    key_space: int
    members: frozenset[int]

    def __post_init__(self) -> None:
        if self.key_space <= 0 or self.key_space % 64 != 0:
            raise ValueError("key_space must be a positive multiple of 64")
        for member in self.members:
            if not 0 <= member < self.key_space:
                raise ValueError(f"member {member} outside key space [0, {self.key_space})")

    @property
    def word_count(self) -> int:
        return self.key_space // 64

    def member(self, key: int) -> bool:
        return 0 <= key < self.key_space and key in self.members

    def image_words(self) -> list[int]:
        """The bitmap as ``key_space // 64`` little-endian 64-bit words — the
        LOAD-phase stream: word ``w`` bit ``b`` set iff ``64*w + b`` is a
        member."""
        words = [0] * self.word_count
        for member in self.members:
            words[member >> 6] |= 1 << (member & 0x3F)
        return words

    @classmethod
    def from_keys(cls, keys: Iterable[int], *, key_space: int) -> MembershipBuildSet:
        return cls(key_space=key_space, members=frozenset(keys))

    @classmethod
    def from_image_words(cls, words: Iterable[int], *, key_space: int) -> MembershipBuildSet:
        image = list(words)
        if len(image) != key_space // 64:
            raise ValueError(f"image has {len(image)} words, expected {key_space // 64}")
        members = {64 * index + bit for index, word in enumerate(image) for bit in range(64) if word & (1 << bit)}
        return cls(key_space=key_space, members=frozenset(members))


def filter_rows_by_membership(
    rows: Iterable[tuple[int, int, int, int]],
    build_set: MembershipBuildSet,
    *,
    mode: MembershipMode,
) -> list[tuple[int, int, int, int]]:
    """Golden semantics for the key-set membership filter tile (probe phase):
    whole (key, value, aux0, aux1) rows pass by whether the row key (``w0.lo``,
    signed int32) is a member of ``build_set``. ``SEMI`` keeps members, ``ANTI``
    keeps non-members; order preserved. Exact bitmap membership — no false
    positives."""
    out: list[tuple[int, int, int, int]] = []
    for row in rows:
        is_member = build_set.member(_wrap_int32(row[0]))
        if is_member == (mode == MembershipMode.SEMI):
            out.append(tuple(row))
    return out


class JoinSource(IntEnum):
    """Output field-select sources for the broadcast hash join tile: the six
    32-bit operands an emitted output field may carry — the matched build row's
    three data fields (``b0``/``b1``/``b2``) and the probe row's three
    (``v0``/``v1``/``v2``). Selectors 6 and 7 are reserved (the tile rejects
    them as ``ERR_CONFIG``). The emitted row's key is always the matched key,
    so it is not a selectable source."""

    B0 = 0  # build value (build row w0.hi)
    B1 = 1  # build aux0  (build row w1.lo)
    B2 = 2  # build aux1  (build row w1.hi)
    V0 = 3  # probe value (probe row w0.hi)
    V1 = 4  # probe aux0  (probe row w1.lo)
    V2 = 5  # probe aux1  (probe row w1.hi)


class JoinErrorCode(IntEnum):
    CONFIG = 1  # an output field-select is a reserved source (6 or 7)
    STREAM = 2  # torn row (input_last on a w0)
    RANGE = 3  # a build or probe key is outside [0, key_space)


class BroadcastJoinConfig(BaseModel):
    """Output field-select for the broadcast hash join: each of the emitted
    row's three data fields (``out0``, ``out1``, ``out2``) is chosen from the
    six join sources. The emitted key is always the matched probe key. Matches
    the tile's ``cfg_out{0,1,2}_sel`` config ports."""

    model_config = ConfigDict(frozen=True)

    out0: JoinSource
    out1: JoinSource
    out2: JoinSource

    def model_post_init(self, context) -> None:
        super().model_post_init(context)
        for sel in (self.out0, self.out1, self.out2):
            if not 0 <= int(sel) <= 5:
                raise ValueError("output field-select must be a JoinSource in [0, 5]")

    def select(self, sources: tuple[int, int, int, int, int, int]) -> tuple[int, int, int]:
        return (sources[self.out0], sources[self.out1], sources[self.out2])


def keyed_asof_join_rows(
    records: Iterable[tuple[int, int, int, int, int]], *, tolerance: int | None = None
) -> list[tuple[int, int, int, int, int, int]]:
    """Golden semantics for the generic keyed backward as-of join tile: one
    host-merged, ``(key, time)``-sorted stream of BUILD and PROBE records
    (build-before-probe at equal key/time). Each record is
    ``(rec_type, key, time, a, b)`` — ``rec_type`` bit0: 0 = build, 1 = probe;
    ``a``/``b`` are two opaque INT32 payload fields. Because the stream is
    key-grouped, exactly one build is live at a time: a build updates the held
    ``(time, a, b)`` for its key (reset on key change), and a probe attaches the
    most recent build with ``build_time <= probe_time`` (within ``tolerance`` on
    the time axis, ``None`` = unbounded), emitting the joined row
    ``(key, time, probe_a, probe_b, build_a, build_b)``. A probe with no live
    build (or one older than tolerance) drops — the tile emits matched joins
    only. Workload-agnostic: the temporal-finance backward as-of (quote = build
    ``{bid, ask}``, trade = probe ``{price, size}``) is exactly this join, and
    :func:`dau_core.temporal.asof_backward` on sorted inputs agrees with it."""
    out: list[tuple[int, int, int, int, int, int]] = []
    cur_key: int | None = None
    held: tuple[int, int, int] | None = None  # (build_time, a, b)
    for rec_type, key, time, a, b in records:
        if key != cur_key:
            cur_key = key
            held = None
        if rec_type & 1:  # probe
            if held is not None and (tolerance is None or time - held[0] <= tolerance):
                out.append((key, time, a, b, held[1], held[2]))
        else:  # build
            held = (time, a, b)
    return out


def broadcast_hash_join_rows(
    build_rows: Iterable[tuple[int, int, int, int]],
    probe_rows: Iterable[tuple[int, int, int, int]],
    *,
    out_sel: BroadcastJoinConfig,
    key_space: int = 1024,
) -> list[tuple[int, int, int, int]]:
    """Golden semantics for the broadcast hash join tile (the join tier's
    payload-emitting Joiner). The build side of ``(key, b0, b1, b2)`` rows is
    loaded into a direct-mapped payload table over the key space
    ``[0, key_space)`` — one slot per key, last-writer-wins on a duplicate
    build key (the build side is expected unique/PK). Then ``(key, v0, v1, v2)``
    probe rows inner-join against it: a probe key that hits emits one
    ``(key, out0, out1, out2)`` row whose three data fields are chosen by
    ``out_sel`` from the six sources ``(b0, b1, b2, v0, v1, v2)``; a probe miss
    drops. Order is preserved. Every build and probe key must lie in
    ``[0, key_space)`` — an out-of-range key is ``ERR_RANGE`` on the tile and
    raises here. All fields are INT32 (two's-complement wraparound).

    An empty ``build_rows`` is the mathematically empty table (every probe
    misses). On the tile, an empty table is the cleared/initial state reached
    by the load-phase sweep or power-on ``INIT`` — a *load batch* itself always
    carries at least one build row, since the stream contract delimits a batch
    with ``input_last`` on a data word and so cannot express a zero-row
    batch."""
    table: dict[int, tuple[int, int, int]] = {}
    for row in build_rows:
        key = _wrap_int32(row[0])
        if not 0 <= key < key_space:
            raise ValueError(f"build key {key} outside key space [0, {key_space})")
        table[key] = (_wrap_int32(row[1]), _wrap_int32(row[2]), _wrap_int32(row[3]))
    out: list[tuple[int, int, int, int]] = []
    for row in probe_rows:
        key = _wrap_int32(row[0])
        if not 0 <= key < key_space:
            raise ValueError(f"probe key {key} outside key space [0, {key_space})")
        if key in table:
            b0, b1, b2 = table[key]
            v0, v1, v2 = _wrap_int32(row[1]), _wrap_int32(row[2]), _wrap_int32(row[3])
            out0, out1, out2 = out_sel.select((b0, b1, b2, v0, v1, v2))
            out.append((key, out0, out1, out2))
    return out


GROUPED_AGGREGATION_CONFIG_VERSION = "0.1"


class GroupedAggregationErrorCode(IntEnum):
    CONFIG = 1
    STREAM = 2


@dataclass(frozen=True)
class GroupAggregate:
    """One closed group from the ordered grouped-aggregation tile: every
    statistic is computed in a single pass. ``first``/``last`` follow input
    order, so partial groups from separate batches merge correctly on the
    host (the partial-bar contract)."""

    key: int
    first: int
    last: int
    minimum: int
    maximum: int
    total: int
    count: int


@dataclass(frozen=True)
class BarAggregate:
    """One closed bar from the fused single-pass bar-aggregation tile: price
    OHLC statistics plus the exact micro-share volume decomposition, all in
    one pass over (key, price, shares, frac) rows."""

    key: int
    open: int
    close: int
    low: int
    high: int
    shares: int
    frac: int
    count: int


@dataclass(frozen=True)
class FieldStats:
    """The single-pass statistics of one data field over a group: first/last in
    input order, running min/max, and the exact int64 sum."""

    first: int
    last: int
    minimum: int
    maximum: int
    total: int


@dataclass(frozen=True)
class GroupedFieldAggregate:
    """One closed group from the GENERIC multi-field aggregator: per-field
    first/last/min/max/sum over the three data fields of a quad row, plus the
    row count. A workload-agnostic superset — OHLCV bars (and other grouped
    reductions) are a field/stat SELECTION over it, not a baked-in tile."""

    key: int
    fields: tuple[FieldStats, FieldStats, FieldStats]
    count: int


def filter_bar_rows_by_key(rows: Iterable[tuple[int, int, int, int]], *, key_mask: int, key_match: int) -> list[tuple[int, int, int, int]]:
    """Golden semantics for the pair-row key partition filter: whole
    (key, price, shares, frac) rows pass when ``(key & mask) == match``
    (32-bit two's-complement key lane), order preserved."""
    return [row for row in rows if ((row[0] & 0xFFFF_FFFF) & (key_mask & 0xFFFF_FFFF)) == (key_match & 0xFFFF_FFFF)]


def range_partition_bar_rows(rows: Iterable[tuple[int, int, int, int]], *, splitters: Iterable[int]) -> list[list[tuple[int, int, int, int]]]:
    """Golden semantics for the range partitioner: route each
    (key, price, shares, frac) row to partition
    ``p = #{ s in splitters : signed32(key) >= signed32(s) }`` (splitters
    monotonic non-decreasing, signed 32-bit key lane), order preserved within
    each partition. Returns one ordered list per partition
    (length ``len(splitters) + 1``)."""
    bounds = [_wrap_int32(s) for s in splitters]
    parts: list[list[tuple[int, int, int, int]]] = [[] for _ in range(len(bounds) + 1)]
    for row in rows:
        key = _wrap_int32(row[0])
        parts[sum(1 for bound in bounds if key >= bound)].append(tuple(row))
    return parts


def gearbox_records(words: Iterable[int], *, record_words: int, record_count: int) -> list[tuple[int, ...]]:
    """Golden semantics for the record gearbox: a pure REFRAMING.

    The input is a flat stream of 64-bit words carrying ``record_count``
    back-to-back records of ``record_words`` words each, delivered in
    beats that need not divide the record size — so records straddle beat
    boundaries and the tail beat may be padded. The output is the record
    sequence, unchanged: no field is inspected, reordered or dropped, so
    the golden is the identity and the interesting property is the RATE
    (one whole record per cycle) rather than the values.

    Words beyond ``record_count * record_words`` are beat padding and are
    ignored; too few words is a framing error the hardware reports as
    ERR_STREAM."""
    if record_words < 1:
        raise ValueError(f"record_words must be positive, got {record_words}")
    if record_count < 0:
        raise ValueError(f"record_count must be non-negative, got {record_count}")
    flat = list(words)
    needed = record_count * record_words
    if len(flat) < needed:
        raise ValueError(f"stream carries {len(flat)} words, {needed} needed for {record_count} records")
    return [tuple(flat[i * record_words : (i + 1) * record_words]) for i in range(record_count)]


def mask_dispatch_records(records: Iterable[Sequence[int]], *, num_partitions: int, exact_branch: bool = False) -> list[list[tuple[int, ...]]]:
    """Golden semantics for the key-mask dispatcher over N-WORD records.

    The quad-row form (:func:`mask_dispatch_rows`) routes two 64-bit words
    keyed on the first word's low half. A fused chain whose head reads
    wider records — the as-of join's three-word ``{tag/key, time,
    payload}`` — routes those records WHOLE on the same key, so a
    per-symbol workflow can be replicated across lanes without reshaping
    the stream. Each record is a sequence of 64-bit words; the routing key
    is ``word0 & 0xFFFFFFFF`` read as a signed 32-bit value, exactly the
    hardware bit-select. Order is preserved within each partition."""
    if num_partitions < 1:
        raise ValueError(f"num_partitions must be positive, got {num_partitions}")
    if not exact_branch and num_partitions & (num_partitions - 1):
        raise ValueError(f"num_partitions must be a power of two, got {num_partitions}")
    parts: list[list[tuple[int, ...]]] = [[] for _ in range(num_partitions)]
    width = None
    for record in records:
        words = tuple(record)
        if width is None:
            width = len(words)
        elif len(words) != width:
            raise ValueError(f"records must all carry the same word count, got {len(words)} after {width}")
        key = words[0] & 0xFFFF_FFFF
        if exact_branch:
            if key == 0 or key > num_partitions:
                continue  # dropped at the router, zero lane cycles
            parts[key - 1].append(words)
        else:
            parts[key & (num_partitions - 1)].append(words)
    return parts


def mask_dispatch_rows(
    rows: Iterable[tuple[int, int, int, int]], *, num_partitions: int, exact_branch: bool = False
) -> list[list[tuple[int, int, int, int]]]:
    """Golden semantics for the key-mask dispatcher: route each
    (key, value, aux0, aux1) row to partition ``key & (num_partitions - 1)``
    (``num_partitions`` a power of two; the signed 32-bit key's low bits —
    two's complement, so negative keys mask exactly like the hardware
    bit-select), order preserved within each partition. The pair-key
    partition (``key & mask == lane``) moved from N per-lane filters into
    one shared router. Returns one ordered list per partition.

    ``exact_branch`` selects the exact-branch-id mode (q19's disjunct
    branches): partition ``key - 1`` for keys in ``[1, num_partitions]``,
    every other key **dropped at the router** (branch 0 — and any negative
    key, whose unsigned 32-bit image exceeds any partition count — costs
    zero lane cycles). ``num_partitions`` needs no power-of-two."""
    if num_partitions < 1:
        raise ValueError(f"num_partitions must be positive, got {num_partitions}")
    if not exact_branch and num_partitions & (num_partitions - 1):
        raise ValueError(f"num_partitions must be a power of two, got {num_partitions}")
    parts: list[list[tuple[int, int, int, int]]] = [[] for _ in range(num_partitions)]
    for row in rows:
        if exact_branch:
            key_u32 = _wrap_int32(row[0]) & 0xFFFF_FFFF
            if not 1 <= key_u32 <= num_partitions:
                continue
            parts[key_u32 - 1].append(tuple(row))
        else:
            parts[_wrap_int32(row[0]) & (num_partitions - 1)].append(tuple(row))
    return parts


def stride_distribute_rows(
    rows: Iterable[tuple[int, int, int, int]], *, rows_per_beat: int, num_partitions: int
) -> tuple[list[tuple[int, int, int, int]], ...]:
    """Route packed rows by static slot position and per-slot round robin.

    Row ``i`` occupies slot ``i % rows_per_beat``. Each slot owns one
    contiguous group of ``num_partitions / rows_per_beat`` lanes and advances
    only its own round-robin counter. This is correct for keyed aggregation
    even though a key may reach several lanes: the host merges per-lane
    partials by key, the same partial-group contract used by q1's two-pass
    aggregation.
    """
    if rows_per_beat < 1:
        raise ValueError(f"rows_per_beat must be positive, got {rows_per_beat}")
    if num_partitions < 1 or num_partitions % rows_per_beat:
        raise ValueError(f"num_partitions must be a positive multiple of rows_per_beat, got {num_partitions}")

    lanes_per_slot = num_partitions // rows_per_beat
    counters = [0] * rows_per_beat
    parts: tuple[list[tuple[int, int, int, int]], ...] = tuple([] for _ in range(num_partitions))
    for index, row in enumerate(rows):
        slot = index % rows_per_beat
        lane = slot * lanes_per_slot + counters[slot]
        parts[lane].append(tuple(row))
        counters[slot] = (counters[slot] + 1) % lanes_per_slot
    return parts


def pack_splitters(values: Sequence[int]) -> str:
    """Pack range-partitioner splitter constants into the ``cfg_splitters`` SV
    bus literal ``{32'h..., ...}``. ``cfg_splitters`` is a flat bus carrying
    splitter ``i`` at ``[i*32 +: 32]``; SV concatenation packs MSB-first, so
    the highest-index splitter is emitted first (reversed). The partitioner
    compares keys and splitters as *signed* 32-bit values
    (``range_partition_bar_rows``); values that would wrap under the
    ``& 0xFFFF_FFFF`` pack are rejected."""
    out_of_range = tuple(value for value in values if not -(1 << 31) <= value <= (1 << 31) - 1)
    if out_of_range:
        raise ValueError(f"splitters must fit signed int32, got {out_of_range}")
    packed = ", ".join(f"32'h{value & 0xFFFF_FFFF:08X}" for value in reversed(values))
    return f"{{{packed}}}"


class SorterErrorCode(IntEnum):
    STREAM = 2
    CAPACITY = 3


def sort_bar_rows_by_key(rows: Iterable[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """Golden semantics for the batch bitonic sorter: rows ascending by the
    signed 32-bit key, STABLE (rows with equal keys keep input order — the
    tile tie-breaks on load index)."""
    return sorted((tuple(row) for row in rows), key=lambda row: _wrap_int32(row[0]))


TOP_K_MAX = 128


class TopKErrorCode(IntEnum):
    """Status codes for the streaming top-k heap tile."""

    OK = 0
    BAD_K = 1  # requested k outside [1, TOP_K_MAX]


def streaming_top_k(rows: Iterable[tuple[int, int, int, int]], *, k: int) -> list[tuple[int, int, int, int]]:
    """Golden semantics for the streaming top-k heap tile: in one pass keep the
    ``k`` rows with the largest signed-32-bit sort key (field 0), breaking ties
    by arrival order (earlier rows win), and emit them sorted by sort key
    DESCENDING (ties arrival ascending) — the ``ORDER BY <key> DESC LIMIT k``
    shape (q3/q10, TSBS group-by-order-by-limit).

    Bounded state: the tile holds at most ``k`` rows in a size-``k`` min-heap
    whose root — the eviction target — is the entry with the smallest key and,
    among equal-key entries, the LATEST arrival (the heap orders on
    ``(key, -arrival)``, not key alone). An arriving row replaces the root only
    when its key is STRICTLY greater, so at the k-th-largest key the earliest
    arrivals are retained — matched by the ``(-key, arrival)`` sort below. This
    root tie-order is part of the tile contract: a key-only min-heap could evict
    an earlier tied row and diverge from this golden. ``k`` is bounded to
    ``[1, TOP_K_MAX]``; the tile reports ``BAD_K`` outside that range."""
    if not 1 <= k <= TOP_K_MAX:
        raise ValueError(f"k must be in [1, {TOP_K_MAX}], got {k}")
    indexed = sorted(
        enumerate(tuple(row) for row in rows),
        key=lambda item: (-_wrap_int32(item[1][0]), item[0]),
    )
    return [row for _, row in indexed[:k]]


def time_bucket_key_rows(rows: Iterable[tuple[int, int, int, int, int]], *, bucket_shift: int, bucket_bits: int) -> list[tuple[int, int, int, int]]:
    """Golden semantics for the generic time-bucket key tile: rewrite each row's
    partition key into a composite ``(partition, time-bucket)`` group key so a
    downstream key-grouped aggregator produces fixed-width time-windowed groups
    per partition. Window = ``2**bucket_shift``; key =
    ``(partition[:PART_BITS] << bucket_bits) | (time >> bucket_shift)[:bucket_bits]``
    with ``PART_BITS = 32 - bucket_bits``. Input rows are
    ``(partition, payload_hi, time, payload2, payload3)``; output the aggregator's
    quad rows ``(key, payload_hi, payload2, payload3)``. Workload-agnostic — any
    time-windowed aggregation composes it before the generic aggregator."""
    part_bits = 32 - bucket_bits
    part_mask = (1 << part_bits) - 1
    bucket_mask = (1 << bucket_bits) - 1
    out: list[tuple[int, int, int, int]] = []
    for partition, payload_hi, time, payload2, payload3 in rows:
        key = ((partition & part_mask) << bucket_bits) | ((time >> bucket_shift) & bucket_mask)
        out.append((key, payload_hi, payload2, payload3))
    return out


def gather_records(words: list[int], *, in_words: int, out_words: int, selectors: tuple[int, ...]) -> list[int]:
    """Gather 32-bit halves from fixed-width records into new records.

    Each packed selector uses bits ``[5:0]`` for source-word index, bit 6 for
    source half (0 low, 1 high), and bits ``[11:7]`` for a logical right shift;
    bits ``[31:12]`` are reserved and must be zero. Selectors are ordered by
    output half: low half, then high half, for each output word. Shifts always
    zero-fill, including when the selected half has its sign bit set.
    """
    if not 1 <= in_words <= 64:
        raise ValueError(f"in_words must be in [1, 64], got {in_words}")
    if not 1 <= out_words <= 8:
        raise ValueError(f"out_words must be in [1, 8], got {out_words}")
    if len(words) % in_words:
        raise ValueError("input word count must be a multiple of in_words")
    if len(selectors) != 2 * out_words:
        raise ValueError(f"selectors must contain {2 * out_words} entries")
    for selector in selectors:
        if selector < 0 or selector >> 32 or selector >> 12:
            raise ValueError("selector reserved bits [31:12] must be zero")
        if (selector & 0x3F) >= in_words:
            raise ValueError("selector source word is outside the input record")

    output: list[int] = []
    for record_offset in range(0, len(words), in_words):
        record = words[record_offset : record_offset + in_words]
        halves = []
        for selector in selectors:
            source = record[selector & 0x3F]
            half = (source >> (32 if selector & 0x40 else 0)) & 0xFFFF_FFFF
            halves.append(half >> ((selector >> 7) & 0x1F))
        output.extend((halves[index] | (halves[index + 1] << 32)) & 0xFFFF_FFFF_FFFF_FFFF for index in range(0, len(halves), 2))
    return output


def grouped_field_aggregate_rows(rows: Iterable[tuple[int, int, int, int]]) -> list[GroupedFieldAggregate]:
    """Golden semantics for the generic multi-field grouped aggregator (ordered
    close-out): consecutive rows with equal keys form a group; each of the three
    data fields ``(v0, v1, v2)`` gets its first/last/min/max/sum in one pass, plus
    the row count. A reappearing key opens a NEW group (partials merged host-side,
    the partial-group contract) — workload-agnostic; OHLCV bars are a selection
    over it (see :func:`bars_from_field_aggregate`)."""
    results: list[GroupedFieldAggregate] = []
    key0: int | None = None
    stats: list[list[int]] | None = None  # per field: [first, last, min, max, total]
    count = 0
    for key, v0, v1, v2 in rows:
        values = (v0, v1, v2)
        if stats is None or key != key0:
            if stats is not None:
                results.append(GroupedFieldAggregate(key=key0, fields=tuple(FieldStats(*s) for s in stats), count=count))
            stats = [[v, v, v, v, v] for v in values]
            count = 1
            key0 = key
        else:
            for field, value in zip(stats, values):
                field[1] = value  # last
                field[2] = min(field[2], value)
                field[3] = max(field[3], value)
                field[4] += value  # total
            count += 1
    if stats is not None:
        results.append(GroupedFieldAggregate(key=key0, fields=tuple(FieldStats(*s) for s in stats), count=count))
    return results


def bars_from_field_aggregate(groups: Iterable[GroupedFieldAggregate]) -> list[BarAggregate]:
    """OHLCV bars as a field/stat SELECTION over the generic multi-field
    aggregate: price = field 0 (open/close/low/high = first/last/min/max),
    shares = sum of field 1, frac = sum of field 2. The finance column
    assignment lives here, not in the aggregator."""
    return [
        BarAggregate(
            key=g.key,
            open=g.fields[0].first,
            close=g.fields[0].last,
            low=g.fields[0].minimum,
            high=g.fields[0].maximum,
            shares=g.fields[1].total,
            frac=g.fields[2].total,
            count=g.count,
        )
        for g in groups
    ]


def bar_aggregate_rows(rows: Iterable[tuple[int, int, int, int]]) -> list[BarAggregate]:
    """Golden semantics for the fused bar-aggregation tile (ordered
    close-out): consecutive rows with equal keys form a bar; volume is
    ``shares * SCALE + frac`` recombined on the host."""
    results: list[BarAggregate] = []
    current: BarAggregate | None = None
    for key, price, shares, frac in rows:
        if current is None or key != current.key:
            if current is not None:
                results.append(current)
            current = BarAggregate(key=key, open=price, close=price, low=price, high=price, shares=shares, frac=frac, count=1)
        else:
            current = BarAggregate(
                key=key,
                open=current.open,
                close=price,
                low=min(current.low, price),
                high=max(current.high, price),
                shares=current.shares + shares,
                frac=current.frac + frac,
                count=current.count + 1,
            )
    if current is not None:
        results.append(current)
    return results


def grouped_aggregate_rows(rows: Iterable[tuple[int, int]]) -> list[GroupAggregate]:
    """Golden semantics for the ordered grouped-aggregation tile
    (close-out mode): consecutive rows with equal keys form a group; a group
    closes when the key changes or input ends. A key that reappears later
    opens a NEW group — partials are merged by the host, never the tile."""
    results: list[GroupAggregate] = []
    current: GroupAggregate | None = None
    for key, value in rows:
        if current is None or key != current.key:
            if current is not None:
                results.append(current)
            current = GroupAggregate(key=key, first=value, last=value, minimum=value, maximum=value, total=value, count=1)
        else:
            current = GroupAggregate(
                key=key,
                first=current.first,
                last=value,
                minimum=min(current.minimum, value),
                maximum=max(current.maximum, value),
                total=current.total + value,
                count=current.count + 1,
            )
    if current is not None:
        results.append(current)
    return results


class EmptyInputError(ValueError):
    pass


class UnsupportedAggregationError(ValueError):
    pass


_INTEGER_RANGES = {
    LogicalType.INT8: (-(1 << 7), (1 << 7) - 1),
    LogicalType.INT16: (-(1 << 15), (1 << 15) - 1),
    LogicalType.INT32: (-(1 << 31), (1 << 31) - 1),
    LogicalType.INT64: (-(1 << 63), (1 << 63) - 1),
    LogicalType.UINT8: (0, (1 << 8) - 1),
    LogicalType.UINT16: (0, (1 << 16) - 1),
    LogicalType.UINT32: (0, (1 << 32) - 1),
    LogicalType.UINT64: (0, (1 << 64) - 1),
}

_FLOAT_TYPES = {LogicalType.FLOAT32, LogicalType.FLOAT64}


def aggregate_values(opcode: OperationCode, values: Iterable[int | float | bool], logical_type: LogicalType) -> int | float:
    materialized_values = list(values)

    if opcode == OperationCode.COUNT:
        return len(materialized_values)

    if not materialized_values:
        if opcode == OperationCode.SUM:
            return 0.0 if logical_type in _FLOAT_TYPES else 0
        raise EmptyInputError(f"{opcode.name.lower()} requires at least one value")

    if logical_type in _FLOAT_TYPES and any(_is_nan(value) for value in materialized_values):
        return math.nan

    if opcode == OperationCode.MIN:
        return min(materialized_values)
    if opcode == OperationCode.MAX:
        return max(materialized_values)
    if opcode == OperationCode.SUM:
        return _sum_values(materialized_values, logical_type)
    if opcode == OperationCode.AVERAGE:
        return float(sum(materialized_values) / len(materialized_values))

    raise UnsupportedAggregationError(f"unsupported operation {opcode!r}")


def synthetic_values(name: str, logical_type: LogicalType) -> list[int | float | bool]:
    if name == "empty":
        return []
    if name == "one":
        return [1.0] if logical_type in _FLOAT_TYPES else [1]
    if name == "odd_ints":
        return [-3, -1, 0, 1, 3]
    if name == "with_nan":
        if logical_type not in _FLOAT_TYPES:
            raise ValueError("with_nan fixture requires a floating-point logical type")
        return [1.0, math.nan, 3.0]
    if name == "repeated":
        return [2.0, 2.0, 2.0, 2.0] if logical_type in _FLOAT_TYPES else [2, 2, 2, 2]
    if name == "sorted":
        return [1.0, 2.0, 3.0, 4.0] if logical_type in _FLOAT_TYPES else [1, 2, 3, 4]
    if name == "skewed":
        return [0.0, 0.0, 0.0, 99.0] if logical_type in _FLOAT_TYPES else [0, 0, 0, 99]
    if name == "bounds":
        return _bounds_fixture(logical_type)
    raise ValueError(f"unknown synthetic fixture {name!r}")


def _sum_values(values: list[int | float | bool], logical_type: LogicalType) -> int | float:
    total = sum(values)
    if logical_type in _INTEGER_RANGES:
        minimum, maximum = _INTEGER_RANGES[logical_type]
        return min(max(int(total), minimum), maximum)
    if logical_type in _FLOAT_TYPES:
        return float(total)
    raise UnsupportedAggregationError(f"sum does not support {logical_type.name.lower()}")


def _bounds_fixture(logical_type: LogicalType) -> list[int | float | bool]:
    if logical_type in _INTEGER_RANGES:
        minimum, maximum = _INTEGER_RANGES[logical_type]
        return [minimum, 0, maximum]
    if logical_type in _FLOAT_TYPES:
        return [-math.inf, 0.0, math.inf]
    if logical_type == LogicalType.BOOL:
        return [False, True]
    raise ValueError(f"no bounds fixture for {logical_type.name.lower()}")


def _is_nan(value: float | bool) -> bool:
    return isinstance(value, float) and math.isnan(value)


KEYED_STATE_MAX_KEYS = 1024


class KeyedStateErrorCode(IntEnum):
    """Status codes for the keyed state tile."""

    OK = 0
    BAD_CAPACITY = 1  # declared capacity outside [1, KEYED_STATE_MAX_KEYS]
    KEY_CAPACITY_EXCEEDED = 2  # a new key arrived with the table full


class KeyedStateOp(IntEnum):
    """What a row does to its key's accumulator."""

    SET = 0  # overwrite with the row's value
    ADD = 1  # accumulate
    MAX = 2  # running maximum
    MIN = 3  # running minimum


def keyed_state_scan(
    rows: Iterable[tuple[int, int]],
    *,
    op: KeyedStateOp,
    capacity: int,
    initial: int = 0,
) -> tuple[list[tuple[int, int]], KeyedStateErrorCode]:
    """Golden semantics for the keyed state tile: a per-key accumulator updated
    once per row, emitting the POST-UPDATE state for every row.

    This is the primitive the stateful family is built on. Unlike every tile
    that precedes it, the output for a row depends on rows that came before it
    with the SAME key — a loop-carried dependency per key, which is exactly the
    shape a SIMD CPU cannot vectorize and a spatial datapath can hold on-chip.

    ``rows`` are ``(key, value)`` pairs in arrival order. Each row reads its
    key's current state, applies ``op``, writes it back, and emits
    ``(key, new_state)``. A key seen for the first time starts from ``initial``,
    so the first row of a key emits ``op(initial, value)`` — NOT ``value``,
    unless ``op`` is SET. That distinction is the whole reason ``initial`` is
    explicit rather than implied.

    **Emitting post-update state, not pre-update, is a deliberate contract.**
    Post-update is what a running aggregate means, it is what the hardware can
    produce without an extra pipeline stage holding the old value, and it makes
    back-to-back same-key rows well defined: the second row observes the first
    row's write. In hardware that is a read-modify-write hazard the tile must
    FORWARD rather than stall, and this golden pins the answer it must forward
    to.

    ``capacity`` is the declared number of distinct keys the table holds. It is
    a hardware fact (a BRAM depth), so exceeding it is an ERROR rather than an
    eviction: silently dropping a key would make the result wrong in a way no
    downstream consumer could detect. Rows already emitted stay emitted, and
    the error is returned alongside them so a caller can see exactly where the
    table filled.

    **Hardware note the HDL must honour**: exact arbitrary-key lookup at one
    row per cycle needs a CAM or a pipelined associative/hash design with
    collision handling and pending-allocation tracking. A plain single-BRAM
    dictionary cannot do it, and the forwarding above must bypass both the
    newest in-flight update and a pending key allocation.

    ``initial=0`` is unambiguous but worth stating: MAX over only negative
    values emits 0, and MIN over only positive values emits 0. Callers wanting
    the first value to win should pass the domain's extreme.

    Returns ``(emitted_rows, error_code)``.
    """
    if not 1 <= capacity <= KEYED_STATE_MAX_KEYS:
        return [], KeyedStateErrorCode.BAD_CAPACITY

    if op not in (KeyedStateOp.SET, KeyedStateOp.ADD, KeyedStateOp.MAX, KeyedStateOp.MIN):
        raise ValueError(f"unsupported keyed state op: {op!r}")

    state: dict[int, int] = {}
    emitted: list[tuple[int, int]] = []
    initial = _wrap_int32(initial)
    for raw_key, raw_value in rows:
        # Keys and values are int32 WIRES, not Python integers. -1 and
        # 0xFFFFFFFF are the same key on a 32-bit bus, and a state a 32-bit
        # port cannot carry is not a state the tile can be in, so both are
        # wrapped into the domain before anything else happens. Skipping this
        # made the golden unimplementable: it split one hardware key into two
        # and emitted 2**31 as a signed result.
        key = _wrap_int32(raw_key)
        value = _wrap_int32(raw_value)
        if key not in state:
            if len(state) >= capacity:
                return emitted, KeyedStateErrorCode.KEY_CAPACITY_EXCEEDED
            state[key] = initial
        current = state[key]
        if op is KeyedStateOp.SET:
            updated = value
        elif op is KeyedStateOp.ADD:
            updated = _wrap_int32(current + value)
        elif op is KeyedStateOp.MAX:
            updated = max(current, value)
        else:
            updated = min(current, value)
        state[key] = updated
        emitted.append((key, updated))
    return emitted, KeyedStateErrorCode.OK


SCRATCHPAD_MAX_WORDS = 16384


class ScratchpadErrorCode(IntEnum):
    """Status codes for the scratchpad owned-arena tile."""

    OK = 0
    BAD_GRANT = 1  # granted_words outside [1, SCRATCHPAD_MAX_WORDS]
    STORAGE_OVERFLOW = 2  # an address at or past the grant boundary


def scratchpad_scan(
    rows: Iterable[tuple[int, int, int]],
    *,
    granted_words: int,
) -> tuple[list[tuple[int, int]], ScratchpadErrorCode]:
    """Golden semantics for the scratchpad tile: the deliberately trivial
    consumer of the OWNED storage class (STORAGE_AND_NOC.md phase 1). A
    private word-addressed arena; each row is ``(write_enable, addr, value)``
    and emits ``(addr, post_op_value)`` — a write emits what it wrote, a read
    emits what is stored, an unwritten word reads 0 (BRAM init).

    The contract under test is the GRANT, not the arithmetic: an address at
    or past ``granted_words`` is STORAGE_OVERFLOW — a defined close-out that
    preserves already-emitted rows, exactly the shape a grow-request replaces
    when the allocator hardens. Addresses and values are int32 wires
    (wrapped); a negative address is never inside any grant.
    """
    if not 1 <= granted_words <= SCRATCHPAD_MAX_WORDS:
        return [], ScratchpadErrorCode.BAD_GRANT

    arena: dict[int, int] = {}
    emitted: list[tuple[int, int]] = []
    for write_enable, raw_addr, raw_value in rows:
        addr = _wrap_int32(raw_addr)
        value = _wrap_int32(raw_value)
        if not 0 <= addr < granted_words:
            return emitted, ScratchpadErrorCode.STORAGE_OVERFLOW
        if write_enable:
            arena[addr] = value
        emitted.append((addr, arena.get(addr, 0)))
    return emitted, ScratchpadErrorCode.OK


class SessionErrorCode(IntEnum):
    """Status codes for the session-gap tile."""

    OK = 0
    BAD_CAPACITY = 1  # capacity outside [1, KEYED_STATE_MAX_KEYS]
    SESSION_CAPACITY_EXCEEDED = 2  # a new key past the table's capacity
    BAD_GAP = 3  # gap must be positive
    TIME_ORDER = 4  # a row's time ran backwards within the batch


def sessionize_scan(
    rows: Iterable[tuple[int, int]],
    *,
    gap: int,
    capacity: int,
) -> tuple[list[tuple[int, int, int, int]], SessionErrorCode]:
    """Golden semantics for the session-gap tile: the Nexmark-Q11 shape, and
    step 2 of the stateful programme (STORAGE_AND_NOC.md). Rows are
    ``(key, time)`` in arrival order; a session is a maximal per-key run of
    rows with no inter-row gap exceeding ``gap``. Each closed session emits
    ``(key, start_time, end_time, row_count)``.

    Sessions close two ways. A row whose time exceeds its key's last time by
    more than ``gap`` closes the old session (emitted immediately, in arrival
    order) and opens a new one. Batch end closes everything still open,
    emitted **sorted by signed key** — a deterministic order no
    implementation detail (hash placement, slot index) can leak into.

    **Phase scope is PROCESSING-order sessionization**: times must be
    non-decreasing across the batch (the generator's contract), and a
    violation is ``TIME_ORDER`` — refused, never silently reordered. Event
    time, watermarks and late arrivals are the next programme step and are
    deliberately absent here.

    ``capacity`` bounds DISTINCT KEYS per batch, exactly like keyed state: a
    gap-close immediately reopens, so it never frees a slot mid-batch. The
    (capacity+1)-th distinct key is ``SESSION_CAPACITY_EXCEEDED``, a defined
    close-out preserving emitted sessions — never an eviction. Keys and
    times are int32 wires (wrapped); ``row_count`` is a CARDINALITY carried
    as an unsigned 32-bit wire in hardware — identical to this unbounded int
    for every batch below 2^32 rows, which no staging region can hold.

    Returns ``(closed_sessions, error_code)``.
    """
    if not 1 <= capacity <= KEYED_STATE_MAX_KEYS:
        return [], SessionErrorCode.BAD_CAPACITY
    if gap <= 0:
        return [], SessionErrorCode.BAD_GAP

    open_sessions: dict[int, tuple[int, int, int]] = {}  # key -> (start, last, count)
    emitted: list[tuple[int, int, int, int]] = []
    previous_time: int | None = None
    for raw_key, raw_time in rows:
        key = _wrap_int32(raw_key)
        time = _wrap_int32(raw_time)
        if previous_time is not None and time < previous_time:
            return emitted, SessionErrorCode.TIME_ORDER
        previous_time = time
        held = open_sessions.get(key)
        if held is None:
            if len(open_sessions) >= capacity:
                return emitted, SessionErrorCode.SESSION_CAPACITY_EXCEEDED
            open_sessions[key] = (time, time, 1)
        else:
            start, last, count = held
            if time - last > gap:
                emitted.append((key, start, last, count))
                open_sessions[key] = (time, time, 1)
            else:
                open_sessions[key] = (start, time, count + 1)
    for key in sorted(open_sessions):
        start, last, count = open_sessions[key]
        emitted.append((key, start, last, count))
    return emitted, SessionErrorCode.OK


HASH_GROUPED_MAX_GROUPS = 1024


class HashGroupedErrorCode(IntEnum):
    """Status codes for the hash-mode grouped field-sum aggregation tile.
    The values ARE the tile's ``status_error_code`` wire encoding, so the
    golden and the HDL cannot drift apart silently."""

    OK = 0
    BAD_CAPACITY = 1  # declared capacity outside [1, HASH_GROUPED_MAX_GROUPS]
    BAD_FIELD_MASK = 2  # a zero mask (an unconfigured tile) or one past the four fields
    GROUP_CAPACITY_EXCEEDED = 3  # a new group key arrived with the table full
    BAD_STREAM = 4  # hardware only: a torn row (input_last on a quad row's w0)


@dataclass(frozen=True)
class HashGroupedAggregate:
    """One group from the hash-mode grouped field-sum aggregation tile:
    INT64 sums of the mask-selected fields (ascending field order) plus the
    group's row count — the same record shape the ORDERED grouped
    field-sum tile emits, so the host decode path is shared."""

    key: int
    field_mask: int
    sums: tuple[int, ...]
    count: int

    def record_words(self) -> list[int]:
        """The group record as unsigned 64-bit stream words: sign-extended
        key, sums re-encoded to two's complement, count last."""
        mask64 = 0xFFFF_FFFF_FFFF_FFFF
        return [self.key & mask64] + [sum_value & mask64 for sum_value in self.sums] + [self.count]


def hash_grouped_field_sum_rows(
    rows: Iterable[tuple[int, int, int, int]],
    *,
    field_mask: int,
    capacity: int,
) -> tuple[list[HashGroupedAggregate], HashGroupedErrorCode]:
    """Golden semantics for the HASH-mode grouped field-sum aggregation tile:
    one INT64 accumulator set per DISTINCT key in a bounded on-chip table,
    drained key-ascending at close-out.

    The difference from :func:`grouped_field_sum_rows` is the whole point of
    the tile: the ordered tile groups CONSECUTIVE equal keys, so a key that
    reappears after another key opens a second partial group the host must
    merge, and the staged table must be pre-sorted on the group key. This
    tile holds a per-key accumulator, so **input order does not matter** and
    a key may reappear anywhere in the batch — one record per distinct key,
    always. That removes the ``sorted_by_key`` staging precondition from
    every grouped scan.

    Two contracts are deliberate and must be honoured bit-for-bit by the HDL:

    **Emission is key-ASCENDING (signed).** A hash table has no intrinsic
    order, so leaking the probe/slot order into the record stream would make
    the result depend on the hash function — an implementation detail no
    golden could pin and no host could rely on. Sorting the drain by signed
    key makes the order implementation-independent and physically true (the
    session-gap tile's min-extraction drain is the same decision).

    **Overflow is an ERROR, never an eviction.** ``capacity`` is a hardware
    fact (the table depth). When a row presents a key not in the table and
    the table is full, the batch reports ``GROUP_CAPACITY_EXCEEDED`` and the
    accumulated groups are returned alongside it: evicting or silently
    dropping the key would make the answer wrong in a way no downstream
    consumer could detect. The host's response is to split the batch by key
    range and merge the partials — the same partial-merge algebra the
    ordered tile's host merge already uses, which is exact because INT64
    sums and counts are additive per key.

    ``field_mask`` bit i selects field i of the quad row ``(key, value,
    aux0, aux1)``; selecting field 0 sums the key itself, which is legal and
    equals ``key * count`` (the ordered tile's convention).

    Returns ``(groups_key_ascending, error_code)``.
    """
    if not 1 <= capacity <= HASH_GROUPED_MAX_GROUPS:
        return [], HashGroupedErrorCode.BAD_CAPACITY
    if not 1 <= field_mask <= 15:
        return [], HashGroupedErrorCode.BAD_FIELD_MASK

    selected = [index for index in range(4) if field_mask & (1 << index)]
    table: dict[int, list[int]] = {}
    counts: dict[int, int] = {}
    error = HashGroupedErrorCode.OK
    for row in rows:
        # keys and field values are int32 WIRES: -1 and 0xFFFFFFFF are the
        # same key on a 32-bit bus (the keyed-state lesson)
        key = _wrap_int32(row[0])
        if key not in table:
            if len(table) >= capacity:
                error = HashGroupedErrorCode.GROUP_CAPACITY_EXCEEDED
                break
            table[key] = [0] * len(selected)
            counts[key] = 0
        sums = table[key]
        for slot, field in enumerate(selected):
            sums[slot] += _wrap_int32(row[field])
        counts[key] += 1
    groups = [
        HashGroupedAggregate(key=key, field_mask=field_mask, sums=tuple(table[key]), count=counts[key])
        for key in sorted(table)  # signed-key ascending: the pinned drain order
    ]
    return groups, error


def merge_hash_grouped(partials: Iterable[Iterable[HashGroupedAggregate]]) -> list[HashGroupedAggregate]:
    """Merge per-batch hash-grouped partials into one key-ascending result —
    the host half of the overflow contract.

    Splitting a batch (by key range, or simply into smaller batches) and
    merging the partials is EXACT: INT64 sums and row counts are additive
    per key, and no other state crosses a batch boundary. This is what makes
    ``GROUP_CAPACITY_EXCEEDED`` a recoverable condition rather than a failed
    query, and the property ``merge(split(rows)) == one_shot(rows)`` is the
    test that keeps it honest. Every partial must carry the same
    ``field_mask``; mixing masks is a caller error, not a merge case."""
    sums_by_key: dict[int, list[int]] = {}
    counts_by_key: dict[int, int] = {}
    field_mask: int | None = None
    for partial in partials:
        for group in partial:
            if field_mask is None:
                field_mask = group.field_mask
            elif group.field_mask != field_mask:
                raise ValueError(f"cannot merge partials with different field masks: {field_mask} vs {group.field_mask}")
            if group.key not in sums_by_key:
                sums_by_key[group.key] = list(group.sums)
                counts_by_key[group.key] = 0
            else:
                totals = sums_by_key[group.key]
                for slot, value in enumerate(group.sums):
                    totals[slot] += value
            counts_by_key[group.key] += group.count
    if field_mask is None:
        return []
    return [
        HashGroupedAggregate(key=key, field_mask=field_mask, sums=tuple(sums_by_key[key]), count=counts_by_key[key]) for key in sorted(sums_by_key)
    ]
