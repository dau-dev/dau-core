`default_nettype none

// Generic fixed-record gather: buffers one IN_WORDS-word record, then emits an
// OUT_WORDS-word projection assembled from independently selected 32-bit
// halves. Selector bits [5:0] choose the source word, bit 6 chooses low/high,
// bits [11:7] apply a right shift, and bits [31:12] are reserved.
//
// The shift is LOGICAL, never arithmetic: partition/key bit patterns with bit
// 31 set must zero-fill instead of sign-extending (the bar-to-roll key extract).
// Config is latched on the first word of a batch. Invalid config drains through
// input_last and reports ERR_CONFIG; input_last before a record's final word is
// ERR_STREAM. This is a mid-lane transform and is silent on successful batches.
//
// SystemVerilog cannot vary named ports with a parameter, so the physical
// interface exposes the maximum 16 selectors; cfg_sel0 through
// cfg_sel{2*OUT_WORDS-1} are active and the remaining ports are ignored.
module dau_int32_record_gather #(
    parameter int IN_WORDS = 4,
    parameter int OUT_WORDS = 1
) (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic [31:0] cfg_sel0,
    input  wire logic [31:0] cfg_sel1,
    input  wire logic [31:0] cfg_sel2,
    input  wire logic [31:0] cfg_sel3,
    input  wire logic [31:0] cfg_sel4,
    input  wire logic [31:0] cfg_sel5,
    input  wire logic [31:0] cfg_sel6,
    input  wire logic [31:0] cfg_sel7,
    input  wire logic [31:0] cfg_sel8,
    input  wire logic [31:0] cfg_sel9,
    input  wire logic [31:0] cfg_sel10,
    input  wire logic [31:0] cfg_sel11,
    input  wire logic [31:0] cfg_sel12,
    input  wire logic [31:0] cfg_sel13,
    input  wire logic [31:0] cfg_sel14,
    input  wire logic [31:0] cfg_sel15,
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
    localparam logic [7:0] ERR_CONFIG = 8'd1;
    localparam logic [7:0] ERR_STREAM = 8'd2;
    localparam int IN_INDEX_BITS = (IN_WORDS <= 1) ? 1 : $clog2(IN_WORDS);

    if (IN_WORDS < 1 || IN_WORDS > 64) begin : g_in_words_check
        $error("IN_WORDS must be in [1, 64]");
    end
    if (OUT_WORDS < 1 || OUT_WORDS > 8) begin : g_out_words_check
        $error("OUT_WORDS must be in [1, 8]");
    end

    typedef enum logic [1:0] {
        STATE_READ,
        STATE_EMIT,
        STATE_DISCARD,
        STATE_STATUS
    } state_t;

    state_t state;
    logic batch_open;
    logic [IN_INDEX_BITS-1:0] input_index;
    logic [2:0] output_index;
    logic row_last;
    logic [7:0] pending_error;
    logic [63:0] record_words [0:IN_WORDS-1];
    wire logic [31:0] cfg_selectors [0:15];
    logic [IN_INDEX_BITS-1:0] active_source_words [0:2*OUT_WORDS-1];
    logic active_source_halves [0:2*OUT_WORDS-1];
    logic [4:0] active_shifts [0:2*OUT_WORDS-1];
    logic config_ok;
    logic input_fire;
    logic output_fire;
    logic status_fire;

    assign cfg_selectors[0] = cfg_sel0;
    assign cfg_selectors[1] = cfg_sel1;
    assign cfg_selectors[2] = cfg_sel2;
    assign cfg_selectors[3] = cfg_sel3;
    assign cfg_selectors[4] = cfg_sel4;
    assign cfg_selectors[5] = cfg_sel5;
    assign cfg_selectors[6] = cfg_sel6;
    assign cfg_selectors[7] = cfg_sel7;
    assign cfg_selectors[8] = cfg_sel8;
    assign cfg_selectors[9] = cfg_sel9;
    assign cfg_selectors[10] = cfg_sel10;
    assign cfg_selectors[11] = cfg_sel11;
    assign cfg_selectors[12] = cfg_sel12;
    assign cfg_selectors[13] = cfg_sel13;
    assign cfg_selectors[14] = cfg_sel14;
    assign cfg_selectors[15] = cfg_sel15;

    always_comb begin
        config_ok = 1'b1;
        for (int selector_index = 0; selector_index < 2 * OUT_WORDS; selector_index++) begin
            if ((cfg_selectors[selector_index][31:12] != 20'd0)
                    || (int'(unsigned'(cfg_selectors[selector_index][5:0])) >= IN_WORDS)) begin
                config_ok = 1'b0;
            end
        end
    end

    function automatic logic [31:0] gather_half(
        input logic [IN_INDEX_BITS-1:0] source_index,
        input logic source_high,
        input logic [4:0] shift
    );
        logic [31:0] source_half;
        begin
            if (int'(unsigned'(source_index)) < IN_WORDS) begin
                source_half = source_high ? record_words[source_index][63:32] : record_words[source_index][31:0];
            end else begin
                source_half = 32'd0;
            end
            gather_half = source_half >> shift;
        end
    endfunction

    assign input_ready = ((state == STATE_READ) || (state == STATE_DISCARD));
    assign input_fire = input_valid && input_ready;
    assign output_valid = state == STATE_EMIT;
    assign output_fire = output_valid && output_ready;
    assign output_data = {
        gather_half(
            active_source_words[2 * int'(unsigned'(output_index)) + 1],
            active_source_halves[2 * int'(unsigned'(output_index)) + 1],
            active_shifts[2 * int'(unsigned'(output_index)) + 1]
        ),
        gather_half(
            active_source_words[2 * int'(unsigned'(output_index))],
            active_source_halves[2 * int'(unsigned'(output_index))],
            active_shifts[2 * int'(unsigned'(output_index))]
        )
    };
    assign output_last = row_last && (int'(unsigned'(output_index)) == OUT_WORDS - 1);
    assign status_valid = state == STATE_STATUS;
    assign status_fire = status_valid && status_ready;
    assign status_error = status_valid;
    assign status_error_code = status_valid ? pending_error : 8'd0;

    always_ff @(posedge clk) begin
        if (rst) begin
            state <= STATE_READ;
            batch_open <= 1'b0;
            input_index <= '0;
            output_index <= 3'd0;
            row_last <= 1'b0;
            pending_error <= 8'd0;
            for (int word_index = 0; word_index < IN_WORDS; word_index++) begin
                record_words[word_index] <= 64'd0;
            end
            for (int selector_index = 0; selector_index < 2 * OUT_WORDS; selector_index++) begin
                active_source_words[selector_index] <= '0;
                active_source_halves[selector_index] <= 1'b0;
                active_shifts[selector_index] <= 5'd0;
            end
        end else begin
            case (state)
                STATE_READ: begin
                    if (input_fire) begin
                        if (!batch_open) begin
                            batch_open <= 1'b1;
                            for (int selector_index = 0; selector_index < 2 * OUT_WORDS; selector_index++) begin
                                active_source_words[selector_index] <= cfg_selectors[selector_index][IN_INDEX_BITS-1:0];
                                active_source_halves[selector_index] <= cfg_selectors[selector_index][6];
                                active_shifts[selector_index] <= cfg_selectors[selector_index][11:7];
                            end
                        end
                        if (!batch_open && !config_ok) begin
                            // Config has precedence on the batch's first beat.
                            input_index <= '0;
                            if (input_last) begin
                                batch_open <= 1'b0;
                                pending_error <= ERR_CONFIG;
                                state <= STATE_STATUS;
                            end else begin
                                state <= STATE_DISCARD;
                            end
                        end else if ((int'(unsigned'(input_index)) != IN_WORDS - 1) && input_last) begin
                            batch_open <= 1'b0;
                            input_index <= '0;
                            pending_error <= ERR_STREAM;
                            state <= STATE_STATUS;
                        end else begin
                            record_words[input_index] <= input_data;
                            if (int'(unsigned'(input_index)) == IN_WORDS - 1) begin
                                input_index <= '0;
                                output_index <= 3'd0;
                                row_last <= input_last;
                                state <= STATE_EMIT;
                            end else begin
                                input_index <= input_index + 1'b1;
                            end
                        end
                    end
                end
                STATE_EMIT: begin
                    if (output_fire) begin
                        if (int'(unsigned'(output_index)) == OUT_WORDS - 1) begin
                            output_index <= 3'd0;
                            if (row_last) batch_open <= 1'b0;
                            state <= STATE_READ;
                        end else begin
                            output_index <= output_index + 3'd1;
                        end
                    end
                end
                STATE_DISCARD: begin
                    if (input_fire && input_last) begin
                        batch_open <= 1'b0;
                        input_index <= '0;
                        pending_error <= ERR_CONFIG;
                        state <= STATE_STATUS;
                    end
                end
                STATE_STATUS: begin
                    if (status_fire) begin
                        pending_error <= 8'd0;
                        state <= STATE_READ;
                    end
                end
                default: begin
                    state <= STATE_READ;
                    batch_open <= 1'b0;
                    input_index <= '0;
                    output_index <= 3'd0;
                    pending_error <= 8'd0;
                end
            endcase
        end
    end
endmodule

`default_nettype wire
