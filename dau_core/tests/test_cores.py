from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from dau_core import hdl
from dau_core.cores import (
    ContractKind,
    CoreDefinition,
    CoreKind,
    ParameterSpec,
    ResourceEnvelope,
    ResourceMeasurement,
    TimingStatus,
    TimingViolationError,
    UnknownCoreError,
    UnmeasuredResourceError,
    core,
    core_for_module,
    cores_of_kind,
    loaded_cores,
    sources_for,
)


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


def test_every_selectable_operator_declares_its_ingest_rate() -> None:
    """Ingest rate is core PUBLIC INTERFACE (the scheduler rate-matches
    opaque cores from the registry declaration alone): every stream operator
    declares it. The floor is the composed 1-row/2-cycle
    lane rate; only silicon-proven faster cores declare 1/1."""
    from fractions import Fraction

    full_rate = set()
    for definition in cores_of_kind(CoreKind.OPERATOR):
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
        "record-gearbox",
    }
    # a rate that scales with a module parameter must name a declared one
    for definition in cores_of_kind(CoreKind.OPERATOR):
        if definition.ingest is not None and definition.ingest.scales_with_param is not None:
            assert definition.ingest.scales_with_param in definition.parameters, definition.name


def test_wide_combinational_tiles_declare_pipeline_depth_without_changing_ingest() -> None:
    for name in ("int32-wide-row-predicate-filter", "int32-wide-row-map-alu"):
        definition = core(name)
        depth = definition.parameters["PIPELINE_DEPTH"]
        assert (depth.default, depth.minimum, depth.maximum) == (3, 1, 8)
        assert definition.ingest is not None
        assert (definition.ingest.rows, definition.ingest.per_cycles) == (1, 1)
        assert definition.ingest.scales_with_param == "SLOTS"


def test_every_selectable_operator_declares_its_contract_kind() -> None:
    """A new core declares its stream contract in the registry — contract
    tests select validators from this tag, never from test-local exemption
    sets."""
    for definition in cores_of_kind(CoreKind.OPERATOR):
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


def test_every_selectable_operator_declares_its_record_words() -> None:
    for definition in cores_of_kind(CoreKind.OPERATOR):
        assert (definition.record_words is None) != (definition.record_words_param is None), definition.name
        assert definition.effective_record_words({}) >= 1


def test_parameterized_record_words_resolve_from_defaults_and_overrides() -> None:
    gather = core("int32-record-gather")
    assert gather.effective_record_words({}) == 4
    assert gather.effective_output_record_words({}) == 1
    assert gather.effective_record_words({"IN_WORDS": 7, "OUT_WORDS": 3}) == 7
    assert gather.effective_output_record_words({"IN_WORDS": 7, "OUT_WORDS": 3}) == 3

    dispatcher = core("int32-key-mask-dispatcher")
    assert dispatcher.effective_record_words({}) == 2
    assert dispatcher.effective_output_record_words({"RECORD_WORDS": 3}) == 3


def test_only_combinational_per_beat_cores_support_pipeline_depth() -> None:
    supported = {definition.name for definition in cores_of_kind(CoreKind.OPERATOR) if definition.supports_pipeline_depth}
    assert supported == {"int32-wide-row-map-alu", "int32-wide-row-predicate-filter"}


def test_sources_for_closes_dependencies_in_order() -> None:
    sources = sources_for("int32-stream-aggregation")
    names = [path.name for path in sources]
    assert names == ["dau_aggregation_pkg.sv", "dau_int32_stream_aggregation.sv"]

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
            if isinstance(definition.resources, ResourceEnvelope):
                assert definition.resources.measured, definition.name
                assert definition.resources.lut > 0, definition.name
            else:
                assert definition.resources, definition.name
                assert all(point.lut > 0 for point in definition.resources), definition.name


# Synthesizable tiles that still carry a SCALAR envelope rather than
# (part, clock, params) measurement points. A scalar envelope cannot answer
# the question that matters -- is this measured for MY board at MY clock --
# so every name here is a tile that can still be priced against the wrong
# hardware. The list is expected to shrink to empty.
#
# It is asserted EXACTLY, in both directions, on purpose. Migrate a tile
# without removing it here and the test fails; add a new tile on a scalar
# envelope and the test fails. The count stops being something a person
# reports after reading the yaml and starts being something the suite owns.
UNMIGRATED_SCALAR_ENVELOPES = frozenset(
    {
        "axi-burst-reader",
        "axi-record-writer",
        "axi-write-mux",
        "int32-broadcast-hash-join",
        "int32-key-mask-dispatcher",
        "int32-key-membership-filter",
        "int32-record-gather",
        "int32-rolling-moments",
        "int32-row-unpack",
        "int32-time-bucket-key",
        "int32-wide-row-predicate-filter",
        "record-gearbox",
        "stream-broadcast",
    }
)


def test_the_scalar_envelope_holdouts_are_exactly_the_declared_ones() -> None:
    """The standard is measurement points; this pins how far from it the
    palette still is, so progress is reported by the suite rather than by
    whoever last read the file.

    Both directions matter. A stale name here means a migration landed and
    nobody tightened the guard -- which is how "the migration is complete"
    got said while thirteen tiles were still scalar. An unlisted name means
    a new tile shipped without measurement points.
    """
    scalar = {
        definition.name
        for definition in loaded_cores().values()
        if definition.kind in (CoreKind.OPERATOR, CoreKind.STREAM_INFRA)
        and definition.resources is not None
        and not isinstance(definition.resources, (list, tuple))
    }
    migrated_but_still_listed = UNMIGRATED_SCALAR_ENVELOPES - scalar
    assert not migrated_but_still_listed, f"migrated -- remove from UNMIGRATED_SCALAR_ENVELOPES: {sorted(migrated_but_still_listed)}"
    newly_scalar = scalar - UNMIGRATED_SCALAR_ENVELOPES
    assert not newly_scalar, (
        f"these tiles carry a scalar envelope, which cannot say which board it was measured on: {sorted(newly_scalar)}. "
        "Measure them at (part, clock, params) via the synthesize-cores task"
    )


def _measured(**overrides):
    """A core carrying one measurement point, built here rather than read from
    the registry: which cores are migrated is a migration detail, and a test
    of the lookup should not move when that changes."""
    point = {
        "part": "xc7k325tffg900-2",
        "clock_ns": 8.0,
        "params": {"SLOTS": 4, "PIPELINE_DEPTH": 3},
        "lut": 2763,
        "ff": 2351,
        "bram36": 0.0,
        "dsp": 16,
        "timing": "met",
        "wns_ns": 1.983,
    }
    point.update(overrides)
    return CoreDefinition.model_validate({**core("int32-wide-row-map-alu").model_dump(), "resources": [point]})


def test_exact_resource_measurement_resolves_with_parameter_defaults() -> None:
    measurement = _measured().resource_measurement(part="xc7k325tffg900-2", clock_ns=8.0, params={})
    assert (measurement.lut, measurement.ff, measurement.dsp) == (2763, 2351, 16)
    assert measurement.params == {"SLOTS": 4, "PIPELINE_DEPTH": 3}
    assert measurement.timing is TimingStatus.MET
    assert measurement.wns_ns == 1.983


def test_resource_measurement_refuses_an_unmeasured_part() -> None:
    with pytest.raises(UnmeasuredResourceError, match="unmeasured part 'xcvu9p-flga2104-2L'"):
        _measured().resource_measurement(part="xcvu9p-flga2104-2L", clock_ns=8.0, params={})


def test_a_legacy_envelope_is_refused_rather_than_read_as_a_measurement() -> None:
    # a scalar envelope carries no part or clock, so it cannot answer "is this
    # measured for MY board" -- the question that matters. Refuse, do not guess.
    with pytest.raises(UnmeasuredResourceError, match="no measurement coordinates"):
        # a core still carrying a scalar envelope; migrating this one later is
        # expected to move this assertion, not delete it
        core("int32-key-membership-filter").resource_measurement(part="xc7k325tffg900-2", clock_ns=8.0, params={})


def test_violated_measurement_is_retrievable_but_not_a_usable_envelope() -> None:
    violated = ResourceMeasurement(
        part="xc7k325tffg900-2",
        clock_ns=8.0,
        params={"SLOTS": 4, "PIPELINE_DEPTH": 3},
        lut=3226,
        ff=1563,
        bram36=0.0,
        dsp=16,
        timing=TimingStatus.VIOLATED,
        wns_ns=-1.383,
    )
    definition = CoreDefinition.model_validate(
        {
            **core("int32-wide-row-map-alu").model_dump(),
            "resources": [violated],
        }
    )
    resolved = definition.resource_measurement(part="xc7k325tffg900-2", clock_ns=8.0, params={})
    assert resolved.timing is TimingStatus.VIOLATED
    assert resolved.wns_ns == -1.383
    with pytest.raises(TimingViolationError, match=r"violated timing \(WNS -1.383 ns\)"):
        resolved.usable_envelope()


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
    silent = {d.name for d in cores_of_kind(CoreKind.OPERATOR) if not d.success_close_out}
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
        "record-gearbox",
    }
    # the grouped multi-sum tile emits many group records but exactly ONE
    # success close-out per batch (after the final group) — terminal-legal
    assert core("int32-grouped-field-sum-aggregation").success_close_out


def test_dispatcher_rate_still_needs_record_aware_modeling() -> None:
    """The dispatcher's framing-shaped rate describes a 2-word record. Its
    64-bit front ingests one WORD per cycle, so an N-word record costs N
    cycles, while a wide framing delivers a whole record per beat-slot —
    two rules the current rate shapes cannot express together. Record shape
    is now declared for detection, but this pins the remaining rate-model gap
    so RECORD_WORDS != 2 is not silently treated as resolved."""
    definition = core("int32-key-mask-dispatcher")
    assert definition.parameters["RECORD_WORDS"].default == 2
    assert definition.effective_record_words({"RECORD_WORDS": 3}) == 3
    assert definition.ingest is not None and definition.ingest.frames_on_param == "IN_WIDTH"
    # the rate declaration still ignores RECORD_WORDS — that is the gap
    narrow = definition.ingest.effective({"IN_WIDTH": 64}, definition.parameters)
    assert narrow == definition.ingest.effective({"IN_WIDTH": 64, "RECORD_WORDS": 3}, definition.parameters)


def test_authoritative_models_reject_impossible_values() -> None:
    # these models back the registry yaml and pricing; a negative LUT count or
    # a zero-length part string is a data-entry bug that must fail at load
    with pytest.raises(ValidationError):
        ResourceEnvelope(lut=-1, ff=0, bram36=0.0, dsp=0)
    with pytest.raises(ValidationError):
        ResourceMeasurement(part="", clock_ns=8.0, lut=1, ff=1, bram36=0.0, dsp=0, timing="met", wns_ns=0.1)
    with pytest.raises(ValidationError):
        ResourceMeasurement(part="xc7k325tffg900-2", clock_ns=0, lut=1, ff=1, bram36=0.0, dsp=0, timing="met", wns_ns=0.1)


def test_parameter_spec_rejects_incoherent_constraints() -> None:
    # the spec mirrors RTL elaboration guards; an incoherent spec would
    # reject every override, so it fails at registration instead
    with pytest.raises(ValidationError, match="minimum 8 exceeds maximum 4"):
        ParameterSpec(default=8, minimum=8, maximum=4)
    with pytest.raises(ValidationError, match="non-empty"):
        ParameterSpec(default=1, choices=())
    with pytest.raises(ValidationError, match="default 3 violates"):
        ParameterSpec(default=3, power_of_two=True)
    with pytest.raises(ValidationError, match="default 5 violates"):
        ParameterSpec(default=5, multiple_of=2)


def test_parameter_spec_violation_is_the_single_constraint_source() -> None:
    # composition wraps this with tile context; the model owns the rule
    spec = ParameterSpec(default=64, multiple_of=64, maximum=4096)
    assert spec.violation(128) is None
    assert "multiple of 64" in spec.violation(65)
    assert ">= 2" in ParameterSpec(default=2, minimum=2).violation(1)
    choice = ParameterSpec(default=64, choices=(64, 128))
    assert choice.violation(64) is None
    assert "one of 64, 128" in choice.violation(96)


def test_storage_intent_prices_against_declared_parameters() -> None:
    definition = CoreDefinition.model_validate(
        {
            **core("int32-row-predicate-filter").model_dump(),
            "parameters": {"CAPACITY": {"default": 1024, "minimum": 1, "maximum": 16384}},
            # the borrowed tile's points are keyed by ITS parameters (none);
            # declaring one here would leave them describing a shape that no
            # longer exists, which the model rightly refuses
            "resources": [],
            "storage_intent": [
                {"role": "arena", "access": "owned", "bytes_per_unit": 4, "scale_parameter": "CAPACITY", "tier_hint": "bram"},
            ],
        }
    )
    [(intent, cost)] = definition.storage_bytes({})
    assert (intent.role, cost) == ("arena", 4 * 1024)  # default CAPACITY
    [(_, cost)] = definition.storage_bytes({"CAPACITY": 4096})
    assert cost == 4 * 4096  # override prices


def test_storage_intent_refuses_an_undeclared_scale_parameter() -> None:
    with pytest.raises(ValidationError, match="scale_parameter 'MISSING'"):
        CoreDefinition.model_validate(
            {
                **core("int32-row-predicate-filter").model_dump(),
                "storage_intent": [
                    {"role": "arena", "access": "owned", "bytes_per_unit": 4, "scale_parameter": "MISSING"},
                ],
            }
        )


def test_storage_intent_without_scale_is_a_fixed_cost() -> None:
    definition = CoreDefinition.model_validate(
        {
            **core("int32-row-predicate-filter").model_dump(),
            "storage_intent": [{"role": "table", "access": "shared", "bytes_per_unit": 8192}],
        }
    )
    [(_, cost)] = definition.storage_bytes({})
    assert cost == 8192


def test_storage_bytes_refuses_illegal_scale_overrides() -> None:
    definition = core("int32-scratchpad")
    with pytest.raises(ValueError, match="not an int"):
        definition.storage_bytes({"CAPACITY": "4096"})
    with pytest.raises(ValueError, match="not an int"):
        definition.storage_bytes({"CAPACITY": True})
    with pytest.raises(ValueError, match="CAPACITY"):
        definition.storage_bytes({"CAPACITY": -2})
    with pytest.raises(ValueError, match="<= 16384"):
        definition.storage_bytes({"CAPACITY": 32768})
