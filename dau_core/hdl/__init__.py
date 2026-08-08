from __future__ import annotations

from importlib.resources import files

DAU_AGGREGATION_PKG_SV = files(__package__).joinpath("dau_aggregation_pkg.sv")
DAU_PREDICATE_PKG_SV = files(__package__).joinpath("dau_predicate_pkg.sv")
DAU_MAP_ALU_PKG_SV = files(__package__).joinpath("dau_map_alu_pkg.sv")
DAU_IDENTITY_REGISTERS_SV = files(__package__).joinpath("dau_identity_registers.sv")
DAU_IDENTITY_AXIL_V = files(__package__).joinpath("dau_identity_axil.v")
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
DAU_INT32_KEY_MASK_DISPATCHER_SV = files(__package__).joinpath("dau_int32_key_mask_dispatcher.sv")
DAU_INT32_STRIDE_DISTRIBUTOR_SV = files(__package__).joinpath("dau_int32_stride_distributor.sv")
DAU_INT32_BITONIC_SORTER_SV = files(__package__).joinpath("dau_int32_bitonic_sorter.sv")
DAU_INT32_ROW_PREDICATE_FILTER_SV = files(__package__).joinpath("dau_int32_row_predicate_filter.sv")
DAU_INT32_FIXED_POINT_DIVIDE_SV = files(__package__).joinpath("dau_int32_fixed_point_divide.sv")
DAU_INT32_FIXED_POINT_SQRT_SV = files(__package__).joinpath("dau_int32_fixed_point_sqrt.sv")
DAU_INT32_ROW_MAP_ALU_SV = files(__package__).joinpath("dau_int32_row_map_alu.sv")
DAU_INT32_FIELD_SUM_AGGREGATION_SV = files(__package__).joinpath("dau_int32_field_sum_aggregation.sv")
DAU_INT32_GROUPED_FIELD_SUM_AGGREGATION_SV = files(__package__).joinpath("dau_int32_grouped_field_sum_aggregation.sv")
DAU_INT32_WIDE_FIELD_SUM_AGGREGATION_SV = files(__package__).joinpath("dau_int32_wide_field_sum_aggregation.sv")
DAU_RECORD_GEARBOX_SV = files(__package__).joinpath("dau_record_gearbox.sv")
DAU_INT32_WIDE_ROW_MAP_ALU_SV = files(__package__).joinpath("dau_int32_wide_row_map_alu.sv")
DAU_INT32_WIDE_ROW_PREDICATE_FILTER_SV = files(__package__).joinpath("dau_int32_wide_row_predicate_filter.sv")
DAU_INT32_GROUPED_FIELD_AGGREGATION_SV = files(__package__).joinpath("dau_int32_grouped_field_aggregation.sv")
DAU_INT32_KEY_MEMBERSHIP_FILTER_SV = files(__package__).joinpath("dau_int32_key_membership_filter.sv")
DAU_INT32_BROADCAST_HASH_JOIN_SV = files(__package__).joinpath("dau_int32_broadcast_hash_join.sv")
DAU_INT32_ROW_UNPACK_SV = files(__package__).joinpath("dau_int32_row_unpack.sv")
DAU_INT32_ASOF_BACKWARD_SV = files(__package__).joinpath("dau_int32_asof_backward.sv")
DAU_INT32_ROLLING_MOMENTS_SV = files(__package__).joinpath("dau_int32_rolling_moments.sv")
DAU_INT32_MARKET_FEATURE_SV = files(__package__).joinpath("dau_int32_market_feature.sv")
DAU_INT32_RECORD_GATHER_SV = files(__package__).joinpath("dau_int32_record_gather.sv")
DAU_INT32_TIME_BUCKET_KEY_SV = files(__package__).joinpath("dau_int32_time_bucket_key.sv")
DAU_INT32_KEYED_STATE_SV = files(__package__).joinpath("dau_int32_keyed_state.sv")
DAU_INT32_SCRATCHPAD_SV = files(__package__).joinpath("dau_int32_scratchpad.sv")
DAU_INT32_HASH_GROUPED_AGGREGATION_SV = files(__package__).joinpath("dau_int32_hash_grouped_aggregation.sv")
DAU_INT32_SESSION_GAP_SV = files(__package__).joinpath("dau_int32_session_gap.sv")
DAU_INT32_STREAMING_TOP_K_SV = files(__package__).joinpath("dau_int32_streaming_top_k.sv")

__all__ = (
    "DAU_AGGREGATION_PKG_SV",
    "DAU_AXI_BURST_READER_SV",
    "DAU_AXI_RAM_SIM_SV",
    "DAU_AXI_RECORD_WRITER_SV",
    "DAU_AXI_STREAM_JOB_SV",
    "DAU_AXI_WRITE_MUX_SV",
    "DAU_BRAM_STREAM_JOB_SV",
    "DAU_IDENTITY_AXIL_V",
    "DAU_IDENTITY_REGISTERS_SV",
    "DAU_INT32_ARROW_LITE_STREAM_AGGREGATION_SV",
    "DAU_INT32_ASOF_BACKWARD_SV",
    "DAU_INT32_BAR_AGGREGATION_SV",
    "DAU_INT32_BITONIC_SORTER_SV",
    "DAU_INT32_BROADCAST_HASH_JOIN_SV",
    "DAU_INT32_FIELD_SUM_AGGREGATION_SV",
    "DAU_INT32_GROUPED_AGGREGATION_SV",
    "DAU_INT32_GROUPED_FIELD_AGGREGATION_SV",
    "DAU_INT32_GROUPED_FIELD_SUM_AGGREGATION_SV",
    "DAU_INT32_HASH_GROUPED_AGGREGATION_SV",
    "DAU_INT32_KEYED_MAP_ALU_SV",
    "DAU_INT32_KEYED_PREDICATE_FILTER_SV",
    "DAU_INT32_KEYED_STATE_SV",
    "DAU_INT32_KEY_MASK_DISPATCHER_SV",
    "DAU_INT32_KEY_MEMBERSHIP_FILTER_SV",
    "DAU_INT32_MAP_ALU_SV",
    "DAU_INT32_MARKET_FEATURE_SV",
    "DAU_INT32_PAIR_KEY_FILTER_SV",
    "DAU_INT32_PREDICATE_FILTER_SV",
    "DAU_INT32_RANGE_PARTITIONER_SV",
    "DAU_INT32_RECORD_BATCH_AGGREGATION_SV",
    "DAU_INT32_RECORD_GATHER_SV",
    "DAU_INT32_ROLLING_MOMENTS_SV",
    "DAU_INT32_FIXED_POINT_DIVIDE_SV",
    "DAU_INT32_FIXED_POINT_SQRT_SV",
    "DAU_INT32_ROW_MAP_ALU_SV",
    "DAU_INT32_ROW_PREDICATE_FILTER_SV",
    "DAU_INT32_ROW_UNPACK_SV",
    "DAU_INT32_SCRATCHPAD_SV",
    "DAU_INT32_SESSION_GAP_SV",
    "DAU_INT32_STREAMING_TOP_K_SV",
    "DAU_INT32_STREAM_AGGREGATION_SV",
    "DAU_INT32_STRIDE_DISTRIBUTOR_SV",
    "DAU_INT32_TIME_BUCKET_KEY_SV",
    "DAU_INT32_WIDE_FIELD_SUM_AGGREGATION_SV",
    "DAU_INT32_WIDE_ROW_MAP_ALU_SV",
    "DAU_INT32_WIDE_ROW_PREDICATE_FILTER_SV",
    "DAU_MAP_ALU_PKG_SV",
    "DAU_PREDICATE_PKG_SV",
    "DAU_RECORD_GEARBOX_SV",
    "DAU_STREAM_BROADCAST_SV",
)
