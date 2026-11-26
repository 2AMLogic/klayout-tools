"""Read gate-level cell models for reset coverage, power intent and initial state."""

from __future__ import annotations

import re

_POWER_PINS = {"VPWR", "VGND", "VPB", "VNB", "VDD", "VSS", "VCC", "VPP"}

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
#: shape -- ``if (…)`` in an ``always`` block, ``ifnone (…)`` in a ``specify``
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


class ResetCoverageError(Exception):
    """A request-level problem: unreadable/unparsable inputs or a bad waiver
    file. Maps to exit code 1; never a coverage finding."""


# --------------------------------------------------------------------- models


class _CellModel:
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


def _read(path: str, what: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError as exc:
        raise ResetCoverageError(
            f"cannot read {what} {path!r}: {exc.strerror or exc}"
        ) from exc


#: A net-type keyword in a port declaration (``input wire CLK``), which the
#: real sky130 cell models carry on every port.
_NET_TYPE_RE = re.compile(r"^(?:wire|reg|logic|tri)\s+")


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


def _build_model(name: str, body: str, udps: dict[str, bool]) -> _CellModel:
    inputs: list[str] = []
    outputs: list[str] = []
    for direction, decl in _DIR_RE.findall(body):
        (inputs if direction == "input" else outputs).extend(_names(decl))
    model = _CellModel(name, inputs, outputs)
    instances = _INSTANCE_RE.findall(body)
    model.primitives = [name.lstrip("\\") for name in instances]
    model.sequential = any(_is_state_udp(udps, t) for t in model.primitives)
    if model.sequential:
        for pin in inputs:
            if pin in _ASYNC_PINS:
                model.async_pins[pin] = _ASYNC_PINS[pin]
        return model
    signal_inputs = [p for p in inputs if p not in _POWER_PINS]
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


def load_cell_models(paths: list[str]) -> dict[str, _CellModel]:
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
    texts = [_COMMENT_RE.sub("", _read(path, "cell-model file")) for path in paths]
    udps = _udp_declarations(texts)
    models: dict[str, _CellModel] = {}
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
        raise ResetCoverageError("no module found in the cell-model file(s)")
    return models
