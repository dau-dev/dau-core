"""Renders the fixed-point exp tile from a declared operating point.

The datapath is :class:`dau_core.fixed_point.ExpDatapath` and the emitted
SystemVerilog mirrors :meth:`ExpDatapath.evaluate` step for step, so the
cocotb bench can compare against it with zero tolerance like every other
tile. The operating point -- internal precision, table size, polynomial
degree, whether Horner steps round -- arrives as configuration, which is why
this is a generator and not a checked-in file: the 256-entry table of
``2**(j/256)`` is a function of those parameters and SystemVerilog cannot
compute it.

The datapath is PIPELINED, one multiply per stage, chosen after measuring
what a multiply costs rather than from the estimate this docstring used to
quote. Measured on the dpv1 Artix: 52 DSP48s at the exact point and 22 at
the light one, against 740 on the part. Folding onto a shared multiplier
would cut that to roughly a ninth, but at one row per nine cycles against
sqrt's and divide's one per three -- the tile would become the bottleneck of
any chain it joined.
"""

# NOTE: no `from __future__ import annotations` — ccflow's Flow.call
# inspects the real annotation objects on __call__

from ccflow import Flow

from .fixed_point import ExpDatapath, exp_domain
from .generators import CoreGenerator, RenderContext, RenderedCore

__all__ = ("ExpTileGenerator",)


def _hex_literal(value: int, width: int) -> str:
    return f"{width}'h{value & ((1 << width) - 1):0{(width + 3) // 4}x}"


class ExpTileGenerator(CoreGenerator):
    """Renders ``dau_fixed_point_exp`` for one operating point."""

    module: str = "dau_fixed_point_exp"
    datapath: ExpDatapath = ExpDatapath()

    @property
    def source_name(self) -> str:
        return f"{self.module}.sv"

    @Flow.call
    def __call__(self, context: RenderContext) -> RenderedCore:  # noqa: ARG002 (ccflow interface)
        point = self.datapath
        constants = point.constants()
        q = point.internal_bits
        entries = 1 << point.table_bits
        # the table and the coefficients are just under q+2 bits (values sit
        # in [1, 2)); the reciprocal of ln2 scaled by the table size is wider
        wide = q + 2
        # the NARROW reciprocal, matching ExpDatapath.evaluate. Emitting the
        # full-precision one here while the model had moved to the narrow
        # constant put k out by 2**(internal_bits - k_bits).
        inv_width = constants["inv_ln2_narrow"].bit_length() + 1

        table = ",\n".join(f"        {_hex_literal(value, wide)}" for value in constants["table"])
        coefficients = ",\n".join(f"        {_hex_literal(value, wide)}" for value in constants["coefficients"])

        # k reaches about +/-4400 over the useful domain; the extra bits keep
        # the intermediate shifts clear of it
        k_width = 20
        _low, high = exp_domain(fractional_bits=point.fractional_bits)
        # A product must be formed at FULL width. In SystemVerilog the
        # operands of `a * b` are sized by the expression context, so
        # `WIDE'((acc * res) >>> Q)` evaluates the multiply at WIDE and throws
        # away the high half BEFORE the shift -- which silently produced zero
        # for every input. The products therefore get their own wires.
        # EVERY multiply gets its own pipeline stage. Multiplying, shifting and
        # adding in one cycle left the DSP48 without its internal register and
        # measured WNS -4.819 ns at 8 ns on the Artix -- a ~12.8 ns path. The
        # product is registered, so each Horner step costs two stages.
        rounding = " + (PROD_ONE <<< (INTERNAL_BITS - 1))" if point.round_steps else ""
        stages = []
        for step in range(point.degree):
            multiply = 4 + 2 * step
            accumulate = multiply + 1
            coefficient = point.degree - 1 - step
            stages.append(
                f"                stage_prod[{multiply}] <= PROD_WIDTH'(stage_acc[{multiply - 1}]) * PROD_WIDTH'(stage_res[{multiply - 1}]);"
            )
            stages.append(
                f"                stage_acc[{accumulate}] <= WIDE'(WIDE'("
                f"(stage_prod[{multiply}]{rounding}) >>> INTERNAL_BITS)"
                f" + signed'({{1'b0, COEFFICIENT[{coefficient}]}}));"
            )
        table_multiply = 4 + 2 * point.degree
        stages.append(
            f"                stage_prod[{table_multiply}] <= PROD_WIDTH'(stage_acc[{table_multiply - 1}])"
            f" * PROD_WIDTH'(signed'({{1'b0, TABLE[stage_k[{table_multiply - 1}][TABLE_BITS-1:0]]}}));"
        )
        stages.append(f"                stage_acc[{table_multiply + 1}] <= WIDE'((stage_prod[{table_multiply}]{rounding}) >>> INTERNAL_BITS);")
        stages.append(f"                stage_acc[{table_multiply + 2}] <= scaled;")
        horner_body = "\n".join(stages)
        depth = table_multiply + 3
        scale_source = table_multiply + 1
        prod_one_decl = "    localparam logic signed [PROD_WIDTH-1:0] PROD_ONE = 1;\n" if point.round_steps else ""

        text = f"""`default_nettype none

// GENERATED by dau_core.exp_generator.ExpTileGenerator -- do not edit.
//
// Fixed-point exp over 16-byte quad rows, rendered for ONE operating point:
//   internal bits {q}, table {entries} entries, degree {point.degree},
//   Horner steps {"rounded" if point.round_steps else "truncated"}
//
// This tile is generated because its {entries}-entry table of 2**(j/{entries}) and
// its polynomial coefficients are transcendental constants at {q} fractional
// bits, and SystemVerilog cannot compute those at elaboration. The registry
// entry names the generator and the operating point; nothing here is hand
// tuned, and editing this file loses the link to the parameters that
// produced it.
//
// Mirrors dau_core.fixed_point.ExpDatapath.evaluate step for step, so the
// bench compares with zero tolerance. Deviation from correctly rounded exp
// is a MEASURED fact of the operating point, swept over every representable
// input and recorded in the registry beside the resource envelope -- not a
// tolerance in the bench.
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
    localparam int WIDE = {wide};
    localparam int INV_WIDTH = {inv_width};
    localparam int K_WIDTH = {k_width};
    localparam int K_BITS = {point.k_bits};
    localparam int TABLE_ENTRIES = {entries};

    localparam logic [INV_WIDTH-1:0] INV_LN2 = {_hex_literal(constants["inv_ln2_narrow"], inv_width)};
    localparam logic [WIDE-1:0] LN2 = {_hex_literal(constants["ln2"], wide)};

    localparam logic [WIDE-1:0] TABLE [TABLE_ENTRIES] = '{{
{table}
    }};

    localparam logic [WIDE-1:0] COEFFICIENT [DEGREE+1] = '{{
{coefficients}
    }};

    localparam logic [7:0] ERR_STREAM = 8'd2;
    localparam logic [7:0] ERR_RANGE  = 8'd4;

    // exp of anything above this does not fit int32; below the low bound the
    // correctly rounded answer is 0, which the final shift produces on its
    // own -- an underflow is an answer, not an error.
    localparam logic signed [31:0] MAX_INPUT = 32'sd{high};

    // the datapath, one multiply per stage:
    //   0 push  1 k  2 shifted ln2 product  3 residue, then a MULTIPLY stage and an ACCUMULATE
    //   stage per Horner step, the table multiply, and the final scale
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

    logic                     stage_valid [DEPTH];
    logic signed [WIDE-1:0]   stage_acc   [DEPTH];
    logic signed [WIDE-1:0]   stage_res   [DEPTH];
    logic signed [K_WIDTH-1:0] stage_k    [DEPTH];
    logic [31:0]              stage_field [DEPTH][4];
    logic [1:0]               stage_dst   [DEPTH];
    logic                     stage_last  [DEPTH];

    logic [31:0] emit_field [4];
    logic        emit_last;

    logic input_fire;
    logic output_fire;
    logic retire_valid;
    logic pipe_advance;
    logic pipe_push;
    logic [31:0] in_field [4];
    logic signed [31:0] src_value;
    logic range_error;

    assign in_field[0] = held_field0;
    assign in_field[1] = held_field1;
    assign in_field[2] = input_data[31:0];
    assign in_field[3] = input_data[63:32];
    assign src_value = signed'(in_field[active_src]);
    assign range_error = (src_value > MAX_INPUT);

    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign retire_valid = stage_valid[DEPTH-1];
    assign pipe_advance = !retire_valid || (out_state == OUT_IDLE);
    assign input_ready = !status_pending && ((in_state == IN_W0) || (in_state == IN_DISCARD) || ((in_state == IN_W1) && pipe_advance));
    assign pipe_push = (in_state == IN_W1) && input_fire && !range_error;

    assign output_valid = (out_state == OUT_W0) || (out_state == OUT_W1);
    assign output_data = (out_state == OUT_W0) ? {{emit_field[1], emit_field[0]}} : {{emit_field[3], emit_field[2]}};
    assign output_last = (out_state == OUT_W1) && emit_last;
    assign status_valid = status_pending;
    assign status_error = 1'b1;
    assign status_error_code = pending_error;

    // Products are formed at full width on purpose; see the generator.
    localparam int PROD_WIDTH = 2 * WIDE;
{prod_one_decl}    logic signed [PROD_WIDTH-1:0] stage_prod [DEPTH];
    logic signed [RES_WIDTH-1:0]  stage_ln2  [DEPTH];

    // Stages read the x CARRIED in the pipeline, never the live input
    // port. Reading src_value here worked for a single parked row and
    // produced garbage the moment two rows were in flight, because each
    // stage then saw a different row's operand.
    // stage 1: the reduction index, from a NARROW constant on purpose
    localparam int KP_WIDTH = 32 + INV_WIDTH;
    logic signed [KP_WIDTH-1:0] k_product;
    assign k_product = KP_WIDTH'(stage_res[0]) * KP_WIDTH'(signed'({{1'b0, INV_LN2}}));

    // stage 2: the residue, x scaled up minus k units of ln2/TABLE_ENTRIES
    localparam int RES_WIDTH = WIDE + K_WIDTH + 1;
    logic signed [RES_WIDTH-1:0] ln2_product;
    // the residue is bounded by ln2/(2*TABLE_ENTRIES), so it fits WIDE by
    // construction and the high bits are deliberately dropped
    /* verilator lint_off UNUSEDSIGNAL */
    logic signed [RES_WIDTH-1:0] residue_full;
    /* verilator lint_on UNUSEDSIGNAL */
    assign ln2_product = RES_WIDTH'(stage_k[1]) * RES_WIDTH'(signed'({{1'b0, LN2}}));
    assign residue_full = (RES_WIDTH'(stage_res[2]) <<< (INTERNAL_BITS - FRACTIONAL_BITS)) - stage_ln2[2];

    // the final scale: 2**e comes out as a shift, e = k >>> TABLE_BITS
    logic signed [K_WIDTH-1:0] final_e;
    logic [7:0] final_shift;
    logic signed [WIDE-1:0] scaled;
    assign final_e = stage_k[{scale_source}] >>> TABLE_BITS;
    assign final_shift = 8'((INTERNAL_BITS - FRACTIONAL_BITS) - final_e);
    // The rounding term must be added in a WIDER type: at the bottom of the
    // domain the shift reaches WIDE-1, and acc + (1 << (WIDE-2)) overflows a
    // signed WIDE, which wrapped a small positive answer to -1.
    localparam int SCALE_WIDTH = WIDE + 2;
    logic signed [SCALE_WIDTH-1:0] scale_sum;
    assign scale_sum = SCALE_WIDTH'(stage_acc[{scale_source}]) + (SCALE_WIDTH'(signed'(1)) <<< (final_shift - 8'd1));
    // beyond SCALE_WIDTH the rounded result is zero, which is the correctly
    // rounded answer for an underflow, not an error
    assign scaled = (final_shift >= 8'(SCALE_WIDTH)) ? WIDE'(signed'(0)) : WIDE'(scale_sum >>> final_shift);

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
                    stage_acc[0] <= WIDE'(signed'({{1'b0, COEFFICIENT[DEGREE]}}));
                    stage_res[0] <= WIDE'(src_value);   // carries x until the residue exists
                    stage_k[0] <= '0;
                    for (int unsigned f = 0; f < 4; f++) stage_field[0][f] <= in_field[f];
                end

                for (int unsigned i = 1; i < DEPTH; i++) begin
                    stage_valid[i] <= stage_valid[i-1];
                    stage_last[i] <= stage_last[i-1];
                    stage_dst[i] <= stage_dst[i-1];
                    stage_k[i] <= stage_k[i-1];
                    stage_acc[i] <= stage_acc[i-1];
                    stage_prod[i] <= stage_prod[i-1];
                    stage_res[i] <= stage_res[i-1];
                    for (int unsigned f = 0; f < 4; f++) stage_field[i][f] <= stage_field[i-1][f];
                end

                // stage 1 overrides: k
                // truncated, not rounded: the rounding add spanned the whole
                // product and measured as a 13-deep CARRY4 chain between two
                // DSPs, which was the tile's critical path
                stage_k[1] <= K_WIDTH'(k_product >>> (K_BITS + FRACTIONAL_BITS));
                // stage 2 overrides: the residue replaces the carried x
                // the shifted product is registered, so the subtract no longer
                // sits combinationally between two DSP48s (clock-to-out ~3.5 ns
                // leaves no room for a carry chain at 8 ns)
                stage_ln2[2] <= ln2_product >>> TABLE_BITS;
                stage_res[3] <= WIDE'(residue_full);
{horner_body}
                // the table multiply, then the scale
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
                        if (range_error) begin
                            pending_error <= ERR_RANGE;
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
