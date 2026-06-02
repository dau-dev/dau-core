from __future__ import annotations

from importlib.resources import files

DAU_IDENTITY_REGISTERS_SV = files(__package__).joinpath("dau_identity_registers.sv")
DAU_IDENTITY_AXIL_V = files(__package__).joinpath("dau_identity_axil.v")
DAU_INT32_AGGREGATION_TILE_SV = files(__package__).joinpath("dau_int32_aggregation_tile.sv")
DAU_INT32_STREAM_AGGREGATION_SV = files(__package__).joinpath("dau_int32_stream_aggregation.sv")
DAU_INT32_ARROW_LITE_STREAM_AGGREGATION_SV = files(__package__).joinpath("dau_int32_arrow_lite_stream_aggregation.sv")

__all__ = (
    "DAU_IDENTITY_AXIL_V",
    "DAU_IDENTITY_REGISTERS_SV",
    "DAU_INT32_AGGREGATION_TILE_SV",
    "DAU_INT32_ARROW_LITE_STREAM_AGGREGATION_SV",
    "DAU_INT32_STREAM_AGGREGATION_SV",
)
