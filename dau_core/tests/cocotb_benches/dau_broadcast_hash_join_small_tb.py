from __future__ import annotations

import cocotb

from dau_core.operators import (
    BroadcastJoinConfig,
    JoinErrorCode,
    JoinSource,
    broadcast_hash_join_rows,
)
from dau_core.tests.cocotb_benches.dau_broadcast_hash_join_tb import (
    _assert_quiescent,
    _drive_cfg,
    _expect_status,
    _load,
    _probe,
    _send_rows,
    _start,
)

# KEY_SPACE == 64 is a small direct-mapped table (AWIDTH == 6): the clear sweep
# is 64 cycles, the boundary keys are 0 and 63, and 64 / -1 are out of range.
# The pytest runner builds the DUT with parameters={"KEY_SPACE": 64}; the
# KEY_SPACE-agnostic helpers are shared with the KEY_SPACE=1024 bench.
_KEY_SPACE = 64

_CARRY_BUILD = BroadcastJoinConfig(out0=JoinSource.B0, out1=JoinSource.B1, out2=JoinSource.B2)

_BUILD = [(7, 100, 101, 102), (0, 200, 201, 202), (63, 300, 301, 302)]
_PROBE = [
    (7, 1, 1, 1),  # hit
    (0, 2, 2, 2),  # hit (low boundary)
    (63, 3, 3, 3),  # hit (high boundary)
    (40, 4, 4, 4),  # in-range miss
]


@cocotb.test()
async def small_table_load_then_probe(dut):
    await _start(dut)
    await _load(dut, _BUILD)
    out = await _probe(dut, _PROBE, _CARRY_BUILD)
    assert out == broadcast_hash_join_rows(_BUILD, _PROBE, out_sel=_CARRY_BUILD, key_space=_KEY_SPACE)
    assert out == [(7, 100, 101, 102), (0, 200, 201, 202), (63, 300, 301, 302)]


@cocotb.test()
async def small_table_second_load_replaces(dut):
    await _start(dut)
    await _load(dut, [(7, 11, 12, 13), (63, 21, 22, 23)])
    assert await _probe(dut, [(7, 0, 0, 0), (63, 0, 0, 0)], _CARRY_BUILD) == [(7, 11, 12, 13), (63, 21, 22, 23)]
    await _load(dut, [(1, 31, 32, 33)])
    assert await _probe(dut, [(7, 0, 0, 0), (63, 0, 0, 0)], _CARRY_BUILD) == []
    assert await _probe(dut, [(1, 0, 0, 0)], _CARRY_BUILD) == [(1, 31, 32, 33)]


@cocotb.test()
async def small_table_out_of_range_keys_reject(dut):
    await _start(dut)
    await _load(dut, _BUILD)
    _drive_cfg(dut, load=0, cfg=_CARRY_BUILD)
    send = cocotb.start_soon(_send_rows(dut, [(_KEY_SPACE, 1, 1, 1)]))  # key 64 out of range
    await _expect_status(dut, error=True, code=JoinErrorCode.RANGE)
    await send
    _drive_cfg(dut, load=0, cfg=_CARRY_BUILD)
    send = cocotb.start_soon(_send_rows(dut, [(-1, 1, 1, 1)]))  # negative out of range
    await _expect_status(dut, error=True, code=JoinErrorCode.RANGE)
    await send
    await _assert_quiescent(dut)
    # recovers
    assert await _probe(dut, _PROBE, _CARRY_BUILD) == broadcast_hash_join_rows(_BUILD, _PROBE, out_sel=_CARRY_BUILD, key_space=_KEY_SPACE)
