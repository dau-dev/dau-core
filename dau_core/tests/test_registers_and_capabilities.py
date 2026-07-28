from importlib.resources import files

import pytest

from dau_core.capabilities import DeviceCapabilities, DmaConstraints, OperatorCapability
from dau_core.registers import (
    CURRENT_REGISTER_MAP_VERSION,
    DAU_MAGIC_WORD,
    DEFAULT_STREAM_JOB_REGISTER_CONTRACT,
    JobControlFlag,
    JobStatusFlag,
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
    assert RegisterOffset.JOB_CONTROL == 0x0050
    assert RegisterOffset.JOB_STATUS == 0x0054
    assert RegisterOffset.INPUT_ADDRESS_LOW == 0x0058
    assert RegisterOffset.OUTPUT_ADDRESS_LOW == 0x0068
    # two-phase load (additive): the free job-block slot between
    # RESULT_LENGTH_HIGH (0x0080) and the pipeline block (0x0090)
    assert RegisterOffset.LOAD_PHASE == 0x0084
    # register map 0.2: composed-capability words, additive, below the NoC
    # lane block at 0x0100
    assert CURRENT_REGISTER_MAP_VERSION == RegisterMapVersion(0, 2)
    assert CURRENT_REGISTER_MAP_VERSION.to_u32() == 0x00000002
    assert RegisterOffset.LANE_COUNT == 0x00C0
    assert RegisterOffset.HOST_OPCODE_BITMAP == 0x00C4
    assert RegisterOffset.SORT_CAPACITY == 0x00C8
    assert JobControlFlag.START == 1
    assert JobStatusFlag.ERROR == 1 << 3
    assert ResetScope.OPERATOR | ResetScope.STREAM == ResetScope.OPERATOR | ResetScope.STREAM


def test_register_block_snapshot_reads_identity_status_and_counters_as_u32_words():
    snapshot = RegisterBlockSnapshot(
        platform_id="DPV1",
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
        job_status=int(JobStatusFlag.DONE),
        input_address=0x0000_0000_0000_1000,
        input_length=0x0000_0000_0000_00C0,
        output_address=0x0000_0000_0000_2000,
        output_length=0x0000_0000_0000_0088,
        result_length=0x0000_0000_0000_0088,
        lane_count=4,
        host_opcode_bitmap=0x20,
        sort_capacity=16,
    )

    assert snapshot.read_u32(RegisterOffset.MAGIC) == DAU_MAGIC_WORD
    assert snapshot.read_u32(RegisterOffset.PLATFORM_ID) == int.from_bytes(b"DPV1", "little")
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
    assert snapshot.read_u32(RegisterOffset.JOB_CONTROL) == 0
    assert snapshot.read_u32(RegisterOffset.JOB_STATUS) == int(JobStatusFlag.DONE)
    assert snapshot.read_u32(RegisterOffset.INPUT_ADDRESS_LOW) == 0x0000_1000
    assert snapshot.read_u32(RegisterOffset.INPUT_LENGTH_LOW) == 0x0000_00C0
    assert snapshot.read_u32(RegisterOffset.OUTPUT_ADDRESS_LOW) == 0x0000_2000
    assert snapshot.read_u32(RegisterOffset.OUTPUT_LENGTH_LOW) == 0x0000_0088
    assert snapshot.read_u32(RegisterOffset.RESULT_LENGTH_LOW) == 0x0000_0088
    assert snapshot.read_u32(RegisterOffset.LANE_COUNT) == 4
    assert snapshot.read_u32(RegisterOffset.HOST_OPCODE_BITMAP) == 0x20
    assert snapshot.read_u32(RegisterOffset.SORT_CAPACITY) == 16
    assert snapshot.read_u32(0x0FFC) == 0xFFFF_FFFF


def test_register_block_snapshot_models_reset_write_side_effects_only():
    snapshot = RegisterBlockSnapshot()

    assert snapshot.write_u32(RegisterOffset.RESET, int(ResetScope.OPERATOR | ResetScope.DMA)) == ResetScope.OPERATOR | ResetScope.DMA
    assert snapshot.write_u32(RegisterOffset.RESET, 0) == ResetScope(0)
    assert snapshot.write_u32(RegisterOffset.JOB_CONTROL, int(JobControlFlag.START)) == JobControlFlag.START

    with pytest.raises(RegisterWriteError, match="MAGIC.*read-only"):
        snapshot.write_u32(RegisterOffset.MAGIC, 1)
    with pytest.raises(RegisterWriteError, match="unknown reset scope bits"):
        snapshot.write_u32(RegisterOffset.RESET, 0x10)
    with pytest.raises(RegisterWriteError, match="unknown job control bits"):
        snapshot.write_u32(RegisterOffset.JOB_CONTROL, 0x4)


def test_stream_job_register_contract_names_host_trigger_status_and_result_addresses():
    contract = DEFAULT_STREAM_JOB_REGISTER_CONTRACT

    assert contract.register_window_offset == 0x1000
    assert contract.input_buffer_address == 0x0000_0000
    assert contract.input_buffer_bytes == 0x0010_0000
    assert contract.output_buffer_address == 0x0010_0000
    assert contract.output_buffer_bytes == 0x0010_0000
    assert contract.result_bytes == 136
    assert contract.start_offset == RegisterOffset.JOB_CONTROL
    assert contract.status_offset == RegisterOffset.JOB_STATUS
    assert contract.error_offset == RegisterOffset.LAST_ERROR
    assert contract.result_length_low_offset == RegisterOffset.RESULT_LENGTH_LOW

    assert dict(contract.manifest_items()) == {
        "register_window_offset": "0x00001000",
        "job_control_offset": "0x00000050",
        "job_status_offset": "0x00000054",
        "last_error_offset": "0x0000002c",
        "input_address_low_offset": "0x00000058",
        "input_length_low_offset": "0x00000060",
        "output_address_low_offset": "0x00000068",
        "output_length_low_offset": "0x00000070",
        "result_length_low_offset": "0x0000007c",
        "job_start_bit": "0",
        "job_busy_bit": "1",
        "job_done_bit": "2",
        "job_error_bit": "3",
        "input_buffer_address": "0x0000000000000000",
        "input_buffer_bytes": "0x0000000000100000",
        "output_buffer_address": "0x0000000000100000",
        "output_buffer_bytes": "0x0000000000100000",
        "result_bytes": "0x0000000000000088",
    }


def test_packaged_identity_register_hdl_exposes_reset_write_pulse_ports():
    source = files("dau_core.hdl").joinpath("dau_identity_registers.sv").read_text()

    assert "input  wire logic        wen" in source
    assert "input  wire logic [31:0] wdata" in source
    assert "output logic [31:0] reset_request" in source
    assert "assign reset_request = (wen && (addr == 16'h0020)) ? wdata : 32'h0000_0000;" in source


def test_packaged_identity_register_hdl_advertises_the_current_register_map():
    """The SV default and decode stay pinned to the Python contract: the
    0.2 bump adds the composed-capability words at 0x00c0..0x00c8."""
    source = files("dau_core.hdl").joinpath("dau_identity_registers.sv").read_text()

    version_word = CURRENT_REGISTER_MAP_VERSION.to_u32()
    assert f"parameter logic [31:0] REGISTER_MAP_VERSION = 32'h{version_word >> 16:04x}_{version_word & 0xFFFF:04x}" in source
    assert "parameter logic [31:0] LANE_COUNT = 32'h0000_0000" in source
    assert "parameter logic [31:0] HOST_OPCODE_BITMAP = 32'h0000_0000" in source
    assert "parameter logic [31:0] SORT_CAPACITY = 32'h0000_0000" in source
    assert "(addr == 16'h00c0) ? LANE_COUNT :" in source
    assert "(addr == 16'h00c4) ? HOST_OPCODE_BITMAP :" in source
    assert "(addr == 16'h00c8) ? SORT_CAPACITY :" in source

    # the AXI-Lite wrapper forwards the capability words and matches the
    # 0.2 default — a wrapper-based shell must not pin the old ABI
    wrapper = files("dau_core.hdl").joinpath("dau_identity_axil.v").read_text()
    assert f"parameter [31:0] REGISTER_MAP_VERSION = 32'h{version_word >> 16:04x}_{version_word & 0xFFFF:04x}" in wrapper
    for parameter in ("LANE_COUNT", "HOST_OPCODE_BITMAP", "SORT_CAPACITY"):
        assert f"parameter [31:0] {parameter} = 32'h0000_0000" in wrapper
        assert f".{parameter}({parameter})" in wrapper

    # the host-side snapshot default tracks the current register map
    assert RegisterBlockSnapshot().register_map_version == CURRENT_REGISTER_MAP_VERSION


def test_device_capabilities_report_supported_operations_and_dma_constraints():
    capabilities = DeviceCapabilities(
        platform_id="dpv1",
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


def test_device_capabilities_keep_host_computed_operators_apart_from_hardware():
    """AVERAGE is a protocol-level host divide over SUM/COUNT results — it
    is advertised in host_operators, never as a hardware operator."""
    average = OperatorCapability(
        opcode=OperationCode.AVERAGE,
        input_types=(LogicalType.INT32,),
        output_type=LogicalType.FLOAT64,
        supports_group_by=False,
    )
    capabilities = DeviceCapabilities(
        platform_id="dpv1",
        pci_vendor_id=0x10EE,
        pci_device_id=0x7011,
        register_map_version=RegisterMapVersion(0, 2),
        stream_protocol_version=StreamProtocolVersion(0, 1),
        dma=DmaConstraints(alignment_bytes=64, descriptor_bytes=32, max_transfer_bytes=(1 << 28) - 1, read_channels=1, write_channels=1),
        operators=(
            OperatorCapability(opcode=OperationCode.SUM, input_types=(LogicalType.INT32,), output_type=LogicalType.INT64, supports_group_by=False),
        ),
        host_operators=(average,),
    )

    assert capabilities.supports(OperationCode.SUM, LogicalType.INT32)
    assert not capabilities.supports(OperationCode.AVERAGE, LogicalType.INT32)
    assert capabilities.supports_host_computed(OperationCode.AVERAGE, LogicalType.INT32)
    assert not capabilities.supports_host_computed(OperationCode.SUM, LogicalType.INT32)
