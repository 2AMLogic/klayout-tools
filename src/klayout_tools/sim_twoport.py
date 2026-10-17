"""``analysis.kind: "twoport"`` -- first-class small-signal S-parameters (#3338).

A request declares the ports and a frequency grid; ``klt sim`` compiles the
two-excitation AC deck itself, reduces it to per-frequency complex
S11/S21/S12/S22 and reports them (plus a Touchstone ``.s2p`` sidecar)::

    {"analysis": {"kind": "twoport",
                  "ports": [{"p": "in",  "n": "0", "z0": 50},
                            {"p": "out", "n": "0", "z0": 50}],
                  "freq": {"sweep": "dec", "points": 20,
                           "start": 1e6, "stop": 1e9}}}

The request form is resolved (:func:`resolve_analysis`) into an ordinary
``ac`` analysis carrying the normalised ``twoport`` object, so the rest of
``klt sim`` sees an AC corner.

Deck. Each port ``k`` gets, at file scope, a source ``Vkltp<k>`` (AC
magnitude 0) behind a series resistor ``Rkltp<k>`` of value ``z0`` driving
port node ``p`` against reference node ``n``. The ``.control`` block then runs
one ``ac`` per excitation (``alter Vkltp<k> ac = 1``, the other source 0),
each in a fresh plot, and ``write``\\ s the two port voltages
``V_j = v(p_j) - v(n_j)`` to ``twoport_<k>.raw``.

Reduction. With a 1 V source behind ``z0`` the incident wave at the excited
port is ``a = 1 / (2 sqrt(z0))`` and the outgoing wave at port ``j`` is
``b_j = V_j / sqrt(z0) - a_j``. For the one ``z0`` the ports share:

    excite port k:  S_kk = 2 V_k - 1,   S_jk = 2 V_j   (j != k)

Both ports must share one ``z0`` (Touchstone 1.1 has one scalar reference
impedance). The port terminations are present at DC too, so the netlist must
bias the circuit through them or around them (bias tee, ideal inductor).

Every failure (rawfile missing, a non-AC plot, a missing or non-finite port
voltage, passes with different frequency axes) is an ``error`` diagnostic --
never a quiet ``pass``.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from typing import Any

from . import sim_raw, touchstone
from .sim_errors import SimError

KIND = "twoport"
SWEEPS = ("lin", "dec")

#: Reason tokens a failed corner's diagnostic message leads with.
ERROR_CODES = (
    "twoport_missing",
    "twoport_raw_invalid",
    "twoport_non_finite",
    "twoport_axis_mismatch",
)

S2P_FILENAME = "twoport.s2p"

_NODE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:$#!<>\[\]]*$")
_GROUND = frozenset({"0", "gnd"})
_REQUEST_KEYS = frozenset({"kind", "ports", "freq"})
_PORT_KEYS = frozenset({"p", "n", "z0"})
_FREQ_KEYS = frozenset({"sweep", "points", "start", "stop"})
_VECTOR_PREFIX = "__klt_tp_"


def is_twoport_request(analysis: Any) -> bool:
    """Is ``analysis`` the (unresolved) request form ``kind: "twoport"``?"""
    return isinstance(analysis, dict) and str(analysis.get("kind", "")).lower() == KIND


def is_twoport(analysis: dict[str, Any]) -> bool:
    """Is ``analysis`` a resolved two-port analysis?"""
    return analysis.get(KIND) is not None


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SimError(f"{where} must be a number (got {value!r})")
    if not math.isfinite(value):
        raise SimError(f"{where} must be finite (got {value!r})")
    return float(value)


def _resolve_port(entry: Any, index: int) -> dict[str, Any]:
    where = f"analysis.ports[{index}]"
    if not isinstance(entry, dict):
        raise SimError(f'{where} must be an object {{"p", "n", "z0"}}')
    unknown = sorted(set(entry) - _PORT_KEYS)
    if unknown:
        raise SimError(
            f"{where}: unknown key(s) {unknown}; allowed: {sorted(_PORT_KEYS)}"
        )
    nodes: dict[str, str] = {}
    for key in ("p", "n"):
        node = entry.get(key)
        if not isinstance(node, str) or not _NODE_RE.match(node):
            raise SimError(
                f"{where}.{key} must be a node name (letters, digits, "
                f"underscore; no whitespace, comma, parenthesis or operator); "
                f"got {node!r}"
            )
        nodes[key] = node
    if "z0" not in entry:
        raise SimError(f"{where}.z0 is required (the port's reference impedance, ohm)")
    z0 = _number(entry["z0"], f"{where}.z0")
    if z0 <= 0:
        raise SimError(f"{where}.z0 must be positive (got {entry['z0']!r})")
    if nodes["p"].lower() == nodes["n"].lower() or (
        nodes["p"].lower() in _GROUND and nodes["n"].lower() in _GROUND
    ):
        raise SimError(f"{where}: p and n are the same node ({nodes['p']!r})")
    if nodes["p"].lower() in _GROUND:
        raise SimError(f"{where}.p must not be ground; put ground on n")
    return {"p": nodes["p"], "n": nodes["n"], "z0": z0}


def _resolve_freq(freq: Any) -> dict[str, Any]:
    if not isinstance(freq, dict):
        raise SimError(
            'analysis.freq must be an object {"sweep", "points", "start", "stop"}'
        )
    unknown = sorted(set(freq) - _FREQ_KEYS)
    if unknown:
        raise SimError(
            f"analysis.freq: unknown key(s) {unknown}; allowed: {sorted(_FREQ_KEYS)}"
        )
    sweep = freq.get("sweep")
    if sweep not in SWEEPS:
        raise SimError(
            f"analysis.freq.sweep must be one of {list(SWEEPS)} (got {sweep!r})"
        )
    points = freq.get("points")
    if isinstance(points, bool) or not isinstance(points, int) or points < 1:
        raise SimError(
            f"analysis.freq.points must be a positive integer (got {points!r})"
        )
    start = _number(freq.get("start"), "analysis.freq.start")
    stop = _number(freq.get("stop"), "analysis.freq.stop")
    if start <= 0:
        raise SimError(f"analysis.freq.start must be positive (got {freq['start']!r})")
    if sweep == "lin" and points == 1:
        if stop != start:
            raise SimError(
                "analysis.freq: a single-point lin sweep needs stop == start "
                f"(got start {start!r}, stop {stop!r})"
            )
    elif stop <= start:
        raise SimError(
            f"analysis.freq.stop must exceed start (got start {start!r}, stop {stop!r})"
        )
    return {"sweep": sweep, "points": points, "start": start, "stop": stop}


def resolve_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
    """``analysis`` itself unless it is the request form ``kind: "twoport"``;
    then the equivalent ``ac`` analysis carrying the normalised ``twoport``
    object. Raises before any engine launch."""
    if not is_twoport_request(analysis):
        return analysis
    unknown = sorted(set(analysis) - _REQUEST_KEYS)
    if unknown:
        raise SimError(
            f'analysis (kind "twoport"): unknown key(s) {unknown}; allowed: '
            f"{sorted(_REQUEST_KEYS)} (a two-port compiles its own AC args)"
        )
    raw_ports = analysis.get("ports")
    if not isinstance(raw_ports, list) or len(raw_ports) != 2:
        raise SimError(
            'analysis.ports must be an array of exactly two {"p", "n", "z0"} '
            f"port objects (got {raw_ports!r})"
        )
    ports = [_resolve_port(entry, i) for i, entry in enumerate(raw_ports)]
    if ports[0]["z0"] != ports[1]["z0"]:
        raise SimError(
            "analysis.ports have differing z0 "
            f"({ports[0]['z0']!r} vs {ports[1]['z0']!r}): the Touchstone 1.1 "
            ".s2p sidecar has one scalar reference impedance, so per-port "
            "differing z0 is not supported"
        )
    if ports[0]["p"].lower() == ports[1]["p"].lower():
        raise SimError(
            f"analysis.ports share the signal node {ports[0]['p']!r}: the two "
            "ports must be distinct"
        )
    freq = _resolve_freq(analysis.get("freq"))
    args = f"{freq['sweep']} {freq['points']} {freq['start']!r} {freq['stop']!r}"
    return {"kind": "ac", "args": args, KIND: {"ports": ports, "freq": freq}}


def refuse_unsupported(
    analysis: dict[str, Any],
    measurements_spec: list[dict[str, Any]],
    *,
    want_waveforms: bool,
    plot_requested: bool,
) -> None:
    """Refuse, by name, what a two-port acquisition does not implement."""
    if not is_twoport(analysis):
        return
    if measurements_spec:
        raise SimError(
            'analysis.kind "twoport" reports S-parameters and takes no '
            "measurements[]: each corner runs two AC excitations, so a "
            "`.meas`/`expr` measurement would read only one of them"
        )
    if want_waveforms or plot_requested:
        raise SimError(
            "options.waveforms / --plot are not supported with analysis.kind "
            '"twoport": the waveform artifact captures one analysis per '
            "corner, and a two-port corner runs two"
        )


def forces_artifacts(analysis: dict[str, Any], keep_artifacts: bool) -> bool:
    """``keep_artifacts``, forced on for a two-port (the sidecar is the result)."""
    return keep_artifacts or is_twoport(analysis)


def refuse_xyce(analysis: dict[str, Any], engine: str) -> None:
    if is_twoport(analysis) and engine == "xyce":
        raise SimError(
            "analysis.kind \"twoport\" is not supported for engine 'xyce': the "
            "two excitations are ngspice `alter`/`ac`/`write` commands in a "
            "`.control` block, which the Xyce deck does not have; use engine "
            "'ngspice'"
        )


# --------------------------------------------------------------------------- #
# Deck emission
# --------------------------------------------------------------------------- #


def _source(index: int) -> str:
    return f"Vkltp{index + 1}"


def deck_lines(analysis: dict[str, Any]) -> list[str]:
    """File-scope port cards: per port, ``z0`` in series with an AC-0 source."""
    if not is_twoport(analysis):
        return []
    lines: list[str] = []
    for index, port in enumerate(analysis[KIND]["ports"]):
        mid = f"klt_tp{index + 1}_x"
        lines.append(f"Rkltp{index + 1} {port['p']} {mid} {port['z0']!r}")
        lines.append(f"{_source(index)} {mid} {_return_node(port)} DC 0 AC 0")
    return lines


def _return_node(port: dict[str, Any]) -> str:
    return "0" if port["n"].lower() in _GROUND else port["n"]


def _voltage_expr(port: dict[str, Any]) -> str:
    if port["n"].lower() in _GROUND:
        return f"v({port['p']})"
    return f"v({port['p']}) - v({port['n']})"


def rawfile_path(corner_dir: str, excited: int) -> str:
    return os.path.join(corner_dir, f"twoport_{excited + 1}.raw")


def control_lines(analysis: dict[str, Any], corner_dir: str) -> list[str]:
    """The two-excitation sequence for the ``.control`` block (after the
    corner prefix, before ``quit``)."""
    ports = analysis[KIND]["ports"]
    names = [f"{_VECTOR_PREFIX}{i}" for i in range(len(ports))]
    lines = ["set filetype=ascii"]
    for excited in range(len(ports)):
        for index in range(len(ports)):
            lines.append(f"alter {_source(index)} ac = {1 if index == excited else 0}")
        lines.append("setplot new")
        lines.append(f"{analysis['kind']} {analysis['args']}")
        lines.extend(
            f"let {name} = {_voltage_expr(port)}"
            for name, port in zip(names, ports, strict=True)
        )
        lines.append(f"write {rawfile_path(corner_dir, excited)} {' '.join(names)}")
    return lines


# --------------------------------------------------------------------------- #
# Reduction (pure)
# --------------------------------------------------------------------------- #


def reduce_s(
    drive1: tuple[list[complex], list[complex]],
    drive2: tuple[list[complex], list[complex]],
) -> dict[str, list[complex]]:
    """``S11/S21/S12/S22`` per frequency from the port voltages ``(V1, V2)`` of
    the port-1 excitation (``drive1``) and the port-2 excitation (``drive2``),
    each driven by 1 V behind the shared ``z0``."""
    v1_a, v2_a = drive1
    v1_b, v2_b = drive2
    return {
        "s11": [2 * v - 1 for v in v1_a],
        "s21": [2 * v for v in v2_a],
        "s12": [2 * v for v in v1_b],
        "s22": [2 * v - 1 for v in v2_b],
    }


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


@dataclass
class TwoportOutcome:
    """One corner's result: frequencies, S arrays and sidecar path, or errors."""

    freqs: list[float] = field(default_factory=list)
    s: dict[str, list[complex]] = field(default_factory=dict)
    z0: float | None = None
    s2p_path: str | None = None
    diagnostics: list[dict[str, str]] = field(default_factory=list)


def _error(code: str, message: str) -> dict[str, str]:
    return {
        "severity": "error",
        "code": "measurement",
        "message": f"[{code}] {message}",
    }


def _fail(code: str, message: str) -> TwoportOutcome:
    return TwoportOutcome(diagnostics=[_error(code, message)])


def _bare(name: str) -> str:
    return name[2:-1] if name.startswith("v(") and name.endswith(")") else name


def _read_drive(
    excited: int, corner_dir: str, port_count: int
) -> tuple[tuple[list[float], list[list[complex]]] | None, dict[str, str] | None]:
    """``(freqs, per-port voltage columns)`` of one excitation, or a
    diagnostic."""
    label = f"port-{excited + 1} excitation"
    path = rawfile_path(corner_dir, excited)
    if not os.path.isfile(path):
        return None, _error(
            "twoport_missing",
            f"{label}: its AC analysis wrote no rawfile (the solve failed, "
            "never ran, or a port node does not resolve)",
        )
    try:
        raw = sim_raw.read_complex_ascii_rawfile(path)
    except sim_raw.RawFileError as exc:
        return None, _error(
            "twoport_raw_invalid", f"{label}: could not read the rawfile: {exc}"
        )
    if not raw.plotname.lower().startswith("ac analysis"):
        return None, _error(
            "twoport_raw_invalid",
            f"{label}: its rawfile holds the {raw.plotname!r} plot, not an AC "
            "analysis (the solve failed -- see this corner's log)",
        )
    if not raw.points:
        return None, _error(
            "twoport_raw_invalid", f"{label}: the rawfile holds no samples"
        )
    columns = {_bare(name): i for i, name in enumerate(raw.names)}
    voltages: list[list[complex]] = []
    for port in range(port_count):
        index = columns.get(f"{_VECTOR_PREFIX}{port}")
        if index is None:
            return None, _error(
                "twoport_missing",
                f"{label}: the voltage of port {port + 1} is missing from the rawfile",
            )
        voltages.append(raw.column(index))
    freqs = [z.real for z in raw.column(0)]
    for port, column in enumerate(voltages):
        if not all(math.isfinite(z.real) and math.isfinite(z.imag) for z in column):
            return None, _error(
                "twoport_non_finite",
                f"{label}: the voltage of port {port + 1} has a non-finite sample",
            )
    if not all(math.isfinite(f) for f in freqs):
        return None, _error(
            "twoport_non_finite", f"{label}: the frequency axis has a non-finite sample"
        )
    return (freqs, voltages), None


def extract_corner(analysis: dict[str, Any], corner_dir: str) -> TwoportOutcome:
    """Decode one finished corner's two rawfiles, reduce to S and write the
    ``.s2p`` sidecar. Never raises; a problem becomes an ``error`` diagnostic.
    An empty outcome for a non-two-port analysis."""
    if not is_twoport(analysis):
        return TwoportOutcome()
    ports = analysis[KIND]["ports"]
    drives = []
    for excited in range(len(ports)):
        drive, diagnostic = _read_drive(excited, corner_dir, len(ports))
        if diagnostic is not None:
            return TwoportOutcome(diagnostics=[diagnostic])
        drives.append(drive)
    (freqs_a, volts_a), (freqs_b, volts_b) = drives  # type: ignore[misc]
    if freqs_a != freqs_b:
        return _fail(
            "twoport_axis_mismatch",
            "the two excitations' AC sweeps have different frequency axes "
            f"({len(freqs_a)} vs {len(freqs_b)} points)",
        )
    z0 = ports[0]["z0"]
    s = reduce_s((volts_a[0], volts_a[1]), (volts_b[0], volts_b[1]))
    outcome = TwoportOutcome(freqs=freqs_a, s=s, z0=z0)
    path = os.path.join(corner_dir, S2P_FILENAME)
    text = touchstone.format_s2p(
        freqs_a,
        s["s11"],
        s["s21"],
        s["s12"],
        s["s22"],
        z0,
        comments=[
            "klt sim twoport S-parameters (issue #3338)",
            f"Touchstone Rev {touchstone.TOUCHSTONE_VERSION} -- frequency unit "
            "Hz, S-parameters, RI (real/imaginary) format",
            f"port 1: {ports[0]['p']} / {ports[0]['n']}",
            f"port 2: {ports[1]['p']} / {ports[1]['n']}",
        ],
    )
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError as exc:
        return _fail("twoport_raw_invalid", f"could not write the .s2p sidecar: {exc}")
    outcome.s2p_path = path
    return outcome


def artifact_fields(
    analysis: dict[str, Any], outcome: TwoportOutcome
) -> dict[str, Any]:
    """The ``artifacts.twoport_s2p`` entry; empty for a non-two-port run."""
    if not is_twoport(analysis):
        return {}
    return {"twoport_s2p": outcome.s2p_path}


def corner_fields(analysis: dict[str, Any], outcome: TwoportOutcome) -> dict[str, Any]:
    """The additive per-corner ``twoport`` field: ports, ``z0`` and the S
    arrays as ``real``/``imag`` lists per parameter. Empty for a non-two-port
    run; the S arrays are empty lists on a failed corner."""
    if not is_twoport(analysis):
        return {}
    spec = analysis[KIND]
    names = ("s11", "s21", "s12", "s22")
    return {
        "twoport": {
            "ports": [dict(port) for port in spec["ports"]],
            "z0_ohm": outcome.z0 if outcome.z0 is not None else spec["ports"][0]["z0"],
            "points": len(outcome.freqs),
            "frequency_hz": list(outcome.freqs),
            "s_parameters": {
                name: {
                    "real": [z.real for z in outcome.s.get(name, [])],
                    "imag": [z.imag for z in outcome.s.get(name, [])],
                }
                for name in names
            },
        }
    }
