"""Compose already-generated ``klt gen`` blocks into one placed circuit.

Pure library: :func:`compose` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints, mirroring ``gen.py``.
Serialisation and human-readable formatting live in the CLI command module
(``cli/gen_compose_cmd.py``).

This is phase 2 of Epic #191 (``klt gen compose``), the build carried by the
accepted spike, ``docs/design/gen-composition-spike.md`` -- read that
document first; its section 2 ("Proposed composition contract") settles the
request/response JSON shape this module implements, section 3 (build vs wrap)
settles that routing is built natively against ``pya.Path``/``pya.Region``
(not a runtime dependency on gdsfactory), and section 5's "Scope proposal for
a first implementing epic" settles this phase's scope: ``placement.strategy:
"row"`` plus *two-pin, point-to-point* Manhattan routing.

Scope (phase 2, this module's current state):

* **Placement**: resolve each ``blocks[]`` entry's own ``generator_report``
  (a ``klt gen`` response, given as a file path or inline object -- see
  :func:`load_generator_report_arg`), then compute each block's ``offset_um``
  per ``placement.strategy`` -- either a single horizontal row placement from
  each block's own reported ``bbox_um`` plus ``placement.spacing_um``
  (``"row"``, see :func:`compute_row_offsets`) or a caller-declared
  ``placement.origins_um`` per block id (``"explicit"``, see
  :func:`resolve_explicit_offsets`, #321) -- and write a composed GDS with
  each block's own top cell instantiated as a translated sub-cell instance
  under one new top cell. ``"explicit"`` placement supports no orientation
  (rotation) and performs no overlap validation of its own -- see
  :func:`resolve_explicit_offsets`'s docstring.
* **Routing** (new this phase): for every 2-pin ``connectivity[]`` net, draw
  a Manhattan metal path (backbone -> corner bends -> straight fill; see
  :func:`manhattan_backbone`) between the two named ports on the resolved
  ``routing.layer_role`` layer at ``routing.width_um`` width, built natively
  as a ``pya.Path``. ``nets[]`` reports ``routed`` and ``route_length_um``
  per net; a net the router cannot connect -- a required jog through an
  inter-block channel narrower than ``routing.width_um``; a route that would
  cross a guard/collector ring's own tap loop or plow through a block's
  interior (e.g. a same-facing port pair reaching a pin on a block's far
  side, or an unrelated third block in a longer row -- :func:`route_two_pin`,
  #199); or a >2-pin bundle net (bundle routing is out of scope this phase,
  spike section 5 item 2) -- is reported in ``unrouted_nets[]`` rather than
  failing the whole request or silently drawing a short. A non-empty
  ``unrouted_nets[]`` with every block placed is a *partial success* (exit
  code 3; see ``cli/gen_compose_cmd.py`` and the spike's "Proposed exit
  codes").
* **``drc_hints``** (new this phase): ``matched_groups[]`` reports every
  distinct ``matched_group_id`` seen among the input blocks' own
  ``generator_report.drc_hints.matched_group_id`` (read-only echo,
  ``placement_symmetric: null`` -- symmetry *verification* is out of scope,
  spike section 5 item 3); ``min_spacing_um`` reports the tightest spacing
  actually used across placement and routing.

``connectivity[]`` entries are validated exactly as in phase 1 (a reference
to a nonexistent block ``id``/port name is an application error, exit code
1); geometry is *advisory* -- ``klt drc`` remains the rule-compliance
authority on the composed output, so a routed net (``routed: true``) is not a
DRC-clean guarantee.

PDK resolution goes through the one resolver every other verb uses
(:func:`klayout_tools.pdk.find_pdk`) -- this module never implements its own
PDK lookup. A block's own geometry is consumed exactly as its
``generator_report`` reported it (``bbox_um``, ``ports[]``, ``cell_name``,
``gds_path``) -- this module never re-derives a block's placement math from
its GDS stream; the GDS stream is only read once, at write time, to copy each
block's already-computed geometry into the composed output.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ._layout import write_layout
from .decks import get_extraction_deck
from .gen import _PDK_ROLE_LAYERS, GenError, _pdk_family
from .pdk import PdkNotFoundError, find_pdk

#: Contract identifier for the request envelope (spike section 2).
REQUEST_SCHEMA = "klt.gen_compose.request/1"

#: Bumped only on a non-additive (breaking) change to this command's own
#: response JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: Placement strategies implemented at this phase. ``"row"`` computes a
#: single horizontal row from each block's own ``bbox_um`` plus a shared
#: ``spacing_um``; ``"explicit"`` instead takes a caller-declared per-block
#: origin (``placement.origins_um``, #321) -- see :func:`resolve_explicit_offsets`.
#: ``"grid"`` is spike-scoped for a later phase (see
#: ``docs/design/gen-composition-spike.md`` section 5).
SUPPORTED_PLACEMENT_STRATEGIES = {"row", "explicit"}

#: Unit outward vector (dx, dy) for each orthogonal ``direction_deg`` a
#: ``klt gen`` port reports. Ports only ever face an axis (0/90/180/270 --
#: see ``gen.py``'s generators), so the router never has to snap a diagonal.
_DIRECTION_VECTORS: dict[int, tuple[int, int]] = {
    0: (1, 0),
    90: (0, 1),
    180: (-1, 0),
    270: (0, -1),
}


class GenComposeError(Exception):
    """Raised when a composition request cannot be fulfilled.

    Covers an unresolvable PDK, a malformed request shape, a ``blocks[]``/
    ``connectivity[]`` reference to a nonexistent ``id``/port, an
    unsupported ``placement.strategy``, and a GDS read/write failure -- the
    CLI turns this into a clean stderr message + exit code 1, never a
    traceback (see ``docs/cli/gen-compose.md``'s exit code table).
    """


def _resolve_relative(path: str, base_dir: str) -> str:
    """Expand env vars/``~`` in ``path``; join relative paths against
    ``base_dir`` (same idiom as ``lvs.py``'s/``sim.py``'s ``_resolve_relative``)."""
    expanded = os.path.expanduser(os.path.expandvars(path))
    if os.path.isabs(expanded):
        return expanded
    return os.path.join(base_dir, expanded)


def load_generator_report_arg(
    value: Any, request_dir: str | None = None
) -> dict[str, Any]:
    """Resolve one ``blocks[].generator_report`` value into a report dict.

    ``value`` is either an inline JSON object (already a ``dict`` -- the
    request document embedded it directly) or a path to a JSON file holding
    one (a ``klt gen`` response captured to disk), mirroring
    ``klt gen --params``'s own path-or-inline duality
    (:func:`klayout_tools.gen.load_params_arg`).

    A relative path string resolves against ``request_dir`` (the directory
    holding the request document itself -- defaults to the current working
    directory when omitted, e.g. for a caller with no request file at all),
    mirroring ``klt lvs``'s ``load_request_arg``/``_resolve_relative``
    convention (``lvs.py``) rather than the process's own cwd. An absolute
    path is unaffected by ``request_dir``.

    Raises :class:`GenComposeError` if ``value`` is neither a ``dict`` nor a
    readable JSON file, or the file doesn't decode to a JSON object.
    """
    if isinstance(value, dict):
        return value

    if not isinstance(value, str) or not value:
        raise GenComposeError(
            "blocks[].generator_report must be a JSON object or a path to one"
        )

    resolved = _resolve_relative(value, request_dir or os.getcwd())
    if not os.path.isfile(resolved):
        raise GenComposeError(f"generator_report file not found: '{resolved}'")

    try:
        with open(resolved, encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise GenComposeError(
            f"could not read generator_report '{resolved}': {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise GenComposeError(
            f"generator_report '{resolved}' is not valid JSON: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise GenComposeError(
            f"generator_report '{resolved}' must decode to a JSON object"
        )
    return data


def compute_row_offsets(
    order: list[str], bboxes_um: dict[str, dict[str, float]], spacing_um: float
) -> dict[str, dict[str, float]]:
    """Compute each block's ``offset_um`` for ``placement.strategy: "row"``.

    Blocks are placed left to right in ``order``: the first block is never
    translated (``offset_um`` is always ``{"x": 0.0, "y": 0.0}``), and each
    subsequent block is translated along ``x`` only so its own reported
    ``bbox_um`` sits exactly ``spacing_um`` past the previous (already
    translated) block's right edge -- ``y`` is never translated, since a row
    only orders blocks along ``x`` (see the module docstring). This holds
    regardless of a block's own ``bbox_um.x0`` (which need not be ``0`` --
    e.g. a guard-ringed block's bbox can extend to negative coordinates), so
    the gap between adjacent *translated* bboxes is always exactly
    ``spacing_um``.

    ``bboxes_um`` maps every block ``id`` in ``order`` to its own
    (pre-translation) ``bbox_um`` dict (``x0``/``y0``/``x1``/``y1``).
    Returns a dict mapping each ``id`` in ``order`` to its ``offset_um``
    (``{"x": float, "y": float}``).
    """
    offsets: dict[str, dict[str, float]] = {}
    cursor_x1: float | None = None
    for block_id in order:
        bbox = bboxes_um[block_id]
        if cursor_x1 is None:
            offset_x = 0.0
        else:
            offset_x = (cursor_x1 + spacing_um) - bbox["x0"]
        offsets[block_id] = {"x": offset_x, "y": 0.0}
        cursor_x1 = bbox["x1"] + offset_x
    return offsets


def resolve_explicit_offsets(
    order: list[str], origins_um: dict[str, dict[str, float]]
) -> dict[str, dict[str, float]]:
    """Compute each block's ``offset_um`` for ``placement.strategy: "explicit"``
    (#321).

    Unlike :func:`compute_row_offsets`, a block's own ``bbox_um`` plays no
    role here at all -- ``origins_um[block_id]`` *is* the block's
    ``offset_um`` directly, applied by :func:`_translate_bbox` exactly the
    same way a ``"row"`` offset is (added straight to ``bbox_um``'s
    x0/y0/x1/y1). This mirrors how ``compute_row_offsets`` already treats the
    first block's ``offset_um`` as ``{0, 0}`` regardless of that block's own
    ``bbox_um.x0`` (which need not be ``0`` -- e.g. a guard-ringed block's
    bbox can extend to negative coordinates): an explicit origin translates a
    block's bbox by exactly that amount, it does not force the bbox's own
    ``(x0, y0)`` corner to land exactly on the declared origin.

    ``origins_um`` maps every block ``id`` in ``order`` to its own
    ``{"x": float, "y": float}`` origin (already validated by
    :func:`_parse_placement` -- every id in ``order`` has exactly one entry,
    no extras). Returns a dict mapping each ``id`` in ``order`` to its
    ``offset_um``.

    Orientation (rotation) is out of scope -- an explicit origin is a
    translation only, exactly like ``"row"``. Overlapping or abutting
    origins are not validated here either -- consistent with this module's
    "geometry is advisory" philosophy (see the module docstring): a
    caller-declared overlap is legal input, and ``klt drc`` remains the
    rule-compliance authority on the composed output.
    """
    return {block_id: dict(origins_um[block_id]) for block_id in order}


def _translate_bbox(
    bbox_um: dict[str, float], offset_um: dict[str, float]
) -> dict[str, float]:
    return {
        "x0": bbox_um["x0"] + offset_um["x"],
        "y0": bbox_um["y0"] + offset_um["y"],
        "x1": bbox_um["x1"] + offset_um["x"],
        "y1": bbox_um["y1"] + offset_um["y"],
    }


def _union_bbox(bboxes_um: list[dict[str, float]]) -> dict[str, float]:
    return {
        "x0": min(b["x0"] for b in bboxes_um),
        "y0": min(b["y0"] for b in bboxes_um),
        "x1": max(b["x1"] for b in bboxes_um),
        "y1": max(b["y1"] for b in bboxes_um),
    }


def _require_bbox(value: Any, where: str) -> dict[str, float]:
    if not isinstance(value, dict):
        raise GenComposeError(f"{where}.bbox_um must be a JSON object")
    try:
        return {
            "x0": float(value["x0"]),
            "y0": float(value["y0"]),
            "x1": float(value["x1"]),
            "y1": float(value["y1"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise GenComposeError(
            f"{where}.bbox_um must have numeric x0/y0/x1/y1 fields"
        ) from exc


def _parse_blocks(
    raw_blocks: Any, request_dir: str | None = None
) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_blocks, list) or not raw_blocks:
        raise GenComposeError("request.blocks must be a non-empty array")

    blocks: dict[str, dict[str, Any]] = {}
    for index, raw_block in enumerate(raw_blocks):
        if not isinstance(raw_block, dict):
            raise GenComposeError(f"request.blocks[{index}] must be a JSON object")

        block_id = raw_block.get("id")
        if not isinstance(block_id, str) or not block_id:
            raise GenComposeError(f"request.blocks[{index}].id is required")
        if block_id in blocks:
            raise GenComposeError(f"request.blocks contains duplicate id '{block_id}'")

        report = load_generator_report_arg(
            raw_block.get("generator_report"), request_dir
        )
        generator = report.get("generator")
        cell_name = report.get("cell_name")
        gds_path = report.get("gds_path")
        if not isinstance(generator, str) or not generator:
            raise GenComposeError(
                f"blocks[{index}] (id '{block_id}'): generator_report.generator "
                "is required"
            )
        if not isinstance(cell_name, str) or not cell_name:
            raise GenComposeError(
                f"blocks[{index}] (id '{block_id}'): generator_report.cell_name "
                "is required"
            )
        if not isinstance(gds_path, str) or not gds_path:
            raise GenComposeError(
                f"blocks[{index}] (id '{block_id}'): generator_report.gds_path "
                "is required"
            )
        bbox_um = _require_bbox(
            report.get("bbox_um"),
            where=f"blocks[{index}] (id '{block_id}').generator_report",
        )
        ports = report.get("ports") or []
        ports_by_name: dict[str, dict[str, Any]] = {
            p["name"]: p
            for p in ports
            if isinstance(p, dict) and isinstance(p.get("name"), str)
        }

        drc_hints = report.get("drc_hints")
        matched_group_id = None
        if isinstance(drc_hints, dict):
            candidate = drc_hints.get("matched_group_id")
            if isinstance(candidate, str) and candidate:
                matched_group_id = candidate

        blocks[block_id] = {
            "id": block_id,
            "generator": generator,
            "cell_name": cell_name,
            "gds_path": gds_path,
            "bbox_um": bbox_um,
            "port_names": set(ports_by_name),
            "ports": ports_by_name,
            "matched_group_id": matched_group_id,
        }

    return blocks


def _parse_explicit_origins(
    raw_origins: Any, order: list[str]
) -> dict[str, dict[str, float]]:
    """Parse and validate ``placement.origins_um`` for ``strategy: "explicit"``
    (#321).

    ``raw_origins`` must be a JSON object whose key set equals ``order``
    exactly (same shape of check :func:`_parse_placement` already applies to
    ``order`` vs. ``blocks[].id`` -- a missing, extra, or unknown id is an
    application error), each value a ``{"x": number, "y": number}`` pair.
    Returns a dict mapping each ``id`` in ``order`` to its parsed
    ``{"x": float, "y": float}`` origin.
    """
    if not isinstance(raw_origins, dict):
        raise GenComposeError(
            "request.placement.origins_um must be a JSON object mapping "
            "every placement.order id to a {x, y} origin when strategy is "
            "'explicit'"
        )

    order_ids = set(order)
    if set(raw_origins) != order_ids or len(raw_origins) != len(order_ids):
        raise GenComposeError(
            "request.placement.origins_um must have exactly one entry for "
            "every placement.order id (no missing or extra/unknown ids)"
        )

    origins: dict[str, dict[str, float]] = {}
    for block_id in order:
        raw_origin = raw_origins[block_id]
        if not isinstance(raw_origin, dict):
            raise GenComposeError(
                f"request.placement.origins_um['{block_id}'] must be a JSON "
                "object with numeric x/y fields"
            )
        x = raw_origin.get("x")
        y = raw_origin.get("y")
        if (
            isinstance(x, bool)
            or isinstance(y, bool)
            or not isinstance(x, (int, float))
            or not isinstance(y, (int, float))
        ):
            raise GenComposeError(
                f"request.placement.origins_um['{block_id}'] must have "
                "numeric x/y fields"
            )
        origins[block_id] = {"x": float(x), "y": float(y)}

    return origins


def _parse_placement(
    raw_placement: Any, block_ids: set[str]
) -> tuple[str, list[str], float, dict[str, dict[str, float]] | None]:
    """Parse and validate ``request.placement``.

    Returns ``(strategy, order, spacing_um, origins_um)``. ``spacing_um`` is
    ``0.0`` (unused) and ``origins_um`` is ``None`` for ``strategy: "row"``;
    conversely ``origins_um`` is a parsed dict and ``spacing_um`` is not read
    from the request at all for ``strategy: "explicit"`` (#321) --
    ``placement.spacing_um`` alongside an ``"explicit"`` strategy is simply
    ignored, not rejected.
    """
    if not isinstance(raw_placement, dict):
        raise GenComposeError("request.placement must be a JSON object")

    strategy = raw_placement.get("strategy")
    if strategy not in SUPPORTED_PLACEMENT_STRATEGIES:
        supported = ", ".join(sorted(SUPPORTED_PLACEMENT_STRATEGIES))
        raise GenComposeError(
            f"request.placement.strategy '{strategy}' is not supported at this "
            f"phase -- supported: {supported}"
        )

    order = raw_placement.get("order")
    if (
        not isinstance(order, list)
        or not order
        or not all(isinstance(o, str) for o in order)
    ):
        raise GenComposeError(
            "request.placement.order must be a non-empty array of strings"
        )

    if set(order) != block_ids or len(order) != len(block_ids):
        raise GenComposeError(
            "request.placement.order must contain every blocks[].id exactly once"
        )

    if strategy == "explicit":
        origins_um = _parse_explicit_origins(raw_placement.get("origins_um"), order)
        return strategy, order, 0.0, origins_um

    spacing_um = raw_placement.get("spacing_um", 0.0)
    if isinstance(spacing_um, bool) or not isinstance(spacing_um, (int, float)):
        raise GenComposeError("request.placement.spacing_um must be a number")
    spacing_um = float(spacing_um)
    if spacing_um < 0:
        raise GenComposeError("request.placement.spacing_um must be >= 0")

    return strategy, order, spacing_um, None


def _validate_block_port(
    blocks: dict[str, dict[str, Any]],
    block_id: str,
    port: str,
    where: str,
) -> dict[str, Any]:
    """Validate that ``block_id``/``port`` name a real block port, raising a
    :class:`GenComposeError` prefixed with ``where`` otherwise. Returns the
    resolved block dict.

    Shared by :func:`_parse_connectivity` and :func:`_parse_pins` so both
    validate a ``{block, port}`` reference identically (a nonexistent block
    ``id`` or port name is the same application error, exit code 1, regardless
    of which request field named it). A block that reported no ``ports[]`` at
    all skips the port-name check (it cannot be validated against an empty
    set) -- the same latitude the connectivity path already allowed.
    """
    block = blocks.get(block_id)
    if block is None:
        raise GenComposeError(f"{where} references unknown block id '{block_id}'")
    if block["port_names"] and port not in block["port_names"]:
        raise GenComposeError(
            f"{where} references unknown port '{port}' on block '{block_id}' -- "
            f"available: {', '.join(sorted(block['port_names']))}"
        )
    return block


def _parse_connectivity(
    raw_connectivity: Any, blocks: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    if raw_connectivity is None:
        return []
    if not isinstance(raw_connectivity, list):
        raise GenComposeError("request.connectivity must be an array")

    connectivity: list[dict[str, Any]] = []
    for index, entry in enumerate(raw_connectivity):
        if not isinstance(entry, dict):
            raise GenComposeError(
                f"request.connectivity[{index}] must be a JSON object"
            )

        net = entry.get("net")
        if not isinstance(net, str) or not net:
            raise GenComposeError(f"request.connectivity[{index}].net is required")

        pins = entry.get("pins")
        if not isinstance(pins, list) or len(pins) < 2:
            raise GenComposeError(
                f"request.connectivity[{index}] (net '{net}').pins must be an "
                "array of at least 2 {block, port} entries"
            )

        parsed_pins: list[dict[str, str]] = []
        for pin_index, pin in enumerate(pins):
            if not isinstance(pin, dict):
                raise GenComposeError(
                    f"request.connectivity[{index}] (net '{net}').pins[{pin_index}] "
                    "must be a JSON object"
                )
            block_id = pin.get("block")
            port = pin.get("port")
            if not isinstance(block_id, str) or not isinstance(port, str):
                raise GenComposeError(
                    f"request.connectivity[{index}] (net '{net}').pins[{pin_index}] "
                    "must have string 'block'/'port' fields"
                )
            _validate_block_port(
                blocks,
                block_id,
                port,
                f"request.connectivity[{index}] (net '{net}')",
            )
            parsed_pins.append({"block": block_id, "port": port})

        connectivity.append({"net": net, "pins": parsed_pins})

    return connectivity


def _parse_pins(
    raw_pins: Any,
    blocks: dict[str, dict[str, Any]],
    connectivity: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Parse and validate the ``pins[]`` request field (#210).

    Each entry -- ``{"net": <string>, "block": <string>, "port": <string>}``
    -- names *exactly one* block port (unlike ``connectivity[]``, whose
    ``pins`` is a 2+ list of ports to wire together) to promote to a labelled,
    top-level pin *without* routing any metal: the port's own already-drawn
    geometry is what gets a ``kdb.Text`` label. Validated the same way
    ``connectivity[]`` is (:func:`_validate_block_port`): an unknown
    ``block``/``port`` is an application error (exit 1).

    A ``(block, port)`` pair that also appears in any ``connectivity[]`` entry
    is rejected: that shape is already labelled by the router, so a second,
    possibly inconsistent ``pins[]`` label on it is ambiguous rather than
    additive.

    Returns a list of ``{net, block, port}`` dicts. ``None``/absent yields an
    empty list (omitting ``pins[]`` entirely must not change any behavior).
    """
    if raw_pins is None:
        return []
    if not isinstance(raw_pins, list):
        raise GenComposeError("request.pins must be an array")

    connectivity_pairs = {
        (pin["block"], pin["port"]) for entry in connectivity for pin in entry["pins"]
    }

    pins: list[dict[str, str]] = []
    for index, entry in enumerate(raw_pins):
        if not isinstance(entry, dict):
            raise GenComposeError(f"request.pins[{index}] must be a JSON object")

        net = entry.get("net")
        if not isinstance(net, str) or not net:
            raise GenComposeError(f"request.pins[{index}].net is required")

        block_id = entry.get("block")
        port = entry.get("port")
        if not isinstance(block_id, str) or not isinstance(port, str):
            raise GenComposeError(
                f"request.pins[{index}] (net '{net}') must have string "
                "'block'/'port' fields"
            )

        _validate_block_port(
            blocks, block_id, port, f"request.pins[{index}] (net '{net}')"
        )

        if (block_id, port) in connectivity_pairs:
            raise GenComposeError(
                f"request.pins[{index}] (net '{net}') names port '{port}' on block "
                f"'{block_id}', which is already labelled by a connectivity[] net "
                "-- a port may be promoted by pins[] or wired by connectivity[], "
                "not both"
            )

        pins.append({"net": net, "block": block_id, "port": port})

    return pins


def _port_has_geometry(port: Any) -> bool:
    """Whether ``port`` carries the ``{x_um, y_um, layer{layer, datatype}}``
    geometry a ``pins[]`` label needs to be placed. A block report that omits
    a port's position/layer cannot be labelled -- reported as a partial-success
    note rather than crashing (#210)."""
    if not isinstance(port, dict):
        return False
    if not isinstance(port.get("x_um"), (int, float)) or isinstance(
        port.get("x_um"), bool
    ):
        return False
    if not isinstance(port.get("y_um"), (int, float)) or isinstance(
        port.get("y_um"), bool
    ):
        return False
    layer = port.get("layer")
    return (
        isinstance(layer, dict)
        and isinstance(layer.get("layer"), int)
        and isinstance(layer.get("datatype"), int)
    )


def _resolve_route_layer(variant: str, layer_role: str) -> tuple[int, int]:
    """Resolve ``routing.layer_role`` to a ``(layer, datatype)`` pair.

    Routing goes through the *same* per-PDK-family role-layer table every
    ``klt gen`` generator already uses (:data:`klayout_tools.gen._PDK_ROLE_LAYERS`)
    -- never a raw ``{layer, datatype}`` pair from the request, and never a
    second, private layer map (spike section 2, ``routing.layer_role``).

    Raises :class:`GenComposeError` when the variant is unsupported, the role
    is not a known role, or the resolved family's curated deck has no layer
    for the role (``None`` entry).
    """
    try:
        family = _pdk_family(variant)
    except GenError as exc:
        raise GenComposeError(str(exc)) from exc

    family_roles = _PDK_ROLE_LAYERS[family]
    if layer_role not in family_roles:
        available = ", ".join(sorted(family_roles))
        raise GenComposeError(
            f"routing.layer_role '{layer_role}' is not a known layer role for "
            f"PDK family '{family}' -- available: {available}"
        )
    pair = family_roles[layer_role]
    if pair is None:
        raise GenComposeError(
            f"routing.layer_role '{layer_role}' has no layer in PDK family "
            f"'{family}''s curated deck -- cannot route on it"
        )
    return pair


def _resolve_label_layer(
    variant: str, draw_layer: tuple[int, int]
) -> tuple[int, int] | None:
    """Resolve the PDK-family net-label layer/datatype that pairs with
    ``draw_layer``, for naming a net on that drawn layer.

    Mirrors `klt extract`'s own label-recognition convention exactly --
    :class:`klayout_tools.decks.ExtractionDeck`'s ``metals[i]`` <->
    ``metal_labels[i]`` correspondence (see ``_extract_netlist()`` in
    ``extract.py``, which only promotes a net to a named ``.SUBCKT`` pin
    when a ``kdb.Text`` on ``metal_labels[i]`` touches a shape on
    ``metals[i]``), plus the ``poly`` <-> ``poly_label`` correspondence the
    deck uses to name a bare-poly gate node that has no metal landing pad
    (#210, ``l2n.connect(poly, poly_label)``). This is the *same*
    per-PDK-family :class:`~klayout_tools.decks.ExtractionDeck` every `klt
    extract` call already resolves via ``get_extraction_deck`` -- never a
    second, private label-layer table.

    ``draw_layer`` is the drawn ``(layer, datatype)`` a shape lives on: a
    ``routing.layer_role``-resolved metal for a routed ``connectivity[]`` net,
    or a ``pins[]`` port's own reported layer (which may be metal *or* poly).

    Returns ``None`` when ``draw_layer`` is neither a ``metals[]`` entry with
    a paired ``metal_labels[]`` layer nor the deck's ``poly`` layer with a
    ``poly_label`` -- the shape is still drawn, just without a net label for
    `klt extract` to promote into a pin (partial success, not an error).
    """
    family = _pdk_family(variant)
    deck = get_extraction_deck(family)
    try:
        index = deck.metals.index(draw_layer)
    except ValueError:
        index = None
    if index is not None:
        if index >= len(deck.metal_labels):
            return None
        return deck.metal_labels[index]
    if draw_layer == deck.poly:
        return deck.poly_label
    return None


def _polyline_midpoint_um(
    points: list[tuple[float, float]],
) -> tuple[float, float]:
    """A point strictly along ``points``' own drawn path, at half its total
    arc length.

    Used to place a routed net's label away from both endpoints (each
    endpoint sits at, or just inside, a block's own port -- see
    :func:`route_two_pin`'s obstacle-overlap check, which already guarantees
    the backbone stays clear of every block's interior beyond a pin's own
    small edge-approach margin). The arc-length midpoint is the point on the
    backbone farthest, in the routing sense, from either endpoint, minimising
    the chance the label lands over a neighbouring block's own metal on the
    same layer (which would misattach the label to that block's net instead
    of this one). Falls back to the first point for a degenerate
    (zero-length) route.
    """
    total = _polyline_length_um(points)
    if total <= 0.0:
        return points[0]

    target = total / 2.0
    accumulated = 0.0
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        seg_len = abs(x1 - x0) + abs(y1 - y0)
        if accumulated + seg_len >= target:
            if seg_len <= 0.0:
                return (x0, y0)
            frac = (target - accumulated) / seg_len
            return (x0 + (x1 - x0) * frac, y0 + (y1 - y0) * frac)
        accumulated += seg_len
    return points[-1]


def _cleanup_points(
    points: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Drop consecutive duplicate and collinear waypoints from a backbone.

    A raw backbone can contain zero-length hops (a stub that lands on top of
    the next waypoint) and three collinear points (a straight run split by a
    degenerate jog); both are geometrically harmless but produce a
    ``pya.Path`` with redundant vertices. Collapsing them keeps the drawn
    path -- and its reported ``route_length_um`` -- minimal and stable.
    """
    deduped: list[tuple[float, float]] = []
    for p in points:
        if not deduped or (
            abs(p[0] - deduped[-1][0]) > 1e-9 or abs(p[1] - deduped[-1][1]) > 1e-9
        ):
            deduped.append(p)

    if len(deduped) <= 2:
        return deduped

    cleaned: list[tuple[float, float]] = [deduped[0]]
    for i in range(1, len(deduped) - 1):
        prev, cur, nxt = cleaned[-1], deduped[i], deduped[i + 1]
        # Collinear (same x for both segments, or same y for both segments)?
        same_x = abs(prev[0] - cur[0]) < 1e-9 and abs(cur[0] - nxt[0]) < 1e-9
        same_y = abs(prev[1] - cur[1]) < 1e-9 and abs(cur[1] - nxt[1]) < 1e-9
        if same_x or same_y:
            continue  # cur adds nothing; skip it
        cleaned.append(cur)
    cleaned.append(deduped[-1])
    return cleaned


def _polyline_length_um(points: list[tuple[float, float]]) -> float:
    """Total length of an orthogonal polyline (sum of ``|dx| + |dy|``)."""
    total = 0.0
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        total += abs(x1 - x0) + abs(y1 - y0)
    return total


def manhattan_backbone(
    a: tuple[float, float],
    dir_a_deg: int,
    b: tuple[float, float],
    dir_b_deg: int,
    stub_um: float,
) -> list[tuple[float, float]]:
    """Generate a two-pin Manhattan backbone from port ``a`` to port ``b``.

    Reimplements gdsfactory's ``route_single`` *algorithm* (spike section 1)
    natively: leave port ``a`` along its outward ``dir_a_deg`` for a short
    ``stub_um`` stub, leave port ``b`` along ``dir_b_deg`` likewise, then
    connect the two stub ends with right-angle-only segments:

    * both ports facing along **x** (the common row-placement case): a single
      vertical jog at the midpoint x between the stub ends (degenerates to a
      straight line when the ports share a y);
    * both facing along **y**: a single horizontal jog at the midpoint y;
    * mixed (one x, one y): a single corner (an "L").

    Returns the cleaned ordered ``(x, y)`` waypoint list (um). ``pya.Path``
    renders each interior corner as a square miter that fully fills the bend,
    so no separate bend-insertion pass is needed -- the corner *is* the bend.
    """
    ax, ay = a
    bx, by = b
    va = _DIRECTION_VECTORS[dir_a_deg]
    vb = _DIRECTION_VECTORS[dir_b_deg]
    sa = (ax + va[0] * stub_um, ay + va[1] * stub_um)
    sb = (bx + vb[0] * stub_um, by + vb[1] * stub_um)

    a_horizontal = va[1] == 0  # port a faces +/-x
    b_horizontal = vb[1] == 0  # port b faces +/-x

    mid: list[tuple[float, float]] = []
    if a_horizontal and b_horizontal:
        mx = (sa[0] + sb[0]) / 2.0
        mid = [(mx, sa[1]), (mx, sb[1])]
    elif not a_horizontal and not b_horizontal:
        my = (sa[1] + sb[1]) / 2.0
        mid = [(sa[0], my), (sb[0], my)]
    else:
        # One port faces x, the other y -> a single corner joins the stubs.
        if a_horizontal:
            mid = [(sb[0], sa[1])]
        else:
            mid = [(sa[0], sb[1])]

    return _cleanup_points([a, sa, *mid, sb, b])


def _block_gap_um(
    bbox_a: dict[str, float], bbox_b: dict[str, float], axis: str
) -> float:
    """Signed gap between two placed bboxes along ``axis`` (``"x"``/``"y"``).

    Positive when the boxes are disjoint along that axis (the value is the
    channel width available between them); ``<= 0`` when they touch or overlap
    (a route jog has room regardless). ``axis`` is the axis the channel spans,
    i.e. for a *vertical* jog the relevant channel is the horizontal (``"x"``)
    gap between the two blocks.
    """
    if axis == "x":
        lo, hi = sorted((bbox_a, bbox_b), key=lambda bb: bb["x0"])
        return hi["x0"] - lo["x1"]
    lo, hi = sorted((bbox_a, bbox_b), key=lambda bb: bb["y0"])
    return hi["y0"] - lo["y1"]


#: Port-name prefixes a ``klt gen`` generator uses for a guard/collector ring's
#: own tap ports (``diff_pair``'s ``add_guard_ring``, ``bjt_array``'s
#: ``add_collector_ring``, and the standalone ``guard_ring`` generator itself
#: -- see ``gen.py``'s ``_diff_pair_describe``/``_bjt_array_describe``/
#: ``_guard_ring_describe``). A block reporting any port with one of these
#: prefixes has a ring drawn *around* its other ports -- any route touching a
#: non-tap port on that block necessarily crosses the ring's own metal loop on
#: its way in or out (#199 case 2).
_RING_TAP_PORT_PREFIXES = ("TAP_", "COLL_")


def _block_has_ring_taps(block: dict[str, Any]) -> bool:
    """Whether ``block`` reports a guard/collector ring (any tap port)."""
    return any(name.startswith(_RING_TAP_PORT_PREFIXES) for name in block["port_names"])


def _port_edge_margin_um(
    port_xy: tuple[float, float], direction_deg: int, bbox_um: dict[str, float]
) -> float:
    """Distance from a port to its own placed block's bbox edge it faces.

    A ``klt gen`` port need not sit exactly on its own block's bbox boundary
    (e.g. ``mos_array``/``diff_pair`` inset every port ~0.2um from the edge
    regardless of ``add_guard_ring``) -- this is the amount of a route's
    approach into that port that is *always* inside the block's own bbox,
    regardless of where the route comes from, and therefore not evidence of
    anything wrong. Used as the routability check's per-block allowance
    (:func:`route_two_pin`) -- an approach that crosses *more* than this
    margin through the block's interior is crossing something else inside it
    (e.g. another device's pin, or -- when the extra margin matches a ring's
    width -- the guard/collector ring, though that specific case is instead
    caught directly by :func:`_block_has_ring_taps`, since the margin here is
    identical whether or not a ring sits in that space).
    """
    x, y = port_xy
    if direction_deg == 0:
        return bbox_um["x1"] - x
    if direction_deg == 180:
        return x - bbox_um["x0"]
    if direction_deg == 90:
        return bbox_um["y1"] - y
    if direction_deg == 270:
        return y - bbox_um["y0"]
    return 0.0  # unreachable -- direction_deg is validated before this is called


def _segment_bbox_interior_overlap_um(
    p0: tuple[float, float], p1: tuple[float, float], bbox_um: dict[str, float]
) -> float:
    """Length of axis-aligned segment ``p0``->``p1`` inside ``bbox_um``'s
    *strict interior* (a segment that only touches the boundary, or lies
    fully outside, returns ``0.0``).

    Every :func:`manhattan_backbone` segment is horizontal or vertical by
    construction, so only those two cases are handled; a (unreachable)
    diagonal or zero-length segment reports no overlap rather than raising.
    """
    eps = 1e-9
    bx0, by0 = bbox_um["x0"] + eps, bbox_um["y0"] + eps
    bx1, by1 = bbox_um["x1"] - eps, bbox_um["y1"] - eps
    if bx0 >= bx1 or by0 >= by1:
        return 0.0  # degenerate (zero-area) bbox

    x0, y0 = p0
    x1, y1 = p1
    horizontal = abs(y0 - y1) < 1e-9
    vertical = abs(x0 - x1) < 1e-9
    if horizontal and not vertical:
        if not (by0 < y0 < by1):
            return 0.0
        lo, hi = sorted((x0, x1))
        return max(0.0, min(hi, bx1) - max(lo, bx0))
    if vertical and not horizontal:
        if not (bx0 < x0 < bx1):
            return 0.0
        lo, hi = sorted((y0, y1))
        return max(0.0, min(hi, by1) - max(lo, by0))
    return 0.0


def route_two_pin(
    pin_a: dict[str, Any],
    pin_b: dict[str, Any],
    blocks: dict[str, dict[str, Any]],
    offsets_um: dict[str, dict[str, float]],
    placed_bboxes_um: dict[str, dict[str, float]],
    width_um: float,
) -> dict[str, Any]:
    """Route one two-pin net and report the result.

    Resolves each pin's port position into the composed coordinate frame
    (port ``x_um``/``y_um`` translated by its block's ``offset_um``),
    generates a Manhattan backbone (:func:`manhattan_backbone`), and applies
    three routability checks before reporting success -- all diagnostic
    heuristics against the composition's own already-known geometry
    (``bbox_um``/``ports[]``), not a DRC check (``klt drc`` remains
    authoritative):

    1. **Channel-width check** (original, phase 2): when the backbone
       requires a jog *across* the channel between the two pins' blocks and
       that channel is narrower than ``width_um``, the wire cannot fit
       without overlapping a block.
    2. **Guard/collector-ring check** (#199 case 2): a block reporting a
       guard or collector ring (any ``TAP_*``/``COLL_*`` port -- see
       :func:`_block_has_ring_taps`) has that ring drawn *around* its other
       ports; a route touching one of those non-tap ports necessarily
       crosses the ring's own metal loop on its way in or out, merging the
       net with the ring's own tap net.
    3. **Obstacle-overlap check** (#199 case 1): after each port's own
       unavoidable "sit set back from my own block's edge" margin is
       excluded (:func:`_port_edge_margin_um`), the drawn backbone must not
       cross the *interior* of any block's bbox -- including the two pins'
       own blocks (a same-facing port pair forces the backbone to plow
       through the destination block's full width to reach a port on its far
       side, e.g. crossing that block's own opposite-facing pin) and any
       third block's bbox the straight-line/single-jog backbone happens to
       cross in a longer row.

    Any of the three reports the net unroutable (spike section 2,
    ``unrouted_nets[]``) rather than silently drawing a short.

    Returns ``{"routed": bool, "route_length_um": float | None,
    "points_um": list | None, "reason": str | None}``.
    """
    block_a = blocks[pin_a["block"]]
    block_b = blocks[pin_b["block"]]
    port_a = block_a["ports"].get(pin_a["port"])
    port_b = block_b["ports"].get(pin_b["port"])

    # A block with no reported ports[] (a hand-crafted generator_report) can't
    # supply a routable position -- treat as unroutable rather than crashing.
    if not isinstance(port_a, dict) or not isinstance(port_b, dict):
        return {
            "routed": False,
            "route_length_um": None,
            "points_um": None,
            "reason": "one or both ports report no position (empty ports[])",
        }

    off_a = offsets_um[pin_a["block"]]
    off_b = offsets_um[pin_b["block"]]
    a = (
        float(port_a["x_um"]) + off_a["x"],
        float(port_a["y_um"]) + off_a["y"],
    )
    b = (
        float(port_b["x_um"]) + off_b["x"],
        float(port_b["y_um"]) + off_b["y"],
    )
    dir_a = int(port_a.get("direction_deg", 0)) % 360
    dir_b = int(port_b.get("direction_deg", 0)) % 360
    if dir_a not in _DIRECTION_VECTORS or dir_b not in _DIRECTION_VECTORS:
        return {
            "routed": False,
            "route_length_um": None,
            "points_um": None,
            "reason": "a port reports a non-orthogonal direction_deg",
        }

    va = _DIRECTION_VECTORS[dir_a]
    vb = _DIRECTION_VECTORS[dir_b]
    stub_um = width_um

    # Guard/collector-ring check (#199 case 2): a block with a ring drawn
    # around it (any TAP_*/COLL_* port reported) cannot be reached at any
    # *other* port without the route crossing the ring's own metal loop --
    # on the way in for a destination pin, or on the way out for a source
    # pin, since the ring fully encloses the block either way. Only meaningful
    # for distinct blocks: a self-net's two ports already both sit inside the
    # same ring, so it draws no *additional* ring crossing.
    if pin_a["block"] != pin_b["block"]:
        for pin, block in ((pin_a, block_a), (pin_b, block_b)):
            if _block_has_ring_taps(block) and not pin["port"].startswith(
                _RING_TAP_PORT_PREFIXES
            ):
                return {
                    "routed": False,
                    "route_length_um": None,
                    "points_um": None,
                    "reason": (
                        f"block '{pin['block']}' has a guard/collector ring "
                        f"(reports a TAP_*/COLL_* port) -- a route to its "
                        f"non-tap port '{pin['port']}' would cross the ring's "
                        "own metal loop and merge this net with the ring's tap "
                        "net; route to the ring's own tap port instead, or "
                        "regenerate the block with add_guard_ring/"
                        "add_collector_ring: false"
                    ),
                }

    # Routability heuristic: a jog perpendicular to the ports' facing axis has
    # to squeeze through the channel between the two blocks. When both ports
    # face x and their y differs (a vertical jog is required), the channel is
    # the horizontal gap; symmetric for both-y. Only meaningful for distinct
    # blocks (a self-net has no inter-block channel).
    if pin_a["block"] != pin_b["block"]:
        bbox_a = placed_bboxes_um[pin_a["block"]]
        bbox_b = placed_bboxes_um[pin_b["block"]]
        if va[1] == 0 and vb[1] == 0 and abs(a[1] - b[1]) > 1e-9:
            gap = _block_gap_um(bbox_a, bbox_b, axis="x")
            if 0.0 <= gap < width_um:
                return {
                    "routed": False,
                    "route_length_um": None,
                    "points_um": None,
                    "reason": (
                        f"vertical jog needs a channel >= width {width_um}um "
                        f"but the gap between blocks is only {gap:.4g}um"
                    ),
                }
        elif va[0] == 0 and vb[0] == 0 and abs(a[0] - b[0]) > 1e-9:
            gap = _block_gap_um(bbox_a, bbox_b, axis="y")
            if 0.0 <= gap < width_um:
                return {
                    "routed": False,
                    "route_length_um": None,
                    "points_um": None,
                    "reason": (
                        f"horizontal jog needs a channel >= width {width_um}um "
                        f"but the gap between blocks is only {gap:.4g}um"
                    ),
                }

    points = manhattan_backbone(a, dir_a, b, dir_b, stub_um)

    # Obstacle-overlap check (#199 case 1): sum how much of the drawn backbone
    # lies inside each block's bbox *interior* (a boundary touch doesn't
    # count -- see _segment_bbox_interior_overlap_um), then compare each
    # block's total against how much crossing is unavoidable there. A pin's
    # own block always gets an allowance equal to that port's own edge margin
    # (_port_edge_margin_um) -- crossing exactly that much is just "the route
    # reached the pin", not a fault. Crossing *more* than that (own blocks) or
    # *any* amount (every other block) means the backbone plowed through a
    # block it shouldn't have -- e.g. a same-facing port pair forcing the
    # route through the destination's full width to reach a pin on its far
    # side, crossing that block's own other pins on the way.
    own_a, own_b = pin_a["block"], pin_b["block"]
    same_block_self_net = own_a == own_b
    allowances_um: dict[str, float] = {}
    if not same_block_self_net:
        allowances_um[own_a] = max(
            0.0, _port_edge_margin_um(a, dir_a, placed_bboxes_um[own_a])
        )
        allowances_um[own_b] = max(
            0.0, _port_edge_margin_um(b, dir_b, placed_bboxes_um[own_b])
        )

    overlap_by_block_um: dict[str, float] = {}
    for seg_p0, seg_p1 in zip(points, points[1:], strict=False):
        for other_id, other_bbox in placed_bboxes_um.items():
            if same_block_self_net and other_id == own_a:
                continue  # a self-net is expected to cross its own block
            length = _segment_bbox_interior_overlap_um(seg_p0, seg_p1, other_bbox)
            if length > 0.0:
                overlap_by_block_um[other_id] = (
                    overlap_by_block_um.get(other_id, 0.0) + length
                )

    margin_eps_um = 1e-6
    for other_id, crossed_um in overlap_by_block_um.items():
        allowed_um = allowances_um.get(other_id, 0.0)
        if crossed_um <= allowed_um + margin_eps_um:
            continue
        if other_id in (own_a, own_b):
            reason = (
                f"backbone crosses {crossed_um:.4g}um through its own pin's "
                f"block '{other_id}' -- more than that pin's own "
                f"{allowed_um:.4g}um edge margin, so the route plows through "
                "the block's interior (e.g. a same-facing port pair reaching "
                "a pin on the block's far side, crossing another pin on the "
                "way) rather than approaching the pin cleanly"
            )
        else:
            reason = (
                f"backbone crosses {crossed_um:.4g}um through unrelated "
                f"block '{other_id}''s bbox -- the route is not "
                "point-to-point between only the two connected blocks"
            )
        return {
            "routed": False,
            "route_length_um": None,
            "points_um": None,
            "reason": reason,
        }

    return {
        "routed": True,
        "route_length_um": _polyline_length_um(points),
        "points_um": points,
        "reason": None,
    }


#: Allowed keys in ``request.pdk`` (spike section 2). Any other key is an
#: application error rather than a silent fallback -- see #328: a typo such
#: as ``{"pdk": {"name": "gf180mcuD"}}`` (``name`` being what ``klt gen``'s
#: own response calls this field) would otherwise be silently treated as
#: ``request.pdk == {}`` and resolve whatever ``$PDK``/the default search
#: order picks, with no indication the request's own value was never read.
_ALLOWED_PDK_KEYS = {"variant", "root"}


def compose(request: dict[str, Any], request_dir: str | None = None) -> dict[str, Any]:
    """Run one composition request end-to-end and return the response envelope.

    ``request`` follows the ``klt.gen_compose.request/1`` shape (spike
    section 2)::

        {
            "schema": "klt.gen_compose.request/1",
            "pdk": {"variant": "sky130A", "root": None},
            "blocks": [
                {"id": "diffpair", "generator_report": "diffpair.json"},
                {"id": "mirror", "generator_report": {...inline klt gen response...}},
            ],
            "placement": {
                "strategy": "row",
                "order": ["diffpair", "mirror"],
                "spacing_um": 1.0,
            },
            "connectivity": [...],
            "routing": {"layer_role": "metal", "width_um": 0.17},
            "options": {"cell_name": "ota_top_0", "output": "ota_top_0.gds"},
        }

    ``pdk``/``connectivity``/``routing``/``options`` are all optional.
    ``request.pdk`` only accepts ``variant``/``root`` (:data:`_ALLOWED_PDK_KEYS`)
    -- an unrecognised key (e.g. ``name``, a plausible typo for ``variant``)
    is an application error, not a silent fallback to ``$PDK``/the default
    search order (#328). ``connectivity[]`` is validated (every referenced
    block ``id``/port must exist) but not yet routed -- ``routing`` is
    accepted and otherwise ignored this phase (phase 2 implements
    point-to-point routing; see the module docstring). Returns a dict
    matching the documented response schema (see ``docs/cli/gen-compose.md``).

    ``request_dir`` is the directory a relative ``blocks[].generator_report``
    path string resolves against (mirrors ``klt lvs``'s
    ``load_request_arg``/``_resolve_relative`` convention, ``lvs.py``) --
    normally the request document's own directory, passed in by
    ``cli/gen_compose_cmd.py``. Defaults to the current working directory
    when omitted (``None``), so a direct/library caller with no request file
    at all (e.g. an inline dict, as ``tests/test_metrics_regression.py``
    calls this function) keeps resolving cwd-relative paths unchanged. An
    absolute ``generator_report`` path, or one given as an inline JSON
    object, is unaffected by ``request_dir`` either way.

    Raises :class:`GenComposeError` for an unresolvable PDK, an unrecognised
    ``request.pdk`` key, a malformed request, an unsupported
    ``placement.strategy``, a ``connectivity[]`` reference to a nonexistent
    block ``id``/port, or a GDS read/write failure.
    """
    if not isinstance(request, dict):
        raise GenComposeError("request must be a JSON object")

    pdk_request = request.get("pdk") or {}
    if not isinstance(pdk_request, dict):
        raise GenComposeError("request.pdk must be a JSON object")
    unknown_pdk_keys = set(pdk_request) - _ALLOWED_PDK_KEYS
    if unknown_pdk_keys:
        allowed = ", ".join(sorted(_ALLOWED_PDK_KEYS))
        raise GenComposeError(
            "request.pdk has unknown field(s): "
            f"{', '.join(sorted(unknown_pdk_keys))} -- allowed: {allowed}"
        )
    try:
        pdk_info = find_pdk(
            variant=pdk_request.get("variant"), root=pdk_request.get("root")
        )
    except PdkNotFoundError as exc:
        raise GenComposeError(str(exc)) from exc

    blocks = _parse_blocks(request.get("blocks"), request_dir or os.getcwd())
    strategy, order, spacing_um, origins_um = _parse_placement(
        request.get("placement"), set(blocks)
    )
    connectivity = _parse_connectivity(request.get("connectivity"), blocks)
    promoted_pins = _parse_pins(request.get("pins"), blocks, connectivity)

    routing = request.get("routing") or {}
    if not isinstance(routing, dict):
        raise GenComposeError("request.routing must be a JSON object")

    options = request.get("options") or {}
    if not isinstance(options, dict):
        raise GenComposeError("request.options must be a JSON object")
    cell_name = options.get("cell_name") or "gen_compose_0"
    output_path = options.get("output") or f"{cell_name}.gds"

    output_dir = os.path.dirname(os.path.abspath(output_path))
    if output_dir and not os.path.isdir(output_dir):
        raise GenComposeError(f"output directory does not exist: {output_dir}")

    bboxes_um = {block_id: block["bbox_um"] for block_id, block in blocks.items()}
    if strategy == "row":
        offsets_um = compute_row_offsets(order, bboxes_um, spacing_um)
    else:
        assert origins_um is not None  # guaranteed by _parse_placement for "explicit"
        offsets_um = resolve_explicit_offsets(order, origins_um)

    placed_bboxes_um = {
        block_id: _translate_bbox(bboxes_um[block_id], offsets_um[block_id])
        for block_id in order
    }
    composed_bbox_um = _union_bbox([placed_bboxes_um[block_id] for block_id in order])

    warnings: list[str] = []
    notes: list[str] = []

    # --- Routing (phase 2) --------------------------------------------------
    # A connectivity[] net is routed only when routing.layer_role/width_um are
    # given; if the caller supplied nets to wire but no routing spec, that's an
    # application error (there is nothing to draw the metal on).
    route_layer: tuple[int, int] | None = None
    label_layer: tuple[int, int] | None = None
    width_um = 0.0
    if connectivity:
        layer_role = routing.get("layer_role")
        if not isinstance(layer_role, str) or not layer_role:
            raise GenComposeError(
                "request.routing.layer_role is required (a layer role such as "
                "'metal') when connectivity[] is non-empty"
            )
        raw_width = routing.get("width_um")
        if (
            isinstance(raw_width, bool)
            or not isinstance(raw_width, (int, float))
            or raw_width <= 0
        ):
            raise GenComposeError(
                "request.routing.width_um is required and must be > 0 when "
                "connectivity[] is non-empty"
            )
        width_um = float(raw_width)
        route_layer = _resolve_route_layer(pdk_info["variant"], layer_role)
        label_layer = _resolve_label_layer(pdk_info["variant"], route_layer)
        if label_layer is None:
            notes.append(
                f"routing.layer_role '{layer_role}' has no PDK label-layer "
                "convention `klt extract` recognises -- routed nets on this "
                "layer will not carry a net label, so they will not survive "
                "as named .SUBCKT pins after extraction"
            )

    nets: list[dict[str, Any]] = []
    unrouted_nets: list[str] = []
    routed_geometry: list[dict[str, Any]] = []
    for entry in connectivity:
        net_label = entry["net"]
        pins = entry["pins"]
        if len(pins) != 2:
            # Bundle (>2-pin) routing is out of scope this phase (spike section
            # 5 item 2). Report as a partial-success unrouted net rather than
            # rejecting the whole request or silently dropping the connection.
            nets.append(
                {
                    "net": net_label,
                    "pins": pins,
                    "routed": False,
                    "route_length_um": None,
                }
            )
            unrouted_nets.append(net_label)
            notes.append(
                f"net '{net_label}' has {len(pins)} pins -- bundle (>2-pin) "
                "routing is out of scope this phase, so it was left unrouted"
            )
            continue

        result = route_two_pin(
            pins[0], pins[1], blocks, offsets_um, placed_bboxes_um, width_um
        )
        nets.append(
            {
                "net": net_label,
                "pins": pins,
                "routed": result["routed"],
                "route_length_um": result["route_length_um"],
            }
        )
        if result["routed"]:
            routed_geometry.append(
                {
                    "net": net_label,
                    "points_um": result["points_um"],
                    "width_um": width_um,
                }
            )
        else:
            unrouted_nets.append(net_label)
            notes.append(f"net '{net_label}' could not be routed: {result['reason']}")

    # --- pins[] (#210): label a single port as a top-level pin, no routing --
    # Each pins[] entry gets one kdb.Text at its port's own composed-frame
    # position, on the label layer resolved for that port's OWN drawn layer
    # (resolved per-entry -- each port can be on a different physical layer,
    # unlike connectivity[]'s single shared routing.layer_role). A port whose
    # layer has no ExtractionDeck label convention is a partial success: a
    # drc_hints note, pin not labelled -- never a hard failure.
    pin_placements: list[dict[str, Any]] = []
    response_pins: list[dict[str, Any]] = []
    for entry in promoted_pins:
        net_label = entry["net"]
        block_id = entry["block"]
        port_name = entry["port"]
        port = blocks[block_id]["ports"].get(port_name)
        labelled = False
        if not _port_has_geometry(port):
            notes.append(
                f"pin '{net_label}' (block '{block_id}' port '{port_name}') has no "
                "reported {x_um, y_um, layer} geometry -- it was not labelled"
            )
        else:
            draw_layer = (port["layer"]["layer"], port["layer"]["datatype"])
            pin_label_layer = _resolve_label_layer(pdk_info["variant"], draw_layer)
            if pin_label_layer is None:
                notes.append(
                    f"pin '{net_label}' (block '{block_id}' port '{port_name}') is "
                    f"on layer {draw_layer[0]}/{draw_layer[1]}, which has no PDK "
                    "label-layer convention `klt extract` recognises -- the pin was "
                    "not labelled, so it will not survive as a named .SUBCKT pin "
                    "after extraction"
                )
            else:
                offset = offsets_um[block_id]
                pin_placements.append(
                    {
                        "net": net_label,
                        "x_um": port["x_um"] + offset["x"],
                        "y_um": port["y_um"] + offset["y"],
                        "layer": pin_label_layer,
                    }
                )
                labelled = True
        response_pins.append(
            {
                "net": net_label,
                "block": block_id,
                "port": port_name,
                "labelled": labelled,
            }
        )

    _write_composed_gds(
        blocks,
        order,
        offsets_um,
        cell_name,
        output_path,
        routed_geometry,
        route_layer,
        label_layer,
        pin_placements,
    )

    # --- drc_hints: matched-group echo + tightest spacing used --------------
    matched_groups = _collect_matched_groups(blocks, order)

    min_spacing_um: float | None = None
    if connectivity and strategy == "row":
        # "row" placement always applies spacing_um between adjacent blocks;
        # routing adds no spacing tighter than that at this phase (routes run
        # through the placed channels), so the tightest spacing actually used
        # is the placement gap. Left null when nothing was routed (phase-1
        # behaviour), and also left null for "explicit" placement (#321) --
        # there is no single shared spacing value to report (per-pair
        # separation is exactly what a caller-declared origin expresses).
        min_spacing_um = spacing_um

    response_blocks = [
        {
            "id": block_id,
            "generator": blocks[block_id]["generator"],
            "offset_um": offsets_um[block_id],
            "bbox_um": placed_bboxes_um[block_id],
        }
        for block_id in order
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "cell_name": cell_name,
        "gds_path": output_path,
        "pdk": {
            "name": pdk_info["variant"],
            "variant": pdk_info["variant"],
            "version": pdk_info["version"],
        },
        "bbox_um": composed_bbox_um,
        "blocks": response_blocks,
        "nets": nets,
        "pins": response_pins,
        "unrouted_nets": unrouted_nets,
        "drc_hints": {
            "min_spacing_um": min_spacing_um,
            "matched_groups": matched_groups,
            "notes": notes,
        },
        "warnings": warnings,
    }


def _collect_matched_groups(
    blocks: dict[str, dict[str, Any]], order: list[str]
) -> list[dict[str, Any]]:
    """Echo every distinct ``matched_group_id`` seen among input blocks.

    Read-only consumption of the ``drc_hints.matched_group_id`` hook every
    array/matched-device ``klt gen`` generator populates (spike section 2).
    One entry per distinct id, in first-seen order, listing which request-level
    block ``id``s carry it. ``placement_symmetric`` is always ``null`` this
    phase -- symmetry *verification* against a declared symmetry axis is out of
    scope (spike section 5 item 3).
    """
    groups: dict[str, list[str]] = {}
    for block_id in order:
        gid = blocks[block_id].get("matched_group_id")
        if gid:
            groups.setdefault(gid, []).append(block_id)
    return [
        {
            "matched_group_id": gid,
            "blocks": block_ids,
            "placement_symmetric": None,
        }
        for gid, block_ids in groups.items()
    ]


def _write_composed_gds(
    blocks: dict[str, dict[str, Any]],
    order: list[str],
    offsets_um: dict[str, dict[str, float]],
    cell_name: str,
    output_path: str,
    routed_geometry: list[dict[str, Any]] | None = None,
    route_layer: tuple[int, int] | None = None,
    label_layer: tuple[int, int] | None = None,
    pin_placements: list[dict[str, Any]] | None = None,
) -> None:
    """Write ``output_path``: one new top cell (``cell_name``) instantiating
    every block's own top cell as a translated sub-cell instance, plus any
    routed metal.

    Each block's GDS is read into its own scratch :class:`kdb.Layout`, its
    reported top cell (``generator_report.cell_name``) is duplicated
    (:meth:`kdb.Cell.copy_tree`) into a fresh sub-cell of the composed
    layout, and that sub-cell is instantiated into ``cell_name`` at the
    block's computed ``offset_um`` -- geometry is copied exactly once (never
    re-derived), and hierarchy is preserved (each block stays its own cell,
    not flattened into the composed top cell).

    Routed nets (``routed_geometry``: a list of ``{net, points_um,
    width_um}``) are drawn as native :class:`kdb.Path` shapes directly on the
    composed top cell, on ``route_layer`` (a ``(layer, datatype)`` pair) --
    top-level metal, not inside any block's sub-cell. When ``label_layer`` is
    given (resolved by :func:`_resolve_label_layer`), each routed net also
    gets one :class:`kdb.Text` label -- named after its own ``net`` field --
    on that layer, at the arc-length midpoint of its drawn path
    (:func:`_polyline_midpoint_um`), so `klt extract`'s label-recognition
    convention (``metals[i]``/``metal_labels[i]`` -- see
    :class:`klayout_tools.decks.ExtractionDeck`) promotes the net to a named
    ``.SUBCKT`` pin instead of an anonymous one (#200).

    ``pin_placements`` (a list of ``{net, x_um, y_um, layer}``, pre-resolved by
    :func:`compose` from the request's ``pins[]``) each get one
    :class:`kdb.Text` at their own composed-frame ``(x_um, y_um)`` on their own
    ``layer`` (a ``(layer, datatype)`` label pair) -- no metal is drawn, the
    port's existing geometry is what the label attaches to. This is
    independent of the ``routed_geometry``/``route_layer`` block above: a
    ``pins[]`` label can land on a poly gate (via ``poly_label``) that carries
    no routed metal at all (#210).
    """
    import klayout.db as kdb

    layout = kdb.Layout()
    top = layout.create_cell(cell_name)
    dbu: float | None = None

    for block_id in order:
        block = blocks[block_id]
        gds_path = block["gds_path"]
        src_layout = kdb.Layout()
        try:
            src_layout.read(gds_path)
        except Exception as exc:  # klayout raises RuntimeError for bad formats/paths
            raise GenComposeError(
                f"block '{block_id}': could not read gds_path '{gds_path}': {exc}"
            ) from exc

        if dbu is None:
            dbu = src_layout.dbu
            layout.dbu = dbu
        elif abs(src_layout.dbu - dbu) > 1e-12:
            raise GenComposeError(
                f"block '{block_id}': gds '{gds_path}' has dbu={src_layout.dbu}, "
                f"which does not match the composed cell's dbu={dbu} -- every "
                "block must share the same dbu"
            )

        src_cell_name = block["cell_name"]
        src_cell = src_layout.cell(src_cell_name)
        if src_cell is None:
            raise GenComposeError(
                f"block '{block_id}': gds '{gds_path}' has no cell named "
                f"'{src_cell_name}' (from its generator_report.cell_name)"
            )

        sub_cell = layout.create_cell(f"{block_id}__{src_cell_name}")
        sub_cell.copy_tree(src_cell)

        offset = offsets_um[block_id]
        ox = int(round(offset["x"] / dbu))
        oy = int(round(offset["y"] / dbu))
        top.insert(kdb.CellInstArray(sub_cell.cell_index(), kdb.Trans(ox, oy)))

    if routed_geometry and route_layer is not None:
        if dbu is None:  # no blocks read (can't happen -- blocks[] is non-empty)
            dbu = layout.dbu
        layer_index = layout.layer(route_layer[0], route_layer[1])
        label_layer_index = (
            layout.layer(label_layer[0], label_layer[1])
            if label_layer is not None
            else None
        )
        for route in routed_geometry:
            points = route["points_um"]
            if not points or len(points) < 2:
                continue
            path_points = [
                kdb.Point(int(round(x / dbu)), int(round(y / dbu))) for (x, y) in points
            ]
            width_dbu = int(round(route["width_um"] / dbu))
            top.shapes(layer_index).insert(kdb.Path(path_points, width_dbu))

            if label_layer_index is not None:
                lx_um, ly_um = _polyline_midpoint_um(points)
                label_point = kdb.Point(
                    int(round(lx_um / dbu)), int(round(ly_um / dbu))
                )
                top.shapes(label_layer_index).insert(
                    kdb.Text(route["net"], kdb.Trans(label_point))
                )

    if pin_placements:
        if dbu is None:  # no blocks read (can't happen -- blocks[] is non-empty)
            dbu = layout.dbu
        for pin in pin_placements:
            layer = pin["layer"]
            pin_layer_index = layout.layer(layer[0], layer[1])
            pin_point = kdb.Point(
                int(round(pin["x_um"] / dbu)), int(round(pin["y_um"] / dbu))
            )
            top.shapes(pin_layer_index).insert(
                kdb.Text(pin["net"], kdb.Trans(pin_point))
            )

    write_layout(layout, output_path, GenComposeError)
