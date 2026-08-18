from __future__ import annotations

import os

import pytest

from dau_core.cores import ContractKind, CoreKind
from dau_core.registry import cores_of_kind


def test_every_operator_conforms_to_the_stream_tile_contract() -> None:
    """Slang-parsed conformance: each non-legacy operator's real port
    interface matches the stream+status contract its registry entry promises
    (drift fails here, not at Vivado elaboration)."""
    # runtime installs may skip (dau-build is a develop extra), but CI runs
    # the develop install: there a missing validator is a broken environment,
    # not a reason to silently pass the registry unchecked
    if os.environ.get("CI", "").strip().lower() in ("1", "true", "yes"):
        try:
            from dau_build import sv_contract
        except ImportError:
            pytest.fail(
                "dau_build.sv_contract is missing in CI: the develop install (dau-build extra) is broken, so the registry conformance check cannot run"
            )
    else:
        sv_contract = pytest.importorskip("dau_build.sv_contract", reason="dau-build not installed")

    failures = []
    for definition in cores_of_kind(CoreKind.OPERATOR):
        # validator selection is registry data: only row-stream tiles run this
        # check (stream-aggregation declares the descriptor-driven
        # Arrow-lite contract, which has no slang validator yet)
        if definition.contract_kind is not ContractKind.ROW_STREAM:
            continue
        violations = sv_contract.validate_stream_tile([definition.source_path()], definition.module, count_port=definition.count_port)
        if violations:
            failures.append(f"{definition.name}: {violations}")
    assert not failures, "\n".join(failures)
