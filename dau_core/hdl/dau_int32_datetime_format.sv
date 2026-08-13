`default_nettype none

// Datetime formatter: one int64 epoch-SECONDS value per beat in, the 19-byte
// ASCII rendering "YYYY-MM-DD HH:MM:SS" out, one beat per cycle
// (dau_core.operators.format_datetime_rows is the golden). This is the
// hardware answer to polars' dt.to_string(fmt), which is a per-row string
// build on the CPU.
//
// RECORD SHAPE. Input is ONE 64-bit word, the whole word being the epoch
// second count -- there is no field select and no configuration, so nothing
// is latched at a batch's first beat. Output is ONE 152-bit beat carrying the
// 19 characters LITTLE-ENDIAN by byte: character i occupies bits [8*i+7:8*i],
// so a host memcpy of the beat's bytes yields the string in order. The output
// beat is deliberately WIDER than the 64-bit lane word: 19 bytes per row at
// one row per cycle is 2.85 GB/s at 150 MHz, which no 64-bit lane carries, so
// a lane consumer either takes the wide beat or pays a three-word serializer
// (and one row per three cycles) behind it.
//
// DOMAIN. Epoch seconds in [0, 253402300799] -- 1970-01-01T00:00:00 through
// 9999-12-31T23:59:59, which is exactly the range a four-digit year renders.
// Anything else (a negative timestamp, a year past 9999) closes the batch out
// with ERR_DOMAIN rather than emitting a wrong string that looks right, the
// same position the sqrt tile takes on a negative radicand. A signed 64-bit
// value with its sign bit set exceeds the bound as an unsigned compare, so
// the range check is ONE comparison.
//
// ARITHMETIC. Two chains, run back to back because latency is free here:
//
//   seconds -> days + second-of-day -> hours, minutes, seconds
//   days    -> civil (y, m, d), by Howard Hinnant's civil_from_days
//
// EVERY division is by a COMPILE-TIME CONSTANT, so every one of them is a
// multiply-shift: q = ((x >> k) * M) >> S, with the power-of-two factor of
// the divisor peeled off first (86400 = 128*675, 3600 = 16*225, 1460 = 4*365,
// ...) so the multiplier is as narrow as the identity allows. There is no
// general divider anywhere in this tile. Each (M, S) pair was verified over
// the exact input range it sees, at BOTH ENDS of every quotient block -- the
// two places a multiply-shift identity fails first.
//
// Binary-to-BCD is DOUBLE DABBLE (shift-and-add-3), not a second round of
// divisions. The year's fourteen iterations are split across two pipeline
// stages so neither carries fourteen chained nibble adds; the five two-digit
// fields (month, day, hour, minute, second) take six iterations on six bits
// each, shallow enough to sit inside one stage.
//
// PIPELINE. Fully pipelined, one row per cycle, PIPE_DEPTH stages plus the
// output register. The whole pipeline advances as one under `pipe_en`, high
// whenever the output register is free -- the same move-as-one discipline as
// the sqrt tile, and for the same reason: per-stage elastic buffering would
// cost more than the latency it saves, and nothing here needs the latency.
//
// A composed-pipeline stage: last-driven only, silent on success -- the
// downstream tile's close-out is authoritative. It raises status only for
// ERR_DOMAIN.
module dau_int32_datetime_format (
    input  wire logic         clk,
    input  wire logic         rst,
    input  wire logic         input_valid,
    output logic              input_ready,
    input  wire logic [63:0]  input_data,
    input  wire logic         input_last,
    output logic              output_valid,
    input  wire logic         output_ready,
    output logic [151:0]      output_data,
    output logic              output_last,
    output logic              status_valid,
    input  wire logic         status_ready,
    output logic              status_error,
    output logic [7:0]        status_error_code
);
    localparam logic [7:0] ERR_DOMAIN = 8'd3;

    // 1970-01-01T00:00:00 .. 9999-12-31T23:59:59
    localparam logic [63:0] MAX_EPOCH_SECONDS = 64'd253402300799;

    // multiply-shift constants: q = ((x >> PRE) * MAGIC) >> SHIFT
    localparam int          PRE_DAY      = 7;                 // 86400 = 128 * 675
    localparam logic [31:0] MAGIC_DAY    = 32'd3257812231;
    localparam int          SHIFT_DAY    = 41;
    localparam int          PRE_HOUR     = 4;                 // 3600 = 16 * 225
    localparam logic [12:0] MAGIC_HOUR   = 13'd4661;
    localparam int          SHIFT_HOUR   = 20;
    localparam int          PRE_MIN      = 2;                 // 60 = 4 * 15
    localparam logic [10:0] MAGIC_MIN    = 11'd1093;
    localparam int          SHIFT_MIN    = 14;
    localparam logic [21:0] MAGIC_ERA    = 22'd3762951;       // / 146097
    localparam int          SHIFT_ERA    = 39;
    localparam logic [14:0] MAGIC_1460   = 15'd22983;         // 1460 = 4 * 365
    localparam int          SHIFT_1460   = 23;
    localparam logic [14:0] MAGIC_36524  = 15'd29399;         // 36524 = 4 * 9131
    localparam int          SHIFT_36524  = 28;
    localparam logic [11:0] MAGIC_146096 = 12'd3675;          // 146096 = 16 * 9131
    localparam int          SHIFT_146096 = 25;
    localparam logic [15:0] MAGIC_365    = 16'd45965;         // / 365
    localparam int          SHIFT_365    = 24;
    localparam logic [5:0]  MAGIC_100    = 6'd41;             // 100 = 4 * 25
    localparam int          SHIFT_100    = 10;
    localparam logic [9:0]  MAGIC_153    = 10'd857;           // / 153
    localparam int          SHIFT_153    = 17;
    localparam logic [10:0] MAGIC_5      = 11'd1639;          // / 5
    localparam int          SHIFT_5      = 13;

    // days from 0000-03-01 to 1970-01-01: Hinnant's epoch shift
    localparam logic [22:0] EPOCH_SHIFT_DAYS = 23'd719468;

    localparam logic [7:0] CH_DASH  = 8'h2D;
    localparam logic [7:0] CH_SPACE = 8'h20;
    localparam logic [7:0] CH_COLON = 8'h3A;

    // Pipeline stages, in dataflow order. Only the first and last are named
    // as parameters (the valid shift register's ends); the rest are the
    // register groups below, one paragraph each in the always_ff.
    //
    //   0 capture   1 day mul   2 days     3 second-of-day
    //   4 hour mul  5 hour      6 rem      7 minute mul    8 minute
    //   9 second-of-minute, and the civil chain's first stage
    //  10 era mul  11 era      12 day-of-era
    //  13 three correction terms          14 year-of-era numerator
    //  15 yoe mul  16 yoe      17 year + day-of-year subtrahend
    //  18 day-of-year          19 mp mul  20 mp (March-based month index)
    //  21 month, day, year fix 22 double dabble A + the 2-digit fields
    //  23 double dabble B
    localparam int S_CAPTURE  = 0;
    localparam int S_BCD_B    = 23;
    localparam int PIPE_DEPTH = 24;

    logic pipe_en;
    logic input_fire;
    logic push;
    logic domain_bad;
    logic status_pending;
    logic drain;

    logic v   [PIPE_DEPTH];
    logic lst [PIPE_DEPTH];

    // A multiply-shift discards the low bits of every product by
    // construction (they are the fraction), and a remainder is computed at
    // the width of its minuend and kept at the width the algorithm bounds it
    // to. Both leave declared bits deliberately unread; synthesis trims them.
    /* verilator lint_off UNUSEDSIGNAL */

    // seconds -> days
    logic [38:0] sec0;
    logic [63:0] day_prod1;
    logic [38:0] sec1;
    logic [22:0] days2;
    logic [38:0] sec2;
    logic [16:0] sod3;
    logic [22:0] days3;
    // second-of-day -> h:m:s
    logic [25:0] hour_prod4;
    logic [16:0] sod4;
    logic [22:0] days4;
    logic [5:0]  hour5;
    logic [16:0] sod5;
    logic [22:0] days5;
    logic [11:0] rem6;
    logic [5:0]  hour6;
    logic [22:0] days6;
    logic [20:0] min_prod7;
    logic [11:0] rem7;
    logic [5:0]  hour7;
    logic [22:0] days7;
    logic [5:0]  min8;
    logic [11:0] rem8;
    logic [5:0]  hour8;
    logic [22:0] days8;
    // civil_from_days; the finished time of day rides along as one 18-bit word
    logic [17:0] tod9;
    logic [22:0] zc9;
    logic [43:0] era_prod10;
    logic [22:0] zc10;
    logic [17:0] tod10;
    logic [4:0]  era11;
    logic [22:0] zc11;
    logic [17:0] tod11;
    logic [17:0] doe12;
    logic [4:0]  era12;
    logic [17:0] tod12;
    logic [30:0] t1_13;
    logic [30:0] t2_13;
    logic [25:0] t3_13;
    logic [17:0] doe13;
    logic [4:0]  era13;
    logic [17:0] tod13;
    logic [17:0] yoe_num14;
    logic [17:0] doe14;
    logic [4:0]  era14;
    logic [17:0] tod14;
    logic [33:0] yoe_prod15;
    logic [17:0] doe15;
    logic [4:0]  era15;
    logic [17:0] tod15;
    logic [9:0]  yoe16;
    logic [17:0] doe16;
    logic [4:0]  era16;
    logic [17:0] tod16;
    logic [13:0] year17;
    logic [17:0] doy_sub17;
    logic [17:0] doe17;
    logic [17:0] tod17;
    logic [8:0]  doy18;
    logic [13:0] year18;
    logic [17:0] tod18;
    logic [20:0] mp_prod19;
    logic [8:0]  doy19;
    logic [13:0] year19;
    logic [17:0] tod19;
    logic [3:0]  mp20;
    logic [8:0]  doy20;
    logic [13:0] year20;
    logic [17:0] tod20;
    logic [3:0]  mon21;
    logic [4:0]  mday21;
    logic [13:0] year21;
    logic [17:0] tod21;
    // binary -> BCD
    logic [29:0] dd_year22;
    logic [39:0] bcd22;   // {second, minute, hour, day, month}, two digits each
    logic [15:0] year_bcd23;
    logic [39:0] bcd23;

    // combinational arithmetic feeding the stage registers. Written out here
    // rather than inline because a part-select of an expression is not legal
    // SystemVerilog, and every one of these is a wide result narrowed to the
    // range the algorithm proves it lives in.
    logic [38:0] sod_full;
    logic [16:0] rem_full;
    logic [11:0] sec_of_min;
    logic [22:0] doe_full;
    logic [15:0] yoe100_prod;
    logic [17:0] doy_sub_next;
    logic [17:0] doy_full;
    logic [10:0] mp_num;
    logic [10:0] mday_num;
    logic [21:0] mday_prod;
    logic [8:0]  mday_full;
    logic        mp_is_jan_feb;
    logic [29:0] dd_year_final;
    /* verilator lint_on UNUSEDSIGNAL */

    assign sod_full     = sec2 - {16'd0, days2} * 39'd86400;
    assign rem_full     = sod5 - {11'd0, hour5} * 17'd3600;
    assign sec_of_min   = rem8 - {6'd0, min8} * 12'd60;
    assign doe_full     = zc11 - {18'd0, era11} * 23'd146097;
    assign yoe100_prod  = {8'd0, yoe16[9:2]} * {10'd0, MAGIC_100};
    assign doy_sub_next = {8'd0, yoe16} * 18'd365
                          + {10'd0, yoe16[9:2]}
                          - {16'd0, yoe100_prod[SHIFT_100+1:SHIFT_100]};
    assign doy_full     = doe17 - doy_sub17;
    assign mp_num       = {2'd0, doy18} * 11'd5 + 11'd2;
    assign mday_num     = {7'd0, mp20} * 11'd153 + 11'd2;
    assign mday_prod    = {11'd0, mday_num} * {11'd0, MAGIC_5};
    assign mday_full    = doy20 - {2'd0, mday_prod[SHIFT_5+6:SHIFT_5]} + 9'd1;
    assign mp_is_jan_feb = mp20 >= 4'd10;
    assign dd_year_final = dd_year_half(dd_year22);

    assign input_fire = input_valid && input_ready;

    // The pipeline moves as one, and may move whenever the output register is
    // free. Deliberately NOT a function of input_valid: deriving readiness
    // from the push would close a combinational loop that simulates fine and
    // fails routed DRC (the lesson the sqrt tile's comment records).
    assign pipe_en = !output_valid || output_ready;
    assign input_ready = !status_pending && (drain || pipe_en);

    assign domain_bad = input_data > MAX_EPOCH_SECONDS;
    assign push = input_fire && !drain && !domain_bad;

    assign status_valid = status_pending;
    assign status_error = 1'b1;
    assign status_error_code = ERR_DOMAIN;

    // one double-dabble iteration over the year's four BCD nibbles, which sit
    // above the fourteen-bit binary field
    function automatic logic [29:0] dd_year_step(input logic [29:0] state);
        logic [29:0] next;
        begin
            next = state;
            if (next[17:14] >= 4'd5) next[17:14] = next[17:14] + 4'd3;
            if (next[21:18] >= 4'd5) next[21:18] = next[21:18] + 4'd3;
            if (next[25:22] >= 4'd5) next[25:22] = next[25:22] + 4'd3;
            if (next[29:26] >= 4'd5) next[29:26] = next[29:26] + 4'd3;
            dd_year_step = {next[28:0], 1'b0};
        end
    endfunction

    // seven of the fourteen iterations: one pipeline stage's worth
    function automatic logic [29:0] dd_year_half(input logic [29:0] state);
        logic [29:0] next;
        begin
            next = state;
            for (int unsigned i = 0; i < 7; i++) next = dd_year_step(next);
            dd_year_half = next;
        end
    endfunction

    // a two-digit field (0..63) to two BCD nibbles: six iterations over two
    // nibbles sitting above a six-bit binary field
    function automatic logic [7:0] bcd2(input logic [5:0] value);
        logic [13:0] next;
        begin
            next = {8'd0, value};
            for (int unsigned i = 0; i < 6; i++) begin
                if (next[9:6] >= 4'd5) next[9:6] = next[9:6] + 4'd3;
                if (next[13:10] >= 4'd5) next[13:10] = next[13:10] + 4'd3;
                next = {next[12:0], 1'b0};
            end
            bcd2 = next[13:6];
        end
    endfunction

    function automatic logic [7:0] ascii(input logic [3:0] digit);
        ascii = {4'h3, digit};
    endfunction

    always_ff @(posedge clk) begin
        if (rst) begin
            for (int unsigned i = 0; i < PIPE_DEPTH; i++) begin
                v[i] <= 1'b0;
                lst[i] <= 1'b0;
            end
            output_valid <= 1'b0;
            output_last <= 1'b0;
            output_data <= '0;
            status_pending <= 1'b0;
            drain <= 1'b0;
        end else begin
            if (pipe_en) begin
                v[S_CAPTURE] <= push;
                lst[S_CAPTURE] <= input_last;
                for (int unsigned i = 1; i < PIPE_DEPTH; i++) begin
                    v[i] <= v[i-1];
                    lst[i] <= lst[i-1];
                end
                output_valid <= v[S_BCD_B];
                output_last <= lst[S_BCD_B];

                // seconds -> days ------------------------------------------
                sec0 <= input_data[38:0];

                day_prod1 <= {32'd0, sec0[38:PRE_DAY]} * {32'd0, MAGIC_DAY};
                sec1 <= sec0;

                days2 <= day_prod1[SHIFT_DAY+22:SHIFT_DAY];
                sec2 <= sec1;

                sod3 <= sod_full[16:0];
                days3 <= days2;

                // second-of-day -> hours -----------------------------------
                hour_prod4 <= {13'd0, sod3[16:PRE_HOUR]} * {13'd0, MAGIC_HOUR};
                sod4 <= sod3;
                days4 <= days3;

                hour5 <= hour_prod4[SHIFT_HOUR+5:SHIFT_HOUR];
                sod5 <= sod4;
                days5 <= days4;

                rem6 <= rem_full[11:0];
                hour6 <= hour5;
                days6 <= days5;

                // second-of-hour -> minutes --------------------------------
                min_prod7 <= {11'd0, rem6[11:PRE_MIN]} * {10'd0, MAGIC_MIN};
                rem7 <= rem6;
                hour7 <= hour6;
                days7 <= days6;

                min8 <= min_prod7[SHIFT_MIN+5:SHIFT_MIN];
                rem8 <= rem7;
                hour8 <= hour7;
                days8 <= days7;

                // the time of day is complete; the civil chain starts here --
                tod9 <= {hour8, min8, sec_of_min[5:0]};
                zc9 <= days8 + EPOCH_SHIFT_DAYS;

                era_prod10 <= {21'd0, zc9} * {22'd0, MAGIC_ERA};
                zc10 <= zc9;
                tod10 <= tod9;

                era11 <= era_prod10[SHIFT_ERA+4:SHIFT_ERA];
                zc11 <= zc10;
                tod11 <= tod10;

                doe12 <= doe_full[17:0];
                era12 <= era11;
                tod12 <= tod11;

                t1_13 <= {15'd0, doe12[17:2]} * {16'd0, MAGIC_1460};
                t2_13 <= {15'd0, doe12[17:2]} * {16'd0, MAGIC_36524};
                t3_13 <= {12'd0, doe12[17:4]} * {14'd0, MAGIC_146096};
                doe13 <= doe12;
                era13 <= era12;
                tod13 <= tod12;

                yoe_num14 <= doe13
                             - {10'd0, t1_13[SHIFT_1460+7:SHIFT_1460]}
                             + {15'd0, t2_13[SHIFT_36524+2:SHIFT_36524]}
                             - {17'd0, t3_13[SHIFT_146096]};
                doe14 <= doe13;
                era14 <= era13;
                tod14 <= tod13;

                yoe_prod15 <= {16'd0, yoe_num14} * {18'd0, MAGIC_365};
                doe15 <= doe14;
                era15 <= era14;
                tod15 <= tod14;

                yoe16 <= yoe_prod15[SHIFT_365+9:SHIFT_365];
                doe16 <= doe15;
                era16 <= era15;
                tod16 <= tod15;

                year17 <= {4'd0, yoe16} + {5'd0, era16} * 14'd400;
                doy_sub17 <= doy_sub_next;
                doe17 <= doe16;
                tod17 <= tod16;

                doy18 <= doy_full[8:0];
                year18 <= year17;
                tod18 <= tod17;

                mp_prod19 <= {10'd0, mp_num} * {11'd0, MAGIC_153};
                doy19 <= doy18;
                year19 <= year18;
                tod19 <= tod18;

                mp20 <= mp_prod19[SHIFT_153+3:SHIFT_153];
                doy20 <= doy19;
                year20 <= year19;
                tod20 <= tod19;

                // Hinnant's month index is March-based: mp 0..9 are March..
                // December of the same year, mp 10..11 are January/February
                // of the NEXT one, which is what the year fix corrects.
                mon21 <= mp_is_jan_feb ? (mp20 - 4'd9) : (mp20 + 4'd3);
                mday21 <= mday_full[4:0];
                year21 <= year20 + {13'd0, mp_is_jan_feb};
                tod21 <= tod20;

                // binary -> BCD --------------------------------------------
                dd_year22 <= dd_year_half({16'd0, year21});
                bcd22 <= {bcd2(tod21[5:0]), bcd2(tod21[11:6]), bcd2(tod21[17:12]),
                          bcd2({1'b0, mday21}), bcd2({2'd0, mon21})};

                year_bcd23 <= dd_year_final[29:14];
                bcd23 <= bcd22;

                // ASCII assembly; character i lives in bits [8*i+7 : 8*i] ---
                output_data <= {ascii(bcd23[35:32]), ascii(bcd23[39:36]),  // SS
                                CH_COLON,
                                ascii(bcd23[27:24]), ascii(bcd23[31:28]),  // MM
                                CH_COLON,
                                ascii(bcd23[19:16]), ascii(bcd23[23:20]),  // HH
                                CH_SPACE,
                                ascii(bcd23[11:8]), ascii(bcd23[15:12]),   // DD
                                CH_DASH,
                                ascii(bcd23[3:0]), ascii(bcd23[7:4]),      // MM
                                CH_DASH,
                                ascii(year_bcd23[3:0]),
                                ascii(year_bcd23[7:4]),
                                ascii(year_bcd23[11:8]),
                                ascii(year_bcd23[15:12])};
            end

            // A domain violation is not pushed. Drain the rest of the batch,
            // then close it out: an error invalidates the batch, so emitting
            // its remaining rows would publish a partial answer.
            if (input_fire && !drain && domain_bad) begin
                if (input_last) status_pending <= 1'b1;
                else drain <= 1'b1;
            end else if (input_fire && drain && input_last) begin
                status_pending <= 1'b1;
            end

            if (status_pending && status_ready) begin
                status_pending <= 1'b0;
                drain <= 1'b0;
                for (int unsigned i = 0; i < PIPE_DEPTH; i++) v[i] <= 1'b0;
                output_valid <= 1'b0;
            end
        end
    end
endmodule

`default_nettype wire
