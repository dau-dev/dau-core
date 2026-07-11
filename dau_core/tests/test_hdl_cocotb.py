from __future__ import annotations

from pathlib import Path
from shutil import which

import pytest

from dau_core.hdl import (
    DAU_AGGREGATION_PKG_SV,
    DAU_AXI_BURST_READER_SV,
    DAU_AXI_RAM_SIM_SV,
    DAU_AXI_RECORD_WRITER_SV,
    DAU_AXI_STREAM_JOB_SV,
    DAU_AXI_WRITE_MUX_SV,
    DAU_INT32_AGGREGATION_TILE_SV,
    DAU_INT32_ARROW_LITE_STREAM_AGGREGATION_SV,
    DAU_INT32_BAR_AGGREGATION_SV,
    DAU_INT32_GROUPED_AGGREGATION_SV,
    DAU_INT32_MAP_ALU_SV,
    DAU_INT32_PAIR_KEY_FILTER_SV,
    DAU_INT32_PREDICATE_FILTER_SV,
    DAU_INT32_RECORD_BATCH_AGGREGATION_SV,
    DAU_INT32_STREAM_AGGREGATION_SV,
    DAU_MAP_ALU_PKG_SV,
    DAU_PREDICATE_PKG_SV,
    DAU_STREAM_BROADCAST_SV,
)

_COCOTB_BENCH_PREFIX = "dau_core.tests.cocotb_benches"
_SV_TB_DIR = Path(__file__).parent / "sv"


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_int32_aggregation_tile_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(
            Path(str(DAU_AGGREGATION_PKG_SV)),
            Path(str(DAU_INT32_AGGREGATION_TILE_SV)),
        ),
        hdl_toplevel="dau_int32_aggregation_tile",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_aggregation_tile_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_int32_stream_aggregation_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(
            Path(str(DAU_AGGREGATION_PKG_SV)),
            Path(str(DAU_INT32_AGGREGATION_TILE_SV)),
            Path(str(DAU_INT32_STREAM_AGGREGATION_SV)),
        ),
        hdl_toplevel="dau_int32_stream_aggregation",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_stream_aggregation_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_arrow_lite_stream_aggregation_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(
            Path(str(DAU_AGGREGATION_PKG_SV)),
            Path(str(DAU_INT32_ARROW_LITE_STREAM_AGGREGATION_SV)),
        ),
        hdl_toplevel="dau_int32_arrow_lite_stream_aggregation",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_arrow_lite_stream_aggregation_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_bar_aggregation_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_BAR_AGGREGATION_SV)),),
        hdl_toplevel="dau_int32_bar_aggregation",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_bar_aggregation_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_axi_bar_job_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(
            Path(str(DAU_AXI_STREAM_JOB_SV)),
            Path(str(DAU_INT32_BAR_AGGREGATION_SV)),
            Path(str(DAU_AXI_RAM_SIM_SV)),
            _SV_TB_DIR / "dau_axi_bar_job_sim.sv",
        ),
        hdl_toplevel="dau_axi_bar_job_sim",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_axi_bar_job_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_pair_key_filter_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_PAIR_KEY_FILTER_SV)),),
        hdl_toplevel="dau_int32_pair_key_filter",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_pair_key_filter_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_axi_bar_noc_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(
            Path(str(DAU_AXI_BURST_READER_SV)),
            Path(str(DAU_AXI_RECORD_WRITER_SV)),
            Path(str(DAU_AXI_WRITE_MUX_SV)),
            Path(str(DAU_STREAM_BROADCAST_SV)),
            Path(str(DAU_INT32_PAIR_KEY_FILTER_SV)),
            Path(str(DAU_INT32_BAR_AGGREGATION_SV)),
            Path(str(DAU_AXI_RAM_SIM_SV)),
            _SV_TB_DIR / "dau_axi_bar_noc_sim.sv",
        ),
        hdl_toplevel="dau_axi_bar_noc_sim",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_axi_bar_noc_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_grouped_aggregation_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_GROUPED_AGGREGATION_SV)),),
        hdl_toplevel="dau_int32_grouped_aggregation",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_grouped_aggregation_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_map_alu_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(Path(str(DAU_MAP_ALU_PKG_SV)), Path(str(DAU_INT32_MAP_ALU_SV))),
        hdl_toplevel="dau_int32_map_alu",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_map_alu_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_predicate_filter_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(Path(str(DAU_PREDICATE_PKG_SV)), Path(str(DAU_INT32_PREDICATE_FILTER_SV))),
        hdl_toplevel="dau_int32_predicate_filter",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_predicate_filter_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_record_batch_aggregation_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(
            Path(str(DAU_AGGREGATION_PKG_SV)),
            Path(str(DAU_INT32_RECORD_BATCH_AGGREGATION_SV)),
        ),
        hdl_toplevel="dau_int32_record_batch_aggregation",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_record_batch_aggregation_tb",
        tmp_path=tmp_path,
    )


def _run_cocotb_verilator(*, sources: tuple[Path, ...], hdl_toplevel: str, test_module: str, tmp_path: Path) -> None:
    # one canonical cocotb-on-Verilator flag set for every functional bench
    cocotb_integration = pytest.importorskip("dau_sim.integrations.cocotb", reason="dau-sim cocotb integration not installed")
    cocotb_integration.run_cocotb_testbench(
        sources=sources,
        hdl_toplevel=hdl_toplevel,
        test_module=test_module,
        build_dir=tmp_path / hdl_toplevel,
        results_xml=tmp_path / f"{hdl_toplevel}-cocotb-results.xml",
    )
