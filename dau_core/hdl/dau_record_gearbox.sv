`default_nettype none

// RECORD GEARBOX — the NoC's framing adapter.
//
// Reads IN_WIDTH-bit beats and emits ONE WHOLE RECORD PER CYCLE as a flat
// RECORD_WORDS*64 bus. Records are packed back-to-back in the input stream
// and freely STRADDLE beat boundaries (a 512-bit beat is eight words; a
// three-word record does not divide it), so the gearbox holds a small word
// window and slides it.
//
// Why this exists: a router or a chain head that consumes N-word records
// through a 64-bit port ingests one WORD per cycle, so an N-word record
// costs N cycles and the front — not the lanes behind it — becomes the
// system bottleneck. Measured on the temporal hero: eight replicated lanes
// could collectively absorb one record per cycle while the 64-bit front
// delivered one every three, and the job sat at ~3 cycles/record. This
// adapter closes exactly that gap, and it is generic: any consumer of
// fixed-size records behind a wide feed can sit behind it.
//
// Contract:
//   - input:  standard valid/ready/data/last over IN_WIDTH-bit beats. The
//             final beat may be partially populated; INPUT_RECORDS tells the
//             gearbox how many records the batch carries so it never emits a
//             record assembled from tail padding.
//   - output: valid/ready/last over one whole record per beat. output_last
//             rides the final record of the batch.
//   - a batch is framed by input_last; the record count is latched with the
//             first beat and counted down, so a batch whose byte length is
//             beat-padded still emits exactly INPUT_RECORDS records.
//
// This is a pure reframing: no field is inspected, reordered or dropped, so
// the golden is the identity on the record sequence (dau_core.operators
// .gearbox_records).
module dau_record_gearbox #(
    parameter int unsigned IN_WIDTH = 512,     // 128/256/512 bits per input beat
    parameter int unsigned RECORD_WORDS = 3    // 64-bit words per record
) (
    input  wire logic                        clk,
    input  wire logic                        rst,
    // how many records this batch carries (latched at the first beat)
    input  wire logic [31:0]                 cfg_input_records,
    input  wire logic                        input_valid,
    output logic                             input_ready,
    input  wire logic [IN_WIDTH-1:0]         input_data,
    input  wire logic                        input_last,
    output logic                             output_valid,
    input  wire logic                        output_ready,
    output logic [RECORD_WORDS*64-1:0]       output_data,
    output logic                             output_last,
    output logic                             status_valid,
    input  wire logic                        status_ready,
    output logic                             status_error,
    output logic [7:0]                       status_error_code
);
    localparam int unsigned BEAT_WORDS = IN_WIDTH / 64;
    // The window holds a partial record plus TWO whole beats. One beat of
    // headroom is not enough to sustain a record per cycle: with room for a
    // single beat the accept threshold is only reached after draining almost
    // the whole window, so beats arrive in bursts and the emitter starves
    // between them (measured 1.25 cycles/record at 512 bits). Two beats let a
    // beat land as soon as ~one beat of room appears — every BEAT_WORDS/
    // RECORD_WORDS cycles at full rate — which keeps the emitter fed.
    localparam int unsigned WINDOW_WORDS = RECORD_WORDS + 2 * BEAT_WORDS;
    localparam int unsigned CNT_W = $clog2(WINDOW_WORDS + 1);
    localparam logic [7:0] ERR_STREAM = 8'd2;  // batch ended before its records did

    if (IN_WIDTH != 128 && IN_WIDTH != 256 && IN_WIDTH != 512) begin : g_bad_width
        $fatal(1, "dau_record_gearbox: IN_WIDTH must be 128, 256 or 512");
    end
    if (RECORD_WORDS < 1 || RECORD_WORDS > 16) begin : g_bad_record
        $fatal(1, "dau_record_gearbox: RECORD_WORDS must be in [1, 16]");
    end

    // sliding word window: words[0] is the oldest unconsumed word
    logic [63:0]        window [WINDOW_WORDS];
    logic [CNT_W-1:0]   filled;      // words currently held
    logic               saw_last;    // the final beat has been accepted
    logic [31:0]        records_left;
    logic               batch_open;

    logic accept_beat;
    logic emit_record;
    logic output_fire;

    assign output_fire = output_valid && output_ready;
    // a beat is accepted whenever the window has room for a whole one
    assign accept_beat = input_valid && input_ready;
    assign input_ready = !saw_last && (filled <= CNT_W'(WINDOW_WORDS - BEAT_WORDS)) && (status_valid == 1'b0);
    // a record can leave as soon as the window holds one and the batch still owes records
    assign emit_record = (filled >= CNT_W'(RECORD_WORDS)) && (records_left != 32'd0) && (!output_valid || output_ready);

    // output_data is REGISTERED alongside output_valid. Presenting the window
    // combinationally would be wrong: the same clock edge that raises
    // output_valid also SHIFTS the window, so a combinational read would show
    // the NEXT record while claiming the current one.

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_gearbox();
        end else begin
            if (output_fire) begin
                output_valid <= 1'b0;
                output_last <= 1'b0;
            end
            if (status_valid && status_ready) begin
                status_valid <= 1'b0;
                status_error <= 1'b0;
                status_error_code <= 8'd0;
            end

            // one record out per cycle while the window can feed it
            if (emit_record) begin
                output_valid <= 1'b1;
                output_last <= (records_left == 32'd1);
                for (int w = 0; w < RECORD_WORDS; w++) begin
                    output_data[64 * w +: 64] <= window[w];
                end
                for (int w = 0; w < WINDOW_WORDS - RECORD_WORDS; w++) begin
                    window[w] <= window[w + RECORD_WORDS];
                end
                records_left <= records_left - 32'd1;
                if (records_left == 32'd1) begin
                    // the batch's last record left: close and re-arm
                    batch_open <= 1'b0;
                    saw_last <= 1'b0;
                    filled <= '0;
                end else begin
                    filled <= filled - CNT_W'(RECORD_WORDS);
                end
            end

            // one beat in per cycle, landing above whatever the emit left
            if (accept_beat) begin
                if (!batch_open) begin
                    batch_open <= 1'b1;
                    records_left <= cfg_input_records;
                end
                for (int w = 0; w < BEAT_WORDS; w++) begin
                    window[(emit_record ? (filled - CNT_W'(RECORD_WORDS)) : filled) + CNT_W'(w)] <= input_data[64 * w +: 64];
                end
                filled <= (emit_record ? (filled - CNT_W'(RECORD_WORDS)) : filled) + CNT_W'(BEAT_WORDS);
                if (input_last) begin
                    saw_last <= 1'b1;
                end
            end

            // the batch ran out of beats while records were still owed: the
            // caller's record count and its byte length disagree
            if (saw_last && !accept_beat && (records_left != 32'd0) && (filled < CNT_W'(RECORD_WORDS)) && !status_valid) begin
                status_valid <= 1'b1;
                status_error <= 1'b1;
                status_error_code <= ERR_STREAM;
                batch_open <= 1'b0;
                saw_last <= 1'b0;
                records_left <= 32'd0;
                filled <= '0;
            end
        end
    end

    task automatic reset_gearbox;
        begin
            for (int w = 0; w < WINDOW_WORDS; w++) begin
                window[w] <= 64'd0;
            end
            filled <= '0;
            saw_last <= 1'b0;
            records_left <= 32'd0;
            batch_open <= 1'b0;
            output_valid <= 1'b0;
            output_last <= 1'b0;
            output_data <= '0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
        end
    endtask
endmodule

`default_nettype wire
