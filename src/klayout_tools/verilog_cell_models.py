"""Shared gate-level Verilog cell-model loader, sequential-element classifier,
and flat-netlist helpers (issue #8046).

Used by ``klt reset-coverage``, ``klt power-intent``, and
``klt functional-verification`` (``options.initial_state``). Each consumer
supplies its own ``error_cls`` exception class or factory so request-level
errors preserve each verb's exception type and diagnostic wording without
cross-verb catch-and-rewrap glue.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .verilog_netlist import VerilogNetlistError, parse_gate_level_verilog

ErrorFactory = Callable[[str], Exception]

POWER_PINS = {"VPWR", "VGND", "VPB", "VNB", "VDD", "VSS", "VCC", "VPP"}
CONST_NETS = {"__CONST0__": 0, "__CONST1__": 1}

#: Clock/enable pins of a sequential cell.
CAPTURE_PINS = ("CLK", "GATE", "CLK_N", "GATE_N")

#: Scan pin pairs: a scan cell's D is only captured while its scan enable
#: is off, which the force-D drive cannot guarantee. sky130 spells the
#: data/enable pair ``SCD``/``SCE``, ASAP7 ``SI``/``SE``; both pins of a
#: pair must be present -- a clock gate's lone ``SE`` is not a scan pin.
SCAN_PIN_PAIRS = (("SCD", "SCE"), ("SI", "SE"))

#: Recognised asynchronous control pins -> (kind, active level).
_ASYNC_PINS = {
    "RESET_B": ("reset", 0),
    "SET_B": ("set", 0),
    "RESET": ("reset", 1),
    "SET": ("set", 1),
    "RN": ("reset", 0),
    "SETN": ("set", 0),
}

# An escaped Verilog identifier ends at whitespace and can contain punctuation.
_IDENT = r"(?:\\[^\s]+(?=\s)|[A-Za-z_][\w$]*)"
_STMT_START = r"(?:^|(?<=;))\s*"
_SEQ_UDP_RE = re.compile(r"udp_(?:dff|dlatch)(?:\$|$)")
_PRIMITIVE_RE = re.compile(
    r"\bprimitive\s+(\\?\S+?)\s*\((.*?)\)\s*;(.*?)\bendprimitive\b", re.S
)
_TABLE_RE = re.compile(r"\btable\b(.*?)\bendtable\b", re.S)
_REG_DECL_RE = re.compile(r"\breg\b\s*(?:\[[^\]]*\]\s*)?([^;]+);")
_MODULE_RE = re.compile(r"\bmodule\s+(\\?\S+?)\s*\((.*?)\)\s*;(.*?)\bendmodule\b", re.S)
_DIR_RE = re.compile(r"\b(input|output|inout)\b\s*(?:\[[^\]]*\]\s*)?([^;]+);")
_COMMENT_RE = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
_GATE_PRIMS = {"buf", "not"}
_KEYWORDS = {
    "module",
    "input",
    "output",
    "inout",
    "wire",
    "reg",
    "assign",
    "supply0",
    "supply1",
}

#: Verilog keywords that can precede a ``(`` without being an
#: instantiation. Only gates and UDPs may be instantiated without an
#: instance name (``altos_dff (q, v, clk, d)``); every other ``keyword (``
#: shape -- ``if (...)`` in an ``always`` block, ``ifnone (...)`` in a ``specify``
#: block -- is a statement, not an instance.
_NON_INSTANCE_TYPES = _KEYWORDS | {
    "always",
    "begin",
    "case",
    "casex",
    "casez",
    "deassign",
    "defparam",
    "disable",
    "else",
    "end",
    "endcase",
    "endfunction",
    "endgenerate",
    "endmodule",
    "endprimitive",
    "endspecify",
    "endtask",
    "endtable",
    "event",
    "for",
    "forever",
    "function",
    "generate",
    "if",
    "ifnone",
    "initial",
    "integer",
    "localparam",
    "macromodule",
    "parameter",
    "primitive",
    "real",
    "realtime",
    "repeat",
    "specify",
    "task",
    "time",
    "wait",
    "while",
}

# Match the whole instance prefix once, including an optional gate/UDP delay
# and instance name. The name may start on another line. Skip reserved words
# before matching so a keyword cannot consume an instance on the next line.
_RESERVED = "|".join(sorted(_NON_INSTANCE_TYPES))
_DELAY_VALUE = r"(?:\([^;]*?\)|\\\S+|`?[\w$.'+-]+)"
_INSTANCE_RE = re.compile(
    rf"{_STMT_START}(?!(?:{_RESERVED})(?![\w$]))({_IDENT})\s*"
    rf"(?:#\s*{_DELAY_VALUE}\s*)?(?:`\w+\s+)?(?:{_IDENT}\s*)?\(",
    re.M,
)

#: A net-type keyword in a port declaration (``input wire CLK``), which the
#: real sky130 cell models carry on every port.
_NET_TYPE_RE = re.compile(r"^(?:wire|reg|logic|tri)\s+")


# --------------------------------------------------------------------- models


class CellModel:
    def __init__(self, name: str, inputs: list[str], outputs: list[str]) -> None:
        self.name = name
        self.inputs = inputs
        self.outputs = outputs
        self.sequential = False
        #: async pin -> (kind, active level); sequential cells only.
        self.async_pins: dict[str, tuple[str, int]] = {}
        #: inversion parity (0/1) when this is a pure buffer/inverter cell.
        self.passthrough_parity: int | None = None
        #: Verilog primitive / sub-module types the model instantiates.
        self.primitives: list[str] = []


def read_text(path: str, what: str, *, error_cls: ErrorFactory) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError as exc:
        raise error_cls(f"cannot read {what} {path!r}: {exc.strerror or exc}") from exc


def _names(decl: str) -> list[str]:
    return [
        _NET_TYPE_RE.sub("", n.strip()).lstrip("\\")
        for n in decl.split(",")
        if n.strip()
    ]


def _is_state_udp(udps: dict[str, bool], primitive: str) -> bool:
    """A declared primitive keeps its declared classification -- including
    combinational. Only a type no supplied file declares falls back on the
    sky130 ``udp_dff*``/``udp_dlatch*`` name."""
    if primitive in udps:
        return udps[primitive]
    return _SEQ_UDP_RE.search(primitive) is not None


def _build_model(name: str, body: str, udps: dict[str, bool]) -> CellModel:
    inputs: list[str] = []
    outputs: list[str] = []
    for direction, decl in _DIR_RE.findall(body):
        (inputs if direction == "input" else outputs).extend(_names(decl))
    model = CellModel(name, inputs, outputs)
    instances = _INSTANCE_RE.findall(body)
    model.primitives = [inst.lstrip("\\") for inst in instances]
    model.sequential = any(_is_state_udp(udps, t) for t in model.primitives)
    if model.sequential:
        for pin in inputs:
            if pin in _ASYNC_PINS:
                model.async_pins[pin] = _ASYNC_PINS[pin]
        return model
    signal_inputs = [p for p in inputs if p not in POWER_PINS]
    only_wires = instances and all(
        t in _GATE_PRIMS or t.split("$")[0].rsplit("__", 1)[-1] == "udp_pwrgood_pp"
        for t in instances
    )
    if only_wires and len(signal_inputs) == 1 and len(outputs) == 1:
        model.passthrough_parity = sum(1 for t in instances if t == "not") % 2
    return model


def _holds_state(header: str, body: str) -> bool:
    """Whether a ``primitive`` is a state-holding UDP: its output is a
    declared register and its table's rows read ``inputs : current :
    next`` (a combinational UDP's single row is ``inputs : output``).

    The output and its register are declared either in the body
    (``output q; reg q;``) or in the header itself (``primitive p (output reg
    q, input d, clk);``); both spellings are read."""
    decls = "".join(f"{p.strip()};" for p in header.split(",") if p.strip()) + body
    outputs: set[str] = set()
    for direction, decl in _DIR_RE.findall(decls):
        if direction == "output":
            outputs.update(_names(decl))
    regs = {n for decl in _REG_DECL_RE.findall(decls) for n in _names(decl)}
    if not regs & outputs:
        return False
    table = _TABLE_RE.search(body)
    if table is None:
        return False
    return any(row.count(":") == 2 for row in table.group(1).split(";") if row.strip())


def _udp_declarations(texts: list[str]) -> dict[str, bool]:
    """Every ``primitive`` declared across the supplied files -> whether it
    holds state, the first declaration winning (ASAP7 re-declares each UDP
    under a second ``ifdef`` branch, the way sky130 re-declares each module).
    A combinational declaration is recorded too, so it is never mistaken for
    an undeclared type."""
    udps: dict[str, bool] = {}
    for text in texts:
        for match in _PRIMITIVE_RE.finditer(text):
            name = match.group(1).lstrip("\\")
            if name not in udps:
                udps[name] = _holds_state(match.group(2), match.group(3))
    return udps


def load_cell_models(
    paths: list[str], *, error_cls: ErrorFactory
) -> dict[str, CellModel]:
    """Scan Verilog cell-model files; the first definition of a cell wins
    (sky130 ships each module several times under ``ifdef`` branches).

    A cell model is *sequential* when its body instantiates a state-holding
    UDP -- any ``primitive`` declared across the supplied files whose output
    is a register (declared in the body or the header) and whose table rows
    read ``inputs : current : next`` -- with or without an instance name
    (ASAP7's ``altos_dff*``/``altos_latch*`` family is unnamed), or a
    ``udp_dff*``/``udp_dlatch*`` primitive by name (the sky130 fallback for
    cell wrappers supplied without the primitive declarations -- used only
    when no supplied file declares the primitive, so a declared
    combinational UDP stays combinational whatever it is called). The
    primitives are indexed across every file before any module is
    classified, so a UDP declared in a different file is recognised
    regardless of source order."""
    texts = [
        _COMMENT_RE.sub("", read_text(path, "cell-model file", error_cls=error_cls))
        for path in paths
    ]
    udps = _udp_declarations(texts)
    models: dict[str, CellModel] = {}
    for text in texts:
        for match in _MODULE_RE.finditer(text):
            name = match.group(1).lstrip("\\")
            if name in models:
                continue
            header_ports = _names(match.group(2))
            body = match.group(3)
            # ANSI-style headers carry directions in the port list itself.
            if not _DIR_RE.search(body):
                body = (
                    "".join(
                        f"{p.strip()};" for p in match.group(2).split(",") if p.strip()
                    )
                    + body
                )
            model = _build_model(name, body, udps)
            if not model.inputs and not model.outputs:
                model.inputs = header_ports
            models[name] = model
    if not models:
        raise error_cls("no module found in the cell-model file(s)")
    return models


# --------------------------------------------------------------- flat netlists


def load_flat_modules(
    netlist_path: str, *, error_cls: ErrorFactory
) -> list[dict[str, Any]]:
    text = read_text(netlist_path, "netlist", error_cls=error_cls)
    try:
        return parse_gate_level_verilog(text)
    except VerilogNetlistError as exc:
        raise error_cls(f"cannot parse netlist {netlist_path!r}: {exc}") from exc


def select_top_module(
    modules: list[dict[str, Any]],
    top: str | None,
    *,
    error_cls: ErrorFactory,
) -> tuple[dict[str, Any], set[str]]:
    by_name = {str(m["name"]): m for m in modules}
    if top is None:
        if len(modules) != 1:
            raise error_cls(
                f"netlist has {len(modules)} modules; name the top with --top "
                f"(one of {sorted(by_name)})"
            )
        return modules[0], set(by_name)
    if top in by_name:
        return by_name[top], set(by_name)
    raise error_cls(f"top module {top!r} not in netlist (have {sorted(by_name)})")


def reject_hierarchical_instances(
    module: dict[str, Any],
    netlist_modules: set[str],
    models: dict[str, CellModel],
    *,
    error_cls: ErrorFactory,
) -> None:
    for inst in module["instances"]:
        if inst["cell"] not in models and inst["cell"] in netlist_modules:
            raise error_cls(
                f"instance {inst['name']!r} is of module {inst['cell']!r} "
                "defined in the netlist; hierarchical netlists are not "
                "supported -- flatten first"
            )


def sequential_elements(
    netlist_path: str,
    cell_model_paths: list[str],
    top: str,
    *,
    error_cls: ErrorFactory,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Every sequential instance of module ``top`` in the gate-level netlist,
    with the pins a testbench-independent initial-state drive needs (issue
    #2998, ``options.initial_state`` of ``klt functional-verification``).

    Returns ``(drivable, unsupported)``. A ``drivable`` entry is
    ``{"instance", "cell", "d_pin", "d_net", "capture_pin", "capture_net"}``:
    force ``d_pin`` and let the clock/enable at ``capture_net`` capture it.
    ``unsupported`` entries (``{"instance", "cell", "reason"}``) are state the
    force-D drive cannot reach: a hard macro, a sequential cell whose model
    has no ``D`` pin or no recognised clock/enable pin, or a scan cell.
    """
    models = load_cell_models(cell_model_paths, error_cls=error_cls)
    modules = load_flat_modules(netlist_path, error_cls=error_cls)
    module, netlist_modules = select_top_module(modules, top, error_cls=error_cls)
    reject_hierarchical_instances(module, netlist_modules, models, error_cls=error_cls)
    drivable: list[dict[str, Any]] = []
    unsupported: list[dict[str, str]] = []
    for inst in module["instances"]:
        model = models.get(inst["cell"])
        if model is None:
            reason = "hard_macro"
        elif not model.sequential:
            continue
        else:
            capture = [p for p in model.inputs if p in CAPTURE_PINS]
            if any(all(p in model.inputs for p in pair) for pair in SCAN_PIN_PAIRS):
                reason = "scan_cell"
            elif "D" not in model.inputs or len(capture) != 1:
                reason = "no_d_or_clock_pin"
            else:
                drivable.append(
                    {
                        "instance": inst["name"],
                        "cell": inst["cell"],
                        "d_pin": "D",
                        "d_net": inst["connections"].get("D"),
                        "capture_pin": capture[0],
                        "capture_net": inst["connections"].get(capture[0]),
                    }
                )
                continue
        unsupported.append(
            {"instance": inst["name"], "cell": inst["cell"], "reason": reason}
        )
    return drivable, unsupported
