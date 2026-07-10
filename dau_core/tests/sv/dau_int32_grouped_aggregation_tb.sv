`timescale 1ns/1ps
`default_nettype none

// Two bars ((570: 100,105,95,102), (571: 102,99)) -> two 5-word group
// records; then reserved mode must report the config error code.
module dau_int32_grouped_aggregation_tb;
    logic clk = 1'b0;
    logic rst = 1'b1;
    logic [1:0] cfg_mode = 2'd0;
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
    logic [63:0] group_count;

    dau_int32_grouped_aggregation dut (
        .clk(clk),
        .rst(rst),
        .cfg_mode(cfg_mode),
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
        .group_count(group_count)
    );

    always #5 clk = ~clk;

    initial begin
        #100000;
        $fatal(1, "watchdog: bench did not finish");
    end

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

    task automatic send_row(input logic [31:0] key, input logic [31:0] value, input logic last_row);
        begin
            while (input_ready !== 1'b1) begin
                tick();
            end
            input_data = {value, key};
            input_last = last_row;
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

    task automatic expect_success(input logic [63:0] expected_groups);
        begin
            status_ready = 1'b1;
            while (status_valid !== 1'b1) begin
                tick();
            end
            if (status_error !== 1'b0 || group_count !== expected_groups) begin
                $fatal(1, "success status mismatch: error=%b groups=%0d", status_error, group_count);
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

        cfg_mode = 2'd0;
        cfg_row_count = 64'd6;
        fork
            begin
                send_row(32'd570, 32'd100, 1'b0);
                send_row(32'd570, 32'd105, 1'b0);
                send_row(32'd570, 32'd95, 1'b0);
                send_row(32'd570, 32'd102, 1'b0);
                send_row(32'd571, 32'd102, 1'b0);
                send_row(32'd571, 32'd99, 1'b1);
            end
            begin
                // bar 570: first=100 last=102 min=95 max=105 sum=402 count=4
                expect_output_word({32'd100, 32'd570}, 1'b0);
                expect_output_word({32'd95, 32'd102}, 1'b0);
                expect_output_word({32'd0, 32'd105}, 1'b0);
                expect_output_word(64'd402, 1'b0);
                expect_output_word(64'd4, 1'b0);
                // bar 571: first=102 last=99 min=99 max=102 sum=201 count=2
                expect_output_word({32'd102, 32'd571}, 1'b0);
                expect_output_word({32'd99, 32'd99}, 1'b0);
                expect_output_word({32'd0, 32'd102}, 1'b0);
                expect_output_word(64'd201, 1'b0);
                expect_output_word(64'd2, 1'b1);
            end
        join
        expect_success(64'd2);

        // reserved mode (hash, not implemented) must fail with config error
        cfg_mode = 2'd1;
        cfg_row_count = 64'd1;
        send_row(32'd1, 32'd2, 1'b1);
        expect_error(8'd1);

        $display("DAU_INT32_GROUPED_AGGREGATION_TB_OK");
        $finish;
    end
endmodule

`default_nettype wire
