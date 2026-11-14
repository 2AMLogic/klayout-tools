"""Reset-coverage report for a gate-level Verilog netlist: which sequential
elements does an asynchronous reset *not* reach (issue #2998)?

Silicon flops do not power up at 0, so a common digital sign-off question is
"from the reset pin alone, does every state element get reset?". This module
answers the structural half of it. Given a flat gate-level netlist, the PDK's
functional cell models and the name of the reset port, it classifies every
instance:

* a *sequential* cell (its model instantiates a ``udp_dff*``/``udp_dlatch*``
  primitive) is **covered** when at least one of its asynchronous reset/set
  pins is driven from the reset port through only buffer/inverter cells with
  the right net polarity (the pin is asserted whenever the port is);
* every other sequential cell is **unreset**, with a machine-readable reason
  code (see :data:`REASONS`);
* an instance whose cell type has no model and is not a module of the netlist
  (a hard macro: SRAM, IP block) is **unreset** with reason ``hard_macro``.

A waiver file lets a design justify an unreset element; a new unwaived entry
fails (``fail-on-unlisted``) and a waiver naming no unreset element fails
(``fail-on-stale``).

Pure library: :func:`run_reset_coverage` returns plain JSON-serialisable
data and never prints, mirroring ``socket_check.py``. Netlist parsing is
:func:`klayout_tools.verilog_netlist.parse_gate_level_verilog`; this module
adds no Verilog grammar of its own beyond a narrow scan of cell-model files.
Cell-model pin names recognised as asynchronous controls are the sky130
family's (``RESET_B``/``SET_B``/``RESET``/``SET``) plus the common
``RN``/``SETN`` spellings; headless, no PDK data is vendored.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .verilog_netlist import VerilogNetlistError, parse_gate_level_verilog

SCHEMA_VERSION = 1

#: Reason codes for an unreset element / unreached pin.
REASONS = {
    "no_async_pin": "sequential cell has no asynchronous reset/set pin",
    "async_pin_unconnected": "the asynchronous pin is not connected in the netlist",
    "tied_off": "the asynchronous pin is tied to a constant (or a tie cell)",
    "gated_by_logic": (
        "the asynchronous pin is driven through logic other than buffers/inverters"
    ),
    "wrong_polarity": "reset port reaches the pin with the opposite sense",
    "not_from_reset_port": "the net chain ends somewhere other than the reset port",
    "hard_macro": "instance of a cell with no functional model (hard macro)",
}

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

_SEQ_UDP_RE = re.compile(r"udp_(?:dff|dlatch)(?:\$|$)")
_MODULE_RE = re.compile(r"\bmodule\s+(\\?\S+?)\s*\((.*?)\)\s*;(.*?)\bendmodule\b", re.S)
_DIR_RE = re.compile(r"\b(input|output|inout)\b\s*(?:\[[^\]]*\]\s*)?([^;]+);")
_STMT_RE = re.compile(r"^\s*([A-Za-z_\\][\w$\\]*)\s+(?:`\w+\s+)?([\w$\\]+)\s*\(", re.M)
_COMMENT_RE = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
_CONST_NETS = {"__CONST0__": 0, "__CONST1__": 1}
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


def _build_model(name: str, body: str) -> _CellModel:
    inputs: list[str] = []
    outputs: list[str] = []
    for direction, decl in _DIR_RE.findall(body):
        (inputs if direction == "input" else outputs).extend(_names(decl))
    model = _CellModel(name, inputs, outputs)
    instances = [(t, n) for t, n in _STMT_RE.findall(body) if t not in _KEYWORDS]
    model.primitives = [t for t, _ in instances]
    model.sequential = any(_SEQ_UDP_RE.search(t) for t, _ in instances)
    if model.sequential:
        for pin in inputs:
            if pin in _ASYNC_PINS:
                model.async_pins[pin] = _ASYNC_PINS[pin]
        return model
    signal_inputs = [p for p in inputs if p not in _POWER_PINS]
    only_wires = instances and all(
        t in _GATE_PRIMS or t.split("$")[0].rsplit("__", 1)[-1] == "udp_pwrgood_pp"
        for t, _ in instances
    )
    if only_wires and len(signal_inputs) == 1 and len(outputs) == 1:
        model.passthrough_parity = sum(1 for t, _ in instances if t == "not") % 2
    return model


def load_cell_models(paths: list[str]) -> dict[str, _CellModel]:
    """Scan Verilog cell-model files; the first definition of a cell wins
    (sky130 ships each module several times under ``ifdef`` branches)."""
    models: dict[str, _CellModel] = {}
    for path in paths:
        text = _COMMENT_RE.sub("", _read(path, "cell-model file"))
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
            model = _build_model(name, body)
            if not model.inputs and not model.outputs:
                model.inputs = header_ports
            models[name] = model
    if not models:
        raise ResetCoverageError("no module found in the cell-model file(s)")
    return models


# ------------------------------------------------------------------- tracing


def _trace(
    net: str,
    drivers: dict[str, tuple[dict, str]],
    models: dict[str, _CellModel],
    reset_port: str,
) -> tuple[str, int]:
    """Walk ``net`` back through buffers/inverters. Returns ``(outcome,
    parity)``: outcome ``"reset_port"`` | ``"tied_off"`` | ``"gated_by_logic"``
    | ``"not_from_reset_port"``; parity is the inversion count mod 2."""
    parity = 0
    seen: set[str] = set()
    while True:
        if net == reset_port:
            return "reset_port", parity
        if net in _CONST_NETS:
            return "tied_off", parity
        if net in seen:
            return "gated_by_logic", parity
        seen.add(net)
        driver = drivers.get(net)
        if driver is None:
            return "not_from_reset_port", parity
        inst, _out_pin = driver
        model = models.get(inst["cell"])
        if model is None:
            return "gated_by_logic", parity
        if model.passthrough_parity is not None:
            parity ^= model.passthrough_parity
            in_pin = next(p for p in model.inputs if p not in _POWER_PINS)
            nxt = inst["connections"].get(in_pin)
            if not isinstance(nxt, str):
                return "not_from_reset_port", parity
            net = nxt
            continue
        if not [p for p in model.inputs if p not in _POWER_PINS]:
            return "tied_off", parity  # tie cell (conb): no signal inputs
        return "gated_by_logic", parity


def _classify_pin(
    pin: str,
    kind: str,
    level: int,
    inst: dict,
    drivers: dict,
    models: dict,
    reset_port: str,
    asserted: int,
) -> dict[str, Any]:
    net = inst["connections"].get(pin)
    entry: dict[str, Any] = {
        "pin": pin,
        "kind": kind,
        "net": net if isinstance(net, str) else None,
    }
    if not isinstance(net, str):
        entry["status"], entry["reason"] = "unreached", "async_pin_unconnected"
        return entry
    outcome, parity = _trace(net, drivers, models, reset_port)
    if outcome == "reset_port":
        if (asserted ^ parity) == level:
            entry["status"], entry["reason"] = "reached", None
        else:
            entry["status"], entry["reason"] = "unreached", "wrong_polarity"
    else:
        entry["status"], entry["reason"] = "unreached", outcome
    return entry


# ------------------------------------------------------------------- waivers


def load_waivers(path: str) -> list[dict[str, str]]:
    """``{"waivers": [{"instance": ..., "justification": ...,
    "reason": <optional code>}]}``. Each needs a non-empty justification."""
    try:
        doc = json.loads(_read(path, "waiver file"))
    except json.JSONDecodeError as exc:
        raise ResetCoverageError(
            f"waiver file {path!r} is not valid JSON: {exc}"
        ) from exc
    entries = doc.get("waivers") if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        raise ResetCoverageError(
            f"waiver file {path!r}: expected an object with a 'waivers' list"
        )
    waivers: list[dict[str, str]] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get("instance"), str):
            raise ResetCoverageError(f"waivers[{i}]: 'instance' must be a string")
        just = entry.get("justification")
        if not isinstance(just, str) or not just.strip():
            raise ResetCoverageError(
                f"waivers[{i}] ({entry['instance']}): 'justification' must be "
                "a non-empty string"
            )
        reason = entry.get("reason")
        if reason is not None and reason not in REASONS:
            raise ResetCoverageError(
                f"waivers[{i}] ({entry['instance']}): unknown reason {reason!r}; "
                f"expected one of {sorted(REASONS)}"
            )
        waivers.append(
            {
                "instance": entry["instance"],
                "justification": just,
                "reason": reason or "",
            }
        )
    return waivers


# ---------------------------------------------------------------------- main


def _select_module(modules: list[dict], top: str | None, reset_port: str) -> dict:
    by_name = {m["name"]: m for m in modules}
    if top is None:
        if len(modules) != 1:
            raise ResetCoverageError(
                f"netlist has {len(modules)} modules; name the top with --top "
                f"(one of {sorted(by_name)})"
            )
        module = modules[0]
    elif top in by_name:
        module = by_name[top]
    else:
        raise ResetCoverageError(
            f"top module {top!r} not in netlist (have {sorted(by_name)})"
        )
    if reset_port not in module["ports"]:
        raise ResetCoverageError(
            f"reset port {reset_port!r} is not a port of module {module['name']!r}"
        )
    return module


def _classify_elements(
    module: dict,
    netlist_modules: set[str],
    models: dict[str, _CellModel],
    reset_port: str,
    asserted: int,
) -> list[dict[str, Any]]:
    drivers: dict[str, tuple[dict, str]] = {}
    for inst in module["instances"]:
        model = models.get(inst["cell"])
        for out in model.outputs if model else []:
            net = inst["connections"].get(out)
            if isinstance(net, str):
                drivers[net] = (inst, out)

    elements: list[dict[str, Any]] = []
    for inst in module["instances"]:
        model = models.get(inst["cell"])
        if model is None:
            if inst["cell"] in netlist_modules:
                raise ResetCoverageError(
                    f"instance {inst['name']!r} is of module {inst['cell']!r} "
                    "defined in the netlist; hierarchical netlists are not "
                    "supported -- flatten first"
                )
            elements.append(_element(inst, "unreset", "hard_macro", []))
        elif not model.sequential:
            continue
        elif not model.async_pins:
            elements.append(_element(inst, "unreset", "no_async_pin", []))
        else:
            pins = [
                _classify_pin(p, k, lvl, inst, drivers, models, reset_port, asserted)
                for p, (k, lvl) in model.async_pins.items()
            ]
            if any(p["status"] == "reached" for p in pins):
                elements.append(_element(inst, "covered", None, pins))
            else:
                elements.append(_element(inst, "unreset", pins[0]["reason"], pins))
    return elements


def run_reset_coverage(
    netlist_path: str,
    cell_model_paths: list[str],
    reset_port: str,
    *,
    reset_polarity: str = "low",
    top: str | None = None,
    waiver_path: str | None = None,
) -> dict[str, Any]:
    """Build the reset-coverage report. See the module docstring."""
    if reset_polarity not in ("low", "high"):
        raise ResetCoverageError("reset_polarity must be 'low' or 'high'")
    asserted = 0 if reset_polarity == "low" else 1
    models = load_cell_models(cell_model_paths)
    try:
        modules = parse_gate_level_verilog(_read(netlist_path, "netlist"))
    except VerilogNetlistError as exc:
        raise ResetCoverageError(
            f"cannot parse netlist {netlist_path!r}: {exc}"
        ) from exc
    module = _select_module(modules, top, reset_port)
    waivers = load_waivers(waiver_path) if waiver_path else []
    elements = _classify_elements(
        module, {str(m["name"]) for m in modules}, models, reset_port, asserted
    )

    return _apply_waivers(
        {
            "schema_version": SCHEMA_VERSION,
            "netlist": netlist_path,
            "module": module["name"],
            "reset_port": reset_port,
            "reset_polarity": reset_polarity,
            "elements": elements,
        },
        waivers,
        waiver_path,
    )


def _element(
    inst: dict, status: str, reason: str | None, pins: list[dict]
) -> dict[str, Any]:
    return {
        "instance": inst["name"],
        "cell": inst["cell"],
        "status": status,
        "reason": reason,
        "pins": pins,
        "waived": False,
        "justification": None,
    }


def _apply_waivers(
    report: dict[str, Any], waivers: list[dict[str, str]], waiver_path: str | None
) -> dict[str, Any]:
    unreset = {e["instance"]: e for e in report["elements"] if e["status"] == "unreset"}
    stale: list[dict[str, str]] = []
    for waiver in waivers:
        element = unreset.get(waiver["instance"])
        if element is None or (
            waiver["reason"] and waiver["reason"] != element["reason"]
        ):
            stale.append(waiver)
            continue
        element["waived"] = True
        element["justification"] = waiver["justification"]
    unwaived = [e["instance"] for e in unreset.values() if not e["waived"]]
    covered = sum(1 for e in report["elements"] if e["status"] == "covered")
    report["waiver_file"] = waiver_path
    report["stale_waivers"] = stale
    report["summary"] = {
        "state_elements": len(report["elements"]),
        "covered": covered,
        "unreset": len(unreset),
        "waived": len(unreset) - len(unwaived),
        "unwaived": len(unwaived),
        "stale_waivers": len(stale),
    }
    report["status"] = "fail" if unwaived or stale else "pass"
    return report


def reason_description(code: str) -> str:
    return REASONS.get(code, code)


# ------------------------------------------------- initial-state drive list

#: Clock/enable pins of a sequential cell.
_CAPTURE_PINS = ("CLK", "GATE", "CLK_N", "GATE_N")

#: Scan pins: a scan cell's D is only captured while its scan enable is off,
#: which the force-D drive cannot guarantee.
_SCAN_PINS = ("SCD", "SCE")


def sequential_elements(
    netlist_path: str, cell_model_paths: list[str], top: str
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
    models = load_cell_models(cell_model_paths)
    try:
        modules = parse_gate_level_verilog(_read(netlist_path, "netlist"))
    except VerilogNetlistError as exc:
        raise ResetCoverageError(
            f"cannot parse netlist {netlist_path!r}: {exc}"
        ) from exc
    by_name = {str(m["name"]): m for m in modules}
    if top not in by_name:
        raise ResetCoverageError(
            f"top module {top!r} not in netlist (have {sorted(by_name)})"
        )
    module: dict[str, Any] = by_name[top]
    drivable: list[dict[str, Any]] = []
    unsupported: list[dict[str, str]] = []
    for inst in module["instances"]:
        model = models.get(inst["cell"])
        if model is None:
            if inst["cell"] in by_name:
                raise ResetCoverageError(
                    f"instance {inst['name']!r} is of module {inst['cell']!r} "
                    "defined in the netlist; hierarchical netlists are not "
                    "supported -- flatten first"
                )
            reason = "hard_macro"
        elif not model.sequential:
            continue
        else:
            capture = [p for p in model.inputs if p in _CAPTURE_PINS]
            if any(p in model.inputs for p in _SCAN_PINS):
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
