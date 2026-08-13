# Tutorial: read a tile's contract end to end

In this tutorial you will take one operator tile and follow it through
every layer that defines it — its registry entry, its golden model, its
measured cost, and the HDL those things describe. By the end you will know
where each kind of fact about a tile lives, which is what you need before
adding or changing one.

We will use `int32-streaming-top-k`, the tile that keeps the K largest rows
by sort key.

You need `dau-core` installed. Nothing here needs hardware or a simulator.

## Step 1: look the tile up in the registry

Open a Python prompt and ask the registry for it:

```python
from dau_core.registry import core

tile = core("int32-streaming-top-k")
print(tile.name)
print(tile.kind.value)
print(tile.module)
```

You should see:

```
int32-streaming-top-k
operator
dau_int32_streaming_top_k
```

The registry is not a Python list — it is a Hydra config tree loaded into
the ccflow `ModelRegistry`. `core()` is a read-through view over whatever
registry your application has composed.

## Step 2: find its meaning

The tile's `golden` field names the function that defines what it computes:

```python
print(tile.golden)
```

```
dau_core.operators:streaming_top_k
```

Import it and run it. Give it four rows and ask for the top two:

```python
from dau_core.operators import streaming_top_k

rows = [(5, 1, 0, 0), (9, 2, 0, 0), (1, 3, 0, 0), (9, 4, 0, 0)]
print(streaming_top_k(rows, k=2))
```

```
[(9, 2, 0, 0), (9, 4, 0, 0)]
```

Look closely at that result. Both rows with key `9` survived, and they came
out in **arrival order** — the row that arrived first is first. That is not
an accident of the implementation; it is the contract. The golden's
docstring states it, and the HDL must reproduce it exactly.

## Step 3: see what it costs

Ask for the resource measurement at a specific coordinate:

```python
measurement = tile.resource_measurement(
    part="xc7a200tfbg484-2", clock_ns=8.0, params={"K": 8}
)
envelope = measurement.usable_envelope()
print(envelope.lut, envelope.ff, envelope.bram36, envelope.dsp)
print(measurement.timing.value, measurement.wns_ns)
```

```
1295 1196 0.0 0
met 4.35
```

Now ask for a coordinate nobody has measured:

```python
from dau_core.cores import UnmeasuredResourceError

try:
    tile.resource_measurement(part="xc7a200tfbg484-2", clock_ns=8.0, params={"K": 3})
except UnmeasuredResourceError as error:
    print("refused:", error)
```

You should see a refusal naming the missing coordinate. The registry does
not interpolate between K=1 and K=5 to invent a number for K=3. Refusing is
the feature: a plausible-looking number that nobody measured is how a
design gets priced against the wrong hardware.

## Step 4: find the HDL those facts describe

The registry knows which sources the tile needs, in compile order:

```python
from dau_core.registry import sources_for

for path in sources_for("int32-streaming-top-k"):
    print(path.name)
```

```
dau_int32_streaming_top_k.sv
```

This tile has no dependencies. A tile that used a shared package would list
that package first — `sources_for` resolves the dependency closure so a
build never has to.

Open that file and read its header comment. It describes the same contract
you saw in step 2, in the language of the hardware: a size-K systolic
sorted register array where physical position encodes arrival order, which
is *why* equal keys keep the earliest arrival without needing a counter.

## Step 5: find the proof that they agree

```python
print(tile.bench)
```

```
dau_core.tests.cocotb_benches.dau_int32_streaming_top_k_tb
```

That bench drives the HDL and compares its output against
`streaming_top_k` — the same function you ran in step 2 — over generated
inputs. It does not compare against hand-written expected values, so the
test author's beliefs about the tile cannot quietly replace the contract.

Run it if you have `verilator` installed:

```bash
python -m pytest dau_core/tests/test_hdl_cocotb.py -q -k streaming_top_k
```

## What you now know

Five facts about a tile live in five places, and each has one owner:

| Fact                          | Lives in                                 |
| ----------------------------- | ---------------------------------------- |
| That it exists, and its shape | the registry yaml                        |
| What it computes              | the golden function                      |
| What it costs                 | measurement points on the registry entry |
| How it computes it            | the SystemVerilog                        |
| That the last two agree       | the cocotb bench                         |

Adding a tile means producing all five, in that order. That is the subject
of [how to add an operator tile](Add-an-Operator-Tile).
