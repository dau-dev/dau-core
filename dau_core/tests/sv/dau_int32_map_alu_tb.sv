`timescale 1ns/1ps
`default_nettype none

// Map [7, -2, 5] with ADD 1000 -> [1007, 998, 1005]; MULSHR bucket
// derivation (v * 139811 >> 23 == v / 60); then a reserved opcode (0) must
// report the config error code.
module dau_int32_map_alu_tb;
    localparam logic [2:0] OP_ADD = 3'd1;
    localparam logic [2:0] OP_MULSHR = 3'd4;

    logic clk = 1'b0;
    logic rst = 1'b1;
    logic [2:0] cfg_op = 3'd0;
    logic [31:0] cfg_constant = 32'd0;
    logic [4:0] cfg_shift = 5'd0;
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

    dau_int32_map_alu dut (
        .clk(clk),
        .rst(rst),
        .cfg_op(cfg_op),
        .cfg_constant(cfg_constant),
        .cfg_shift(cfg_shift),
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

    task automatic expect_success;
        begin
            status_ready = 1'b1;
            while (status_valid !== 1'b1) begin
                tick();
            end
            if (status_error !== 1'b0) begin
                $fatal(1, "success status mismatch: error=%b code=%0d", status_error, status_error_code);
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

        // ADD 1000 over [7, -2, 5] (odd row count)
        cfg_op = OP_ADD;
        cfg_constant = 32'd1000;
        cfg_row_count = 64'd3;
        fork
            begin
                send_word({32'hfffffffe, 32'd7}, 1'b0);  // 7, -2
                send_word({32'd0, 32'd5}, 1'b1);         // 5 (odd final)
            end
            begin
                expect_output_word({32'd998, 32'd1007}, 1'b0);
                expect_output_word({32'd0, 32'd1005}, 1'b1);  // pad lane zeroed by the tile
            end
        join
        expect_success();

        // MULSHR bucket derivation: 34260 s -> minute 571
        cfg_op = OP_MULSHR;
        cfg_constant = 32'd139811;
        cfg_shift = 5'd23;
        cfg_row_count = 64'd2;
        fork
            send_word({32'd86399, 32'd34260}, 1'b1);
            expect_output_word({32'd1439, 32'd571}, 1'b1);
        join
        expect_success();

        // reserved opcode 0 must fail with the config error
        cfg_op = 3'd0;
        cfg_row_count = 64'd2;
        send_word({32'd2, 32'd1}, 1'b1);
        expect_error(8'd1);

        $display("DAU_INT32_MAP_ALU_TB_OK");
        $finish;
    end
endmodule

`default_nettype wire
