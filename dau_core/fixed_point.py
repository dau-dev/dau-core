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
from functools import cache, lru_cache

from .models import CheckedModel

# enough digits to decide a rounding boundary that double cannot
_ORACLE_PRECISION = 80

EXP_FRACTIONAL_BITS = 16


def _decimal_context() -> None:
    getcontext().prec = max(getcontext().prec, _ORACLE_PRECISION)


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


class ExpDatapath(CheckedModel):
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


@cache
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


# The registered exp operating points are registry entries (``fixed-point-exp``
# and ``fixed-point-exp-light``): each core's ``generator.datapath`` is its
# ``ExpDatapath`` and its ``accuracy_ulp`` the exhaustively measured
# deviation from :func:`exp_reference`. The exact point is correctly rounded
# (0 ULP, agreeing with the reference on every input); the light point is
# faithful (never more than 1 ULP) for roughly a third of the multiplier
# width. Q32 was measured too and reaches 3 ULP, so 36 bits is the floor
# worth registering. A bound that is asserted rather than swept is not worth
# having: sampling once declared a configuration flawless that an exhaustive
# sweep showed wrong on three inputs.

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


class LnDatapath(CheckedModel):
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


@cache
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


NORMAL_CDF_FRACTIONAL_BITS = 16


def normal_cdf_reference(x: int, *, fractional_bits: int = NORMAL_CDF_FRACTIONAL_BITS) -> int:
    """The standard normal CDF of a Q(32-f).f value, correctly rounded.

    TOTAL: every int32 input has an answer, so unlike ln and exp there is no
    error path. Beyond about +/-4.33 the result saturates to 0 or 1 exactly,
    which IS the correctly rounded answer rather than a clamp.
    """
    _decimal_context()
    one = 1 << fractional_bits
    value = Decimal(x) / one
    # 0.5 * erfc(-v / sqrt(2)), evaluated in Decimal via the error function
    half = Decimal("0.5")
    result = half * _erfc(-value / Decimal(2).sqrt())
    return int((result * one + half).to_integral_value(rounding="ROUND_FLOOR"))


def _erfc(z: Decimal) -> Decimal:
    """Complementary error function to the working precision."""
    if z < 0:
        return 2 - _erfc(-z)
    if z == 0:
        return Decimal(1)
    if z > 12:  # underflows the working precision
        return Decimal(0)
    # erf(z) = 2/sqrt(pi) * sum_{n} (-1)^n z^(2n+1) / (n! (2n+1))
    pi = _pi()
    total = Decimal(0)
    term_z = z
    factorial = Decimal(1)
    for n in range(200):
        if n:
            factorial *= n
            term_z *= z * z
        term = (Decimal(-1) ** n) * term_z / (factorial * (2 * n + 1))
        total += term
        if abs(term) < Decimal(10) ** (-getcontext().prec):
            break
    erf = 2 / pi.sqrt() * total
    return 1 - erf


@lru_cache(maxsize=1)
def _pi() -> Decimal:
    """Pi to the working precision (Chudnovsky is overkill; Machin suffices)."""
    getcontext().prec += 10
    one = Decimal(1)

    def arctan_inverse(value: int) -> Decimal:
        total = term = one / value
        square = value * value
        divisor = 1
        while True:
            term = -term / square
            divisor += 2
            step = term / divisor
            if not step:
                return total
            total += step

    result = 4 * (4 * arctan_inverse(5) - arctan_inverse(239))
    getcontext().prec -= 10
    return +result


def normal_cdf_domain(*, fractional_bits: int = NORMAL_CDF_FRACTIONAL_BITS) -> tuple[int, int]:
    """The inputs where the CDF is neither 0 nor 1 to the last bit.

    Small enough to sweep EXHAUSTIVELY, which is what makes a correctness
    claim about this tile a proof rather than a sample.
    """
    one = 1 << fractional_bits
    limit = 0
    while normal_cdf_reference(limit, fractional_bits=fractional_bits) < one:
        limit += one // 16
    return -limit, limit


class NormalCdfDatapath(CheckedModel):
    """One operating point of the normal-CDF datapath.

    Folds on symmetry (the CDF of -x is one minus the CDF of x), so only the
    positive half is approximated, then evaluates a PER-SEGMENT Taylor
    polynomial: the magnitude's top ``table_bits`` select a segment and the
    coefficients are the CDF's derivatives at that segment, which have closed
    forms in the density. Piecewise rather than one polynomial because the
    CDF has no useful global series.

    Saturation is exact rather than a clamp: beyond the domain the correctly
    rounded answer IS 0 or 1, so this tile is TOTAL and needs no error path
    -- the one transcendental here that does not.
    """

    internal_bits: int = 52
    # 512 segments at degree 3 is the CHEAPEST correctly-rounded point on the
    # measured curve: 256/degree-3 leaves 6 of 573,441 inputs wrong, and
    # 256/degree-4 also reaches 0 but spends a fourth multiply. A multiply
    # costs 5-9 DSP48s and the extra segments cost about one BRAM36, and DSPs
    # are the scarcer resource here.
    # 1024 segments at degree 3 is the cheapest correctly-rounded point once
    # the range is power-of-two aligned: 512/degree-3 leaves 4 of 573,441
    # wrong, and 512/degree-4 also reaches 0 but spends a fourth multiply.
    # Segments cost BRAM, multiplies cost 5-9 DSP48s each, and DSPs are the
    # scarcer resource.
    table_bits: int = 10
    degree: int = 3
    fractional_bits: int = NORMAL_CDF_FRACTIONAL_BITS
    # The segmented range is [0, 2**(span_bits - f)), a POWER OF TWO, so the
    # segment index is a shift and the segment start a mask. Segmenting the
    # true domain edge (4.375) instead made both a division by a
    # non-power-of-two constant -- free in Python, a runtime divider in
    # fabric, and measured at WNS -9.576 ns on the Artix.
    span_bits: int = 19

    def model_post_init(self, context) -> None:
        super().model_post_init(context)
        if not 1 <= self.degree <= 5:
            raise ValueError("degree must be in [1, 5]")
        if not 1 <= self.table_bits <= 12:
            raise ValueError("table_bits must be in [1, 12]")

    @property
    def multiplies(self) -> int:
        """The Horner chain; the symmetry fold and saturation are free."""
        return self.degree

    def constants(self) -> dict:
        return _normal_cdf_constants(self.internal_bits, self.table_bits, self.degree, self.fractional_bits, self.span_bits)

    def evaluate(self, x: int) -> int:
        """The integer datapath, bit for bit as the HDL will run it."""
        c = self.constants()
        q, f = self.internal_bits, self.fractional_bits
        one = 1 << f
        negative = x < 0
        magnitude = -x if negative else x
        if magnitude >= c["limit"]:
            return 0 if negative else one

        # shift and mask, not divide -- see span_bits
        index = magnitude >> c["segment_shift"]
        segment_start = index << c["segment_shift"]
        residue = (magnitude - segment_start) << (q - f)

        coefficients = c["coefficients"][index]
        acc = coefficients[self.degree]
        for term in range(self.degree - 1, -1, -1):
            acc = ((acc * residue) >> q) + coefficients[term]

        shift = q - f
        result = (acc + (1 << (shift - 1))) >> shift
        result = max(0, min(one, result))
        return (one - result) if negative else result


@cache
def _normal_cdf_constants(internal_bits: int, table_bits: int, degree: int, fractional_bits: int, span_bits: int) -> dict:
    _decimal_context()
    scale = Decimal(2) ** internal_bits
    one = 1 << fractional_bits
    _low, saturate_at = normal_cdf_domain(fractional_bits=fractional_bits)
    segments = 1 << table_bits
    segment_shift = span_bits - table_bits
    root_two = Decimal(2).sqrt()
    root_two_pi = (2 * _pi()).sqrt()

    coefficients = []
    for index in range(segments):
        start_ticks = index << segment_shift
        start = Decimal(start_ticks) / one
        density = (-start * start / 2).exp() / root_two_pi
        # derivatives of the CDF at the segment START, in closed form:
        # d0 = Phi, d1 = phi, d2 = -x*phi, d3 = (x^2-1)*phi, ...
        taylor = [
            Decimal("0.5") * _erfc(-start / root_two),
            density,
            -start * density / 2,
            (start * start - 1) * density / 6,
            start * (3 - start * start) * density / 24,
            (start**4 - 6 * start * start + 3) * density / 120,
        ]
        coefficients.append(tuple(round(scale * taylor[term]) for term in range(degree + 1)))

    return {
        "limit": saturate_at,
        "segments": segments,
        "segment_shift": segment_shift,
        "coefficients": tuple(coefficients),
    }
