# Why resource envelopes are measurements that refuse

A `CoreDefinition` carries its resource cost as a list of *measurement
points*, each keyed by the part it was synthesized for, the clock period it
met (or none, for a combinational core), and the module parameters it was
built with. The registry once allowed a second shape, a scalar envelope:
one LUT/FF/BRAM/DSP tuple for the core with no coordinates. It is gone, and
the reason is worth stating, because the change looked like bookkeeping
and was not.

## A scalar envelope is a number without a question

Take a made-up figure: "this core costs 733 LUTs" is only meaningful as an answer to "on which
part, at which clock, with which parameters?" A scalar envelope answers a
question nobody asked, and it answers it identically no matter what is
asked.

That has produced two concrete failures.

**A scalar envelope let one part's number price another part's build.** A
design targeting the Kintex was priced with a wide-predicate number
measured on the Artix. The estimate reported `measured=True` while mixing
parts, and it hid a tile that had never met timing on the target part.

**A scalar envelope ignores parameters, including ones that change the
cost.** When the shared dispatcher gained a hash decode, that mode cost 3
DSP48s where the mask decode costs none. The scalar envelope has no place
to record "depends on `MODE`", so a hash-dispatched design would have
priced as DSP-free and overflowed at implementation.

## Refusal is the feature

A migrated core does not interpolate, extrapolate, or fall back. Ask it
for a coordinate it has not been measured at and it raises
`UnmeasuredResourceError` naming the missing coordinate.

That is deliberate, and it is stronger than returning a marker value. A
marker can be dropped by a numeric consumer — summed, compared, formatted
— and become a plausible-looking number somewhere downstream. An exception
cannot be silently consumed. The planner catches it and turns it into a
typed refusal that names the measurement which would close it, so "we
cannot price this" is a *reportable* state rather than a wrong number.

The same logic applies to timing: a point whose `timing` is `VIOLATED` is
never usable as a budget. A violated build is data about a failure, not a
smaller envelope.

## Where the numbers come from

Points are produced by the `dau-build` `synthesize-cores` task, one
out-of-context synthesis per (part, clock, parameter) combination, and the
task flags drift against whatever the registry already declares. Synthesis
is never hand-run; a number nobody can reproduce through the task is not a
number the registry will carry.

## What a measured point does not promise

An out-of-context envelope is measured with the core alone. Composed into
a shell, the same core shares routing resources and placement pressure
with everything else, and its timing margin can move substantially — one
tile measured +0.410 ns standalone and −0.348 ns composed, a swing of
0.76 ns that turned a pass into a failure.

So a measurement point is an honest answer to "what does this core cost by
itself", and an *input* to the question "will this composition close". It
is not the second answer. A margin under roughly half a nanosecond should
be read as a composed-build risk rather than headroom, and that is a
judgement the registry records in prose beside the numbers, because the
numbers alone cannot carry it.

## The cost of the discipline

Measuring a core means synthesizing it across its parameter space on every
supported part, which is real machine time, and a core that has not been
measured on a part cannot price a design for that part: the estimator
raises rather than borrowing another part's number. That refusal is the
cost of never again letting an Artix figure price a Kintex build.
