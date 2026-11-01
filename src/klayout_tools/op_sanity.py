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

import copy
import os
import re
import subprocess
import time
from collections.abc import Callable
from typing import Any

from . import _spice_number as spice_number
from . import env_provenance
from ._jsonnum import is_number
from ._paths import (
    _load_request_json,
    _resolve_relative,
    fold_spice_continuations,
    validate_request_shape,
)
from ._provenance import build_provenance, sha256_file
from .pdk import PdkNotFoundError, find_pdk
from .sim import (
    _SIMULATION_ABORTED_RE,
    DEFAULT_TIMEOUT_S,
    CornerPoint,
    SimError,
    _classify_diagnostics,
    _corner_lib_lines,
    _expand_corners,
    _extract_engine_version,
    _needs_models_lib,
    _resolve_corner_section_libs,
    _resolve_models_lib,
    _resolve_netlist_include,
    _resolve_osdi_preload,
    _resolve_param_file,
    _scale_supply_waveforms,
    _supply_alter_lines,
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
    "B": 2,
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

    __slots__ = (
        "devices",
        "nodes",
        "node_degree",
        "sources",
        "params",
        "unresolved",
        "unexpanded",
        "expanded_instances",
        "expansion_truncated",
    )

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
        #: ``X`` instances that are neither a recognised MOS model nor a
        #: resolvable user subcircuit -- their contents are not linted
        self.unexpanded: list[str] = []
        #: user-subcircuit instances expanded so far (bounded by
        #: ``_MAX_EXPANDED_INSTANCES``)
        self.expanded_instances = 0
        #: set once the expansion cap was hit; later subckt calls are skipped
        self.expansion_truncated = False


#: ``--op-lint-corner`` sentinel: bias every expanded corner (issue #4650).
ALL_CORNERS = "all"


def _strip_comment(line: str) -> str:
    """Drop a trailing ``;``/``$`` SPICE inline comment."""
    for marker in (";", "$ "):
        index = line.find(marker)
        if index >= 0:
            line = line[:index]
    return line.rstrip()


def _logical_lines(text: str) -> list[str]:
    """Join ``+`` continuation lines onto their parent, dropping blank and
    ``*`` comment lines.

    A leading ``+`` line with nothing preceding it to fold onto (e.g. the
    very first non-blank/non-comment line of ``text`` starts with ``+``) is
    dropped, matching this function's pre-#1741 behaviour -- unlike
    ``pex.py``/``netlist_normalize.py``, which keep such an orphan
    continuation as a literal line. :func:`fold_spice_continuations` keeps
    an orphan verbatim (``+`` prefix intact) rather than dropping it, so the
    post-pass below filters any still-``+``-prefixed entry it returns.
    """
    lines: list[str] = []
    for raw in text.splitlines():
        line = _strip_comment(raw.strip())
        if not line or line.startswith("*"):
            continue
        lines.append(line)
    return [
        line for line in fold_spice_continuations(lines) if not line.startswith("+")
    ]


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
    return spice_number.parse(token)


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
    # A device inside a user subcircuit carries its instance path
    # (`xdut.m1`); ngspice names its vectors `m.<path>` and, for a PDK cell,
    # `m.<path>.<inner element>`.
    leaf = lowered.rsplit(".", 1)[-1]
    if leaf.startswith("x"):
        inner = (
            [override]
            if override
            else [f"m{model.lower()}", "m0", f"m{model.lower()}_0"]
        )
        return [f"m.{lowered}.{name}" for name in inner]
    # A plain `M` element's vectors live directly under its own name.
    if override:
        return [override]
    return [f"m.{lowered}" if "." in lowered else lowered]


#: Hard caps on the hierarchy walk: nesting depth of user subcircuit calls,
#: the number of files one netlist may pull in through ``.include``, and the
#: total number of subcircuit instances expanded (a deep fan-out hierarchy
#: grows exponentially even within the depth cap).
_MAX_SUBCKT_DEPTH = 16
_MAX_INCLUDE_FILES = 64
_MAX_EXPANDED_INSTANCES = 20000

_SUBCKT_CARD_RE = re.compile(r"^\.subckt\b", re.IGNORECASE)
_ENDS_CARD_RE = re.compile(r"^\.(ends|eom)\b", re.IGNORECASE)
INCLUDE_CARD_RE = re.compile(r"^\.(include|inc)\s+(.+)$", re.IGNORECASE)


class _SubcktDef:
    """A user ``.subckt`` definition collected from the netlist or an include."""

    __slots__ = ("name", "ports", "lines", "from_include")

    def __init__(
        self, name: str, ports: list[str], lines: list[str], from_include: bool
    ) -> None:
        self.name = name
        self.ports = ports
        self.lines = lines
        self.from_include = from_include


class IncludeState:
    """Bookkeeping for one parse's include expansion."""

    __slots__ = ("seen", "base_dir")

    def __init__(self, base_dir: str | None) -> None:
        self.seen: set[str] = set()
        self.base_dir = base_dir


def expand_includes(
    lines: list[str], base_dir: str | None, state: IncludeState
) -> list[str]:
    """Replace each resolvable ``.include``/``.inc`` card with the logical
    lines of the file it names (recursively, relative to that file's own
    directory). A reference that is unresolved, uses a ``$VAR``, or would
    exceed the file cap is dropped rather than failing -- the structural lint
    never assumes more than it can read."""
    out: list[str] = []
    for line in lines:
        card = INCLUDE_CARD_RE.match(line)
        if card is None:
            out.append(line)
            continue
        if base_dir is None:
            continue
        reference = card.group(2).strip().strip("\"'")
        if "$" in reference:
            continue
        path = _resolve_netlist_include(reference, base_dir)
        if path is None:
            continue
        real = os.path.realpath(path)
        if real in state.seen or len(state.seen) >= _MAX_INCLUDE_FILES:
            continue
        state.seen.add(real)
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        out.extend(expand_includes(_logical_lines(text), os.path.dirname(path), state))
    return out


def _split_subckts(
    lines: list[str], defs: dict[str, _SubcktDef], from_include: bool
) -> list[str]:
    """Register every ``.subckt`` block in ``lines`` into ``defs`` (nested
    definitions are registered globally) and return the top-level lines."""
    top: list[str] = []
    stack: list[_SubcktDef] = []
    for line in lines:
        if _SUBCKT_CARD_RE.match(line):
            tokens = _positional_tokens(line)
            ports = [tok.lower() for tok in tokens[2:]]
            if "params:" in ports:
                ports = ports[: ports.index("params:")]
            name = tokens[1].lower() if len(tokens) > 1 else ""
            definition = _SubcktDef(name, ports, [], from_include)
            if name:
                defs.setdefault(name, definition)
            stack.append(definition)
            continue
        if _ENDS_CARD_RE.match(line):
            if stack:
                stack.pop()
            continue
        if stack:
            stack[-1].lines.append(line)
        else:
            top.append(line)
    return top


def parse_netlist(
    text: str, model_overrides: dict[str, Any], base_dir: str | None = None
) -> ParsedNetlist:
    """Parse a ``klt sim``-convention netlist *body* into the structural
    facts the lint needs: MOS instances with their four terminals, every
    node and its terminal count, and the independent voltage sources (which
    is how the supply rails are identified).

    User subcircuits -- ``.subckt`` blocks in the body or in files it
    ``.include``s (resolved relative to ``base_dir``, the netlist's own
    directory) -- are expanded where an ``X`` line instantiates them: their
    MOS devices are reported with hierarchical names (``xdut.m1``) and their
    nodes mapped through the port list. A definition nothing instantiates
    contributes nothing. An ``X`` line whose subcircuit cannot be resolved
    (and is not a recognised PDK MOS model) is listed in
    :attr:`ParsedNetlist.unexpanded`. ``.control``/``.end`` cards are not
    expected here at all (``klt sim`` generates those -- see ``sim.py``'s
    "Netlist convention").
    """
    parsed = ParsedNetlist()
    model_kinds: dict[str, str] = {}
    state = IncludeState(base_dir)

    body_lines = _logical_lines(text)
    defs: dict[str, _SubcktDef] = {}
    # Definitions in the body win over same-named ones from an include.
    top_lines = _split_subckts(body_lines, defs, from_include=False)
    top_lines = expand_includes(top_lines, base_dir, state)
    # Includes can also sit inside a subckt body; expand those before the
    # definitions are used so the flat DUT file is visible to the walk.
    for definition in list(defs.values()):
        definition.lines = expand_includes(definition.lines, base_dir, state)
    top_lines = _split_subckts(top_lines, defs, from_include=True)
    for definition in list(defs.values()):
        if any(_SUBCKT_CARD_RE.match(entry) for entry in definition.lines):
            definition.lines = _split_subckts(definition.lines, defs, True)

    # First pass: `.param` values and `.model` channel types, which later
    # lines may reference regardless of ordering (or hierarchy level).
    scan = list(top_lines)
    for definition in defs.values():
        scan.extend(definition.lines)
    for line in scan:
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

    _walk_lines(
        parsed,
        top_lines,
        defs,
        model_kinds,
        model_overrides,
        prefix="",
        node_map={},
        stack=(),
    )
    return parsed


def _map_node(token: str, prefix: str, node_map: dict[str, str]) -> str:
    node = token.lower()
    if not prefix or node == "0":
        return node
    return node_map.get(node, f"{prefix.lower()}{node}")


def _walk_lines(
    parsed: ParsedNetlist,
    lines: list[str],
    defs: dict[str, _SubcktDef],
    model_kinds: dict[str, str],
    model_overrides: dict[str, Any],
    *,
    prefix: str,
    node_map: dict[str, str],
    stack: tuple[str, ...],
) -> None:
    for line in lines:
        if line.startswith("."):
            continue

        tokens = _positional_tokens(line)
        if len(tokens) < 3:
            continue
        leaf = tokens[0]
        name = f"{prefix}{leaf}"
        element = leaf[0].upper()

        def mapped(items: list[str]) -> list[str]:
            return [_map_node(tok, prefix, node_map) for tok in items]

        if element == "M" and len(tokens) >= 6:
            nodes = mapped(tokens[1:5])
            model = tokens[5]
            kind = model_kinds.get(model.lower()) or _mos_kind(model)
            _record_mos(parsed, name, kind, model, nodes, model_overrides)
        elif element == "X" and len(tokens) >= 3:
            _walk_instance(
                parsed,
                tokens,
                name,
                mapped(tokens[1:-1]),
                defs,
                model_kinds,
                model_overrides,
                stack,
            )
        else:
            _walk_passive(parsed, line, tokens, mapped, prefix)


def _walk_instance(
    parsed: ParsedNetlist,
    tokens: list[str],
    name: str,
    nodes: list[str],
    defs: dict[str, _SubcktDef],
    model_kinds: dict[str, str],
    model_overrides: dict[str, Any],
    stack: tuple[str, ...],
) -> None:
    """Handle one ``X`` card: expand a user subckt, report a PDK device cell
    as one MOS, or record it as unresolved/unexpanded."""
    model = tokens[-1]
    definition = defs.get(model.lower())
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
    pdk_style = len(nodes) == 4 and kind is not None
    # A subckt that came from an `.include` and reads as a 4-terminal
    # MOS model is a PDK device cell, reported as one device (the
    # behaviour before includes were followed); a body-defined one is
    # the user's own hierarchy and is expanded.
    if (
        definition is not None
        and not (pdk_style and definition.from_include)
        and len(nodes) == len(definition.ports)
        and model.lower() not in stack
        and len(stack) < _MAX_SUBCKT_DEPTH
    ):
        if parsed.expanded_instances >= _MAX_EXPANDED_INSTANCES:
            parsed.expansion_truncated = True
            _record_nodes(parsed, nodes)
            return
        parsed.expanded_instances += 1
        _walk_lines(
            parsed,
            definition.lines,
            defs,
            model_kinds,
            model_overrides,
            prefix=f"{name}.",
            node_map=dict(zip(definition.ports, nodes, strict=True)),
            stack=(*stack, model.lower()),
        )
    elif pdk_style:
        _record_mos(parsed, name, kind, model, nodes, model_overrides)
    else:
        if len(tokens) >= 6 and len(nodes) == 4 and _looks_like_mos(model):
            parsed.unresolved.append(name)
        else:
            parsed.unexpanded.append(name)
        _record_nodes(parsed, nodes)


def _walk_passive(
    parsed: ParsedNetlist,
    line: str,
    tokens: list[str],
    mapped: Callable[[list[str]], list[str]],
    prefix: str,
) -> None:
    """Handle every non-``M``/``X`` card: record its terminals, top-level
    ``V`` source values, and ``B``-source node references."""
    leaf = tokens[0]
    element = leaf[0].upper()
    count = _ELEMENT_TERMINALS.get(element)
    if count is None:
        return
    nodes = mapped(tokens[1 : 1 + count])
    if element == "V" and len(nodes) == 2 and not prefix:
        rest = " ".join(tokens[3:])
        parsed.sources[leaf[1:].lower()] = (
            nodes[0],
            nodes[1],
            _source_value(rest, parsed.params),
        )
    _record_nodes(parsed, nodes)
    if element == "B":
        # A behavioural source's expression reads other nodes: each
        # `v(<node>)` reference is a terminal on that node.
        for match in _MEAS_NODE_RE.finditer(line):
            refs = [grp for grp in match.groups() if grp]
            _record_nodes(parsed, mapped(refs))


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
    osdi_preload: tuple[str, ...] = (),
) -> None:
    """Generate the ``.op`` deck: the corner's ``.lib``/``.include``/
    ``.temp`` cards (identical to ``sim.py``'s ``_write_corner_deck``, so a
    lint and a sweep of the same request see the same circuit), then a
    ``.control`` block that ``pre_osdi``-loads the request's
    ``options.osdi_preload`` libraries (issue #2513, same as the sweep's
    deck), ``alter``s the supply sources, runs ``op``, and ``print``s every
    candidate op-point vector plus every probed node voltage, each device's
    block introduced by an ``echo`` marker."""
    lines = ["* klt sim --op-lint -- generated operating-point deck, do not edit"]
    # Same helper the sweep's deck uses, so the lint cannot drift from it on
    # multi-section bundles or issue #2522's per-section corner libraries.
    lines.extend(_corner_lib_lines(point, models_lib))
    lines.append(f".include {netlist_path}")
    lines.append(f".temp {point.temperature_c}")

    lines.append(".control")
    for path in osdi_preload:
        lines.append(f"pre_osdi {path}")
    lines.extend(_supply_alter_lines(point))
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
    if is_number(explicit):
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
    ``v(<node>)`` reference inside the request's own measurements -- the
    latter is what makes the common Loop A failure ("my measurement reads a
    node that does not exist") a named finding instead of a silent 0 V.

    A measurement contributes its ``v(...)`` references whether it is declared
    as a ``.meas`` card (``spice``) or as a post-analysis expression (``expr``,
    issue #2533) -- see :func:`_measurement_node_references`.
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

    for node, origin in _measurement_node_references(request):
        _add(node, origin)
    return collected


def _measurement_node_references(
    request: dict[str, Any],
) -> list[tuple[str, str]]:
    """Every ``v(<node>)`` reference in the request's own measurements, paired
    with the measurement name it came from.

    Scans both declarable measurement forms: a ``.meas`` card (``spice``) and
    a post-analysis expression (``expr``, issue #2533). ``expr`` is
    specifically how an *operating-point* quantity is declared -- the very
    analysis this lint runs -- so leaving it unscanned would blind the lint to
    exactly the measurements most likely to reach it.

    Also scans each ``analysis_steps[].measurements[]`` entry (issue #2482),
    reported as ``<step>.<name>`` -- with a step sequence, the top-level list
    holds only derived expressions, and the node references live in the
    steps.

    Split out of :func:`_declared_nodes` so that function's branch count does
    not grow with the number of scanned fields (see
    ``scripts/check_complexity_baseline.py``).
    """
    references: list[tuple[str, str]] = []
    for label, spec in _all_measurement_specs(request):
        origin = f"measurement {label!r}"
        for field in ("spice", "expr"):
            text = spec.get(field)
            if not isinstance(text, str):
                continue
            for match in _MEAS_NODE_RE.finditer(text):
                references.extend((group, origin) for group in match.groups() if group)
    return references


def _all_measurement_specs(
    request: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    """``(label, spec)`` for every measurement object the request declares:
    the top-level ``measurements[]``, then each ``analysis_steps[]`` step's
    own (labelled ``<step>.<name>``). Malformed entries are skipped -- ``klt
    sim`` itself owns validating the request's shape."""
    specs = [
        (str(spec.get("name", "?")), spec)
        for spec in request.get("measurements") or []
        if isinstance(spec, dict)
    ]
    steps = request.get("analysis_steps")
    for step in steps if isinstance(steps, list) else []:
        if not isinstance(step, dict) or not isinstance(step.get("measurements"), list):
            continue
        specs.extend(
            (f"{step.get('name', '?')}.{spec.get('name', '?')}", spec)
            for spec in step["measurements"]
            if isinstance(spec, dict)
        )
    return specs


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


def _refuse_param_axis(point: Any) -> None:
    """Issue #3035: the lint's deck does not apply the parameter axis, so it
    would bias the netlist's own defaults while echoing a corner that names
    other values."""
    if point.params:
        raise OpSanityError(
            f"corner '{point.corner_id}' carries corners.params, which the "
            "operating-point lint does not apply; lint a request without a "
            "parameter axis"
        )
    if point.options:
        # Issue #3125: the lint's deck carries no corner `.options` card.
        raise OpSanityError(
            f"corner '{point.corner_id}' carries corners.options, which the "
            "operating-point lint does not apply; lint a request without an "
            "options axis"
        )


def _corner_margin_entry(
    corner_id: str,
    devices: list[MosInstance],
    diagnostics: list[dict[str, str]],
) -> dict[str, Any]:
    """One ``corner_margins`` row: the worst ``|Vds| - |Vdsat|`` over the MOS
    devices that answered both values (``null`` when none did -- never a
    fabricated margin), the device owning it, and how many sit in triode."""
    worst_device: str | None = None
    worst_margin: float | None = None
    triode_count = 0
    for device in devices:
        vds = device.values.get("vds_v")
        vdsat = device.values.get("vdsat_v")
        if vds is None or vdsat is None:
            continue
        margin = abs(vds) - abs(vdsat)
        if margin < 0:
            triode_count += 1
        if worst_margin is None or margin < worst_margin:
            worst_margin = margin
            worst_device = device.name
    return {
        "corner_id": corner_id,
        "worst_device": worst_device,
        "worst_margin_v": None if worst_margin is None else round(worst_margin, 6),
        "triode_count": triode_count,
        "diagnostics": [d for d in diagnostics if d["severity"] == "error"],
    }


def _other_corner_entry(
    other: Any,
    *,
    pristine_devices: list[MosInstance],
    netlist_path: str,
    models_lib: str | None,
    probe_nodes: Any,
    timeout_s: Any,
    osdi_preload: Any,
) -> dict[str, Any]:
    """Bias one additional expanded corner and return its ``corner_margins`` row."""
    other_devices = copy.deepcopy(pristine_devices)
    other_log, _, _, other_launch = _run_op(
        netlist_path=netlist_path,
        models_lib=models_lib if _needs_models_lib(other) else None,
        point=other,
        devices=other_devices,
        probe_nodes=probe_nodes,
        timeout_s=timeout_s,
        osdi_preload=osdi_preload,
    )
    other_resolved, other_voltages = _parse_op_log(other_log, other_devices)
    other_diagnostics = list(other_launch)
    other_diagnostics.extend(_classify_diagnostics(other_log, netlist_path))
    produced = bool(other_voltages or other_resolved)
    if produced and not _SIMULATION_ABORTED_RE.search(other_log):
        for diagnostic in other_diagnostics:
            if diagnostic["code"] in ("singular_matrix", "nonconvergence"):
                diagnostic["severity"] = "warning"
    if other_devices and not produced:
        other_diagnostics.append(
            {
                "severity": "error",
                "code": "op_point_unavailable",
                "message": "the operating point produced no values",
            }
        )
    return _corner_margin_entry(other.corner_id, other_devices, other_diagnostics)


def _margin_sort_key(entry: dict[str, Any]) -> tuple[bool, float, str]:
    margin = entry["worst_margin_v"]
    return (margin is None, 0.0 if margin is None else margin, entry["corner_id"])


def _corner_margins_table(
    points: list[Any],
    first: Any,
    first_devices: list[MosInstance],
    first_diagnostics: list[dict[str, str]],
    **bias: Any,
) -> tuple[list[dict[str, Any]], bool]:
    """Build the ``corner_margins`` table (worst margin first) and report
    whether any corner other than the first raised an error diagnostic."""
    table = [_corner_margin_entry(first.corner_id, first_devices, first_diagnostics)]
    other_error = False
    for other in points:
        if other is first:
            continue
        entry = _other_corner_entry(other, **bias)
        table.append(entry)
        other_error = other_error or bool(entry["diagnostics"])
    table.sort(key=_margin_sort_key)
    return table, other_error


def _select_points(points: list[Any], corner: str | None) -> tuple[Any, list[Any]]:
    """Pick the first-reported point and the points to bias (all of them for
    ``corner="all"``), refusing a ``params`` axis on any biased point."""
    all_corners = corner == ALL_CORNERS
    if corner is not None and not all_corners:
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
    biased_points = points if all_corners else [point]
    for biased in biased_points:
        _refuse_param_axis(biased)
    return point, biased_points


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
    second corner sweep -- except for ``corner="all"`` (:data:`ALL_CORNERS`),
    which additionally biases every other expanded corner, sequentially, to
    fill the report's ``corner_margins`` table (issue #4650). The top-level
    ``corner``/``devices``/``findings`` still describe the first corner.

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

    parsed = parse_netlist(netlist_text, model_overrides, os.path.dirname(netlist_path))

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
    all_corners = corner == ALL_CORNERS
    point, biased_points = _select_points(points, corner)

    models = request.get("models") or {}
    models_lib: str | None = None
    try:
        # Issues #2522/#4759: resolve per-section corner libs and
        # `models.param_file`, then `models.lib` iff a section reads it.
        _resolve_corner_section_libs(biased_points, models, request_dir)
        _resolve_param_file([point, *biased_points], models, request_dir)
        if any(_needs_models_lib(biased) for biased in biased_points):
            models_lib = _resolve_models_lib(models, request_dir)
    except SimError as exc:
        raise OpSanityError(str(exc)) from exc
    point_models_lib = models_lib if _needs_models_lib(point) else None

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
    # Issue #2513: the same request document drives the sweep and this lint,
    # so an OSDI-model PDK's preload must reach this deck too -- otherwise
    # every device would be an undefined model here and only here.
    try:
        osdi_preload = _resolve_osdi_preload(options, request_dir)
    except SimError as exc:
        raise OpSanityError(str(exc)) from exc

    ground, supply = _classify_rails(
        parsed, sorted(point.supply_v), op_lint.get("rails") or {}
    )

    probe_nodes = sorted(node for node in parsed.nodes if node != "0")
    # Corners other than the first are biased on pristine device copies,
    # since `_parse_op_log` fills `values` in place.
    pristine_devices = copy.deepcopy(parsed.devices) if all_corners else []
    log_text, engine_version, runtime_s, launch_diagnostics = _run_op(
        netlist_path=netlist_path,
        models_lib=point_models_lib,
        point=point,
        devices=parsed.devices,
        probe_nodes=probe_nodes,
        timeout_s=timeout_s,
        osdi_preload=osdi_preload,
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

    for name in parsed.unexpanded:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "unsupported_hierarchy",
                "message": (
                    f"could not expand subcircuit instance {name}: its "
                    "definition was not found in the netlist or its "
                    "`.include`d files and it is not a recognised MOS model, "
                    "so any devices inside it were not linted."
                ),
            }
        )

    if parsed.expansion_truncated:
        diagnostics.append(
            {
                "severity": "warning",
                "code": "unsupported_hierarchy",
                "message": (
                    "subcircuit expansion stopped after "
                    f"{_MAX_EXPANDED_INSTANCES} instances (hierarchy "
                    "expansion cap); devices inside the remaining "
                    "subcircuit instances were not linted."
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

    status = _op_status(diagnostics, parsed, produced_values, bool(findings))

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

    corner_margins: list[dict[str, Any]] | None = None
    if all_corners:
        corner_margins, other_error = _corner_margins_table(
            points,
            point,
            parsed.devices,
            diagnostics,
            pristine_devices=pristine_devices,
            netlist_path=netlist_path,
            models_lib=models_lib,
            probe_nodes=probe_nodes,
            timeout_s=timeout_s,
            osdi_preload=osdi_preload,
        )
        if other_error:
            status = "error"

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
        "corner_margins": corner_margins,
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


def _op_status(
    diagnostics: list[dict[str, str]],
    parsed: ParsedNetlist,
    produced_values: bool,
    has_findings: bool,
) -> str:
    """Overall ``status`` of an op-lint run: error > findings > clean."""
    analysis_failed = any(
        diagnostic["severity"] == "error" for diagnostic in diagnostics
    ) or (parsed.devices and not produced_values)
    if analysis_failed:
        return "error"
    # No device was enumerated and some `X` instance went unexpanded: an
    # empty device list must never read as "every device is saturated".
    if has_findings or (parsed.unexpanded and not parsed.devices):
        return "findings"
    return "clean"


def _run_op(
    *,
    netlist_path: str,
    models_lib: str | None,
    point: CornerPoint,
    devices: list[MosInstance],
    probe_nodes: list[str],
    timeout_s: float,
    osdi_preload: tuple[str, ...] = (),
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
        # Issue #3624: a `corners.supply_waveforms` source is rescaled in a
        # per-corner netlist copy (as the sweep does), not `alter`ed.
        try:
            deck_netlist_path, _ = _scale_supply_waveforms(
                netlist_path, netlist_path, work_dir, point
            )
        except SimError as exc:
            raise OpSanityError(str(exc)) from exc
        _write_op_deck(
            deck_path=deck_path,
            netlist_path=deck_netlist_path,
            models_lib=models_lib,
            point=point,
            devices=devices,
            probe_nodes=probe_nodes,
            osdi_preload=osdi_preload,
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
