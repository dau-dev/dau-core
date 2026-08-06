# Why a golden model, not a specification document

Every operator tile in `dau-core` has a pure-Python function that computes
what the tile computes. The tile's cocotb bench runs the HDL and the
function over the same inputs and asserts the outputs are identical. That
function is called the tile's *golden*, and it — not any prose — is the
definition of what the tile means.

## The problem a written specification cannot solve

A specification written in prose has to be interpreted twice: once by the
person writing the HDL, and once by the person writing the host code that
consumes the tile's output. Those two interpretations agree only by luck,
and they disagree in the places prose is weakest — tie-breaking, boundary
values, what happens on overflow, what order results come out in.

Those are exactly the places where hardware and software drift apart, and
where the drift is invisible until a result is wrong. A tile that emits
its top-k rows in "sorted order" is under-specified: sorted how, and which
of two equal keys wins? A prose answer can be read two ways. A function
cannot.

## What executability buys

Because the golden is code, three things become possible that a document
cannot offer.

**The bench can be a proof rather than a checklist.** The cocotb bench does
not compare the HDL against hand-written expected values — it compares
against the golden over generated inputs, including adversarial ones. A
hand-written expected value encodes the test author's belief; a golden
encodes the contract, and the test author's beliefs cannot silently enter.

**The host can compute the same answer.** `dau-polars` executes compiled
scans on the golden models to produce the answer the device would produce.
That makes a device result checkable without a device, and it makes the
CPU fallback path *definitionally* consistent with the offload path rather
than approximately so.

**Disagreements become discoverable rather than theoretical.** When a
tile's HDL and its golden disagree, the bench fails and someone has to
decide which is right. That decision is often interesting: it surfaced,
for instance, that the hash-mode grouped aggregation's golden was returning
partial results on overflow while the HDL returned none — and the HDL was
correct, because the host's recovery re-runs the batch and would have
double-counted a returned partial.

## The contract extends past the arithmetic

A golden defines more than the values. It also fixes:

- **Ordering.** `streaming_top_k` emits key-descending with ties broken by
  arrival, earliest first. That is a decision, not an implementation
  detail — it means the tile may not use a data structure whose eviction
  order differs, however much faster it might be.
- **Error behaviour.** Golden functions return a status code alongside
  their results. The code values *are* the tile's `status_error_code` wire
  encoding, so the two cannot drift apart silently.
- **Domain.** Keys and fields are int32 *wires*, so `-1` and `0xFFFFFFFF`
  are the same key. Goldens wrap into that domain explicitly, because a
  golden that used Python's unbounded integers would be describing a
  machine nobody can build.

## What a golden is not

A golden is not a fallback implementation and not a performance reference.
It runs on the CPU because that is where tests run, not because the
product ever executes it in place of hardware. It is written for clarity
over speed; a golden that had been optimised would be harder to read
against the HDL, which is the only thing it exists to be read against.

It is also not a place for hardware structure. The golden for a systolic
array is a sorted list. The golden for a hash table is a dictionary. The
tile is free to be anything that produces identical output — which is
precisely the freedom that lets a tile be retimed, pipelined, or rebuilt
without renegotiating its meaning.
