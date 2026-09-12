"""Per-device operating-point sanity lint ("op-sanity") for a SPICE netlist.

Pure library: :func:`run_op_sanity` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints, mirroring ``sim.py`` /
``drc.py``. Serialisation and human-readable formatting live in the CLI
command module (``cli/sim_cmd.py``, behind ``klt sim --op-lint``); the same
entry point is also wired into ``klt eval`` as the ``op-sanity`` gate kind
(see ``eval.py``'s :data:`~klayout_tools.eval._INVOKE_FNS`).

Why this exists (issue #1718)
------------------------------
When an agent-authored netlist fails functionally, the feedback it gets back
is the *measurement* outcome (``gain_db`` ~ 0, no oscillation, a ``.meas``
miss) or a raw ngspice error. Neither names the device that is actually
wrong. This verb answers the narrower, far more actionable question --
**"which MOSFET is not doing its job, and why?"** -- by running a single
``.op`` analysis and walking every MOS instance in the netlist:

- **off** -- ``|Vgs| < |Vth|`` with ``Id`` at/below a noise floor.
- **triode** -- ``|Vds| < |Vdsat|`` plus a corner-aware margin.
- **wiring smells** -- drain tied to its own rail (NMOS drain on ground,
  PMOS drain on the supply), gate shorted to source, bulk tied to neither
  its own source nor the matching rail.
- **netlist hygiene** -- a declared (or ``.meas``-referenced) I/O node that
  does not exist in the netlist at all, and a node with only one terminal
  on it (floating).

This is a *diagnostic*, never a fixer: proposing the next candidate sizing
stays agent-side (the recorded ``#310`` scope decision). It is a clean-room
reimplementation of the per-device operating-point feedback idea published
in Lai et al., *AnalogCoder* (AAAI 2025, arXiv 2405.14918) -- reimplemented
from the paper against real PDK models, not from their (unlicensed) code.

Request document
-----------------
A ``klt sim`` request (``docs/cli/sim.md``) *is* an op-sanity request: only
``netlist`` is required, ``models``/``corners`` are read exactly as
``klt sim`` reads them, and ``analysis``/``measurements`` are ignored for
the analysis itself (this verb always runs a plain ``op``) -- though
``measurements[].spice`` is still mined for ``v(<node>)`` references, which
is where the "declared output node absent from the netlist" hygiene finding
comes from. An optional ``op_lint`` block tunes thresholds, declares extra
I/O nodes, and overrides per-model conventions; see ``docs/cli/sim.md``'s
"Operating-point lint" section for the full field reference.

Reading per-device values: ``print @m...[param]``, not ``.meas``
-----------------------------------------------------------------
ngspice has no ``.MEASURE OP`` statement (an operating point has no sweep
variable to search over -- see ``sim.py``'s ``_validate_meas_card`` and
issue #205), so per-device operating-point values can only be read with
``print @<element>[<param>]`` inside a ``.control`` block. That is exactly
the path ``klt size`` already uses for the single device it is sizing
(``size.py``'s ``_OP_PARAMS``/``_write_sweep_deck``/``_parse_sweep_log``);
this module generalises it from "the one sized device" to "every MOS
instance in an arbitrary netlist". No new SPICE-interaction pattern, and no
new dependency.

Instance-path resolution is the one wrinkle: a PDK device instantiated as
``X1 d g s b <model>`` exposes its op-point vectors at
``@m.<instance>.<inner element>[param]``, and the inner element name is a
*PDK convention*, not a standard -- sky130 names it ``m`` + the subcircuit
name (``msky130_fd_pr__nfet_01v8``), gf180mcu names it ``m0`` (both verified
against the installed PDKs with ngspice 46 while building this module).
Rather than guess one and silently report nothing, the generated deck
``print``s *every* candidate path for each device; an unknown vector name is
a non-fatal ``Error: no such device or model name`` log line in ngspice's
batch mode (also verified), so the run costs one process either way and the
candidate that actually answered is recorded per device in
``devices[].op_point_element``. ``op_lint.models.<model>.op_point_element``
overrides the candidate list for a PDK that follows neither convention.

Sign conventions: compare magnitudes
-------------------------------------
BSIM4 reports ``vgs``/``vds``/``vth``/``vdsat`` as **positive magnitudes for
a PMOS** (verified: a sky130 ``pfet_01v8`` at ``Vgs = -1.2 V`` reports
``vgs = 1.2``), while a bare SPICE level-1 device reports ``vdsat``
negative for the same device. Every threshold comparison here is therefore
on ``abs()`` -- which is also exactly how issue #1718 states the conditions
(``|Vgs| < |Vth|``, ``|Vds| < |Vdsat|``). Node-level facts that genuinely
need a sign (which rail a node sits on) come from the reported node
voltages, never from a device's reported terminal voltages.

Structural checks never need the simulator
-------------------------------------------
The wiring-smell and netlist-hygiene checks are computed from the parsed
netlist alone, so they are still reported when the ``.op`` itself fails to
converge (``status: "error"``) -- which is the case where an agent most
needs them. The simulation-side counterpart of "floating node" (ngspice's
singular-matrix / gmin-stepping narration) is not re-implemented here:
``sim.py``'s existing ``_classify_diagnostics``/#205 recovery logic is
called directly, so the two never disagree.

Exit codes (mirrored in ``cli/sim_cmd.py`` -- see ``docs/cli/sim.md``):
    0 - ran; no ``error``-severity finding (``status`` ``clean``, or
        ``findings`` with warnings only)
    1 - failed to run at all (bad request, unresolvable netlist/model
        library, unknown corner) -- raised here as :class:`OpSanityError`
    3 - ran; at least one ``error``-severity finding
    4 - the operating-point analysis itself did not produce a trustworthy
        result (``status: "error"``)
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand.) The 3-vs-4 split mirrors ``klt sim``'s own: a real finding must
never be confused with "the analysis never ran".
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Any

from . import env_provenance
from ._paths import _load_request_json, _resolve_relative, validate_request_shape
from ._provenance import build_provenance, sha256_file
from .pdk import PdkNotFoundError, find_pdk
from .sim import (
    _SIMULATION_ABORTED_RE,
    DEFAULT_TIMEOUT_S,
    CornerPoint,
    SimError,
    _classify_diagnostics,
    _expand_corners,
    _extract_engine_version,
    _resolve_models_lib,
)

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: Op-point vectors requested per device. Deliberately the same names
#: ``size.py``'s ``_OP_PARAMS`` uses (minus ``gm``, which no check here
#: needs), so both modules read one vocabulary out of ngspice.
_OP_PARAMS = ("vgs", "vth", "vds", "vdsat", "id")

#: Response-field name each :data:`_OP_PARAMS` entry parses into.
_OP_PARAM_KEYS = {
    "vgs": "vgs_v",
    "vth": "vth_v",
    "vds": "vds_v",
    "vdsat": "vdsat_v",
    "id": "id_a",
}

#: Boltzmann constant over elementary charge, in volts per kelvin -- the
#: thermal-voltage scale the default triode margin is expressed in (see
#: :func:`_triode_margin_v`).
_KT_OVER_Q_V_PER_K = 8.617333262e-5

#: Default multiple of the thermal voltage ``kT/q`` used as the triode
#: margin. 2 kT/q is ~52 mV at 27 C, ~40 mV at -40 C and ~69 mV at 125 C --
#: i.e. the margin widens with temperature exactly as the moderate-inversion
#: transition it is meant to cover does, which is what makes this
#: "corner-aware" rather than one hardcoded millivolt count. Deliberately
#: small: a device sitting *within* one transition region's width of
#: ``Vdsat`` is what this warns about, not a device comfortably in
#: saturation.
_DEFAULT_TRIODE_MARGIN_KT = 2.0

#: Drain-current magnitude at or below which a device whose ``|Vgs|`` is
#: already under ``|Vth|`` is reported **off** rather than merely
#: subthreshold. 1 nA is well below any intentional bias current in the
#: analog blocks this repo targets (the 5T OTA reference runs a 20 uA tail)
#: and well above the leakage a genuinely-off BSIM4 device reports.
_DEFAULT_OFF_CURRENT_A = 1e-9

#: Node names always treated as ground, independent of what the request
#: declares. ``0`` is SPICE's own global ground; the rest are the
#: conventional spellings sky130/gf180mcu netlists and this repo's own
#: extracted netlists use.
_DEFAULT_GROUND_NODES = frozenset(
    {"0", "gnd", "gnd!", "gnda", "gndd", "vss", "vssa", "vssd", "vgnd", "vsubs"}
)

#: Node names treated as the positive supply when the request declares no
#: ``corners.supply_v`` axis to derive it from (see :func:`_classify_rails`).
_DEFAULT_SUPPLY_NODES = frozenset(
    {"vdd", "vdda", "vddd", "vcc", "vcca", "vccd", "vpwr", "vpb"}
)

#: Case-insensitive substrings mapping a model/subcircuit name to a MOS
#: channel type. Complements ``sim.py``'s ``_SUBCKT_FAMILY_KEYWORDS`` (which
#: answers "is this a mosfet?" but not "n or p?"). First match wins, so the
#: ``nfet``/``pfet`` PDK spellings are checked before the generic
#: ``nmos``/``pmos`` ones.
_MOS_KIND_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("nfet", "nmos"),
    ("pfet", "pmos"),
    ("nmos", "nmos"),
    ("pmos", "pmos"),
    ("nch", "nmos"),
    ("pch", "pmos"),
)

_DEVICE_MARKER = "KLT_OP_DEVICE"
_NODE_MARKER = "KLT_OP_NODES"

_DEVICE_MARKER_RE = re.compile(rf"^{_DEVICE_MARKER}\s+(\d+)\s+(\S+)\s*$")
_OP_VALUE_RE = re.compile(r"^@(\S+?)\[(\w+)\]\s*=\s*([-+0-9.eEgGnNaAiIfF]+)\s*$")
_NODE_VALUE_RE = re.compile(
    r"^v\(([^)]+)\)\s*=\s*([-+0-9.eEgGnNaAiIfF]+)\s*$", re.IGNORECASE
)

#: A ``name = value`` parameter assignment anywhere in an element line --
#: stripped before positional (node/model) parsing, so ``W=1``/``L = 0.15``/
#: ``w={2*wu}`` never get mistaken for a node or a model name.
_ASSIGNMENT_RE = re.compile(r"[\w.]+\s*=\s*(?:\{[^}]*\}|'[^']*'|\"[^\"]*\"|\S+)")

#: ``.model <name> <type>`` -- the netlist-local way a plain ``M`` element's
#: channel type is declared (e.g. ``.model nfet nmos level=1``).
_MODEL_CARD_RE = re.compile(r"^\.model\s+(\S+)\s+(\S+)", re.IGNORECASE)

#: ``.param <name>=<value>`` assignments at the netlist's top level, used to
#: resolve a source value written as ``DC {vdd}``.
_PARAM_CARD_RE = re.compile(r"^\.param\b", re.IGNORECASE)

#: A ``v(<node>)`` / ``v(<node>,<node>)`` reference inside a ``.meas`` card.
_MEAS_NODE_RE = re.compile(r"\bv\s*\(\s*([^),\s]+)\s*(?:,\s*([^)\s]+)\s*)?\)", re.I)

#: Terminal counts for the element types whose node positions are fixed.
#: Used only for node-degree bookkeeping (the ``floating_node`` check) --
#: element types absent from this table contribute no terminals rather than
#: being guessed at.
_ELEMENT_TERMINALS = {
    "R": 2,
    "C": 2,
    "L": 2,
    "V": 2,
    "I": 2,
    "D": 2,
    "E": 4,
    "G": 4,
    "Q": 3,
    "J": 3,
    "M": 4,
}


class OpSanityError(Exception):
    """Raised when the lint cannot even be attempted: a missing/malformed
    request, an unreadable netlist, an unresolvable model library, or a
    ``--op-lint-corner`` naming a corner the request does not declare.

    Deliberately *not* raised for a run that found problems -- those are
    ``findings[]`` entries, the whole point of the verb -- nor for an
    operating point that failed to converge, which is reported as
    ``status: "error"`` with diagnostics (mirroring ``sim.py``'s
    ``SimError`` split).
    """


# --------------------------------------------------------------------------- #
# Netlist parsing (structural -- no simulator involved)
# --------------------------------------------------------------------------- #


class MosInstance:
    """One MOS instance found at the netlist body's top level."""

    __slots__ = ("name", "kind", "model", "nodes", "op_candidates", "values")

    def __init__(
        self,
        name: str,
        kind: str | None,
        model: str,
        nodes: dict[str, str],
        op_candidates: list[str],
    ) -> None:
        self.name = name
        self.kind = kind
        self.model = model
        self.nodes = nodes
        self.op_candidates = op_candidates
        self.values: dict[str, float | None] = dict.fromkeys(_OP_PARAM_KEYS.values())


class ParsedNetlist:
    """Everything the structural checks need out of one netlist body."""

    __slots__ = ("devices", "nodes", "node_degree", "sources", "params", "unresolved")

    def __init__(self) -> None:
        self.devices: list[MosInstance] = []
        #: every node name (lower-cased) referenced by a top-level element
        self.nodes: set[str] = set()
        #: node name -> number of element terminals attached to it
        self.node_degree: dict[str, int] = {}
        #: independent voltage sources: name (lower-cased, no `V` prefix) ->
        #: ``(plus_node, minus_node, value_or_None)``
        self.sources: dict[str, tuple[str, str, float | None]] = {}
        self.params: dict[str, float] = {}
        #: instance names that look like MOS devices but whose channel type
        #: could not be determined
        self.unresolved: list[str] = []


def _strip_comment(line: str) -> str:
    """Drop a trailing ``;``/``$`` SPICE inline comment."""
    for marker in (";", "$ "):
        index = line.find(marker)
        if index >= 0:
            line = line[:index]
    return line.rstrip()


def _logical_lines(text: str) -> list[str]:
    """Join ``+`` continuation lines onto their parent, dropping blank and
    ``*`` comment lines."""
    joined: list[str] = []
    for raw in text.splitlines():
        line = _strip_comment(raw.strip())
        if not line or line.startswith("*"):
            continue
        if line.startswith("+"):
            if joined:
                joined[-1] = f"{joined[-1]} {line[1:].strip()}"
            continue
        joined.append(line)
    return joined


def _positional_tokens(line: str) -> list[str]:
    """Split an element line into positional tokens, with every
    ``name=value`` parameter assignment removed first."""
    return _ASSIGNMENT_RE.sub(" ", line).split()


def _parse_number(raw: str, params: dict[str, float]) -> float | None:
    """Parse a SPICE scalar -- a plain number with an optional engineering
    suffix, or a ``{name}``/``name`` reference to a ``.param`` -- or
    ``None`` when the value is an expression this does not evaluate."""
    token = raw.strip().strip("{}'\"")
    if not token:
        return None
    lowered = token.lower()
    if lowered in params:
        return params[lowered]
    match = re.match(
        r"^([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)"
        r"(t|g|meg|k|mil|m|u|n|p|f)?",
        lowered,
    )
    if match is None:
        return None
    value = float(match.group(1))
    suffix = match.group(2)
    scale = {
        "t": 1e12,
        "g": 1e9,
        "meg": 1e6,
        "k": 1e3,
        "mil": 25.4e-6,
        "m": 1e-3,
        "u": 1e-6,
        "n": 1e-9,
        "p": 1e-12,
        "f": 1e-15,
    }
    return value * scale.get(suffix or "", 1.0)


def _mos_kind(name: str) -> str | None:
    """MOS channel type implied by a model/subcircuit name, or ``None``."""
    lowered = name.lower()
    for keyword, kind in _MOS_KIND_KEYWORDS:
        if keyword in lowered:
            return kind
    return None


def _op_candidates(instance: str, model: str, override: str | None) -> list[str]:
    """ngspice op-point instance paths to probe for one device, most likely
    first. See this module's docstring for why more than one is probed."""
    lowered = instance.lower()
    if lowered.startswith("x"):
        inner = (
            [override]
            if override
            else [f"m{model.lower()}", "m0", f"m{model.lower()}_0"]
        )
        return [f"m.{lowered}.{name}" for name in inner]
    # A plain `M` element's vectors live directly under its own name.
    return [override] if override else [lowered]


def parse_netlist(text: str, model_overrides: dict[str, Any]) -> ParsedNetlist:
    """Parse a ``klt sim``-convention netlist *body* into the structural
    facts the lint needs: top-level MOS instances with their four terminals,
    every node and its terminal count, and the independent voltage sources
    (which is how the supply rails are identified).

    Only the body's **top level** is walked: device lines inside a
    ``.subckt``/``.ends`` block define a subcircuit, they are not instances,
    and an ``X`` call to such a block is reported as one device the way any
    other PDK-style call is. ``.control``/``.end`` cards are not expected
    here at all (``klt sim`` generates those -- see ``sim.py``'s "Netlist
    convention").
    """
    parsed = ParsedNetlist()
    depth = 0
    model_kinds: dict[str, str] = {}

    lines = _logical_lines(text)

    # First pass: `.param` values and `.model` channel types, which later
    # lines may reference regardless of ordering.
    for line in lines:
        if _PARAM_CARD_RE.match(line):
            for match in _ASSIGNMENT_RE.finditer(line):
                name, _, value = match.group(0).partition("=")
                number = _parse_number(value, parsed.params)
                if number is not None:
                    parsed.params[name.strip().lower()] = number
            continue
        card = _MODEL_CARD_RE.match(line)
        if card is not None:
            kind = _mos_kind(card.group(2))
            if kind is not None:
                model_kinds[card.group(1).lower()] = kind

    for line in lines:
        lowered = line.lower()
        if lowered.startswith(".subckt"):
            depth += 1
            continue
        if lowered.startswith(".ends") or lowered.startswith(".eom"):
            depth = max(0, depth - 1)
            continue
        if line.startswith(".") or depth > 0:
            continue

        tokens = _positional_tokens(line)
        if len(tokens) < 3:
            continue
        name = tokens[0]
        element = name[0].upper()

        if element == "M" and len(tokens) >= 6:
            nodes = [tok.lower() for tok in tokens[1:5]]
            model = tokens[5]
            kind = model_kinds.get(model.lower()) or _mos_kind(model)
            _record_mos(parsed, name, kind, model, nodes, model_overrides)
        elif element == "X" and len(tokens) >= 6:
            nodes = [tok.lower() for tok in tokens[1:-1]]
            model = tokens[-1]
            # The `op_lint.models.<model>.kind` override is consulted BEFORE
            # the naming heuristics, mirroring `_record_mos`'s own precedence
            # on the unconditional `M` path: it exists precisely to resolve a
            # model name this tool cannot classify, so gating it behind those
            # heuristics would make it unreachable for the PDK subcircuit
            # calls (`X<name> d g s b <model>`) it is documented to fix -- and
            # a model name that also fails `_looks_like_mos` would drop out of
            # the report with no `device_kind_unknown` diagnostic at all.
            kind = (
                (model_overrides.get(model.lower()) or {}).get("kind")
                or model_kinds.get(model.lower())
                or _mos_kind(model)
            )
            if len(nodes) == 4 and kind is not None:
                _record_mos(parsed, name, kind, model, nodes, model_overrides)
            else:
                if len(nodes) == 4 and _looks_like_mos(model):
                    parsed.unresolved.append(name)
                _record_nodes(parsed, nodes)
        else:
            count = _ELEMENT_TERMINALS.get(element)
            if count is None:
                continue
            nodes = [tok.lower() for tok in tokens[1 : 1 + count]]
            if element == "V" and len(nodes) == 2:
                rest = " ".join(tokens[3:])
                parsed.sources[name[1:].lower()] = (
                    nodes[0],
                    nodes[1],
                    _source_value(rest, parsed.params),
                )
            _record_nodes(parsed, nodes)

    return parsed


def _looks_like_mos(model: str) -> bool:
    """Whether a 4-terminal subcircuit name smells like a MOS device even
    though :func:`_mos_kind` could not tell n from p -- used only to report
    it as unresolved rather than silently ignoring it."""
    lowered = model.lower()
    return "fet" in lowered or "mos" in lowered


def _record_nodes(parsed: ParsedNetlist, nodes: list[str]) -> None:
    for node in nodes:
        parsed.nodes.add(node)
        parsed.node_degree[node] = parsed.node_degree.get(node, 0) + 1


def _record_mos(
    parsed: ParsedNetlist,
    name: str,
    kind: str | None,
    model: str,
    nodes: list[str],
    model_overrides: dict[str, Any],
) -> None:
    override = (model_overrides.get(model.lower()) or {}).get("op_point_element")
    kind = (model_overrides.get(model.lower()) or {}).get("kind") or kind
    if kind is None:
        parsed.unresolved.append(name)
    parsed.devices.append(
        MosInstance(
            name=name,
            kind=kind,
            model=model,
            nodes={
                "drain": nodes[0],
                "gate": nodes[1],
                "source": nodes[2],
                "bulk": nodes[3],
            },
            op_candidates=_op_candidates(name, model, override),
        )
    )
    _record_nodes(parsed, nodes)


def _source_value(rest: str, params: dict[str, float]) -> float | None:
    """DC value of an independent source, from the text after its two nodes
    (``DC 1.8`` / ``DC {vdd}`` / a bare ``1.8``), or ``None`` when the value
    is an expression this does not evaluate."""
    tokens = rest.split()
    for index, token in enumerate(tokens):
        if token.upper() == "DC" and index + 1 < len(tokens):
            return _parse_number(tokens[index + 1], params)
    if tokens:
        return _parse_number(tokens[0], params)
    return None


# --------------------------------------------------------------------------- #
# Rail classification
# --------------------------------------------------------------------------- #


def _classify_rails(
    parsed: ParsedNetlist,
    supply_keys: list[str],
    overrides: dict[str, Any],
) -> tuple[set[str], set[str]]:
    """Ground and positive-supply node sets for this netlist.

    Three sources, in increasing precedence: the conventional node-name
    spellings (:data:`_DEFAULT_GROUND_NODES`/:data:`_DEFAULT_SUPPLY_NODES`),
    the request's own ``corners.supply_v`` keys -- which name **source
    elements** ``klt sim`` ``alter``s, so each one's positive terminal is by
    construction a declared supply rail -- and an explicit
    ``op_lint.rails`` override.

    Deliberately narrow: a node is *not* promoted to "supply" merely because
    some voltage source drives it. A testbench's own stimulus source (``Vin
    in 0 DC 0.9``) drives a node to a nonzero DC voltage without that node
    being a rail, and treating it as one would turn every input-driven
    drain into a false ``drain_tied_to_rail`` finding.
    """
    ground = {node for node in _DEFAULT_GROUND_NODES if node in parsed.nodes}
    ground.add("0")
    supply = {node for node in _DEFAULT_SUPPLY_NODES if node in parsed.nodes}

    for key in supply_keys:
        entry = parsed.sources.get(key.lower())
        if entry is None:
            continue
        plus, minus, value = entry
        if value is not None and value < 0:
            plus, minus = minus, plus
        if minus in ground or minus == "0":
            supply.add(plus)

    # A source tied across two nodes with a resolvable 0 V value is a
    # short -- the far node is the same rail as the near one (this is how
    # `Vvsubs vsubs 0 DC 0` style substrate ties read).
    for plus, minus, value in parsed.sources.values():
        if value == 0.0:
            if minus in ground:
                ground.add(plus)
            elif plus in ground:
                ground.add(minus)

    declared_ground = overrides.get("ground")
    if isinstance(declared_ground, list):
        ground |= {str(node).lower() for node in declared_ground}
    declared_supply = overrides.get("supply")
    if isinstance(declared_supply, list):
        supply |= {str(node).lower() for node in declared_supply}

    return ground, supply - ground


# --------------------------------------------------------------------------- #
# Deck generation / log parsing
# --------------------------------------------------------------------------- #


def _write_op_deck(
    *,
    deck_path: str,
    netlist_path: str,
    models_lib: str | None,
    point: CornerPoint,
    devices: list[MosInstance],
    probe_nodes: list[str],
) -> None:
    """Generate the ``.op`` deck: the corner's ``.lib``/``.include``/
    ``.temp`` cards (identical to ``sim.py``'s ``_write_corner_deck``, so a
    lint and a sweep of the same request see the same circuit), then a
    ``.control`` block that ``alter``s the supply sources, runs ``op``, and
    ``print``s every candidate op-point vector plus every probed node
    voltage, each device's block introduced by an ``echo`` marker."""
    lines = ["* klt sim --op-lint -- generated operating-point deck, do not edit"]
    if point.process_sections is not None:
        for section in point.process_sections:
            lines.append(f".lib {models_lib} {section}")
    elif point.process is not None:
        lines.append(f".lib {models_lib} {point.process}")
    lines.append(f".include {netlist_path}")
    lines.append(f".temp {point.temperature_c}")

    lines.append(".control")
    for key, value in sorted(point.supply_v.items()):
        lines.append(f"alter {key}={value}")
    lines.append("op")
    for index, device in enumerate(devices):
        lines.append(f"echo {_DEVICE_MARKER} {index} {device.name}")
        for candidate in device.op_candidates:
            for param in _OP_PARAMS:
                lines.append(f"print @{candidate}[{param}]")
    lines.append(f"echo {_NODE_MARKER}")
    for node in probe_nodes:
        lines.append(f"print v({node})")
    lines.append("quit")
    lines.append(".endc")
    lines.append(".end")

    with open(deck_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _parse_op_log(
    log_text: str, devices: list[MosInstance]
) -> tuple[dict[int, str], dict[str, float]]:
    """Attribute every ``@path[param] = value`` line in the log to the device
    whose marker most recently preceded it, and collect ``v(node)`` values.

    Returns ``(resolved_element_by_device_index, node_voltages)`` and fills
    each device's ``values`` in place. When several candidate paths answered
    for one device (never observed in practice -- the candidates are
    mutually exclusive PDK conventions), the one that answered for the most
    parameters wins, ties broken by candidate order.
    """
    per_device: dict[int, dict[str, dict[str, float]]] = {}
    node_voltages: dict[str, float] = {}
    current: int | None = None

    for raw_line in log_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        marker = _DEVICE_MARKER_RE.match(line)
        if marker is not None:
            current = int(marker.group(1))
            continue
        if line == _NODE_MARKER:
            current = None
            continue
        value_match = _OP_VALUE_RE.match(line)
        if value_match is not None and current is not None:
            path, param, raw_value = value_match.groups()
            try:
                number = float(raw_value)
            except ValueError:
                continue
            per_device.setdefault(current, {}).setdefault(path.lower(), {})[param] = (
                number
            )
            continue
        node_match = _NODE_VALUE_RE.match(line)
        if node_match is not None:
            try:
                node_voltages[node_match.group(1).strip().lower()] = float(
                    node_match.group(2)
                )
            except ValueError:
                continue

    resolved: dict[int, str] = {}
    for index, device in enumerate(devices):
        answered = per_device.get(index) or {}
        best: str | None = None
        for candidate in device.op_candidates:
            found = answered.get(candidate.lower())
            if found and (best is None or len(found) > len(answered[best])):
                best = candidate.lower()
        if best is None:
            continue
        resolved[index] = best
        for param, value in answered[best].items():
            key = _OP_PARAM_KEYS.get(param)
            if key is not None:
                device.values[key] = value
    return resolved, node_voltages


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


def _triode_margin_v(temperature_c: float, thresholds: dict[str, Any]) -> float:
    """Corner-aware ``Vdsat`` margin, in volts.

    ``op_lint.thresholds.triode_margin_v`` pins it absolutely; otherwise it
    is ``triode_margin_kt`` (default :data:`_DEFAULT_TRIODE_MARGIN_KT`)
    thermal voltages at *this corner's* temperature -- see that constant's
    comment for why the thermal voltage is the right scale.
    """
    explicit = thresholds.get("triode_margin_v")
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool):
        return float(explicit)
    multiple = thresholds.get("triode_margin_kt", _DEFAULT_TRIODE_MARGIN_KT)
    if not isinstance(multiple, (int, float)) or isinstance(multiple, bool):
        multiple = _DEFAULT_TRIODE_MARGIN_KT
    kelvin = float(temperature_c) + 273.15
    return float(multiple) * _KT_OVER_Q_V_PER_K * kelvin


def _finding(
    check: str,
    severity: str,
    message: str,
    suggestion: str,
    *,
    device: MosInstance | None = None,
    node: str | None = None,
    values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One ``findings[]`` entry. Every key is always present (``null`` when
    it does not apply) so a consumer never has to branch on key existence --
    the same "flat, fully-populated payload" convention the rest of the JSON
    contract follows."""
    return {
        "check": check,
        "severity": severity,
        "device": device.name if device is not None else None,
        "kind": device.kind if device is not None else None,
        "node": node,
        "nodes": dict(device.nodes) if device is not None else None,
        "values": values,
        "message": message,
        "suggestion": suggestion,
    }


def _format_v(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.4g} V"


def _classify_region(device: MosInstance, off_current_a: float, margin_v: float) -> str:
    """Operating region for one device from its reported values."""
    vgs = device.values["vgs_v"]
    vth = device.values["vth_v"]
    vds = device.values["vds_v"]
    vdsat = device.values["vdsat_v"]
    current = device.values["id_a"]
    if vgs is None or vth is None:
        return "unknown"
    if abs(vgs) < abs(vth):
        if current is not None and abs(current) <= off_current_a:
            return "off"
        return "subthreshold"
    if vds is None or vdsat is None:
        return "unknown"
    if abs(vds) < abs(vdsat) + margin_v:
        return "triode"
    return "saturation"


def _operating_point_findings(
    devices: list[MosInstance],
    regions: dict[str, str],
    off_current_a: float,
    margin_v: float,
) -> list[dict[str, Any]]:
    """``off``/``triode`` findings, one device at a time, in netlist order."""
    findings: list[dict[str, Any]] = []
    for device in devices:
        region = regions[device.name]
        values = dict(device.values)
        if region == "off":
            findings.append(
                _finding(
                    "off",
                    "error",
                    f"{device.name} ({device.kind}): |Vgs| "
                    f"{_format_v(device.values['vgs_v'])} is below |Vth| "
                    f"{_format_v(device.values['vth_v'])} and Id is "
                    f"{device.values['id_a']:.3g} A -- the device is off and "
                    "contributes no transconductance",
                    f"raise the gate bias on node `{device.nodes['gate']}` "
                    f"above Vth (or check that node has a DC path at all); "
                    "an off device makes every small-signal measurement "
                    "downstream of it meaningless",
                    device=device,
                    values=values,
                )
            )
        elif region == "triode":
            findings.append(
                _finding(
                    "triode",
                    "warning",
                    f"{device.name} ({device.kind}): |Vds| "
                    f"{_format_v(device.values['vds_v'])} is below |Vdsat| "
                    f"{_format_v(device.values['vdsat_v'])} + "
                    f"{margin_v * 1e3:.0f} mV margin -- the device is in "
                    "triode, not saturation",
                    f"give node `{device.nodes['drain']}` more headroom "
                    f"above `{device.nodes['source']}` (or reduce the "
                    "overdrive) if this device is meant to act as a "
                    "transconductor rather than a switch/resistor",
                    device=device,
                    values=values,
                )
            )
    return findings


def _wiring_findings(
    devices: list[MosInstance], ground: set[str], supply: set[str]
) -> list[dict[str, Any]]:
    """Structural "this can never work" wiring smells, computed from the
    netlist alone (no simulator needed -- see this module's docstring)."""
    findings: list[dict[str, Any]] = []
    for device in devices:
        nodes = device.nodes
        own_rail = ground if device.kind == "nmos" else supply
        other_rail = supply if device.kind == "nmos" else ground

        if device.kind in ("nmos", "pmos") and nodes["drain"] in own_rail:
            rail_name = "ground" if device.kind == "nmos" else "the positive supply"
            findings.append(
                _finding(
                    "drain_tied_to_rail",
                    "error",
                    f"{device.name} ({device.kind}): drain node "
                    f"`{nodes['drain']}` is {rail_name}, the same rail this "
                    "device's source normally returns to -- its drain "
                    "voltage can never move, so the device drives nothing",
                    "re-check the terminal order on this instance "
                    "(`<drain> <gate> <source> <bulk>`); a drain shorted to "
                    "its own rail is almost always a swapped drain/source "
                    "or a mistyped node name",
                    device=device,
                )
            )

        if nodes["gate"] == nodes["source"]:
            findings.append(
                _finding(
                    "gate_shorted_to_source",
                    "error",
                    f"{device.name} ({device.kind}): gate and source are the "
                    f"same node `{nodes['gate']}` -- Vgs is 0 by "
                    "construction, so the device is permanently off",
                    "drive the gate from the intended bias/signal node; if "
                    "a diode connection was intended, tie the gate to the "
                    "*drain*, not the source",
                    device=device,
                )
            )

        if device.kind in ("nmos", "pmos") and nodes["bulk"] not in own_rail:
            if nodes["bulk"] != nodes["source"]:
                detail = "the positive supply" if device.kind == "pmos" else "ground"
                extra = " and is the other rail" if nodes["bulk"] in other_rail else ""
                findings.append(
                    _finding(
                        "bulk_not_tied",
                        "warning",
                        f"{device.name} ({device.kind}): bulk node "
                        f"`{nodes['bulk']}` is tied to neither its own "
                        f"source `{nodes['source']}` nor {detail}{extra} -- "
                        "the body effect (and any forward-biased junction) "
                        "is not what the sizing assumed",
                        f"tie the bulk to {detail}, or to the device's own "
                        "source if this is a deep-nwell/isolated device "
                        "that genuinely needs it",
                        device=device,
                    )
                )
    return findings


def _hygiene_findings(
    parsed: ParsedNetlist,
    declared_nodes: list[tuple[str, str]],
    ground: set[str],
    supply: set[str],
) -> list[dict[str, Any]]:
    """Netlist-hygiene findings: a declared I/O node that is not in the
    netlist at all, and a node with exactly one terminal on it."""
    findings: list[dict[str, Any]] = []
    for node, origin in declared_nodes:
        if node in parsed.nodes:
            continue
        findings.append(
            _finding(
                "missing_node",
                "error",
                f"declared node `{node}` ({origin}) does not appear "
                "anywhere in the netlist",
                "fix the spelling, or add the missing connection -- a "
                "measurement referencing a node the netlist never creates "
                "can only ever report 0 V or fail outright",
                node=node,
            )
        )

    for node in sorted(parsed.node_degree):
        if node in ground or node in supply:
            continue
        if parsed.node_degree[node] != 1:
            continue
        findings.append(
            _finding(
                "floating_node",
                "warning",
                f"node `{node}` has exactly one element terminal on it -- "
                "it is floating, with no DC path to anywhere",
                "connect it, or give it a DC reference (`.options rshunt`); "
                "a floating node is the usual cause of the singular-matrix / "
                "gmin-stepping narration `klt sim` classifies separately",
                node=node,
            )
        )
    return findings


def _declared_nodes(
    request: dict[str, Any], op_lint: dict[str, Any]
) -> list[tuple[str, str]]:
    """Node names the request claims exist, each paired with where the claim
    came from (for the finding's message).

    Two sources: an explicit ``op_lint.nodes`` declaration (a flat list, or
    an ``{"inputs": [...], "outputs": [...]}`` object), and every
    ``v(<node>)`` reference inside the request's own ``.meas`` cards -- the
    latter is what makes the common Loop A failure ("my measurement reads a
    node that does not exist") a named finding instead of a silent 0 V.
    """
    collected: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(node: str, origin: str) -> None:
        lowered = node.strip().lower()
        if not lowered or lowered in seen:
            return
        seen.add(lowered)
        collected.append((lowered, origin))

    declared = op_lint.get("nodes")
    if isinstance(declared, list):
        for node in declared:
            _add(str(node), "op_lint.nodes")
    elif isinstance(declared, dict):
        for role in ("inputs", "outputs"):
            for node in declared.get(role) or []:
                _add(str(node), f"op_lint.nodes.{role}")

    for spec in request.get("measurements") or []:
        spice = spec.get("spice")
        if not isinstance(spice, str):
            continue
        for match in _MEAS_NODE_RE.finditer(spice):
            for group in match.groups():
                if group:
                    _add(group, f"measurement {spec.get('name', '?')!r}")
    return collected


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def load_request(request_path: str) -> dict[str, Any]:
    """Read and minimally validate an op-sanity request JSON file.

    The same document ``klt sim`` takes (see ``sim.load_request``), but only
    ``netlist`` is required: this verb always runs a plain ``op``, so a
    request's ``analysis``/``measurements`` are not needed for it to work.
    """
    request = _load_request_json(request_path, OpSanityError)
    return validate_request_shape(
        request,
        "request file",
        error_cls=OpSanityError,
        required_fields=("netlist",),
    )


def run_op_sanity(
    request_path: str,
    *,
    corner: str | None = None,
) -> dict[str, Any]:
    """Lint the operating point of the netlist declared by the request at
    ``request_path``.

    ``corner`` selects which expanded corner point of the request's own
    ``corners`` matrix to bias at, by ``corner_id``; the first expanded
    point is used when omitted. Exactly one corner is run -- this is a
    diagnostic meant to be cheap enough to run on every Loop A miss, not a
    second corner sweep.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/sim.md``'s "Operating-point lint"). Raises
    :class:`OpSanityError` for anything that prevents the lint from being
    attempted at all; an operating point that ran but did not converge is
    reported as ``status: "error"`` with diagnostics, never raised.
    """
    request = load_request(request_path)
    request_dir = os.path.dirname(os.path.abspath(request_path))
    repo_root = env_provenance.find_repo_root(request_dir)

    netlist_path = _resolve_relative(request["netlist"], request_dir)
    if not os.path.isfile(netlist_path):
        raise OpSanityError(f"netlist not found: {netlist_path}")
    try:
        with open(netlist_path, encoding="utf-8", errors="replace") as handle:
            netlist_text = handle.read()
    except OSError as exc:  # pragma: no cover - unreadable-after-stat race
        raise OpSanityError(f"could not read netlist: {exc}") from exc

    op_lint = request.get("op_lint") or {}
    if not isinstance(op_lint, dict):
        raise OpSanityError("request.op_lint must be an object")
    thresholds = op_lint.get("thresholds") or {}
    if not isinstance(thresholds, dict):
        raise OpSanityError("request.op_lint.thresholds must be an object")
    model_overrides = {
        str(key).lower(): value for key, value in (op_lint.get("models") or {}).items()
    }

    parsed = parse_netlist(netlist_text, model_overrides)

    corners_spec = request.get("corners") or {}
    try:
        points = _expand_corners(corners_spec, request.get("exclude") or [])
    except SimError as exc:
        raise OpSanityError(str(exc)) from exc
    if not points:
        raise OpSanityError(
            "this request's `exclude` rules leave no corner to bias the "
            "operating point at"
        )
    if corner is not None:
        selected = [point for point in points if point.corner_id == corner]
        if not selected:
            available = ", ".join(point.corner_id for point in points)
            raise OpSanityError(
                f"corner '{corner}' is not in this request's matrix "
                f"(available: {available})"
            )
        point = selected[0]
    else:
        point = points[0]

    models = request.get("models") or {}
    models_lib: str | None = None
    if point.process is not None:
        try:
            models_lib = _resolve_models_lib(models, request_dir)
        except SimError as exc:
            raise OpSanityError(str(exc)) from exc

    provenance_pdk: dict[str, Any] | None = None
    if models.get("pdk") or models.get("pdk_root"):
        try:
            provenance_pdk = find_pdk(
                variant=models.get("pdk"), root=models.get("pdk_root")
            )
        except PdkNotFoundError:
            provenance_pdk = None

    options = request.get("options") or {}
    timeout_s = op_lint.get("timeout_s", options.get("timeout_s", DEFAULT_TIMEOUT_S))

    ground, supply = _classify_rails(
        parsed, sorted(point.supply_v), op_lint.get("rails") or {}
    )

    probe_nodes = sorted(node for node in parsed.nodes if node != "0")
    log_text, engine_version, runtime_s, launch_diagnostics = _run_op(
        netlist_path=netlist_path,
        models_lib=models_lib,
        point=point,
        devices=parsed.devices,
        probe_nodes=probe_nodes,
        timeout_s=timeout_s,
    )

    resolved, node_voltages = _parse_op_log(log_text, parsed.devices)

    diagnostics = list(launch_diagnostics)
    diagnostics.extend(_classify_diagnostics(log_text, netlist_path))

    # Issue #205, applied to an `op` run: ngspice narrates its own
    # gmin/source-stepping recovery attempts with `singular matrix` /
    # nonconvergence text even on a run that ultimately converges. The
    # independent evidence that it did converge here is that the operating
    # point actually produced values -- `sim.py` uses "every requested
    # measurement came back" for the same judgement, and the same
    # `simulation(s) aborted` trailer still vetoes recovery.
    produced_values = bool(node_voltages) or bool(resolved)
    if produced_values and not _SIMULATION_ABORTED_RE.search(log_text):
        for diagnostic in diagnostics:
            if diagnostic["code"] in ("singular_matrix", "nonconvergence"):
                diagnostic["severity"] = "warning"

    for index, device in enumerate(parsed.devices):
        if index in resolved:
            continue
        diagnostics.append(
            {
                "severity": "warning",
                "code": "op_point_unavailable",
                "message": (
                    f"no operating-point vector answered for {device.name} "
                    f"(model {device.model}); tried "
                    f"{', '.join('@' + path for path in device.op_candidates)}. "
                    "Set request.op_lint.models."
                    f"{device.model}.op_point_element to this PDK's inner "
                    "element name to enable the off/triode checks for it."
                ),
            }
        )
    for name in parsed.unresolved:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "device_kind_unknown",
                "message": (
                    f"could not tell whether {name} is an NMOS or a PMOS "
                    "from its model name; its polarity-dependent checks "
                    "(drain-tied-to-rail, bulk tie) were skipped. Set "
                    "request.op_lint.models.<model>.kind to resolve it."
                ),
            }
        )

    margin_v = _triode_margin_v(point.temperature_c, thresholds)
    off_current_a = thresholds.get("off_current_a", _DEFAULT_OFF_CURRENT_A)
    if not isinstance(off_current_a, (int, float)) or isinstance(off_current_a, bool):
        off_current_a = _DEFAULT_OFF_CURRENT_A
    off_current_a = float(off_current_a)

    regions = {
        device.name: _classify_region(device, off_current_a, margin_v)
        for device in parsed.devices
    }

    findings = _operating_point_findings(
        parsed.devices, regions, off_current_a, margin_v
    )
    findings.extend(_wiring_findings(parsed.devices, ground, supply))
    findings.extend(
        _hygiene_findings(parsed, _declared_nodes(request, op_lint), ground, supply)
    )

    error_count = sum(1 for entry in findings if entry["severity"] == "error")
    warning_count = len(findings) - error_count

    analysis_failed = any(
        diagnostic["severity"] == "error" for diagnostic in diagnostics
    ) or (parsed.devices and not produced_values)
    if analysis_failed:
        status = "error"
    elif findings:
        status = "findings"
    else:
        status = "clean"

    devices_payload = [
        {
            "name": device.name,
            "kind": device.kind,
            "model": device.model,
            "op_point_element": resolved.get(index),
            "nodes": dict(device.nodes),
            "values": dict(device.values),
            "region": regions[device.name],
        }
        for index, device in enumerate(parsed.devices)
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "netlist": env_provenance.repo_relative_path(netlist_path, repo_root=repo_root),
        "corner": {
            "corner_id": point.corner_id,
            "process": point.process,
            "supply_v": dict(point.supply_v),
            "temperature_c": point.temperature_c,
        },
        "device_count": len(parsed.devices),
        "finding_count": len(findings),
        "error_count": error_count,
        "warning_count": warning_count,
        "thresholds": {
            "triode_margin_v": round(margin_v, 6),
            "off_current_a": off_current_a,
        },
        "rails": {"ground": sorted(ground), "supply": sorted(supply)},
        "devices": devices_payload,
        "findings": findings,
        "node_voltages": dict(sorted(node_voltages.items())),
        "diagnostics": diagnostics,
        "environment": {
            "engine": "ngspice",
            "engine_version": engine_version,
            "models_lib": env_provenance.repo_relative_path(
                models_lib, repo_root=repo_root
            ),
            "models_lib_sha256": sha256_file(models_lib) if models_lib else None,
            "netlist_sha256": sha256_file(netlist_path),
            "runtime_s": runtime_s,
        },
        "provenance": build_provenance(
            deck_name=(os.path.basename(models_lib) if models_lib else None),
            deck_path=models_lib,
            pdk=provenance_pdk,
        ),
    }


def _run_op(
    *,
    netlist_path: str,
    models_lib: str | None,
    point: CornerPoint,
    devices: list[MosInstance],
    probe_nodes: list[str],
    timeout_s: float,
) -> tuple[str, str | None, float, list[dict[str, str]]]:
    """Run the generated ``.op`` deck through ``ngspice -b``.

    Never raises: a launch failure or a timeout comes back as a
    ``severity: "error"`` diagnostic and an empty log, so the structural
    checks still get reported (this module's docstring, "Structural checks
    never need the simulator"). Mirrors ``sim.py``'s ``_run_corner``
    invocation pattern -- one short-lived subprocess, killable, engine
    version scraped from stdout.
    """
    import shutil
    import tempfile

    work_dir = tempfile.mkdtemp(prefix="klt-op-sanity-")
    deck_path = os.path.join(work_dir, "op.cir")
    log_path = os.path.join(work_dir, "ngspice.log")
    diagnostics: list[dict[str, str]] = []
    engine_version: str | None = None
    log_text = ""

    try:
        _write_op_deck(
            deck_path=deck_path,
            netlist_path=netlist_path,
            models_lib=models_lib,
            point=point,
            devices=devices,
            probe_nodes=probe_nodes,
        )
        started = time.monotonic()
        try:
            completed = subprocess.run(
                ["ngspice", "-b", deck_path, "-o", log_path],
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            engine_version = _extract_engine_version(completed.stdout)
        except subprocess.TimeoutExpired:
            diagnostics.append(
                {
                    "severity": "error",
                    "code": "timeout",
                    "message": (
                        f"ngspice did not complete the operating point within "
                        f"{timeout_s}s, killed"
                    ),
                }
            )
        except FileNotFoundError as exc:
            diagnostics.append(
                {
                    "severity": "error",
                    "code": "unknown",
                    "message": f"could not launch ngspice: {exc}",
                }
            )
        runtime_s = round(time.monotonic() - started, 3)

        if os.path.isfile(log_path):
            try:
                with open(log_path, encoding="utf-8", errors="replace") as handle:
                    log_text = handle.read()
            except OSError:
                log_text = ""
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    return log_text, engine_version, runtime_s, diagnostics
