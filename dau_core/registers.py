from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, IntFlag

DAU_MAGIC_WORD = 0x44415531


@dataclass(frozen=True, order=True)
class RegisterMapVersion:
    major: int
    minor: int

    def __post_init__(self) -> None:
        _validate_u16("major", self.major)
        _validate_u16("minor", self.minor)

    def to_u32(self) -> int:
        return (self.major << 16) | self.minor

    @classmethod
    def from_u32(cls, value: int) -> "RegisterMapVersion":
        _validate_u32("value", value)
        return cls(major=(value >> 16) & 0xFFFF, minor=value & 0xFFFF)


@dataclass(frozen=True, order=True)
class StreamProtocolVersion:
    major: int
    minor: int

    def __post_init__(self) -> None:
        _validate_u16("major", self.major)
        _validate_u16("minor", self.minor)

    def to_u32(self) -> int:
        return (self.major << 16) | self.minor

    @classmethod
    def from_u32(cls, value: int) -> "StreamProtocolVersion":
        _validate_u32("value", value)
        return cls(major=(value >> 16) & 0xFFFF, minor=value & 0xFFFF)


class RegisterOffset(IntEnum):
    MAGIC = 0x0000
    REGISTER_MAP_VERSION = 0x0004
    STREAM_PROTOCOL_VERSION = 0x0008
    PLATFORM_ID = 0x000C
    SHELL_VERSION = 0x0010
    BUILD_ID_LOW = 0x0014
    BUILD_ID_HIGH = 0x0018
    STATUS = 0x001C
    RESET = 0x0020
    DMA_CONSTRAINTS = 0x0024
    OPERATOR_BITMAP = 0x0028
    LAST_ERROR = 0x002C
    H2C_BYTES_LOW = 0x0030
    H2C_BYTES_HIGH = 0x0034
    C2H_BYTES_LOW = 0x0038
    C2H_BYTES_HIGH = 0x003C
    ROWS_LOW = 0x0040
    ROWS_HIGH = 0x0044
    BATCHES = 0x0048
    STALLS = 0x004C
    JOB_CONTROL = 0x0050
    JOB_STATUS = 0x0054
    INPUT_ADDRESS_LOW = 0x0058
    INPUT_ADDRESS_HIGH = 0x005C
    INPUT_LENGTH_LOW = 0x0060
    INPUT_LENGTH_HIGH = 0x0064
    OUTPUT_ADDRESS_LOW = 0x0068
    OUTPUT_ADDRESS_HIGH = 0x006C
    OUTPUT_LENGTH_LOW = 0x0070
    OUTPUT_LENGTH_HIGH = 0x0074
    OPERATION = 0x0078
    RESULT_LENGTH_LOW = 0x007C
    RESULT_LENGTH_HIGH = 0x0080
    # static composed-pipeline configuration (the OHLCV shell): the filter
    # row count comes from INPUT_LENGTH (one row per 64-bit word), never a
    # separate register
    PIPELINE_FILTER_OP = 0x0090  # [2:0] op0, [5:4] combine, [10:8] op1, [16] target lane
    PIPELINE_FILTER_CONSTANT0 = 0x0094
    PIPELINE_FILTER_CONSTANT1 = 0x0098
    PIPELINE_MAP_OP = 0x009C  # [2:0] op, [12:8] shift, [16] target lane
    PIPELINE_MAP_CONSTANT = 0x00A0
    PIPELINE_SELECTED_COUNT_LOW = 0x00B0
    PIPELINE_SELECTED_COUNT_HIGH = 0x00B4
    PIPELINE_GROUP_COUNT_LOW = 0x00B8
    PIPELINE_GROUP_COUNT_HIGH = 0x00BC


# broadcast-NoC lane register block: one NOC_LANE_STRIDE-sized window per
# lane starting at NOC_LANE_BASE (inside the 4 KiB register window; the
# global JOB_*/INPUT_* registers stay authoritative for the shared scan)
NOC_LANE_BASE = 0x0100
NOC_LANE_STRIDE = 0x20


class NocLaneRegisterOffset(IntEnum):
    OUTPUT_ADDRESS_LOW = 0x00
    RESULT_LENGTH_LOW = 0x04
    RECORD_COUNT_LOW = 0x08
    RECORD_COUNT_HIGH = 0x0C
    LANE_ERROR = 0x10


def noc_lane_register(lane: int, offset: NocLaneRegisterOffset | int) -> int:
    """BAR window offset of one lane's register."""
    if lane < 0:
        raise ValueError("lane must be non-negative")
    return NOC_LANE_BASE + lane * NOC_LANE_STRIDE + int(offset)


class ResetScope(IntFlag):
    OPERATOR = 1 << 0
    STREAM = 1 << 1
    DMA = 1 << 2
    SHELL = 1 << 3


class JobControlFlag(IntFlag):
    START = 1 << 0
    ABORT = 1 << 1


class JobStatusFlag(IntFlag):
    IDLE = 1 << 0
    BUSY = 1 << 1
    DONE = 1 << 2
    ERROR = 1 << 3


class RegisterWriteError(ValueError):
    pass


_RESET_SCOPE_MASK = int(ResetScope.OPERATOR | ResetScope.STREAM | ResetScope.DMA | ResetScope.SHELL)
_JOB_CONTROL_MASK = int(JobControlFlag.START | JobControlFlag.ABORT)


@dataclass(frozen=True)
class StreamJobRegisterContract:
    register_window_offset: int = 0x1000
    input_buffer_address: int = 0x0000_0000
    input_buffer_bytes: int = 0x0010_0000
    output_buffer_address: int = 0x0010_0000
    output_buffer_bytes: int = 0x0010_0000
    result_bytes: int = 136
    start_offset: RegisterOffset = RegisterOffset.JOB_CONTROL
    status_offset: RegisterOffset = RegisterOffset.JOB_STATUS
    error_offset: RegisterOffset = RegisterOffset.LAST_ERROR
    result_length_low_offset: RegisterOffset = RegisterOffset.RESULT_LENGTH_LOW

    def __post_init__(self) -> None:
        _validate_u32("register_window_offset", self.register_window_offset)
        for name in ("input_buffer_address", "input_buffer_bytes", "output_buffer_address", "output_buffer_bytes", "result_bytes"):
            _validate_u64(name, getattr(self, name))

    def manifest_items(self) -> tuple[tuple[str, str], ...]:
        return (
            ("register_window_offset", _hex32(self.register_window_offset)),
            ("job_control_offset", _hex32(int(self.start_offset))),
            ("job_status_offset", _hex32(int(self.status_offset))),
            ("last_error_offset", _hex32(int(self.error_offset))),
            ("input_address_low_offset", _hex32(int(RegisterOffset.INPUT_ADDRESS_LOW))),
            ("input_length_low_offset", _hex32(int(RegisterOffset.INPUT_LENGTH_LOW))),
            ("output_address_low_offset", _hex32(int(RegisterOffset.OUTPUT_ADDRESS_LOW))),
            ("output_length_low_offset", _hex32(int(RegisterOffset.OUTPUT_LENGTH_LOW))),
            ("result_length_low_offset", _hex32(int(self.result_length_low_offset))),
            ("job_start_bit", str(_flag_bit(JobControlFlag.START))),
            ("job_busy_bit", str(_flag_bit(JobStatusFlag.BUSY))),
            ("job_done_bit", str(_flag_bit(JobStatusFlag.DONE))),
            ("job_error_bit", str(_flag_bit(JobStatusFlag.ERROR))),
            ("input_buffer_address", _hex64(self.input_buffer_address)),
            ("input_buffer_bytes", _hex64(self.input_buffer_bytes)),
            ("output_buffer_address", _hex64(self.output_buffer_address)),
            ("output_buffer_bytes", _hex64(self.output_buffer_bytes)),
            ("result_bytes", _hex64(self.result_bytes)),
        )


@dataclass(frozen=True)
class RegisterBlockSnapshot:
    register_map_version: RegisterMapVersion = field(default_factory=lambda: RegisterMapVersion(0, 1))
    stream_protocol_version: StreamProtocolVersion = field(default_factory=lambda: StreamProtocolVersion(0, 1))
    platform_id: str = "NITE"
    shell_version: int = 0
    build_id: int = 0
    status: int = 0
    dma_constraints: int = 0
    operator_bitmap: int = 0
    last_error: int = 0
    h2c_bytes: int = 0
    c2h_bytes: int = 0
    rows: int = 0
    batches: int = 0
    stalls: int = 0
    job_status: int = int(JobStatusFlag.IDLE)
    input_address: int = 0x0000_0000
    input_length: int = 0
    output_address: int = 0x0010_0000
    output_length: int = 136
    operation: int = 0
    result_length: int = 0

    def __post_init__(self) -> None:
        _platform_id_to_u32(self.platform_id)
        for name in ("shell_version", "status", "dma_constraints", "operator_bitmap", "last_error", "batches", "stalls", "job_status", "operation"):
            _validate_u32(name, getattr(self, name))
        for name in (
            "build_id",
            "h2c_bytes",
            "c2h_bytes",
            "rows",
            "input_address",
            "input_length",
            "output_address",
            "output_length",
            "result_length",
        ):
            _validate_u64(name, getattr(self, name))

    def read_u32(self, offset: RegisterOffset | int) -> int:
        values = {
            RegisterOffset.MAGIC: DAU_MAGIC_WORD,
            RegisterOffset.REGISTER_MAP_VERSION: self.register_map_version.to_u32(),
            RegisterOffset.STREAM_PROTOCOL_VERSION: self.stream_protocol_version.to_u32(),
            RegisterOffset.PLATFORM_ID: _platform_id_to_u32(self.platform_id),
            RegisterOffset.SHELL_VERSION: self.shell_version,
            RegisterOffset.BUILD_ID_LOW: _low_u32(self.build_id),
            RegisterOffset.BUILD_ID_HIGH: _high_u32(self.build_id),
            RegisterOffset.STATUS: self.status,
            RegisterOffset.RESET: 0,
            RegisterOffset.DMA_CONSTRAINTS: self.dma_constraints,
            RegisterOffset.OPERATOR_BITMAP: self.operator_bitmap,
            RegisterOffset.LAST_ERROR: self.last_error,
            RegisterOffset.H2C_BYTES_LOW: _low_u32(self.h2c_bytes),
            RegisterOffset.H2C_BYTES_HIGH: _high_u32(self.h2c_bytes),
            RegisterOffset.C2H_BYTES_LOW: _low_u32(self.c2h_bytes),
            RegisterOffset.C2H_BYTES_HIGH: _high_u32(self.c2h_bytes),
            RegisterOffset.ROWS_LOW: _low_u32(self.rows),
            RegisterOffset.ROWS_HIGH: _high_u32(self.rows),
            RegisterOffset.BATCHES: self.batches,
            RegisterOffset.STALLS: self.stalls,
            RegisterOffset.JOB_CONTROL: 0,
            RegisterOffset.JOB_STATUS: self.job_status,
            RegisterOffset.INPUT_ADDRESS_LOW: _low_u32(self.input_address),
            RegisterOffset.INPUT_ADDRESS_HIGH: _high_u32(self.input_address),
            RegisterOffset.INPUT_LENGTH_LOW: _low_u32(self.input_length),
            RegisterOffset.INPUT_LENGTH_HIGH: _high_u32(self.input_length),
            RegisterOffset.OUTPUT_ADDRESS_LOW: _low_u32(self.output_address),
            RegisterOffset.OUTPUT_ADDRESS_HIGH: _high_u32(self.output_address),
            RegisterOffset.OUTPUT_LENGTH_LOW: _low_u32(self.output_length),
            RegisterOffset.OUTPUT_LENGTH_HIGH: _high_u32(self.output_length),
            RegisterOffset.OPERATION: self.operation,
            RegisterOffset.RESULT_LENGTH_LOW: _low_u32(self.result_length),
            RegisterOffset.RESULT_LENGTH_HIGH: _high_u32(self.result_length),
        }
        try:
            register_offset = RegisterOffset(offset)
        except ValueError:
            return 0xFFFF_FFFF
        return values.get(register_offset, 0xFFFF_FFFF)

    def write_u32(self, offset: RegisterOffset | int, value: int) -> ResetScope | JobControlFlag:
        _validate_u32("value", value)
        register_offset = RegisterOffset(offset)
        if register_offset == RegisterOffset.RESET:
            if value & ~_RESET_SCOPE_MASK:
                raise RegisterWriteError(f"unknown reset scope bits: 0x{value & ~_RESET_SCOPE_MASK:x}")
            return ResetScope(value)
        if register_offset == RegisterOffset.JOB_CONTROL:
            if value & ~_JOB_CONTROL_MASK:
                raise RegisterWriteError(f"unknown job control bits: 0x{value & ~_JOB_CONTROL_MASK:x}")
            return JobControlFlag(value)
        raise RegisterWriteError(f"{register_offset.name} is read-only")


def _platform_id_to_u32(platform_id: str) -> int:
    encoded = platform_id.encode("ascii")
    if not 1 <= len(encoded) <= 4:
        raise ValueError("platform_id must be 1 to 4 ASCII bytes")
    return int.from_bytes(encoded.ljust(4, b"\x00"), "little")


def _low_u32(value: int) -> int:
    return value & 0xFFFF_FFFF


def _high_u32(value: int) -> int:
    return (value >> 32) & 0xFFFF_FFFF


def _hex32(value: int) -> str:
    _validate_u32("value", value)
    return f"0x{value:08x}"


def _hex64(value: int) -> str:
    _validate_u64("value", value)
    return f"0x{value:016x}"


def _flag_bit(flag: IntFlag) -> int:
    value = int(flag)
    if value == 0 or value & (value - 1):
        raise ValueError(f"flag must contain exactly one bit: {flag}")
    return value.bit_length() - 1


def _validate_u16(name: str, value: int) -> None:
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"{name} must fit in 16 bits")


def _validate_u32(name: str, value: int) -> None:
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"{name} must fit in 32 bits")


def _validate_u64(name: str, value: int) -> None:
    if not 0 <= value <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError(f"{name} must fit in 64 bits")


DEFAULT_STREAM_JOB_REGISTER_CONTRACT = StreamJobRegisterContract()
