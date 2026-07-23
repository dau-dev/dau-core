from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.temporal import MarketFeatures, asof_backward, market_features, matched_rows

# (symbol, time, bid, ask)
_QUOTES = [
    (570, 100, 10, 12),
    (570, 105, 11, 13),
    (570, 120, 9, 11),
    (571, 100, 50, 52),
    (571, 110, 51, 54),
]
# (symbol, time, price, size)
_TRADES = [
    (570, 99, 11, 5),  # pre-first-quote miss
    (570, 100, 12, 3),
    (570, 107, 13, 2),
    (570, 130, 8, 1),
    (571, 105, 53, 7),
    (571, 111, 55, 4),
    (572, 100, 1, 1),  # no quotes: miss
]


def _merge(quotes, trades):
    tagged = [(0, s, t, bid, ask) for (s, t, bid, ask) in quotes]
    tagged += [(1, s, t, price, size) for (s, t, price, size) in trades]
    tagged.sort(key=lambda r: (r[1], r[2], r[0]))
    return tagged


def _expected(quotes, trades, *, tolerance=None):
    st = sorted(trades, key=lambda r: (r[0], r[1]))
    sq = sorted(quotes, key=lambda r: (r[0], r[1]))
    return market_features(matched_rows(asof_backward(st, sq, tolerance=tolerance)))


@cocotb.test()
async def chain_matches_align_then_derive_golden(dut):
    await _start(dut)
    feats = await _run(dut, _merge(_QUOTES, _TRADES))
    assert feats == _expected(_QUOTES, _TRADES)


@cocotb.test()
async def chain_backpressure_both_sides(dut):
    await _start(dut)
    feats = await _run(dut, _merge(_QUOTES, _TRADES), ready_pattern=(1, 0, 0, 1, 0))
    assert feats == _expected(_QUOTES, _TRADES)


@cocotb.test()
async def chain_with_tolerance(dut):
    await _start(dut)
    tol = 10
    feats = await _run(dut, _merge(_QUOTES, _TRADES), tolerance=tol)
    assert feats == _expected(_QUOTES, _TRADES, tolerance=tol)


@cocotb.test()
async def chain_exposes_the_match_count_at_terminal_status(dut):
    await _start(dut)
    feats = await _run(dut, _merge(_QUOTES, _TRADES))
    # the join latches the as-of match count so it survives to the chain's
    # terminal status (the tile clears its own live counter on reset)
    assert int(dut.match_count.value) == len(feats)


@cocotb.test()
async def chain_all_miss_still_closes_out(dut):
    await _start(dut)
    # a stream with no qualifying quote for any trade: the as-of emits zero
    # market rows and no output_last, so the join must synthesize the terminal
    # success close-out (the feature tile would otherwise never close)
    trades = [(9, 5, 1, 1), (9, 6, 2, 2)]
    feats = await _run(dut, _merge([], trades))
    assert feats == []
    assert market_features(matched_rows(asof_backward(trades, []))) == []
    assert int(dut.match_count.value) == 0


@cocotb.test()
async def chain_propagates_an_asof_error(dut):
    await _start(dut)
    dut.output_ready.value = 1
    # input_last on w0 of an as-of record: a torn record -> as-of ERR_STREAM,
    # which the join must surface as the chain's error close-out
    dut.input_data.value = 570
    dut.input_last.value = 1
    dut.input_valid.value = 1
    while True:
        await ReadOnly()
        fired = int(dut.input_ready.value) == 1
        await RisingEdge(dut.clk)
        if fired:
            break
    dut.input_valid.value = 0
    dut.input_last.value = 0
    dut.status_ready.value = 1
    for _ in range(64):
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 1
            assert int(dut.status_error_code.value) == 2  # ERR_STREAM
            return
        await RisingEdge(dut.clk)
    raise AssertionError("chain did not surface the as-of error")


@cocotb.test()
async def chain_propagates_an_error_after_prior_matches(dut):
    await _start(dut)
    dut.output_ready.value = 1
    # a full quote+trade (one matched market row, feature output in flight), then
    # a torn as-of record -> error. The join must surface the error close-out and
    # the abort must flush the feature so nothing is stranded / hangs.
    records = [(0, 570, 100, 10, 12), (1, 570, 100, 12, 3)]  # quote, matching trade
    send_words = []
    for is_trade, symbol, time, a, b in records:
        send_words.append(((is_trade & 0xFFFFFFFF) << 32) | (symbol & 0xFFFFFFFF))
        send_words.append(time & ((1 << 64) - 1))
        send_words.append(((b & 0xFFFFFFFF) << 32) | (a & 0xFFFFFFFF))
    send_words.append(571)  # a torn record: w0 with input_last -> ERR_STREAM

    for index, word in enumerate(send_words):
        dut.input_data.value = word
        dut.input_last.value = int(index == len(send_words) - 1)
        dut.input_valid.value = 1
        while True:
            await ReadOnly()
            fired = int(dut.input_ready.value) == 1
            await RisingEdge(dut.clk)
            if fired:
                break
    dut.input_valid.value = 0
    dut.input_last.value = 0
    dut.status_ready.value = 1
    for _ in range(128):
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 1
            assert int(dut.status_error_code.value) == 2
            return
        await RisingEdge(dut.clk)
    raise AssertionError("chain did not surface the error after prior matches")


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    dut.cfg_has_tolerance.value = 0
    dut.cfg_tolerance.value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _run(dut, records, *, ready_pattern=(1,), tolerance=None):
    dut.cfg_has_tolerance.value = int(tolerance is not None)
    dut.cfg_tolerance.value = (tolerance if tolerance is not None else 0) & ((1 << 64) - 1)
    send = cocotb.start_soon(_send_records(dut, records))
    words = await _collect_output(dut, ready_pattern)
    await send
    count = await _accept_success_status(dut)
    assert len(words) % 7 == 0, "feature records are 7 words"
    assert count == len(words) // 7
    return [_decode(words[offset : offset + 7]) for offset in range(0, len(words), 7)]


def _decode(words) -> MarketFeatures:
    return MarketFeatures(
        symbol_id=_s32(words[1] & 0xFFFFFFFF),
        time=_s64(words[0]),
        price=_s32(words[1] >> 32),
        size=_s32(words[3] & 0xFFFFFFFF),
        midpoint=_s32(words[2] & 0xFFFFFFFF),
        spread=_s64(words[4]),
        trade_sign=_s32(words[2] >> 32),
        signed_volume=_s64(words[5]),
        dollar_volume=_s64(words[6]),
    )


async def _send_records(dut, records) -> None:
    words = []
    for is_trade, symbol, time, a, b in records:
        words.append(((is_trade & 0xFFFFFFFF) << 32) | (symbol & 0xFFFFFFFF))
        words.append(time & ((1 << 64) - 1))
        words.append(((b & 0xFFFFFFFF) << 32) | (a & 0xFFFFFFFF))
    for index, word in enumerate(words):
        dut.input_data.value = word
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


async def _collect_output(dut, ready_pattern):
    words = []
    pattern_index = 0
    for _ in range(16384):
        ready = ready_pattern[pattern_index % len(ready_pattern)]
        pattern_index += 1
        dut.output_ready.value = ready
        await ReadOnly()
        fired = ready == 1 and int(dut.output_valid.value) == 1
        word = int(dut.output_data.value)
        last = int(dut.output_last.value)
        status_pending = int(dut.status_valid.value) == 1
        await RisingEdge(dut.clk)
        if fired:
            words.append(word)
            if last == 1:
                dut.output_ready.value = 0
                return words
        elif status_pending:
            dut.output_ready.value = 0
            return words
    raise AssertionError("timed out collecting chain output")


async def _accept_success_status(dut) -> int:
    dut.status_ready.value = 1
    for _ in range(128):
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 0
            count = int(dut.feature_count.value)
            await RisingEdge(dut.clk)
            dut.status_ready.value = 0
            return count
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for success status")


def _s32(value: int) -> int:
    return value - (1 << 32) if value >= 1 << 31 else value


def _s64(value: int) -> int:
    return value - (1 << 64) if value >= 1 << 63 else value
