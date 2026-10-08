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
from dau_core.cores import ContractKind, CoreKind
from dau_core.design import UnitKind
from dau_core.registers import CURRENT_REGISTER_MAP_VERSION
from dau_core.registry import cores_of_kind
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
        "stream-aggregation",
        "arrow-lite-stream-aggregation",
        "record-batch-aggregation",
        "field-sum-aggregation",
        "grouped-field-sum-aggregation",
        "hash-grouped-aggregation",
        "wide-field-sum-aggregation",
    }
    for definition in declared.values():
        if definition.name in (
            "field-sum-aggregation",
            "grouped-field-sum-aggregation",
            "hash-grouped-aggregation",
            "wide-field-sum-aggregation",
        ):
            # the field-sum tiles have a sum+count datapath only: they must
            # never over-declare MIN/MAX they cannot execute
            assert definition.opcodes == (OperationCode.SUM, OperationCode.COUNT), definition.name
        else:
            assert definition.opcodes == _HW_AGGREGATION_OPCODES, definition.name
        assert OperationCode.AVERAGE not in definition.opcodes, definition.name
        assert definition.opcode_input_types == (LogicalType.INT32,), definition.name
        assert definition.unit_kind == UnitKind.AGGREGATION, definition.name


def test_registry_declares_sorter_capacity() -> None:
    sorter = tile_capability("bitonic-sorter")
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
    assert lane.tile.core == "bar-aggregation"
    assert lane.tile.count_port == "bar_count"
    assert lane.partition is not None and lane.partition.core == "pair-key-filter"
    assert dict(lane.partition.config)["cfg_key_match"] == "32'd0"


def test_sorted_scan_contract_carries_sorter_capacity_and_splitters() -> None:
    contract = capability_contract(sorted_scan_spec(lanes=4))
    assert contract.lane_count == 4
    assert contract.sort_capacity == 16
    assert contract.operator_bitmap == 0
    assert contract.partitioner is not None
    assert contract.partitioner.core == "range-partitioner"
    assert "cfg_splitters" in dict(contract.partitioner.config)


def test_scalar_aggregation_tile_contract_advertises_hardware_and_host_truth() -> None:
    contract = CapabilityContract(
        name="mm-stream-job",
        lanes=(LaneCapability(tile=tile_capability("record-batch-aggregation")),),
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
    # the contract states the map version it was composed against, so this
    # tracks CURRENT rather than a literal -- pinning "0.2" here made a map
    # revision look like a contract regression
    assert mapping["register_map_version"] == f"{CURRENT_REGISTER_MAP_VERSION.major}.{CURRENT_REGISTER_MAP_VERSION.minor}"
    assert mapping["operator_bitmap"] == "0x00000000"
    assert mapping["lane_count"] == 2
    assert mapping["sort_capacity"] == 16
    restored = CapabilityContract.from_mapping(json.loads(json.dumps(mapping)))
    assert restored == contract
    # a composed design binds its configs as literals: not reconfigurable,
    # and the manifest says so rather than leaving a reader to assume
    assert mapping["reconfigurable"] is False

    with pytest.raises(ValueError, match="not a dau capability contract"):
        CapabilityContract.from_mapping({"contract": "other"})
    with pytest.raises(ValueError, match="unsupported capability contract schema"):
        CapabilityContract.from_mapping({"contract": "dau-capability-contract", "schema": 999})


def test_contract_to_design_counts_units_by_identical_tile() -> None:
    aggregation = tile_capability("record-batch-aggregation")
    sorter = tile_capability("bitonic-sorter")
    contract = CapabilityContract(
        name="mixed",
        lanes=(
            LaneCapability(tile=aggregation),
            LaneCapability(tile=aggregation),
            LaneCapability(tile=sorter),
            LaneCapability(tile=tile_capability("bar-aggregation")),  # no unit contribution
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
    spec = ScanCompositionSpec(name="bar-scan", lanes=(LaneSpec(tile=TileSpec(core="bar-aggregation", config={"cfg_mode": "2'd0"})),))
    contract = capability_contract(spec)
    assert contract.lanes[0].tile == tile_capability(TileSpec(core="bar-aggregation", config={"cfg_mode": "2'd0"}))


def test_chain_lanes_carry_chain_capabilities_and_opcode_truth() -> None:
    from dau_core.tests.test_composition import q6_scan_spec

    contract = capability_contract(q6_scan_spec(lanes=4))
    lane = contract.lanes[0]
    assert [stage.core for stage in lane.chain] == [
        "row-predicate-filter",
        "row-predicate-filter",
        "row-map-alu",
    ]
    assert lane.tile.core == "field-sum-aggregation"
    assert lane.tile.count_port == "aggregated_count"
    # the field-sum tile's real HDL opcodes reach the bitmap; AVERAGE derives
    # host-side from SUM+COUNT and never appears as hardware
    assert contract.hardware_opcodes == (OperationCode.SUM, OperationCode.COUNT)
    assert contract.operator_bitmap == opcode_bitmap((OperationCode.SUM, OperationCode.COUNT))
    assert contract.host_opcodes == (OperationCode.AVERAGE,)
    assert OperationCode.AVERAGE not in contract.hardware_opcodes

    mapping = contract.to_mapping()
    assert len(mapping["lanes"][0]["chain"]) == 3
    restored = CapabilityContract.from_mapping(json.loads(json.dumps(mapping)))
    assert restored == contract


def test_chainless_lane_mapping_round_trips_without_chain_key() -> None:
    # older manifests carry no "chain" key: absence decodes as an empty chain
    contract = capability_contract(bar_noc_spec(lanes=1))
    mapping = contract.to_mapping()
    del mapping["lanes"][0]["chain"]
    restored = CapabilityContract.from_mapping(json.loads(json.dumps(mapping)))
    assert restored == contract


def test_the_contract_models_are_ccflow_models_not_dataclasses() -> None:
    """The contract is config data -- what a manifest carries and a planner
    matches against -- so it is declared the way every other registry
    declaration is. Pinned so it cannot drift back to a dataclass."""
    import dataclasses

    from ccflow import BaseModel

    from dau_core.capability_contract import TileCapability

    for model in (TileCapability, LaneCapability, CapabilityContract):
        assert issubclass(model, BaseModel), model
        assert not dataclasses.is_dataclass(model), model
        assert model.model_config.get("frozen") is True, model


def test_a_contract_copies_with_a_field_replaced_and_stays_hashable() -> None:
    lane = LaneCapability(tile=tile_capability("record-batch-aggregation"))
    contract = CapabilityContract(name="twin", lanes=(lane,), partitioner=tile_capability("key-mask-dispatcher"))
    twin = contract.model_copy(update={"partitioner": None})
    assert twin.partitioner is None and twin.lanes == contract.lanes
    assert len({lane.tile, tile_capability("record-batch-aggregation")}) == 1, "frozen models hash by value"


def test_reconfigurable_is_a_manifest_fact() -> None:
    """The fact rides the contract (and so the manifest), never a caller
    flag: a design whose chain configs are runtime registers advertises it,
    and a manifest written before the key existed reads as baked."""
    baked = capability_contract(sorted_scan_spec(lanes=2))
    assert baked.reconfigurable is False
    registers = baked.model_copy(update={"reconfigurable": True})
    mapping = registers.to_mapping()
    assert mapping["reconfigurable"] is True
    assert CapabilityContract.from_mapping(json.loads(json.dumps(mapping))) == registers
    legacy = baked.to_mapping()
    del legacy["reconfigurable"]
    assert CapabilityContract.from_mapping(legacy).reconfigurable is False
