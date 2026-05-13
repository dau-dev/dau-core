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


class ResetScope(IntFlag):
    OPERATOR = 1 << 0
    STREAM = 1 << 1
    DMA = 1 << 2
    SHELL = 1 << 3


class RegisterWriteError(ValueError):
    pass


_RESET_SCOPE_MASK = int(ResetScope.OPERATOR | ResetScope.STREAM | ResetScope.DMA | ResetScope.SHELL)


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

    def __post_init__(self) -> None:
        _platform_id_to_u32(self.platform_id)
        for name in ("shell_version", "status", "dma_constraints", "operator_bitmap", "last_error", "batches", "stalls"):
            _validate_u32(name, getattr(self, name))
        for name in ("build_id", "h2c_bytes", "c2h_bytes", "rows"):
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
        }
        try:
            register_offset = RegisterOffset(offset)
        except ValueError:
            return 0xFFFF_FFFF
        return values.get(register_offset, 0xFFFF_FFFF)

    def write_u32(self, offset: RegisterOffset | int, value: int) -> ResetScope:
        _validate_u32("value", value)
        register_offset = RegisterOffset(offset)
        if register_offset != RegisterOffset.RESET:
            raise RegisterWriteError(f"{register_offset.name} is read-only")
        if value & ~_RESET_SCOPE_MASK:
            raise RegisterWriteError(f"unknown reset scope bits: 0x{value & ~_RESET_SCOPE_MASK:x}")
        return ResetScope(value)


def _platform_id_to_u32(platform_id: str) -> int:
    encoded = platform_id.encode("ascii")
    if not 1 <= len(encoded) <= 4:
        raise ValueError("platform_id must be 1 to 4 ASCII bytes")
    return int.from_bytes(encoded.ljust(4, b"\x00"), "little")


def _low_u32(value: int) -> int:
    return value & 0xFFFF_FFFF


def _high_u32(value: int) -> int:
    return (value >> 32) & 0xFFFF_FFFF


def _validate_u16(name: str, value: int) -> None:
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"{name} must fit in 16 bits")


def _validate_u32(name: str, value: int) -> None:
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"{name} must fit in 32 bits")


def _validate_u64(name: str, value: int) -> None:
    if not 0 <= value <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError(f"{name} must fit in 64 bits")
