# Why each tile has a golden model

Every operator tile in `dau-core` has a pure-Python function that computes
what the tile computes. The tile's cocotb bench runs the HDL and the
function on the same inputs and asserts the outputs are identical. That
function is the tile's *golden*, and it defines what the tile means. No
prose specification does.

## Where prose specifications go wrong

A specification written in prose gets interpreted twice: once by the person
writing the HDL and once by the person writing the host code that reads the
tile's output. The two readings agree only if the prose was unambiguous,
and prose is weakest in exactly the places hardware and software tend to
drift apart: tie-breaking, boundary values, overflow, and output order. The
drift stays invisible until a result is wrong.

Saying a top-k tile emits its rows "in sorted order" leaves two questions
open: sorted by what, and which of two equal keys comes first. A sentence
can be read either way. A function cannot.

## What an executable definition makes possible

**The bench compares against the contract.** The cocotb bench does not
check the HDL against hand-written expected values. It checks it against
the golden over generated inputs, including adversarial ones. A
hand-written expected value records what the test author believed; the
golden records the contract, so the author's assumptions do not end up in
the test.

**The host can compute the same answer.** `dau-polars` runs compiled scans
on the golden models to get the answer the device would produce. That lets
anyone check a device result without a device, and it keeps the host path
consistent with the device path by definition.

**A disagreement has to be resolved.** When a tile's HDL and its golden
disagree, the bench fails and someone has to decide which is right. These
decisions are often informative. One showed that the hash-mode grouped
aggregation's golden returned partial results on overflow while the HDL
returned none. The HDL was correct: the host recovers by re-running the
batch, and a partial result would have been counted twice.

## The golden fixes more than the arithmetic

A golden also defines:

- **Ordering.** `streaming_top_k` emits rows by descending key, with ties
  broken by arrival, earliest first. That is part of the contract, so the
  tile cannot use a structure that evicts in a different order, however much
  faster it would be.
- **Errors.** Golden functions return a status code with their results. The
  code values are the tile's `status_error_code` wire encoding, so the two
  cannot drift apart unnoticed.
- **Domain.** Keys and fields are fixed-width *wires*, so `-1` and
  `0xFFFFFFFF` are the same 32-bit key. Goldens wrap into that domain
  explicitly, because Python's unbounded integers would describe hardware
  that cannot be built.

## What a golden does not do

A golden is not a fallback implementation or a performance reference. It
runs on the CPU because that is where tests run; the product never executes
it in place of hardware. It is written for clarity over speed, since its job
is to be read alongside the HDL.

It also contains no hardware structure. The golden for a systolic array is a
sorted list, and the golden for a hash table is a dictionary. The tile can
be built any way that produces identical output, which is what lets a tile
be retimed, pipelined, or rewritten without changing what it means.
