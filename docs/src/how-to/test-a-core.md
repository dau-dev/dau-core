# How to test a core

This guide shows you how to check that a tile does what its registry entry
says. It assumes the tile already has a golden, HDL, and an entry; see
[how to add an operator tile](add-an-operator-tile.md) for the order those
come in.

Nothing here needs Vivado. The bench steps need `verilator` on `PATH` and
the develop extras, which bring in the cocotb integration:

```bash
pip install -e .[develop]
```

Run the steps below in order. Each is cheaper than the next and rules out
a kind of failure the next one would report in a more confusing way.

## 1. Prove the golden against itself

The golden defines the tile, so it cannot be checked against the hardware;
the hardware is checked against it. Test the golden against properties
instead, in `dau_core/tests/test_<name>.py`:

- permutation invariance where order should not matter;
- agreement with an existing golden wherever the two overlap;
- boundary values, including the int32 wire identity (`-1` and
  `0xFFFFFFFF` are the same key);
- the algebra, if the tile has a host-side counterpart:
  `merge(split(rows)) == one_shot(rows)`.

```bash
python -m pytest dau_core/tests/test_<name>.py -q
```

## 2. Prove the HDL equal to the golden

The cocotb bench in `dau_core/tests/cocotb_benches/dau_<name>_tb.py`
drives the DUT and compares its output with the golden's, using no
hand-written expected values. Build the tile's config object once and pass the same
object to both sides, so a misread config cannot cancel itself out:

<!-- excerpt -->

```python
predicate = FilterPredicate(op0=PredicateOp.GT, constant0=4)
kept, count = await _run_filter(dut, _VALUES, predicate, ready_pattern=(1, 0, 0, 1, 1, 0))
assert kept == filter_values(_VALUES, predicate)
```

Cover the nominal case, backpressure (a stalled beat stays asserted and
byte-stable), reset, repeated batches back to back, malformed input, and
every error path, asserting `status_error_code` against the golden's
`IntEnum` value instead of a literal.

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

A bench that compares nothing always passes. Add your tile's failure mode
to the mutation list in `dau_core/tests/test_benches_can_fail.py`. Each
entry edits a copy of the HDL, runs the bench, and asserts that it fails,
and that it failed while running rather than at compile time.

```bash
python -m pytest dau_core/tests/test_benches_can_fail.py -q
```

## 4. Prove the declared ingest floor

`ingest` is part of the public interface, because a scheduler matches
rates using only that declaration. Check it in the bench with the rate
probe instead of trusting it:

<!-- excerpt -->

```python
from .rate_probe import assert_sustained_ingest

await assert_sustained_ingest(dut, "<name>", send=send, input_beats=256)
```

It reads the declared rate from the registry, holds every sink ready,
forbids gaps in input valid, and fails when the measured rate is more than
one percent short, with a message suggesting either lowering the
declaration or fixing the RTL. It measures *input acceptance*, ending at the
last accepted beat, so a tile that buffers a whole batch and does its work
afterwards needs a measurement that covers its drain instead.

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
the recognized forms: `X must be in [a, b]`, `X must be between a and b`,
`X must divide N and lie in [a, b]`, `X must be a/b/c`, or
`X must be a, b, or c`. A message it cannot parse has to be added to the
holdout list, and that list is itself asserted in both directions.

This is the one bench tier that runs on **every** change. It is
`verilator --lint-only`, with no compile and no simulation, so it takes
seconds where the cocotb benches take half an hour, and it is what an RTL
retype breaks. `make coverage` runs it and skips the compile tier;
`make test-hdl` runs both.

Because every test in that file skips when `verilator` is missing, a
failed tool install would leave the tier green without elaborating
anything. Set `DAU_REQUIRE_VERILATOR=1` to turn that skip into a failure.
CI sets it, which is what makes the tier usable as a gate:

```bash
DAU_REQUIRE_VERILATOR=1 make coverage
```

## 6. Run the registry-wide guards

These walk the whole registry instead of your tile, and they catch
declarations that are well formed but wrong: an entry with no envelope, a
golden that does not import, a source file no entry claims.

```bash
python -m pytest dau_core/tests/test_cores.py -q
```

See the [registry guard reference](../reference/registry-guards.md) for what each one
checks. Four of them compare exact sets instead of predicates. If your tile
belongs in one, a new silent-on-success stage for example, adding it to the
pinned set is part of the change, so a reviewer sees it in the diff.

## 7. Run everything

```bash
python -m pytest dau_core -q
python -m ruff check dau_core && python -m ruff format --check dau_core
```

## Next

- [Measure a resource envelope](measure-a-resource-envelope.md): the one step
  that needs a synthesis host.
- [Registry-wide guards](../reference/registry-guards.md): the full list, and what each one
  checks.
- [Why each tile has a golden model](../explanation/why-each-tile-has-a-golden-model.md): why
  step 1 comes first.
