from __future__ import annotations

from pathlib import Path
from shutil import which

import pytest

from dau_core.hdl import DAU_INT32_AGGREGATION_TILE_SV, DAU_INT32_ARROW_LITE_STREAM_AGGREGATION_SV, DAU_INT32_STREAM_AGGREGATION_SV

_COCOTB_BENCH_PREFIX = "dau_core.tests.cocotb_benches"


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_int32_aggregation_tile_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_AGGREGATION_TILE_SV)),),
        hdl_toplevel="dau_int32_aggregation_tile",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_aggregation_tile_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_int32_stream_aggregation_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(
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
        sources=(Path(str(DAU_INT32_ARROW_LITE_STREAM_AGGREGATION_SV)),),
        hdl_toplevel="dau_int32_arrow_lite_stream_aggregation",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_arrow_lite_stream_aggregation_tb",
        tmp_path=tmp_path,
    )


def _run_cocotb_verilator(*, sources: tuple[Path, ...], hdl_toplevel: str, test_module: str, tmp_path: Path) -> None:
    runner_module = pytest.importorskip("cocotb_tools.runner", reason="cocotb runner not installed")
    runner = runner_module.get_runner("verilator")
    build_dir = tmp_path / hdl_toplevel
    runner.build(
        sources=sources,
        hdl_toplevel=hdl_toplevel,
        build_dir=build_dir,
        always=True,
        build_args=("--timing", "-Wno-fatal"),
    )
    runner.test(
        hdl_toplevel=hdl_toplevel,
        test_module=test_module,
        build_dir=build_dir,
        results_xml=str(tmp_path / f"{hdl_toplevel}-cocotb-results.xml"),
    )
