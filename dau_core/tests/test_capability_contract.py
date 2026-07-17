from __future__ import annotations

import json

import pytest

from dau_core.capability_contract import (
    CAPABILITY_CONTRACT_SCHEMA,
    CapabilityContract,
    LaneCapability,
    capability_contract,
    opcode_bitmap,
    tile_capability,
)
from dau_core.composition import LaneSpec, ScanCompositionSpec, TileSpec
from dau_core.cores import ContractKind, CoreKind, cores_of_kind
from dau_core.design import UnitKind
from dau_core.registers import CURRENT_REGISTER_MAP_VERSION
from dau_core.stream import LogicalType, OperationCode
from dau_core.tests.test_composition import bar_noc_spec, sorted_scan_spec

_HW_AGGREGATION_OPCODES = (OperationCode.MIN, OperationCode.MAX, OperationCode.SUM, OperationCode.COUNT)


def test_opcode_bitmap_encoding_matches_the_register_word() -> None:
    assert opcode_bitmap(()) == 0
    assert opcode_bitmap(_HW_AGGREGATION_OPCODES) == 0x1E
    assert opcode_bitmap((OperationCode.AVERAGE,)) == 0x20


def test_registry_declares_hardware_opcodes_only_for_opcode_driven_tiles() -> None:
    """MIN/MAX/SUM/COUNT are the implemented HDL opcodes; AVERAGE is
    protocol-level (host divide) and must never be declared as hardware."""
    declared = {d.name: d for d in cores_of_kind(CoreKind.OPERATOR) if d.opcodes}
    assert set(declared) == {
        "int32-stream-aggregation",
        "int32-arrow-lite-stream-aggregation",
        "int32-record-batch-aggregation",
        "int32-aggregation-tile",
    }
    for definition in declared.values():
        assert definition.opcodes == _HW_AGGREGATION_OPCODES, definition.name
        assert OperationCode.AVERAGE not in definition.opcodes, definition.name
        assert definition.opcode_input_types == (LogicalType.INT32,), definition.name
        assert definition.unit_kind == UnitKind.AGGREGATION, definition.name


def test_registry_declares_sorter_capacity() -> None:
    sorter = tile_capability("int32-bitonic-sorter")
    assert sorter.unit_kind == UnitKind.SORT
    assert sorter.row_capacity == 16
    assert sorter.opcodes == ()
    assert sorter.count_port == "sorted_count"
    assert sorter.contract_kind == ContractKind.ROW_STREAM


def test_bar_noc_contract_advertises_no_scalar_aggregation_hardware() -> None:
    contract = capability_contract(bar_noc_spec(lanes=4))
    assert contract.lane_count == 4
    assert contract.operator_bitmap == 0x0000_0000  # bar tiles execute no scalar OperationCode
    assert contract.host_opcode_bitmap == 0x0000_0000
    assert contract.sort_capacity == 0
    assert contract.register_map_version == CURRENT_REGISTER_MAP_VERSION
    lane = contract.lanes[0]
    assert lane.tile.core == "int32-bar-aggregation"
    assert lane.tile.count_port == "bar_count"
    assert lane.partition is not None and lane.partition.core == "int32-pair-key-filter"
    assert dict(lane.partition.config)["cfg_key_match"] == "32'd0"


def test_sorted_scan_contract_carries_sorter_capacity_and_splitters() -> None:
    contract = capability_contract(sorted_scan_spec(lanes=4))
    assert contract.lane_count == 4
    assert contract.sort_capacity == 16
    assert contract.operator_bitmap == 0
    assert contract.partitioner is not None
    assert contract.partitioner.core == "int32-range-partitioner"
    assert "cfg_splitters" in dict(contract.partitioner.config)


def test_scalar_aggregation_tile_contract_advertises_hardware_and_host_truth() -> None:
    contract = CapabilityContract(
        name="mm-stream-job",
        lanes=(LaneCapability(tile=tile_capability("int32-record-batch-aggregation")),),
    )
    assert contract.hardware_opcodes == _HW_AGGREGATION_OPCODES
    assert contract.operator_bitmap == 0x1E
    # AVERAGE is representable host-side (SUM/COUNT divide), never hardware
    assert contract.host_opcodes == (OperationCode.AVERAGE,)
    assert contract.host_opcode_bitmap == 0x20
    assert OperationCode.AVERAGE not in contract.hardware_opcodes


def test_contract_mapping_round_trips_and_is_json_able() -> None:
    contract = capability_contract(sorted_scan_spec(lanes=2))
    mapping = contract.to_mapping()
    assert mapping["contract"] == "dau-capability-contract"
    assert mapping["schema"] == CAPABILITY_CONTRACT_SCHEMA
    assert mapping["register_map_version"] == "0.2"
    assert mapping["operator_bitmap"] == "0x00000000"
    assert mapping["lane_count"] == 2
    assert mapping["sort_capacity"] == 16
    restored = CapabilityContract.from_mapping(json.loads(json.dumps(mapping)))
    assert restored == contract

    with pytest.raises(ValueError, match="not a dau capability contract"):
        CapabilityContract.from_mapping({"contract": "other"})
    with pytest.raises(ValueError, match="unsupported capability contract schema"):
        CapabilityContract.from_mapping({"contract": "dau-capability-contract", "schema": 999})


def test_contract_to_design_counts_units_by_identical_tile() -> None:
    aggregation = tile_capability("int32-record-batch-aggregation")
    sorter = tile_capability("int32-bitonic-sorter")
    contract = CapabilityContract(
        name="mixed",
        lanes=(
            LaneCapability(tile=aggregation),
            LaneCapability(tile=aggregation),
            LaneCapability(tile=sorter),
            LaneCapability(tile=tile_capability("int32-bar-aggregation")),  # no unit contribution
        ),
    )
    design = contract.to_design()
    assert design.name == "mixed"
    assert design.capacity(UnitKind.AGGREGATION) == 2
    assert design.capacity(UnitKind.SORT) == 1
    assert design.aggregation_capacity(OperationCode.SUM, LogicalType.INT32) == 2
    assert design.aggregation_capacity(OperationCode.AVERAGE, LogicalType.INT32) == 0
    assert design.aggregation_capacity(OperationCode.SUM, LogicalType.FLOAT32) == 0


def test_bar_noc_contract_design_offers_no_aggregation_capacity() -> None:
    design = capability_contract(bar_noc_spec(lanes=4)).to_design()
    assert design.capacity(UnitKind.AGGREGATION) == 0
    assert design.units == ()


def test_composition_and_hand_built_contracts_share_one_tile_derivation() -> None:
    spec = ScanCompositionSpec(name="bar-scan", lanes=(LaneSpec(tile=TileSpec(core="int32-bar-aggregation", config={"cfg_mode": "2'd0"})),))
    contract = capability_contract(spec)
    assert contract.lanes[0].tile == tile_capability(TileSpec(core="int32-bar-aggregation", config={"cfg_mode": "2'd0"}))
