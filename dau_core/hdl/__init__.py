from __future__ import annotations

from importlib.resources import files

DAU_AGGREGATION_PKG_SV = files(__package__).joinpath("dau_aggregation_pkg.sv")
DAU_PREDICATE_PKG_SV = files(__package__).joinpath("dau_predicate_pkg.sv")
DAU_MAP_ALU_PKG_SV = files(__package__).joinpath("dau_map_alu_pkg.sv")
DAU_IDENTITY_REGISTERS_SV = files(__package__).joinpath("dau_identity_registers.sv")
DAU_IDENTITY_AXIL_V = files(__package__).joinpath("dau_identity_axil.v")
DAU_INT32_AGGREGATION_TILE_SV = files(__package__).joinpath("dau_int32_aggregation_tile.sv")
DAU_INT32_STREAM_AGGREGATION_SV = files(__package__).joinpath("dau_int32_stream_aggregation.sv")
DAU_INT32_ARROW_LITE_STREAM_AGGREGATION_SV = files(__package__).joinpath("dau_int32_arrow_lite_stream_aggregation.sv")
DAU_INT32_RECORD_BATCH_AGGREGATION_SV = files(__package__).joinpath("dau_int32_record_batch_aggregation.sv")
DAU_INT32_PREDICATE_FILTER_SV = files(__package__).joinpath("dau_int32_predicate_filter.sv")
DAU_INT32_MAP_ALU_SV = files(__package__).joinpath("dau_int32_map_alu.sv")
DAU_INT32_GROUPED_AGGREGATION_SV = files(__package__).joinpath("dau_int32_grouped_aggregation.sv")
DAU_INT32_BAR_AGGREGATION_SV = files(__package__).joinpath("dau_int32_bar_aggregation.sv")
DAU_INT32_KEYED_PREDICATE_FILTER_SV = files(__package__).joinpath("dau_int32_keyed_predicate_filter.sv")
DAU_INT32_KEYED_MAP_ALU_SV = files(__package__).joinpath("dau_int32_keyed_map_alu.sv")
DAU_BRAM_STREAM_JOB_SV = files(__package__).joinpath("dau_bram_stream_job.sv")
DAU_AXI_STREAM_JOB_SV = files(__package__).joinpath("dau_axi_stream_job.sv")
DAU_AXI_RAM_SIM_SV = files(__package__).joinpath("dau_axi_ram_sim.sv")
DAU_AXI_BURST_READER_SV = files(__package__).joinpath("dau_axi_burst_reader.sv")
DAU_AXI_RECORD_WRITER_SV = files(__package__).joinpath("dau_axi_record_writer.sv")
DAU_AXI_WRITE_MUX_SV = files(__package__).joinpath("dau_axi_write_mux.sv")
DAU_STREAM_BROADCAST_SV = files(__package__).joinpath("dau_stream_broadcast.sv")
DAU_INT32_PAIR_KEY_FILTER_SV = files(__package__).joinpath("dau_int32_pair_key_filter.sv")
DAU_INT32_RANGE_PARTITIONER_SV = files(__package__).joinpath("dau_int32_range_partitioner.sv")

__all__ = (
    "DAU_AGGREGATION_PKG_SV",
    "DAU_AXI_BURST_READER_SV",
    "DAU_AXI_RAM_SIM_SV",
    "DAU_AXI_RECORD_WRITER_SV",
    "DAU_AXI_STREAM_JOB_SV",
    "DAU_AXI_WRITE_MUX_SV",
    "DAU_INT32_PAIR_KEY_FILTER_SV",
    "DAU_INT32_RANGE_PARTITIONER_SV",
    "DAU_STREAM_BROADCAST_SV",
    "DAU_MAP_ALU_PKG_SV",
    "DAU_PREDICATE_PKG_SV",
    "DAU_BRAM_STREAM_JOB_SV",
    "DAU_IDENTITY_AXIL_V",
    "DAU_IDENTITY_REGISTERS_SV",
    "DAU_INT32_AGGREGATION_TILE_SV",
    "DAU_INT32_MAP_ALU_SV",
    "DAU_INT32_ARROW_LITE_STREAM_AGGREGATION_SV",
    "DAU_INT32_BAR_AGGREGATION_SV",
    "DAU_INT32_GROUPED_AGGREGATION_SV",
    "DAU_INT32_KEYED_MAP_ALU_SV",
    "DAU_INT32_KEYED_PREDICATE_FILTER_SV",
    "DAU_INT32_PREDICATE_FILTER_SV",
    "DAU_INT32_RECORD_BATCH_AGGREGATION_SV",
    "DAU_INT32_STREAM_AGGREGATION_SV",
)
