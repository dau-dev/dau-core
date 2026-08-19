`default_nettype none

// Shared map/ALU semantics: one definition of the ALU opcodes and both
// pipeline stages (single-cycle ops; product finish for the registered
// multiply) so single-stream and keyed map tiles cannot drift
// (dau_core.operators.MapConfig.apply is the golden; wraparound arithmetic).
//
// THE OPERANDS ARRIVE ALREADY SIGN-EXTENDED to MAP_ELEMENT_BITS and the
// caller TRUNCATES the result back to its own element width, the same shape
// dau_aggregation_pkg uses. That is what lets one package serve tiles at
// several element widths: sign-extending in and truncating out is the same
// wraparound arithmetic one width down, for every opcode here -- add, sub and
// the bitwise pair by construction, shift-left because the low bits of a
// wider shift are the narrower shift, and both multiply forms because an
// arithmetic right shift commutes with sign extension.
//
// MAP_ELEMENT_BITS IS THE FAMILY'S UPPER BOUND, and it is derived rather than
// asserted: MULSHR needs the whole product to shift, so the widest element
// the package can express is half the product path. A tile reading this
// package therefore bounds its element type at MAP_ELEMENT_BITS and says so
// in its own elaboration guard. A wider element is a wider product, which is
// a different package and a different tile.
package dau_map_alu_pkg;
    // the widest element the ALU steps below carry, and the product path that
    // decides it
    localparam int MAP_ELEMENT_BITS = 32;
    localparam int MAP_PRODUCT_BITS = 2 * MAP_ELEMENT_BITS;

    localparam logic [2:0] MAP_OP_ADD = 3'd1;
    localparam logic [2:0] MAP_OP_SUB = 3'd2;
    localparam logic [2:0] MAP_OP_MUL = 3'd3;
    localparam logic [2:0] MAP_OP_MULSHR = 3'd4;
    localparam logic [2:0] MAP_OP_SHL = 3'd5;
    localparam logic [2:0] MAP_OP_AND = 3'd6;
    localparam logic [2:0] MAP_OP_OR = 3'd7;

    // stage 1: ops that finish in one cycle (multiplies only capture operands)
    function automatic logic [MAP_ELEMENT_BITS-1:0] map_cheap_result(
        input logic [2:0] op,
        input logic [MAP_ELEMENT_BITS-1:0] value,
        input logic [MAP_ELEMENT_BITS-1:0] constant_value,
        input logic [4:0] shift_value
    );
        begin
            unique case (op)
                MAP_OP_ADD: map_cheap_result = value + constant_value;
                MAP_OP_SUB: map_cheap_result = value - constant_value;
                MAP_OP_SHL: map_cheap_result = value << shift_value;
                MAP_OP_AND: map_cheap_result = value & constant_value;
                MAP_OP_OR:  map_cheap_result = value | constant_value;
                default: map_cheap_result = '0;
            endcase
        end
    endfunction

    // stage 2: select between the registered product path and the finished
    // single-cycle result
    function automatic logic [MAP_ELEMENT_BITS-1:0] map_finish_sample(
        input logic [2:0] op,
        input logic signed [MAP_PRODUCT_BITS-1:0] product,
        input logic [MAP_ELEMENT_BITS-1:0] cheap,
        input logic [4:0] shift_value
    );
        begin
            unique case (op)
                MAP_OP_MUL: map_finish_sample = product[MAP_ELEMENT_BITS-1:0];
                MAP_OP_MULSHR: map_finish_sample = MAP_ELEMENT_BITS'(product >>> shift_value);
                default: map_finish_sample = cheap;
            endcase
        end
    endfunction
endpackage

`default_nettype wire
