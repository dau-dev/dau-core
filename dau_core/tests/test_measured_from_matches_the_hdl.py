"""A recorded envelope must say which RTL produced it, and still match it.

Resource numbers are not self-checking. They are transcribed from a
synthesis run into the registry by hand, and once written nothing in the
tree can tell whether the RTL beneath them has moved since: the drift
detector that would notice (``synthesize_cores.parse_reports``'
``registered_matches``) only runs when somebody runs Vivado. Two separate
stale-envelope findings -- ``row-unpack`` understated by ~1000 LUT at three
of its coordinates, ``wide-field-sum-aggregation`` by up to 106 LUT and 141
FF at three more -- trace to ONE commit with one shape: it re-measured the
coordinates it ADDED and not the ones it CHANGED. Both were found by
accident, months later, while attributing something else.

``measured_from`` closes that. It stamps the recorded numbers with a digest
of the HDL closure they describe (:meth:`CoreDefinition.hdl_closure_digest`
-- the tile's own source, the packages it imports, the headers it includes),
and this module recomputes it. An edit to any of those files fails HERE,
naming the tile, in the fast job, seconds after the edit -- which is the
property that matters: the check fires on exactly the tiles a change
TOUCHED, not on the ones somebody remembers to look at.
"""

from __future__ import annotations

import pytest

from dau_core.cores import (
    HDL_CLOSURE_DIGEST_VERSION,
    CoreDefinition,
    CoreKind,
    UnknownCoreSourceError,
)
from dau_core.exp_generator import ExpTileGenerator
from dau_core.registry import loaded_cores
from dau_core.tests.test_cores import OWN_PACKAGE, own_cores


def _resolve(name: str):
    return loaded_cores().get(name)


# Cores whose recorded numbers predate `measured_from` and therefore carry no
# stamp. Nothing can be inferred about them: not that the RTL moved, and not
# that it did not.
#
# ALLOWING THIS IS A CHOICE, and it is the opposite of the one the design
# cache made for its timing gate. There, an absent `wns_ns` REFUSES, and the
# argument for refusing is about the WRITERS: every manifest a shell build
# stamps carries `build_status` and `wns_ns` together, and the one writer that
# emits `built` without a slack emits no `composition_spec` and was never
# cacheable, so refusing the ambiguous state invalidated no history because
# the ambiguous state could not arise.
#
# That argument is not available here, and the writers say so in the other
# direction: no writer has ever emitted a stamp, so "absent" is the state of
# EVERY point in the registry -- 234 of them across 54 cores. Refusing it
# would fail the whole palette on the first run and the guard would be
# switched off the same afternoon. Allowing it silently would mean the guard
# never binds on anything, forever, since nothing would ever force a stamp.
#
# So it is allowed and PINNED, in the exact-set style this suite already uses
# for `UNMIGRATED_SCALAR_ENVELOPES` and `WIDTH_NAMED_TILES`, asserted in BOTH
# directions. A tile that gains a stamp must leave this set or the test fails.
# A NEW tile, or one whose numbers are re-measured, cannot arrive unstamped
# without adding a line here that a reviewer has to accept. The set can only
# shrink, and the suite -- not whoever last read the yaml -- owns the count.
#
# 54 as of 2026-08-18, which was every measured core in the palette; 48
# once the map/ALU family re-measured its whole recorded surface -- all
# nine of the wide tile's coordinates, all four of each other tile's --
# and stamped what came back.
UNSTAMPED_ENVELOPES: frozenset[str] = frozenset(
    {
        "arrow-lite-stream-aggregation",
        "axi-burst-reader",
        "axi-random-probe",
        "axi-record-writer",
        "axi-stream-job",
        "axi-write-mux",
        "bar-aggregation",
        "bitonic-sorter",
        "bram-stream-job",
        "broadcast-hash-join",
        "datetime-format",
        "field-sum-aggregation",
        "fixed-point-divide",
        "fixed-point-exp",
        "fixed-point-exp-light",
        "fixed-point-ln",
        "fixed-point-ln-light",
        "fixed-point-sqrt",
        "grouped-aggregation",
        "grouped-field-aggregation",
        "grouped-field-sum-aggregation",
        "hash-grouped-aggregation",
        "int32-keyed-predicate-filter",
        "int32-predicate-filter",
        "int32-record-gather",
        "key-mask-dispatcher",
        "key-membership-filter",
        "keyed-state",
        "normal-cdf",
        "pair-key-filter",
        "range-partitioner",
        "record-batch-aggregation",
        "record-gearbox",
        "rolling-extremum",
        "rolling-moments",
        "row-predicate-filter",
        "row-unpack",
        "scratchpad",
        "session-gap",
        "stream-aggregation",
        "stream-broadcast",
        "streaming-dedup",
        "streaming-top-k",
        "stride-distributor",
        "time-bucket-key",
        "wide-field-sum-aggregation",
        "wide-row-predicate-filter",
    }
)


def _measured_own_cores() -> tuple[CoreDefinition, ...]:
    return tuple(definition for definition in own_cores() if definition.resources is not None)


def test_every_stamped_envelope_matches_its_hdl_closure() -> None:
    """THE GUARD. A stamped tile's HDL must still digest to what its numbers
    were recorded against.

    Failing here does NOT mean the numbers are wrong -- it means nobody has
    said whether they are. There are exactly two honest ways out and neither
    is editing the digest to match:

    1. Re-measure the tile's recorded coordinates through
       ``dau-build task=tasks/build/synthesize-cores`` and record what comes
       back, stamp included.
    2. Show the edit is netlist-neutral -- one control coordinate reporting
       ``envelope_drift=none`` against the pre-change tree -- and re-stamp
       with that evidence in the pull request.
    """
    stale = []
    for definition in _measured_own_cores():
        if definition.measured_from is None:
            continue
        current = definition.hdl_closure_digest(_resolve)
        if current != definition.measured_from:
            closure = ", ".join(name for name, _ in definition.hdl_closure(_resolve))
            stale.append(f"{definition.name}: recorded {definition.measured_from}, HDL now digests to {current} (closure: {closure})")
    assert not stale, (
        "these tiles' HDL has changed since their resource numbers were measured, so the registry is pricing designs "
        "against RTL that no longer exists:\n  " + "\n  ".join(stale) + "\n"
        "Re-measure through `dau-build task=tasks/build/synthesize-cores` and record what comes back, or show the edit "
        "is netlist-neutral at a control coordinate and re-stamp with that evidence."
    )


def test_the_unstamped_envelopes_are_exactly_the_declared_ones() -> None:
    """Both directions, for the reason every exact set in this suite is:
    a stale name means a tile got stamped and nobody tightened the guard, and
    an unlisted name means a measurement shipped with no record of what it
    measured."""
    unstamped = {definition.name for definition in _measured_own_cores() if definition.measured_from is None}
    stamped_but_still_listed = UNSTAMPED_ENVELOPES - unstamped
    assert not stamped_but_still_listed, f"stamped -- remove from UNSTAMPED_ENVELOPES: {sorted(stamped_but_still_listed)}"
    newly_unstamped = unstamped - UNSTAMPED_ENVELOPES
    assert not newly_unstamped, (
        f"these tiles record resource numbers with no record of the RTL they were measured against: {sorted(newly_unstamped)}. "
        "`dau-build task=tasks/build/synthesize-cores` reports the digest to stamp beside the numbers"
    )


def test_every_core_has_a_computable_hdl_closure() -> None:
    """A closure that cannot be resolved is a defect whether or not the tile
    is stamped yet -- otherwise a missing header or an unregistered dependency
    stays invisible until the day somebody first tries to stamp that tile."""
    for definition in own_cores():
        digest = definition.hdl_closure_digest(_resolve)
        assert digest.startswith(f"{HDL_CLOSURE_DIGEST_VERSION}:"), definition.name
        assert definition.hdl_closure(_resolve), definition.name


def test_the_closure_holds_the_packages_a_tile_imports_and_the_headers_it_includes() -> None:
    """The two directions that decide whether the guard works.

    Too permissive and it misses the edit that matters: a package edit must
    reach every tile that imports it. Too strict and every unrelated edit
    re-measures the world: a header a tile does not include must not reach it.
    """
    map_alu = loaded_cores()["int32-map-alu"]
    closure = dict(map_alu.hdl_closure(_resolve))
    assert f"{OWN_PACKAGE}/dau_map_alu_pkg.sv" in closure, "a package a tile imports is in its closure"
    assert f"{OWN_PACKAGE}/types.svh" not in closure, "a header a tile does not include is not in its closure"

    predicate_filter = loaded_cores()["row-predicate-filter"]
    included = dict(predicate_filter.hdl_closure(_resolve))
    assert f"{OWN_PACKAGE}/types.svh" in included, "a header a tile DOES include is in its closure"
    assert f"{OWN_PACKAGE}/dau_predicate_pkg.sv" in included


def test_a_generated_core_is_digested_from_its_rendered_text() -> None:
    """A generated tile has no checked-in `.sv`, so a digest over files on
    disk would be blind to the one thing that decides its RTL. Its constants
    live in the generator and in the registry entry's generator config; move
    either and the digest must move."""
    definition = loaded_cores()["fixed-point-exp"]
    assert isinstance(definition.generator, ExpTileGenerator)
    before = definition.hdl_closure_digest(_resolve)
    datapath = definition.generator.datapath
    moved = definition.model_copy(
        update={"generator": definition.generator.model_copy(update={"datapath": datapath.model_copy(update={"degree": datapath.degree + 1})})}
    )
    assert moved.hdl_closure_digest(lambda name: moved if name == moved.name else _resolve(name)) != before


def test_a_moved_dependency_moves_every_dependent_tiles_digest(tmp_path, monkeypatch) -> None:
    """Proved by construction rather than by being green: editing a shared
    package must fail the tiles that import it, which is the case a per-file
    stamp on the tile alone would miss."""
    package = loaded_cores()["map-alu-pkg"]
    original = package.source_path().read_bytes()
    definition = loaded_cores()["int32-map-alu"]
    before = definition.hdl_closure_digest(_resolve)
    try:
        package.source_path().write_bytes(original + b"\n// an edit to the shared package\n")
        assert definition.hdl_closure_digest(_resolve) != before
    finally:
        package.source_path().write_bytes(original)
    assert definition.hdl_closure_digest(_resolve) == before


def test_an_unresolvable_include_refuses_rather_than_digesting_a_partial_closure(tmp_path, monkeypatch) -> None:
    """A closure that quietly dropped a file it could not find would produce a
    digest that is stable across an edit to that file -- the exact failure the
    stamp exists to prevent, dressed as a pass."""
    (tmp_path / "dau_stand_in.sv").write_text('`include "not_shipped_here.svh"\nmodule dau_stand_in; endmodule\n')
    monkeypatch.setattr("dau_core.cores.package_root", lambda package: tmp_path)
    definition = CoreDefinition(
        name="includes-a-missing-header",
        kind=CoreKind.STREAM_INFRA,
        module="dau_stand_in",
        source="dau_stand_in.sv",
        source_package="stand_in.hdl",
        description="a stand-in whose source includes a header no source directory ships",
    )
    with pytest.raises(UnknownCoreSourceError, match="not closed"):
        definition.hdl_closure_digest(lambda name: definition if name == definition.name else None)


def test_an_unregistered_dependency_refuses_rather_than_digesting_a_partial_closure() -> None:
    """The same rule one edge out: a package the registry cannot resolve is a
    file synthesis would read and the digest would not."""
    definition = loaded_cores()["int32-map-alu"]
    with pytest.raises(UnknownCoreSourceError, match="registry does not resolve"):
        definition.hdl_closure_digest(lambda name: definition if name == definition.name else None)


def test_a_stamp_without_a_measurement_is_refused() -> None:
    """The stamp says which RTL a measurement describes; without one it says
    nothing and reads as though the tile were checked."""
    with pytest.raises(ValueError, match="records no resources"):
        CoreDefinition(
            name="stamped-but-unmeasured",
            kind=CoreKind.SHELL_ADAPTER,
            module="dau_identity_registers",
            source="dau_identity_registers.sv",
            description="a stand-in stamping RTL it recorded no numbers for",
            measured_from=f"{HDL_CLOSURE_DIGEST_VERSION}:{'0' * 64}",
        )


def test_the_shell_register_block_is_stamped_and_its_stamp_matches() -> None:
    """`identity-registers` is a combinational shell adapter measured on both
    parts (2026-10-06) with no clock; its stamp was written by the build
    host's run and must match the RTL this checkout carries."""
    definition = loaded_cores()["identity-registers"]
    assert definition.resources is not None and all(point.clock_ns is None for point in definition.resources)
    assert definition.measured_from == definition.hdl_closure_digest(_resolve)
