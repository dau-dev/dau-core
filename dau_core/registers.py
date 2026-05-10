from __future__ import annotations

from dataclasses import dataclass
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


def _validate_u16(name: str, value: int) -> None:
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"{name} must fit in 16 bits")


def _validate_u32(name: str, value: int) -> None:
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"{name} must fit in 32 bits")
