`timescale 1ns/1ps
`default_nettype none

// One bar (570: prices 100,95 / shares 10,2 / frac 500000,250000) then a
// second bar (571) -> two 6-word records; reserved mode -> config error.
module dau_bar_aggregation_tb;
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
    logic [63:0] bar_count;

    dau_bar_aggregation dut (
        .clk(clk), .rst(rst), .cfg_mode(cfg_mode), .cfg_row_count(cfg_row_count),
        .input_valid(input_valid), .input_ready(input_ready), .input_data(input_data), .input_last(input_last),
        .output_valid(output_valid), .output_ready(output_ready), .output_data(output_data), .output_last(output_last),
        .status_valid(status_valid), .status_ready(status_ready), .status_error(status_error),
        .status_error_code(status_error_code), .bar_count(bar_count)
    );

    always #5 clk = ~clk;

    initial begin
        #200000;
        $fatal(1, "watchdog: bench did not finish");
    end

    task automatic tick;
        begin
            @(posedge clk);
            #1;
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

    task automatic expect_success(input logic [63:0] expected_bars);
        begin
            status_ready = 1'b1;
            while (status_valid !== 1'b1) begin
                tick();
            end
            if (status_error !== 1'b0 || bar_count !== expected_bars) begin
                $fatal(1, "success status mismatch: error=%b bars=%0d", status_error, bar_count);
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
            if (status_error !== 1'b1 || status_error_code !== expected_code) begin
                $fatal(1, "error status mismatch: error=%b code=%0d", status_error, status_error_code);
            end
            tick();
            status_ready = 1'b0;
        end
    endtask

    initial begin
        rst = 1'b1;
        tick();
        rst = 1'b0;
        tick();

        cfg_mode = 2'd0;
        cfg_row_count = 64'd3;
        fork
            begin
                send_word({32'd100, 32'd570}, 1'b0);      // row 1 w0
                send_word({32'd500000, 32'd10}, 1'b0);    // row 1 w1
                send_word({32'd95, 32'd570}, 1'b0);       // row 2 w0
                send_word({32'd250000, 32'd2}, 1'b0);     // row 2 w1
                send_word({32'd102, 32'd571}, 1'b0);      // row 3 w0 (key change)
                send_word({32'd0, 32'd7}, 1'b1);          // row 3 w1 (final)
            end
            begin
                // bar 570: open=100 close=95 low=95 high=100 shares=12 frac=750000 count=2
                expect_output_word({32'd100, 32'd570}, 1'b0);
                expect_output_word({32'd95, 32'd95}, 1'b0);
                expect_output_word({32'd0, 32'd100}, 1'b0);
                expect_output_word(64'd12, 1'b0);
                expect_output_word(64'd750000, 1'b0);
                expect_output_word(64'd2, 1'b0);
                // bar 571: single row
                expect_output_word({32'd102, 32'd571}, 1'b0);
                expect_output_word({32'd102, 32'd102}, 1'b0);
                expect_output_word({32'd0, 32'd102}, 1'b0);
                expect_output_word(64'd7, 1'b0);
                expect_output_word(64'd0, 1'b0);
                expect_output_word(64'd1, 1'b1);
            end
        join
        expect_success(64'd2);

        // reserved mode must fail with the config error
        cfg_mode = 2'd1;
        cfg_row_count = 64'd1;
        send_word({32'd1, 32'd1}, 1'b0);
        send_word({32'd1, 32'd1}, 1'b1);
        expect_error(8'd1);

        $display("DAU_BAR_AGGREGATION_TB_OK");
        $finish;
    end
endmodule

`default_nettype wire
