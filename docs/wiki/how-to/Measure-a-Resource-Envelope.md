# How to measure a resource envelope

This guide shows you how to produce the `resources:` measurement points a
registry entry holds, and how to get them into `cores.yaml`.

Envelopes come from the `dau-build` `synthesize-cores` task. **Never run
synthesis by hand.** The registry only takes numbers the task can
reproduce, because a hand-run number loses the coordinate it was measured
at: the part, the clock, and the parameters.

You need a host with Vivado and the part's device support installed.

## Write the handoff

The task resolves `/dau-core/<name>` entries through the registry, closes
their dependencies, and plans one out-of-context synthesis per core.
Without `model.execute=true` it writes the plan and stops, which is the
form to put up for review or pass to someone who has the toolchain:

```bash
dau-build task=tasks/build/synthesize-cores \
  'model.cores=[/dau-core/<name>]' \
  model.output_root=./ooc \
  model.part=<part> \
  model.clock_period_ns=8.0
```

Under `output_root` you get, per core, an `.ooc.xdc` carrying the clock
constraint, an `.ooc.tcl` that reads the dependency-closed sources and runs
`synth_design -mode out_of_context`, and a `synthesize-cores.sh` that
invokes Vivado over them. The XDC is read *before* synthesis, so the run is
constrained rather than measured afterwards.

Selecting a platform instead of a bare part takes the part from
configuration:

```bash
dau-build task=tasks/build/synthesize-cores \
  'model.cores=[/dau-core/<name>]' \
  model.output_root=./ooc \
  platform=platforms/<vendor>/<board>
```

## Sweep the parameters

A parameterized core costs different amounts at different parameter
values, and an envelope that cannot record that will misprice a build. Pass
the overrides explicitly:

```bash
dau-build task=tasks/build/synthesize-cores \
  'model.cores=[/dau-core/<name>]' \
  model.output_root=./ooc model.part=<part> \
  '+model.parameters={<name>: {PARAM: <value>}}' \
  model.execute=true
```

An override that violates the entry's `ParameterSpec` is rejected before
Vivado is invoked. Run the sweep for every value the catalog can request,
on every supported part, and at every clock the board can run the job at.

A combinational core has no clock to constrain: give it an empty clock port
(`'+model.clock_ports={<name>: ""}'`) and the task skips both the XDC
and the timing report.

## Run it

```bash
dau-build task=tasks/build/synthesize-cores \
  'model.cores=[/dau-core/<name>]' \
  model.output_root=./ooc model.part=<part> \
  model.execute=true
```

With `execute=true` the task runs Vivado per core, parses utilization and
timing, and reports one line per core with `lut`, `ff`, `bram36`, `dsp`,
the worst negative slack, and whether timing was met. It also compares
against whatever the registry already declares, and flags drift.

Drift comparison requires *every* axis to match. A coordinate the
registry has never been measured at is reported as having nothing to
compare against, not as a mismatch, so an unmeasured part does not look
like a regression.

## Record the points

There is no writer: you transcribe the results. Each run becomes one entry
in the core's `resources:` list, keyed by the coordinate it was measured
at, with a comment recording where it came from:

```yaml
  # Measured via dau-build task=tasks/build/synthesize-cores.
  resources:
    - part: xc7a200tfbg484-2
      clock_ns: 8.0
      params: {CAPACITY: 1024}
      lut: 84
      ff: 210
      bram36: 1.0
      dsp: 0
      timing: met
      wns_ns: 3.857
```

Every point's `params` must name exactly the parameters the entry declares
— no more and no fewer — and no two points may share a
`(part, clock_ns, params)` coordinate. A point whose `timing` disagrees
with the sign of its `wns_ns` is rejected at load.

Confirm the entry loads and resolves at the coordinate you measured:

```bash
python -c "from dau_core.registry import core; print(core('<name>').resource_measurement(part='<part>', clock_ns=8.0, params={'PARAM': <value>}))"
```

## Read a thin margin as a warning

An out-of-context margin is measured with the core alone. Composed into a
shell it shares routing and placement pressure with everything else, and
the margin moves: one tile measured +0.410 ns standalone and −0.348 ns
composed. Treat anything under about half a nanosecond as a risk for the
composed build, not headroom, and budget a retime before composing it.

A point whose `timing` is `violated` records a failed build. You can still
read it, but `usable_envelope()` raises instead of returning it as a
budget.

## Next

- [Why resource costs are measured](Why-Resources-Are-Measured): why an
  unmeasured coordinate raises instead of interpolating.
- [`CoreDefinition` fields](Core-Definition): the `resources` field and
  the lookup that reads it.
- [Registry-wide guards](Registry-Guards): the guards that fail a
  synthesizable core with no envelope.
