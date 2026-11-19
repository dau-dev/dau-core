from __future__ import annotations

from pathlib import Path
from shutil import which

import pytest

from dau_core.hdl import (
    DAU_AXI_RAM_SIM_SV,
    DAU_HDL_INCLUDE_DIR,
)

_SV_DIR = Path(__file__).parent / "sv"


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_int32_stream_aggregation_systemverilog_bench_runs_under_verilator(tmp_path: Path) -> None:
    result = _run_verilator_testbench(
        sources=(
            DAU_HDL_INCLUDE_DIR / "dau_aggregation_pkg.sv",
            DAU_HDL_INCLUDE_DIR / "dau_stream_aggregation.sv",
            _SV_DIR / "dau_stream_aggregation_tb.sv",
        ),
        top_module="dau_stream_aggregation_tb",
        work_dir=tmp_path,
    )

    assert "DAU_STREAM_AGGREGATION_TB_OK" in result.stdout


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_arrow_lite_stream_aggregation_systemverilog_bench_runs_under_verilator(tmp_path: Path) -> None:
    result = _run_verilator_testbench(
        sources=(
            DAU_HDL_INCLUDE_DIR / "dau_aggregation_pkg.sv",
            DAU_HDL_INCLUDE_DIR / "dau_arrow_lite_stream_aggregation.sv",
            _SV_DIR / "dau_arrow_lite_stream_aggregation_tb.sv",
        ),
        top_module="dau_arrow_lite_stream_aggregation_tb",
        work_dir=tmp_path,
    )

    assert "DAU_ARROW_LITE_STREAM_AGGREGATION_TB_OK" in result.stdout


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_bar_aggregation_systemverilog_bench_runs_under_verilator(tmp_path: Path) -> None:
    result = _run_verilator_testbench(
        sources=(
            DAU_HDL_INCLUDE_DIR / "dau_bar_aggregation.sv",
            _SV_DIR / "dau_bar_aggregation_tb.sv",
        ),
        top_module="dau_bar_aggregation_tb",
        work_dir=tmp_path,
    )

    assert "DAU_BAR_AGGREGATION_TB_OK" in result.stdout


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_axi_bar_job_systemverilog_bench_runs_under_verilator(tmp_path: Path) -> None:
    result = _run_verilator_testbench(
        sources=(
            DAU_HDL_INCLUDE_DIR / "dau_axi_stream_job.sv",
            DAU_HDL_INCLUDE_DIR / "dau_bar_aggregation.sv",
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
            DAU_HDL_INCLUDE_DIR / "dau_grouped_aggregation.sv",
            _SV_DIR / "dau_grouped_aggregation_tb.sv",
        ),
        top_module="dau_grouped_aggregation_tb",
        work_dir=tmp_path,
    )

    assert "DAU_GROUPED_AGGREGATION_TB_OK" in result.stdout


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_map_alu_systemverilog_bench_runs_under_verilator(tmp_path: Path) -> None:
    result = _run_verilator_testbench(
        sources=(
            DAU_HDL_INCLUDE_DIR / "dau_map_alu_pkg.sv",
            DAU_HDL_INCLUDE_DIR / "dau_int32_map_alu.sv",
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
            DAU_HDL_INCLUDE_DIR / "dau_predicate_pkg.sv",
            DAU_HDL_INCLUDE_DIR / "dau_int32_predicate_filter.sv",
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
            DAU_HDL_INCLUDE_DIR / "dau_aggregation_pkg.sv",
            DAU_HDL_INCLUDE_DIR / "dau_record_batch_aggregation.sv",
            _SV_DIR / "dau_record_batch_aggregation_tb.sv",
        ),
        top_module="dau_record_batch_aggregation_tb",
        work_dir=tmp_path,
    )

    assert "DAU_RECORD_BATCH_AGGREGATION_TB_OK" in result.stdout


def _run_verilator_testbench(**kwargs):
    verilator_module = pytest.importorskip("dau_sim.integrations.verilator", reason="dau-sim Verilator integration not installed")
    # The include path is not optional and belongs here rather than at each
    # call: a tile that `includes types.svh does not compile without it, and
    # Verilator does not search the including file's own directory. The cocotb
    # runner has carried the same flag since the element types landed; these
    # SystemVerilog benches are simply the first to instantiate a tile that
    # needs it.
    kwargs.setdefault("extra_args", (f"-I{DAU_HDL_INCLUDE_DIR}",))
    return verilator_module.run_verilator_testbench(**kwargs)
