`default_nettype none

// Shared INT32 aggregation step semantics. Single source of truth for the
// accumulate step used by the aggregation tile and the stream modules;
// matches dau_core.aggregations golden semantics for MIN/MAX/SUM/COUNT.
package dau_aggregation_pkg;

    localparam logic [2:0] DAU_OP_MIN   = 3'd1;
    localparam logic [2:0] DAU_OP_MAX   = 3'd2;
    localparam logic [2:0] DAU_OP_SUM   = 3'd3;
    localparam logic [2:0] DAU_OP_COUNT = 3'd4;

    function automatic logic signed [63:0] aggregate_next(
        input logic [2:0] opcode,
        input logic signed [63:0] current_value,
        input logic [63:0] current_count,
        input logic signed [31:0] sample,
        input logic [63:0] next_count
    );
        logic signed [63:0] sample_value;
        begin
            sample_value = {{32{sample[31]}}, sample};
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
