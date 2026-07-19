`default_nettype none

// Row-unpack front-end feed tile: widens a stream of PACKED rows into the
// standard 16-byte int32 quad rows the operator tiles consume. Each input
// word carries one complete packed row (<=64 bits of the 64-bit word); the
// tile bit-extracts four configured fields, sign- or zero-extends each to
// int32, and emits the 16-byte quad row as two 64-bit words
// (w0 = {value, key}, w1 = {aux1, aux0}; field i is the i-th 32-bit lane in
// (key, value, aux0, aux1) order — field 0 = key = w0.lo — byte-identical to
// what the pair-key partition / row filters / row map / field-sum tiles
// expect). One input word in, two output words out, order and count
// preserved. dau_core.operators.unpack_rows is the golden.
//
// Why this tile exists: the dpv1 scan feed is DDR-BYTE-bandwidth-bound
// (~970 MB/s profiled). Staging rows PACKED to DDR (fewer bytes/row) and
// widening them on-chip here ~doubles rows/sec off DDR AND halves the host
// DMA, with the operators downstream unchanged. This MVP covers packed rows
// up to 64 bits (q6 at 59 bits, q1 at 63 bits).
//
// Each output field i is bits packed[cfg_field{i}_offset +: cfg_field{i}_width]
// (offset 0..63, width 1..32) sign-extended when cfg_field{i}_signed else
// zero-extended. A field descriptor with width==0, width>32, or
// offset+width>64 is invalid: config precedence (batch-latched at the first
// row) drains the batch, then reports ERR_CONFIG.
//
// A composed-pipeline front-end stage: silent on success (the downstream
// tile's close-out is authoritative — success_close_out=False), raising its
// own status only for an invalid config. There is no torn-row status here:
// one input word is always one complete packed row, so the input framing
// cannot tear. The 2-beat output runs through a decoupled emit engine with a
// drain gate (S_ERR_DRAIN) so a following batch's ERR_CONFIG can never
// overtake this batch's un-drained final output beat.
//
// FOLLOW-UP (not wired here): realizing the feed win requires (a) the scan
// composition to stage packed rows and place this unpacker after the burst
// reader, before the broadcast/partition fan-out, and (b) a flywheel
// pack-schema deriving each field's offset/width from the column ranges.
module dau_int32_row_unpack (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic [5:0]  cfg_field0_offset,
    input  wire logic [5:0]  cfg_field0_width,
    input  wire logic        cfg_field0_signed,
    input  wire logic [5:0]  cfg_field1_offset,
    input  wire logic [5:0]  cfg_field1_width,
    input  wire logic        cfg_field1_signed,
    input  wire logic [5:0]  cfg_field2_offset,
    input  wire logic [5:0]  cfg_field2_width,
    input  wire logic        cfg_field2_signed,
    input  wire logic [5:0]  cfg_field3_offset,
    input  wire logic [5:0]  cfg_field3_width,
    input  wire logic        cfg_field3_signed,
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

    typedef enum logic [1:0] {
        S_RUN,       // accept packed rows (batch config latches on the first)
        S_DISCARD,   // bad config: consume the batch to input_last
        S_ERR_DRAIN, // hold input closed until the in-flight output beat drains
        S_STATUS     // hold the ERR_CONFIG close-out until accepted
    } state_t;

    typedef enum logic [1:0] {
        EMIT_IDLE,
        EMIT_W0,
        EMIT_W1
    } emit_t;

    state_t state;
    emit_t  emit_state;

    // batch-latched field descriptors (quasi-static cfg inputs sampled at the
    // first row so a mid-batch change cannot re-slice the packed rows)
    logic       batch_open;
    logic [5:0] active_offset0, active_offset1, active_offset2, active_offset3;
    logic [5:0] active_width0, active_width1, active_width2, active_width3;
    logic       active_signed0, active_signed1, active_signed2, active_signed3;

    logic [63:0] emit_w0;
    logic [63:0] emit_w1;
    logic        emit_last;

    logic input_fire;
    logic output_fire;
    logic emit_free;
    logic first_row;
    logic config_ok;

    logic [5:0] eff_offset0, eff_offset1, eff_offset2, eff_offset3;
    logic [5:0] eff_width0, eff_width1, eff_width2, eff_width3;
    logic       eff_signed0, eff_signed1, eff_signed2, eff_signed3;

    logic [31:0] field0;
    logic [31:0] field1;
    logic [31:0] field2;
    logic [31:0] field3;
    logic [63:0] row_out_w0;
    logic [63:0] row_out_w1;

    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    // the emit engine frees on the accepted final beat of a row pair
    assign emit_free = (emit_state == EMIT_IDLE) || ((emit_state == EMIT_W1) && output_fire);

    // S_RUN loads the emit engine, so it must be free (or freeing) that cycle;
    // the check never depends on the incoming data
    assign input_ready = ((state == S_RUN) && emit_free) || (state == S_DISCARD);

    assign output_valid = emit_state != EMIT_IDLE;
    assign output_data = (emit_state == EMIT_W0) ? emit_w0 : emit_w1;
    assign output_last = (emit_state == EMIT_W1) && emit_last;

    assign first_row = !batch_open;

    assign eff_offset0 = first_row ? cfg_field0_offset : active_offset0;
    assign eff_width0 = first_row ? cfg_field0_width : active_width0;
    assign eff_signed0 = first_row ? cfg_field0_signed : active_signed0;
    assign eff_offset1 = first_row ? cfg_field1_offset : active_offset1;
    assign eff_width1 = first_row ? cfg_field1_width : active_width1;
    assign eff_signed1 = first_row ? cfg_field1_signed : active_signed1;
    assign eff_offset2 = first_row ? cfg_field2_offset : active_offset2;
    assign eff_width2 = first_row ? cfg_field2_width : active_width2;
    assign eff_signed2 = first_row ? cfg_field2_signed : active_signed2;
    assign eff_offset3 = first_row ? cfg_field3_offset : active_offset3;
    assign eff_width3 = first_row ? cfg_field3_width : active_width3;
    assign eff_signed3 = first_row ? cfg_field3_signed : active_signed3;

    // config precedence: a descriptor with width 0, width > 32, or a field
    // running past bit 64 is invalid (checked at the first row and latched)
    function automatic logic field_cfg_ok(input logic [5:0] offset, input logic [5:0] width);
        begin
            field_cfg_ok = (width >= 6'd1) && (width <= 6'd32) && (({1'b0, offset} + {1'b0, width}) <= 7'd64);
        end
    endfunction

    assign config_ok = field_cfg_ok(cfg_field0_offset, cfg_field0_width)
        && field_cfg_ok(cfg_field1_offset, cfg_field1_width)
        && field_cfg_ok(cfg_field2_offset, cfg_field2_width)
        && field_cfg_ok(cfg_field3_offset, cfg_field3_width);

    // barrel-extract one field to LSB, mask to width, then sign/zero-extend to
    // int32 (the exact 32-bit pattern a downstream signed compare reads back)
    function automatic logic [31:0] extract_field(
        input logic [63:0] packed_row,
        input logic [5:0]  offset,
        input logic [5:0]  width,
        input logic        signed_ext
    );
        logic [63:0] shifted;
        logic [31:0] mask;
        logic        sign_bit;
        logic [31:0] raw;
        begin
            shifted = packed_row >> offset;
            // width low bits set (width in [1,32]; width==32 => all ones)
            mask = (width >= 6'd32) ? 32'hFFFF_FFFF : ((32'd1 << width) - 32'd1);
            raw = shifted[31:0] & mask;
            sign_bit = shifted[width - 6'd1];     // the field's top bit
            if (signed_ext && sign_bit) begin
                extract_field = raw | ~mask;
            end else begin
                extract_field = raw;
            end
        end
    endfunction

    assign field0 = extract_field(input_data, eff_offset0, eff_width0, eff_signed0);
    assign field1 = extract_field(input_data, eff_offset1, eff_width1, eff_signed1);
    assign field2 = extract_field(input_data, eff_offset2, eff_width2, eff_signed2);
    assign field3 = extract_field(input_data, eff_offset3, eff_width3, eff_signed3);
    // w0 = {value, key}, w1 = {aux1, aux0}
    assign row_out_w0 = {field1, field0};
    assign row_out_w1 = {field3, field2};

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            case (emit_state)
                EMIT_W0: begin
                    if (output_fire) begin
                        emit_state <= EMIT_W1;
                    end
                end
                EMIT_W1: begin
                    if (output_fire) begin
                        emit_state <= EMIT_IDLE;
                    end
                end
                default: ;
            endcase

            case (state)
                S_RUN: begin
                    if (input_fire) begin
                        if (first_row) begin
                            batch_open <= 1'b1;
                            active_offset0 <= cfg_field0_offset;
                            active_width0 <= cfg_field0_width;
                            active_signed0 <= cfg_field0_signed;
                            active_offset1 <= cfg_field1_offset;
                            active_width1 <= cfg_field1_width;
                            active_signed1 <= cfg_field1_signed;
                            active_offset2 <= cfg_field2_offset;
                            active_width2 <= cfg_field2_width;
                            active_signed2 <= cfg_field2_signed;
                            active_offset3 <= cfg_field3_offset;
                            active_width3 <= cfg_field3_width;
                            active_signed3 <= cfg_field3_signed;
                        end
                        if (first_row && !config_ok) begin
                            // config precedence over stream shape: drain the
                            // batch, then CONFIG (never emit a mis-sliced row)
                            if (input_last) begin
                                if (emit_state == EMIT_IDLE) begin
                                    raise_error(ERR_CONFIG);
                                end else begin
                                    batch_open <= 1'b0;
                                    state <= S_ERR_DRAIN;
                                end
                            end else begin
                                state <= S_DISCARD;
                            end
                        end else begin
                            // widen this packed row and emit its quad-row pair
                            start_emit(row_out_w0, row_out_w1, input_last);
                            if (input_last) begin
                                // silent success: the downstream tile closes out
                                batch_open <= 1'b0;
                            end
                        end
                    end
                end
                S_DISCARD: begin
                    if (input_fire && input_last) begin
                        if (emit_state == EMIT_IDLE) begin
                            raise_error(ERR_CONFIG);
                        end else begin
                            state <= S_ERR_DRAIN;
                        end
                    end
                end
                S_ERR_DRAIN: begin
                    // hold input closed until the in-flight output beat has
                    // drained, then raise the config-error close-out
                    if (emit_state == EMIT_IDLE) begin
                        raise_error(ERR_CONFIG);
                    end
                end
                S_STATUS: begin
                    if (status_valid && status_ready) begin
                        status_valid <= 1'b0;
                        status_error <= 1'b0;
                        status_error_code <= 8'd0;
                        state <= S_RUN;
                    end
                end
                default: begin
                    reset_state();
                end
            endcase
        end
    end

    task automatic start_emit(input logic [63:0] w0, input logic [63:0] w1, input logic last_row);
        begin
            emit_w0 <= w0;
            emit_w1 <= w1;
            emit_last <= last_row;
            emit_state <= EMIT_W0;
        end
    endtask

    task automatic raise_error(input logic [7:0] code);
        begin
            batch_open <= 1'b0;
            status_valid <= 1'b1;
            status_error <= 1'b1;
            status_error_code <= code;
            state <= S_STATUS;
        end
    endtask

    task automatic reset_state;
        begin
            state <= S_RUN;
            emit_state <= EMIT_IDLE;
            batch_open <= 1'b0;
            active_offset0 <= 6'd0;
            active_width0 <= 6'd0;
            active_signed0 <= 1'b0;
            active_offset1 <= 6'd0;
            active_width1 <= 6'd0;
            active_signed1 <= 1'b0;
            active_offset2 <= 6'd0;
            active_width2 <= 6'd0;
            active_signed2 <= 1'b0;
            active_offset3 <= 6'd0;
            active_width3 <= 6'd0;
            active_signed3 <= 1'b0;
            emit_w0 <= 64'd0;
            emit_w1 <= 64'd0;
            emit_last <= 1'b0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
        end
    endtask
endmodule

`default_nettype wire
