from __future__ import annotations

from dau_core.hdl import DAU_INT32_ARROW_LITE_STREAM_AGGREGATION_SV, DAU_INT32_STREAM_AGGREGATION_SV


def test_packaged_int32_stream_aggregation_exposes_descriptor_ports() -> None:
    source = DAU_INT32_STREAM_AGGREGATION_SV.read_text()

    assert DAU_INT32_STREAM_AGGREGATION_SV.is_file()
    assert "module dau_int32_stream_aggregation" in source
    for port in (
        "input  wire logic              descriptor_valid",
        "output logic                   descriptor_ready",
        "input  wire logic [63:0]       descriptor_row_count",
        "input  wire logic [15:0]       descriptor_logical_type",
        "input  wire logic [15:0]       descriptor_physical_width_bits",
        "input  wire logic [63:0]       descriptor_payload_length",
        "input  wire logic [2:0]        descriptor_opcode",
        "output logic [15:0]            result_logical_type",
        "output logic [63:0]            result_payload_length",
        "output logic [63:0]            result_row_count",
        "output logic [7:0]             error_code",
    ):
        assert port in source
    assert "dau_aggregation_pkg::aggregate_next" in source


def test_packaged_int32_stream_aggregation_names_stream_protocol_constants() -> None:
    source = DAU_INT32_STREAM_AGGREGATION_SV.read_text()

    assert "localparam logic [15:0] LT_INT32  = 16'd12;" in source
    assert "localparam logic [15:0] LT_INT64  = 16'd13;" in source
    assert "localparam logic [15:0] LT_UINT64 = 16'd23;" in source
    assert "localparam logic [7:0] ERR_DESCRIPTOR = 8'd1;" in source
    assert "localparam logic [7:0] ERR_OPCODE     = 8'd2;" in source


def test_packaged_arrow_lite_stream_aggregation_exposes_packed_ready_valid_ports() -> None:
    source = DAU_INT32_ARROW_LITE_STREAM_AGGREGATION_SV.read_text()

    assert DAU_INT32_ARROW_LITE_STREAM_AGGREGATION_SV.is_file()
    assert "module dau_int32_arrow_lite_stream_aggregation" in source
    for port in (
        "input  wire logic        input_valid",
        "output logic             input_ready",
        "input  wire logic [63:0] input_data",
        "input  wire logic        input_last",
        "output logic             output_valid",
        "input  wire logic        output_ready",
        "output logic [63:0]      output_data",
        "output logic             output_last",
        "output logic             status_valid",
        "input  wire logic        status_ready",
        "output logic             status_error",
        "output logic [7:0]       status_error_code",
    ):
        assert port in source


def test_packaged_arrow_lite_stream_aggregation_names_protocol_layout_constants() -> None:
    source = DAU_INT32_ARROW_LITE_STREAM_AGGREGATION_SV.read_text()

    assert "localparam logic [31:0] DAU_MAGIC = 32'h44415531;" in source
    assert "localparam logic [63:0] INPUT_PAYLOAD_OFFSET = 64'd192;" in source
    assert "localparam logic [63:0] RESULT_PAYLOAD_OFFSET = 64'd128;" in source
    assert "localparam logic [7:0] ERR_DESCRIPTOR = 8'd1;" in source
    assert "localparam logic [7:0] ERR_OPCODE     = 8'd2;" in source
