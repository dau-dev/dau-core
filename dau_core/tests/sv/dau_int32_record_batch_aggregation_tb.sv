`timescale 1ns/1ps
`default_nettype none

// Two-column record batch: column 0 is a decoy, the operation selects
// column 1 (sum), proving descriptor-table walk, column select, and
// payload skip. A second stream selects an out-of-range column and must
// report a descriptor error.
module dau_int32_record_batch_aggregation_tb;
    localparam logic [63:0] HEADER_WORD0 = 64'h0040010044415531;
    localparam logic [63:0] HEADER_COUNTS_2COL = 64'h0000000000010002;
    localparam logic [63:0] COL0_WORD0 = 64'h00000020000c0000;
    localparam logic [63:0] COL1_WORD0 = 64'h00000020000c0001;
    localparam logic [63:0] OP_SUM_COL1_WORD0 = 64'h0000000100030000;
    localparam logic [63:0] OP_SUM_COL5_WORD0 = 64'h0000000500030000;
    localparam logic [63:0] OP_INT64_WORD1 = 64'h000000000040000d;
    localparam logic [63:0] PAYLOAD_ONES = 64'h0000000100000001;
    localparam logic [63:0] PAYLOAD_10_NEG3 = 64'hfffffffd0000000a;
    localparam logic [63:0] PAYLOAD_5_8 = 64'h0000000800000005;

    logic clk = 1'b0;
    logic rst = 1'b1;
    logic input_valid = 1'b0;
    logic input_ready;
    logic [63:0] input_data = 64'd0;
    logic input_last = 1'b0;
    logic output_valid;
    logic output_ready = 1'b0;
    logic [63:0] output_data;
    logic output_last;
    logic status_valid;
    logic status_ready = 1'b0;
    logic status_error;
    logic [7:0] status_error_code;

    dau_int32_record_batch_aggregation dut (
        .clk(clk),
        .rst(rst),
        .input_valid(input_valid),
        .input_ready(input_ready),
        .input_data(input_data),
        .input_last(input_last),
        .output_valid(output_valid),
        .output_ready(output_ready),
        .output_data(output_data),
        .output_last(output_last),
        .status_valid(status_valid),
        .status_ready(status_ready),
        .status_error(status_error),
        .status_error_code(status_error_code)
    );

    always #5 clk = ~clk;

    task automatic tick;
        begin
            @(posedge clk);
            #1;
        end
    endtask

    task automatic apply_reset;
        begin
            rst = 1'b1;
            input_valid = 1'b0;
            input_last = 1'b0;
            output_ready = 1'b0;
            status_ready = 1'b0;
            tick();
            rst = 1'b0;
            tick();
        end
    endtask

    task automatic send_word(input logic [63:0] word, input logic last_word);
        begin
            // the one-sample-per-cycle payload path deasserts ready every
            // other payload cycle; wait for it
            while (input_ready !== 1'b1) begin
                tick();
            end
            input_data = word;
            input_last = last_word;
            input_valid = 1'b1;
            tick();
            input_valid = 1'b0;
            input_last = 1'b0;
        end
    endtask

    // header + 2 column descriptors + 1 op descriptor; payloads at 256/320.
    task automatic send_two_column_stream(input logic [63:0] op_word0);
        begin
            send_word(HEADER_WORD0, 1'b0);
            send_word(64'h0000000000001234, 1'b0);
            send_word(64'd9, 1'b0);
            send_word(64'd4, 1'b0);
            send_word(HEADER_COUNTS_2COL, 1'b0);
            send_word(64'd64, 1'b0);
            send_word(64'd256, 1'b0);
            send_word(64'd128, 1'b0);
            // column 0 descriptor (decoy int32)
            send_word(COL0_WORD0, 1'b0);
            send_word(64'd4, 1'b0);
            send_word(64'd0, 1'b0);
            send_word(64'd256, 1'b0);
            send_word(64'd16, 1'b0);
            send_word(64'd0, 1'b0);
            send_word(64'd0, 1'b0);
            send_word(64'd0, 1'b0);
            // column 1 descriptor (selected int32)
            send_word(COL1_WORD0, 1'b0);
            send_word(64'd4, 1'b0);
            send_word(64'd0, 1'b0);
            send_word(64'd320, 1'b0);
            send_word(64'd16, 1'b0);
            send_word(64'd0, 1'b0);
            send_word(64'd0, 1'b0);
            send_word(64'd0, 1'b0);
            // operation descriptor
            send_word(op_word0, 1'b0);
            send_word(OP_INT64_WORD1, 1'b0);
            send_word(64'd0, 1'b0);
            send_word(64'd0, 1'b0);
            send_word(64'd0, 1'b0);
            send_word(64'd0, 1'b0);
            send_word(64'd0, 1'b0);
            send_word(64'd0, 1'b0);
            // column 0 payload (skipped) + alignment padding
            send_word(PAYLOAD_ONES, 1'b0);
            send_word(PAYLOAD_ONES, 1'b0);
            send_word(64'd0, 1'b0);
            send_word(64'd0, 1'b0);
            send_word(64'd0, 1'b0);
            send_word(64'd0, 1'b0);
            send_word(64'd0, 1'b0);
            send_word(64'd0, 1'b0);
            // column 1 payload (10, -3, 5, 8) + alignment padding
            send_word(PAYLOAD_10_NEG3, 1'b0);
            send_word(PAYLOAD_5_8, 1'b0);
            send_word(64'd0, 1'b0);
            send_word(64'd0, 1'b0);
            send_word(64'd0, 1'b0);
            send_word(64'd0, 1'b0);
            send_word(64'd0, 1'b0);
            send_word(64'd0, 1'b1);
        end
    endtask

    task automatic expect_result(input logic [63:0] expected_value, input logic [15:0] expected_type);
        integer output_words;
        begin
            output_words = 0;
            wait (output_valid === 1'b1);
            output_ready = 1'b0;
            repeat (3) begin
                tick();
                if (output_valid !== 1'b1) begin
                    $fatal(1, "output_valid dropped under backpressure");
                end
            end
            output_ready = 1'b1;
            while (output_words < 17) begin
                if (output_valid === 1'b1) begin
                    output_words = output_words + 1;
                    if (output_words == 1 && output_data !== HEADER_WORD0) begin
                        $fatal(1, "result header word0 mismatch");
                    end
                    if (output_words == 9 && output_data[31:16] !== expected_type) begin
                        $fatal(1, "result logical type mismatch");
                    end
                    if (output_words == 17) begin
                        if (output_data !== expected_value || output_last !== 1'b1) begin
                            $fatal(1, "result payload mismatch");
                        end
                    end
                end
                tick();
            end
            output_ready = 1'b0;
            status_ready = 1'b1;
            if (status_valid !== 1'b1 || status_error !== 1'b0 || status_error_code !== 8'd0) begin
                $fatal(1, "success status mismatch");
            end
            tick();
            status_ready = 1'b0;
        end
    endtask

    task automatic expect_error(input logic [7:0] expected_code);
        begin
            status_ready = 1'b1;
            wait (status_valid === 1'b1);
            if (status_error !== 1'b1 || status_error_code !== expected_code || output_valid !== 1'b0) begin
                $fatal(1, "error status mismatch");
            end
            tick();
            status_ready = 1'b0;
        end
    endtask

    initial begin
        apply_reset();
        send_two_column_stream(OP_SUM_COL1_WORD0);
        expect_result(64'd20, 16'd13);

        send_two_column_stream(OP_SUM_COL5_WORD0);
        expect_error(8'd1);

        $display("DAU_INT32_RECORD_BATCH_AGGREGATION_TB_OK");
        $finish;
    end
endmodule

`default_nettype wire
