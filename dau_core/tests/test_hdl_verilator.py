from __future__ import annotations

from pathlib import Path
from shutil import which

import pytest

from dau_core.hdl import (
    DAU_AGGREGATION_PKG_SV,
    DAU_AXI_RAM_SIM_SV,
    DAU_AXI_STREAM_JOB_SV,
    DAU_INT32_ARROW_LITE_STREAM_AGGREGATION_SV,
    DAU_INT32_BAR_AGGREGATION_SV,
    DAU_INT32_GROUPED_AGGREGATION_SV,
    DAU_INT32_MAP_ALU_SV,
    DAU_INT32_PREDICATE_FILTER_SV,
    DAU_INT32_RECORD_BATCH_AGGREGATION_SV,
    DAU_INT32_STREAM_AGGREGATION_SV,
    DAU_MAP_ALU_PKG_SV,
    DAU_PREDICATE_PKG_SV,
)

_SV_DIR = Path(__file__).parent / "sv"


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_int32_stream_aggregation_systemverilog_bench_runs_under_verilator(tmp_path: Path) -> None:
    result = _run_verilator_testbench(
        sources=(
            Path(str(DAU_AGGREGATION_PKG_SV)),
            Path(str(DAU_INT32_STREAM_AGGREGATION_SV)),
            _SV_DIR / "dau_int32_stream_aggregation_tb.sv",
        ),
        top_module="dau_int32_stream_aggregation_tb",
        work_dir=tmp_path,
    )

    assert "DAU_INT32_STREAM_AGGREGATION_TB_OK" in result.stdout


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_arrow_lite_stream_aggregation_systemverilog_bench_runs_under_verilator(tmp_path: Path) -> None:
    result = _run_verilator_testbench(
        sources=(
            Path(str(DAU_AGGREGATION_PKG_SV)),
            Path(str(DAU_INT32_ARROW_LITE_STREAM_AGGREGATION_SV)),
            _SV_DIR / "dau_int32_arrow_lite_stream_aggregation_tb.sv",
        ),
        top_module="dau_int32_arrow_lite_stream_aggregation_tb",
        work_dir=tmp_path,
    )

    assert "DAU_INT32_ARROW_LITE_STREAM_AGGREGATION_TB_OK" in result.stdout


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_bar_aggregation_systemverilog_bench_runs_under_verilator(tmp_path: Path) -> None:
    result = _run_verilator_testbench(
        sources=(
            Path(str(DAU_INT32_BAR_AGGREGATION_SV)),
            _SV_DIR / "dau_int32_bar_aggregation_tb.sv",
        ),
        top_module="dau_int32_bar_aggregation_tb",
        work_dir=tmp_path,
    )

    assert "DAU_INT32_BAR_AGGREGATION_TB_OK" in result.stdout


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_axi_bar_job_systemverilog_bench_runs_under_verilator(tmp_path: Path) -> None:
    result = _run_verilator_testbench(
        sources=(
            Path(str(DAU_AXI_STREAM_JOB_SV)),
            Path(str(DAU_INT32_BAR_AGGREGATION_SV)),
            Path(str(DAU_AXI_RAM_SIM_SV)),
            _SV_DIR / "dau_axi_bar_job_sim.sv",
            _SV_DIR / "dau_axi_bar_job_tb.sv",
        ),
        top_module="dau_axi_bar_job_tb",
        work_dir=tmp_path,
    )

    assert "DAU_AXI_BAR_JOB_TB_OK" in result.stdout


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_grouped_aggregation_systemverilog_bench_runs_under_verilator(tmp_path: Path) -> None:
    result = _run_verilator_testbench(
        sources=(
            Path(str(DAU_INT32_GROUPED_AGGREGATION_SV)),
            _SV_DIR / "dau_int32_grouped_aggregation_tb.sv",
        ),
        top_module="dau_int32_grouped_aggregation_tb",
        work_dir=tmp_path,
    )

    assert "DAU_INT32_GROUPED_AGGREGATION_TB_OK" in result.stdout


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_map_alu_systemverilog_bench_runs_under_verilator(tmp_path: Path) -> None:
    result = _run_verilator_testbench(
        sources=(
            Path(str(DAU_MAP_ALU_PKG_SV)),
            Path(str(DAU_INT32_MAP_ALU_SV)),
            _SV_DIR / "dau_int32_map_alu_tb.sv",
        ),
        top_module="dau_int32_map_alu_tb",
        work_dir=tmp_path,
    )

    assert "DAU_INT32_MAP_ALU_TB_OK" in result.stdout


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_predicate_filter_systemverilog_bench_runs_under_verilator(tmp_path: Path) -> None:
    result = _run_verilator_testbench(
        sources=(
            Path(str(DAU_PREDICATE_PKG_SV)),
            Path(str(DAU_INT32_PREDICATE_FILTER_SV)),
            _SV_DIR / "dau_int32_predicate_filter_tb.sv",
        ),
        top_module="dau_int32_predicate_filter_tb",
        work_dir=tmp_path,
    )

    assert "DAU_INT32_PREDICATE_FILTER_TB_OK" in result.stdout


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_record_batch_aggregation_systemverilog_bench_runs_under_verilator(tmp_path: Path) -> None:
    result = _run_verilator_testbench(
        sources=(
            Path(str(DAU_AGGREGATION_PKG_SV)),
            Path(str(DAU_INT32_RECORD_BATCH_AGGREGATION_SV)),
            _SV_DIR / "dau_int32_record_batch_aggregation_tb.sv",
        ),
        top_module="dau_int32_record_batch_aggregation_tb",
        work_dir=tmp_path,
    )

    assert "DAU_INT32_RECORD_BATCH_AGGREGATION_TB_OK" in result.stdout


def _run_verilator_testbench(**kwargs):
    verilator_module = pytest.importorskip("dau_sim.integrations.verilator", reason="dau-sim Verilator integration not installed")
    return verilator_module.run_verilator_testbench(**kwargs)
