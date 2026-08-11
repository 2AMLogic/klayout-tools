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
from typing import Any

from ._check_result import finish as _finish
from ._check_result import fmt_layer as _fmt_layer
from ._check_result import skipped as _skipped
from ._layout import cells_in_hierarchy, load_layout
from ._layout import select_top_cells as _select_top_cells

#: Every check name this module runs, in report order.
CHECK_NAMES: tuple[str, ...] = ("pins", "outline", "reserved_layers")

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
    layout_path: str, descriptor_path: str, top: str | None = None
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

    checks = [
        _check_pins(layout, top_cells, descriptor["pins"]),
        _check_outline(layout, top_cells, descriptor["outline"]),
        _check_reserved_layers(
            layout, descriptor["reserved_layers"], scope_cell_indices
        ),
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

    name = data.get("name")
    if name is not None and not isinstance(name, str):
        raise SocketCheckError("socket descriptor 'name' must be a string or null")

    return {
        "name": name,
        "outline": outline,
        "pins": pins,
        "reserved_layers": reserved_layers,
        "budgets": budgets,
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
            }
        )
    return validated


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


def _check_pins(
    layout: Any, top_cells: list[Any], pins: list[dict[str, Any]]
) -> dict[str, Any]:
    if not pins:
        return _skipped("pins", "socket descriptor declares no pins")

    texts_by_name: dict[str, list[dict[str, Any]]] = {}
    for entry in _collect_texts(layout, top_cells):
        texts_by_name.setdefault(entry["text"], []).append(entry)

    dbu_um = layout.dbu
    violations: list[dict[str, Any]] = []

    for pin in pins:
        matches = texts_by_name.get(pin["name"], [])
        if not matches:
            violations.append(
                {
                    "pin": pin["name"],
                    "reason": "missing",
                    "expected": _pin_expected(pin),
                    "actual": [],
                }
            )
            continue

        on_layer = [m for m in matches if m["layer"] == pin["layer"]]
        if not on_layer:
            actual = sorted(
                (_text_actual(m, dbu_um) for m in matches),
                key=lambda a: (a["cell"], a["layer"], a["x_um"], a["y_um"]),
            )
            violations.append(
                {
                    "pin": pin["name"],
                    "reason": "wrong_layer",
                    "expected": _pin_expected(pin),
                    "actual": actual,
                }
            )
            continue

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
            violations.append(
                {
                    "pin": pin["name"],
                    "reason": "misplaced",
                    "expected": _pin_expected(pin),
                    "actual": actual,
                }
            )

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
