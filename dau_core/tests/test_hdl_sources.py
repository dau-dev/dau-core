from __future__ import annotations

from dau_core.hdl import DAU_IDENTITY_AXIL_V, DAU_IDENTITY_REGISTERS_SV


def test_packaged_identity_axi_lite_wrapper_instantiates_register_block() -> None:
    source = DAU_IDENTITY_AXIL_V.read_text()

    assert DAU_IDENTITY_REGISTERS_SV.is_file()
    assert "module dau_identity_axil" in source
    assert DAU_IDENTITY_AXIL_V.name == "dau_identity_axil.v"
    assert "dau_identity_registers #(" in source
    assert ".addr(identity_addr)" in source
    assert ".wen(write_fire)" in source
    assert "assign identity_addr = {4'h0, selected_addr[11:0]};" in source
    assert "assign write_fire = !s_axi_bvalid && s_axi_awvalid && s_axi_wvalid;" in source
    assert "assign read_fire = !write_fire && !s_axi_rvalid && s_axi_arvalid;" in source
    assert 'X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI AWADDR"' in source
    assert 'X_INTERFACE_PARAMETER = "XIL_INTERFACENAME S_AXI' in source


def test_identity_registers_inputs_have_explicit_net_types() -> None:
    source = DAU_IDENTITY_REGISTERS_SV.read_text()

    assert "input  wire logic [15:0] addr" in source
    assert "input  wire logic        wen" in source
    assert "input  wire logic [31:0] wdata" in source


def test_identity_axi_lite_wrapper_exposes_expected_axil_ports() -> None:
    source = DAU_IDENTITY_AXIL_V.read_text()

    for port in (
        "s_axi_awaddr",
        "s_axi_awvalid",
        "s_axi_awready",
        "s_axi_wdata",
        "s_axi_wstrb",
        "s_axi_wvalid",
        "s_axi_wready",
        "s_axi_bresp",
        "s_axi_bvalid",
        "s_axi_bready",
        "s_axi_araddr",
        "s_axi_arvalid",
        "s_axi_arready",
        "s_axi_rdata",
        "s_axi_rresp",
        "s_axi_rvalid",
        "s_axi_rready",
        "reset_request",
    ):
        assert port in source
