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
    DAU_INT32_ASOF_BACKWARD_SV,
    DAU_INT32_BAR_AGGREGATION_SV,
    DAU_INT32_BITONIC_SORTER_SV,
    DAU_INT32_BROADCAST_HASH_JOIN_SV,
    DAU_INT32_FIELD_SUM_AGGREGATION_SV,
    DAU_INT32_GROUPED_AGGREGATION_SV,
    DAU_INT32_GROUPED_FIELD_AGGREGATION_SV,
    DAU_INT32_GROUPED_FIELD_SUM_AGGREGATION_SV,
    DAU_INT32_KEY_MASK_DISPATCHER_SV,
    DAU_INT32_KEY_MEMBERSHIP_FILTER_SV,
    DAU_INT32_KEYED_MAP_ALU_SV,
    DAU_INT32_KEYED_PREDICATE_FILTER_SV,
    DAU_INT32_MAP_ALU_SV,
    DAU_INT32_MARKET_FEATURE_SV,
    DAU_INT32_PAIR_KEY_FILTER_SV,
    DAU_INT32_PREDICATE_FILTER_SV,
    DAU_INT32_RANGE_PARTITIONER_SV,
    DAU_INT32_RECORD_BATCH_AGGREGATION_SV,
    DAU_INT32_ROLLING_MOMENTS_SV,
    DAU_INT32_ROW_MAP_ALU_SV,
    DAU_INT32_ROW_PREDICATE_FILTER_SV,
    DAU_INT32_ROW_UNPACK_SV,
    DAU_INT32_STREAM_AGGREGATION_SV,
    DAU_INT32_STREAMING_TOP_K_SV,
    DAU_INT32_TIME_BUCKET_KEY_SV,
    DAU_INT32_WIDE_FIELD_SUM_AGGREGATION_SV,
    DAU_INT32_WIDE_ROW_MAP_ALU_SV,
    DAU_INT32_WIDE_ROW_PREDICATE_FILTER_SV,
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
def test_asof_backward_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_ASOF_BACKWARD_SV)),),
        hdl_toplevel="dau_int32_asof_backward",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_asof_backward_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_time_bucket_key_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_TIME_BUCKET_KEY_SV)),),
        hdl_toplevel="dau_int32_time_bucket_key",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_time_bucket_key_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_hero_datapath_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    # the full fused temporal datapath: as-of -> feature -> bucket -> generic
    # aggregator -> rolling-moments, golden-equal end to end
    _run_cocotb_verilator(
        sources=(
            Path(str(DAU_INT32_ASOF_BACKWARD_SV)),
            Path(str(DAU_INT32_MARKET_FEATURE_SV)),
            Path(str(DAU_INT32_TIME_BUCKET_KEY_SV)),
            Path(str(DAU_INT32_GROUPED_FIELD_AGGREGATION_SV)),
            Path(str(DAU_INT32_ROLLING_MOMENTS_SV)),
            _SV_TB_DIR / "dau_hero_datapath.sv",
        ),
        hdl_toplevel="dau_hero_datapath",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_hero_datapath_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_bucket_agg_chain_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    # the fused, fully-generic bar stage: time-bucket key -> generic aggregator,
    # selected into OHLCV bars downstream (no finance-specific bar tile)
    _run_cocotb_verilator(
        sources=(
            Path(str(DAU_INT32_TIME_BUCKET_KEY_SV)),
            Path(str(DAU_INT32_GROUPED_FIELD_AGGREGATION_SV)),
            _SV_TB_DIR / "dau_bucket_agg_chain.sv",
        ),
        hdl_toplevel="dau_bucket_agg_chain",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_bucket_agg_chain_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_market_feature_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_MARKET_FEATURE_SV)),),
        hdl_toplevel="dau_int32_market_feature",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_market_feature_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_asof_feature_chain_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    # the fused align -> derive datapath: the as-of tile streams market rows
    # straight into the feature tile (same 4-word record, no reformatting)
    _run_cocotb_verilator(
        sources=(
            Path(str(DAU_INT32_ASOF_BACKWARD_SV)),
            Path(str(DAU_INT32_MARKET_FEATURE_SV)),
            _SV_TB_DIR / "dau_asof_feature_chain.sv",
        ),
        hdl_toplevel="dau_asof_feature_chain",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_asof_feature_chain_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_rolling_moments_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_ROLLING_MOMENTS_SV)),),
        hdl_toplevel="dau_int32_rolling_moments",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_rolling_moments_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_rolling_moments_window_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # WINDOW=2 is the minimum legal window (a size-1 sample variance is
    # undefined); the WINDOW-agnostic bench reads the env var to size its
    # expectations to the DUT.
    monkeypatch.setenv("DAU_ROLLING_WINDOW", "2")
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_ROLLING_MOMENTS_SV)),),
        hdl_toplevel="dau_int32_rolling_moments",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_rolling_moments_tb",
        tmp_path=tmp_path,
        parameters={"WINDOW": 2},
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_streaming_top_k_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_STREAMING_TOP_K_SV)),),
        hdl_toplevel="dau_int32_streaming_top_k",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_streaming_top_k_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_streaming_top_k_k_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # K=1 is the degenerate single-max case; the K-agnostic bench reads the env
    # var to size its expectations to the DUT.
    monkeypatch.setenv("DAU_TOP_K", "1")
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_STREAMING_TOP_K_SV)),),
        hdl_toplevel="dau_int32_streaming_top_k",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_streaming_top_k_tb",
        tmp_path=tmp_path,
        parameters={"K": 1},
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
@pytest.mark.parametrize("data_width", [64, 128, 256, 512])
def test_burst_reader_streams_exactly_at_every_width_tier(tmp_path: Path, data_width: int) -> None:
    """The width-tier contract: the reader reproduces the memory window
    bit-exactly at every catalog DATA_WIDTH (64/128/256/512)."""
    _run_cocotb_verilator(
        sources=(Path(str(DAU_AXI_BURST_READER_SV)),),
        hdl_toplevel="dau_axi_burst_reader",
        test_module="dau_core.tests.cocotb_benches.dau_axi_burst_reader_tb",
        tmp_path=tmp_path,
        parameters={"DATA_WIDTH": data_width},
    )


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
def test_row_unpack_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_ROW_UNPACK_SV)),),
        hdl_toplevel="dau_int32_row_unpack",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_row_unpack_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_key_mask_dispatcher_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_KEY_MASK_DISPATCHER_SV)),),
        hdl_toplevel="dau_int32_key_mask_dispatcher",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_key_mask_dispatcher_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_key_mask_dispatcher_wide_front_runs_real_cocotb(tmp_path: Path) -> None:
    # IN_WIDTH=128 (the rows/cycle axis): one whole quad row per input beat;
    # the shared front sustains 1 row/cycle on mask-distributed keys
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_KEY_MASK_DISPATCHER_SV)),),
        hdl_toplevel="dau_int32_key_mask_dispatcher",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_key_mask_dispatcher_wide_tb",
        tmp_path=tmp_path,
        parameters={"IN_WIDTH": 128},
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
@pytest.mark.parametrize(("in_width", "num_partitions"), [(256, 4), (512, 4), (512, 8)])
def test_key_mask_dispatcher_multi_row_runs_real_cocotb(tmp_path: Path, in_width: int, num_partitions: int) -> None:
    # IN_WIDTH=256/512 (2/4 quad rows per beat, the width-tier catalog):
    # parallel per-lane routing, first-pending conflict arbitration, the
    # all-ones PAD ROW dropped at the front
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_KEY_MASK_DISPATCHER_SV)),),
        hdl_toplevel="dau_int32_key_mask_dispatcher",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_key_mask_dispatcher_multi_tb",
        tmp_path=tmp_path,
        parameters={"IN_WIDTH": in_width, "NUM_PARTITIONS": num_partitions},
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_key_mask_dispatcher_exact_branch_wide_runs_real_cocotb(tmp_path: Path) -> None:
    # EXACT_BRANCH=1 at the wide front, NUM_PARTITIONS=3 (no power-of-two):
    # dest = key - 1, out-of-range keys dropped at the router for zero lane
    # cycles — the q19 disjunct-branch fan-out
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_KEY_MASK_DISPATCHER_SV)),),
        hdl_toplevel="dau_int32_key_mask_dispatcher",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_key_mask_dispatcher_exact_tb",
        tmp_path=tmp_path,
        parameters={"IN_WIDTH": 128, "EXACT_BRANCH": 1, "NUM_PARTITIONS": 3},
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_key_mask_dispatcher_exact_branch_narrow_runs_real_cocotb(tmp_path: Path) -> None:
    # the same exact-branch mode on the two-beat narrow front
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_KEY_MASK_DISPATCHER_SV)),),
        hdl_toplevel="dau_int32_key_mask_dispatcher",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_key_mask_dispatcher_exact_tb",
        tmp_path=tmp_path,
        parameters={"EXACT_BRANCH": 1, "NUM_PARTITIONS": 3},
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_row_unpack_wide_quad_stream_runs_real_cocotb(tmp_path: Path) -> None:
    # OUT_WIDTH=128 (the rows/cycle axis): one whole quad row per output beat,
    # sustaining 1 row/cycle where the 64-bit framing serializes 2 beats/row
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_ROW_UNPACK_SV)),),
        hdl_toplevel="dau_int32_row_unpack",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_row_unpack_wide_tb",
        tmp_path=tmp_path,
        parameters={"OUT_WIDTH": 128},
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_keyed_predicate_filter_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(Path(str(DAU_PREDICATE_PKG_SV)), Path(str(DAU_INT32_KEYED_PREDICATE_FILTER_SV))),
        hdl_toplevel="dau_int32_keyed_predicate_filter",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_keyed_predicate_filter_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_keyed_map_alu_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(Path(str(DAU_MAP_ALU_PKG_SV)), Path(str(DAU_INT32_KEYED_MAP_ALU_SV))),
        hdl_toplevel="dau_int32_keyed_map_alu",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_keyed_map_alu_tb",
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
@pytest.mark.parametrize("slots", [1, 2, 4])
def test_wide_row_predicate_filter_runs_real_cocotb_against_systemverilog(tmp_path: Path, slots: int) -> None:
    _run_cocotb_verilator(
        sources=(Path(str(DAU_PREDICATE_PKG_SV)), Path(str(DAU_INT32_WIDE_ROW_PREDICATE_FILTER_SV))),
        hdl_toplevel="dau_int32_wide_row_predicate_filter",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_wide_row_predicate_filter_tb",
        tmp_path=tmp_path,
        parameters={"SLOTS": slots},
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
@pytest.mark.parametrize("slots", [1, 2, 4])
def test_wide_row_map_alu_runs_real_cocotb_against_systemverilog(tmp_path: Path, slots: int) -> None:
    _run_cocotb_verilator(
        sources=(Path(str(DAU_MAP_ALU_PKG_SV)), Path(str(DAU_INT32_WIDE_ROW_MAP_ALU_SV))),
        hdl_toplevel="dau_int32_wide_row_map_alu",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_wide_row_map_alu_tb",
        tmp_path=tmp_path,
        parameters={"SLOTS": slots},
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
@pytest.mark.parametrize("slots", [1, 2, 4])
def test_wide_field_sum_aggregation_runs_real_cocotb_against_systemverilog(tmp_path: Path, slots: int) -> None:
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_WIDE_FIELD_SUM_AGGREGATION_SV)),),
        hdl_toplevel="dau_int32_wide_field_sum_aggregation",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_wide_field_sum_aggregation_tb",
        tmp_path=tmp_path,
        parameters={"SLOTS": slots},
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_grouped_field_aggregation_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_GROUPED_FIELD_AGGREGATION_SV)),),
        hdl_toplevel="dau_int32_grouped_field_aggregation",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_grouped_field_aggregation_tb",
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
def test_key_membership_filter_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_KEY_MEMBERSHIP_FILTER_SV)),),
        hdl_toplevel="dau_int32_key_membership_filter",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_key_membership_filter_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_key_membership_filter_single_word_bitmap_runs_real_cocotb(tmp_path: Path) -> None:
    # KEY_SPACE=64 is the WORDS==1 corner: a single-word bitmap (AWIDTH forced
    # to 1, the LOAD phase closing out from S_ARM without entering S_LOAD)
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_KEY_MEMBERSHIP_FILTER_SV)),),
        hdl_toplevel="dau_int32_key_membership_filter",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_key_membership_filter_single_word_tb",
        tmp_path=tmp_path,
        parameters={"KEY_SPACE": 64},
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_broadcast_hash_join_runs_real_cocotb_against_systemverilog(tmp_path: Path) -> None:
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_BROADCAST_HASH_JOIN_SV)),),
        hdl_toplevel="dau_int32_broadcast_hash_join",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_broadcast_hash_join_tb",
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
def test_broadcast_hash_join_small_key_space_runs_real_cocotb(tmp_path: Path) -> None:
    # KEY_SPACE=64 is a small direct-mapped table (AWIDTH=6): the clear sweep,
    # the boundary keys, and the out-of-range reject exercised in real RTL at a
    # non-default parameter. The KEY_SPACE-agnostic bench reads the env var to
    # size its keys to the DUT.
    _run_cocotb_verilator(
        sources=(Path(str(DAU_INT32_BROADCAST_HASH_JOIN_SV)),),
        hdl_toplevel="dau_int32_broadcast_hash_join",
        test_module=f"{_COCOTB_BENCH_PREFIX}.dau_int32_broadcast_hash_join_small_tb",
        tmp_path=tmp_path,
        parameters={"KEY_SPACE": 64},
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
