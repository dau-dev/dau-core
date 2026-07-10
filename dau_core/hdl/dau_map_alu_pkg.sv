`default_nettype none

// Shared map/ALU semantics: one definition of the ALU opcodes and both
// pipeline stages (single-cycle ops; product finish for the registered
// multiply) so single-stream and keyed map tiles cannot drift
// (dau_core.operators.MapConfig.apply is the golden; INT32 wraparound).
package dau_map_alu_pkg;
    localparam logic [2:0] MAP_OP_ADD = 3'd1;
    localparam logic [2:0] MAP_OP_SUB = 3'd2;
    localparam logic [2:0] MAP_OP_MUL = 3'd3;
    localparam logic [2:0] MAP_OP_MULSHR = 3'd4;
    localparam logic [2:0] MAP_OP_SHL = 3'd5;
    localparam logic [2:0] MAP_OP_AND = 3'd6;
    localparam logic [2:0] MAP_OP_OR = 3'd7;

    // stage 1: ops that finish in one cycle (multiplies only capture operands)
    function automatic logic [31:0] map_cheap_result(
        input logic [2:0] op,
        input logic [31:0] value,
        input logic [31:0] constant_value,
        input logic [4:0] shift_value
    );
        begin
            unique case (op)
                MAP_OP_ADD: map_cheap_result = value + constant_value;
                MAP_OP_SUB: map_cheap_result = value - constant_value;
                MAP_OP_SHL: map_cheap_result = value << shift_value;
                MAP_OP_AND: map_cheap_result = value & constant_value;
                MAP_OP_OR:  map_cheap_result = value | constant_value;
                default: map_cheap_result = 32'd0;
            endcase
        end
    endfunction

    // stage 2: select between the registered product path and the finished
    // single-cycle result
    function automatic logic [31:0] map_finish_sample(
        input logic [2:0] op,
        input logic signed [63:0] product,
        input logic [31:0] cheap,
        input logic [4:0] shift_value
    );
        begin
            unique case (op)
                MAP_OP_MUL: map_finish_sample = product[31:0];
                MAP_OP_MULSHR: map_finish_sample = 32'(product >>> shift_value);
                default: map_finish_sample = cheap;
            endcase
        end
    endfunction
endpackage

`default_nettype wire
