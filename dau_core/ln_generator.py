"""Renders the fixed-point ln tile from a declared operating point.

The datapath is :class:`dau_core.fixed_point.LnDatapath` and the emitted
SystemVerilog mirrors :meth:`LnDatapath.evaluate` step for step, so the
cocotb bench compares against it with zero tolerance like every other tile.
Generated rather than checked in for the same reason exp and the normal CDF
are: the 256-entry tables of ``ln(1 + j/256)`` and ``1/(1 + j/256)`` and the
series coefficients are transcendental constants at the configured internal
precision, and SystemVerilog cannot compute those at elaboration.

ln adds one thing exp does not have: LEADING-ZERO NORMALISATION. ``x`` is
split as ``m * 2**e`` with ``m`` in [1, 2), and ``ln(x) = e*ln2 + ln(m)``.
The exponent half is EXACT integer work -- a priority encode, a barrel shift
and one multiply by a constant -- so only the mantissa series approximates.
That is why the accuracy question is confined to the polynomial: the
registered point is FAITHFUL (measured 1 ULP), and the deviation is a
measured fact recorded in the registry, not a tolerance in the bench.

The lessons the exp tile paid for are repeated here rather than rediscovered:
every product gets its own full-width wire, and every multiply gets its own
pipeline stage.
"""

# NOTE: no `from __future__ import annotations` — ccflow's Flow.call
# inspects the real annotation objects on __call__

from ccflow import Flow

from .fixed_point import LnDatapath
from .generators import CoreGenerator, RenderContext, RenderedCore

__all__ = ("LnTileGenerator",)

# The widest int32 magnitude bit, so the mantissa shift is always a LEFT
# shift: the model branches to a right shift when internal_bits is below the
# input's msb, and an operating point that narrow would need HDL this
# generator does not emit.
_MAX_MSB = 30


def _hex_literal(value: int, width: int) -> str:
    return f"{width}'h{value & ((1 << width) - 1):0{(width + 3) // 4}x}"


class LnTileGenerator(CoreGenerator):
    """Renders ``dau_int32_fixed_point_ln`` for one operating point."""

    module: str = "dau_int32_fixed_point_ln"
    datapath: LnDatapath = LnDatapath()

    @property
    def source_name(self) -> str:
        return f"{self.module}.sv"

    @Flow.call
    def __call__(self, context: RenderContext) -> RenderedCore:  # noqa: ARG002 (ccflow interface)
        point = self.datapath
        constants = point.constants()
        q = point.internal_bits
        entries = 1 << point.table_bits
        # The emitted normalisation and final scale are the model's q >= msb
        # and shift > 0 branches. Refuse rather than render HDL that mirrors
        # the other branch of a model this file claims to reproduce exactly.
        if q <= point.fractional_bits or q < _MAX_MSB:
            raise ValueError(f"the ln tile renders internal_bits > fractional_bits and >= {_MAX_MSB}, got {q} and {point.fractional_bits}")

        # the mantissa's leading one sits at bit q, so it is q+1 bits; the
        # reciprocal of 1 is 2**q exactly, which needs the same width
        mant_width = q + 1
        # signed working width: the series coefficient of v is 2**q, the
        # largest value on the datapath that is not a product
        wide = q + 2

        reciprocal = ",\n".join(f"        {_hex_literal(value, mant_width)}" for value in constants["reciprocal"])
        ln_table = ",\n".join(f"        {_hex_literal(value, wide)}" for value in constants["ln_table"])
        series = ",\n".join(f"        {_hex_literal(constants['series'][k], wide)}" for k in range(point.degree + 1))

        # A product must be formed at FULL width. In SystemVerilog the
        # operands of `a * b` are sized by the expression context, so
        # `WIDE'((acc * v) >>> Q)` evaluates the multiply at WIDE and throws
        # away the high half BEFORE the shift -- which silently produced zero
        # for every input on the exp tile. The products therefore get their
        # own wires, and a shift only ever reads a register that is already
        # full width.
        # EVERY multiply gets its own pipeline stage. Multiplying, shifting
        # and adding in one cycle left the DSP48 without its internal
        # register and measured WNS -4.819 ns at 8 ns on the Artix.
        rounding = " + (PROD_ONE <<< (INTERNAL_BITS - 1))" if point.round_steps else ""
        stages = []
        for step in range(point.degree - 1):
            multiply = 4 + 2 * step
            accumulate = multiply + 1
            term = point.degree - 1 - step
            stages.append(
                f"                stage_prod[{multiply}] <= PROD_WIDTH'(stage_acc[{multiply - 1}]) * PROD_WIDTH'(stage_res[{multiply - 1}]);"
            )
            stages.append(
                f"                stage_acc[{accumulate}] <= WIDE'(WIDE'((stage_prod[{multiply}]{rounding}) >>> INTERNAL_BITS) + SERIES[{term}]);"
            )
        # the last term of the series has no coefficient to fold in: ln(1+v)
        # has no constant term, so the chain ends on a bare multiply
        final_multiply = 2 * point.degree + 2
        stages.append(
            f"                stage_prod[{final_multiply}] <= PROD_WIDTH'(stage_acc[{final_multiply - 1}]) * PROD_WIDTH'(stage_res[{final_multiply - 1}]);"
        )
        stages.append(f"                stage_acc[{final_multiply + 1}] <= WIDE'((stage_prod[{final_multiply}]{rounding}) >>> INTERNAL_BITS);")
        horner_body = "\n".join(stages)

        exponent_stage = final_multiply + 2
        depth = exponent_stage + 3
        prod_one_decl = "    localparam logic signed [PROD_WIDTH-1:0] PROD_ONE = 1;\n" if point.round_steps else ""

        text = f"""`default_nettype none

// GENERATED by dau_core.ln_generator.LnTileGenerator -- do not edit.
//
// Fixed-point natural log over 16-byte quad rows, rendered for ONE operating
// point:
//   internal bits {q}, tables {entries} entries, degree {point.degree},
//   Horner steps {"rounded" if point.round_steps else "truncated"}
//
// This tile is generated because its two {entries}-entry tables -- ln(1 + j/{entries})
// and the reciprocal 1/(1 + j/{entries}) -- and its series coefficients are
// transcendental constants at {q} fractional bits, which SystemVerilog cannot
// compute at elaboration. The registry entry names the generator and the
// operating point; nothing here is hand tuned, and editing this file loses
// the link to the parameters that produced it.
//
// x = m * 2**e with m in [1, 2), so ln(x) = e*ln2 + ln(m). The EXPONENT half
// is exact integer work (priority encode, barrel shift, one multiply by a
// constant); only the mantissa series approximates. The mantissa's top
// TABLE_BITS index the reciprocal table, which reduces the residue to
// |v| <= 1/{entries} before the series -- that table is what keeps the degree low
// without a divider.
//
// Mirrors dau_core.fixed_point.LnDatapath.evaluate step for step, so the
// bench compares with ZERO TOLERANCE. Deviation from correctly rounded ln is
// a MEASURED fact of the operating point, swept over every representable
// input and recorded in the registry -- not a tolerance in the bench. This
// point is FAITHFUL, not correctly rounded.
//
// A non-positive input has no logarithm and closes the batch out with
// ERR_DOMAIN, the position the sqrt tile takes: a substituted value would be
// a wrong number that looks right.
module {self.module} #(
    parameter int INTERNAL_BITS = {q},
    parameter int TABLE_BITS = {point.table_bits},
    parameter int DEGREE = {point.degree},
    /* verilator lint_off UNUSEDPARAM */
    // states the operating point in the source; the rounding it selects is
    // already baked into the emitted Horner steps
    parameter int ROUND_STEPS = {1 if point.round_steps else 0},
    /* verilator lint_on UNUSEDPARAM */
    parameter int FRACTIONAL_BITS = {point.fractional_bits}
) (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic [1:0]  cfg_dst_field,
    input  wire logic [1:0]  cfg_src_field,
    input  wire logic        input_valid,
    output logic             input_ready,
    input  wire logic [63:0] input_data,
    input  wire logic        input_last,
    output logic             output_valid,
    input  wire logic        output_ready,
    output logic [63:0]      output_data,
    output logic             output_last,
    output logic             status_valid,
    input  wire logic        status_ready,
    output logic             status_error,
    output logic [7:0]       status_error_code
);
    localparam int MANT_WIDTH = {mant_width};
    localparam int WIDE = {wide};
    // Products are formed at full width on purpose; see the generator.
    localparam int PROD_WIDTH = 2 * WIDE;
    // the exponent is msb - FRACTIONAL_BITS, so it is in [-31, 30]
    localparam int E_WIDTH = 8;
    // the FULL width of an E_WIDTH exponent times a WIDE constant
    localparam int ELN2_WIDTH = WIDE + E_WIDTH;
    // e*ln2 + ln(1 + j/N) + the series result: three terms, two carry bits
    localparam int TOTAL_WIDTH = ELN2_WIDTH + 2;
    localparam int TABLE_ENTRIES = {entries};
    localparam int SHIFT = INTERNAL_BITS - FRACTIONAL_BITS;

    localparam logic [E_WIDTH-1:0]        Q_BITS = E_WIDTH'(INTERNAL_BITS);
    localparam logic signed [E_WIDTH-1:0] F_BITS = E_WIDTH'(FRACTIONAL_BITS);
    localparam logic signed [WIDE-1:0]    LN2 = {_hex_literal(constants["ln2"], wide)};

    localparam logic [MANT_WIDTH-1:0] RECIPROCAL [TABLE_ENTRIES] = '{{
{reciprocal}
    }};

    localparam logic signed [WIDE-1:0] LN_TABLE [TABLE_ENTRIES] = '{{
{ln_table}
    }};

    // index k holds the coefficient of v**k in ln(1+v) = v - v^2/2 + ...;
    // index 0 is the absent constant term, carried so the indexing matches
    // the model's
    localparam logic signed [WIDE-1:0] SERIES [DEGREE+1] = '{{
{series}
    }};

    localparam logic [7:0] ERR_STREAM = 8'd2;
    localparam logic [7:0] ERR_DOMAIN = 8'd3;

    // the datapath, one multiply per stage:
    //   0 push  1 normalise  2 reciprocal product  3 the residue v, then a
    //   MULTIPLY and an ACCUMULATE stage per Horner step, the bare final
    //   multiply, the e*ln2 product, the exponent sum and the final scale
    localparam int DEPTH = {depth};

    typedef enum logic [1:0] {{ IN_W0, IN_W1, IN_DISCARD }} in_state_t;
    typedef enum logic [1:0] {{ OUT_IDLE, OUT_W0, OUT_W1 }} out_state_t;

    in_state_t  in_state;
    out_state_t out_state;

    logic        batch_open;
    logic [1:0]  active_dst;
    logic [1:0]  active_src;
    logic        status_pending;
    logic [7:0]  pending_error;
    logic [31:0] held_field0;
    logic [31:0] held_field1;

    logic                          stage_valid [DEPTH];
    logic signed [WIDE-1:0]        stage_acc   [DEPTH];
    logic signed [WIDE-1:0]        stage_res   [DEPTH];
    logic signed [PROD_WIDTH-1:0]  stage_prod  [DEPTH];
    logic signed [E_WIDTH-1:0]     stage_exp   [DEPTH];
    logic [TABLE_BITS-1:0]         stage_index [DEPTH];
    logic [31:0]                   stage_field [DEPTH][4];
    logic [1:0]                    stage_dst   [DEPTH];
    logic                          stage_last  [DEPTH];

    // Values that live for exactly ONE stage are single registers rather
    // than DEPTH-deep arrays: the pipeline advances as a unit, so a register
    // written on one advance and read on the next IS a stage, and the copies
    // the array form would carry are dead the moment they are made.
    logic [E_WIDTH-1:0]             msb_reg;
    logic [MANT_WIDTH-1:0]          mant_reg;
    logic signed [ELN2_WIDTH-1:0]   eln2_reg;
    logic signed [TOTAL_WIDTH-1:0]  partial_reg;

    logic [31:0] emit_field [4];
    logic        emit_last;

    logic input_fire;
    logic output_fire;
    logic retire_valid;
    logic pipe_advance;
    logic pipe_push;
    logic [31:0] in_field [4];
    logic signed [31:0] src_value;
    logic domain_error;

    assign in_field[0] = held_field0;
    assign in_field[1] = held_field1;
    assign in_field[2] = input_data[31:0];
    assign in_field[3] = input_data[63:32];
    assign src_value = signed'(in_field[active_src]);
    // ln of zero or a negative has no value; the batch is invalidated
    assign domain_error = (src_value <= 32'sd0);

    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign retire_valid = stage_valid[DEPTH-1];
    assign pipe_advance = !retire_valid || (out_state == OUT_IDLE);
    assign input_ready = !status_pending && ((in_state == IN_W0) || (in_state == IN_DISCARD) || ((in_state == IN_W1) && pipe_advance));
    assign pipe_push = (in_state == IN_W1) && input_fire && !domain_error;

    assign output_valid = (out_state == OUT_W0) || (out_state == OUT_W1);
    assign output_data = (out_state == OUT_W0) ? {{emit_field[1], emit_field[0]}} : {{emit_field[3], emit_field[2]}};
    assign output_last = (out_state == OUT_W1) && emit_last;
    assign status_valid = status_pending;
    assign status_error = 1'b1;
    assign status_error_code = pending_error;
{prod_one_decl}
    // Leading-zero normalisation, the part exp does not have. msb_index is
    // the model's x.bit_length() - 1: the highest set bit of a POSITIVE
    // int32, so only 31 bits are scanned and the last match wins.
    logic [E_WIDTH-1:0] msb_index;
    always_comb begin
        msb_index = '0;
        for (int unsigned b = 0; b < 31; b++) begin
            if (src_value[b]) msb_index = E_WIDTH'(b);
        end
    end

    // msb_index is at most {_MAX_MSB}, so reading it as signed is positive
    logic signed [E_WIDTH-1:0] exponent_value;
    assign exponent_value = signed'(msb_index) - F_BITS;

    // stage 1: x scaled so its leading one lands on bit INTERNAL_BITS, which
    // is m in [1, 2) at INTERNAL_BITS fractional bits. Stages read the x
    // CARRIED in the pipeline, never the live input port -- reading the port
    // works for a single parked row and produces garbage the moment two rows
    // are in flight.
    logic [E_WIDTH-1:0]    shift_amount;
    logic [MANT_WIDTH-1:0] normalised;
    assign shift_amount = Q_BITS - msb_reg;
    assign normalised = MANT_WIDTH'(unsigned'(stage_res[0])) << shift_amount;

    // stage 2: the mantissa's top TABLE_BITS, below its implicit leading one
    logic [TABLE_BITS-1:0] mant_index;
    assign mant_index = mant_reg[INTERNAL_BITS-1 -: TABLE_BITS];
    logic signed [PROD_WIDTH-1:0] recip_product;
    assign recip_product = PROD_WIDTH'(signed'({{1'b0, mant_reg}})) * PROD_WIDTH'(signed'({{1'b0, RECIPROCAL[mant_index]}}));

    // the exponent term, EXACT: a small signed integer times a constant
    logic signed [ELN2_WIDTH-1:0] eln2_product;
    assign eln2_product = ELN2_WIDTH'(stage_exp[{exponent_stage - 1}]) * ELN2_WIDTH'(LN2);

    // the final scale, rounded like the model's last step. ln of any
    // positive int32 lands in about +/-2**20, so the rounded result is WIDE
    // by a wide margin and there is no range error to raise.
    logic signed [TOTAL_WIDTH-1:0] total_sum;
    logic signed [WIDE-1:0] rounded;
    assign total_sum = partial_reg + TOTAL_WIDTH'(stage_acc[{exponent_stage + 1}]);
    assign rounded = WIDE'((total_sum + (TOTAL_WIDTH'(signed'(1)) <<< (SHIFT - 1))) >>> SHIFT);

    always_ff @(posedge clk) begin
        if (rst) begin
            in_state <= IN_W0;
            out_state <= OUT_IDLE;
            batch_open <= 1'b0;
            status_pending <= 1'b0;
            pending_error <= 8'd0;
            emit_last <= 1'b0;
            for (int unsigned i = 0; i < DEPTH; i++) stage_valid[i] <= 1'b0;
        end else begin
            if (pipe_advance) begin
                stage_valid[0] <= pipe_push;
                if (pipe_push) begin
                    stage_last[0] <= input_last;
                    stage_dst[0] <= active_dst;
                    stage_acc[0] <= SERIES[DEGREE];
                    stage_res[0] <= WIDE'(src_value);   // carries x until v exists
                    stage_exp[0] <= exponent_value;
                    msb_reg <= msb_index;
                    for (int unsigned f = 0; f < 4; f++) stage_field[0][f] <= in_field[f];
                end

                for (int unsigned i = 1; i < DEPTH; i++) begin
                    stage_valid[i] <= stage_valid[i-1];
                    stage_last[i] <= stage_last[i-1];
                    stage_dst[i] <= stage_dst[i-1];
                    stage_acc[i] <= stage_acc[i-1];
                    stage_res[i] <= stage_res[i-1];
                    stage_prod[i] <= stage_prod[i-1];
                    stage_exp[i] <= stage_exp[i-1];
                    stage_index[i] <= stage_index[i-1];
                    for (int unsigned f = 0; f < 4; f++) stage_field[i][f] <= stage_field[i-1][f];
                end

                // stage 1 override: the normalised mantissa
                mant_reg <= normalised;
                // stage 2 overrides: the reciprocal product and the table index
                stage_prod[2] <= recip_product;
                stage_index[2] <= mant_index;
                // stage 3 override: v = m/(1 + j/N) - 1, bounded by 1/N
                stage_res[3] <= WIDE'(WIDE'(stage_prod[2] >>> INTERNAL_BITS) - (WIDE'(signed'(1)) <<< INTERNAL_BITS));
{horner_body}
                // the exponent product, then the exact sum, then the scale
                eln2_reg <= eln2_product;
                partial_reg <= TOTAL_WIDTH'(eln2_reg) + TOTAL_WIDTH'(LN_TABLE[stage_index[{exponent_stage}]]);
                stage_acc[{exponent_stage + 2}] <= rounded;
            end

            case (in_state)
                IN_W0:
                    if (input_fire) begin
                        if (input_last) begin
                            pending_error <= ERR_STREAM;
                            status_pending <= 1'b1;
                        end else begin
                            held_field0 <= input_data[31:0];
                            held_field1 <= input_data[63:32];
                            if (!batch_open) begin
                                batch_open <= 1'b1;
                                active_dst <= cfg_dst_field;
                                active_src <= cfg_src_field;
                            end
                            in_state <= IN_W1;
                        end
                    end

                IN_W1:
                    if (input_fire) begin
                        if (domain_error) begin
                            pending_error <= ERR_DOMAIN;
                            if (input_last) status_pending <= 1'b1;
                            else in_state <= IN_DISCARD;
                        end else begin
                            in_state <= IN_W0;
                            if (input_last) batch_open <= 1'b0;
                        end
                    end

                IN_DISCARD:
                    if (input_fire && input_last) status_pending <= 1'b1;

                default: in_state <= IN_W0;
            endcase

            case (out_state)
                OUT_IDLE:
                    if (retire_valid) begin
                        for (int unsigned f = 0; f < 4; f++) emit_field[f] <= stage_field[DEPTH-1][f];
                        emit_field[stage_dst[DEPTH-1]] <= 32'(stage_acc[DEPTH-1]);
                        emit_last <= stage_last[DEPTH-1];
                        out_state <= OUT_W0;
                    end

                OUT_W0: if (output_fire) out_state <= OUT_W1;
                OUT_W1: if (output_fire) out_state <= OUT_IDLE;
                default: out_state <= OUT_IDLE;
            endcase

            if (status_pending && status_ready) begin
                // the error close-out invalidates the whole batch, so rows of
                // it still in flight are dropped rather than emitted
                status_pending <= 1'b0;
                in_state <= IN_W0;
                batch_open <= 1'b0;
                for (int unsigned i = 0; i < DEPTH; i++) stage_valid[i] <= 1'b0;
            end
        end
    end
endmodule

`default_nettype wire
"""
        return RenderedCore(
            module=self.module,
            source_name=self.source_name,
            text=text,
            parameters={
                "INTERNAL_BITS": q,
                "TABLE_BITS": point.table_bits,
                "DEGREE": point.degree,
                "ROUND_STEPS": int(point.round_steps),
                "FRACTIONAL_BITS": point.fractional_bits,
            },
        )
