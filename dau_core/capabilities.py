from __future__ import annotations

from dataclasses import dataclass

from .registers import RegisterMapVersion, StreamProtocolVersion
from .stream import LogicalType, OperationCode

# DAU platform variant 1 (dpv1) identity — the single home for these
# values; drivers, orchestration, and tooling import them rather than
# repeating literals. The wire identity word stays "NITE" (deployed
# bitstreams report it in the PLATFORM_ID register).
DPV1_PLATFORM_ID = "NITE"
XILINX_VENDOR_ID = 0x10EE
DPV1_DEVICE_ID = 0x7011
DPV1_PCI_ID = f"{XILINX_VENDOR_ID:04x}:{DPV1_DEVICE_ID:04x}"


@dataclass(frozen=True)
class DmaConstraints:
    alignment_bytes: int
    descriptor_bytes: int
    max_transfer_bytes: int
    read_channels: int
    write_channels: int

    def __post_init__(self) -> None:
        if self.alignment_bytes <= 0:
            raise ValueError("alignment_bytes must be positive")
        if self.descriptor_bytes <= 0:
            raise ValueError("descriptor_bytes must be positive")
        if self.max_transfer_bytes <= 0:
            raise ValueError("max_transfer_bytes must be positive")
        if self.read_channels < 0 or self.write_channels < 0:
            raise ValueError("DMA channel counts cannot be negative")

    def is_aligned(self, value: int) -> bool:
        return value % self.alignment_bytes == 0

    @property
    def max_aligned_transfer_bytes(self) -> int:
        return self.max_transfer_bytes - (self.max_transfer_bytes % self.alignment_bytes)


@dataclass(frozen=True)
class OperatorCapability:
    opcode: OperationCode
    input_types: tuple[LogicalType, ...]
    output_type: LogicalType
    supports_group_by: bool

    def supports(self, opcode: OperationCode, logical_type: LogicalType) -> bool:
        return self.opcode == opcode and logical_type in self.input_types


@dataclass(frozen=True)
class DeviceCapabilities:
    platform_id: str
    pci_vendor_id: int
    pci_device_id: int
    register_map_version: RegisterMapVersion
    stream_protocol_version: StreamProtocolVersion
    dma: DmaConstraints
    operators: tuple[OperatorCapability, ...]

    def supports(self, opcode: OperationCode, logical_type: LogicalType) -> bool:
        return any(operator.supports(opcode, logical_type) for operator in self.operators)
