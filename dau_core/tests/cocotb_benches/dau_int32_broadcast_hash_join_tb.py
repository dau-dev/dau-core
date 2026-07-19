from __future__ import annotations

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import (
    BroadcastJoinConfig,
    JoinErrorCode,
    JoinSource,
    broadcast_hash_join_rows,
)

_MASK32 = 0xFFFF_FFFF
_MASK64 = 0xFFFF_FFFF_FFFF_FFFF
_INT32_MIN = -(1 << 31)
_INT32_MAX = (1 << 31) - 1

# matches the DUT parameter default
_KEY_SPACE = 1024

# build side: (key, b0, b1, b2) — unique keys (PK), fits BRAM
_BUILD = [
    (7, 100, 101, 102),
    (100, 200, 201, 202),
    (63, 300, 301, 302),
    (1023, 400, 401, 402),
    (5, 500, 501, 502),
]

# probe side: (key, v0, v1, v2) — some hit, some miss
_PROBE = [
    (7, 1000, 1001, 1002),  # hit
    (999, 2000, 2001, 2002),  # miss
    (63, 3000, 3001, 3002),  # hit
    (100, 4000, 4001, 4002),  # hit
    (200, 5000, 5001, 5002),  # miss
    (1023, 6000, 6001, 6002),  # hit
]

# carry build cols (b0,b1,b2), probe cols (v0,v1,v2), and a q3-style mix
_CARRY_BUILD = BroadcastJoinConfig(JoinSource.B0, JoinSource.B1, JoinSource.B2)
_CARRY_PROBE = BroadcastJoinConfig(JoinSource.V0, JoinSource.V1, JoinSource.V2)
_Q3_MIX = BroadcastJoinConfig(JoinSource.B0, JoinSource.V0, JoinSource.B2)


@cocotb.test()
async def probe_before_any_load_misses(dut):
    # MUST stay the first test in this module: it validates the power-on /
    # zero-initialised empty table. The table is BRAM and rst does NOT clear it
    # (only the load-phase sweep or the INIT does), so once a later test loads
    # build rows the table is no longer empty. Run first, before any load
    # populates the BRAM, the initial content is 0 (every slot invalid) and
    # every probe misses.
    await _start(dut)
    assert await _probe(dut, _PROBE, _CARRY_BUILD) == []
    assert int(dut.joined_count.value) == 0


@cocotb.test()
async def load_then_probe_inner_join(dut):
    await _start(dut)
    await _load(dut, _BUILD)
    for cfg in (_CARRY_BUILD, _CARRY_PROBE, _Q3_MIX):
        out = await _probe(dut, _PROBE, cfg)
        assert out == broadcast_hash_join_rows(_BUILD, _PROBE, out_sel=cfg, key_space=_KEY_SPACE), cfg


@cocotb.test()
async def carry_build_columns_exact_hits(dut):
    await _start(dut)
    await _load(dut, _BUILD)
    out = await _probe(dut, _PROBE, _CARRY_BUILD)
    # only the four hits emit, in probe order, carrying the build payload
    assert out == [
        (7, 100, 101, 102),
        (63, 300, 301, 302),
        (100, 200, 201, 202),
        (1023, 400, 401, 402),
    ]


@cocotb.test()
async def q3_style_mix_carries_build_and_probe_columns(dut):
    await _start(dut)
    await _load(dut, _BUILD)
    out = await _probe(dut, _PROBE, _Q3_MIX)
    # out = (key, b0, v0, b2) per hit
    assert out == [
        (7, 100, 1000, 102),
        (63, 300, 3000, 302),
        (100, 200, 4000, 202),
        (1023, 400, 6000, 402),
    ]


@cocotb.test()
async def all_probe_rows_miss_emits_nothing(dut):
    # empty result: every probe key misses -> zero emitted rows -> success close-out
    await _start(dut)
    await _load(dut, _BUILD)
    misses = [(999, 1, 1, 1), (200, 2, 2, 2), (500, 3, 3, 3)]  # none in the build set
    assert await _probe(dut, misses, _CARRY_BUILD) == []
    assert int(dut.joined_count.value) == 0


@cocotb.test()
async def last_writer_wins_on_duplicate_build_key(dut):
    await _start(dut)
    # key 7 built twice; the second payload wins
    build = [(7, 11, 12, 13), (7, 21, 22, 23), (9, 90, 91, 92)]
    await _load(dut, build)
    out = await _probe(dut, [(7, 0, 0, 0), (9, 0, 0, 0)], _CARRY_BUILD)
    assert out == [(7, 21, 22, 23), (9, 90, 91, 92)]
    assert out == broadcast_hash_join_rows(build, [(7, 0, 0, 0), (9, 0, 0, 0)], out_sel=_CARRY_BUILD, key_space=_KEY_SPACE)


@cocotb.test()
async def boundary_keys_join(dut):
    await _start(dut)
    build = [(0, 1, 2, 3), (_KEY_SPACE - 1, 4, 5, 6)]
    await _load(dut, build)
    probe = [(0, 0, 0, 0), (_KEY_SPACE - 1, 0, 0, 0), (1, 0, 0, 0)]  # low, high, in-range miss
    out = await _probe(dut, probe, _CARRY_BUILD)
    assert out == [(0, 1, 2, 3), (_KEY_SPACE - 1, 4, 5, 6)]


@cocotb.test()
async def second_load_replaces_the_table(dut):
    await _start(dut)
    await _load(dut, [(7, 11, 12, 13), (63, 21, 22, 23)])
    assert await _probe(dut, [(7, 0, 0, 0), (63, 0, 0, 0)], _CARRY_BUILD) == [(7, 11, 12, 13), (63, 21, 22, 23)]
    # a fresh load sweeps the valid bits — the old keys are gone
    await _load(dut, [(100, 31, 32, 33), (200, 41, 42, 43)])
    assert await _probe(dut, [(7, 0, 0, 0), (63, 0, 0, 0)], _CARRY_BUILD) == []
    assert await _probe(dut, [(100, 0, 0, 0), (200, 0, 0, 0)], _CARRY_BUILD) == [(100, 31, 32, 33), (200, 41, 42, 43)]


@cocotb.test()
async def repeated_probe_batches_reuse_the_table(dut):
    await _start(dut)
    await _load(dut, _BUILD)
    for _ in range(3):
        out = await _probe(dut, _PROBE, _CARRY_BUILD)
        assert out == broadcast_hash_join_rows(_BUILD, _PROBE, out_sel=_CARRY_BUILD, key_space=_KEY_SPACE)


@cocotb.test()
async def joined_count_reports_emitted_rows(dut):
    await _start(dut)
    await _load(dut, _BUILD)
    out = await _probe(dut, _PROBE, _CARRY_BUILD)
    assert int(dut.joined_count.value) == len(out) == 4
    # a load leaves joined_count at zero
    await _load(dut, _BUILD)
    assert int(dut.joined_count.value) == 0


@cocotb.test()
async def last_lands_on_final_emitted_row(dut):
    # trailing probe rows miss: last must move onto the final emitted word
    await _start(dut)
    await _load(dut, [(7, 100, 101, 102)])
    rows = [(7, 1, 1, 1), (100, 2, 2, 2), (200, 3, 3, 3)]  # only the first hits
    assert await _probe(dut, rows, _CARRY_BUILD) == [(7, 100, 101, 102)]


@cocotb.test()
async def output_backpressure_holds_stable(dut):
    await _start(dut)
    await _load(dut, _BUILD)
    out = await _probe(dut, _PROBE, _CARRY_BUILD, ready_pattern=(1, 0, 0, 1, 0))
    assert out == broadcast_hash_join_rows(_BUILD, _PROBE, out_sel=_CARRY_BUILD, key_space=_KEY_SPACE)


@cocotb.test()
async def reset_mid_probe_recovers(dut):
    await _start(dut)
    await _load(dut, _BUILD)
    # drive a lone w0 then assert reset, abandoning the batch
    _drive_cfg(dut, load=0, cfg=_CARRY_BUILD)
    dut.input_valid.value = 1
    dut.input_data.value = (1000 << 32) | 7
    dut.input_last.value = 0
    await RisingEdge(dut.clk)
    dut.input_valid.value = 0
    dut.rst.value = 1
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)
    await _assert_quiescent(dut)
    # a clean load + probe works after reset
    await _load(dut, _BUILD)
    assert await _probe(dut, _PROBE, _CARRY_BUILD) == broadcast_hash_join_rows(_BUILD, _PROBE, out_sel=_CARRY_BUILD, key_space=_KEY_SPACE)


@cocotb.test()
async def out_of_range_probe_key_reports_range_error(dut):
    await _start(dut)
    await _load(dut, _BUILD)
    _drive_cfg(dut, load=0, cfg=_CARRY_BUILD)
    # a probe row whose key >= KEY_SPACE
    send = cocotb.start_soon(_send_rows(dut, [(_KEY_SPACE, 1, 1, 1)]))
    await _expect_status(dut, error=True, code=JoinErrorCode.RANGE)
    await send
    assert int(dut.joined_count.value) == 0
    # recovers
    assert await _probe(dut, _PROBE, _CARRY_BUILD) == broadcast_hash_join_rows(_BUILD, _PROBE, out_sel=_CARRY_BUILD, key_space=_KEY_SPACE)


@cocotb.test()
async def negative_probe_key_reports_range_error(dut):
    await _start(dut)
    await _load(dut, _BUILD)
    _drive_cfg(dut, load=0, cfg=_CARRY_BUILD)
    send = cocotb.start_soon(_send_rows(dut, [(-1, 1, 1, 1)]))
    await _expect_status(dut, error=True, code=JoinErrorCode.RANGE)
    await send
    assert await _probe(dut, _PROBE, _CARRY_BUILD) == broadcast_hash_join_rows(_BUILD, _PROBE, out_sel=_CARRY_BUILD, key_space=_KEY_SPACE)


@cocotb.test()
async def out_of_range_build_key_reports_range_error(dut):
    await _start(dut)
    _drive_cfg(dut, load=1, cfg=_CARRY_BUILD)
    send = cocotb.start_soon(_send_rows(dut, [(_KEY_SPACE, 1, 1, 1)]))  # build key out of range
    await _expect_status(dut, error=True, code=JoinErrorCode.RANGE)
    await send
    # recovers: a well-formed load then probe works
    await _load(dut, _BUILD)
    assert await _probe(dut, _PROBE, _CARRY_BUILD) == broadcast_hash_join_rows(_BUILD, _PROBE, out_sel=_CARRY_BUILD, key_space=_KEY_SPACE)


@cocotb.test()
async def range_error_mid_batch_after_hits(dut):
    # prior hits, then an out-of-range key: the batch is invalid (ERR_RANGE)
    await _start(dut)
    await _load(dut, _BUILD)
    _drive_cfg(dut, load=0, cfg=_CARRY_BUILD)
    drain = cocotb.start_soon(_drain_words(dut))
    send = cocotb.start_soon(_send_words(dut, _pack_rows([(7, 1, 1, 1), (63, 2, 2, 2)]) + _pack_rows([(_KEY_SPACE, 3, 3, 3)])))
    await _expect_status(dut, error=True, code=JoinErrorCode.RANGE)
    await send
    drain.kill()
    dut.output_ready.value = 0
    assert int(dut.joined_count.value) == 0
    assert await _probe(dut, _PROBE, _CARRY_BUILD) == broadcast_hash_join_rows(_BUILD, _PROBE, out_sel=_CARRY_BUILD, key_space=_KEY_SPACE)


@cocotb.test()
async def torn_probe_row_reports_stream_error(dut):
    await _start(dut)
    await _load(dut, _BUILD)
    _drive_cfg(dut, load=0, cfg=_CARRY_BUILD)
    await _send_words(dut, [(1000 << 32) | 7])  # input_last on a w0
    await _expect_status(dut, error=True, code=JoinErrorCode.STREAM)
    assert int(dut.joined_count.value) == 0
    assert await _probe(dut, _PROBE, _CARRY_BUILD) == broadcast_hash_join_rows(_BUILD, _PROBE, out_sel=_CARRY_BUILD, key_space=_KEY_SPACE)


@cocotb.test()
async def torn_build_row_reports_stream_error(dut):
    await _start(dut)
    _drive_cfg(dut, load=1, cfg=_CARRY_BUILD)
    await _send_words(dut, [(100 << 32) | 7])  # a build w0 carrying input_last
    await _expect_status(dut, error=True, code=JoinErrorCode.STREAM)
    await _load(dut, _BUILD)
    assert await _probe(dut, _PROBE, _CARRY_BUILD) == broadcast_hash_join_rows(_BUILD, _PROBE, out_sel=_CARRY_BUILD, key_space=_KEY_SPACE)


@cocotb.test()
async def torn_probe_row_mid_batch_reports_stream_error(dut):
    await _start(dut)
    await _load(dut, _BUILD)
    _drive_cfg(dut, load=0, cfg=_CARRY_BUILD)
    send = cocotb.start_soon(_send_words(dut, _pack_rows(_PROBE[:2]) + [(999 << 32) | 999]))
    collect = cocotb.start_soon(_drain_words(dut))
    await _expect_status(dut, error=True, code=JoinErrorCode.STREAM)
    await send
    collect.kill()
    dut.output_ready.value = 0
    assert int(dut.joined_count.value) == 0
    assert await _probe(dut, _PROBE, _CARRY_BUILD) == broadcast_hash_join_rows(_BUILD, _PROBE, out_sel=_CARRY_BUILD, key_space=_KEY_SPACE)


@cocotb.test()
async def reserved_out_select_drains_batch_then_reports_config_error(dut):
    await _start(dut)
    await _load(dut, _BUILD)
    _drive_cfg(dut, load=0, cfg=_CARRY_BUILD)
    dut.cfg_out1_sel.value = 6  # reserved source
    send = cocotb.start_soon(_send_rows(dut, _PROBE))
    await _expect_status(dut, error=True, code=JoinErrorCode.CONFIG)
    await send
    assert await _probe(dut, _PROBE, _CARRY_BUILD) == broadcast_hash_join_rows(_BUILD, _PROBE, out_sel=_CARRY_BUILD, key_space=_KEY_SPACE)


@cocotb.test()
async def out_select_seven_reports_config_error(dut):
    await _start(dut)
    await _load(dut, _BUILD)
    _drive_cfg(dut, load=0, cfg=_CARRY_BUILD)
    dut.cfg_out2_sel.value = 7  # reserved source
    send = cocotb.start_soon(_send_rows(dut, _PROBE[:1]))
    await _expect_status(dut, error=True, code=JoinErrorCode.CONFIG)
    await send


@cocotb.test()
async def config_precedence_over_torn_and_out_of_range_first_row(dut):
    # config validity is checked at the first probe row and takes precedence
    # over stream shape: a first row that is ALSO torn or out-of-range still
    # reports CONFIG, not STREAM / RANGE
    await _start(dut)
    await _load(dut, _BUILD)
    _drive_cfg(dut, load=0, cfg=_CARRY_BUILD)
    dut.cfg_out0_sel.value = 6  # reserved source
    await _send_words(dut, [(1000 << 32) | 7])  # torn w0 AND bad config -> CONFIG
    await _expect_status(dut, error=True, code=JoinErrorCode.CONFIG)

    _drive_cfg(dut, load=0, cfg=_CARRY_BUILD)
    dut.cfg_out2_sel.value = 7  # reserved source
    send = cocotb.start_soon(_send_rows(dut, [(_KEY_SPACE, 1, 1, 1)]))  # out-of-range key AND bad config
    await _expect_status(dut, error=True, code=JoinErrorCode.CONFIG)
    await send

    assert await _probe(dut, _PROBE, _CARRY_BUILD) == broadcast_hash_join_rows(_BUILD, _PROBE, out_sel=_CARRY_BUILD, key_space=_KEY_SPACE)


@cocotb.test()
async def field_select_latches_at_first_row(dut):
    # the field-select is latched at the first probe row; changing the cfg
    # ports mid-batch must not re-select the emitted fields
    await _start(dut)
    await _load(dut, _BUILD)
    _drive_cfg(dut, load=0, cfg=_CARRY_BUILD)  # first row latches CARRY_BUILD
    # after the first word fires, flip the live cfg to CARRY_PROBE
    send = cocotb.start_soon(_send_words_flip_cfg(dut, _pack_rows([(7, 1, 1, 1), (63, 2, 2, 2)]), flip_after=0, new_cfg=_CARRY_PROBE))
    words = await _collect_probe_output(dut, (1,))
    await send
    out = [
        (_signed32(words[o] & _MASK32), _signed32(words[o] >> 32), _signed32(words[o + 1] & _MASK32), _signed32(words[o + 1] >> 32))
        for o in range(0, len(words), 2)
    ]
    # both rows carry the BUILD payload (the latched cfg), not the live probe cfg
    assert out == [(7, 100, 101, 102), (63, 300, 301, 302)]


@cocotb.test()
async def error_status_survives_backpressure(dut):
    # while status_ready is held low the error close-out stays stable and the
    # tile refuses new input; recovery only after the handshake
    await _start(dut)
    await _load(dut, _BUILD)
    _drive_cfg(dut, load=0, cfg=_CARRY_BUILD)
    dut.status_ready.value = 0
    await _send_words(dut, [(1000 << 32) | 7])  # torn w0

    found = False
    for _ in range(8):
        await ReadOnly()
        found = int(dut.status_valid.value) == 1
        await RisingEdge(dut.clk)
        if found:
            break
    assert found, "error status never asserted"
    for _ in range(6):
        await ReadOnly()
        assert int(dut.status_valid.value) == 1, "error status not held"
        assert int(dut.status_error.value) == 1
        assert int(dut.status_error_code.value) == JoinErrorCode.STREAM
        assert int(dut.input_ready.value) == 0, "tile accepted input while status pending"
        await RisingEdge(dut.clk)

    await _expect_status(dut, error=True, code=JoinErrorCode.STREAM)
    assert await _probe(dut, _PROBE, _CARRY_BUILD) == broadcast_hash_join_rows(_BUILD, _PROBE, out_sel=_CARRY_BUILD, key_space=_KEY_SPACE)


@cocotb.test()
async def success_status_survives_backpressure(dut):
    # the zero-emitted-row success close-out holds stable while status_ready is
    # low and the tile refuses new input until the handshake completes
    await _start(dut)
    await _load(dut, _BUILD)
    _drive_cfg(dut, load=0, cfg=_CARRY_BUILD)
    dut.status_ready.value = 0
    await _send_rows(dut, [(999, 1, 1, 1), (200, 2, 2, 2)])  # all miss -> success close-out

    found = False
    for _ in range(8):
        await ReadOnly()
        found = int(dut.status_valid.value) == 1
        await RisingEdge(dut.clk)
        if found:
            break
    assert found, "success status never asserted"
    for _ in range(6):
        await ReadOnly()
        assert int(dut.status_valid.value) == 1, "success status not held"
        assert int(dut.status_error.value) == 0
        assert int(dut.status_error_code.value) == 0
        assert int(dut.input_ready.value) == 0, "tile accepted input while status pending"
        await RisingEdge(dut.clk)

    await _expect_status(dut, error=False)


@cocotb.test()
async def load_done_status_survives_backpressure(dut):
    # the load-done success close-out holds stable under status backpressure
    await _start(dut)
    _drive_cfg(dut, load=1, cfg=_CARRY_BUILD)
    dut.status_ready.value = 0
    await _send_rows(dut, _BUILD)

    found = False
    for _ in range(_KEY_SPACE + 64):  # after the clear sweep + build rows
        await ReadOnly()
        found = int(dut.status_valid.value) == 1
        await RisingEdge(dut.clk)
        if found:
            break
    assert found, "load-done status never asserted"
    for _ in range(6):
        await ReadOnly()
        assert int(dut.status_valid.value) == 1, "load-done status not held"
        assert int(dut.status_error.value) == 0
        assert int(dut.input_ready.value) == 0
        await RisingEdge(dut.clk)
    await _expect_status(dut, error=False)
    assert await _probe(dut, _PROBE, _CARRY_BUILD) == broadcast_hash_join_rows(_BUILD, _PROBE, out_sel=_CARRY_BUILD, key_space=_KEY_SPACE)


@cocotb.test()
async def status_close_out_waits_for_backpressured_tail(dut):
    # Cross-batch re-arm under output backpressure (the emit-drain gate). Batch
    # A emits two joined rows and its final beat is held under backpressure;
    # batch B follows immediately and would raise a success close-out (zero
    # emitted rows). The tile must not present B's close-out until A's last
    # output beat has handshaked -- otherwise B's per-batch close-out overtakes
    # A's still-in-flight tail (the lane status mux serialises one close-out per
    # batch, and the record writer's done waits on it).
    await _start(dut)
    await _load(dut, [(7, 11, 12, 13), (63, 21, 22, 23)])
    _drive_cfg(dut, load=0, cfg=_CARRY_BUILD)

    a_rows = [(7, 1, 1, 1), (63, 2, 2, 2)]  # both hit: A emits 2, last kept
    b_rows = [(200, 9, 9, 9)]  # miss: zero emitted -> success close-out
    a_expected = broadcast_hash_join_rows([(7, 11, 12, 13), (63, 21, 22, 23)], a_rows, out_sel=_CARRY_BUILD, key_space=_KEY_SPACE)
    expected = [(word, int(i == 3)) for i, word in enumerate(_pack_rows(a_expected))]

    sender = cocotb.start_soon(_send_batches(dut, [_pack_rows(a_rows), _pack_rows(b_rows)]))
    dut.status_ready.value = 1  # a downstream mux ready to accept a close-out

    words: list[tuple[int, int]] = []
    fires = 0
    a_tail_done = False
    holding = False
    hold = 0
    for _ in range(400):
        if fires < 2:
            ready = 1  # drain A's first row and trigger the flush-load of its tail
        elif not holding and not a_tail_done:
            holding = True
            ready = 0
        elif holding and hold < 15:
            ready = 0
        else:
            ready = 1  # release: A's tail drains, then B's close-out may appear
        dut.output_ready.value = ready

        await ReadOnly()
        valid = int(dut.output_valid.value) == 1
        last = int(dut.output_last.value) == 1
        word = int(dut.output_data.value)
        status = int(dut.status_valid.value) == 1
        fired = ready == 1 and valid
        # THE PROPERTY: no status close-out while a prior batch's output beat is
        # still un-drained (A's final beat has not yet handshaked).
        assert not (status and not a_tail_done), (
            "batch B success close-out asserted (status_valid=1) before batch A's "
            "final output beat handshaked -- cross-batch status overtake under backpressure"
        )
        await RisingEdge(dut.clk)
        if holding:
            hold += 1
        if fired:
            words.append((word, last))
            fires += 1
            if last:
                a_tail_done = True
                break

    assert a_tail_done, "A's final beat never handshaked"
    assert words == expected, f"emitted beats/last wrong: {words} != {expected}"

    # only now may B's success close-out appear; then the lane is quiescent
    await _expect_status(dut, error=False)
    assert int(dut.joined_count.value) == 0
    await _assert_quiescent(dut)
    await sender


@cocotb.test()
async def error_close_out_waits_for_backpressured_tail(dut):
    # Intra-batch error ordering under output backpressure (the emit-drain gate
    # for errors). Two hits are driven, then a torn w0 raises ERR_STREAM.
    # Accepting the second hit displaces the first into the emit engine, where
    # it stalls under backpressure; the torn w0 is then accepted (word-0 state
    # is unconditionally ready) and raises the error. The error close-out must
    # not handshake until that stalled beat drains -- otherwise the error status
    # overtakes a still-valid output beat, which the record writer would strand
    # into the next job once it consumes the status.
    await _start(dut)
    await _load(dut, [(7, 11, 12, 13), (63, 21, 22, 23)])
    _drive_cfg(dut, load=0, cfg=_CARRY_BUILD)

    dut.output_ready.value = 0
    dut.status_ready.value = 1
    # hit(7), hit(63), then a torn w0; all accepted while output is backpressured
    words = _pack_rows([(7, 1, 1, 1), (63, 2, 2, 2)]) + [(999 << 32) | 100]
    await _send_words(dut, words)

    # a joined beat is now stalled in the emit engine and ERR_STREAM has been
    # raised internally; the status must stay silent while output is un-drained
    for _ in range(15):
        await ReadOnly()
        assert int(dut.output_valid.value) == 1, "the joined tail is not in flight"
        assert int(dut.status_valid.value) == 0, (
            "ERR_STREAM close-out asserted while a prior joined beat is still un-drained -- intra-batch error overtake under backpressure"
        )
        await RisingEdge(dut.clk)

    dut.output_ready.value = 1
    await _expect_status(dut, error=True, code=JoinErrorCode.STREAM)
    assert int(dut.joined_count.value) == 0
    # recovers: reload the full build and probe normally (the drained stalled
    # beat did not leak into the next job)
    await _load(dut, _BUILD)
    assert await _probe(dut, _PROBE, _CARRY_BUILD) == broadcast_hash_join_rows(_BUILD, _PROBE, out_sel=_CARRY_BUILD, key_space=_KEY_SPACE)


@cocotb.test()
async def constrained_random_batches_match_golden(dut):
    await _start(dut)
    rng = random.Random(0x105D_1234)
    sources = list(JoinSource)
    for trial in range(30):
        keys = rng.sample(range(_KEY_SPACE), rng.randint(1, 12))
        build = [(k, rng.randint(-50, 50), rng.randint(-50, 50), rng.randint(-50, 50)) for k in keys]
        await _load(dut, build)
        probe = []
        for _ in range(rng.randint(1, 12)):
            if keys and rng.random() < 0.5:
                key = rng.choice(keys)  # bias toward hits
            else:
                key = rng.randrange(_KEY_SPACE)
            probe.append((key, rng.randint(-50, 50), rng.randint(-50, 50), rng.randint(-50, 50)))
        cfg = BroadcastJoinConfig(rng.choice(sources), rng.choice(sources), rng.choice(sources))
        pattern = tuple(rng.choice((0, 1)) for _ in range(rng.randint(1, 6)))
        if not any(pattern):
            pattern = pattern + (1,)
        out = await _probe(dut, probe, cfg, ready_pattern=pattern)
        expected = broadcast_hash_join_rows(build, probe, out_sel=cfg, key_space=_KEY_SPACE)
        assert out == expected, f"trial {trial}: keys={keys} cfg={cfg} probe={probe}"
        assert int(dut.joined_count.value) == len(expected)


# ---- helpers ----


def _signed32(value: int) -> int:
    value &= _MASK32
    return value - (1 << 32) if value >= (1 << 31) else value


def _drive_cfg(dut, *, load: int, cfg: BroadcastJoinConfig) -> None:
    dut.cfg_load.value = int(load)
    dut.cfg_out0_sel.value = int(cfg.out0)
    dut.cfg_out1_sel.value = int(cfg.out1)
    dut.cfg_out2_sel.value = int(cfg.out2)


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    _drive_cfg(dut, load=0, cfg=_CARRY_BUILD)
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


def _pack_rows(rows) -> list[int]:
    words = []
    for f0, f1, f2, f3 in rows:
        words.append(((f1 & _MASK32) << 32) | (f0 & _MASK32))
        words.append(((f3 & _MASK32) << 32) | (f2 & _MASK32))
    return words


async def _send_words(dut, words) -> None:
    for index, word in enumerate(words):
        dut.input_data.value = word & _MASK64
        dut.input_last.value = int(index == len(words) - 1)
        dut.input_valid.value = 1
        while True:
            await ReadOnly()
            fired = int(dut.input_ready.value) == 1
            await RisingEdge(dut.clk)
            if fired:
                break
    dut.input_valid.value = 0
    dut.input_last.value = 0


async def _send_rows(dut, rows) -> None:
    await _send_words(dut, _pack_rows(rows))


async def _send_words_flip_cfg(dut, words, *, flip_after: int, new_cfg: BroadcastJoinConfig) -> None:
    # drive words back-to-back, flipping the live field-select cfg after the
    # word at index flip_after handshakes (to prove the batch latch)
    for index, word in enumerate(words):
        dut.input_data.value = word & _MASK64
        dut.input_last.value = int(index == len(words) - 1)
        dut.input_valid.value = 1
        while True:
            await ReadOnly()
            fired = int(dut.input_ready.value) == 1
            await RisingEdge(dut.clk)
            if fired:
                break
        if index == flip_after:
            _drive_cfg(dut, load=0, cfg=new_cfg)
    dut.input_valid.value = 0
    dut.input_last.value = 0


async def _send_batches(dut, batches) -> None:
    # drive several back-to-back batches with input_last on the final word of
    # each, holding input_valid across the boundaries so the next batch follows
    # with no gap
    for words in batches:
        for index, word in enumerate(words):
            dut.input_data.value = word & _MASK64
            dut.input_last.value = int(index == len(words) - 1)
            dut.input_valid.value = 1
            while True:
                await ReadOnly()
                fired = int(dut.input_ready.value) == 1
                await RisingEdge(dut.clk)
                if fired:
                    break
    dut.input_valid.value = 0
    dut.input_last.value = 0


async def _drain_words(dut) -> None:
    dut.output_ready.value = 1
    while True:
        await RisingEdge(dut.clk)


async def _load(dut, build_rows) -> None:
    # input_last rides on a data word, so the smallest legal load is one build
    # row; an empty table is exercised by probing before any load (the table is
    # zero-initialised). Callers pass >= 1 build row.
    assert build_rows, "a load batch needs at least one build row (empty table = probe before load)"
    _drive_cfg(dut, load=1, cfg=_CARRY_BUILD)
    await _send_words(dut, _pack_rows(build_rows))
    await _expect_status(dut, error=False)
    await _assert_quiescent(dut)


async def _probe(dut, rows, cfg: BroadcastJoinConfig, *, ready_pattern=(1,)):
    _drive_cfg(dut, load=0, cfg=cfg)
    send = cocotb.start_soon(_send_rows(dut, rows))
    words = await _collect_probe_output(dut, ready_pattern)
    await send
    out = []
    for offset in range(0, len(words), 2):
        w0, w1 = words[offset], words[offset + 1]
        out.append((_signed32(w0 & _MASK32), _signed32(w0 >> 32), _signed32(w1 & _MASK32), _signed32(w1 >> 32)))
    return out


async def _collect_probe_output(dut, ready_pattern) -> list[int]:
    # collect emitted row words until output_last, then confirm the tile stays
    # silent (rows emitted -> the downstream terminal owns the status); when
    # zero rows emit, the success close-out arrives with no output beats.
    words: list[int] = []
    pattern_index = 0
    held = None
    for _ in range(200000):
        ready = ready_pattern[pattern_index % len(ready_pattern)]
        pattern_index += 1
        dut.output_ready.value = ready
        dut.status_ready.value = 1  # let a zero-row success close-out through
        await ReadOnly()
        valid = int(dut.output_valid.value) == 1
        status = int(dut.status_valid.value) == 1
        fired = ready == 1 and valid
        word = int(dut.output_data.value)
        last = int(dut.output_last.value)
        if held is not None:
            assert valid, "output_valid dropped while a beat was stalled"
            assert (word, last) == held, "output beat changed while stalled"
        if not words and status and not valid:
            # zero emitted rows: the success close-out arrived first
            await RisingEdge(dut.clk)
            dut.output_ready.value = 0
            dut.status_ready.value = 0
            await _assert_quiescent(dut)
            return words
        held = None if (fired or not valid) else (word, last)
        await RisingEdge(dut.clk)
        if fired:
            words.append(word)
            if last == 1:
                dut.output_ready.value = 0
                # rows emitted: the join stays silent (the downstream terminal's
                # status is authoritative)
                await _assert_quiescent(dut)
                return words
    raise AssertionError(f"timed out collecting join output ({len(words)} words)")


async def _assert_quiescent(dut) -> None:
    dut.output_ready.value = 1
    dut.status_ready.value = 1
    for _ in range(6):
        await ReadOnly()
        assert int(dut.output_valid.value) == 0, "output beat after batch completion"
        assert int(dut.status_valid.value) == 0, "status beat after batch completion"
        await RisingEdge(dut.clk)
    dut.output_ready.value = 0
    dut.status_ready.value = 0


async def _expect_status(dut, *, error: bool, code: int = 0) -> None:
    dut.status_ready.value = 1
    for _ in range(1 << 20):
        await ReadOnly()
        fired = int(dut.status_valid.value) == 1
        observed_error = int(dut.status_error.value)
        observed_code = int(dut.status_error_code.value)
        await RisingEdge(dut.clk)
        if fired:
            assert observed_error == int(error)
            if error:
                assert observed_code == code, f"status code {observed_code} != {code}"
            else:
                assert observed_code == 0, f"success close-out carried code {observed_code}"
            dut.status_ready.value = 0
            return
    raise AssertionError("timed out waiting for join status")
