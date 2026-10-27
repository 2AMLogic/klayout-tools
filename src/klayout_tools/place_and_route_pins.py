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
_PORT_NAME = r"(?:\\\S+|(?!(?:input|output|inout)\b)[A-Za-z_][\w$]*)"
_PORT_DECL_RE = re.compile(
    r"\b(?:input|output|inout)\b\s*(?:wire\s+|reg\s+)?"
    r"(?:\[\s*(\d+)\s*:\s*(\d+)\s*\]\s*)?"
    rf"({_PORT_NAME}(?:\s*,\s*{_PORT_NAME})*)"
)
_PORT_NAME_RE = re.compile(_PORT_NAME)
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


def _port_bits(name: str, msb: str, lsb: str) -> list[str]:
    """``name`` itself, or ``name[i]`` for each bit of its ``[msb:lsb]`` range."""
    if msb == "":
        return [name]
    hi, lo = int(msb), int(lsb)
    step = -1 if hi >= lo else 1
    return [f"{name}[{i}]" for i in range(hi, lo + step, step)]


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
        for msb, lsb, names in _PORT_DECL_RE.findall(line):
            for name in _PORT_NAME_RE.findall(names):
                for bit in _port_bits(name.lstrip("\\"), msb, lsb):
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


def check_io_pin_constraints(
    io_spec: dict[str, Any] | None, netlist_path: str, hdl_toplevel: str
) -> None:
    """:func:`check_pin_constraints` for a validated ``request.io`` (or none)."""
    if io_spec and io_spec.get("pin_constraints"):
        check_pin_constraints(
            io_spec["pin_constraints"], netlist_top_ports(netlist_path, hdl_toplevel)
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


_PPL_0110_RE = re.compile(
    r"PPL-0110\]?\s*Constraint has (?P<pins>\d+) pins?, "
    r"but only (?P<slots>\d+) available slots?"
)


def slot_shortage_hint(
    log: str,
    constraints: list[dict[str, Any]] | None,
    ports: list[str] | None,
) -> str | None:
    """Hint for a failed ``place`` whose log holds ``PPL-0110`` lines, else
    ``None`` (issue #4155).

    OpenROAD's ``PPL-0111`` summary only counts the over-subscribed
    constraints; the useful ``PPL-0110`` line (pins vs. slots) does not name
    the entry. An entry is named only when its glob-expanded pin count equals
    the line's pin count and no other entry does; otherwise the ``PPL-0110``
    line is quoted verbatim.
    """
    found = list(_PPL_0110_RE.finditer(log))
    if not found:
        return None
    parts: list[str] = []
    for match in found:
        want = int(match.group("pins"))
        candidates: list[int] = []
        if constraints and ports is not None:
            for index, entry in enumerate(constraints):
                bits = {hit for pat in entry["pins"] for hit in match_pins(pat, ports)}
                if len(bits) == want:
                    candidates.append(index)
        if len(candidates) == 1:
            parts.append(
                f"io.pin_constraints[{candidates[0]}] has {want} pins but its "
                f"range has only {match.group('slots')} available slots"
            )
        else:
            parts.append(match.group(0))
    return "; ".join(parts) + " -- widen range_um or drop it for those entries"


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


_COMPONENTS_STMT_RE = re.compile(r"-\s+(\S+)\s+\S+([^;]*);")


def read_def_instance_groups(
    def_path: str, groups: list[dict[str, Any]]
) -> list[dict[str, Any]] | None:
    """Placed-origin bounding box of each ``request.instance_groups`` entry,
    read from the DEF ``COMPONENTS`` section (issue #4344), or ``None`` when
    the DEF is unreadable or has no ``UNITS``.

    Each result is ``{name, matched, bbox, max_extent_um, within_extent}``:
    ``matched`` counts the instances whose name matches any glob of the group;
    ``bbox`` is ``{x_min_um, x_max_um, y_min_um, y_max_um}`` over the *origins*
    of those that are placed (COMPONENTS carries no master size), or ``None``
    when none are; ``within_extent`` is ``None`` unless ``max_extent_um`` was
    given and a ``bbox`` exists.
    """
    try:
        with open(def_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None
    units = _UNITS_RE.search(text)
    if units is None:
        return None
    scale = int(units.group(1))
    start = text.find("\nCOMPONENTS ")
    end = text.find("END COMPONENTS", start)
    placed: list[tuple[str, int | None, int | None]] = []
    if start >= 0 and end >= 0:
        for name, rest in _COMPONENTS_STMT_RE.findall(text[start:end]):
            at = _PIN_PLACED_RE.search(rest)
            placed.append(
                (
                    name.replace("\\", ""),
                    int(at.group(1)) if at else None,
                    int(at.group(2)) if at else None,
                )
            )
    names = [name for name, _, _ in placed]
    results: list[dict[str, Any]] = []
    for group in groups:
        hits = {
            hit for pattern in group["instances"] for hit in match_pins(pattern, names)
        }
        origins = [
            (x, y)
            for name, x, y in placed
            if name in hits and x is not None and y is not None
        ]
        bbox = None
        if origins:
            xs: list[int] = [x for x, _ in origins if x is not None]
            ys: list[int] = [y for _, y in origins if y is not None]
            bbox = {
                "x_min_um": round(min(xs) / scale, 4),
                "x_max_um": round(max(xs) / scale, 4),
                "y_min_um": round(min(ys) / scale, 4),
                "y_max_um": round(max(ys) / scale, 4),
            }
        bound = group.get("max_extent_um")
        within = None
        if bbox is not None and bound is not None:
            span = max(
                bbox["x_max_um"] - bbox["x_min_um"], bbox["y_max_um"] - bbox["y_min_um"]
            )
            within = span <= bound
        results.append(
            {
                "name": group["name"],
                "matched": len(hits),
                "bbox": bbox,
                "max_extent_um": bound,
                "within_extent": within,
            }
        )
    return results
