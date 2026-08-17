"""Write a GDSII/OASIS stream from a primitive shape/label description.

``klt draw`` is the deliberately-dumb write-side counterpart to the read-side
verbs (``layers``/``stats``/``cells``/``drc``/...): given a JSON description of
polygons and labels on explicit ``(layer, datatype)`` pairs, it emits a stream
verbatim. It has **no** PDK awareness and does **no** rule checking -- that is
the point. Unlike ``klt gen``, which validates generator params against the
resolved PDK's minimums and refuses out-of-range values, ``klt draw`` will
happily place a 0.15 um-wide poly bar that violates a width rule, so a DRC
flow's negative case (a known-bad fixture that *must* come back flagged) can be
produced with ``klt`` alone. See issue #230 for the motivation.

Pure library: :func:`draw` returns plain, JSON-serialisable Python data and
never prints, mirroring ``gen.py``/``render.py``. Serialisation and
human-readable formatting live in ``cli/draw_cmd.py``.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ._layout import write_layout

#: Request-envelope schema identifier (see ``docs/cli/draw.md``).
REQUEST_SCHEMA = "klt.draw.request/1"

#: Response ``schema_version`` -- versioned independently per command
#: (``docs/json-contract.md``).
SCHEMA_VERSION = 1

#: Default database unit in microns when the request does not set one.
DEFAULT_DBU_UM = 0.001

#: Default top-cell name when the request does not set one.
DEFAULT_CELL_NAME = "TOP"

#: Stamped into every response so a caller can never mistake a ``draw`` output
#: for a design-legal, rule-checked cell (the self-labelling marker option 1 of
#: issue #230 asks for, applied to the primitive verb).
NOT_DESIGN_LEGAL_WARNING = (
    "written verbatim by `klt draw`: no PDK awareness and no rule checking "
    "were applied; this cell is not guaranteed to be design-legal"
)

#: Prefix reserved for caller annotations. JSON has no comment syntax, so a
#: request file checked into a repository (the motivating case: a known-bad DRC
#: fixture whose dimensions must stay "wrong") has nowhere to record *why* it is
#: shaped the way it is. Any key beginning with this prefix -- ``_purpose``,
#: ``_expected_rules``, a per-shape ``_rule`` note -- is accepted and ignored
#: everywhere in the request, at every nesting level, and is guaranteed never to
#: be given meaning by a future version. Every *other* unrecognised key is an
#: application error (issue #950), so a typo in a real key (``rect_nm`` for
#: ``rect_um``) is caught instead of silently dropped. Same posture as
#: ``klt gen-compose``'s ``request.pdk`` (``gen_compose._ALLOWED_PDK_KEYS``).
ANNOTATION_KEY_PREFIX = "_"

#: Allowed keys per request object, enforced by :func:`_reject_unknown_keys`.
#: See ``docs/cli/draw.md`` -> "Unrecognised keys".
_ALLOWED_REQUEST_KEYS = frozenset({"schema", "params", "options"})
_ALLOWED_PARAMS_KEYS = frozenset({"dbu_um", "shapes", "labels"})
_ALLOWED_OPTIONS_KEYS = frozenset({"cell_name", "output"})
_ALLOWED_SHAPE_KEYS = frozenset({"layer", "name", "rect_um", "polygon_um", "array"})
_ALLOWED_ARRAY_KEYS = frozenset({"pitch_um", "count"})
_ALLOWED_LABEL_KEYS = frozenset({"layer", "text", "at_um"})

#: Allow-list per ``_shape_layer``/``draw`` entry kind, so shapes and labels are
#: each held to their own key set rather than a permissive union.
_ALLOWED_ENTRY_KEYS: dict[str, frozenset[str]] = {
    "shape": _ALLOWED_SHAPE_KEYS,
    "label": _ALLOWED_LABEL_KEYS,
}


def _is_annotation_key(key: Any) -> bool:
    """True for a reserved caller-annotation key (see :data:`ANNOTATION_KEY_PREFIX`)."""
    return isinstance(key, str) and key.startswith(ANNOTATION_KEY_PREFIX)


def _reject_unknown_keys(
    mapping: dict[Any, Any], allowed: frozenset[str], context: str
) -> None:
    """Raise :class:`DrawError` if ``mapping`` carries an unrecognised key.

    Keys beginning with :data:`ANNOTATION_KEY_PREFIX` are the documented escape
    hatch and are always accepted. Everything else must be in ``allowed``.
    """
    unknown = [k for k in mapping if k not in allowed and not _is_annotation_key(k)]
    if not unknown:
        return
    raise DrawError(
        f"{context} has unknown key(s): {', '.join(sorted(str(k) for k in unknown))}"
        f" -- allowed: {', '.join(sorted(allowed))}"
        f" (keys beginning with '{ANNOTATION_KEY_PREFIX}' are reserved for "
        "caller annotations and ignored)"
    )


def _is_number(value: Any) -> bool:
    """True for a JSON number (int/float), excluding ``bool`` (a Python int)."""
    return not isinstance(value, bool) and isinstance(value, (int, float))


class DrawError(Exception):
    """A ``klt draw`` request could not be satisfied.

    Raised for a malformed request (bad shape/label description, empty
    ``shapes``, an unwritable output path, ...). The CLI layer turns it into
    the documented error envelope; it is importable so callers embedding the
    library can ``except DrawError``.
    """


def load_params_arg(value: str | None) -> dict[str, Any]:
    """Resolve a CLI ``--params`` value into a ``params`` dict.

    ``value`` is either a path to an existing JSON file (files win first) or an
    inline JSON object string, mirroring ``klt gen``'s ``--params`` semantics.
    ``None`` (flag omitted) is an error here -- unlike ``gen``, ``draw`` has no
    useful default request (an empty layout is never what the caller wants).

    Raises :class:`DrawError` if the value is missing, is neither a readable
    JSON file nor valid inline JSON, or decodes to something other than an
    object. Key-level validation (including the unknown-key policy of issue
    #950) is :func:`draw`'s job, so a library caller that never goes through
    the CLI gets exactly the same checks.
    """
    if value is None:
        raise DrawError(
            "--params is required (a path to a JSON file or an inline JSON "
            'object, e.g. \'{"shapes": [{"layer": [66, 20], '
            '"rect_um": [0, 0, 1, 0.15]}]}\')'
        )

    if os.path.isfile(value):
        try:
            with open(value, encoding="utf-8") as handle:
                data = json.load(handle)
        except OSError as exc:
            raise DrawError(f"could not read params file '{value}': {exc}") from exc
        except json.JSONDecodeError as exc:
            raise DrawError(f"params file '{value}' is not valid JSON: {exc}") from exc
    else:
        try:
            data = json.loads(value)
        except json.JSONDecodeError as exc:
            raise DrawError(
                "--params must be a path to a JSON file or an inline JSON "
                f"object: {exc}"
            ) from exc

    if not isinstance(data, dict):
        raise DrawError("--params must decode to a JSON object")
    return data


def draw(request: dict[str, Any]) -> dict[str, Any]:
    """Write one primitive layout request and return the response envelope.

    ``request`` follows the ``klt.draw.request/1`` shape::

        {
            "schema": "klt.draw.request/1",
            "params": {
                "dbu_um": 0.001,          # optional, default 0.001
                "shapes": [
                    {"layer": [66, 20], "name": "poly.drawing",
                     "rect_um": [0, 0, 1, 0.15]},
                    {"layer": [65, 20], "polygon_um": [[0, 0], [1, 0], [0, 1]]},
                    {"layer": [41, 0], "rect_um": [0, 0, 0.26, 0.26],
                     "array": {"pitch_um": [0.86, 0.86], "count": [71, 71]}},
                ],
                "labels": [               # optional
                    {"layer": [66, 20], "text": "IN", "at_um": [0.5, 0.5]},
                ],
            },
            "options": {"cell_name": "TOP", "output": "out.gds"},
        }

    ``params.dbu_um`` and ``options`` are optional; ``params.shapes`` is
    required and must be non-empty. Returns a dict matching the documented
    response schema (see ``docs/cli/draw.md``). Raises :class:`DrawError` for a
    malformed request or a write failure.

    Unrecognised keys are **rejected** at every level of the request, so a typo
    in an optional key is an error rather than a silently-dropped value. Keys
    beginning with :data:`ANNOTATION_KEY_PREFIX` (``_``) are the documented
    escape hatch: accepted and ignored anywhere, so a request file can carry its
    own rationale (issue #950).
    """
    if not isinstance(request, dict):
        raise DrawError("request must be a JSON object")
    _reject_unknown_keys(request, _ALLOWED_REQUEST_KEYS, "request")

    params = request.get("params") or {}
    if not isinstance(params, dict):
        raise DrawError("request.params must be a JSON object")
    _reject_unknown_keys(params, _ALLOWED_PARAMS_KEYS, "params")

    options = request.get("options") or {}
    if not isinstance(options, dict):
        raise DrawError("request.options must be a JSON object")
    _reject_unknown_keys(options, _ALLOWED_OPTIONS_KEYS, "options")

    dbu_um = _resolve_dbu(params.get("dbu_um"))

    shapes = params.get("shapes")
    if not isinstance(shapes, list):
        raise DrawError("params.shapes is required and must be a list")
    if not shapes:
        raise DrawError(
            "params.shapes must not be empty -- `klt draw` will not write an "
            "empty layout (nothing to prove a DRC flow against)"
        )

    labels = params.get("labels") or []
    if not isinstance(labels, list):
        raise DrawError("params.labels must be a list")

    cell_name = options.get("cell_name") or DEFAULT_CELL_NAME
    if not isinstance(cell_name, str) or not cell_name:
        raise DrawError("options.cell_name must be a non-empty string")
    output_path = options.get("output") or f"{cell_name}.gds"
    if not isinstance(output_path, str) or not output_path:
        raise DrawError("options.output must be a non-empty string")

    output_dir = os.path.dirname(os.path.abspath(output_path))
    if output_dir and not os.path.isdir(output_dir):
        raise DrawError(f"output directory does not exist: {output_dir}")

    # Imported lazily so `klt --version`/arg parsing don't pay the KLayout
    # import cost -- same posture as the read-side verbs (see _layout.py).
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.dbu = dbu_um
    top = layout.create_cell(cell_name)

    # Track per-(layer, datatype) shape counts and names for the response, in
    # first-seen order, so the report reads back deterministically.
    layer_order: list[tuple[int, int]] = []
    layer_names: dict[tuple[int, int], str | None] = {}
    layer_counts: dict[tuple[int, int], int] = {}

    def _layer_index(pair: tuple[int, int], name: str | None) -> int:
        if pair not in layer_counts:
            layer_order.append(pair)
            layer_counts[pair] = 0
            layer_names[pair] = None
        # A later shape may attach a name to a previously-unnamed layer.
        if name is not None:
            layer_names[pair] = name
        idx = layout.layer(pair[0], pair[1])
        current_name = layer_names[pair]
        if current_name is not None:
            layout.set_info(idx, kdb.LayerInfo(pair[0], pair[1], current_name))
        return idx

    def _to_dbu(value_um: float) -> int:
        return int(round(value_um / dbu_um))

    for i, shape in enumerate(shapes):
        pair, name = _shape_layer(shape, i)
        idx = _layer_index(pair, name)
        geom = _build_shape_geometry(shape, i, _to_dbu, kdb)
        array_spec = _parse_array(shape, i, _to_dbu)
        target = top.shapes(idx)
        if array_spec is None:
            target.insert(geom)
            layer_counts[pair] += 1
        else:
            (pitch_x_dbu, pitch_y_dbu), (count_x, count_y) = array_spec
            for xi in range(count_x):
                for yi in range(count_y):
                    offset = kdb.Vector(xi * pitch_x_dbu, yi * pitch_y_dbu)
                    target.insert(geom.moved(offset))
            layer_counts[pair] += count_x * count_y

    for i, label in enumerate(labels):
        pair, name = _shape_layer(label, i, kind="label")
        idx = _layer_index(pair, name)
        text = _build_label(label, i, _to_dbu, kdb)
        top.shapes(idx).insert(text)

    write_layout(layout, output_path, DrawError)

    bbox = top.bbox()
    if bbox.empty():
        bbox_um = None
    else:
        bbox_um = {
            "x0": bbox.left * dbu_um,
            "y0": bbox.bottom * dbu_um,
            "x1": bbox.right * dbu_um,
            "y1": bbox.top * dbu_um,
        }

    layers_report = [
        {
            "layer": pair[0],
            "datatype": pair[1],
            "name": layer_names[pair],
            "shapes": layer_counts[pair],
        }
        for pair in layer_order
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "generator": "draw",
        "gds_path": output_path,
        "cell_name": cell_name,
        "dbu_um": dbu_um,
        "shape_count": sum(layer_counts.values()),
        "label_count": len(labels),
        "layers": layers_report,
        "bbox_um": bbox_um,
        "warnings": [NOT_DESIGN_LEGAL_WARNING],
    }


def _resolve_dbu(value: Any) -> float:
    if value is None:
        return DEFAULT_DBU_UM
    if not _is_number(value):
        raise DrawError("params.dbu_um must be a positive number")
    if value <= 0:
        raise DrawError("params.dbu_um must be a positive number")
    return float(value)


def _shape_layer(
    entry: Any, index: int, kind: str = "shape"
) -> tuple[tuple[int, int], str | None]:
    """Validate and extract ``(layer, datatype)`` and optional ``name``.

    Also enforces the entry's own key allow-list, so an unrecognised key inside
    a ``shapes[]``/``labels[]`` entry is reported by name (issue #950).
    """
    if not isinstance(entry, dict):
        raise DrawError(f"{kind}[{index}] must be a JSON object")
    _reject_unknown_keys(entry, _ALLOWED_ENTRY_KEYS[kind], f"{kind}[{index}]")

    layer = entry.get("layer")
    if (
        not isinstance(layer, list)
        or len(layer) != 2
        or any(isinstance(v, bool) or not isinstance(v, int) for v in layer)
        or any(v < 0 for v in layer)
    ):
        raise DrawError(
            f"{kind}[{index}].layer must be a [layer, datatype] pair of "
            "non-negative integers"
        )

    name = entry.get("name")
    if name is not None and not isinstance(name, str):
        raise DrawError(f"{kind}[{index}].name must be a string when present")

    return (layer[0], layer[1]), name


def _build_shape_geometry(shape: dict[str, Any], index: int, to_dbu, kdb) -> Any:
    """Build the klayout geometry object for one shape entry.

    Exactly one geometry key must be present: ``rect_um`` or ``polygon_um``.
    """
    geometry_keys = [k for k in ("rect_um", "polygon_um") if k in shape]
    if len(geometry_keys) != 1:
        raise DrawError(
            f"shape[{index}] must have exactly one geometry key (rect_um or polygon_um)"
        )

    if "rect_um" in shape:
        rect = shape["rect_um"]
        if (
            not isinstance(rect, list)
            or len(rect) != 4
            or any(not _is_number(v) for v in rect)
        ):
            raise DrawError(
                f"shape[{index}].rect_um must be [x0, y0, x1, y1] of numbers"
            )
        # Normalise so a caller-supplied x1<x0 / y1<y0 still produces a valid
        # box (documented in docs/cli/draw.md) rather than an empty one.
        x0, y0, x1, y1 = (to_dbu(v) for v in rect)
        lo_x, hi_x = sorted((x0, x1))
        lo_y, hi_y = sorted((y0, y1))
        return kdb.Box(lo_x, lo_y, hi_x, hi_y)

    points = shape["polygon_um"]
    if not isinstance(points, list) or len(points) < 3:
        raise DrawError(
            f"shape[{index}].polygon_um must be a list of at least 3 [x, y] points"
        )
    kdb_points = []
    for j, point in enumerate(points):
        if (
            not isinstance(point, list)
            or len(point) != 2
            or any(not _is_number(v) for v in point)
        ):
            raise DrawError(f"shape[{index}].polygon_um[{j}] must be an [x, y] pair")
        kdb_points.append(kdb.Point(to_dbu(point[0]), to_dbu(point[1])))
    return kdb.Polygon(kdb_points)


def _parse_array(
    shape: dict[str, Any], index: int, to_dbu
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """Validate and resolve an optional ``array`` field on a shape entry.

    ``None`` when the shape has no ``array`` key -- the caller then draws the
    unit geometry exactly once, matching pre-#553 behaviour byte for byte.
    Otherwise returns ``((pitch_x_dbu, pitch_y_dbu), (count_x, count_y))``:
    the pitch is snapped to database units via the same ``to_dbu`` rounding
    used for every other coordinate (so instance ``i`` lands at
    ``unit_geometry + i * pitch_dbu`` exactly, never accumulated float
    drift), and ``count`` is the number of instances per axis (``[1, 1]`` is
    equivalent to omitting ``array`` entirely).
    """
    array = shape.get("array")
    if array is None:
        return None
    if not isinstance(array, dict):
        raise DrawError(f"shape[{index}].array must be a JSON object when present")
    _reject_unknown_keys(array, _ALLOWED_ARRAY_KEYS, f"shape[{index}].array")

    pitch = array.get("pitch_um")
    if (
        not isinstance(pitch, list)
        or len(pitch) != 2
        or any(not _is_number(v) for v in pitch)
    ):
        raise DrawError(f"shape[{index}].array.pitch_um must be [dx, dy] of numbers")

    count = array.get("count")
    if (
        not isinstance(count, list)
        or len(count) != 2
        or any(isinstance(v, bool) or not isinstance(v, int) for v in count)
        or any(v <= 0 for v in count)
    ):
        raise DrawError(
            f"shape[{index}].array.count must be [nx, ny] of positive integers"
        )

    pitch_dbu = (to_dbu(pitch[0]), to_dbu(pitch[1]))
    return pitch_dbu, (count[0], count[1])


def _build_label(label: dict[str, Any], index: int, to_dbu, kdb) -> Any:
    text = label.get("text")
    if not isinstance(text, str) or not text:
        raise DrawError(f"label[{index}].text must be a non-empty string")

    at = label.get("at_um")
    if not isinstance(at, list) or len(at) != 2 or any(not _is_number(v) for v in at):
        raise DrawError(f"label[{index}].at_um must be an [x, y] pair of numbers")

    return kdb.Text(text, kdb.Trans(kdb.Vector(to_dbu(at[0]), to_dbu(at[1]))))
