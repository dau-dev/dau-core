`timescale 1ns/1ps
`default_nettype none

// The composed AXI job datapath (runner + bar tile + RAM sim): load three
// rows through the backdoor, run a job, check the two bar records land in
// the output region with the right result length and bar count; then a
// misaligned length must fail fast with the job-length error.
module dau_axi_bar_job_tb;
    logic clk = 1'b0;
    logic rst = 1'b1;
    logic start = 1'b0;
    logic [31:0] input_address = 32'd0;
    logic [31:0] input_length_bytes = 32'd0;
    logic [31:0] output_address = 32'd0;
    logic busy;
    logic done;
    logic error;
    logic [7:0] error_code;
    logic [31:0] result_length_bytes;
    logic [63:0] bar_count;
    logic bd_write = 1'b0;
    logic [31:0] bd_index = 32'd0;
    logic [63:0] bd_wdata = 64'd0;
    logic [63:0] bd_rdata;

    dau_axi_bar_job_sim dut (
        .clk(clk), .rst(rst),
        .start(start), .input_address(input_address), .input_length_bytes(input_length_bytes),
        .output_address(output_address), .busy(busy), .done(done), .error(error),
        .error_code(error_code), .result_length_bytes(result_length_bytes), .bar_count(bar_count),
        .bd_write(bd_write), .bd_index(bd_index), .bd_wdata(bd_wdata), .bd_rdata(bd_rdata)
    );

    always #5 clk = ~clk;

    initial begin
        #400000;
        $fatal(1, "watchdog: bench did not finish");
    end

    task automatic tick;
        begin
            @(posedge clk);
            #1;
        end
    endtask

    task automatic backdoor_write(input logic [31:0] index, input logic [63:0] word);
        begin
            bd_index = index;
            bd_wdata = word;
            bd_write = 1'b1;
            tick();
            bd_write = 1'b0;
        end
    endtask

    task automatic expect_word(input logic [31:0] index, input logic [63:0] expected);
        begin
            bd_index = index;
            #1;
            if (bd_rdata !== expected) begin
                $fatal(1, "memory word %0d mismatch: got %h, expected %h", index, bd_rdata, expected);
            end
        end
    endtask

    task automatic run_job(input logic [31:0] in_addr, input logic [31:0] in_len, input logic [31:0] out_addr);
        begin
            input_address = in_addr;
            input_length_bytes = in_len;
            output_address = out_addr;
            start = 1'b1;
            tick();
            start = 1'b0;
            while (done !== 1'b1) begin
                tick();
            end
        end
    endtask

    initial begin
        rst = 1'b1;
        tick();
        rst = 1'b0;
        tick();

        // rows at word 0: (570,100,10,500000) (570,95,2,250000) (571,102,7,0)
        backdoor_write(32'd0, {32'd100, 32'd570});
        backdoor_write(32'd1, {32'd500000, 32'd10});
        backdoor_write(32'd2, {32'd95, 32'd570});
        backdoor_write(32'd3, {32'd250000, 32'd2});
        backdoor_write(32'd4, {32'd102, 32'd571});
        backdoor_write(32'd5, {32'd0, 32'd7});

        run_job(32'h0000_0000, 32'd48, 32'h0004_0000);
        if (error !== 1'b0) begin
            $fatal(1, "job errored with code %0d", error_code);
        end
        if (result_length_bytes !== 32'd96 || bar_count !== 64'd2) begin
            $fatal(1, "result mismatch: length=%0d bars=%0d", result_length_bytes, bar_count);
        end

        // bar 570: open=100 close=95 low=95 high=100 shares=12 frac=750000 count=2
        expect_word(32'h8000, {32'd100, 32'd570});
        expect_word(32'h8001, {32'd95, 32'd95});
        expect_word(32'h8002, {32'd0, 32'd100});
        expect_word(32'h8003, 64'd12);
        expect_word(32'h8004, 64'd750000);
        expect_word(32'h8005, 64'd2);
        // bar 571: single row
        expect_word(32'h8006, {32'd102, 32'd571});
        expect_word(32'h8007, {32'd102, 32'd102});
        expect_word(32'h8008, {32'd0, 32'd102});
        expect_word(32'h8009, 64'd7);
        expect_word(32'h800A, 64'd0);
        expect_word(32'h800B, 64'd1);

        // misaligned length: not a multiple of the 16-byte row grid
        run_job(32'h0000_0000, 32'd24, 32'h0004_0000);
        if (error !== 1'b1 || error_code !== 8'hFE) begin
            $fatal(1, "expected job-length error, got error=%b code=%0d", error, error_code);
        end

        $display("DAU_AXI_BAR_JOB_TB_OK");
        $finish;
    end
endmodule

`default_nettype wire
