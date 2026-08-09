"""Integer models of the fixed-point transcendental datapaths.

Two layers, and the distinction is the whole accuracy contract:

* :func:`exp_reference` is the MATHEMATICAL answer -- true ``exp`` correctly
  rounded to the fixed-point format. It is what "right" means.
* :class:`ExpDatapath` is an ALGORITHM: a specific range reduction, table and
  polynomial at a specific internal precision. The HDL mirrors it exactly, so
  the cocotb benches compare against it with zero tolerance, like every other
  tile in the package.

An operating point that deviates from the reference does so by a MEASURED
amount, swept exhaustively over every representable input and recorded in the
registry beside the resource envelope. Accuracy is a measured fact here, not
a claim -- sampling reported one configuration as flawless when an exhaustive
sweep found three wrong inputs, so nothing is believed without the full
sweep.

See dau-docs-private/design/transcendental-tiles.md for how the operating
points were chosen.
"""

from __future__ import annotations

import math
from decimal import Decimal, getcontext
from functools import lru_cache

from ccflow import BaseModel
from pydantic import ConfigDict

#: enough digits to decide a rounding boundary that double cannot
_ORACLE_PRECISION = 80

EXP_FRACTIONAL_BITS = 16


def _decimal_context() -> None:
    if getcontext().prec < _ORACLE_PRECISION:
        getcontext().prec = _ORACLE_PRECISION


def exp_reference(x: int, *, fractional_bits: int = EXP_FRACTIONAL_BITS) -> int:
    """True ``exp`` of a Q(32-f).f value, correctly rounded to Q(32-f).f.

    The mathematical contract. Computed in 80 digits, so the half-way cases
    that double precision cannot decide are decided here.

    Raises when the result does not fit int32; the tile reports ``RANGE``.
    """
    _decimal_context()
    one = 1 << fractional_bits
    value = (Decimal(x) / one).exp() * one
    rounded = int((value + Decimal("0.5")).to_integral_value(rounding="ROUND_FLOOR"))
    if rounded > (1 << 31) - 1:
        raise ValueError(f"exp({x / one}) does not fit int32")
    return rounded


def exp_domain(*, fractional_bits: int = EXP_FRACTIONAL_BITS) -> tuple[int, int]:
    """The inputs where ``exp`` is neither 0 nor an overflow, inclusive.

    Below the low bound the correctly rounded answer is 0; above the high
    bound it does not fit int32. Small enough to sweep exhaustively, which is
    what makes a correctness claim about this tile a proof rather than a
    sample.
    """
    one = 1 << fractional_bits
    low = math.ceil(math.log(0.5 / one) * one)
    high = math.floor(math.log(((1 << 31) - 1 + 1) / one) * one)
    return low, high


class ExpDatapath(BaseModel):
    """One operating point of the exp datapath.

    ``x`` is reduced as ``k/2**table_bits`` units of ln2 plus a residue, the
    table supplies ``2**(j/2**table_bits)``, and a Horner polynomial of
    ``degree`` evaluates ``exp(residue)`` -- all in ``internal_bits``
    fractional bits.

    ``round_steps`` rounds each Horner step instead of truncating it, which
    costs an adder per step in hardware. It buys accuracy only where the
    internal precision is the binding constraint; past that the polynomial
    degree binds instead and rounding changes nothing.
    """

    model_config: ConfigDict = ConfigDict(frozen=True)

    internal_bits: int = 52
    table_bits: int = 8
    degree: int = 4
    round_steps: bool = False
    fractional_bits: int = EXP_FRACTIONAL_BITS
    # Fractional bits of the reciprocal-of-ln2 constant that picks the range
    # reduction index. Deliberately NARROW: k only chooses the split between
    # table index and residue, and a k off by one merely widens the residue,
    # which the polynomial still covers. Computing it at internal_bits made
    # it the widest multiply in the datapath for no accuracy at all --
    # measured correctly rounded at every width down to 12 bits.
    # 12, not 24: the constant is 21 bits instead of 34, which shrinks the
    # multiply and the fabric adder summing its partial products -- that
    # adder was an 11-deep CARRY4 chain between two DSPs and the tile's
    # critical path. Measured correctly rounded at every width down to 12.
    k_bits: int = 12

    def model_post_init(self, context) -> None:
        super().model_post_init(context)
        if not 1 <= self.degree <= 8:
            raise ValueError("degree must be in [1, 8]")
        if not 0 <= self.table_bits <= 12:
            raise ValueError("table_bits must be in [0, 12]")
        if self.internal_bits <= self.fractional_bits:
            raise ValueError("internal_bits must exceed fractional_bits")

    @property
    def multiplies(self) -> int:
        """Multiplies on the datapath: the Horner chain, the table, and the
        two in the range reduction. This is what the DSP cost tracks."""
        return self.degree + 1 + 2

    def constants(self) -> dict:
        return _exp_constants(self.internal_bits, self.table_bits, self.degree, self.k_bits)

    def evaluate(self, x: int) -> int:
        """The integer datapath, bit for bit as the HDL runs it."""
        c = self.constants()
        q, f = self.internal_bits, self.fractional_bits
        half = 1 << (q - 1)
        # TRUNCATED, not rounded. The rounding add spanned the whole product
        # and measured as a 13-deep CARRY4 chain between two DSPs -- the
        # critical path of the whole tile. k only picks the table/residue
        # split, so a half-step bias in it is absorbed by the residue.
        k = (x * c["inv_ln2_narrow"]) >> (self.k_bits + f)
        x_q = x << (q - f)
        residue = x_q - ((k * c["ln2"]) >> self.table_bits)

        acc = c["coefficients"][self.degree]
        for index in range(self.degree - 1, -1, -1):
            product = acc * residue
            acc = ((product + half) >> q if self.round_steps else product >> q) + c["coefficients"][index]
        product = acc * c["table"][k & ((1 << self.table_bits) - 1)]
        acc = (product + half) >> q if self.round_steps else product >> q

        shift = (q - f) - (k >> self.table_bits)
        return (acc + (1 << (shift - 1))) >> shift if shift > 0 else acc << (-shift)


@lru_cache(maxsize=None)
def _exp_constants(internal_bits: int, table_bits: int, degree: int, k_bits: int) -> dict:
    _decimal_context()
    scale = Decimal(2) ** internal_bits
    ln2 = Decimal(2).ln()
    entries = 1 << table_bits
    return {
        "ln2": round(scale * ln2),
        "inv_ln2": round(scale * entries / ln2),
        "inv_ln2_narrow": round(Decimal(2) ** k_bits * entries / ln2),
        "table": tuple(round(scale * (Decimal(2) ** (Decimal(j) / entries))) for j in range(entries)),
        "coefficients": tuple(round(scale / math.factorial(n)) for n in range(degree + 1)),
    }


#: The registered operating points, chosen by exhaustive sweep (every one of
#: the 1,453,635 representable inputs), not by sampling. Their accuracy is
#: pinned by the dau-core suite and recorded in the registry beside the
#: resource envelope.
#:
#: EXACT is correctly rounded: it agrees with :func:`exp_reference` on every
#: input, so for this point the algorithm and the mathematics coincide.
EXP_EXACT = ExpDatapath(internal_bits=52, table_bits=8, degree=4)
#: LIGHT is FAITHFUL: never more than one ULP from the correctly rounded
#: answer, for roughly a third of the multiplier width. Q32 was measured too
#: and reaches 3 ULP, so 36 bits is the floor worth registering.
EXP_LIGHT = ExpDatapath(internal_bits=36, table_bits=8, degree=3, round_steps=True)

#: Exhaustively measured deviation from :func:`exp_reference`, in ULP, for
#: each registered point. A bound that is asserted rather than swept is not
#: worth having: sampling declared a configuration flawless that an
#: exhaustive sweep showed wrong on three inputs.
EXP_MEASURED_ULP: dict[str, int] = {"exact": 0, "light": 1}

LN_FRACTIONAL_BITS = 16


def ln_reference(x: int, *, fractional_bits: int = LN_FRACTIONAL_BITS) -> int:
    """True ``ln`` of a Q(32-f).f value, correctly rounded to Q(32-f).f.

    The mathematical contract, like :func:`exp_reference`. Computed in 80
    digits so the half-way cases double precision cannot decide are decided
    here.

    Raises on a non-positive input; the tile reports ``DOMAIN``, the same
    position the sqrt tile takes -- ln of zero or a negative has no value and
    a substituted one would be a wrong number that looks right.
    """
    if x <= 0:
        raise ValueError(f"ln of a non-positive fixed-point value is undefined, got {x}")
    _decimal_context()
    one = 1 << fractional_bits
    value = (Decimal(x) / one).ln() * one
    return int((value + Decimal("0.5")).to_integral_value(rounding="ROUND_FLOOR"))


class LnDatapath(BaseModel):
    """One operating point of the ln datapath.

    ``x`` is normalised to ``m * 2**e`` with ``m`` in [1, 2), so
    ``ln(x) = e*ln2 + ln(m)``. The mantissa's top ``table_bits`` index two
    tables -- ``ln(1 + j/N)`` and the reciprocal ``1/(1 + j/N)`` -- reducing
    the residue to ``|v| <= 1/N`` before a short Horner series for
    ``ln(1+v)``. The reciprocal table is what keeps the polynomial degree low
    without a divider: a direct series on [0,1) would need many more terms.

    The exponent term is EXACT integer work: ``e`` is a small signed integer
    and ``ln2`` a constant, so the only approximation is the mantissa series.
    """

    model_config: ClassVar = ConfigDict(frozen=True)

    internal_bits: int = 52
    table_bits: int = 8
    degree: int = 3
    round_steps: bool = False
    fractional_bits: int = LN_FRACTIONAL_BITS

    def model_post_init(self, context) -> None:
        super().model_post_init(context)
        if not 1 <= self.degree <= 8:
            raise ValueError("degree must be in [1, 8]")
        if not 1 <= self.table_bits <= 12:
            raise ValueError("table_bits must be in [1, 12]")

    @property
    def multiplies(self) -> int:
        """The reciprocal multiply, the Horner chain, and e*ln2."""
        return 1 + self.degree + 1

    def constants(self) -> dict:
        return _ln_constants(self.internal_bits, self.table_bits)

    def evaluate(self, x: int) -> int:
        """The integer datapath, bit for bit as the HDL will run it."""
        if x <= 0:
            raise ValueError("ln datapath requires a positive input")
        c = self.constants()
        q, f = self.internal_bits, self.fractional_bits
        half = 1 << (q - 1)

        # normalise: x = m * 2**e with m in [1, 2), m carried at q bits
        msb = x.bit_length() - 1
        exponent = msb - f
        mantissa = (x << (q - msb)) if q >= msb else (x >> (msb - q))

        index = (mantissa >> (q - self.table_bits)) & ((1 << self.table_bits) - 1)
        # v = m * recip[index] - 1, small by construction
        v = ((mantissa * c["reciprocal"][index]) >> q) - (1 << q)

        # ln(1+v) by Horner on the alternating series
        acc = c["series"][self.degree]
        for term in range(self.degree - 1, 0, -1):
            product = acc * v
            acc = ((product + half) >> q if self.round_steps else product >> q) + c["series"][term]
        product = acc * v
        acc = (product + half) >> q if self.round_steps else product >> q

        total = exponent * c["ln2"] + c["ln_table"][index] + acc
        shift = q - f
        return (total + (1 << (shift - 1))) >> shift if shift > 0 else total << (-shift)


@lru_cache(maxsize=None)
def _ln_constants(internal_bits: int, table_bits: int) -> dict:
    _decimal_context()
    scale = Decimal(2) ** internal_bits
    entries = 1 << table_bits
    ln_table = []
    reciprocal = []
    for j in range(entries):
        base = 1 + Decimal(j) / entries
        ln_table.append(round(scale * base.ln()))
        reciprocal.append(round(scale / base))
    # ln(1+v) = v - v^2/2 + v^3/3 - ...; index k holds the coefficient of v^k
    series = [0] + [round(scale * (Decimal(-1) ** (k + 1)) / k) for k in range(1, 9)]
    return {
        "ln2": round(scale * Decimal(2).ln()),
        "ln_table": tuple(ln_table),
        "reciprocal": tuple(reciprocal),
        "series": tuple(series),
    }
