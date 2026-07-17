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
    DAU_INT32_BITONIC_SORTER_SV,
    DAU_INT32_FIELD_SUM_AGGREGATION_SV,
    DAU_INT32_GROUPED_AGGREGATION_SV,
    DAU_INT32_GROUPED_FIELD_SUM_AGGREGATION_SV,
    DAU_INT32_MAP_ALU_SV,
    DAU_INT32_PAIR_KEY_FILTER_SV,
    DAU_INT32_PREDICATE_FILTER_SV,
    DAU_INT32_RANGE_PARTITIONER_SV,
    DAU_INT32_RECORD_BATCH_AGGREGATION_SV,
    DAU_INT32_ROW_MAP_ALU_SV,
    DAU_INT32_ROW_PREDICATE_FILTER_SV,
    DAU_INT32_STREAM_AGGREGATION_SV,
    DAU_MAP_ALU_PKG_SV,
    DAU_PREDICATE_PKG_SV,
    DAU_STREAM_BROADCAST_SV,
)

_COCOTB_BENCH_PREFIX = "dau_core.tests.cocotb_benches"
_SV_TB_DIR = Path(__file__).parent / "sv"


def _bar_noc_sim_sv(tmp_path: Path) -> Path:
    """Generate the broadcast-NoC sim harness (four key-filtered bar lanes)
    from the same composition data the shell top is walked from."""
    scan_composition = pytest.importorskip("dau_build.scan_composition", reason="dau-build not installed")
    composition = scan_composition.ScanComposition(
        name="bar-noc-4",
        module_name="dau_axi_bar_noc",
        burst_beats=16,
        lanes=tuple(
            scan_composition.LaneTile(
                module="dau_int32_bar_aggregation",
                config={"cfg_mode": "2'd0", "cfg_row_count": "64'd0"},
                count_port="bar_count",
                partition=scan_composition.TileInstance(
                    module="dau_int32_pair_key_filter",
                    config={"cfg_key_mask": "32'd3", "cfg_key_match": f"32'd{lane}"},
                ),
            )
            for lane in range(4)
        ),
    )
    path = tmp_path / "dau_axi_bar_noc_sim.v"
    path.write_text(scan_composition.generate_scan_composition_sim_sv(composition, module_name="dau_axi_bar_noc_sim"))
    return path


def _sort_noc_sim_sv(tmp_path: Path) -> Path:
    """Generate the sorted-scan sim harness (shared range partitioner into
    four sorter lanes) with the splitters surfaced as a testbench-driven
    config input."""
    scan_composition = pytest.importorskip("dau_build.scan_composition", reason="dau-build not installed")
    composition = scan_composition.ScanComposition(
        name="sorted-scan",
        module_name="dau_axi_sort_noc",
        burst_beats=16,
        lanes=tuple(scan_composition.LaneTile(module="dau_int32_bitonic_sorter", count_port="sorted_count") for _ in range(4)),
        partitioner=scan_composition.TileInstance(
            module="dau_int32_range_partitioner",
            config={"cfg_splitters": "cfg_splitters"},
        ),
    )
    path = tmp_path / "dau_axi_sort_noc_sim.v"
    path.write_text(
        scan_composition.generate_scan_composition_sim_sv(
            composition,
            module_name="dau_axi_sort_noc_sim",
            config_inputs={"cfg_splitters": 96},
        )
    )
    return path


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
def test_range_partitioner_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_RANGE_PARTITIONER_SV)),),
        hdl_toplevel="dau_int32_range_partitioner",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_range_partitioner_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_bitonic_sorter_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_BITONIC_SORTER_SV)),),
        hdl_toplevel="dau_int32_bitonic_sorter",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_bitonic_sorter_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
@pytest.mark.parametrize("capacity", [2, 4])
def test_bitonic_sorter_small_capacity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capacity: int) -> None:
    # CAPACITY=2 is the minimum legal network (a single compare-exchange);
    # CAPACITY=4 is the smallest multi-stage one. The bench reads the env var
    # to size its vectors to the DUT.
    monkeypatch.setenv("DAU_SORTER_CAPACITY", str(capacity))
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_BITONIC_SORTER_SV)),),
        hdl_toplevel="dau_int32_bitonic_sorter",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_bitonic_sorter_tb",
        tmp_path=tmp_path,
        parameters={"CAPACITY": capacity},
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_range_partitioner_non_power_of_two_partitions(tmp_path: Path) -> None:
    # 3 partitions (2 splitters): exercises the decode's non-power-of-two
    # destination width; the N-agnostic bench tests run, the N==4 ones skip
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_RANGE_PARTITIONER_SV)),),
        hdl_toplevel="dau_int32_range_partitioner",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_range_partitioner_tb",
        tmp_path=tmp_path,
        parameters={"NUM_PARTITIONS": 3},
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_row_predicate_filter_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(Path(str(DAU_PREDICATE_PKG_SV)), Path(str(DAU_INT32_ROW_PREDICATE_FILTER_SV))),
        hdl_toplevel="dau_int32_row_predicate_filter",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_row_predicate_filter_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_row_map_alu_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(Path(str(DAU_MAP_ALU_PKG_SV)), Path(str(DAU_INT32_ROW_MAP_ALU_SV))),
        hdl_toplevel="dau_int32_row_map_alu",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_row_map_alu_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_field_sum_aggregation_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_FIELD_SUM_AGGREGATION_SV)),),
        hdl_toplevel="dau_int32_field_sum_aggregation",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_field_sum_aggregation_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_grouped_field_sum_aggregation_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_GROUPED_FIELD_SUM_AGGREGATION_SV)),),
        hdl_toplevel="dau_int32_grouped_field_sum_aggregation",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_grouped_field_sum_aggregation_tb",
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
            _bar_noc_sim_sv(tmp_path),
        ),
        hdl_toplevel="dau_axi_bar_noc_sim",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_axi_bar_noc_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_axi_sort_noc_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    # partition -> local sort -> concat: the sorted-scan NoC composition
    _run_cocotb_verilator(
        sources=(
            Path(str(DAU_AXI_BURST_READER_SV)),
            Path(str(DAU_AXI_RECORD_WRITER_SV)),
            Path(str(DAU_AXI_WRITE_MUX_SV)),
            Path(str(DAU_INT32_RANGE_PARTITIONER_SV)),
            Path(str(DAU_INT32_BITONIC_SORTER_SV)),
            Path(str(DAU_AXI_RAM_SIM_SV)),
            _sort_noc_sim_sv(tmp_path),
        ),
        hdl_toplevel="dau_axi_sort_noc_sim",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_axi_sort_noc_tb",
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


def _run_cocotb_verilator(*, sources: tuple[Path, ...], hdl_toplevel: str, test_module: str, tmp_path: Path, parameters=None) -> None:
    # one canonical cocotb-on-Verilator flag set for every functional bench
    cocotb_integration = pytest.importorskip("dau_sim.integrations.cocotb", reason="dau-sim cocotb integration not installed")
    cocotb_integration.run_cocotb_testbench(
        sources=sources,
        hdl_toplevel=hdl_toplevel,
        test_module=test_module,
        build_dir=tmp_path / hdl_toplevel,
        results_xml=tmp_path / f"{hdl_toplevel}-cocotb-results.xml",
        parameters=parameters,
    )
