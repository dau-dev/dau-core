from __future__ import annotations

from pathlib import Path

import pytest

from dau_core import hdl
from dau_core.cores import CORES, ContractKind, CoreKind, UnknownCoreError, core, core_for_module, cores_of_kind, sources_for


def test_every_hdl_file_belongs_to_exactly_one_core() -> None:
    hdl_dir = Path(str(hdl.__path__[0]))
    hdl_files = {path.name for path in hdl_dir.iterdir() if path.suffix in (".sv", ".v")}
    registry_files = [definition.source for definition in CORES]
    assert sorted(registry_files) == sorted(set(registry_files)), "duplicate source in registry"
    assert set(registry_files) == hdl_files, f"registry/hdl drift: missing {hdl_files - set(registry_files)}, stale {set(registry_files) - hdl_files}"


def test_all_source_paths_exist() -> None:
    for definition in CORES:
        assert definition.source_path().is_file(), definition.name


def test_goldens_resolve_to_callables() -> None:
    for definition in CORES:
        if definition.golden is not None:
            assert callable(definition.golden_callable()), definition.name


def test_benches_are_importable_modules() -> None:
    from importlib import import_module

    for definition in CORES:
        if definition.bench is not None:
            import_module(definition.bench)


def test_every_operator_has_a_golden() -> None:
    for definition in cores_of_kind(CoreKind.OPERATOR):
        assert definition.golden is not None, f"operator {definition.name} has no golden"


def test_legacy_operators_stay_verified_but_are_not_selectable() -> None:
    from dau_core.cores import selectable_operators

    legacy = [d for d in cores_of_kind(CoreKind.OPERATOR) if d.legacy]
    assert {d.name for d in legacy} == {"int32-aggregation-tile"}, "legacy operator set changed — confirm intent"
    for definition in legacy:
        # legacy tiles stay in the registry and stay tested against their golden
        assert definition.golden is not None and definition.bench is not None, definition.name
        # ...but a planner/new composition is never offered them
        assert definition not in selectable_operators(), definition.name
        # and the composition validator refuses them regardless (no status counter)
        assert definition.count_port is None, definition.name


def test_every_selectable_operator_declares_its_contract_kind() -> None:
    """A new core declares its stream contract in the registry — contract
    tests select validators from this tag, never from test-local exemption
    sets."""
    for definition in cores_of_kind(CoreKind.OPERATOR):
        if definition.legacy:
            # predates the stream+status contract; declares none
            assert definition.contract_kind is None, definition.name
        else:
            assert definition.contract_kind is not None, f"operator {definition.name} declares no contract_kind"
    # the one descriptor-driven tile is registry data, not a test special case;
    # pinned as a set: no Arrow-lite validator exists yet, so a newly
    # descriptor-tagged operator would skip conformance — confirm intent here
    descriptor = {d.name for d in cores_of_kind(CoreKind.OPERATOR) if d.contract_kind is ContractKind.ARROW_LITE_DESCRIPTOR}
    assert descriptor == {"int32-stream-aggregation"}, "descriptor-tagged operator set changed — confirm intent"
    # only operators declare a stream contract
    for definition in CORES:
        if definition.kind is not CoreKind.OPERATOR:
            assert definition.contract_kind is None, definition.name


def test_sources_for_closes_dependencies_in_order() -> None:
    sources = sources_for("int32-stream-aggregation")
    names = [path.name for path in sources]
    assert names.index("dau_aggregation_pkg.sv") < names.index("dau_int32_stream_aggregation.sv")
    assert "dau_int32_aggregation_tile.sv" in names  # transitive dep

    # composing the NoC lane set resolves every block once
    lane = sources_for("axi-burst-reader", "stream-broadcast", "int32-pair-key-filter", "int32-bar-aggregation", "axi-record-writer", "axi-write-mux")
    assert len(lane) == len(set(lane)) == 6


def test_lookup_errors_are_typed() -> None:
    with pytest.raises(UnknownCoreError):
        core("no-such-core")
    with pytest.raises(UnknownCoreError):
        core_for_module("no_such_module")
    assert core_for_module("dau_int32_bar_aggregation").name == "int32-bar-aggregation"


def test_synthesizable_cores_carry_measured_envelopes() -> None:
    """Every operator/stream-infra core carries a measured OOC envelope
    (xc7a200tfbg484-2, Vivado 2025.2 out-of-context synthesis)."""
    for definition in CORES:
        if definition.kind in (CoreKind.OPERATOR, CoreKind.STREAM_INFRA):
            assert definition.resources is not None, definition.name
            assert definition.resources.measured, definition.name
            assert definition.resources.lut > 0, definition.name
