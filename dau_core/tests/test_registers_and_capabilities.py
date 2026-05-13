from importlib.resources import files

import pytest

from dau_core.capabilities import DeviceCapabilities, DmaConstraints, OperatorCapability
from dau_core.registers import (
    DAU_MAGIC_WORD,
    RegisterBlockSnapshot,
    RegisterMapVersion,
    RegisterOffset,
    RegisterWriteError,
    ResetScope,
    StreamProtocolVersion,
)
from dau_core.stream import LogicalType, OperationCode


def test_register_contract_uses_dau_magic_and_aligned_offsets():
    assert DAU_MAGIC_WORD == 0x44415531
    assert RegisterMapVersion(0, 1).to_u32() == 0x00000001
    assert StreamProtocolVersion(0, 1).to_u32() == 0x00000001
    assert RegisterOffset.MAGIC == 0x0000
    assert RegisterOffset.STREAM_PROTOCOL_VERSION % 4 == 0
    assert ResetScope.OPERATOR | ResetScope.STREAM == ResetScope.OPERATOR | ResetScope.STREAM


def test_register_block_snapshot_reads_identity_status_and_counters_as_u32_words():
    snapshot = RegisterBlockSnapshot(
        platform_id="NITE",
        shell_version=0x0102_0304,
        build_id=0x1122_3344_5566_7788,
        status=0x0000_0003,
        dma_constraints=0x0040_0020,
        operator_bitmap=0b101,
        last_error=0xDEAD_BEEF,
        h2c_bytes=0x0102_0304_0506_0708,
        c2h_bytes=0x1112_1314_1516_1718,
        rows=0x2122_2324_2526_2728,
        batches=9,
        stalls=10,
    )

    assert snapshot.read_u32(RegisterOffset.MAGIC) == DAU_MAGIC_WORD
    assert snapshot.read_u32(RegisterOffset.PLATFORM_ID) == int.from_bytes(b"NITE", "little")
    assert snapshot.read_u32(RegisterOffset.SHELL_VERSION) == 0x0102_0304
    assert snapshot.read_u32(RegisterOffset.BUILD_ID_LOW) == 0x5566_7788
    assert snapshot.read_u32(RegisterOffset.BUILD_ID_HIGH) == 0x1122_3344
    assert snapshot.read_u32(RegisterOffset.STATUS) == 0x0000_0003
    assert snapshot.read_u32(RegisterOffset.RESET) == 0
    assert snapshot.read_u32(RegisterOffset.DMA_CONSTRAINTS) == 0x0040_0020
    assert snapshot.read_u32(RegisterOffset.OPERATOR_BITMAP) == 0b101
    assert snapshot.read_u32(RegisterOffset.LAST_ERROR) == 0xDEAD_BEEF
    assert snapshot.read_u32(RegisterOffset.H2C_BYTES_LOW) == 0x0506_0708
    assert snapshot.read_u32(RegisterOffset.H2C_BYTES_HIGH) == 0x0102_0304
    assert snapshot.read_u32(RegisterOffset.C2H_BYTES_LOW) == 0x1516_1718
    assert snapshot.read_u32(RegisterOffset.C2H_BYTES_HIGH) == 0x1112_1314
    assert snapshot.read_u32(RegisterOffset.ROWS_LOW) == 0x2526_2728
    assert snapshot.read_u32(RegisterOffset.ROWS_HIGH) == 0x2122_2324
    assert snapshot.read_u32(RegisterOffset.BATCHES) == 9
    assert snapshot.read_u32(RegisterOffset.STALLS) == 10
    assert snapshot.read_u32(0x0FFC) == 0xFFFF_FFFF


def test_register_block_snapshot_models_reset_write_side_effects_only():
    snapshot = RegisterBlockSnapshot()

    assert snapshot.write_u32(RegisterOffset.RESET, int(ResetScope.OPERATOR | ResetScope.DMA)) == ResetScope.OPERATOR | ResetScope.DMA
    assert snapshot.write_u32(RegisterOffset.RESET, 0) == ResetScope(0)

    with pytest.raises(RegisterWriteError, match="MAGIC.*read-only"):
        snapshot.write_u32(RegisterOffset.MAGIC, 1)
    with pytest.raises(RegisterWriteError, match="unknown reset scope bits"):
        snapshot.write_u32(RegisterOffset.RESET, 0x10)


def test_packaged_identity_register_hdl_exposes_reset_write_pulse_ports():
    source = files("dau_core.hdl").joinpath("dau_identity_registers.sv").read_text()

    assert "input  wire logic        wen" in source
    assert "input  wire logic [31:0] wdata" in source
    assert "output logic [31:0] reset_request" in source
    assert "assign reset_request = (wen && (addr == 16'h0020)) ? wdata : 32'h0000_0000;" in source


def test_device_capabilities_report_supported_operations_and_dma_constraints():
    capabilities = DeviceCapabilities(
        platform_id="nitefury",
        pci_vendor_id=0x10EE,
        pci_device_id=0x7011,
        register_map_version=RegisterMapVersion(0, 1),
        stream_protocol_version=StreamProtocolVersion(0, 1),
        dma=DmaConstraints(alignment_bytes=64, descriptor_bytes=32, max_transfer_bytes=(1 << 28) - 1, read_channels=1, write_channels=1),
        operators=(
            OperatorCapability(
                opcode=OperationCode.AVERAGE,
                input_types=(LogicalType.INT32, LogicalType.FLOAT32),
                output_type=LogicalType.FLOAT64,
                supports_group_by=False,
            ),
        ),
    )

    assert capabilities.supports(OperationCode.AVERAGE, LogicalType.INT32)
    assert capabilities.supports(OperationCode.AVERAGE, LogicalType.FLOAT32)
    assert not capabilities.supports(OperationCode.AVERAGE, LogicalType.BOOL)
    assert capabilities.dma.is_aligned(128)
    assert not capabilities.dma.is_aligned(96)
    assert capabilities.dma.max_aligned_transfer_bytes == (1 << 28) - 64
