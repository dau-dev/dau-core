from __future__ import annotations

import os

import pytest

from dau_core.cores import CoreKind, cores_of_kind

# genuinely different interfaces, exempt by design (not drift):
# - int32-stream-aggregation speaks the descriptor-driven Arrow-lite protocol,
#   not the row-stream tile contract (a registry contract tag is future work)
_NOT_ROW_STREAM_TILES = {"int32-stream-aggregation"}


def test_every_operator_conforms_to_the_stream_tile_contract() -> None:
    """Slang-parsed conformance: each non-legacy operator's real port
    interface matches the stream+status contract its registry entry promises
    (drift fails here, not at Vivado elaboration)."""
    # runtime installs may skip (dau-build is a develop extra), but CI runs
    # the develop install: there a missing validator is a broken environment,
    # not a reason to silently pass the registry unchecked
    if os.environ.get("CI"):
        import dau_build.sv_contract as sv_contract
    else:
        sv_contract = pytest.importorskip("dau_build.sv_contract", reason="dau-build not installed")

    failures = []
    for definition in cores_of_kind(CoreKind.OPERATOR):
        if definition.legacy or definition.name in _NOT_ROW_STREAM_TILES:
            continue
        violations = sv_contract.validate_stream_tile([definition.source_path()], definition.module, count_port=definition.count_port)
        if violations:
            failures.append(f"{definition.name}: {violations}")
    assert not failures, "\n".join(failures)
