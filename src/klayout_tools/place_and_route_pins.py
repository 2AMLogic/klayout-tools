"""Top-level pin placement for ``klt place-and-route`` (issue #3885).

Three small, independent pieces, kept out of ``place_and_route.py`` so the
latter's module-level complexity does not grow:

* :func:`netlist_top_ports` -- the top module's port bits, read from the
  gate-level Verilog, so ``io.pin_constraints`` patterns can be checked at
  request time (a pattern that matches nothing is an error, never a silent
  no-op -- the same failure class as issue #2473).
* :func:`pin_constraint_lines` -- the ``set_io_pin_constraint`` Tcl emitted
  before ``place_pins``.
* :func:`read_def_pins` -- the final edge/coordinate of every top-level pin,
  read from the DEF the run already wrote, for the response's ``pins[]``.

OpenROAD region names are ``top``/``right``/``bottom``/``left``; the request
uses compass names (``north``/``east``/``south``/``west``).
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any

EDGE_TO_REGION = {
    "north": "top",
    "east": "right",
    "south": "bottom",
    "west": "left",
}

_MODULE_START_RE = re.compile(r"^\s*module\s+(\\?\S+?)\s*[\s(;]")
_MODULE_END_RE = re.compile(r"^\s*endmodule\b")
_PORT_DECL_RE = re.compile(
    r"\b(?:input|output|inout)\b\s*(?:wire\s+|reg\s+)?"
    r"(?:\[\s*(\d+)\s*:\s*(\d+)\s*\]\s*)?(\\\S+|[A-Za-z_][\w$]*)"
)
_UNITS_RE = re.compile(r"UNITS\s+DISTANCE\s+MICRONS\s+(\d+)\s*;")
_DIEAREA_RE = re.compile(
    r"DIEAREA\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)"
)
_PIN_START_RE = re.compile(r"^\s*-\s+(\S+)\s+\+\s+NET\b")
_PIN_LAYER_RE = re.compile(
    r"\+\s*LAYER\s+(\S+)\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)"
)
_PIN_PLACED_RE = re.compile(
    r"\+\s*(?:PLACED|FIXED|COVER)\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)"
)


def netlist_top_ports(netlist_path: str, module_name: str) -> list[str] | None:
    """Port bit names (``a[3]`` for a bus bit) of ``module_name`` in the
    Verilog at ``netlist_path``, or ``None`` when the file or module cannot
    be read/found (the caller then skips the match check rather than guess)."""
    try:
        with open(netlist_path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    ports: list[str] = []
    in_module = False
    found = False
    for line in lines:
        if not in_module:
            match = _MODULE_START_RE.match(line)
            if match and match.group(1).lstrip("\\") == module_name:
                in_module = found = True
            else:
                continue
        elif _MODULE_END_RE.match(line):
            break
        for msb, lsb, name in _PORT_DECL_RE.findall(line):
            name = name.lstrip("\\")
            if msb == "":
                bits = [name]
            else:
                hi, lo = int(msb), int(lsb)
                step = -1 if hi >= lo else 1
                bits = [f"{name}[{i}]" for i in range(hi, lo + step, step)]
            for bit in bits:
                if bit not in ports:
                    ports.append(bit)
    return ports if found else None


def match_pins(pattern: str, ports: list[str]) -> list[str]:
    """Ports matching ``pattern`` as a glob, with ``[``/``]`` literal (so
    ``a[3]`` names that bit rather than a character class)."""
    escaped = pattern.replace("[", "[[]")
    return [port for port in ports if fnmatch.fnmatchcase(port, escaped)]


def check_pin_constraints(
    constraints: list[dict[str, Any]], ports: list[str] | None
) -> None:
    """Raise when a pattern matches no top-level port, or a port is matched by
    two entries. No-op when the port list is unknown."""
    from .place_and_route_request import PlaceAndRouteError

    if ports is None:
        return
    owner: dict[str, int] = {}
    for index, entry in enumerate(constraints):
        for pattern in entry["pins"]:
            hits = match_pins(pattern, ports)
            if not hits:
                raise PlaceAndRouteError(
                    f"request.io.pin_constraints[{index}].pins pattern "
                    f"'{pattern}' matches no top-level port of the netlist"
                )
            for hit in hits:
                if owner.setdefault(hit, index) != index:
                    raise PlaceAndRouteError(
                        f"request.io.pin_constraints: pin '{hit}' is matched "
                        f"by entries {owner[hit]} and {index}"
                    )


def pin_constraint_lines(constraints: list[dict[str, Any]]) -> list[str]:
    """``set_io_pin_constraint`` Tcl, one command per entry; ``[]`` for none."""
    lines: list[str] = []
    for entry in constraints:
        region = EDGE_TO_REGION[entry["edge"]]
        span = entry.get("range_um")
        interval = f"{span[0]:g}-{span[1]:g}" if span else "*"
        names = " ".join(_tcl_word(p) for p in entry["pins"])
        flags = " -group -order" if entry.get("ordered") else ""
        lines.append(
            f"set_io_pin_constraint -pin_names {{{names}}} "
            f"-region {region}:{interval}{flags}"
        )
    return lines


def _tcl_word(pattern: str) -> str:
    # Inside a braced word `[`/`]` are literal, which is what `a[3]` needs.
    return pattern


def read_def_pins(def_path: str) -> list[dict[str, Any]] | None:
    """Final edge and coordinates of every top-level pin in ``def_path``
    (``[{name, edge, x_um, y_um, layer}]``), or ``None`` when the DEF is
    unreadable or has no ``DIEAREA``."""
    try:
        with open(def_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None
    units = _UNITS_RE.search(text)
    die = _DIEAREA_RE.search(text)
    if units is None or die is None:
        return None
    scale = int(units.group(1))
    x0, y0, x1, y1 = (int(v) for v in die.groups())
    start = text.find("\nPINS ")
    end = text.find("END PINS", start)
    if start < 0 or end < 0:
        return []
    pins: list[dict[str, Any]] = []
    for block in re.split(r"(?m)^\s*-\s+", text[start:end])[1:]:
        block = "- " + block
        head = _PIN_START_RE.match(block)
        placed = _PIN_PLACED_RE.search(block)
        if head is None or placed is None:
            continue
        layer = _PIN_LAYER_RE.search(block)
        px, py = int(placed.group(1)), int(placed.group(2))
        # Pin geometry is relative to the placed origin; use its centre.
        if layer:
            cx = px + (int(layer.group(2)) + int(layer.group(4))) / 2
            cy = py + (int(layer.group(3)) + int(layer.group(5))) / 2
        else:
            cx, cy = px, py
        dist = {
            "west": abs(px - x0),
            "east": abs(x1 - px),
            "south": abs(py - y0),
            "north": abs(y1 - py),
        }
        pins.append(
            {
                "name": head.group(1).replace("\\", ""),
                "edge": min(dist, key=lambda edge: dist[edge]),
                "x_um": round(cx / scale, 4),
                "y_um": round(cy / scale, 4),
                "layer": layer.group(1) if layer else None,
            }
        )
    return pins
