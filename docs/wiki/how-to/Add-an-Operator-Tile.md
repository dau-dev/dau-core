# How to add an operator tile

This guide takes a new streaming operator from an idea to a registered core
that a composition can instantiate. It follows the order the project always
uses — **golden, HDL, bench, registry entry, envelope** — because each stage
is the acceptance test for the next, and skipping one moves the failure
later where it costs more.

You will need `verilator` for the bench, and access to a synthesis host for
the final envelope step.

## 1. Write the golden first

The golden is a pure function in `dau_core/operators.py` (or
`dau_core/temporal.py` for time-series shapes) that computes what the tile
computes. It is the definition of the tile's meaning, so write it before
any HDL exists and settle the semantics there.

Decide and encode these explicitly — they are the parts prose gets wrong:

- **The domain.** Fields are fixed-width, null-free *wires* — the width is
  a tile PARAMETER, not part of the tile's identity. Wrap at the declared
  width so `-1` and the all-ones pattern are the same key.

  **Do not put a width in the name.** Part of the existing palette is still
  called `int32-*` because it was written before the width was parameterized,
  and the moment such a tile gains its parameter the name is wrong: the row
  predicate filter's `FIELD_WIDTH` accepts 16, 32 and 64, so `int32-` would
  have been false for two of its three legal configurations — which is why
  it is now just `row-predicate-filter`. A name that claims a width the
  parameters contradict has to be either renamed or kept in step forever,
  and the second never happens.

  That debt is being paid down tile by tile, and `WIDTH_NAMED_TILES` in
  `dau_core/tests/test_cores.py` holds the remaining set EXACTLY, each entry
  with a comment saying why the width is still in its identity and what
  removes it; the tiles that keep a width on purpose do so because for them
  the width really is part of the contract. Your new tile is not one of
  them: give it a width in its name and that guard fails.

- **Emission order.** If your tile holds state, say exactly what order
  results leave in, and pick something implementation-independent.

- **Error codes.** Add an `IntEnum` whose values ARE the tile's
  `status_error_code` wire encoding, and say so in its docstring.

- **Capacity behaviour.** A bounded table that fills is an error, never an
  eviction. State what the caller gets back.

Return `(results, error_code)` when the tile can fail.

## 2. Prove the golden against itself

Add `dau_core/tests/test_<name>.py` with property tests, not just examples:
permutation invariance if order should not matter, agreement with an
existing golden where the two overlap, boundary values, and the declared-width wire
identity. If your tile has a host-side counterpart (a merge, a split), prove
the algebra — `merge(split(rows)) == one_shot(rows)`.

Run it:

```bash
python -m pytest dau_core/tests/test_<name>.py -q
```

## 3. Write the HDL

Put `dau_<name>.sv` in `dau_core/hdl/` and export it from
`dau_core/hdl/__init__.py`. **Name it for what it computes, not for a
width** — declare a `FIELD_WIDTH` (or equivalent) parameter and let the
composition choose. Follow the palette's conventions:

- `input_valid`/`input_ready`/`input_data`/`input_last` in,
  `output_*` out, plus the `status_*` close-out channel.
- Exactly one close-out per batch; silent on success mid-chain, terminal
  closes out.
- Latch configuration on a batch's first beat.
- **If you hold an array in BRAM, access it from a dedicated reset-free
  process** and keep valid bits in a separate narrow array — an array
  touched by a reset-carrying FSM process infers distributed RAM instead.
- Mirror the golden's error-code values as localparams.

Lint before simulating:

```bash
verilator --lint-only -Wall --timing dau_core/hdl/dau_<name>.sv
```

## 4. Write the cocotb bench

Add `dau_core/tests/cocotb_benches/dau_<name>_tb.py`. It drives the
HDL and compares against the golden — never against hand-written expected
values.

Cover, at minimum: the nominal case, backpressure (a stalled beat must stay
asserted and byte-stable), reset, repeated batches back to back, malformed
input, and every error path. If the tile is stateful, add the
**randomized-delay patience harness**: inject random delays on every
channel and assert the emitted sequence is bit-identical across seeds.

If the tile is parameterized, **run the bench at its boundaries** — the
declared minimum, a small value that forces collisions or short scans, and
the default. Cases whose point is holding several items at once should skip
at parameter values too small for them rather than being weakened to fit.

Wire it into `dau_core/tests/test_hdl_cocotb.py` with one runner per
parameter value, then:

```bash
python -m pytest dau_core/tests/test_hdl_cocotb.py -q -k <name>
```

## 5. Add the registry entry

Append a `CoreDefinition` to `dau_core/config/dau-core/cores.yaml`. There is
no Python-literal registry — the yaml is the registry. Declare:

`name`, `kind: operator`, `module`, `source`, a `description` that says what
the tile does and what its contracts are, `golden` (as
`module:function`), `bench`, `count_port`, `contract_kind`, `record_words`,
`success_close_out`, and `parameters` mirroring your RTL elaboration guards.

If the tile advertises capabilities, add `opcodes`, `opcode_input_types`,
and `unit_kind` — a composition built only from tiles that declare none
reports an empty opcode bitmap and is invisible to the planner.

Declare `ingest` honestly: state the assumption your rate depends on and
name the adversarial case rather than padding the number. If the tile owns
on-chip storage, declare `storage_intent`.

Check it loads:

```bash
python -c "from dau_core.registry import core; print(core('<name>'))"
```

## 6. Measure the envelope

Resource numbers come from the `dau-build` `synthesize-cores` task, never
from a hand-run synthesis:

```bash
dau-build task=tasks/build/synthesize-cores \
  'model.cores=[/dau-core/<name>]' \
  model.output_root=./ooc model.part=<part> \
  '+model.parameters={<name>: {PARAM: <value>}}' \
  model.execute=true
```

Run it for every parameter value the catalog can request, on every
supported part, and add the results as `resources:` measurement points
keyed by `part`, `clock_ns`, and `params`. A drift guard refuses a
synthesizable core with no envelope, which is what tells you the step is
still outstanding.

**Read a thin margin as a warning.** An out-of-context margin under roughly
half a nanosecond is a composed-build risk, not headroom — budget a retime
before composing it.

## 7. Confirm the whole suite

```bash
python -m pytest dau_core -q
ruff check dau_core/ && ruff format dau_core/
```

The tile is now instantiable by any `ScanCompositionSpec` that names it.
