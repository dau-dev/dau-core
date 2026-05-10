from dau_core.capabilities import DeviceCapabilities, DmaConstraints, OperatorCapability
from dau_core.registers import DAU_MAGIC_WORD, RegisterMapVersion, RegisterOffset, ResetScope, StreamProtocolVersion
from dau_core.stream import LogicalType, OperationCode


def test_register_contract_uses_dau_magic_and_aligned_offsets():
    assert DAU_MAGIC_WORD == 0x44415531
    assert RegisterMapVersion(0, 1).to_u32() == 0x00000001
    assert StreamProtocolVersion(0, 1).to_u32() == 0x00000001
    assert RegisterOffset.MAGIC == 0x0000
    assert RegisterOffset.STREAM_PROTOCOL_VERSION % 4 == 0
    assert ResetScope.OPERATOR | ResetScope.STREAM == ResetScope.OPERATOR | ResetScope.STREAM


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
