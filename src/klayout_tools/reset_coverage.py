"""Reset-coverage report for a gate-level Verilog netlist: which sequential
elements does an asynchronous reset *not* reach (issue #2998)?

Silicon flops do not power up at 0, so a common digital sign-off question is
"from the reset pin alone, does every state element get reset?". This module
answers the structural half of it. Given a flat gate-level netlist, the PDK's
functional cell models and the name of the reset port, it classifies every
instance:

* a *sequential* cell (its model instantiates a state-holding UDP -- a
  ``primitive`` declared in the supplied files whose output is a register
  and whose table rows read ``inputs : current : next``, instantiated with
  or without an instance name; sky130's ``udp_dff*``/``udp_dlatch*``
  spelling is kept as a name fallback for wrappers supplied without their
  primitives' declarations) is **covered** when at least one of its
  asynchronous reset/set
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
data and never prints, mirroring ``socket_check.py``. Netlist parsing and
cell-model loading live in :mod:`klayout_tools.verilog_netlist` and
:mod:`klayout_tools.verilog_cell_models`. Cell-model pin names recognised as
asynchronous controls are the sky130 family's
(``RESET_B``/``SET_B``/``RESET``/``SET``) plus the common ``RN``/``SETN``
spellings; headless, no PDK data is vendored.
"""

from __future__ import annotations

import json
from typing import Any

from .verilog_cell_models import (
    CONST_NETS,
    POWER_PINS,
    CellModel,
    load_cell_models,
    load_flat_modules,
    read_text,
    reject_hierarchical_instances,
    select_top_module,
)
from .verilog_cell_models import (
    sequential_elements as _sequential_elements,
)

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


class ResetCoverageError(Exception):
    """A request-level problem: unreadable/unparsable inputs or a bad waiver
    file. Maps to exit code 1; never a coverage finding."""


# ------------------------------------------------------------------- tracing


def _trace(
    net: str,
    drivers: dict[str, tuple[dict, str]],
    models: dict[str, CellModel],
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
        if net in CONST_NETS:
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
            in_pin = next(p for p in model.inputs if p not in POWER_PINS)
            nxt = inst["connections"].get(in_pin)
            if not isinstance(nxt, str):
                return "not_from_reset_port", parity
            net = nxt
            continue
        if not [p for p in model.inputs if p not in POWER_PINS]:
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
        doc = json.loads(read_text(path, "waiver file", error_cls=ResetCoverageError))
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


def _classify_elements(
    module: dict,
    models: dict[str, CellModel],
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
    models = load_cell_models(cell_model_paths, error_cls=ResetCoverageError)
    modules = load_flat_modules(netlist_path, error_cls=ResetCoverageError)
    module, netlist_modules = select_top_module(
        modules, top, error_cls=ResetCoverageError
    )
    if reset_port not in module["ports"]:
        raise ResetCoverageError(
            f"reset port {reset_port!r} is not a port of module {module['name']!r}"
        )
    waivers = load_waivers(waiver_path) if waiver_path else []
    reject_hierarchical_instances(
        module, netlist_modules, models, error_cls=ResetCoverageError
    )
    elements = _classify_elements(module, models, reset_port, asserted)

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


def sequential_elements(
    netlist_path: str, cell_model_paths: list[str], top: str
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Compatibility wrapper around :func:`.verilog_cell_models.sequential_elements`
    bound to :class:`ResetCoverageError`."""
    return _sequential_elements(
        netlist_path, cell_model_paths, top, error_cls=ResetCoverageError
    )
