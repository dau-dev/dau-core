from __future__ import annotations

from importlib.resources import files
from pathlib import Path

# The directory cores `include headers from. A tool compiling a core of this
# palette must be told about it -- Verilator does NOT search the including
# file's directory, so a bench or lint run that omits it fails on the include
# rather than on anything to do with the core.
DAU_HDL_INCLUDE_DIR = Path(str(files(__name__)))

DAU_AXI_RAM_SIM_SV = files(__name__).joinpath("dau_axi_ram_sim.sv")

__all__ = (
    "DAU_AXI_RAM_SIM_SV",
    "DAU_HDL_INCLUDE_DIR",
)
