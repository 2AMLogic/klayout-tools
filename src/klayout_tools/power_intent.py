"""Power-intent report for a gate-level Verilog netlist with a power-gated
domain (issue #4479).

A power-gated block is correct only if its isolation, state classification and
control sequencing are right. This module checks those structurally, from a
flat gate-level netlist, the PDK's functional cell models and a small JSON
intent file -- no simulator, no UPF/CPF:

1. **Isolation coverage.** Every net driven by a gated-domain instance that
   leaves the domain (read by an always-on instance, or a module port) must
   reach the always-on side only through a *clamp cell*: a cell whose model is
   a single ``and`` (clamps to 0) or ``or`` (clamps to 1) primitive, with every
   other signal input on the always-on side. Buffers/inverters between the
   boundary and the clamp are followed. Violations: ``no_clamp``,
   ``wrong_clamp_value``, ``undeclared_clamp_value``.
2. **State classification.** Every sequential instance is ``retained``
   (always-on) or ``gated`` per the intent file (``unclassified_state``
   otherwise), and a retained element must not be fed by a gated-domain net
   unless the feed is isolated by a clamp cell or the element's clock is
   declared stopped (``retained_fed_by_gated``).
3. **Sequencing**, on the intent file's *declared* event sequence only (no
   waveform/trace checking): ``iso_release``, ``reset_release`` and
   ``clk_restart`` each come after ``rail_good``, in the declared power-up
   order; ``iso_assert`` comes before ``switch_off``. Violations:
   ``out_of_order``, ``missing_event``.

Intent file (JSON object)::

    {"gated_instances": ["u_core"],
     "always_on": {"ports": ["iso_n"], "instances": []},
     "clamp_values": {"core_out": 0},
     "default_clamp": null,
     "retained": ["u_ret"],
     "stopped_clock_nets": [],
     "sequence": ["iso_assert", "switch_off", "rail_good", "iso_release",
                  "reset_release", "clk_restart"],
     "power_up_order": ["iso_release", "reset_release", "clk_restart"]}

Only ``gated_instances`` is required. Pure library: :func:`run_power_intent`
returns plain JSON-serialisable data and never prints. Parsing and cell-model
loading are shared with :mod:`.reset_coverage`.
"""

from __future__ import annotations

import json
from typing import Any

from .reset_coverage import (
    _CAPTURE_PINS,
    _CONST_NETS,
    _POWER_PINS,
    ResetCoverageError,
    _CellModel,
    _read,
    load_cell_models,
)
from .verilog_netlist import VerilogNetlistError, parse_gate_level_verilog

SCHEMA_VERSION = 1

#: Control events of the declared sequence.
EVENTS = (
    "rail_good",
    "iso_release",
    "reset_release",
    "clk_restart",
    "iso_assert",
    "switch_off",
)
DEFAULT_POWER_UP_ORDER = ("iso_release", "reset_release", "clk_restart")

#: Violation reason codes.
REASONS = {
    "no_clamp": "gated-domain output reaches the always-on side without a clamp cell",
    "wrong_clamp_value": "clamp cell clamps to a value other than the declared one",
    "undeclared_clamp_value": "output is clamped but no clamp value is declared for it",
    "unclassified_state": "sequential element is neither retained nor gated",
    "retained_fed_by_gated": (
        "retained element is fed by a gated-domain net with no isolation clamp "
        "and a running clock"
    ),
    "out_of_order": "control events occur in an order the intent forbids",
    "missing_event": "a control event required by the sequencing rules is absent",
}

_WIRE_PRIMS = {"buf"}


class PowerIntentError(Exception):
    """A request-level problem: unreadable/unparsable inputs or a bad intent
    file. Maps to exit code 1; never a finding."""


# -------------------------------------------------------------------- intent


class _Intent:
    def __init__(self, doc: dict, path: str) -> None:
        def strs(key: str, container: dict = doc) -> list[str]:
            value = container.get(key, [])
            if not isinstance(value, list) or not all(
                isinstance(v, str) for v in value
            ):
                raise PowerIntentError(
                    f"intent file {path!r}: {key!r} must be a list of strings"
                )
            return value

        self.gated = strs("gated_instances")
        if not self.gated:
            raise PowerIntentError(
                f"intent file {path!r}: 'gated_instances' must name an instance"
            )
        always_on = doc.get("always_on", {})
        if not isinstance(always_on, dict):
            raise PowerIntentError(
                f"intent file {path!r}: 'always_on' must be an object"
            )
        self.always_on_ports = strs("ports", always_on)
        self.always_on_instances = strs("instances", always_on)
        self.retained = strs("retained")
        self.stopped_clocks = strs("stopped_clock_nets")
        clamps = doc.get("clamp_values", {})
        if not isinstance(clamps, dict) or not all(
            v in (0, 1) for v in clamps.values()
        ):
            raise PowerIntentError(
                f"intent file {path!r}: 'clamp_values' must map net -> 0 or 1"
            )
        self.clamp_values: dict[str, int] = {k: int(v) for k, v in clamps.items()}
        default = doc.get("default_clamp")
        if default not in (None, 0, 1):
            raise PowerIntentError(
                f"intent file {path!r}: 'default_clamp' must be 0 or 1"
            )
        self.default_clamp = default
        sequence = doc.get("sequence")
        if sequence is not None:
            if not isinstance(sequence, list) or not all(
                isinstance(e, str) for e in sequence
            ):
                raise PowerIntentError(
                    f"intent file {path!r}: 'sequence' must be a list of event names"
                )
            unknown = sorted(set(sequence) - set(EVENTS))
            if unknown:
                raise PowerIntentError(
                    f"intent file {path!r}: unknown sequence event(s) {unknown}; "
                    f"expected {list(EVENTS)}"
                )
            if len(set(sequence)) != len(sequence):
                raise PowerIntentError(
                    f"intent file {path!r}: 'sequence' repeats an event"
                )
        self.sequence: list[str] | None = sequence
        order = doc.get("power_up_order", list(DEFAULT_POWER_UP_ORDER))
        if (
            not isinstance(order, list)
            or not order
            or not set(order) <= {"iso_release", "reset_release", "clk_restart"}
            or len(set(order)) != len(order)
        ):
            raise PowerIntentError(
                f"intent file {path!r}: 'power_up_order' must be a permutation of a "
                "subset of iso_release, reset_release, clk_restart"
            )
        self.power_up_order: list[str] = order
        both = sorted(set(self.gated) & set(self.retained))
        if both:
            raise PowerIntentError(
                f"intent file {path!r}: instance(s) {both} are both gated and retained"
            )


def load_intent(path: str) -> _Intent:
    try:
        doc = json.loads(_read(path, "intent file"))
    except json.JSONDecodeError as exc:
        raise PowerIntentError(
            f"intent file {path!r} is not valid JSON: {exc}"
        ) from exc
    except ResetCoverageError as exc:
        raise PowerIntentError(str(exc)) from exc
    if not isinstance(doc, dict):
        raise PowerIntentError(f"intent file {path!r}: expected a JSON object")
    return _Intent(doc, path)


# ------------------------------------------------------------------ analysis


def _nets(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def _signal_inputs(model: _CellModel) -> list[str]:
    return [p for p in model.inputs if p not in _POWER_PINS]


def _clamp_value(model: _CellModel | None) -> int | None:
    """0 for a single-``and`` cell, 1 for a single-``or`` cell, else ``None``."""
    if model is None or model.sequential or len(_signal_inputs(model)) < 2:
        return None
    logic = [
        p
        for p in model.primitives
        if p not in _WIRE_PRIMS
        and p.split("$")[0].rsplit("__", 1)[-1] != "udp_pwrgood_pp"
    ]
    if logic == ["and"]:
        return 0
    if logic == ["or"]:
        return 1
    return None


class _Design:
    def __init__(
        self, module: dict, models: dict[str, _CellModel], intent: _Intent
    ) -> None:
        self.module = module
        self.models = models
        self.intent = intent
        self.insts = {i["name"]: i for i in module["instances"]}
        for name in (*intent.gated, *intent.retained, *intent.always_on_instances):
            if name not in self.insts:
                raise PowerIntentError(
                    f"intent names instance {name!r} which is not in module "
                    f"{module['name']!r}"
                )
        for port in intent.always_on_ports:
            if port not in module["ports"]:
                raise PowerIntentError(
                    f"intent names always-on port {port!r} which is not a port of "
                    f"module {module['name']!r}"
                )
        self.gated = set(intent.gated)
        #: net -> driving instance (modelled outputs only)
        self.driver: dict[str, dict] = {}
        #: net -> [(instance, pin)] reading it
        self.readers: dict[str, list[tuple[dict, str]]] = {}
        for inst in module["instances"]:
            model = models.get(inst["cell"])
            if model is None:
                if inst["name"] in self.gated:
                    raise PowerIntentError(
                        f"gated instance {inst['name']!r} has no cell model "
                        f"({inst['cell']!r}); its outputs cannot be determined"
                    )
                for pin, value in inst["connections"].items():
                    for net in _nets(value):
                        self.readers.setdefault(net, []).append((inst, pin))
                continue
            for pin, value in inst["connections"].items():
                for net in _nets(value):
                    if pin in model.outputs:
                        self.driver[net] = inst
                    elif pin not in _POWER_PINS:
                        self.readers.setdefault(net, []).append((inst, pin))

    def passthrough(self, inst: dict) -> bool:
        model = self.models.get(inst["cell"])
        return model is not None and model.passthrough_parity is not None

    def root(self, net: str) -> tuple[str, dict | None]:
        """Walk ``net`` back through buffers/inverters; return the root net and
        its driver (``None`` for a port/constant/unknown source)."""
        seen: set[str] = set()
        while net not in seen:
            seen.add(net)
            driver = self.driver.get(net)
            if driver is None or not self.passthrough(driver):
                return net, driver
            model = self.models[driver["cell"]]
            in_pin = _signal_inputs(model)[0]
            nxt = _nets(driver["connections"].get(in_pin))
            if not nxt:
                return net, driver
            net = nxt[0]
        return net, None

    def is_always_on(self, net: str) -> bool:
        if net in _CONST_NETS:
            return True
        root, driver = self.root(net)
        if driver is not None:
            if driver["name"] in self.gated:
                return False
            listed = self.intent.always_on_instances
            return not listed or driver["name"] in listed
        ports = self.intent.always_on_ports
        return not ports or root in ports or root in _CONST_NETS

    def clamp_controls_ok(self, inst: dict, data_pin: str) -> bool:
        model = self.models[inst["cell"]]
        others = [p for p in _signal_inputs(model) if p != data_pin]
        return all(
            nets and all(self.is_always_on(n) for n in nets)
            for nets in (_nets(inst["connections"].get(p)) for p in others)
        )


def _check_isolation(design: _Design) -> list[dict[str, Any]]:
    intent = design.intent
    ports = set(design.module["ports"])
    boundary: dict[str, dict] = {}
    for inst in design.module["instances"]:
        if inst["name"] not in design.gated:
            continue
        model = design.models[inst["cell"]]
        for pin in model.outputs:
            for net in _nets(inst["connections"].get(pin)):
                crosses = net in ports or any(
                    r[0]["name"] not in design.gated
                    for r in design.readers.get(net, [])
                )
                if crosses:
                    boundary[net] = inst

    results: list[dict[str, Any]] = []
    for net in sorted(boundary):
        findings: list[dict[str, Any]] = []
        clamps: list[dict[str, Any]] = []
        _follow(design, net, net, findings, clamps, set())
        expected = intent.clamp_values.get(net, intent.default_clamp)
        for clamp in clamps:
            if expected is None:
                findings.append(
                    {"reason": "undeclared_clamp_value", "sink": clamp["cell"]}
                )
            elif clamp["value"] != expected:
                findings.append(
                    {
                        "reason": "wrong_clamp_value",
                        "sink": clamp["cell"],
                        "clamp_value": clamp["value"],
                    }
                )
        results.append(
            {
                "net": net,
                "driver": boundary[net]["name"],
                "expected_clamp": expected,
                "clamps": clamps,
                "status": "fail" if findings else "pass",
                "findings": findings,
            }
        )
    return results


def _follow(
    design: _Design,
    origin: str,
    net: str,
    findings: list[dict[str, Any]],
    clamps: list[dict[str, Any]],
    seen: set[str],
) -> None:
    """Follow the readers of ``net`` (a gated-domain output) outside the
    domain: record each clamp reached, each unclamped sink."""
    if net in seen:
        return
    seen.add(net)
    if net in design.module["ports"]:
        findings.append({"reason": "no_clamp", "sink": f"port:{net}"})
    for inst, pin in design.readers.get(net, []):
        if inst["name"] in design.gated:
            continue
        model = design.models.get(inst["cell"])
        value = _clamp_value(model)
        if value is not None and design.clamp_controls_ok(inst, pin):
            clamps.append({"cell": inst["name"], "type": inst["cell"], "value": value})
        elif design.passthrough(inst):
            for out in model.outputs:  # type: ignore[union-attr]
                for nxt in _nets(inst["connections"].get(out)):
                    _follow(design, origin, nxt, findings, clamps, seen)
        else:
            findings.append({"reason": "no_clamp", "sink": f"{inst['name']}.{pin}"})


def _gated_feed(design: _Design, net: str, seen: set[str]) -> str | None:
    """Name of a gated-domain net reaching ``net`` through un-isolated
    combinational logic, or ``None``."""
    if net in seen:
        return None
    seen.add(net)
    driver = design.driver.get(net)
    if driver is None:
        return None
    if driver["name"] in design.gated:
        return net
    model = design.models[driver["cell"]]
    if model.sequential:
        return None
    pins = _signal_inputs(model)
    if _clamp_value(model) is not None:
        if any(
            p != q
            and all(design.is_always_on(n) for n in _nets(driver["connections"].get(p)))
            and _nets(driver["connections"].get(p))
            for p in pins
            for q in pins
        ):
            return None
    for pin in pins:
        for nxt in _nets(driver["connections"].get(pin)):
            found = _gated_feed(design, nxt, seen)
            if found:
                return found
    return None


def _check_state(design: _Design) -> list[dict[str, Any]]:
    intent = design.intent
    retained = set(intent.retained)
    elements: list[dict[str, Any]] = []
    for inst in design.module["instances"]:
        model = design.models.get(inst["cell"])
        listed = inst["name"] in retained
        if model is None or not model.sequential:
            continue
        entry: dict[str, Any] = {
            "instance": inst["name"],
            "cell": inst["cell"],
            "domain": None,
            "status": "pass",
            "findings": [],
        }
        if inst["name"] in design.gated:
            entry["domain"] = "gated"
        elif listed:
            entry["domain"] = "retained"
        else:
            entry["status"] = "fail"
            entry["findings"].append({"reason": "unclassified_state"})
        if entry["domain"] == "retained":
            capture = [p for p in model.inputs if p in _CAPTURE_PINS]
            stopped = any(
                design.root(n)[0] in intent.stopped_clocks or n in intent.stopped_clocks
                for p in capture
                for n in _nets(inst["connections"].get(p))
            )
            if not stopped:
                for pin in _signal_inputs(model):
                    for net in _nets(inst["connections"].get(pin)):
                        gated_net = _gated_feed(design, net, set())
                        if gated_net:
                            entry["status"] = "fail"
                            entry["findings"].append(
                                {
                                    "reason": "retained_fed_by_gated",
                                    "pin": pin,
                                    "gated_net": gated_net,
                                }
                            )
        elements.append(entry)
    return elements


def _check_sequence(intent: _Intent) -> tuple[str, list[dict[str, Any]]]:
    if intent.sequence is None:
        return "skipped", []
    seq = intent.sequence
    pairs = [("rail_good", e) for e in intent.power_up_order]
    pairs += list(zip(intent.power_up_order, intent.power_up_order[1:], strict=False))
    pairs.append(("iso_assert", "switch_off"))
    findings: list[dict[str, Any]] = []
    missing = {e for pair in pairs for e in pair if e not in seq}
    for event in sorted(missing):
        findings.append({"reason": "missing_event", "event": event})
    for before, after in pairs:
        if before in seq and after in seq and seq.index(before) > seq.index(after):
            findings.append(
                {"reason": "out_of_order", "before": before, "after": after}
            )
    return ("fail" if findings else "pass"), findings


def run_power_intent(
    netlist_path: str,
    cell_model_paths: list[str],
    intent_path: str,
    *,
    top: str | None = None,
) -> dict[str, Any]:
    """Build the power-intent report. See the module docstring."""
    intent = load_intent(intent_path)
    try:
        models = load_cell_models(cell_model_paths)
        modules = parse_gate_level_verilog(_read(netlist_path, "netlist"))
    except ResetCoverageError as exc:
        raise PowerIntentError(str(exc)) from exc
    except VerilogNetlistError as exc:
        raise PowerIntentError(f"cannot parse netlist {netlist_path!r}: {exc}") from exc
    by_name = {str(m["name"]): m for m in modules}
    if top is None:
        if len(modules) != 1:
            raise PowerIntentError(
                f"netlist has {len(modules)} modules; name the top with --top "
                f"(one of {sorted(by_name)})"
            )
        module = modules[0]
    elif top in by_name:
        module = by_name[top]
    else:
        raise PowerIntentError(
            f"top module {top!r} not in netlist (have {sorted(by_name)})"
        )
    for inst in module["instances"]:  # type: ignore[attr-defined]
        if inst["cell"] not in models and inst["cell"] in by_name:
            raise PowerIntentError(
                f"instance {inst['name']!r} is of module {inst['cell']!r} defined in "
                "the netlist; hierarchical netlists are not supported -- flatten first"
            )

    design = _Design(module, models, intent)  # type: ignore[arg-type]
    isolation = _check_isolation(design)
    state = _check_state(design)
    seq_status, seq_findings = _check_sequence(intent)

    violations: list[dict[str, Any]] = []
    for item in isolation:
        for f in item["findings"]:
            violations.append({"check": "isolation", "net": item["net"], **f})
    for el in state:
        for f in el["findings"]:
            violations.append({"check": "state", "instance": el["instance"], **f})
    for f in seq_findings:
        violations.append({"check": "sequencing", **f})

    def status(items: list[dict[str, Any]]) -> str:
        return "fail" if any(i["status"] == "fail" for i in items) else "pass"

    return {
        "schema_version": SCHEMA_VERSION,
        "netlist": netlist_path,
        "module": module["name"],
        "intent": intent_path,
        "checks": {
            "isolation": {"status": status(isolation), "boundary_nets": isolation},
            "state": {"status": status(state), "elements": state},
            "sequencing": {
                "status": seq_status,
                "sequence": intent.sequence,
                "power_up_order": intent.power_up_order,
                "findings": seq_findings,
            },
        },
        "violations": violations,
        "summary": {
            "boundary_nets": len(isolation),
            "state_elements": len(state),
            "violations": len(violations),
        },
        "status": "fail" if violations else "pass",
    }


def reason_description(code: str) -> str:
    return REASONS.get(code, code)
