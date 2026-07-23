from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum, IntFlag

from ccflow import BaseModel

from .registers import DAU_MAGIC_WORD


class InvalidStreamError(ValueError):
    pass


class LogicalType(IntEnum):
    BOOL = 1
    INT8 = 10
    INT16 = 11
    INT32 = 12
    INT64 = 13
    UINT8 = 20
    UINT16 = 21
    UINT32 = 22
    UINT64 = 23
    FLOAT32 = 30
    FLOAT64 = 31


class OperationCode(IntEnum):
    MIN = 1
    MAX = 2
    SUM = 3
    COUNT = 4
    AVERAGE = 5

    @classmethod
    def from_name(cls, name: str) -> OperationCode:
        try:
            return cls[name.strip().upper()]
        except KeyError:
            known = ", ".join(opcode.name.lower() for opcode in cls)
            raise ValueError(f"unknown operation {name!r}; expected one of: {known}") from None


def output_type_for(opcode: OperationCode) -> LogicalType:
    """The wire type an aggregation opcode produces (single source of truth
    for every backend, API layer, and example)."""
    if opcode is OperationCode.AVERAGE:
        return LogicalType.FLOAT64
    if opcode is OperationCode.COUNT:
        return LogicalType.UINT64
    return LogicalType.INT64


class StreamAggregationErrorCode(IntEnum):
    DESCRIPTOR = 1
    OPCODE = 2
    TILE = 3


class GroupRole(IntEnum):
    VALUE = 0
    KEY = 1
    OUTPUT = 2


class PolicyFlag(IntFlag):
    NONE = 0
    SATURATING_SUM = 1 << 0
    IEEE_FLOAT_NAN = 1 << 1
    FLOAT64_AVERAGE = 1 << 2


class StreamFlag(IntFlag):
    NONE = 0
    HAS_GROUP_FIELDS = 1 << 0


@dataclass(frozen=True)
class BatchHeader:
    schema_hash: int
    batch_id: int
    row_count: int
    column_count: int
    operation_count: int
    group_key_count: int
    descriptor_table_offset: int
    payload_offset: int
    payload_length: int
    version_major: int = 0
    version_minor: int = 1
    header_length: int = 64
    flags: StreamFlag = StreamFlag.HAS_GROUP_FIELDS

    SIZE_BYTES = 64
    _STRUCT = struct.Struct("<IBBBBQQQHHHHQQQ")

    def __post_init__(self) -> None:
        _validate_u8("version_major", self.version_major)
        _validate_u8("version_minor", self.version_minor)
        _validate_u8("header_length", self.header_length)
        _validate_u8("flags", int(self.flags))
        _validate_u16("column_count", self.column_count)
        _validate_u16("operation_count", self.operation_count)
        _validate_u16("group_key_count", self.group_key_count)
        _validate_u64("schema_hash", self.schema_hash)
        _validate_u64("batch_id", self.batch_id)
        _validate_u64("row_count", self.row_count)
        _validate_u64("descriptor_table_offset", self.descriptor_table_offset)
        _validate_u64("payload_offset", self.payload_offset)
        _validate_u64("payload_length", self.payload_length)

    def pack(self) -> bytes:
        return self._STRUCT.pack(
            DAU_MAGIC_WORD,
            self.version_major,
            self.version_minor,
            self.header_length,
            int(self.flags),
            self.schema_hash,
            self.batch_id,
            self.row_count,
            self.column_count,
            self.operation_count,
            self.group_key_count,
            0,
            self.descriptor_table_offset,
            self.payload_offset,
            self.payload_length,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "BatchHeader":
        if len(data) != cls.SIZE_BYTES:
            raise InvalidStreamError(f"batch header must be {cls.SIZE_BYTES} bytes")
        unpacked = cls._STRUCT.unpack(data)
        magic_word = unpacked[0]
        if magic_word != DAU_MAGIC_WORD:
            raise InvalidStreamError(f"invalid stream magic 0x{magic_word:08x}")
        return cls(
            version_major=unpacked[1],
            version_minor=unpacked[2],
            header_length=unpacked[3],
            flags=StreamFlag(unpacked[4]),
            schema_hash=unpacked[5],
            batch_id=unpacked[6],
            row_count=unpacked[7],
            column_count=unpacked[8],
            operation_count=unpacked[9],
            group_key_count=unpacked[10],
            descriptor_table_offset=unpacked[12],
            payload_offset=unpacked[13],
            payload_length=unpacked[14],
        )


@dataclass(frozen=True)
class ColumnDescriptor:
    field_id: int
    logical_type: LogicalType
    physical_width_bits: int
    row_count: int
    null_count: int
    payload_offset: int
    payload_length: int
    validity_offset: int = 0
    validity_length: int = 0
    group_role: GroupRole = GroupRole.VALUE
    flags: int = 0

    SIZE_BYTES = 64
    _STRUCT = struct.Struct("<HHHHQQQQQQHHI")

    def __post_init__(self) -> None:
        _validate_u16("field_id", self.field_id)
        _validate_u16("physical_width_bits", self.physical_width_bits)
        _validate_u16("flags", self.flags)
        _validate_u64("row_count", self.row_count)
        _validate_u64("null_count", self.null_count)
        _validate_u64("payload_offset", self.payload_offset)
        _validate_u64("payload_length", self.payload_length)
        _validate_u64("validity_offset", self.validity_offset)
        _validate_u64("validity_length", self.validity_length)

    def pack(self) -> bytes:
        return self._STRUCT.pack(
            self.field_id,
            int(self.logical_type),
            self.physical_width_bits,
            self.flags,
            self.row_count,
            self.null_count,
            self.payload_offset,
            self.payload_length,
            self.validity_offset,
            self.validity_length,
            int(self.group_role),
            0,
            0,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "ColumnDescriptor":
        if len(data) != cls.SIZE_BYTES:
            raise InvalidStreamError(f"column descriptor must be {cls.SIZE_BYTES} bytes")
        unpacked = cls._STRUCT.unpack(data)
        return cls(
            field_id=unpacked[0],
            logical_type=LogicalType(unpacked[1]),
            physical_width_bits=unpacked[2],
            flags=unpacked[3],
            row_count=unpacked[4],
            null_count=unpacked[5],
            payload_offset=unpacked[6],
            payload_length=unpacked[7],
            validity_offset=unpacked[8],
            validity_length=unpacked[9],
            group_role=GroupRole(unpacked[10]),
        )


@dataclass(frozen=True)
class OperationDescriptor:
    operation_id: int
    opcode: OperationCode
    input_column_id: int
    output_field_id: int
    output_type: LogicalType
    accumulator_width_bits: int
    policy_flags: PolicyFlag = PolicyFlag.NONE
    group_key_set_id: int = 0

    SIZE_BYTES = 64
    _STRUCT = struct.Struct("<HHHHHHIHHIQQQQQ")

    def __post_init__(self) -> None:
        _validate_u16("operation_id", self.operation_id)
        _validate_u16("input_column_id", self.input_column_id)
        _validate_u16("output_field_id", self.output_field_id)
        _validate_u16("accumulator_width_bits", self.accumulator_width_bits)
        _validate_u16("group_key_set_id", self.group_key_set_id)
        _validate_u32("policy_flags", int(self.policy_flags))

    def pack(self) -> bytes:
        return self._STRUCT.pack(
            self.operation_id,
            int(self.opcode),
            self.input_column_id,
            self.output_field_id,
            int(self.output_type),
            self.accumulator_width_bits,
            int(self.policy_flags),
            self.group_key_set_id,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "OperationDescriptor":
        if len(data) != cls.SIZE_BYTES:
            raise InvalidStreamError(f"operation descriptor must be {cls.SIZE_BYTES} bytes")
        unpacked = cls._STRUCT.unpack(data)
        return cls(
            operation_id=unpacked[0],
            opcode=OperationCode(unpacked[1]),
            input_column_id=unpacked[2],
            output_field_id=unpacked[3],
            output_type=LogicalType(unpacked[4]),
            accumulator_width_bits=unpacked[5],
            policy_flags=PolicyFlag(unpacked[6]),
            group_key_set_id=unpacked[7],
        )


class RecordBatchLimits(BaseModel):
    """Caps for multi-column record batches (see dau-docs record-batch contract).

    Defaults are sized to the 1 MiB stream-job staging buffers: header +
    descriptor table + payload must fit one staging region.
    """

    model_config = {"frozen": True}

    max_columns: int = 16
    max_rows: int = 1 << 20
    max_batch_bytes: int = 0x0010_0000
    alignment_bytes: int = 64

    def model_post_init(self, context) -> None:
        super().model_post_init(context)
        if self.max_columns < 1:
            raise ValueError("max_columns must be positive")
        if self.max_rows < 1:
            raise ValueError("max_rows must be positive")
        if self.max_batch_bytes < 1:
            raise ValueError("max_batch_bytes must be positive")
        if self.alignment_bytes < 1:
            raise ValueError("alignment_bytes must be positive")


DEFAULT_RECORD_BATCH_LIMITS = RecordBatchLimits()


def validate_record_batch(
    header: BatchHeader, columns: tuple[ColumnDescriptor, ...], *, limits: RecordBatchLimits = DEFAULT_RECORD_BATCH_LIMITS
) -> None:
    """Validate the multi-column batch shape shared by host codecs and HDL models."""
    if header.column_count != len(columns):
        raise InvalidStreamError(f"header column_count {header.column_count} does not match {len(columns)} descriptors")
    if not columns:
        raise InvalidStreamError("record batch requires at least one column")
    if len(columns) > limits.max_columns:
        raise InvalidStreamError(f"column count {len(columns)} exceeds limit {limits.max_columns}")
    if header.row_count > limits.max_rows:
        raise InvalidStreamError(f"row_count {header.row_count} exceeds limit {limits.max_rows}")
    batch_end = header.payload_offset + header.payload_length
    if batch_end > limits.max_batch_bytes:
        raise InvalidStreamError(f"batch extent {batch_end} exceeds limit {limits.max_batch_bytes}")
    field_ids = [column.field_id for column in columns]
    if len(set(field_ids)) != len(field_ids):
        raise InvalidStreamError("column field_id values must be unique")
    previous_end = header.payload_offset
    for column in columns:
        if column.row_count != header.row_count:
            raise InvalidStreamError(f"column {column.field_id} row_count does not match batch header")
        if column.payload_offset % limits.alignment_bytes != 0:
            raise InvalidStreamError(f"column {column.field_id} payload offset must be {limits.alignment_bytes}-byte aligned")
        if column.payload_offset < previous_end:
            raise InvalidStreamError(f"column {column.field_id} payload overlaps the previous column")
        if column.payload_offset + column.payload_length > batch_end:
            raise InvalidStreamError(f"column {column.field_id} payload extends beyond the batch payload window")
        previous_end = column.payload_offset + column.payload_length


def _validate_u8(name: str, value: int) -> None:
    if not 0 <= value <= 0xFF:
        raise ValueError(f"{name} must fit in 8 bits")


def _validate_u16(name: str, value: int) -> None:
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"{name} must fit in 16 bits")


def _validate_u32(name: str, value: int) -> None:
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"{name} must fit in 32 bits")


def _validate_u64(name: str, value: int) -> None:
    if not 0 <= value <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError(f"{name} must fit in 64 bits")
