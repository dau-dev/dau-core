"""A named parameter binding must name a parameter the module declares.

`dau_identity_axil` passed `.SHELL_VERSION(...)` and `.DMA_CONSTRAINTS(...)`
to `dau_identity_registers` for some time after those two parameters were
deleted from it. The wrapper is packaged HDL that no cocotb bench
instantiates, so nothing elaborated it and nothing failed — the break would
have surfaced as a Vivado elaboration error in a synthesis run, which is the
slowest and most expensive place to find it.

Python-side guards caught neither half: the deletion was checked against the
generators that STAMP parameters, and this wrapper only forwards them.

Every named binding in a `dau_*` instantiation of another `dau_*` module,
checked against that module's declared parameter list. Cheap, and it runs in
the unit suite rather than after forty minutes of synthesis.
"""

from __future__ import annotations

import re
from pathlib import Path

from dau_core import hdl

_HDL_DIR = Path(str(hdl.__path__[0]))

# `module <name> #( ... ) (` — the parameter list is what precedes the port
# list; `parameter [31:0] NAME` / `parameter int NAME` / `parameter NAME`.
_MODULE = re.compile(r"^\s*module\s+(\w+)\s*#?\(", re.MULTILINE)
_PARAMETER = re.compile(r"\bparameter\b(?:\s+(?:logic|int|bit|reg|wire|integer))?(?:\s+(?:signed|unsigned))?\s*(?:\[[^\]]*\]\s*)?(\w+)\s*=")
# the port list opens after the parameter list closes; the two may be
# separated by a newline (`)\n(`), which a literal ") (" match misses
_PORT_LIST = re.compile(r"\)\s*\(")
# `dau_something #( .NAME(...), ... ) instance_name (`
_INSTANTIATION = re.compile(r"\b(dau_\w+)\s*#\s*\((.*?)\)\s*\w+\s*\(", re.DOTALL)
_BINDING = re.compile(r"\.(\w+)\s*\(")


def _module_parameters() -> dict[str, set[str]]:
    """Declared parameter names for every dau module, by module name."""
    table: dict[str, set[str]] = {}
    for path in sorted(_HDL_DIR.glob("*.sv")) + sorted(_HDL_DIR.glob("*.v")):
        source = path.read_text(encoding="utf-8")
        for match in _MODULE.finditer(source):
            name = match.group(1)
            # the parameter list runs to the port list; taking the text up to
            # the first `) (` after the module header is enough for the
            # single-module-per-file convention these files follow
            tail = source[match.end() :]
            port_list = _PORT_LIST.search(tail)
            header = tail[: port_list.start() if port_list else len(tail)]
            table.setdefault(name, set()).update(_PARAMETER.findall(header))
    return table


def test_every_parameter_binding_names_a_declared_parameter() -> None:
    declared = _module_parameters()
    assert "dau_identity_registers" in declared, "parsed no modules -- the patterns above have gone stale"

    checked = 0
    for path in sorted(_HDL_DIR.glob("*.sv")) + sorted(_HDL_DIR.glob("*.v")):
        source = path.read_text(encoding="utf-8")
        for module, parameter_text in _INSTANTIATION.findall(source):
            if module not in declared:
                continue  # instantiating something outside this package
            for binding in _BINDING.findall(parameter_text):
                checked += 1
                assert binding in declared[module], (
                    f"{path.name} binds .{binding}() on {module}, which declares no such parameter "
                    f"(it has {sorted(declared[module])}). Vivado would fail elaboration here"
                )

    assert checked >= 10, f"parsed almost no parameter bindings ({checked}) -- the patterns above have gone stale"


def test_the_identity_wrapper_forwards_every_capability_parameter() -> None:
    """The specific regression, from the other direction: the wrapper is the
    only path some shells have to the identity block, so a capability it
    fails to forward is advertised as zero no matter what the shell stamps."""
    wrapper = (_HDL_DIR / "dau_identity_axil.v").read_text(encoding="utf-8")
    declared = _module_parameters()["dau_identity_registers"]

    forwarded = set(_BINDING.findall(_INSTANTIATION.search(wrapper).group(2)))
    missing = sorted(declared - forwarded)
    assert not missing, f"dau_identity_axil does not forward {missing}; a shell using the wrapper advertises them as zero"
