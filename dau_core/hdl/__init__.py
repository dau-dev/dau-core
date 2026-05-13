from __future__ import annotations

from importlib.resources import files

DAU_IDENTITY_REGISTERS_SV = files(__package__).joinpath("dau_identity_registers.sv")
DAU_IDENTITY_AXIL_V = files(__package__).joinpath("dau_identity_axil.v")

__all__ = ("DAU_IDENTITY_AXIL_V", "DAU_IDENTITY_REGISTERS_SV")
