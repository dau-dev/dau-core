`default_nettype none

// Wide-lane row-atomic compound predicate filter: SLOTS whole 16-byte quad
// rows per beat, slot s at [128*s +: 128], with each row laid out as
// {aux1, aux0, value, key} (field i at bits [32*i +: 32]). The reserved
// ALL-ONES 128-bit row is the PAD: pads pass unchanged and are neither
// evaluated nor counted. A non-pad row that fails the three-term predicate
// is dropped by replacing its slot with PAD — positions are preserved and
// no compaction occurs. A globally stalled three-stage pipeline preserves
// elastic valid/ready flow; input_last travels with its beat.
//
// Predicate config and selected_count semantics match the 64-bit row tile.
// Config latches on the first beat of each batch; invalid config drains the
// batch and reports ERR_CONFIG. Whole-row beats cannot tear, so there is no
// ERR_STREAM path. This composed-pipeline stage is silent on success; even
// an all-dropped batch forwards its final beat as pads, leaving downstream
// responsible for the success close-out.
module dau_int32_wide_row_predicate_filter
    import dau_predicate_pkg::*;
#(
    parameter int SLOTS = 4  // quad rows per beat: 1, 2, or 4
) (
    input  wire logic                         clk,
    input  wire logic                         rst,
    input  wire logic [1:0]                   cfg_field0,
    input  wire logic [2:0]                   cfg_op0,
    input  wire logic [31:0]                  cfg_constant0,
    input  wire logic [1:0]                   cfg_combine01,
    input  wire logic [1:0]                   cfg_field1,
    input  wire logic [2:0]                   cfg_op1,
    input  wire logic [31:0]                  cfg_constant1,
    input  wire logic [1:0]                   cfg_combine2,
    input  wire logic [1:0]                   cfg_field2,
    input  wire logic [2:0]                   cfg_op2,
    input  wire logic [31:0]                  cfg_constant2,
    input  wire logic                         input_valid,
    output logic                              input_ready,
    input  wire logic [SLOTS*128-1:0]         input_data,
    input  wire logic                         input_last,
    output logic                              output_valid,
    input  wire logic                         output_ready,
    output logic [SLOTS*128-1:0]              output_data,
    output logic                              output_last,
    output logic                              status_valid,
    input  wire logic                         status_ready,
    output logic                              status_error,
    output logic [7:0]                        status_error_code,
    output logic [63:0]                       selected_count
);
    if (!(SLOTS == 1 || SLOTS == 2 || SLOTS == 4)) begin : g_bad_slots
        $error("SLOTS must be 1, 2, or 4");
    end

    localparam logic [7:0] ERR_CONFIG = 8'd1;
    localparam logic [127:0] PAD_ROW = {128{1'b1}};

    typedef enum logic [1:0] {
        STATE_FILTER,  // accept and filter one beat of SLOTS rows per cycle
        STATE_DISCARD, // bad config: consume the batch to input_last
        STATE_STATUS   // hold an error close-out until accepted
    } state_t;

    state_t state;

    logic        batch_open;
    logic        batch_input_closed;
    logic [1:0]  active_field0;
    logic [2:0]  active_op0;
    logic [31:0] active_constant0;
    logic [1:0]  active_combine01;
    logic [1:0]  active_field1;
    logic [2:0]  active_op1;
    logic [31:0] active_constant1;
    logic [1:0]  active_combine2;
    logic [1:0]  active_field2;
    logic [2:0]  active_op2;
    logic [31:0] active_constant2;

    logic advance;
    logic input_fire;
    logic output_fire;
    logic first_beat;
    logic config_ok;
    logic accepted_filter_beat;

    logic                         stage1_valid;
    logic [SLOTS*128-1:0]         stage1_data;
    logic                         stage1_last;
    logic [SLOTS-1:0]             stage1_live;
    logic [SLOTS*32-1:0]          stage1_field0;
    logic [SLOTS*32-1:0]          stage1_field1;
    logic [SLOTS*32-1:0]          stage1_field2;

    logic                         stage2_valid;
    logic [SLOTS*128-1:0]         stage2_data;
    logic                         stage2_last;
    logic [SLOTS-1:0]             stage2_live;
    logic [SLOTS-1:0]             stage2_match;
    logic [SLOTS-1:0]             stage1_match;
    logic [SLOTS*128-1:0]         stage2_filtered_data;
    logic [2:0]                   stage2_selected_count;

    assign advance = !output_valid || output_ready;
    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign input_ready = advance && (((state == STATE_FILTER) && !batch_input_closed)
        || (state == STATE_DISCARD));

    assign first_beat = !batch_open;
    assign accepted_filter_beat = input_fire && (state == STATE_FILTER)
        && (!first_beat || config_ok);

    assign config_ok = predicate_op_valid(cfg_op0)
        && (cfg_combine01 <= COMBINE_OR)
        && (cfg_combine2 <= COMBINE_OR)
        && ((cfg_combine01 == COMBINE_SINGLE) || predicate_op_valid(cfg_op1))
        && ((cfg_combine2 == COMBINE_SINGLE) || predicate_op_valid(cfg_op2));

    function automatic logic [31:0] field_select(input logic [1:0] sel, input logic [127:0] row);
        begin
            unique case (sel)
                2'd0: field_select = row[31:0];
                2'd1: field_select = row[63:32];
                2'd2: field_select = row[95:64];
                default: field_select = row[127:96];
            endcase
        end
    endfunction

    function automatic logic selected_fields_match(
        input logic [31:0] value0,
        input logic [31:0] value1,
        input logic [31:0] value2
    );
        logic term0;
        logic term1;
        logic term2;
        logic level01;
        begin
            term0 = predicate_compare(active_op0, value0, active_constant0);
            term1 = predicate_compare(active_op1, value1, active_constant1);
            term2 = predicate_compare(active_op2, value2, active_constant2);
            level01 = (active_combine01 == COMBINE_AND) ? (term0 && term1)
                : (active_combine01 == COMBINE_OR) ? (term0 || term1)
                : term0;
            selected_fields_match = (active_combine2 == COMBINE_AND) ? (level01 && term2)
                : (active_combine2 == COMBINE_OR) ? (level01 || term2)
                : level01;
        end
    endfunction

    always_comb begin
        stage1_match = '0;
        for (int s = 0; s < SLOTS; s++) begin
            stage1_match[s] = selected_fields_match(
                stage1_field0[32*s +: 32],
                stage1_field1[32*s +: 32],
                stage1_field2[32*s +: 32]
            );
        end
    end

    always_comb begin
        stage2_filtered_data = stage2_data;
        stage2_selected_count = 3'd0;
        for (int s = 0; s < SLOTS; s++) begin
            if (stage2_live[s]) begin
                if (stage2_match[s]) begin
                    stage2_selected_count = stage2_selected_count + 3'd1;
                end else begin
                    stage2_filtered_data[128*s +: 128] = PAD_ROW;
                end
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            if (advance) begin
                output_valid <= stage2_valid;
                output_data <= stage2_filtered_data;
                output_last <= stage2_last;

                stage2_valid <= stage1_valid;
                stage2_data <= stage1_data;
                stage2_last <= stage1_last;
                stage2_live <= stage1_live;
                stage2_match <= stage1_match;

                stage1_valid <= accepted_filter_beat;
                stage1_data <= input_data;
                stage1_last <= input_last;
                for (int s = 0; s < SLOTS; s++) begin
                    stage1_live[s] <= input_data[128*s +: 128] != PAD_ROW;
                    if (first_beat) begin
                        stage1_field0[32*s +: 32] <= field_select(cfg_field0, input_data[128*s +: 128]);
                        stage1_field1[32*s +: 32] <= field_select(cfg_field1, input_data[128*s +: 128]);
                        stage1_field2[32*s +: 32] <= field_select(cfg_field2, input_data[128*s +: 128]);
                    end else begin
                        stage1_field0[32*s +: 32] <= field_select(active_field0, input_data[128*s +: 128]);
                        stage1_field1[32*s +: 32] <= field_select(active_field1, input_data[128*s +: 128]);
                        stage1_field2[32*s +: 32] <= field_select(active_field2, input_data[128*s +: 128]);
                    end
                end

                if (stage2_valid) begin
                    selected_count <= selected_count + {{61{1'b0}}, stage2_selected_count};
                end
            end

            if (output_fire && output_last) begin
                batch_open <= 1'b0;
                batch_input_closed <= 1'b0;
            end
            case (state)
                STATE_FILTER: begin
                    if (input_fire) begin
                        if (first_beat) begin
                            batch_open <= 1'b1;
                            selected_count <= 64'd0;
                            active_field0 <= cfg_field0;
                            active_op0 <= cfg_op0;
                            active_constant0 <= cfg_constant0;
                            active_combine01 <= cfg_combine01;
                            active_field1 <= cfg_field1;
                            active_op1 <= cfg_op1;
                            active_constant1 <= cfg_constant1;
                            active_combine2 <= cfg_combine2;
                            active_field2 <= cfg_field2;
                            active_op2 <= cfg_op2;
                            active_constant2 <= cfg_constant2;
                        end
                        if (first_beat && !config_ok) begin
                            // config precedence over stream shape: drain the
                            // batch, then CONFIG
                            if (input_last) begin
                                raise_error(ERR_CONFIG);
                            end else begin
                                state <= STATE_DISCARD;
                            end
                        end else begin
                            if (input_last) begin
                                batch_input_closed <= 1'b1;
                            end
                        end
                    end
                end
                STATE_DISCARD: begin
                    if (input_fire && input_last) begin
                        raise_error(ERR_CONFIG);
                    end
                end
                STATE_STATUS: begin
                    if (status_valid && status_ready) begin
                        status_valid <= 1'b0;
                        status_error <= 1'b0;
                        status_error_code <= 8'd0;
                        state <= STATE_FILTER;
                    end
                end
                default: begin
                    reset_state();
                end
            endcase
        end
    end

    task automatic raise_error(input logic [7:0] code);
        begin
            output_valid <= 1'b0;
            output_last <= 1'b0;
            selected_count <= 64'd0;
            batch_open <= 1'b0;
            batch_input_closed <= 1'b0;
            stage1_valid <= 1'b0;
            stage2_valid <= 1'b0;
            status_valid <= 1'b1;
            status_error <= 1'b1;
            status_error_code <= code;
            state <= STATE_STATUS;
        end
    endtask

    task automatic reset_state;
        begin
            state <= STATE_FILTER;
            batch_open <= 1'b0;
            batch_input_closed <= 1'b0;
            active_field0 <= 2'd0;
            active_op0 <= 3'd0;
            active_constant0 <= 32'd0;
            active_combine01 <= 2'd0;
            active_field1 <= 2'd0;
            active_op1 <= 3'd0;
            active_constant1 <= 32'd0;
            active_combine2 <= 2'd0;
            active_field2 <= 2'd0;
            active_op2 <= 3'd0;
            active_constant2 <= 32'd0;
            stage1_valid <= 1'b0;
            stage1_data <= '0;
            stage1_last <= 1'b0;
            stage1_live <= '0;
            stage1_field0 <= '0;
            stage1_field1 <= '0;
            stage1_field2 <= '0;
            stage2_valid <= 1'b0;
            stage2_data <= '0;
            stage2_last <= 1'b0;
            stage2_live <= '0;
            stage2_match <= '0;
            output_valid <= 1'b0;
            output_data <= '0;
            output_last <= 1'b0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
            selected_count <= 64'd0;
        end
    endtask
endmodule

`default_nettype wire
