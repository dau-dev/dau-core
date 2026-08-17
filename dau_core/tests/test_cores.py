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
    UnmeasuredResourceError,
    render_root,
)
from dau_core.registry import (
    UnknownCoreError,
    core,
    core_for_module,
    cores_of_kind,
    loaded_cores,
    sources_for,
)

# dau-core's own cores, which is what `source_package`'s default names. The
# registry is open: a third party ships tiles by composing their own hydra
# config into `/dau-core`, and `loaded_cores()` then returns theirs beside
# ours. Every guard below that asserts an EXACT set -- the HDL directory
# listing, the descriptor-tagged operators, the envelope-free tiles, the
# scalar-envelope holdouts -- is a statement about OUR palette, and would
# otherwise report an installed vendor tile as drift in this suite. Scoping
# them here is what makes the seam usable: a vendor can install tiles without
# failing our tests, and we still hold our own palette to the exact standard.
OWN_PACKAGE = "dau_core.hdl"
HDL_SUFFIXES = (".sv", ".v")


def own_cores() -> tuple:
    return tuple(d for d in loaded_cores().values() if d.source_package == OWN_PACKAGE)


def committed_hdl(hdl_dir: Path) -> set[str] | None:
    """The HDL git CARRIES under ``hdl_dir``, as paths relative to it, or
    None when there is no repository to ask.

    Committedness is a git fact, and only git can answer it. A directory
    listing cannot: the suite renders every generated core into the render
    root on each run (``conftest._render_generated_cores``), so a rendered
    file being PRESENT there is the normal state and says nothing about
    whether someone checked it in. Absent a repository -- an installed
    distribution -- nothing is committed and there is nothing to check.
    """
    import subprocess

    try:
        listed = subprocess.run(
            ["git", "-C", str(hdl_dir), "ls-files", "--", "."],
            capture_output=True,
            check=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return {line for line in listed.splitlines() if line.endswith(HDL_SUFFIXES)}


def test_every_hdl_file_belongs_to_exactly_one_core() -> None:
    """Checked-in HDL and registry entries match exactly, in both directions.

    A GENERATED core is excluded because it has no checked-in file by
    definition -- and asserted to have none, which is the same guard from the
    other side: a generated core with a file beside the entry means someone
    committed rendered output, and whichever of the two is stale will be the
    one that gets synthesized.

    The reach is the whole package, not one directory listing. Listing
    ``hdl/`` alone left the render root -- a SUBDIRECTORY of it -- outside
    this guard entirely, and three rendered tiles sat committed there until a
    rename made their filenames disagree with their contents. So the walk
    descends, and the committed set comes from git: a rendered file being
    present in the render root is the normal working state (the suite renders
    there on every run), and what distinguishes that from the mistake is
    whether it was checked in.
    """
    hdl_dir = Path(str(hdl.__path__[0]))
    rendered_root = render_root(OWN_PACKAGE)
    hdl_files = {path.name for path in hdl_dir.iterdir() if path.suffix in HDL_SUFFIXES}

    generated = {d.source for d in own_cores() if d.generator is not None}
    committed_generated = generated & hdl_files
    assert not committed_generated, (
        f"rendered output is committed beside the registry: {sorted(committed_generated)}. "
        "A generated core renders from its operating point; a file here would shadow it"
    )

    registry_files = [d.source for d in own_cores() if d.generator is None]
    assert sorted(registry_files) == sorted(set(registry_files)), "duplicate source in registry"
    assert set(registry_files) == hdl_files, f"registry/hdl drift: missing {hdl_files - set(registry_files)}, stale {set(registry_files) - hdl_files}"

    stray = sorted(
        path.relative_to(hdl_dir).as_posix()
        for path in hdl_dir.rglob("*")
        if path.suffix in HDL_SUFFIXES and path.parent not in (hdl_dir, rendered_root)
    )
    assert not stray, (
        f"HDL lives in a subdirectory no core reads from: {stray}. Every checked-in tile sits beside the "
        f"registry entry naming it, and the only subdirectory any core resolves through is the render root ({rendered_root.name}/)"
    )

    committed = committed_hdl(hdl_dir)
    if committed is not None:
        assert committed == set(registry_files), (
            f"committed HDL is not the registry's checked-in sources: extra {sorted(committed - set(registry_files))}, "
            f"missing {sorted(set(registry_files) - committed)}. Rendered output committed anywhere under "
            f"{hdl_dir.name}/ is a second copy of a tile only its operating point defines, and whichever copy is stale is the one that gets synthesized"
        )


def test_all_source_paths_exist() -> None:
    for definition in loaded_cores().values():
        if definition.generator is not None:
            continue  # refuses until rendered; covered by test_generated_cores
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
    # int32-row-unpack left this set when silicon proved its MULTI-row rate:
    # it declares 1/128 row per cycle scaled by OUT_WIDTH, which is 1/2 at the
    # 64-bit framing and 8 at 1024. The multi-row claim rides the scaling
    # parameter exactly as the wide-lane cores' does, so the declared figure is
    # below 1/1 rather than at it.
    assert full_rate == {
        # a straight-line arithmetic pipeline with no state and no record
        # framing: one word in, one beat out, no reason to be slower. The
        # cocotb-free evidence is the tile's own verilator run -- 4096 rows in
        # 4128 cycles with the output always ready.
        "datetime-format",
        "int32-key-mask-dispatcher",
        "stride-distributor",
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
    descriptor = {d.name for d in own_cores() if d.kind is CoreKind.OPERATOR and d.contract_kind is ContractKind.ARROW_LITE_DESCRIPTOR}
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
    """Recorded operator/stream-infra envelopes are measured OOC values.

    EMPTY as of 2026-08-16: the stride distributor was the last operator
    carrying no envelope at all, and no design containing it could be
    priced. Asserted exactly, so a tile shipped without a measurement fails
    here rather than surfacing as an unpriceable design later.
    """
    resource_free = {
        definition.name
        for definition in own_cores()
        if definition.kind in (CoreKind.OPERATOR, CoreKind.STREAM_INFRA) and definition.resources is None
    }
    assert resource_free == set(), (
        f"these tiles carry no measured envelope, so nothing can price a design containing them: {sorted(resource_free)}. "
        "Measure them at (part, clock, params) via the synthesize-cores task"
    )
    for definition in own_cores():
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
# EMPTY as of 2026-08-11: every operator and stream-infra tile now carries
# (part, clock, params) measurement points. The last 13 migrated in one
# batch. `identity-registers` still has a scalar envelope but is a
# SHELL-ADAPTER, outside this set -- it is a combinational parameter ROM
# with no clock and therefore no timing paths, so `synthesize-cores` finds
# no slack line and it needs the combinational path (clock_ports entry "").
UNMIGRATED_SCALAR_ENVELOPES: frozenset[str] = frozenset()


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
        for definition in own_cores()
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


# Tiles whose NAME still carries a width. The rule the how-to teaches is
# that a width is a PARAMETER, never part of the identity, and a name that
# claims a width its parameters contradict has to be either renamed or kept
# in step forever -- the second never happens.
#
# Asserted EXACTLY, in both directions, for the same reason as the set
# above: a tile renamed without shrinking this set fails here, and a NEW
# tile shipped with a width in its name fails here. That is what makes a
# staged migration safe -- at every point the suite states how far it has
# got, so "the migration is complete" cannot be said while thirty remain.
#
# 43 as of 2026-08-16; 36 once the seven fixed-point and transcendental
# tiles landed their rename, and 31 once the five dau-core-only tiles
# (scratchpad, keyed-state, session-gap, streaming-dedup and the stride
# distributor) landed theirs. It ends at 5, NOT at 0: for the five REDESIGN
# tiles below the width is a true statement about the contract, and
# replacing a true name with a false one is the exact failure this rule
# exists to prevent. Each of those five leaves when its redesign lands.
WIDTH_NAMED_TILES: frozenset[str] = frozenset(
    {
        # RETYPE -- compare/select/mask/index datapaths over the 128-bit
        # quad row. The width is already a container: the transport unit
        # stays 128 bits, so a narrower element packs MORE fields per row
        # rather than shrinking the beat. Removed by giving each an
        # element-type parameter and substituting [31:0] -> [FIELD_BITS-1:0],
        # exactly the edit `int32-row-predicate-filter` already proved.
        # `predicate_compare` is already width-generic, so the filter family
        # is the cheap half.
        "int32-row-predicate-filter",  # parameter already landed; only the name remains
        "int32-wide-row-predicate-filter",
        "int32-pair-key-filter",
        "int32-key-membership-filter",
        "int32-bitonic-sorter",
        "int32-streaming-top-k",
        "int32-range-partitioner",
        "int32-key-mask-dispatcher",
        "int32-time-bucket-key",  # PART_BITS = 32 - BUCKET_BITS is already parametric in the width
        "int32-row-unpack",  # FAMILY-COUPLED: its output IS the quad row, so it moves when the family's field width does
        "int32-row-map-alu",  # blocked on dau_map_alu_pkg, which is 32-bit throughout
        "int32-wide-row-map-alu",  # same block; generalize the package once and four tiles follow
        # RETYPE-DOWN -- the emitted record's width is a function of the
        # input width. int8/int16 ride free (the INT64 accumulator is ample
        # and only framing changes); int64 CANNOT, because the sums need more
        # than 64 bits and the record cannot express them -- that is a
        # versioned stream-format change, i.e. a different tile. Removed by
        # deciding the narrow-type record packing rule ONCE for the family.
        "int32-field-sum-aggregation",
        "int32-grouped-field-sum-aggregation",
        "int32-grouped-field-aggregation",  # 11-word record packs pairs two-per-64-bit-word, host-decode-visible
        "int32-grouped-aggregation",
        "int32-bar-aggregation",  # 6-word record, same packing question
        "int32-wide-field-sum-aggregation",
        "int32-stream-aggregation",
        "int32-arrow-lite-stream-aggregation",
        "int32-record-batch-aggregation",
        "int32-hash-grouped-aggregation",
        "int32-rolling-moments",  # carries an undeclared sum_sq magnitude precondition; widening makes it worse, so declare or fix it first
        "int32-broadcast-hash-join",  # slot is {valid, b0, b1, b2}, so BRAM cost moves with the type: every offered type is its own measured point
        # RENAME -- the name is already false and no RTL changes. Both are in
        # the built `hero-temporal` design, so they carry a cache and silicon
        # consequence the seven already-renamed tiles did not.
        "int32-market-feature",  # the record mixes int32 lanes with INT64 spread, signed volume and dollar volume
        "int32-asof-backward",  # the MERGE AXIS is already int64 (time[63:0], a 65-bit tolerance difference); only key and payload lanes are 32-bit
        # REDESIGN -- these five KEEP their names. The width is genuinely part
        # of the contract, so renaming would replace a true name with a false
        # one. Four consume TWO SAMPLES PER 64-BIT WORD, hardcoded in five
        # separate idioms; generalizing to N turns a 2-input select into a
        # popcount plus variable-rotate compaction network whose cost is
        # superlinear in N. They leave this set when that redesign lands, not
        # before.
        "int32-predicate-filter",
        "int32-map-alu",
        "int32-keyed-predicate-filter",
        "int32-keyed-map-alu",
        # and one config-ABI case: the selector config word encodes 32-bit
        # halves (bit 6 = low/high, bits [11:7] = shift), so other widths need
        # more selector bits -- a host-visible protocol change.
        "int32-record-gather",
    }
)


def test_the_width_named_tiles_are_exactly_the_declared_ones() -> None:
    """The naming rule is that a width is a parameter, not an identity; this
    pins how much of the palette still contradicts it.

    Scoped to `own_cores()` for the same reason every exact-set guard here
    is: an installed third-party tile with a width in its name is that
    vendor's business, not drift in our palette.
    """
    width_named = {definition.name for definition in own_cores() if definition.name.startswith("int32-")}
    renamed_but_still_listed = WIDTH_NAMED_TILES - width_named
    assert not renamed_but_still_listed, f"renamed -- remove from WIDTH_NAMED_TILES: {sorted(renamed_but_still_listed)}"
    newly_width_named = width_named - WIDTH_NAMED_TILES
    assert not newly_width_named, (
        f"these tiles put a width in their identity: {sorted(newly_width_named)}. "
        "Name the tile for what it computes and declare the width as a parameter"
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
        # identity-registers is the last scalar envelope in the registry (a
        # combinational parameter ROM). When it migrates, this assertion
        # moves to a synthetic definition rather than being deleted -- the
        # refusal is the behaviour under test, not the core.
        core("identity-registers").resource_measurement(part="xc7k325tffg900-2", clock_ns=8.0, params={})


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
        "datetime-format",
        "fixed-point-ln-light",
        "rolling-extremum",
        "int32-pair-key-filter",
        "int32-range-partitioner",
        "int32-key-mask-dispatcher",
        "stride-distributor",
        "int32-row-predicate-filter",
        "int32-row-map-alu",
        "fixed-point-sqrt",
        "fixed-point-divide",
        "fixed-point-exp",
        "normal-cdf",
        "fixed-point-exp-light",
        "fixed-point-ln",
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


def test_an_element_type_parameter_accepts_a_width_or_a_spelling() -> None:
    """The two forms are the same declaration where they overlap, and the
    spelling reaches types a width never could."""
    spec = ParameterSpec(element_type=True, default="int32", choices=["int16", "int32", "int64"], minimum=8, maximum=64)

    # stored as the encoding, which for a plain signed integer IS the width --
    # so this declares exactly what `default: 32` always declared
    assert spec.default == 32
    assert spec.choices == (16, 32, 64)
    assert ParameterSpec(element_type=True, default=32).default == spec.default

    assert spec.violation("int16") is None
    assert spec.violation(16) is None  # the integer form still means int16
    assert spec.violation(64) is None

    # a type is matched WHOLE: q16.16 is 32 bits wide and is still not int32
    assert "got q16.16" in spec.violation("q16.16")
    assert "got float32" in spec.violation("float32")
    # and the message names types rather than printing their encodings
    assert "one of int16, int32, int64" in spec.violation("float32")


def test_an_element_type_parameter_bounds_the_width_not_the_encoding() -> None:
    """A registry bound mirrors an RTL guard about BITS. q16.16 encodes as
    4128 and is a 32-bit type; bounding the encoding would reject it for being
    a large number, which is a statement about nothing."""
    spec = ParameterSpec(element_type=True, default="int32", minimum=8, maximum=64)

    assert spec.violation("q16.16") is None
    assert spec.violation("float64") is None
    assert "must be <= 64" in spec.violation("int128")
    assert "must be >= 8" in spec.violation(4)


def test_an_element_type_spelling_is_refused_where_no_type_is_declared() -> None:
    """A size parameter takes a size. Silently reading "int32" as 32 on a
    capacity is the misreading the whole mechanism exists to prevent."""
    sized = ParameterSpec(default=1024)
    assert "only a parameter declared element_type" in sized.violation("int32")
    with pytest.raises(ValidationError):
        ParameterSpec(default="int32")


def test_an_element_type_default_must_name_a_type_the_spec_admits() -> None:
    with pytest.raises(ValidationError, match="names no element type"):
        ParameterSpec(element_type=True, default="int33x")
    with pytest.raises(ValidationError, match="default float32 violates"):
        ParameterSpec(element_type=True, default="float32", choices=["int32"])


def test_a_measurement_coordinate_may_name_the_type_it_was_measured_at() -> None:
    """The same point, written either way, is one point -- so migrating a
    registry entry to spellings cannot orphan an envelope that was measured
    before types existed."""
    spelled = ResourceMeasurement(
        part="xc7a200tfbg484-2", clock_ns=8.0, params={"FIELD_WIDTH": "int32"}, lut=704, ff=514, bram36=0.0, dsp=0, timing="met", wns_ns=0.873
    )
    assert spelled.params == {"FIELD_WIDTH": 32}

    definition = core("int32-row-predicate-filter")
    measured = definition.resource_measurement(part="xc7a200tfbg484-2", clock_ns=8.0, params={"FIELD_WIDTH": 32})
    assert measured.lut == 704


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
    definition = core("scratchpad")
    with pytest.raises(ValueError, match="not an int"):
        definition.storage_bytes({"CAPACITY": "4096"})
    with pytest.raises(ValueError, match="not an int"):
        definition.storage_bytes({"CAPACITY": True})
    with pytest.raises(ValueError, match="CAPACITY"):
        definition.storage_bytes({"CAPACITY": -2})
    with pytest.raises(ValueError, match="<= 16384"):
        definition.storage_bytes({"CAPACITY": 32768})


def test_a_core_resolves_hdl_from_the_package_that_declares_it(tmp_path, monkeypatch) -> None:
    """A tile contributed by another distribution finds its own HDL.

    The registry is a hydra search path, so any package joining it through
    the `hydra.lernaplugins` entry point can contribute cores. Their HDL
    used to be resolved under dau_core.hdl regardless, so a third-party
    tile could be registered from anywhere but had to vendor its Verilog
    into this package. `source_package` is the seam.
    """
    import sys

    from dau_core.cores import CoreDefinition, CoreKind, IngestRate

    foreign = tmp_path / "vendor_tiles" / "hdl"
    foreign.mkdir(parents=True)
    (foreign / "__init__.py").write_text("")
    (foreign / "vendor_widget.sv").write_text("module vendor_widget; endmodule\n")
    (tmp_path / "vendor_tiles" / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))

    definition = CoreDefinition(
        name="vendor-widget",
        kind=CoreKind.OPERATOR,
        module="vendor_widget",
        source="vendor_widget.sv",
        source_package="vendor_tiles.hdl",
        description="A tile from outside this package.",
        record_words=1,
        ingest=IngestRate(rows=1, per_cycles=2),
    )
    resolved = definition.source_path()
    assert resolved == foreign / "vendor_widget.sv"
    assert resolved.is_file()
    # and it is NOT resolved under dau_core's own tree
    from dau_core import hdl as dau_core_hdl

    assert Path(str(dau_core_hdl.__path__[0])) not in resolved.parents

    for module in [name for name in sys.modules if name.startswith("vendor_tiles")]:
        del sys.modules[module]


def test_a_well_formed_vendor_tile_does_not_trip_our_palette_guards(monkeypatch) -> None:
    """Installing a third-party tile must not fail THIS suite.

    Four guards here assert an exact set over the whole registry -- the HDL
    directory listing, the descriptor-tagged operators, the envelope-free
    tiles, and the scalar-envelope holdouts. Each is a statement about
    dau-core's own palette, and each read `loaded_cores()`, so a vendor tile
    registered through the entry point showed up as drift in all four. A
    contributor's first experience of the platform was our test suite
    failing on their correct tile.

    The line drawn is between those four and the universal-property guards
    (every operator has a golden, a contract kind, record words, a measured
    envelope). Those are contract requirements a well-formed tile satisfies,
    so they stay registry-wide on purpose -- which is why the tile below is
    built well-formed rather than minimal. It is the exact-set guards that
    fail even a perfect vendor tile, and only those are scoped.
    """
    from dau_core.cores import IngestRate

    vendor = CoreDefinition(
        name="vendor-widget",
        kind=CoreKind.OPERATOR,
        module="vendor_widget",
        source="vendor_widget.sv",
        source_package="vendor_tiles.hdl",
        description="A well-formed tile from outside this package.",
        record_words=1,
        ingest=IngestRate(rows=1, per_cycles=2),
        contract_kind=ContractKind.ROW_STREAM,
        golden="dau_core.operators:filter_values",
        resources=[
            ResourceMeasurement(
                part="xc7k325t",
                clock_ns=8.0,
                lut=100,
                ff=100,
                bram36=0,
                dsp=0,
                timing=TimingStatus.MET,
                wns_ns=1.0,
            )
        ],
    )
    # Both bindings: this module imported the accessor by name, and
    # `cores_of_kind` reads the registry's own. Patching only one would leave
    # the vendor tile invisible to half the guards and prove nothing.
    #
    # The module target is `sys.modules[__name__]` rather than a dotted
    # string. There is no `__init__.py` here, so pytest imports this file as
    # top-level `test_cores` while `"dau_core.tests.test_cores"` resolves
    # through the implicit namespace package to a SECOND copy -- patching
    # that one succeeds, changes nothing, and the test passes against
    # unscoped guards.
    import sys

    with_vendor = {**loaded_cores(), vendor.name: vendor}
    monkeypatch.setattr("dau_core.registry.loaded_cores", lambda: with_vendor)
    monkeypatch.setattr(sys.modules[__name__], "loaded_cores", lambda: with_vendor)

    assert vendor in cores_of_kind(CoreKind.OPERATOR), "the tile must be visible to the guards"

    # The four exact-set guards, which is the regression. Each fails against
    # an unscoped `own_cores()`: the vendor source is not in our HDL
    # directory, and its name is in neither the descriptor set, the
    # envelope-free set, nor the scalar-holdout set.
    test_every_hdl_file_belongs_to_exactly_one_core()
    test_every_selectable_operator_declares_its_contract_kind()
    test_synthesizable_cores_carry_measured_envelopes()
    test_the_scalar_envelope_holdouts_are_exactly_the_declared_ones()

    assert vendor not in own_cores(), "a vendor tile must not count as ours"


def test_a_core_naming_an_uninstalled_hdl_package_refuses_clearly() -> None:
    """The failure names the missing distribution rather than surfacing an
    ImportError from inside a path resolution."""
    import pytest

    from dau_core.cores import CoreDefinition, CoreKind, IngestRate, UnknownCoreSourceError

    definition = CoreDefinition(
        name="absent-widget",
        kind=CoreKind.OPERATOR,
        module="absent_widget",
        source="absent_widget.sv",
        source_package="no_such_vendor.hdl",
        description="Declares a package nobody installed.",
        record_words=1,
        ingest=IngestRate(rows=1, per_cycles=2),
    )
    with pytest.raises(UnknownCoreSourceError, match="no_such_vendor.hdl"):
        definition.source_path()


def test_a_measured_point_matches_a_derived_clock_period() -> None:
    """A clock period is DERIVED (1000 / job_clock_mhz), so exact float
    equality is the wrong test.

    150 MHz gives 6.666666666666667 while a registry writes 6.66667: the same
    physical clock, and the lookup refused it as unmeasured while printing the
    point it had just rejected. Real clock tiers differ by more than a
    nanosecond, so a picosecond tolerance keeps the "never a neighboring
    point" guarantee intact while tolerating representation.
    """
    from dau_core.cores import CoreDefinition, IngestRate, ResourceMeasurement, TimingStatus, UnmeasuredResourceError

    definition = CoreDefinition(
        name="probe",
        kind=CoreKind.OPERATOR,
        module="dau_probe",
        source="dau_probe.sv",
        description="probe",
        golden="dau_core.operators:unpack_rows",
        ingest=IngestRate(rows=1, per_cycles=1),
        record_words=1,
        resources=(
            ResourceMeasurement(
                part="xc7k325tffg900-2",
                clock_ns=6.66667,
                params={},
                lut=1,
                ff=1,
                bram36=0.0,
                dsp=0,
                timing=TimingStatus.MET,
                wns_ns=0.1,
            ),
        ),
    )
    point = definition.resource_measurement(part="xc7k325tffg900-2", clock_ns=1000 / 150, params={})
    assert point.lut == 1

    # and a genuinely different tier still misses
    with pytest.raises(UnmeasuredResourceError):
        definition.resource_measurement(part="xc7k325tffg900-2", clock_ns=8.0, params={})


def test_an_unresolvable_hdl_source_says_so() -> None:
    """A declared source that does not resolve is a DECLARATION error.

    An out-of-tree tile whose HDL directory is not an importable package
    resolves to a MultiplexedPath that prints the right path and does not
    exist, so the first symptom used to be a synthesis run against a missing
    file. The requirement -- an __init__.py beside the .sv files, so importlib
    can traverse the directory -- is stated in the error rather than left to
    be discovered.
    """
    from dau_core.cores import CoreDefinition, IngestRate, UnknownCoreSourceError

    definition = CoreDefinition(
        name="probe-missing-source",
        kind=CoreKind.OPERATOR,
        module="dau_probe_missing",
        source="dau_probe_missing.sv",
        description="probe",
        golden="dau_core.operators:unpack_rows",
        ingest=IngestRate(rows=1, per_cycles=1),
        record_words=1,
    )
    with pytest.raises(UnknownCoreSourceError, match="importable package"):
        definition.source_path()
