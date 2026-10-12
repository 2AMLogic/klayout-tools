"""Check a GDSII/OASIS layout against a socket/template descriptor: does the
layout fit its declared outline, expose its declared pins, and stay off its
reserved layers?

Pure library: :func:`run_socket_check` returns plain Python data (a ``dict``
of JSON-serialisable primitives) and never prints, mirroring ``drc.py`` /
``precheck.py``.

Headless invariant: uses only the pip ``klayout`` package's batch database
API (``klayout.db``) -- no GUI, no Qt, and no dependency on the standalone
``klayout`` application binary.

Scope: a socket descriptor (see ``docs/schemas/socket.schema.json``) declares
four things -- an ``outline`` (bounding box), a ``pins`` list (name,
position, layer, and informational size), a ``reserved_layers`` list, and a
``budgets`` list of arbitrary named numeric interface budgets (e.g. maximum
signal-path resistance/capacitance, minimum power-stripe width). Only the
first three are *mechanically checkable* from geometry alone -- this module
runs a genuine pass/fail check for each of them. ``budgets`` are reported
back verbatim with a ``"declared_unverified"`` status rather than
half-implemented as a check that can't run: verifying a resistance/
capacitance/current budget needs simulation or parasitic-extraction data
this module does not have (see ``docs/cli/socket-check.md``'s Scope
section). A pin's ``width_um``/``height_um`` are likewise descriptive
metadata only in this version -- not checked against drawn geometry.

Unlike ``drc.py``/``precheck.py`` (which report bounding boxes in database
units), this module reports positions/bounding boxes in **micrometres**,
matching the descriptor's own coordinate convention (see
``docs/schemas/socket.schema.json``) -- the caller who authored the socket
descriptor in um should never need to know the checked layout's dbu to read
a violation.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from ._check_result import finish as _finish
from ._check_result import fmt_layer as _fmt_layer
from ._check_result import skipped as _skipped
from ._layout import cells_in_hierarchy, load_layout
from ._layout import select_top_cells as _select_top_cells

#: Every check name this module runs, in report order. ``power_ports``
#: (issue #2839) is appended last, so the original three keep their
#: positions; it is ``"skipped"`` unless requested (see
#: :func:`run_socket_check`).
CHECK_NAMES: tuple[str, ...] = ("pins", "outline", "reserved_layers", "power_ports")

#: The four outline edges, in report order (issue #2839).
POWER_PORT_EDGES: tuple[str, ...] = ("left", "bottom", "right", "top")

#: Default edge band for the ``power_ports`` check (issue #2839), in
#: micrometres: a power/ground port's drawn shape must **touch** the outline
#: (distance ``<= 0``) on at least one counted edge. **Provisional**: the T1
#: item 11 port-accessibility amendment (2AMLogic/product#134) is not yet
#: ratified and leaves the band open (its draft proposes "one minimum metal
#: pitch of the port's layer"). Zero is the conservative choice -- the
#: strictest band, and PDK-independent -- and every consumer can widen it
#: (descriptor ``power_ports.edge_band_um``, ``--edge-band-um``, or a
#: ``klt signoff`` manifest's ``power_ports.edge_band_um``).
DEFAULT_POWER_PORT_EDGE_BAND_UM = 0.0

#: LEF ``USE`` values that make a declared pin a supply port (issue #2839).
SUPPLY_PIN_USES: frozenset[str] = frozenset({"POWER", "GROUND"})

#: Name substrings (checked case-insensitively against each alphanumeric
#: token, e.g. ``AVDD33`` matches ``VDD``, ``VD`` does not) that classify a
#: pin as a supply when the descriptor does not declare an explicit
#: ``use``/``direction`` -- this repo's own documented heuristic (there is
#: no mechanical way to derive this from geometry alone), not an
#: open_pdks/LEF-spec convention. Shared by `klt lef-abstract`'s emitted
#: ``PIN USE`` and this module's ``power_ports`` check (issue #2839). See
#: :func:`classify_pin_by_name`.
GROUND_NAME_TOKENS: tuple[str, ...] = ("VSS", "VGND", "GND", "VNB")
POWER_NAME_TOKENS: tuple[str, ...] = ("VDD", "VPWR", "VCC", "VPB")

_NAME_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def classify_pin_by_name(name: str) -> tuple[str, str]:
    """``(direction, use)`` heuristic for a pin the descriptor does not
    explicitly classify. Name tokens are matched case-insensitively as whole
    alphanumeric runs (``AVDD33`` matches ``VDD`` via its ``AVDD`` token
    containing it; ``VD`` alone does not match anything) against
    :data:`GROUND_NAME_TOKENS`/:data:`POWER_NAME_TOKENS`. Falls back to
    ``("INOUT", "ANALOG")`` -- LEF has no way to derive true signal
    direction from geometry alone, and ``ANALOG`` is the LEF-spec ``USE``
    value for exactly this class of pin (`klt lef-abstract`'s macros are
    analog blocks, per Epic #393's own scope).
    """
    tokens = [tok.upper() for tok in _NAME_TOKEN_RE.findall(name)]
    for token in tokens:
        if any(marker in token for marker in GROUND_NAME_TOKENS):
            return "INOUT", "GROUND"
        if any(marker in token for marker in POWER_NAME_TOKENS):
            return "INOUT", "POWER"
    return "INOUT", "ANALOG"


#: Descriptor format identifier (see ``docs/schemas/socket.schema.json``).
SOCKET_SCHEMA = "klt.socket/1"

#: Valid ``pins[].direction``/``pins[].use`` values -- LEF's own ``PIN
#: DIRECTION``/``PIN USE`` vocabulary (LEF 5.7 section 5.6.2). Optional,
#: descriptive-only fields in this module (``klt socket-check`` never checks
#: them against drawn geometry -- there is no mechanical way to derive
#: signal direction from geometry alone); :mod:`klayout_tools.lef_abstract`
#: (issue #438) is the first consumer that reads them, to classify a LEF
#: ``PIN`` it emits without guessing when the descriptor already states it.
_VALID_PIN_DIRECTIONS = frozenset({"INPUT", "OUTPUT", "INOUT", "FEEDTHRU"})
_VALID_PIN_USES = frozenset({"SIGNAL", "ANALOG", "POWER", "GROUND", "CLOCK"})


class SocketCheckError(Exception):
    """Raised when a layout cannot be checked against a socket descriptor:
    bad layout file, missing/unreadable/malformed descriptor file, or a
    descriptor that fails validation against the documented shape.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback.
    """


def run_socket_check(
    layout_path: str,
    descriptor_path: str,
    top: str | None = None,
    *,
    require_power_ports: bool = False,
    edge_band_um: float | None = None,
) -> dict[str, Any]:
    """Check the layout at ``layout_path`` against the socket descriptor at
    ``descriptor_path``.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/socket-check.md``)::

        {
            "schema_version": 1,
            "file": <layout_path as provided>,
            "socket": <descriptor_path as provided>,
            "socket_name": <descriptor's own "name", or None>,
            "dbu_um": <database unit in micrometres, float>,
            "status": "pass" | "fail",
            "check_count": <int>,
            "checks": [
                {
                    "name": str,
                    "status": "pass" | "fail" | "skipped",
                    "violation_count": <int>,
                    "violations": [...],
                    "skip_reason": str | None,
                },
                ...
            ],
            "budgets": [
                {"name": str, "value": float, "unit": str, "notes": str | None,
                 "status": "declared_unverified"},
                ...
            ],
            "power_ports": {...},   # issue #2839, see below
        }

    ``schema_version`` is versioned independently per command (see
    ``docs/json-contract.md``); it starts at ``1``.

    ``status`` is ``"fail"`` iff any check's own ``status`` is ``"fail"`` -- a
    ``"skipped"`` check (the descriptor declared no pins, or no reserved
    layers) never causes an overall failure, only a documented gap in
    coverage (mirroring ``klt precheck``'s skip convention). ``budgets``
    never affects ``status`` at all -- it is purely a declared, unverified
    report (see this module's docstring).

    ``checks`` always has one entry per name in :data:`CHECK_NAMES`, in that
    order:

    - ``pins`` -- every declared pin has a text label, on its declared layer,
      at its declared position (within ``tolerance_um``), somewhere in the
      layout. Skipped when the descriptor declares no pins.
    - ``outline`` -- every drawn shape (polygon/box/path; text labels are not
      considered "drawn geometry" for this check) across every layer and top
      cell falls within the descriptor's ``outline`` bounding box. Always
      runs (``outline`` is a required descriptor field).
    - ``reserved_layers`` -- no shape is drawn on a ``(layer, datatype)`` pair
      listed in the descriptor's ``reserved_layers``. Skipped when the
      descriptor declares no reserved layers.
    - ``power_ports`` (issue #2839) -- every supply pin name (a pin whose
      ``use`` is ``POWER``/``GROUND``, declared or by
      ``klt lef-abstract``'s name heuristic) has at least one
      **edge-accessible** port, and at least one ``POWER`` and one
      ``GROUND`` pin is declared. Skipped unless requested -- by the
      descriptor's own ``power_ports`` object or ``require_power_ports`` --
      so no existing descriptor changes verdict. See
      :func:`grade_power_ports` and ``docs/cli/socket-check.md``.

    ``power_ports`` (the top-level block, issue #2839) is always present:
    the parameters in force plus one **fact** record per supply pin (port
    geometry, distance to each outline edge, approach-channel clearance per
    edge), computed whether or not the check was requested, so a consumer
    (``klt signoff``'s T1 item 11) can re-grade the same facts under its own
    parameters. ``edge_band_um`` overrides the descriptor's own band.

    ``top`` (issue #554) restricts every check to one named top cell instead
    of every top cell in the stream (today's default, unchanged when
    omitted): ``pins``/``outline`` scope by filtering the ``top_cells`` list
    each already flattens per-cell (already hierarchy-inclusive via
    ``begin_shapes_rec``); ``reserved_layers`` additionally restricts its
    own whole-stream ``Layout.each_cell()`` shape-count walk to that cell's
    own hierarchy (see :func:`klayout_tools._layout.cells_in_hierarchy`) --
    it would otherwise keep reporting reserved-layer usage from *every* top
    cell's hierarchy regardless of ``--top``. A named cell absent from the
    stream is a :class:`SocketCheckError`, matching ``klt ring-check --top``.

    Raises :class:`SocketCheckError` if the layout file is missing/
    unreadable, the descriptor file is missing/unreadable/not valid JSON,
    ``top`` names a cell absent from the layout, or the descriptor fails the
    documented shape validation (see :func:`_load_descriptor`).
    """
    layout = load_layout(layout_path, SocketCheckError)
    top_cells = _select_top_cells(layout, top, SocketCheckError)

    scope_cell_indices: set[int] | None = None
    if top is not None:
        # `_select_top_cells` above already validated `top` and returned
        # exactly one cell.
        scope_cell_indices = {
            cell.cell_index() for cell in cells_in_hierarchy(layout, top_cells[0])
        }

    descriptor = _load_descriptor(descriptor_path)
    if edge_band_um is not None and (not _is_number(edge_band_um) or edge_band_um < 0):
        raise SocketCheckError("edge_band_um must be a non-negative number")

    power_ports_block, power_ports_check = _power_ports(
        layout,
        top_cells,
        descriptor,
        requested=require_power_ports or descriptor["power_ports"] is not None,
        edge_band_um=edge_band_um,
    )

    checks = [
        _check_pins(layout, top_cells, descriptor["pins"]),
        _check_outline(layout, top_cells, descriptor["outline"]),
        _check_reserved_layers(
            layout, descriptor["reserved_layers"], scope_cell_indices
        ),
        power_ports_check,
    ]

    overall_status = "fail" if any(c["status"] == "fail" for c in checks) else "pass"

    return {
        "schema_version": 1,
        "file": layout_path,
        "socket": descriptor_path,
        "socket_name": descriptor["name"],
        "dbu_um": layout.dbu,
        "status": overall_status,
        "check_count": len(checks),
        "checks": checks,
        "budgets": _budgets_report(descriptor["budgets"]),
        "power_ports": power_ports_block,
    }


def _is_number(value: Any) -> bool:
    """True for a JSON number (int/float), excluding ``bool`` (a Python int)."""
    return not isinstance(value, bool) and isinstance(value, (int, float))


def _is_layer_pair(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(v, int) and not isinstance(v, bool) for v in value)
    )


# --------------------------------------------------------------------------- #
# descriptor loading / validation
# --------------------------------------------------------------------------- #


def _load_descriptor(path: str) -> dict[str, Any]:
    """Read, parse, and validate a socket descriptor file.

    Validates against the shape documented in
    ``docs/schemas/socket.schema.json`` by hand (mirroring
    ``draw.py``/``gen.py``'s request-validation convention, not a
    ``jsonschema`` runtime dependency -- the schema file itself is validated
    against real descriptor fixtures in ``tests/test_socket_check.py``
    instead, matching this repo's existing request/response modules).

    Returns a dict with every field normalised to its declared default
    (``pins``/``reserved_layers``/``budgets`` always lists, ``name`` always
    present even if ``None``).

    Raises :class:`SocketCheckError` for a missing/unreadable file, invalid
    JSON, a non-object top level, or any field that fails validation.
    """
    if not os.path.exists(path):
        raise SocketCheckError(f"socket descriptor not found: {path}")
    if os.path.isdir(path):
        raise SocketCheckError(f"socket descriptor is not a file: {path}")

    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise SocketCheckError(
            f"could not read socket descriptor '{path}': {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SocketCheckError(
            f"socket descriptor '{path}' is not valid JSON: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise SocketCheckError("socket descriptor must decode to a JSON object")

    schema_id = data.get("schema")
    if schema_id is not None and schema_id != SOCKET_SCHEMA:
        raise SocketCheckError(
            f"socket descriptor 'schema' must be {SOCKET_SCHEMA!r} if present, "
            f"got {schema_id!r}"
        )

    outline = _validate_outline(data.get("outline"))
    pins = _validate_pins(data.get("pins") or [])
    reserved_layers = _validate_reserved_layers(data.get("reserved_layers") or [])
    budgets = _validate_budgets(data.get("budgets") or [])

    power_ports = _validate_power_ports(data.get("power_ports"))

    name = data.get("name")
    if name is not None and not isinstance(name, str):
        raise SocketCheckError("socket descriptor 'name' must be a string or null")

    return {
        "name": name,
        "outline": outline,
        "pins": pins,
        "reserved_layers": reserved_layers,
        "budgets": budgets,
        "power_ports": power_ports,
    }


def _validate_outline(outline: Any) -> dict[str, float]:
    if not isinstance(outline, dict):
        raise SocketCheckError(
            "socket descriptor 'outline' is required and must be an object with "
            "x0/y0/x1/y1 (micrometres)"
        )
    for key in ("x0", "y0", "x1", "y1"):
        if not _is_number(outline.get(key)):
            raise SocketCheckError(
                f"socket descriptor 'outline.{key}' is required and must be a number"
            )
    if outline["x1"] <= outline["x0"] or outline["y1"] <= outline["y0"]:
        raise SocketCheckError(
            "socket descriptor 'outline' must satisfy x1 > x0 and y1 > y0"
        )
    return {
        "x0": float(outline["x0"]),
        "y0": float(outline["y0"]),
        "x1": float(outline["x1"]),
        "y1": float(outline["y1"]),
    }


def _validate_pins(pins: Any) -> list[dict[str, Any]]:
    if not isinstance(pins, list):
        raise SocketCheckError("socket descriptor 'pins' must be a list")

    validated: list[dict[str, Any]] = []
    for i, pin in enumerate(pins):
        if not isinstance(pin, dict):
            raise SocketCheckError(f"socket descriptor 'pins[{i}]' must be an object")
        name = pin.get("name")
        if not isinstance(name, str) or not name:
            raise SocketCheckError(
                f"socket descriptor 'pins[{i}].name' is required and must be a "
                "non-empty string"
            )
        layer = pin.get("layer")
        if not _is_layer_pair(layer):
            raise SocketCheckError(
                f"socket descriptor 'pins[{i}].layer' must be a [layer, datatype] "
                "pair of integers"
            )
        for key in ("x", "y"):
            if not _is_number(pin.get(key)):
                raise SocketCheckError(
                    f"socket descriptor 'pins[{i}].{key}' is required and must be "
                    "a number"
                )
        tolerance_um = pin.get("tolerance_um")
        if tolerance_um is not None:
            if not _is_number(tolerance_um) or tolerance_um < 0:
                raise SocketCheckError(
                    f"socket descriptor 'pins[{i}].tolerance_um' must be a "
                    "non-negative number"
                )
        for key in ("width_um", "height_um"):
            value = pin.get(key)
            if value is not None and not _is_number(value):
                raise SocketCheckError(
                    f"socket descriptor 'pins[{i}].{key}' must be a number"
                )

        direction = pin.get("direction")
        if direction is not None and direction not in _VALID_PIN_DIRECTIONS:
            raise SocketCheckError(
                f"socket descriptor 'pins[{i}].direction' must be one of: "
                + ", ".join(sorted(_VALID_PIN_DIRECTIONS))
            )
        use = pin.get("use")
        if use is not None and use not in _VALID_PIN_USES:
            raise SocketCheckError(
                f"socket descriptor 'pins[{i}].use' must be one of: "
                + ", ".join(sorted(_VALID_PIN_USES))
            )
        metal_layer = _validate_metal_layer(pin.get("metal_layer"), i)

        validated.append(
            {
                "name": name,
                "layer": (layer[0], layer[1]),
                "x": float(pin["x"]),
                "y": float(pin["y"]),
                "tolerance_um": float(tolerance_um)
                if tolerance_um is not None
                else 0.0,
                "width_um": pin.get("width_um"),
                "height_um": pin.get("height_um"),
                "direction": direction,
                "use": use,
                "metal_layer": metal_layer,
            }
        )
    return validated


def _validate_metal_layer(metal_layer: Any, index: int) -> tuple[int, int] | None:
    """A pin's optional ``metal_layer`` (issue #2839), as a tuple."""
    if metal_layer is None:
        return None
    if not _is_layer_pair(metal_layer):
        raise SocketCheckError(
            f"socket descriptor 'pins[{index}].metal_layer' must be a "
            "[layer, datatype] pair of integers or null"
        )
    return (metal_layer[0], metal_layer[1])


def _validate_power_ports(power_ports: Any) -> dict[str, Any] | None:
    """Validate the descriptor's optional ``power_ports`` object (issue
    #2839). ``None`` when absent -- the ``power_ports`` check then stays
    skipped unless the caller requests it."""
    if power_ports is None:
        return None
    if not isinstance(power_ports, dict):
        raise SocketCheckError("socket descriptor 'power_ports' must be an object")
    unknown = set(power_ports) - {"edge_band_um", "edges", "allowed_layers"}
    if unknown:
        raise SocketCheckError(
            "socket descriptor 'power_ports' has unknown field(s): "
            + ", ".join(sorted(unknown))
        )
    band = power_ports.get("edge_band_um")
    if band is not None and (not _is_number(band) or band < 0):
        raise SocketCheckError(
            "socket descriptor 'power_ports.edge_band_um' must be a non-negative number"
        )
    edges = power_ports.get("edges")
    if edges is not None and (
        not isinstance(edges, list)
        or not edges
        or any(edge not in POWER_PORT_EDGES for edge in edges)
    ):
        raise SocketCheckError(
            "socket descriptor 'power_ports.edges' must be a non-empty list of: "
            + ", ".join(POWER_PORT_EDGES)
        )
    allowed = power_ports.get("allowed_layers")
    if allowed is not None and (
        not isinstance(allowed, list) or not all(_is_layer_pair(v) for v in allowed)
    ):
        raise SocketCheckError(
            "socket descriptor 'power_ports.allowed_layers' must be a list of "
            "[layer, datatype] pairs of integers or null"
        )
    return {
        "edge_band_um": float(band) if band is not None else None,
        "edges": list(edges) if edges is not None else None,
        "allowed_layers": (
            [(v[0], v[1]) for v in allowed] if allowed is not None else None
        ),
    }


def _validate_reserved_layers(reserved_layers: Any) -> list[tuple[int, int]]:
    if not isinstance(reserved_layers, list):
        raise SocketCheckError("socket descriptor 'reserved_layers' must be a list")

    validated: list[tuple[int, int]] = []
    for i, entry in enumerate(reserved_layers):
        if not _is_layer_pair(entry):
            raise SocketCheckError(
                f"socket descriptor 'reserved_layers[{i}]' must be a "
                "[layer, datatype] pair of integers"
            )
        validated.append((entry[0], entry[1]))
    return validated


def _validate_budgets(budgets: Any) -> list[dict[str, Any]]:
    if not isinstance(budgets, list):
        raise SocketCheckError("socket descriptor 'budgets' must be a list")

    validated: list[dict[str, Any]] = []
    for i, budget in enumerate(budgets):
        if not isinstance(budget, dict):
            raise SocketCheckError(
                f"socket descriptor 'budgets[{i}]' must be an object"
            )
        name = budget.get("name")
        if not isinstance(name, str) or not name:
            raise SocketCheckError(
                f"socket descriptor 'budgets[{i}].name' is required and must be "
                "a non-empty string"
            )
        if not _is_number(budget.get("value")):
            raise SocketCheckError(
                f"socket descriptor 'budgets[{i}].value' is required and must "
                "be a number"
            )
        unit = budget.get("unit")
        if not isinstance(unit, str) or not unit:
            raise SocketCheckError(
                f"socket descriptor 'budgets[{i}].unit' is required and must be "
                "a non-empty string"
            )
        notes = budget.get("notes")
        if notes is not None and not isinstance(notes, str):
            raise SocketCheckError(
                f"socket descriptor 'budgets[{i}].notes' must be a string or null"
            )
        validated.append(
            {
                "name": name,
                "value": float(budget["value"]),
                "unit": unit,
                "notes": notes,
            }
        )
    return validated


def _budgets_report(budgets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": budget["name"],
            "value": budget["value"],
            "unit": budget["unit"],
            "notes": budget["notes"],
            "status": "declared_unverified",
        }
        for budget in budgets
    ]


# --------------------------------------------------------------------------- #
# pins
# --------------------------------------------------------------------------- #


def _collect_texts(layout: Any, top_cells: list[Any]) -> list[dict[str, Any]]:
    """Return every text label in the layout as a flat list of dicts:
    ``{"text", "layer" (tuple), "x_dbu", "y_dbu", "cell"}``, across every
    layer and top cell (instance placement transforms applied, matching
    ``precheck.py``'s ``pin_labels_over_drawing`` convention)."""
    import klayout.db as kdb

    entries: list[dict[str, Any]] = []
    for layer_index in layout.layer_indexes():
        info = layout.get_info(layer_index)
        layer_tuple = (info.layer, info.datatype)
        for cell in top_cells:
            texts = kdb.Texts(cell.begin_shapes_rec(layer_index))
            if texts.is_empty():
                continue
            for text in texts.each():
                bbox = text.bbox()
                entries.append(
                    {
                        "text": text.string,
                        "layer": layer_tuple,
                        "x_dbu": bbox.left,
                        "y_dbu": bbox.bottom,
                        "cell": cell.name,
                    }
                )
    return entries


def _pin_expected(pin: dict[str, Any]) -> dict[str, Any]:
    return {
        "layer": _fmt_layer(pin["layer"]),
        "x_um": pin["x"],
        "y_um": pin["y"],
        "tolerance_um": pin["tolerance_um"],
    }


def _text_actual(entry: dict[str, Any], dbu_um: float) -> dict[str, Any]:
    return {
        "layer": _fmt_layer(entry["layer"]),
        "x_um": entry["x_dbu"] * dbu_um,
        "y_um": entry["y_dbu"] * dbu_um,
        "cell": entry["cell"],
    }


def _texts_by_name(
    layout: Any, top_cells: list[Any]
) -> dict[str, list[dict[str, Any]]]:
    texts_by_name: dict[str, list[dict[str, Any]]] = {}
    for entry in _collect_texts(layout, top_cells):
        texts_by_name.setdefault(entry["text"], []).append(entry)
    return texts_by_name


def _pin_violation(
    pin: dict[str, Any],
    texts_by_name: dict[str, list[dict[str, Any]]],
    dbu_um: float,
) -> dict[str, Any] | None:
    """The ``pins`` check's violation for one declared pin, or ``None`` when
    its label is present on its declared layer within tolerance. Shared by
    :func:`_check_pins` and the ``power_ports`` facts (issue #2839), so the
    two never disagree about whether a pin's label was found."""
    matches = texts_by_name.get(pin["name"], [])
    if not matches:
        return {
            "pin": pin["name"],
            "reason": "missing",
            "expected": _pin_expected(pin),
            "actual": [],
        }

    on_layer = [m for m in matches if m["layer"] == pin["layer"]]
    if not on_layer:
        actual = sorted(
            (_text_actual(m, dbu_um) for m in matches),
            key=lambda a: (a["cell"], a["layer"], a["x_um"], a["y_um"]),
        )
        return {
            "pin": pin["name"],
            "reason": "wrong_layer",
            "expected": _pin_expected(pin),
            "actual": actual,
        }

    expected_x_dbu = round(pin["x"] / dbu_um)
    expected_y_dbu = round(pin["y"] / dbu_um)
    tolerance_dbu = round(pin["tolerance_um"] / dbu_um)
    within_tolerance = [
        m
        for m in on_layer
        if abs(m["x_dbu"] - expected_x_dbu) <= tolerance_dbu
        and abs(m["y_dbu"] - expected_y_dbu) <= tolerance_dbu
    ]
    if not within_tolerance:
        actual = sorted(
            (_text_actual(m, dbu_um) for m in on_layer),
            key=lambda a: (a["cell"], a["layer"], a["x_um"], a["y_um"]),
        )
        return {
            "pin": pin["name"],
            "reason": "misplaced",
            "expected": _pin_expected(pin),
            "actual": actual,
        }
    return None


def _check_pins(
    layout: Any, top_cells: list[Any], pins: list[dict[str, Any]]
) -> dict[str, Any]:
    if not pins:
        return _skipped("pins", "socket descriptor declares no pins")

    texts_by_name = _texts_by_name(layout, top_cells)
    dbu_um = layout.dbu
    violations: list[dict[str, Any]] = []
    for pin in pins:
        violation = _pin_violation(pin, texts_by_name, dbu_um)
        if violation is not None:
            violations.append(violation)

    violations.sort(key=lambda v: v["pin"])
    return _finish("pins", violations)


# --------------------------------------------------------------------------- #
# outline
# --------------------------------------------------------------------------- #


def _check_outline(
    layout: Any, top_cells: list[Any], outline: dict[str, float]
) -> dict[str, Any]:
    dbu_um = layout.dbu
    x0_dbu = round(outline["x0"] / dbu_um)
    y0_dbu = round(outline["y0"] / dbu_um)
    x1_dbu = round(outline["x1"] / dbu_um)
    y1_dbu = round(outline["y1"] / dbu_um)

    violations: list[dict[str, Any]] = []
    for layer_index in layout.layer_indexes():
        info = layout.get_info(layer_index)
        layer_label = _fmt_layer((info.layer, info.datatype))
        for cell in top_cells:
            iterator = cell.begin_shapes_rec(layer_index)
            while not iterator.at_end():
                shape = iterator.shape()
                polygon = shape.polygon
                if polygon is not None:
                    polygon = polygon.transformed(iterator.trans())
                    bbox = polygon.bbox()
                    if (
                        bbox.left < x0_dbu
                        or bbox.bottom < y0_dbu
                        or bbox.right > x1_dbu
                        or bbox.top > y1_dbu
                    ):
                        violations.append(
                            {
                                "cell": iterator.cell().name,
                                "layer": layer_label,
                                "bbox_um": {
                                    "x0": bbox.left * dbu_um,
                                    "y0": bbox.bottom * dbu_um,
                                    "x1": bbox.right * dbu_um,
                                    "y1": bbox.top * dbu_um,
                                },
                            }
                        )
                iterator.next()

    violations.sort(
        key=lambda v: (
            v["cell"],
            v["layer"],
            v["bbox_um"]["x0"],
            v["bbox_um"]["y0"],
        )
    )
    return _finish("outline", violations)


# --------------------------------------------------------------------------- #
# reserved_layers
# --------------------------------------------------------------------------- #


def _check_reserved_layers(
    layout: Any,
    reserved_layers: list[tuple[int, int]],
    scope_cell_indices: set[int] | None = None,
) -> dict[str, Any]:
    if not reserved_layers:
        return _skipped(
            "reserved_layers", "socket descriptor declares no reserved layers"
        )

    reserved = set(reserved_layers)
    cells = (
        list(layout.each_cell())
        if scope_cell_indices is None
        else [layout.cell(i) for i in scope_cell_indices]
    )
    violations: list[dict[str, Any]] = []
    for layer_index in layout.layer_indexes():
        info = layout.get_info(layer_index)
        layer_tuple = (info.layer, info.datatype)
        if layer_tuple not in reserved:
            continue
        shape_count = sum(cell.shapes(layer_index).size() for cell in cells)
        if shape_count == 0:
            continue
        violations.append(
            {
                "layer": _fmt_layer(layer_tuple),
                "name": info.name if info.name else None,
                "shapes": shape_count,
            }
        )

    violations.sort(key=lambda v: v["layer"])
    return _finish("reserved_layers", violations)


# --------------------------------------------------------------------------- #
# power_ports (issue #2839)
# --------------------------------------------------------------------------- #

#: Per-port failure reasons, in the order a port progresses through them --
#: :func:`assess_power_port` reports the first that applies, and
#: :func:`grade_power_ports` reports, per supply name, the reason of the
#: port that got *furthest* (the most actionable one to fix).
POWER_PORT_REASONS: tuple[str, ...] = (
    "missing_label",
    "no_drawn_geometry",
    "interior_only",
    "channel_blocked",
    "layer_disallowed",
)


def _supply_use(pin: dict[str, Any]) -> tuple[str, str]:
    """``(use, use_source)`` for a declared pin: its declared ``use`` when
    given, else ``klt lef-abstract``'s own name heuristic -- the same
    classification the LEF ``PIN USE`` is emitted with, so the GDS-side and
    LEF-side views of "which pins are supplies" can never disagree."""
    if pin.get("use") is not None:
        return pin["use"], "declared"
    return classify_pin_by_name(pin["name"])[1], "heuristic"


def _power_port_parameters(
    descriptor: dict[str, Any], edge_band_um: float | None
) -> dict[str, Any]:
    declared = descriptor["power_ports"] or {}
    if edge_band_um is not None:
        band = float(edge_band_um)
    elif declared.get("edge_band_um") is not None:
        band = declared["edge_band_um"]
    else:
        band = DEFAULT_POWER_PORT_EDGE_BAND_UM
    allowed = declared.get("allowed_layers")
    return {
        "edge_band_um": band,
        "edges": list(declared.get("edges") or POWER_PORT_EDGES),
        "allowed_layers": (
            [_fmt_layer(pair) for pair in allowed] if allowed is not None else None
        ),
        # The amendment these parameters implement is unratified
        # (2AMLogic/product#134) -- the defaults are a provisional choice.
        "provisional": True,
    }


def _port_facts(
    layout: Any,
    top_cells: list[Any],
    pin: dict[str, Any],
    outline_dbu: tuple[int, int, int, int],
    texts_by_name: dict[str, list[dict[str, Any]]],
    region_cache: dict[tuple[int, int], Any],
) -> dict[str, Any]:
    """Measure one supply pin's port geometry against the outline. Pure
    facts -- no parameter is applied here, so a consumer can re-grade them
    under its own edge band/edges/layers (:func:`grade_power_ports`)."""
    import klayout.db as kdb

    dbu_um = layout.dbu
    use, use_source = _supply_use(pin)
    metal = pin["metal_layer"] or pin["layer"]
    label_violation = _pin_violation(pin, texts_by_name, dbu_um)

    facts: dict[str, Any] = {
        "name": pin["name"],
        "use": use,
        "use_source": use_source,
        "layer": _fmt_layer(pin["layer"]),
        "metal_layer": _fmt_layer(metal),
        "x_um": pin["x"],
        "y_um": pin["y"],
        "label_found": label_violation is None,
        "label_reason": None if label_violation is None else label_violation["reason"],
        "geometry_source": "none",
        "bbox_um": None,
        "edge_distance_um": None,
        "channel_clear": None,
    }

    if metal not in region_cache:
        region = kdb.Region()
        layer_index = layout.find_layer(metal[0], metal[1])
        if layer_index is not None:
            for cell in top_cells:
                region.insert(kdb.Region(cell.begin_shapes_rec(layer_index)))
        region_cache[metal] = region.merged()
    merged = region_cache[metal]

    x_dbu = round(pin["x"] / dbu_um)
    y_dbu = round(pin["y"] / dbu_um)
    probe = kdb.Region(kdb.Box(x_dbu - 1, y_dbu - 1, x_dbu + 1, y_dbu + 1))
    port = merged.interacting(probe)
    if port.is_empty():
        return facts

    others = merged.not_interacting(probe)
    bbox = port.bbox()
    x0, y0, x1, y1 = outline_dbu
    distances_dbu = {
        "left": bbox.left - x0,
        "bottom": bbox.bottom - y0,
        "right": x1 - bbox.right,
        "top": y1 - bbox.top,
    }
    corridors = {
        "left": kdb.Box(x0, bbox.bottom, bbox.left, bbox.top),
        "bottom": kdb.Box(bbox.left, y0, bbox.right, bbox.bottom),
        "right": kdb.Box(bbox.right, bbox.bottom, x1, bbox.top),
        "top": kdb.Box(bbox.left, bbox.top, bbox.right, y1),
    }
    channel_clear = {}
    for edge in POWER_PORT_EDGES:
        if distances_dbu[edge] <= 0:
            # Touching (or crossing) the outline: no corridor to obstruct.
            channel_clear[edge] = True
            continue
        channel_clear[edge] = (others & kdb.Region(corridors[edge])).is_empty()

    facts.update(
        {
            "geometry_source": "drawn",
            "bbox_um": {
                "x0": round(bbox.left * dbu_um, 6),
                "y0": round(bbox.bottom * dbu_um, 6),
                "x1": round(bbox.right * dbu_um, 6),
                "y1": round(bbox.top * dbu_um, 6),
            },
            "edge_distance_um": {
                edge: round(distances_dbu[edge] * dbu_um, 6)
                for edge in POWER_PORT_EDGES
            },
            "channel_clear": channel_clear,
        }
    )
    return facts


def assess_power_port(
    port: dict[str, Any],
    *,
    edge_band_um: float,
    edges: list[str] | tuple[str, ...] = POWER_PORT_EDGES,
    allowed_layers: list[str] | None = None,
) -> tuple[str | None, list[str]]:
    """Grade one ``power_ports.ports[]`` fact record (issue #2839) --
    ``(None, accessible_edges)`` when it is an edge-accessible port, else
    ``(<reason>, [])`` with the first :data:`POWER_PORT_REASONS` entry that
    applies.

    A port is edge-accessible when its label was found, drawn conductor on
    its metal layer sits under the label, and on at least one counted
    ``edges`` side that conductor is within ``edge_band_um`` of the outline
    with a clear approach channel (no other conductor on the same layer
    between the port and the edge). ``allowed_layers`` (``"layer/datatype"``
    strings, matched against the port's ``metal_layer`` or label ``layer``)
    restricts which layers count; ``None`` allows any.

    Pure: reads only the JSON fact record, so ``klt signoff`` grades a cited
    envelope with exactly this function under its own parameters.
    """
    if not port.get("label_found"):
        return "missing_label", []
    if port.get("geometry_source") != "drawn":
        return "no_drawn_geometry", []
    distances = port.get("edge_distance_um") or {}
    clear = port.get("channel_clear") or {}
    in_band = [
        edge
        for edge in edges
        if isinstance(distances.get(edge), (int, float))
        and distances[edge] <= edge_band_um + 1e-9
    ]
    if not in_band:
        return "interior_only", []
    accessible = [edge for edge in in_band if clear.get(edge) is True]
    if not accessible:
        return "channel_blocked", []
    if allowed_layers is not None and not (
        port.get("metal_layer") in allowed_layers or port.get("layer") in allowed_layers
    ):
        return "layer_disallowed", []
    return None, accessible


def grade_power_ports(
    ports: list[dict[str, Any]],
    *,
    edge_band_um: float,
    edges: list[str] | tuple[str, ...] = POWER_PORT_EDGES,
    allowed_layers: list[str] | None = None,
    required_names: list[str] | None = None,
    require_power_and_ground: bool = True,
) -> list[dict[str, Any]]:
    """Grade a ``power_ports.ports[]`` fact list (issue #2839) and return
    one violation per supply name with no edge-accessible port -- ``[]``
    when every one has at least one.

    Each violation is ``{"pin": <name> | None, "reason": <str>, "use": <str>
    | None}``. ``reason`` is a :data:`POWER_PORT_REASONS` entry (the reason
    of that name's port that got furthest), or ``"missing"`` for a
    ``required_names`` entry no supply port carries at all, or
    ``"no_power_pin"``/``"no_ground_pin"`` (``pin: None``) when
    ``require_power_and_ground`` and no supply port has that ``use``.

    Names are compared case-insensitively. ``required_names`` (``klt
    signoff`` passes its cited ERC spec's declared supply nets) restricts
    grading to those names and requires each to be present; ``None`` grades
    every supply name in ``ports``.
    """
    supply = [p for p in ports if p.get("use") in SUPPLY_PIN_USES]
    violations = _missing_use_violations(supply) if require_power_and_ground else []

    by_name: dict[str, list[dict[str, Any]]] = {}
    for port in supply:
        by_name.setdefault(str(port.get("name", "")).upper(), []).append(port)

    if required_names is None:
        names = [(group[0]["name"], key) for key, group in by_name.items()]
    else:
        names = [(name, name.upper()) for name in required_names]

    for display, key in names:
        group = by_name.get(key)
        if not group:
            violations.append({"pin": display, "reason": "missing", "use": None})
            continue
        reason = _best_port_reason(
            group,
            edge_band_um=edge_band_um,
            edges=edges,
            allowed_layers=allowed_layers,
        )
        if reason is not None:
            violations.append(
                {"pin": display, "reason": reason, "use": group[0].get("use")}
            )
    return violations


def _missing_use_violations(supply: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``no_power_pin``/``no_ground_pin`` for a supply set lacking either."""
    uses = {port["use"] for port in supply}
    return [
        {"pin": None, "reason": reason, "use": use}
        for use, reason in (("POWER", "no_power_pin"), ("GROUND", "no_ground_pin"))
        if use not in uses
    ]


def _best_port_reason(
    group: list[dict[str, Any]],
    *,
    edge_band_um: float,
    edges: list[str] | tuple[str, ...],
    allowed_layers: list[str] | None,
) -> str | None:
    """``None`` when any port in ``group`` is edge-accessible, else the
    reason of the port that got furthest (:data:`POWER_PORT_REASONS`)."""
    reasons = []
    for port in group:
        reason, _edges = assess_power_port(
            port,
            edge_band_um=edge_band_um,
            edges=edges,
            allowed_layers=allowed_layers,
        )
        if reason is None:
            return None
        reasons.append(reason)
    return max(reasons, key=POWER_PORT_REASONS.index)


def _power_ports(
    layout: Any,
    top_cells: list[Any],
    descriptor: dict[str, Any],
    *,
    requested: bool,
    edge_band_um: float | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the top-level ``power_ports`` fact block and the
    ``power_ports`` check (issue #2839)."""
    dbu_um = layout.dbu
    outline = descriptor["outline"]
    outline_dbu = (
        round(outline["x0"] / dbu_um),
        round(outline["y0"] / dbu_um),
        round(outline["x1"] / dbu_um),
        round(outline["y1"] / dbu_um),
    )
    parameters = _power_port_parameters(descriptor, edge_band_um)

    supply_pins = [
        pin for pin in descriptor["pins"] if _supply_use(pin)[0] in SUPPLY_PIN_USES
    ]
    texts_by_name = _texts_by_name(layout, top_cells) if supply_pins else {}
    region_cache: dict[tuple[int, int], Any] = {}
    ports = [
        _port_facts(layout, top_cells, pin, outline_dbu, texts_by_name, region_cache)
        for pin in supply_pins
    ]
    ports.sort(key=lambda p: (p["name"], p["x_um"], p["y_um"]))

    block = {
        "requested": requested,
        "parameters": parameters,
        "outline_um": dict(outline),
        "ports": ports,
    }
    if not requested:
        check = _skipped(
            "power_ports",
            "power-port edge accessibility not requested (no descriptor "
            "'power_ports' object, no --require-power-ports)",
        )
    else:
        violations = grade_power_ports(
            ports,
            edge_band_um=parameters["edge_band_um"],
            edges=parameters["edges"],
            allowed_layers=parameters["allowed_layers"],
        )
        violations.sort(key=lambda v: (v["pin"] or "", v["reason"]))
        check = _finish("power_ports", violations)
    return block, check
