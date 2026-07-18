from __future__ import annotations

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import (
    MembershipBuildSet,
    MembershipErrorCode,
    MembershipKind,
    MembershipMode,
    filter_rows_by_membership,
)

_MASK32 = 0xFFFF_FFFF
_MASK64 = 0xFFFF_FFFF_FFFF_FFFF
_INT32_MIN = -(1 << 31)
_INT32_MAX = (1 << 31) - 1

# matches the DUT parameter default
_KEY_SPACE = 4096

# (key, value, aux0, aux1) — the membership key is w0.lo (the row key)
_ROWS = [
    (7, 1000, 5, 10),
    (100, 2000, 6, 30),
    (7, 1500, 7, 23),
    (4095, -500, 6, 24),
    (63, 3000, 4, 1),
    (200, 2500, 6, 40),
]


def _build_set(keys) -> MembershipBuildSet:
    return MembershipBuildSet.from_keys(keys, key_space=_KEY_SPACE)


@cocotb.test()
async def semi_keeps_members_anti_keeps_non_members(dut):
    await _start(dut)
    build = _build_set({7, 63, 4095})
    await _load(dut, build)
    for mode in (MembershipMode.SEMI, MembershipMode.ANTI):
        out = await _probe(dut, _ROWS, mode)
        assert out == filter_rows_by_membership(_ROWS, build, mode=mode), mode.name


@cocotb.test()
async def semi_keeps_exactly_the_hits(dut):
    await _start(dut)
    build = _build_set({7, 63, 4095})
    await _load(dut, build)
    out = await _probe(dut, _ROWS, MembershipMode.SEMI)
    assert out == [(7, 1000, 5, 10), (7, 1500, 7, 23), (4095, -500, 6, 24), (63, 3000, 4, 1)]


@cocotb.test()
async def anti_keeps_exactly_the_misses(dut):
    await _start(dut)
    build = _build_set({7, 63, 4095})
    await _load(dut, build)
    out = await _probe(dut, _ROWS, MembershipMode.ANTI)
    assert out == [(100, 2000, 6, 30), (200, 2500, 6, 40)]


@cocotb.test()
async def empty_build_set(dut):
    await _start(dut)
    build = _build_set(set())
    await _load(dut, build)
    assert await _probe(dut, _ROWS, MembershipMode.SEMI) == []
    assert await _probe(dut, _ROWS, MembershipMode.ANTI) == _ROWS


@cocotb.test()
async def full_build_set(dut):
    # every in-range id is a member: semi keeps in-range keys, anti keeps only
    # the out-of-range keys (there are none in _ROWS, all keys are in range)
    await _start(dut)
    build = _build_set(range(_KEY_SPACE))
    await _load(dut, build)
    assert await _probe(dut, _ROWS, MembershipMode.SEMI) == _ROWS
    assert await _probe(dut, _ROWS, MembershipMode.ANTI) == []


@cocotb.test()
async def boundary_and_out_of_range_keys(dut):
    await _start(dut)
    build = _build_set({0, _KEY_SPACE - 1})
    await _load(dut, build)
    rows = [
        (0, 1, 1, 1),  # member (low boundary)
        (_KEY_SPACE - 1, 2, 2, 2),  # member (high boundary)
        (_KEY_SPACE, 3, 3, 3),  # out of range -> non-member
        (-1, 4, 4, 4),  # negative -> non-member
        (1, 5, 5, 5),  # in range, not set -> non-member
    ]
    assert await _probe(dut, rows, MembershipMode.SEMI) == filter_rows_by_membership(rows, build, mode=MembershipMode.SEMI)
    assert await _probe(dut, rows, MembershipMode.ANTI) == filter_rows_by_membership(rows, build, mode=MembershipMode.ANTI)


@cocotb.test()
async def a_second_load_replaces_the_membership_memory(dut):
    await _start(dut)
    await _load(dut, _build_set({7, 63}))
    assert await _probe(dut, _ROWS, MembershipMode.SEMI) == [(7, 1000, 5, 10), (7, 1500, 7, 23), (63, 3000, 4, 1)]
    # a fresh image overwrites every word — the old members are gone
    await _load(dut, _build_set({100, 200}))
    assert await _probe(dut, _ROWS, MembershipMode.SEMI) == [(100, 2000, 6, 30), (200, 2500, 6, 40)]


@cocotb.test()
async def int32_extreme_probe_keys_are_non_members(dut):
    await _start(dut)
    build = _build_set({0})
    await _load(dut, build)
    rows = [(_INT32_MIN, 1, 1, 1), (_INT32_MAX, 2, 2, 2), (0, 3, 3, 3), (-1, 4, 4, 4)]
    for mode in (MembershipMode.SEMI, MembershipMode.ANTI):
        assert await _probe(dut, rows, mode) == filter_rows_by_membership(rows, build, mode=mode), mode.name


@cocotb.test()
async def last_lands_on_final_kept_row(dut):
    # trailing rows drop: last must move onto the final forwarded word
    await _start(dut)
    await _load(dut, _build_set({7}))
    rows = [(7, 1, 1, 1), (100, 2, 2, 2), (200, 3, 3, 3)]
    assert await _probe(dut, rows, MembershipMode.SEMI) == [(7, 1, 1, 1)]


@cocotb.test()
async def zero_rows_closes_out_success(dut):
    await _start(dut)
    await _load(dut, _build_set({999}))
    assert await _probe(dut, _ROWS, MembershipMode.SEMI) == []
    assert int(dut.selected_count.value) == 0


@cocotb.test()
async def selected_count_reports_forwarded_rows(dut):
    await _start(dut)
    build = _build_set({7, 63, 4095})
    await _load(dut, build)
    out = await _probe(dut, _ROWS, MembershipMode.SEMI)
    assert int(dut.selected_count.value) == len(out) == 4
    # a load leaves selected_count at zero
    await _load(dut, build)
    assert int(dut.selected_count.value) == 0


@cocotb.test()
async def backpressure_on_the_output(dut):
    await _start(dut)
    build = _build_set({7, 63, 4095})
    await _load(dut, build)
    out = await _probe(dut, _ROWS, MembershipMode.SEMI, ready_pattern=(1, 0, 0, 1, 0))
    assert out == filter_rows_by_membership(_ROWS, build, mode=MembershipMode.SEMI)


@cocotb.test()
async def torn_probe_row_reports_stream_error(dut):
    await _start(dut)
    await _load(dut, _build_set({7}))
    _drive_cfg(dut, load=0, mode=MembershipMode.SEMI, kind=MembershipKind.EXACT_BITMAP)
    await _send_words(dut, [(100 << 32) | 7])  # input_last on a w0
    await _expect_status(dut, error=True, code=MembershipErrorCode.STREAM)
    assert int(dut.selected_count.value) == 0
    # recovers: the next probe filters normally
    assert await _probe(dut, _ROWS, MembershipMode.SEMI) == [(7, 1000, 5, 10), (7, 1500, 7, 23)]


@cocotb.test()
async def torn_row_mid_batch_reports_stream_error(dut):
    await _start(dut)
    build = _build_set({7, 63, 4095})
    await _load(dut, build)
    _drive_cfg(dut, load=0, mode=MembershipMode.SEMI, kind=MembershipKind.EXACT_BITMAP)
    send = cocotb.start_soon(_send_words(dut, _pack_rows(_ROWS[:2]) + [(999 << 32) | 999]))
    collect = cocotb.start_soon(_drain_words(dut))
    await _expect_status(dut, error=True, code=MembershipErrorCode.STREAM)
    await send
    collect.kill()
    dut.output_ready.value = 0
    assert int(dut.selected_count.value) == 0
    assert await _probe(dut, _ROWS, MembershipMode.SEMI) == filter_rows_by_membership(_ROWS, build, mode=MembershipMode.SEMI)


@cocotb.test()
async def unconfigured_kind_drains_batch_then_reports_config_error(dut):
    await _start(dut)
    await _load(dut, _build_set({7}))
    _drive_cfg(dut, load=0, mode=MembershipMode.SEMI, kind=MembershipKind.EXACT_BITMAP)
    dut.cfg_kind.value = 0  # reserved: an unconfigured tile fails explicitly
    send = cocotb.start_soon(_send_rows(dut, _ROWS))
    await _expect_status(dut, error=True, code=MembershipErrorCode.CONFIG)
    await send
    assert await _probe(dut, _ROWS, MembershipMode.SEMI) == [(7, 1000, 5, 10), (7, 1500, 7, 23)]


@cocotb.test()
async def bloom_kind_is_rejected_as_config_error(dut):
    # bloom is a reserved future config; v0 rejects it
    await _start(dut)
    await _load(dut, _build_set({7}))
    _drive_cfg(dut, load=0, mode=MembershipMode.SEMI, kind=MembershipKind.BLOOM)
    send = cocotb.start_soon(_send_rows(dut, _ROWS))
    await _expect_status(dut, error=True, code=MembershipErrorCode.CONFIG)
    await send
    assert await _probe(dut, _ROWS, MembershipMode.SEMI) == [(7, 1000, 5, 10), (7, 1500, 7, 23)]


@cocotb.test()
async def short_image_reports_load_error(dut):
    await _start(dut)
    _drive_cfg(dut, load=1, mode=MembershipMode.SEMI, kind=MembershipKind.EXACT_BITMAP)
    # only half the image words, then input_last: too short
    await _send_words(dut, _build_set({7}).image_words()[: _KEY_SPACE // 128])
    await _expect_status(dut, error=True, code=MembershipErrorCode.LOAD)
    # recovers: a well-formed load then probe works
    await _load(dut, _build_set({7}))
    assert await _probe(dut, _ROWS, MembershipMode.SEMI) == [(7, 1000, 5, 10), (7, 1500, 7, 23)]


@cocotb.test()
async def over_long_image_reports_load_error(dut):
    await _start(dut)
    _drive_cfg(dut, load=1, mode=MembershipMode.SEMI, kind=MembershipKind.EXACT_BITMAP)
    # a full image plus one extra word carrying input_last: too long
    await _send_words(dut, _build_set({7}).image_words() + [0])
    await _expect_status(dut, error=True, code=MembershipErrorCode.LOAD)
    await _load(dut, _build_set({7}))
    assert await _probe(dut, _ROWS, MembershipMode.SEMI) == [(7, 1000, 5, 10), (7, 1500, 7, 23)]


@cocotb.test()
async def error_status_survives_backpressure(dut):
    # while status_ready is held low the error close-out must stay stable and
    # the filter must refuse new input; recovery only after the handshake
    await _start(dut)
    await _load(dut, _build_set({7}))
    _drive_cfg(dut, load=0, mode=MembershipMode.SEMI, kind=MembershipKind.EXACT_BITMAP)
    dut.status_ready.value = 0
    await _send_words(dut, [(100 << 32) | 7])  # torn w0

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
        assert int(dut.status_error_code.value) == MembershipErrorCode.STREAM
        assert int(dut.input_ready.value) == 0, "filter accepted input while status pending"
        await RisingEdge(dut.clk)

    await _expect_status(dut, error=True, code=MembershipErrorCode.STREAM)
    assert await _probe(dut, _ROWS, MembershipMode.SEMI) == [(7, 1000, 5, 10), (7, 1500, 7, 23)]


@cocotb.test()
async def success_status_survives_backpressure(dut):
    # the per-batch success close-out must hold stable while status_ready is low
    # and the filter must refuse new input until the handshake completes
    await _start(dut)
    await _load(dut, _build_set({999}))
    _drive_cfg(dut, load=0, mode=MembershipMode.SEMI, kind=MembershipKind.EXACT_BITMAP)
    dut.status_ready.value = 0
    await _send_rows(dut, _ROWS)  # zero forwarded rows -> success close-out

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
        assert int(dut.input_ready.value) == 0, "filter accepted input while status pending"
        await RisingEdge(dut.clk)

    await _expect_status(dut, error=False)
    assert await _probe(dut, _ROWS, MembershipMode.ANTI) == _ROWS


@cocotb.test()
async def constrained_random_batches_match_golden(dut):
    await _start(dut)
    rng = random.Random(0x3E37B17)
    for trial in range(40):
        # a fresh random build set, reloaded each trial
        members = {rng.randrange(_KEY_SPACE) for _ in range(rng.randint(0, 20))}
        build = _build_set(members)
        await _load(dut, build)
        rows = []
        for _ in range(rng.randint(1, 12)):
            if rng.random() < 0.5 and members:
                key = rng.choice(sorted(members))  # bias toward hits
            elif rng.random() < 0.15:
                key = rng.choice((_INT32_MIN, _INT32_MAX, -1, _KEY_SPACE, 0, _KEY_SPACE - 1))
            else:
                key = rng.randrange(_KEY_SPACE)
            rows.append((key, rng.randint(-40, 40), rng.randint(-40, 40), rng.randint(-40, 40)))
        mode = rng.choice((MembershipMode.SEMI, MembershipMode.ANTI))
        pattern = tuple(rng.choice((0, 1)) for _ in range(rng.randint(1, 6)))
        if not any(pattern):
            pattern = pattern + (1,)
        out = await _probe(dut, rows, mode, ready_pattern=pattern)
        expected = filter_rows_by_membership(rows, build, mode=mode)
        assert out == expected, f"trial {trial}: members={sorted(members)} mode={mode.name} rows={rows}"
        assert int(dut.selected_count.value) == len(expected)


# ---- helpers ----


def _signed32(value: int) -> int:
    value &= _MASK32
    return value - (1 << 32) if value >= (1 << 31) else value


def _drive_cfg(dut, *, load: int, mode: MembershipMode, kind: MembershipKind) -> None:
    dut.cfg_load.value = int(load)
    dut.cfg_mode.value = int(mode)
    dut.cfg_kind.value = int(kind)


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    _drive_cfg(dut, load=0, mode=MembershipMode.SEMI, kind=MembershipKind.EXACT_BITMAP)
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
    for key, value, aux0, aux1 in rows:
        words.append(((value & _MASK32) << 32) | (key & _MASK32))
        words.append(((aux1 & _MASK32) << 32) | (aux0 & _MASK32))
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


async def _drain_words(dut) -> None:
    dut.output_ready.value = 1
    while True:
        await RisingEdge(dut.clk)


async def _load(dut, build_set: MembershipBuildSet) -> None:
    _drive_cfg(dut, load=1, mode=MembershipMode.SEMI, kind=MembershipKind.EXACT_BITMAP)
    await _send_words(dut, build_set.image_words())
    await _expect_status(dut, error=False)
    await _assert_quiescent(dut)


async def _probe(dut, rows, mode: MembershipMode, *, ready_pattern=(1,)):
    _drive_cfg(dut, load=0, mode=mode, kind=MembershipKind.EXACT_BITMAP)
    send = cocotb.start_soon(_send_rows(dut, rows))
    words = await _collect_probe_output(dut, ready_pattern)
    await send
    out = []
    for offset in range(0, len(words), 2):
        w0, w1 = words[offset], words[offset + 1]
        out.append((_signed32(w0 & _MASK32), _signed32(w0 >> 32), _signed32(w1 & _MASK32), _signed32(w1 >> 32)))
    return out


async def _collect_probe_output(dut, ready_pattern) -> list[int]:
    # collect forwarded row words until output_last, then consume the per-batch
    # success close-out; when zero rows forward the success close-out arrives
    # with no output beats.
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
            # zero forwarded rows: the success close-out arrived first
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
                # rows forwarded: the probe filter stays silent (the downstream
                # terminal's status is authoritative)
                await _assert_quiescent(dut)
                return words
    raise AssertionError(f"timed out collecting membership output ({len(words)} words)")


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
    raise AssertionError("timed out waiting for membership status")
