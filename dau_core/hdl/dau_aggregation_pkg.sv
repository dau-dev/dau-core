`default_nettype none

// Shared aggregation step semantics. Single source of truth for the
// accumulate step used by the aggregation tile and the stream modules;
// matches the dau_core.operators golden semantics for MIN/MAX/SUM/COUNT.
//
// THE SAMPLE ARRIVES ALREADY SIGN-EXTENDED to 64 bits. It used to arrive as a
// bare 32-bit value that this function widened, which fixed every caller at
// int32 for no reason: the accumulator is INT64 either way, and the only
// width-dependent step is the extension, which the caller is the one that
// knows the element's type. Widening at the call site is what lets the three
// descriptor tiles carry an element-type parameter without a second package.
package dau_aggregation_pkg;

    localparam logic [2:0] DAU_OP_MIN   = 3'd1;
    localparam logic [2:0] DAU_OP_MAX   = 3'd2;
    localparam logic [2:0] DAU_OP_SUM   = 3'd3;
    localparam logic [2:0] DAU_OP_COUNT = 3'd4;

    function automatic logic signed [63:0] aggregate_next(
        input logic [2:0] opcode,
        input logic signed [63:0] current_value,
        input logic [63:0] current_count,
        input logic signed [63:0] sample_value,
        input logic [63:0] next_count
    );
        begin
            unique case (opcode)
                DAU_OP_MIN: aggregate_next = (current_count == 64'd0) ? sample_value : ((sample_value < current_value) ? sample_value : current_value);
                DAU_OP_MAX: aggregate_next = (current_count == 64'd0) ? sample_value : ((sample_value > current_value) ? sample_value : current_value);
                DAU_OP_SUM: aggregate_next = (current_count == 64'd0) ? sample_value : (current_value + sample_value);
                DAU_OP_COUNT: aggregate_next = $signed(next_count);
                default: aggregate_next = 64'sd0;
            endcase
        end
    endfunction

endpackage

`default_nettype wire
