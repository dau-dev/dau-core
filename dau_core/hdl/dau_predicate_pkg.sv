`default_nettype none

// Shared predicate semantics for every filter tile: one definition of the
// comparison opcodes and the two-term combine so single-stream and keyed
// filters cannot drift (dau_core.operators.FilterPredicate is the golden).
package dau_predicate_pkg;
    localparam logic [2:0] OP_EQ = 3'd1;
    localparam logic [2:0] OP_NE = 3'd2;
    localparam logic [2:0] OP_LT = 3'd3;
    localparam logic [2:0] OP_LE = 3'd4;
    localparam logic [2:0] OP_GT = 3'd5;
    localparam logic [2:0] OP_GE = 3'd6;

    localparam logic [1:0] COMBINE_SINGLE = 2'd0;
    localparam logic [1:0] COMBINE_AND = 2'd1;
    localparam logic [1:0] COMBINE_OR = 2'd2;

    function automatic logic predicate_op_valid(input logic [2:0] op);
        begin
            predicate_op_valid = (op >= OP_EQ) && (op <= OP_GE);
        end
    endfunction

    // Compares at the WIDEST field width the palette supports, so one
    // comparison serves int8/int16/int32/int64 tiles alike.
    //
    // Callers MUST sign-extend their field to this width -- `64'(signed'(f))`
    // where f is FIELD_WIDTH bits. A narrow UNSIGNED vector passed here
    // zero-extends, which silently turns every negative field into a large
    // positive one and makes LT/GE wrong for exactly the values a signed
    // comparison exists to handle.
    function automatic logic predicate_compare(input logic [2:0] op, input logic signed [63:0] value, input logic signed [63:0] constant_value);
        begin
            unique case (op)
                OP_EQ: predicate_compare = value == constant_value;
                OP_NE: predicate_compare = value != constant_value;
                OP_LT: predicate_compare = value < constant_value;
                OP_LE: predicate_compare = value <= constant_value;
                OP_GT: predicate_compare = value > constant_value;
                OP_GE: predicate_compare = value >= constant_value;
                default: predicate_compare = 1'b0;
            endcase
        end
    endfunction

    function automatic logic predicate_matches(
        input logic [2:0] op0,
        input logic [31:0] constant0,
        input logic [1:0] combine,
        input logic [2:0] op1,
        input logic [31:0] constant1,
        input logic [31:0] value
    );
        logic first;
        logic second;
        begin
            // sign-extend explicitly: these operands are 32-bit UNSIGNED
            // vectors, and letting them widen implicitly would zero-extend
            first = predicate_compare(op0, 64'(signed'(value)), 64'(signed'(constant0)));
            second = predicate_compare(op1, 64'(signed'(value)), 64'(signed'(constant1)));
            unique case (combine)
                COMBINE_AND: predicate_matches = first && second;
                COMBINE_OR: predicate_matches = first || second;
                default: predicate_matches = first;
            endcase
        end
    endfunction
endpackage

`default_nettype wire
