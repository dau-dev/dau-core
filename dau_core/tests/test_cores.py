from __future__ import annotations

from pathlib import Path

import pytest

from dau_core import hdl
from dau_core.cores import ContractKind, CoreKind, UnknownCoreError, core, core_for_module, cores_of_kind, loaded_cores, sources_for


def test_every_hdl_file_belongs_to_exactly_one_core() -> None:
    hdl_dir = Path(str(hdl.__path__[0]))
    hdl_files = {path.name for path in hdl_dir.iterdir() if path.suffix in (".sv", ".v")}
    registry_files = [definition.source for definition in loaded_cores().values()]
    assert sorted(registry_files) == sorted(set(registry_files)), "duplicate source in registry"
    assert set(registry_files) == hdl_files, f"registry/hdl drift: missing {hdl_files - set(registry_files)}, stale {set(registry_files) - hdl_files}"


def test_all_source_paths_exist() -> None:
    for definition in loaded_cores().values():
        assert definition.source_path().is_file(), definition.name


def test_goldens_resolve_to_callables() -> None:
    for definition in loaded_cores().values():
        if definition.golden is not None:
            assert callable(definition.golden_callable()), definition.name


def test_benches_are_importable_modules() -> None:
    from importlib import import_module

    for definition in loaded_cores().values():
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


def test_every_selectable_operator_declares_its_ingest_rate() -> None:
    """Ingest rate is core PUBLIC INTERFACE (the scheduler rate-matches
    opaque cores from the registry declaration alone): every non-legacy
    stream operator declares it. The floor is the composed 1-row/2-cycle
    lane rate; only silicon-proven faster cores declare 1/1."""
    from fractions import Fraction

    full_rate = set()
    for definition in cores_of_kind(CoreKind.OPERATOR):
        if definition.legacy:
            assert definition.ingest is None, definition.name
            continue
        assert definition.ingest is not None, f"operator {definition.name} declares no ingest rate"
        assert definition.ingest.rows_per_cycle <= Fraction(1, 1), f"{definition.name}: >1 row/cycle needs multi-row silicon proof"
        if definition.ingest.rows_per_cycle == Fraction(1, 1):
            full_rate.add(definition.name)
    # the proven-faster set is pinned: a new full-rate claim is a deliberate,
    # measured upgrade, not a copy-paste default. The wide-lane cores are
    # 1/1 PER SLOT by construction (scales_with_param carries the multi-row
    # claim; their cocotb benches prove one beat per cycle).
    assert full_rate == {
        "int32-key-mask-dispatcher",
        "int32-stride-distributor",
        "int32-row-unpack",
        "int32-wide-row-predicate-filter",
        "int32-wide-row-map-alu",
        "int32-wide-field-sum-aggregation",
    }
    # a rate that scales with a module parameter must name a declared one
    for definition in cores_of_kind(CoreKind.OPERATOR):
        if definition.ingest is not None and definition.ingest.scales_with_param is not None:
            assert definition.ingest.scales_with_param in definition.parameters, definition.name


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
    for definition in loaded_cores().values():
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
    """Recorded operator/stream-infra envelopes are measured OOC values."""
    resource_free = {
        definition.name
        for definition in loaded_cores().values()
        if definition.kind in (CoreKind.OPERATOR, CoreKind.STREAM_INFRA) and definition.resources is None
    }
    assert resource_free == {"int32-stride-distributor"}
    for definition in loaded_cores().values():
        if definition.kind in (CoreKind.OPERATOR, CoreKind.STREAM_INFRA) and definition.resources is not None:
            assert definition.resources.measured, definition.name
            assert definition.resources.lut > 0, definition.name


def test_registry_is_hydra_composition_not_a_python_literal() -> None:
    """Composition IS the registry: cores load from dau-core's hydra config
    tree into the ccflow ModelRegistry at /dau-core/<name>. A parallel
    Python-literal core list is the anti-pattern this guards against."""
    import dau_core.cores as cores_module
    from dau_core.config import load_config

    assert not hasattr(cores_module, "CORES"), "parallel Python-literal registry reappeared"
    registry = load_config(overwrite=True)
    names = set(registry.models["dau-core"].models)
    assert names == set(loaded_cores()), "registry path view and accessor view drifted"
    assert "int32-bar-aggregation" in names


def test_accessors_defer_to_a_composed_registry() -> None:
    """The composed registry is the source of truth: accessors read through
    to /dau-core (no snapshot cache), so a consumer's hydra overrides are
    visible and never clobbered by a default reload."""
    from dau_core.config import load_config

    try:
        load_config(["dau-core.int32-bar-aggregation.description=OVERRIDDEN"], overwrite=True)
        assert core("int32-bar-aggregation").description == "OVERRIDDEN"
    finally:
        load_config(overwrite=True)  # restore packaged defaults
    assert core("int32-bar-aggregation").description != "OVERRIDDEN"


def test_silent_on_success_operator_set_is_pinned() -> None:
    """The status contract's two classes are registry truth: silent-on-success
    cores (chain-stage/partition legal) versus per-batch success close-out
    cores (terminal-tile legal). Changing a core's class changes lane status
    semantics — confirm intent here."""
    silent = {d.name for d in cores_of_kind(CoreKind.OPERATOR) if not d.legacy and not d.success_close_out}
    assert silent == {
        "int32-pair-key-filter",
        "int32-range-partitioner",
        "int32-key-mask-dispatcher",
        "int32-stride-distributor",
        "int32-row-predicate-filter",
        "int32-row-map-alu",
        "int32-key-membership-filter",
        "int32-broadcast-hash-join",
        "int32-row-unpack",
        "int32-record-gather",
        "int32-time-bucket-key",
        "int32-wide-row-predicate-filter",
        "int32-wide-row-map-alu",
    }
    # the grouped multi-sum tile emits many group records but exactly ONE
    # success close-out per batch (after the final group) — terminal-legal
    assert core("int32-grouped-field-sum-aggregation").success_close_out


def test_the_dispatcher_rate_is_only_claimed_for_quad_row_records() -> None:
    """The dispatcher's framing-shaped rate describes a 2-word record. Its
    64-bit front ingests one WORD per cycle, so an N-word record costs N
    cycles, while a wide framing delivers a whole record per beat-slot —
    two rules the current rate shapes cannot express together. This pins
    the gap so a composition at RECORD_WORDS != 2 is not silently
    over-predicted; closing it is the tile-ABI work."""
    definition = core("int32-key-mask-dispatcher")
    assert definition.parameters["RECORD_WORDS"].default == 2
    assert definition.ingest is not None and definition.ingest.frames_on_param == "IN_WIDTH"
    # the declaration ignores RECORD_WORDS entirely — that is the gap
    narrow = definition.ingest.effective({"IN_WIDTH": 64}, definition.parameters)
    assert narrow == definition.ingest.effective({"IN_WIDTH": 64, "RECORD_WORDS": 3}, definition.parameters)
