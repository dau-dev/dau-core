"""A tile's declared record shape must be able to hold the beat its RTL emits.

`datetime-format` declared `output_record_words: 1` -- 64 bits -- while its
`output_data` port is 152 bits wide. Its description said so in prose, in
capitals, for the tile's whole life. Prose is not what the composition layer
reads: `check_record_width_compatibility` compares the declared word counts,
and it is detection-only, so a 152-bit beat wired into a one-word consumer
would have dropped 11 of the 19 characters with nothing raised.

The guard is deliberately narrow. `output_record_words` counts words in a
logical RECORD, not bits in a beat, and 35 of the 36 literal-width tiles
declare MORE words than one beat holds -- `int32-rolling-moments` declares 3
on a 64-bit port and streams them as three consecutive beats. That is the
convention and it is fine. The only thing that is never fine is a record
declared too small to hold a single beat of itself, which is unsatisfiable
under any beat count.

Parameterized widths (`OUT_WIDTH-1`, `SLOTS*128-1`) are skipped: their width
is not known until elaboration, so there is nothing to compare here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from dau_core.registry import loaded_cores

HDL = Path(__file__).resolve().parent.parent / "hdl"

_PORT = re.compile(r"output\s+(?:logic\s+|wire\s+)?(?:\[\s*([^\]]+?)\s*:\s*0\s*\]\s+)?output_data\b")


def _literal_bits(source: str) -> int | None:
    """Width of `output_data` when it is a constant, else None."""
    match = _PORT.search(source)
    if match is None:
        return None
    span = (match.group(1) or "").strip()
    if not span:
        return 1
    minus_one = re.fullmatch(r"(\d+)\s*-\s*1", span)
    if minus_one:
        return int(minus_one.group(1))
    return int(span) + 1 if span.isdigit() else None


def _declared_words(definition) -> int | None:
    try:
        return definition.effective_output_record_words({})
    except ValueError:
        return None  # cores with no input shape derive nothing to compare


def cores_with_literal_output_ports():
    for name, definition in sorted(loaded_cores().items()):
        source = getattr(definition, "source", None)
        if not source or not (HDL / source).exists():
            continue
        bits = _literal_bits((HDL / source).read_text())
        words = _declared_words(definition)
        if bits is not None and words is not None:
            yield name, bits, words


@pytest.mark.parametrize("name,bits,words", list(cores_with_literal_output_ports()))
def test_a_record_is_wide_enough_for_one_beat_of_itself(name: str, bits: int, words: int) -> None:
    assert words * 64 >= bits, (
        f"{name}: output_data is {bits} bits but output_record_words={words} declares only "
        f"{words * 64}. A record cannot be narrower than one beat of itself, and the composition "
        f"width check is detection-only -- it would report a mismatch and wire it up anyway."
    )


def test_the_guard_actually_inspects_the_rtl() -> None:
    """A parse that silently matched nothing would make every case above vacuous."""
    checked = list(cores_with_literal_output_ports())
    assert len(checked) > 30, f"only {len(checked)} tiles parsed; the port regex has stopped matching"
    widths = {bits for _, bits, _ in checked}
    assert widths == {64, 152}, f"unexpected literal port widths {sorted(widths)} -- re-read the convention"


def test_datetime_format_is_the_one_tile_that_widens_its_port() -> None:
    """The deviation is real and load-bearing, so it is pinned rather than left
    to be rediscovered: every other tile keeps the row-stream contract's 64-bit
    port and takes more beats. This one widened the port instead, which is why
    nothing in the palette can consume it without an adapter."""
    wide = [name for name, bits, _ in cores_with_literal_output_ports() if bits != 64]
    assert wide == ["datetime-format"], f"a second tile now widens its port: {wide}"
