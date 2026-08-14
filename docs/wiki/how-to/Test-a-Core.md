# How to test a core

This guide shows you how to prove a tile means what its registry entry
says. It assumes the tile already has a golden, HDL, and an entry — see
[how to add an operator tile](Add-an-Operator-Tile) for the order those
arrive in.

Nothing here needs Vivado. The bench steps need `verilator` on `PATH` and
the develop extras, which bring in the cocotb integration:

```bash
pip install -e .[develop]
```

The layers below are ordered by what they can catch. Run them in order:
each one is cheap relative to the next and rules out a class of failure the
next one would otherwise report confusingly.

## 1. Prove the golden against itself

The golden is the definition of the tile, so it cannot be checked against
the hardware — the hardware is checked against *it*. Check it against
properties instead, in `dau_core/tests/test_<name>.py`:

- permutation invariance where order should not matter;
- agreement with an existing golden wherever the two overlap;
- boundary values, including the int32 wire identity (`-1` and
  `0xFFFFFFFF` are the same key);
- the algebra, if the tile has a host-side counterpart —
  `merge(split(rows)) == one_shot(rows)`.

```bash
python -m pytest dau_core/tests/test_<name>.py -q
```

## 2. Prove the HDL equal to the golden

The cocotb bench in `dau_core/tests/cocotb_benches/dau_<name>_tb.py`
drives the DUT and compares against the golden — never against hand-written
expected values. Build the tile's config object once and pass the same
object to both sides, so a config misreading cannot cancel out:

```python
predicate = FilterPredicate(op0=PredicateOp.GT, constant0=4)
kept, count = await _run_filter(dut, _VALUES, predicate, ready_pattern=(1, 0, 0, 1, 1, 0))
assert kept == filter_values(_VALUES, predicate)
```

Cover the nominal case, backpressure (a stalled beat stays asserted and
byte-stable), reset, repeated batches back to back, malformed input, and
every error path — asserting `status_error_code` against the golden's
`IntEnum` value rather than a literal.

Wire the bench into `dau_core/tests/test_hdl_cocotb.py` with one runner per
parameter value, then:

```bash
python -m pytest dau_core/tests/test_hdl_cocotb.py -q -k <name>
```

For a parameterized tile the runner sets two coupled channels from the same
pytest parameter: `parameters={"SLOTS": slots}` tells Verilator how to
elaborate, and an environment variable tells the bench how to shape its
stimulus. Set both, or the bench will exercise the default shape against a
differently elaborated DUT.

## 3. Prove the bench can fail

A bench that compares nothing passes forever. Add your tile's failure mode
to the mutation list in `dau_core/tests/test_benches_can_fail.py`: each
entry edits a copy of the HDL, runs the bench, and asserts it fails — and
that it failed in the *run* rather than at compile time.

```bash
python -m pytest dau_core/tests/test_benches_can_fail.py -q
```

## 4. Prove the declared ingest floor

`ingest` is public interface: a scheduler rate-matches a core it cannot
inspect from that declaration alone. Assert it in the bench rather than
trusting it, using the rate probe:

```python
from .rate_probe import assert_sustained_ingest

await assert_sustained_ingest(dut, "<name>", send=send, input_beats=256)
```

It reads the floor out of the registry, holds every sink ready, forbids
input-valid gaps, and fails when the measured rate falls more than one
percent short — with a message naming both remedies, lowering the
declaration or fixing the RTL. It measures *input acceptance*, ending at
the last accepted beat, so a tile that buffers a whole batch and works
afterwards needs a measurement spanning its drain instead.

## 5. Prove the declared parameter space elaborates

The registry advertises a parameter's bounds; the RTL enforces them in a
generate-scope `$error`. Two guards keep those from drifting, and both are
already parametrized over the palette, so a new tile is covered as soon as
it is registered:

```bash
python -m pytest dau_core/tests/test_hdl_elaboration.py -q -k <name>
python -m pytest dau_core/tests/test_parameter_bounds_match_the_hdl.py -q
```

The second one *parses* the `$error` text, so write the message in one of
the recognised forms — `X must be in [a, b]`, `X must be between a and b`,
`X must divide N and lie in [a, b]`, `X must be a/b/c`, or
`X must be a, b, or c`. A message it cannot parse has to be added to the
holdout list, and that list is itself asserted in both directions.

## 6. Run the registry-wide guards

These walk the whole registry rather than your tile, and they are what
catches a declaration that is well-formed but wrong — an entry with no
envelope, a golden that does not import, a source file nothing claims.

```bash
python -m pytest dau_core/tests/test_cores.py -q
```

See the [registry guard reference](Registry-Guards) for what each one
refuses. Four of them compare exact sets rather than predicates: if your
tile legitimately belongs in one — a new silent-on-success stage, say —
adding it to the pinned set is part of the change, and the diff is the
place where that intent gets reviewed.

## 7. Run everything

```bash
python -m pytest dau_core -q
python -m ruff check dau_core && python -m ruff format --check dau_core
```

## Next

- [Measure a resource envelope](Measure-a-Resource-Envelope) — the one
  step that needs a synthesis host.
- [Registry-wide guards](Registry-Guards) — the full list, and what each
  refuses.
- [Why a golden model rather than a specification](Why-Goldens-Define-Meaning) —
  why step 1 comes first.
