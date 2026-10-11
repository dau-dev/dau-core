# Why resource costs are measured

A `CoreDefinition` records its resource cost as a list of *measurement
points*. Each point is keyed by the part it was synthesized for, the clock
period it was constrained to (or none, for a combinational core), and the
module parameters it was built with. The registry used to allow a second
form, a scalar envelope: one LUT/FF/BRAM/DSP tuple per core with no
coordinates. That form was removed, and the reasons are worth recording.

## A cost only means something at a coordinate

"This core costs 733 LUTs" (a made-up figure) only answers the question if
you also know the part, the clock, and the parameters. A scalar envelope
gives the same number whichever of those you ask about.

That caused two real failures.

**One part's number priced another part's build.** A design targeting the
Kintex was priced with a wide-predicate figure measured on the Artix. The
estimate reported `measured=True` while mixing parts, and it hid a tile that
had never met timing on the target part.

**Parameters that change the cost were ignored.** When the shared
dispatcher gained a hash decode, that mode cost 3 DSP48s where the mask
decode costs none. A scalar envelope has nowhere to record that the cost
depends on `MODE`, so a hash-dispatched design would have been priced as
DSP-free and failed at implementation.

## An unmeasured coordinate raises

A core does not interpolate, extrapolate, or fall back to a nearby point.
Ask for a coordinate it has not been measured at and it raises
`UnmeasuredResourceError`, naming the missing coordinate.

An exception is used instead of a sentinel value on purpose. A sentinel can
be summed, compared, or formatted by numeric code and turn into a
plausible-looking number further down. An exception has to be handled. The
planner catches it and reports which measurement would fill the gap, so
"this cannot be priced" is something it can tell the user instead of a
wrong number.

Timing follows the same rule. A point whose `timing` is `VIOLATED` is never
usable as a budget: it records a failed build, not a smaller one.

## Where the numbers come from

Points are produced by the `dau-build` `synthesize-cores` task, one
out-of-context synthesis per (part, clock, parameter) combination, and the
task reports any drift from what the registry already declares. Synthesis
is never run by hand, because a hand-run number loses the coordinate it was
measured at.

## What a measured point does not tell you

An out-of-context run measures the core by itself. Inside a shell the same
core shares routing and placement with everything else, and its timing
margin can move a long way: one tile measured +0.410 ns standalone and
−0.348 ns composed, a 0.76 ns swing that turned a pass into a failure.

A measurement point answers "what does this core cost on its own", and it
is an input to "will this composition close timing". It does not answer
the second question. Treat a margin under about half a nanosecond as a risk
for the composed build, not as headroom. The registry notes this next to
the numbers, since the numbers cannot show it.

## What it costs

Measuring a core means synthesizing it across its parameter space on every
supported part, which takes real machine time. Until a core has been
measured on a part, a design for that part cannot be priced: the estimator
raises instead of borrowing another part's number. That is the trade for
never again pricing a Kintex build with an Artix figure.
