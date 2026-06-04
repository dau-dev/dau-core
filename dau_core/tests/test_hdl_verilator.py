from __future__ import annotations

from pathlib import Path
from shutil import which

import pytest

from dau_core.hdl import DAU_INT32_AGGREGATION_TILE_SV, DAU_INT32_ARROW_LITE_STREAM_AGGREGATION_SV, DAU_INT32_STREAM_AGGREGATION_SV

_SV_DIR = Path(__file__).parent / "sv"


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_int32_aggregation_tile_systemverilog_bench_runs_under_verilator(tmp_path: Path) -> None:
    result = _run_verilator_testbench(
        sources=(Path(str(DAU_INT32_AGGREGATION_TILE_SV)), _SV_DIR / "dau_int32_aggregation_tile_tb.sv"),
        top_module="dau_int32_aggregation_tile_tb",
        work_dir=tmp_path,
    )

    assert "DAU_INT32_AGGREGATION_TILE_TB_OK" in result.stdout


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_int32_stream_aggregation_systemverilog_bench_runs_under_verilator(tmp_path: Path) -> None:
    result = _run_verilator_testbench(
        sources=(
            Path(str(DAU_INT32_AGGREGATION_TILE_SV)),
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
            Path(str(DAU_INT32_ARROW_LITE_STREAM_AGGREGATION_SV)),
            _SV_DIR / "dau_int32_arrow_lite_stream_aggregation_tb.sv",
        ),
        top_module="dau_int32_arrow_lite_stream_aggregation_tb",
        work_dir=tmp_path,
    )

    assert "DAU_INT32_ARROW_LITE_STREAM_AGGREGATION_TB_OK" in result.stdout


def _run_verilator_testbench(**kwargs):
    verilator_module = pytest.importorskip("dau_sim.integrations.verilator", reason="dau-sim Verilator integration not installed")
    return verilator_module.run_verilator_testbench(**kwargs)
