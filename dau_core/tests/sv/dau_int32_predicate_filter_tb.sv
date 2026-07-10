`timescale 1ns/1ps
`default_nettype none

// Filter [7, -2, 5, 11, 5] with GT 4 -> [7, 5, 11, 5]; then an invalid
// opcode (0) must report the config error code.
module dau_int32_predicate_filter_tb;
    localparam logic [2:0] OP_GT = 3'd5;

    logic clk = 1'b0;
    logic rst = 1'b1;
    logic [2:0] cfg_op0 = 3'd0;
    logic [31:0] cfg_constant0 = 32'd0;
    logic [1:0] cfg_combine = 2'd0;
    logic [2:0] cfg_op1 = 3'd0;
    logic [31:0] cfg_constant1 = 32'd0;
    logic [63:0] cfg_row_count = 64'd0;
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
    logic [63:0] selected_count;

    dau_int32_predicate_filter dut (
        .clk(clk),
        .rst(rst),
        .cfg_op0(cfg_op0),
        .cfg_constant0(cfg_constant0),
        .cfg_combine(cfg_combine),
        .cfg_op1(cfg_op1),
        .cfg_constant1(cfg_constant1),
        .cfg_row_count(cfg_row_count),
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
        .status_error_code(status_error_code),
        .selected_count(selected_count)
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

    task automatic expect_output_word(input logic [63:0] expected, input logic expected_last);
        begin
            output_ready = 1'b1;
            while (output_valid !== 1'b1) begin
                tick();
            end
            if (output_data !== expected || output_last !== expected_last) begin
                $fatal(1, "output word mismatch: got %h last=%b, expected %h last=%b", output_data, output_last, expected, expected_last);
            end
            tick();
            output_ready = 1'b0;
        end
    endtask

    task automatic expect_success(input logic [63:0] expected_count);
        begin
            status_ready = 1'b1;
            while (status_valid !== 1'b1) begin
                tick();
            end
            if (status_error !== 1'b0 || selected_count !== expected_count) begin
                $fatal(1, "success status mismatch: error=%b count=%0d", status_error, selected_count);
            end
            tick();
            status_ready = 1'b0;
        end
    endtask

    task automatic expect_error(input logic [7:0] expected_code);
        begin
            status_ready = 1'b1;
            while (status_valid !== 1'b1) begin
                tick();
            end
            if (status_error !== 1'b1 || status_error_code !== expected_code || output_valid !== 1'b0) begin
                $fatal(1, "error status mismatch: error=%b code=%0d", status_error, status_error_code);
            end
            tick();
            status_ready = 1'b0;
        end
    endtask

    initial begin
        apply_reset();

        // GT 4 over [7, -2, 5, 11, 5] (odd row count)
        cfg_op0 = OP_GT;
        cfg_constant0 = 32'd4;
        cfg_combine = 2'd0;
        cfg_row_count = 64'd5;
        fork
            begin
                send_word({32'hfffffffe, 32'd7}, 1'b0);  // 7, -2
                send_word({32'd11, 32'd5}, 1'b0);        // 5, 11
                send_word({32'd0, 32'd5}, 1'b1);         // 5 (odd final)
            end
            begin
                expect_output_word({32'd5, 32'd7}, 1'b0);   // kept: 7, 5
                expect_output_word({32'd5, 32'd11}, 1'b1);  // kept: 11, 5 (final)
            end
        join
        expect_success(64'd4);

        // reserved opcode 0 must fail with the config error
        cfg_op0 = 3'd0;
        cfg_row_count = 64'd2;
        send_word({32'd2, 32'd1}, 1'b1);
        expect_error(8'd1);

        $display("DAU_INT32_PREDICATE_FILTER_TB_OK");
        $finish;
    end
endmodule

`default_nettype wire
