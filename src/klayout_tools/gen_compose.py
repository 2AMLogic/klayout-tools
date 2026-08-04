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
* **Via-drop routing** (issue #454, re-raising #433's Ask options 1/2): a
  family whose curated extraction deck declares a second routing-metal level
  (e.g. sky130's ``"metal2"``/met1) can be selected as ``routing.layer_role``
  even though every ``klt gen`` block's own pads are drawn on the base
  ``"metal"`` role -- :func:`route_two_pin` drops the backbone back down to
  each target pin's own layer via the connecting via (sky130's ``"via1"``/
  mcon) exactly at that pin's position, so the backbone itself never runs
  across another pad on the pad layer. This is what makes a same-block bus
  (e.g. chaining a matched array's unit terminals) routable without either
  accepting a same-layer short or failing #433's self-net pad-crossing
  rejection -- see :func:`_resolve_via_drop_layer`.
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
``gds_path``) -- this module never re-derives a block's *placement math* from
its GDS stream; the GDS stream is read at write time, to copy each block's
already-computed geometry into the composed output, and (#453/#469) for the
route-layer *obstacle* shapes of a block a self-net lands on, since a port's
reported ``width_um`` is its contact size rather than the extent of the pad
metal drawn around it (see :func:`read_block_layer_geometry`). That is the
one place this module looks at a block's *shapes* rather than at its
``generator_report`` -- it reads obstacle geometry only, for a self-net's own
block, and never touches placement.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ._layout import write_layout
from .decks import ExtractionDeck, get_extraction_deck
from .gen import (
    _PDK_ROLE_LAYERS,
    CONTACT_SIZE_UM,
    ENCLOSURE_MARGIN_UM,
    GenError,
    _pdk_family,
)
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

#: Via-drop square side (um, issue #454) -- the same drawn contact/via size
#: every `klt gen` generator's own unit devices already use (`gen.CONTACT_SIZE_UM`),
#: so a via-drop's via is never a second, unvalidated size.
_VIA_DROP_SIZE_UM = CONTACT_SIZE_UM

#: Landing-pad square side (um, issue #454) drawn on *both* sides of a
#: via-drop (the backbone's own ``route_layer`` and the target pin's own
#: layer), independent of the route's own ``width_um`` -- the same
#: `CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM` contact-enclosure convention
#: `gen.py`'s own unit-device layouts already use (e.g. `_bjt_unit_layout`'s
#: `contact_region_um`), not a new unvalidated margin. Drawing an explicit
#: landing pad -- rather than relying on the backbone's own trace width --
#: guarantees the via's enclosure requirement is met even when a caller
#: requests a `routing.width_um` narrower than a full contact-enclosure
#: footprint (e.g. sky130's own `li1.width.1` minimum, 0.17um).
_VIA_LANDING_SIZE_UM = CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM


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
        # The block's own minimum same-layer spacing, used as the clearance a
        # route must keep from the cut ends of a ring opening (#434). Absent
        # (or unusable) means "no clearance claimed" rather than an error --
        # every other consumer of drc_hints treats it as advisory too.
        min_spacing_um = 0.0
        if isinstance(drc_hints, dict):
            candidate = drc_hints.get("matched_group_id")
            if isinstance(candidate, str) and candidate:
                matched_group_id = candidate
            spacing = drc_hints.get("min_spacing_um")
            if not isinstance(spacing, bool) and isinstance(spacing, (int, float)):
                min_spacing_um = max(0.0, float(spacing))

        blocks[block_id] = {
            "id": block_id,
            "generator": generator,
            "cell_name": cell_name,
            "gds_path": gds_path,
            "bbox_um": bbox_um,
            "port_names": set(ports_by_name),
            "ports": ports_by_name,
            "matched_group_id": matched_group_id,
            "min_spacing_um": min_spacing_um,
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

    A ``GAP_*`` port (a ring opening, #434) is rejected outright: it marks
    *absence* of metal -- where a route may cross the ring -- so it can be
    neither wired by ``connectivity[]`` nor labelled by ``pins[]``.
    """
    block = blocks.get(block_id)
    if block is None:
        raise GenComposeError(f"{where} references unknown block id '{block_id}'")
    if block["port_names"] and port not in block["port_names"]:
        raise GenComposeError(
            f"{where} references unknown port '{port}' on block '{block_id}' -- "
            f"available: {', '.join(sorted(block['port_names']))}"
        )
    if port.startswith(_RING_GAP_PORT_PREFIX) and _ring_port_side(port) is not None:
        raise GenComposeError(
            f"{where} references port '{port}' on block '{block_id}', which "
            "marks a ring *opening* (a routing hole through the guard/collector "
            "ring), not a conductor -- route to the port inside the ring the "
            "opening exists to reach, or to one of the ring's own TAP_*/COLL_* "
            "tap ports"
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


def _port_own_layer(port: dict[str, Any]) -> tuple[int, int] | None:
    """The ``(layer, datatype)`` a port's own reported ``layer{layer,
    datatype}`` geometry names, or ``None`` when it is missing/malformed.

    Distinct from :func:`_port_has_geometry` (which also requires
    ``x_um``/``y_um``) -- callers of this helper already know the port has a
    usable position and only need its physical layer, e.g. to decide whether
    a via-drop is needed (issue #454, see :func:`_resolve_via_drop_layer`).
    """
    layer = port.get("layer")
    if not isinstance(layer, dict):
        return None
    layer_num, datatype = layer.get("layer"), layer.get("datatype")
    if (
        isinstance(layer_num, int)
        and not isinstance(layer_num, bool)
        and isinstance(datatype, int)
        and not isinstance(datatype, bool)
    ):
        return (layer_num, datatype)
    return None


def _resolve_via_drop_layer(
    deck: ExtractionDeck,
    route_layer: tuple[int, int],
    port_layer: tuple[int, int],
) -> tuple[tuple[int, int] | None, str | None]:
    """Resolve whether a route drawn on ``route_layer`` needs a via-drop to
    reach a target pin drawn on ``port_layer`` (issue #454, re-raising
    #433's Ask options 1/2: a ``metal2``/via role pair plus router support
    for actually using it).

    Looks the two layers up in the resolved PDK family's own
    :class:`~klayout_tools.decks.ExtractionDeck` ``metals``/``vias`` stack
    (:func:`~klayout_tools.decks.get_extraction_deck`) -- the *same*
    connectivity data ``klt extract``'s per-layer connectivity loop already
    walks (``connect(metals[i], vias[i])`` / ``connect(vias[i],
    metals[i + 1])``), never a second, private via table.

    Returns ``(via_layer, error)``:

    * ``(None, None)`` -- no drop needed. Either ``port_layer`` already *is*
      ``route_layer`` (the pre-#454 single-metal routing path, unchanged),
      or one of the two layers is not itself a member of ``deck.metals`` at
      all (e.g. a bare-poly gate port) -- via-drop only ever applies between
      two declared routing-metal levels, so a route to any other role draws
      directly on ``route_layer`` exactly as it always has.
    * ``(via_pair, None)`` -- a drop is needed and resolved; draw a via on
      ``via_pair`` at the pin's own position.
    * ``(None, reason)`` -- a drop is needed but not resolvable (the two
      metals-stack levels are more than one via hop apart, or the deck
      declares no via for the hop needed) -- the caller reports the net
      unroutable rather than drawing a disconnected short.
    """
    if route_layer == port_layer:
        return None, None
    try:
        route_idx = deck.metals.index(route_layer)
        port_idx = deck.metals.index(port_layer)
    except ValueError:
        return None, None  # not a metals-stack level -- an unrelated role
    if abs(route_idx - port_idx) != 1:
        return None, (
            f"routing.layer_role's metal (deck metals[{route_idx}]) is more "
            f"than one via hop from this pin's own layer (deck "
            f"metals[{port_idx}]) -- gen_compose's via-drop only supports a "
            "single-hop drop"
        )
    via_index = min(route_idx, port_idx)
    if via_index >= len(deck.vias):
        return None, (
            "the resolved PDK's extraction deck declares no via connecting "
            f"deck metals[{route_idx}] and metals[{port_idx}]"
        )
    return deck.vias[via_index], None


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
    *,
    stub_a_um: float | None = None,
    stub_b_um: float | None = None,
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

    ``stub_a_um``/``stub_b_um`` override ``stub_um`` for one port when a
    longer stub is needed to lift the connecting jog clear of a block the
    port sits inside (e.g. a gate landing pad recessed below its block's top
    edge, issue #461); each defaults to ``stub_um``.

    Returns the cleaned ordered ``(x, y)`` waypoint list (um). ``pya.Path``
    renders each interior corner as a square miter that fully fills the bend,
    so no separate bend-insertion pass is needed -- the corner *is* the bend.
    """
    ax, ay = a
    bx, by = b
    va = _DIRECTION_VECTORS[dir_a_deg]
    vb = _DIRECTION_VECTORS[dir_b_deg]
    sa_len = stub_um if stub_a_um is None else stub_a_um
    sb_len = stub_um if stub_b_um is None else stub_b_um
    sa = (ax + va[0] * sa_len, ay + va[1] * sa_len)
    sb = (bx + vb[0] * sb_len, by + vb[1] * sb_len)

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


#: Port-name prefix a ``klt gen`` generator uses to report a *ring opening*
#: -- the routing gap ``params.ring_gap_side`` cuts through one side of a
#: guard/collector ring (#434, see ``gen.py``'s ``_ring_ports``). A
#: ``GAP_<side>`` entry is a marker, not a conductor: ``x_um``/``y_um`` is the
#: opening's centre on the ring's own centre line, ``width_um`` is how long
#: the opening is along that side, and ``direction_deg`` is the side's outward
#: normal. It is the one place a route may cross the ring without merging with
#: the ring's tap net.
_RING_GAP_PORT_PREFIX = "GAP_"

#: The four sides of a ring, as named by ``TAP_``/``COLL_``/``GAP_`` ports.
_RING_SIDES = ("N", "S", "E", "W")


def _block_has_ring_taps(block: dict[str, Any]) -> bool:
    """Whether ``block`` reports a guard/collector ring (any tap port)."""
    return any(name.startswith(_RING_TAP_PORT_PREFIXES) for name in block["port_names"])


def _ring_port_side(name: str) -> str | None:
    """The ring side a ``TAP_<side>``/``COLL_<side>``/``GAP_<side>`` port name
    refers to, or ``None`` for any other port name."""
    for prefix in (*_RING_TAP_PORT_PREFIXES, _RING_GAP_PORT_PREFIX):
        if name.startswith(prefix):
            side = name[len(prefix) :]
            if side in _RING_SIDES:
                return side
    return None


def _ring_gap_ports(block: dict[str, Any]) -> dict[str, dict[str, float]]:
    """The ring openings ``block`` declares, keyed by side (#434).

    Reads every ``GAP_<side>`` port with usable geometry into
    ``{"x_um", "y_um", "opening_um"}`` (block-local coordinates). An empty
    dict means the block's ring is a closed loop, which is what the
    guard/collector-ring check (#199 case 2) requires it to reject routes
    into.
    """
    gaps: dict[str, dict[str, float]] = {}
    for name, port in block["ports"].items():
        if not name.startswith(_RING_GAP_PORT_PREFIX) or not isinstance(port, dict):
            continue
        side = name[len(_RING_GAP_PORT_PREFIX) :]
        if side not in _RING_SIDES:
            continue
        values: list[float] = []
        for key in ("x_um", "y_um", "width_um"):
            value = port.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                break
            values.append(float(value))
        if len(values) != 3 or values[2] <= 0.0:
            continue  # a gap with no usable geometry cannot be routed through
        gaps[side] = {"x_um": values[0], "y_um": values[1], "opening_um": values[2]}
    return gaps


def _ring_side_lines(block: dict[str, Any]) -> dict[str, float]:
    """Each ring side's own centre-line coordinate in the block's local frame
    (an ``x`` for ``E``/``W``, a ``y`` for ``N``/``S``), keyed by side.

    Read off the ring's own reported ports: a ``TAP_``/``COLL_`` tap port sits
    at the midpoint of its side, and a ``GAP_`` port sits on the same centre
    line, so between them a ring reports where all four of its sides run --
    which is what :func:`_ring_gap_route_conflict` needs to tell a route that
    passes through a declared opening from one that cuts the ring's metal
    somewhere else.
    """
    lines: dict[str, float] = {}
    for name, port in block["ports"].items():
        side = _ring_port_side(name)
        if side is None or not isinstance(port, dict):
            continue
        value = port.get("x_um" if side in ("E", "W") else "y_um")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        lines[side] = float(value)
    return lines


def _ring_gap_route_conflict(
    points: list[tuple[float, float]],
    block: dict[str, Any],
    offset_um: dict[str, float],
    bbox_um: dict[str, float],
    width_um: float,
) -> str | None:
    """Why ``points`` may not cross ``block``'s (gapped) guard/collector ring,
    or ``None`` when every crossing passes cleanly through a declared opening.

    Applied only to a block that declares at least one ``GAP_<side>`` opening
    (#434) -- a closed ring is still rejected outright by the name-based
    guard/collector-ring check in :func:`route_two_pin`. Every segment of the
    backbone is tested against all four of the ring's own side centre lines
    (:func:`_ring_side_lines`), inside the block's placed bbox:

    * a crossing on a side with no declared opening merges the net with the
      ring's tap net exactly as before;
    * a crossing on the gapped side must clear the opening's edges by half
      the route width plus the block's own reported ``min_spacing_um``, so the
      drawn wire fits *through* the opening rather than shorting to either cut
      end of the ring;
    * a segment running *along* a ring side lies on the ring's metal for its
      whole length, which is a short however wide the opening is.

    A ring that does not report where all four of its sides run cannot be
    checked this way, and is rejected rather than assumed clear.
    """
    eps = 1e-9
    gaps = _ring_gap_ports(block)
    lines = _ring_side_lines(block)
    missing = [side for side in _RING_SIDES if side not in lines]
    if missing:
        return (
            f"block '{block['id']}' declares a ring opening but reports no "
            f"port locating its ring's {'/'.join(missing)} side(s) -- the "
            "route cannot be shown to pass through the opening rather than "
            "across the ring's metal elsewhere"
        )

    clearance_um = width_um / 2.0 + block.get("min_spacing_um", 0.0)
    for side, local in lines.items():
        vertical = side in ("E", "W")
        line = local + (offset_um["x"] if vertical else offset_um["y"])
        if vertical:
            extent = (bbox_um["y0"], bbox_um["y1"])
        else:
            extent = (bbox_um["x0"], bbox_um["x1"])

        window: tuple[float, float] | None = None
        gap = gaps.get(side)
        if gap is not None:
            centre = (
                gap["y_um"] + offset_um["y"]
                if vertical
                else gap["x_um"] + offset_um["x"]
            )
            half = gap["opening_um"] / 2.0
            window = (centre - half, centre + half)

        for p0, p1 in zip(points, points[1:], strict=False):
            # "across" is the coordinate the side's centre line is fixed in;
            # "along" is the coordinate that runs down the side.
            across0, across1 = (p0[0], p1[0]) if vertical else (p0[1], p1[1])
            along0, along1 = (p0[1], p1[1]) if vertical else (p0[0], p1[0])

            if abs(across0 - line) <= eps and abs(across1 - line) <= eps:
                seg_lo, seg_hi = sorted((along0, along1))
                if min(seg_hi, extent[1]) - max(seg_lo, extent[0]) > eps:
                    return (
                        f"backbone runs along block '{block['id']}''s ring on "
                        f"its {side} side -- a route laid on the ring's own "
                        "metal merges this net with the ring's tap net, "
                        "whatever opening the ring declares"
                    )
                continue

            if (across0 - line) * (across1 - line) >= 0.0:
                continue  # this segment does not cross the side's centre line

            t = (line - across0) / (across1 - across0)
            at = along0 + t * (along1 - along0)
            if not (extent[0] - eps <= at <= extent[1] + eps):
                continue  # crosses the line beyond the block -- clear of the ring

            if window is None:
                return (
                    f"backbone crosses block '{block['id']}''s ring on its "
                    f"{side} side, which declares no opening -- the route "
                    "would merge this net with the ring's tap net; cut the "
                    f"opening on that side (params.ring_gap_side: '{side}') "
                    "or route through the side that already has one"
                )
            if (
                at - clearance_um < window[0] - eps
                or at + clearance_um > window[1] + eps
            ):
                return (
                    f"backbone crosses block '{block['id']}''s ring on its "
                    f"{side} side at {at:.4g}um, outside the "
                    f"[{window[0]:.4g}, {window[1]:.4g}]um opening it declares "
                    f"(a {width_um}um-wide route needs {clearance_um:.4g}um of "
                    "clearance inside the opening) -- widen the opening "
                    "(params.ring_gap_um), move it (params.ring_gap_offset_um), "
                    "or place the blocks so the route lines up with it"
                )

    return None


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


def read_block_layer_geometry(
    block_id: str,
    block: dict[str, Any],
    offset_um: dict[str, float],
    layer: tuple[int, int],
) -> dict[str, Any] | None:
    """Read ``block``'s **drawn** shapes on ``layer`` into the composed frame.

    Returns ``{"region": kdb.Region, "dbu": float}`` -- the block's own GDS
    geometry on the ``(layer, datatype)`` pair the route is drawn on, merged
    and translated by the block's placed ``offset_um``, in integer database
    units -- or ``None`` when the block draws nothing there.

    This is the one place this module looks at a block's *shapes* rather than
    at its ``generator_report``. It exists because a ``klt gen`` port's
    reported ``width_um`` is the port's **contact/access** size, not the
    extent of the pad metal drawn around it (e.g. a ``bjt_array`` base tie
    reports ``width_um: 0.22`` -- ``CONTACT_SIZE_UM`` -- for a pad whose drawn
    local metal is 0.42um x 0.68um, see ``gen.py``'s ``_bjt_unit_layout``), so
    the reported-geometry pad model :func:`route_two_pin`'s self-net check
    (check 3, ``_segment_bbox_interior_overlap_um``) starts from systematically
    *under*-estimates what a route can short to (#453/#469). Placement math is
    still never re-derived from the stream -- this reads obstacle geometry
    only, and only for a self-net's own block.
    """
    import klayout.db as kdb

    gds_path = block["gds_path"]
    src_layout = kdb.Layout()
    try:
        src_layout.read(gds_path)
    except Exception as exc:  # klayout raises RuntimeError for bad formats/paths
        raise GenComposeError(
            f"block '{block_id}': could not read gds_path '{gds_path}': {exc}"
        ) from exc

    src_cell_name = block["cell_name"]
    src_cell = src_layout.cell(src_cell_name)
    if src_cell is None:
        raise GenComposeError(
            f"block '{block_id}': gds '{gds_path}' has no cell named "
            f"'{src_cell_name}' (from its generator_report.cell_name)"
        )

    dbu = src_layout.dbu
    layer_index = src_layout.find_layer(layer[0], layer[1])
    if layer_index is None:
        return None

    region = kdb.Region(src_cell.begin_shapes_rec(layer_index))
    region.merge()
    if region.is_empty():
        return None
    region.transform(
        kdb.Trans(
            int(round(offset_um["x"] / dbu)),
            int(round(offset_um["y"] / dbu)),
        )
    )
    return {"region": region, "dbu": dbu}


def _drawn_route_region(
    points_um: list[tuple[float, float]], width_um: float, dbu: float
):
    """The metal a routed backbone actually draws, as a ``kdb.Region``.

    Built from the *same* ``kdb.Path(points, width)`` construction
    :func:`_write_composed_gds` inserts into the composed cell, so this check
    and the drawn output cannot disagree about the route's footprint.
    """
    import klayout.db as kdb

    path = kdb.Path(
        [kdb.Point(int(round(x / dbu)), int(round(y / dbu))) for (x, y) in points_um],
        int(round(width_um / dbu)),
    )
    return kdb.Region(path.polygon())


def _self_net_drawn_short(
    points_um: list[tuple[float, float]],
    geometry: dict[str, Any],
    a: tuple[float, float],
    b: tuple[float, float],
    width_um: float,
    own_ports: dict[str, Any],
    own_offset: dict[str, float],
) -> tuple[float, list[str]] | None:
    """Whether a self-net's drawn metal lands on its own block's *other*
    drawn shapes on the route layer.

    ``geometry`` is :func:`read_block_layer_geometry`'s result for the block
    both pins sit on. Every merged shape of that block on the route layer is
    an obstacle **except** the two the route is supposed to land on -- the
    shapes holding the two endpoint ports themselves. Any remaining shape the
    drawn metal actually overlaps (positive area; a mere edge touch is a
    spacing question for ``klt drc``, not a short) is a silent short.

    Returns ``(overlap_um2, crossed_port_names)`` for the first such shape
    set, or ``None`` when the route only lands on its own two endpoints.
    """
    import klayout.db as kdb

    region = geometry["region"]
    dbu = geometry["dbu"]

    def _probe(x_um: float, y_um: float):
        px = int(round(x_um / dbu))
        py = int(round(y_um / dbu))
        return kdb.Box(px - 1, py - 1, px + 1, py + 1)

    endpoints = kdb.Region()
    for x_um, y_um in (a, b):
        endpoints.insert(_probe(x_um, y_um))

    # Everything on this block's route layer that is *not* one of the two
    # shapes the route is meant to terminate on.
    obstacles = region.not_interacting(endpoints)
    if obstacles.is_empty():
        return None

    overlap = obstacles & _drawn_route_region(points_um, width_um, dbu)
    if overlap.is_empty():
        return None

    overlap_um2 = overlap.area() * dbu * dbu
    hit = obstacles.interacting(overlap)
    crossed: list[str] = []
    for name, port in own_ports.items():
        if not _port_has_geometry(port):
            continue
        probe = kdb.Region(
            _probe(
                float(port["x_um"]) + own_offset["x"],
                float(port["y_um"]) + own_offset["y"],
            )
        )
        if not hit.interacting(probe).is_empty():
            crossed.append(name)
    return overlap_um2, crossed


def route_two_pin(
    pin_a: dict[str, Any],
    pin_b: dict[str, Any],
    blocks: dict[str, dict[str, Any]],
    offsets_um: dict[str, dict[str, float]],
    placed_bboxes_um: dict[str, dict[str, float]],
    width_um: float,
    route_layer: tuple[int, int] | None = None,
    extraction_deck: ExtractionDeck | None = None,
    block_geometry: dict[str, dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Route one two-pin net and report the result.

    Resolves each pin's port position into the composed coordinate frame
    (port ``x_um``/``y_um`` translated by its block's ``offset_um``),
    generates a Manhattan backbone (:func:`manhattan_backbone`), and applies
    five routability checks before reporting success -- all diagnostic
    heuristics against the composition's own already-known geometry
    (``bbox_um``/``ports[]``) or, for check 4, the block's own drawn shapes
    (:func:`read_block_layer_geometry`) -- not a DRC check (``klt drc``
    remains authoritative):

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
    3. **Self-net pad-crossing check** (#433): a same-block net's backbone is
       always inside its own block's bbox, so the obstacle-overlap check
       below exempts it entirely -- but nothing else was checking whether
       that backbone runs straight over one of the block's *other* pads
       (e.g. bussing a bjt_array's emitters together necessarily crosses the
       base pads sitting between them). Every other port on the same block
       that shares the route's own ``route_layer`` is treated as a square
       pad footprint (side length its reported ``width_um``); a backbone
       overlapping that footprint's interior is rejected rather than drawn
       as a silent short. A same-direction degenerate-jog variant of this
       same check (#453's conservative fallback) additionally rejects any
       other same-facing port on the same row/column between the two pins
       outright, regardless of its reported ``width_um`` -- see the
       ``conservative_same_dir`` block below.
    4. **Self-net drawn-metal check** (#453/#469): check 3's reported-
       ``width_um`` pad model, even with its same-direction fallback, is
       still built from *reported* geometry and can miss a short whenever
       the pins sit on different rows/columns or the route is wide enough to
       reach a pad the modelled square under-states. When ``block_geometry``
       supplies the block's actual drawn shapes on ``route_layer``
       (:func:`read_block_layer_geometry`), the route's own drawn metal
       (:func:`_drawn_route_region`, the same ``kdb.Path`` the composed cell
       gets) is intersected with them: overlapping any merged shape other
       than the two the endpoints sit on (positive area only -- an edge
       touch is a ``klt drc`` spacing question, not a short) is a silent
       short. This check is independent of, and composes with, check 3 --
       it catches cases (different rows, wider routes) check 3's reported-
       geometry model cannot see.
    5. **Obstacle-overlap check** (#199 case 1): after each port's own
       unavoidable "sit set back from my own block's edge" margin is
       excluded (:func:`_port_edge_margin_um`), the drawn backbone must not
       cross the *interior* of any block's bbox -- including the two pins'
       own blocks (a same-facing port pair forces the backbone to plow
       through the destination block's full width to reach a port on its far
       side, e.g. crossing that block's own opposite-facing pin) and any
       third block's bbox the straight-line/single-jog backbone happens to
       cross in a longer row.
    6. **Via-drop resolution** (#454): when ``route_layer`` and a pin's own
       reported layer differ, :func:`_resolve_via_drop_layer` looks up
       whether ``extraction_deck`` connects the two with a single via hop
       (e.g. ``routing.layer_role: "metal2"`` backbone reaching a
       ``"metal"``-role li1 pad via sky130's ``mcon``). A pin whose own layer
       *is* ``route_layer`` needs no drop; a pin on an unrelated role (e.g. a
       bare-poly gate) is left exactly as before #454 (drawn directly on
       ``route_layer``, no via). Only a pin whose layer is a *different*
       ``deck.metals`` level than ``route_layer``, more than one via hop
       away, is rejected here -- reported unroutable rather than drawing a
       disconnected short.

    Checks 1-5 report the net unroutable (spike section 2,
    ``unrouted_nets[]``) rather than silently drawing a short; check 6 does
    the same for a route it cannot connect end to end. ``route_layer`` (the
    ``(layer, datatype)`` pair ``routing.layer_role`` resolved to, see
    :func:`_resolve_route_layer`) is optional only for callers that predate
    #433/don't care about check 3 (e.g. direct unit tests) -- ``compose()``
    always passes it when ``connectivity[]`` is non-empty, since that's the
    only time a route is actually drawn. ``block_geometry`` (block ``id`` ->
    :func:`read_block_layer_geometry` result, ``None`` for a block that draws
    nothing on the route layer) is likewise optional and likewise always
    supplied by ``compose()`` for the block a self-net lands on; without it
    check 4 is skipped and only check 3's weaker reported-geometry pad model
    applies. ``extraction_deck`` is likewise optional and only consulted
    (check 6) when both it and ``route_layer`` are given -- omitting it (e.g.
    a pre-#454 caller) draws every pin directly on ``route_layer`` with no
    via-drop, exactly as before that issue.

    Returns ``{"routed": bool, "route_length_um": float | None,
    "points_um": list | None, "via_drops": list, "reason": str | None}``.
    ``via_drops`` is a list of ``{"x_um", "y_um", "via_layer", "port_layer"}``
    entries (empty unless a drop was resolved), consumed by
    :func:`_write_composed_gds` to draw each drop's via + landing pads.
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

    # Guard/collector-ring check (#199 case 2): a block with a *closed* ring
    # drawn around it (any TAP_*/COLL_* port reported, no GAP_* opening)
    # cannot be reached at any other port without the route crossing the
    # ring's own metal loop -- on the way in for a destination pin, or on the
    # way out for a source pin, since the ring fully encloses the block either
    # way. Only meaningful for distinct blocks: a self-net's two ports already
    # both sit inside the same ring, so it draws no *additional* ring crossing.
    #
    # A block whose ring declares an opening (params.ring_gap_side, #434) is
    # not rejected here: it is collected instead, and the drawn backbone is
    # checked against the ring's own geometry below, once `points` exists --
    # the route is allowed only if it actually passes through that opening.
    ring_pins: list[tuple[dict[str, Any], dict[str, Any]]] = []
    if pin_a["block"] != pin_b["block"]:
        for pin, block in ((pin_a, block_a), (pin_b, block_b)):
            if not _block_has_ring_taps(block) or pin["port"].startswith(
                _RING_TAP_PORT_PREFIXES
            ):
                continue
            if _ring_gap_ports(block):
                ring_pins.append((pin, block))
                continue
            return {
                "routed": False,
                "route_length_um": None,
                "points_um": None,
                "reason": (
                    f"block '{pin['block']}' has a closed guard/collector ring "
                    f"(reports a TAP_*/COLL_* port and no GAP_* opening) -- a "
                    f"route to its non-tap port '{pin['port']}' would cross the "
                    "ring's own metal loop and merge this net with the ring's "
                    "tap net; route to the ring's own tap port instead, "
                    "regenerate the block with a routing opening in the ring "
                    "(params.ring_gap_side/ring_gap_um), or regenerate it with "
                    "add_guard_ring/add_collector_ring: false"
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

    # Recessed same-direction vertical ports (#461): a gate landing pad now
    # sits *above* its own gate port, so a port facing +y no longer sits on
    # its block's top edge -- it is inset by the pad's own height. Two such
    # ports bussed across a row connect through a horizontal jog; left at the
    # default one-width stub that jog runs at pad height, straight through
    # both blocks' poly. Lift each stub so the jog clears both blocks' tops
    # (or bottoms, for -y). Scoped to ring-free blocks: a ringed block routes
    # through its declared gap at port level, never over its own top. For the
    # pre-#461 geometry (gate port exactly on the block edge) this reduces to
    # the default stub, so no existing route changes.
    stub_a_um = stub_b_um = stub_um
    if (
        pin_a["block"] != pin_b["block"]
        and dir_a == dir_b
        and va[0] == 0
        and not _block_has_ring_taps(block_a)
        and not _block_has_ring_taps(block_b)
    ):
        bbox_a = placed_bboxes_um[pin_a["block"]]
        bbox_b = placed_bboxes_um[pin_b["block"]]
        if dir_a == 90:
            jog_y = max(bbox_a["y1"], bbox_b["y1"]) + stub_um
            stub_a_um = max(stub_um, jog_y - a[1])
            stub_b_um = max(stub_um, jog_y - b[1])
        else:  # dir 270
            jog_y = min(bbox_a["y0"], bbox_b["y0"]) - stub_um
            stub_a_um = max(stub_um, a[1] - jog_y)
            stub_b_um = max(stub_um, b[1] - jog_y)

    points = manhattan_backbone(
        a, dir_a, b, dir_b, stub_um, stub_a_um=stub_a_um, stub_b_um=stub_b_um
    )
    margin_eps_um = 1e-6

    # Ring-opening check (#434): for every pin on a block whose ring declares
    # an opening, the drawn backbone must reach that pin *through* the opening
    # -- every other crossing of the ring's metal is the same short the
    # closed-ring check above rejects.
    for pin, block in ring_pins:
        conflict = _ring_gap_route_conflict(
            points,
            block,
            offsets_um[pin["block"]],
            placed_bboxes_um[pin["block"]],
            width_um,
        )
        if conflict is not None:
            return {
                "routed": False,
                "route_length_um": None,
                "points_um": None,
                "reason": conflict,
            }

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

    # Self-net pad-crossing check (#433): the whole-block bbox check below
    # skips a self-net's own block entirely (a same-block net's backbone is,
    # by construction, always inside its own block's bbox -- that check would
    # otherwise reject every self-net). But skipping it also means nothing
    # else was checking whether the backbone runs straight over one of that
    # block's *other* pads -- exactly what happens bussing an array's unit
    # devices (e.g. chaining a bjt_array's emitters): a same-layer pad in the
    # backbone's path shorts to it, silently, since a self-net was never
    # compared against the block's own ports[] at all. Approximate each other
    # port on this block as a square pad footprint (side length its own
    # reported ``width_um``, centered on its position), *inflated* by the
    # route's own trace half-width on every side -- ``_segment_bbox_interior_
    # overlap_um`` treats a backbone segment as a zero-width centerline, but
    # the wire actually drawn is ``width_um`` wide, so a centerline that
    # merely passes within ``width_um / 2`` of a pad's edge still draws metal
    # on top of it. This Minkowski-sum inflation is what makes the check
    # actually catch pads much narrower than the route (e.g. a bjt_array
    # base contact's reported ``width_um`` alone is too small to reach a jog
    # stubbed out by the route's own width -- only their sum is). Reject the
    # route if the backbone overlaps this inflated footprint's interior --
    # mirroring the bbox-interior accounting above, just against a pad
    # instead of a whole block.
    #
    # Same-direction degenerate-jog check (#453): the inflated-footprint test
    # above models each other port as a square of side its *reported*
    # ``width_um``. For an array unit's own pad that badly *under*-estimates the
    # real drawn metal in the port's facing direction -- e.g. a bjt_array
    # base-tie tap draws li1 metal several times taller than its reported
    # ``width_um`` (the reported width is roughly the contact size, not the pad
    # extent). When both pins face the *same* direction and share the
    # coordinate along that facing axis (same row for a vertical facing, same
    # column for a horizontal one), ``manhattan_backbone()`` collapses to a
    # single straight jog lifted just one stub width (``width_um``) to the
    # ports' outward side -- so a route wide enough that the jog clears the
    # under-sized reported square still plows straight through the real pad of
    # any intervening same-facing port. That is exactly the reproduction in
    # #453 (bussing two same-row north-facing bjt_array emitters across the
    # intervening unit's north-facing base-tie pad composed ``routed: true``
    # and DRC-clean while extraction showed the whole array's shared base node
    # absorbed into the emitter net). Treat any other same-layer port that
    # faces the same direction and sits strictly between the two pins along the
    # perpendicular axis (on the same row/column) as crossed: its pad opens
    # toward the jog, so bussing across it draws a silent short regardless of
    # how small its reported ``width_um`` is.
    if same_block_self_net:
        own_ports = blocks[own_a].get("ports") or {}
        own_offset = offsets_um[own_a]
        route_half_um = width_um / 2.0
        skip_port_names = {pin_a["port"], pin_b["port"]}
        facing_vertical = va[1] != 0
        # A degenerate single-jog backbone only forms when both pins face the
        # same direction *and* share their facing-axis coordinate (same row for
        # a vertical facing, same column for a horizontal one).
        same_line = (
            abs(a[1] - b[1]) < 1e-9 if facing_vertical else abs(a[0] - b[0]) < 1e-9
        )
        conservative_same_dir = dir_a == dir_b and same_line
        for other_name, other_port in own_ports.items():
            if other_name in skip_port_names or not _port_has_geometry(other_port):
                continue
            other_layer = (
                other_port["layer"]["layer"],
                other_port["layer"]["datatype"],
            )
            if route_layer is not None and other_layer != route_layer:
                continue  # a pad on a different physical layer can't short
            px = float(other_port["x_um"]) + own_offset["x"]
            py = float(other_port["y_um"]) + own_offset["y"]

            # #453: an intervening same-facing pad on the jog's own row/column
            # is crossed no matter how small its reported footprint is.
            other_dir = int(other_port.get("direction_deg", 0)) % 360
            if conservative_same_dir and other_dir == dir_a:
                if facing_vertical:
                    between = (
                        min(a[0], b[0]) < px < max(a[0], b[0]) and abs(py - a[1]) < 1e-9
                    )
                else:
                    between = (
                        min(a[1], b[1]) < py < max(a[1], b[1]) and abs(px - a[0]) < 1e-9
                    )
                if between:
                    axis = "row" if facing_vertical else "column"
                    return {
                        "routed": False,
                        "route_length_um": None,
                        "points_um": None,
                        "reason": (
                            f"self-net between two same-facing ports on the same "
                            f"{axis} jogs directly over block '{own_a}''s own "
                            f"port '{other_name}' (same facing direction, same "
                            "drawing layer) -- bussing this net across the block "
                            "would draw a silent short to that pad's real drawn "
                            "metal, which extends past its reported width_um "
                            "footprint in its facing direction; route to a "
                            "layer_role with a metal2/via stack instead, or wire "
                            "this net externally"
                        ),
                    }

            pad_w = other_port.get("width_um")
            if (
                not isinstance(pad_w, (int, float))
                or isinstance(pad_w, bool)
                or (pad_w <= 0)
            ):
                pad_w = width_um  # no reported pad size -- fall back to trace width
            half = float(pad_w) / 2.0 + route_half_um
            pad_bbox_um = {
                "x0": px - half,
                "y0": py - half,
                "x1": px + half,
                "y1": py + half,
            }
            crossed_um = sum(
                _segment_bbox_interior_overlap_um(seg_p0, seg_p1, pad_bbox_um)
                for seg_p0, seg_p1 in zip(points, points[1:], strict=False)
            )
            if crossed_um > margin_eps_um:
                return {
                    "routed": False,
                    "route_length_um": None,
                    "points_um": None,
                    "reason": (
                        f"self-net backbone crosses {crossed_um:.4g}um through "
                        f"block '{own_a}''s own port '{other_name}' on the same "
                        "drawing layer -- bussing this net across the block "
                        "would draw a silent short to that pad; route to a "
                        "layer_role with a metal2/via stack instead, or wire "
                        "this net externally"
                    ),
                }

    # Self-net drawn-metal check (#453/#469): check 3 above models each other
    # port as a square built from its *reported* ``width_um``, which is a
    # port's contact/access size -- not the extent of the pad metal drawn
    # around it. A bjt_array base tie, for instance, reports ``width_um:
    # 0.22`` (``CONTACT_SIZE_UM``) for a pad whose drawn local metal is
    # 0.42um x 0.68um. So the modelled square (and even its same-direction
    # same-row/column fallback, which only fires for a degenerate single-jog
    # backbone) systematically under-states the real obstacle and misses any
    # short outside that narrow shape -- notably a same-facing pair on
    # *different* rows/columns, or a route wide enough to reach an adjacent
    # row's pad. Compare the route's *drawn* metal against the block's own
    # *drawn* shapes on the route layer instead: every merged shape except
    # the two the endpoints land on is an obstacle, and overlapping one
    # (positive area -- an edge touch is a spacing question for `klt drc`,
    # not a short) is the same silent short, just measured against geometry
    # the reported port model cannot see. This is independent of, and
    # composes with, check 3: either can catch a case the other misses.
    if same_block_self_net and block_geometry is not None:
        geometry = block_geometry.get(own_a)
        if geometry is not None:
            drawn = _self_net_drawn_short(
                points,
                geometry,
                a,
                b,
                width_um,
                blocks[own_a].get("ports") or {},
                offsets_um[own_a],
            )
            if drawn is not None:
                overlap_um2, crossed_names = drawn
                if not crossed_names:
                    where = "drawn geometry (no port of its own sits on it)"
                else:
                    noun = "port" if len(crossed_names) == 1 else "ports"
                    where = f"{noun} " + ", ".join(f"'{n}'" for n in crossed_names)
                return {
                    "routed": False,
                    "route_length_um": None,
                    "points_um": None,
                    "reason": (
                        f"self-net's drawn {width_um}um metal overlaps "
                        f"{overlap_um2:.4g}um^2 of block '{own_a}''s own drawn "
                        f"pad metal on the route layer ({where}) -- bussing "
                        "this net across the block would draw a silent short "
                        "to that pad (its drawn metal is larger than the "
                        "contact size its port reports); route to a layer_role "
                        "with a metal2/via stack instead, or wire this net "
                        "externally"
                    ),
                }

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

    # Via-drop resolution (#454, check 5 -- see docstring): only consulted
    # when both a route_layer and an extraction_deck are given (pre-#454
    # callers that pass neither draw exactly as before, no via-drop). For
    # each endpoint whose own reported layer differs from route_layer, either
    # resolve the connecting via (drop needed and available), find nothing to
    # do (not a metals-stack level -- an unrelated role, unchanged legacy
    # behavior), or reject the whole net as unroutable (a drop is needed but
    # not resolvable).
    via_drops: list[dict[str, Any]] = []
    if route_layer is not None and extraction_deck is not None:
        for pin, port, pos in ((pin_a, port_a, a), (pin_b, port_b, b)):
            port_layer = _port_own_layer(port)
            if port_layer is None:
                continue  # no reported layer -- draw directly, legacy behavior
            via_layer, drop_error = _resolve_via_drop_layer(
                extraction_deck, route_layer, port_layer
            )
            if drop_error is not None:
                return {
                    "routed": False,
                    "route_length_um": None,
                    "points_um": None,
                    "reason": (
                        f"pin '{pin['port']}' on block '{pin['block']}' is drawn "
                        f"on layer {port_layer}, which routing.layer_role's "
                        f"{route_layer} cannot reach: {drop_error}"
                    ),
                }
            if via_layer is not None:
                via_drops.append(
                    {
                        "x_um": pos[0],
                        "y_um": pos[1],
                        "via_layer": via_layer,
                        "port_layer": port_layer,
                    }
                )

    return {
        "routed": True,
        "route_length_um": _polyline_length_um(points),
        "points_um": points,
        "via_drops": via_drops,
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
    extraction_deck: ExtractionDeck | None = None
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
        # Resolved once for the whole request (issue #454) -- route_two_pin's
        # via-drop check (5) consults this same ExtractionDeck.metals/.vias
        # stack per net, never a second, private via table.
        extraction_deck = get_extraction_deck(_pdk_family(pdk_info["variant"]))

    # Drawn-geometry obstacles for the self-net drawn-metal check (#453/#469),
    # read lazily and cached per block: only a *self*-net needs them (every
    # other net is already covered by the whole-block bbox check), and only
    # once per block however many self-nets land on it.
    block_geometry_cache: dict[str, dict[str, Any] | None] = {}

    def _block_geometry_for(block_id: str) -> dict[str, dict[str, Any] | None]:
        if route_layer is not None and block_id not in block_geometry_cache:
            block_geometry_cache[block_id] = read_block_layer_geometry(
                block_id, blocks[block_id], offsets_um[block_id], route_layer
            )
        return block_geometry_cache

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
            pins[0],
            pins[1],
            blocks,
            offsets_um,
            placed_bboxes_um,
            width_um,
            route_layer,
            extraction_deck,
            (
                _block_geometry_for(pins[0]["block"])
                if pins[0]["block"] == pins[1]["block"]
                else None
            ),
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
                    "via_drops": result.get("via_drops", []),
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

    Routed nets (``routed_geometry``: a list of ``{net, points_um, width_um,
    via_drops}``) are drawn as native :class:`kdb.Path` shapes directly on the
    composed top cell, on ``route_layer`` (a ``(layer, datatype)`` pair) --
    top-level metal, not inside any block's sub-cell. When ``label_layer`` is
    given (resolved by :func:`_resolve_label_layer`), each routed net also
    gets one :class:`kdb.Text` label -- named after its own ``net`` field --
    on that layer, at the arc-length midpoint of its drawn path
    (:func:`_polyline_midpoint_um`), so `klt extract`'s label-recognition
    convention (``metals[i]``/``metal_labels[i]`` -- see
    :class:`klayout_tools.decks.ExtractionDeck`) promotes the net to a named
    ``.SUBCKT`` pin instead of an anonymous one (#200).

    Each entry's ``via_drops`` (resolved by :func:`route_two_pin`'s check 5,
    issue #454) is a list of ``{x_um, y_um, via_layer, port_layer}`` -- one
    per endpoint that needed to drop from ``route_layer`` down to its own
    pin's layer. Each drop draws a via square (``_VIA_DROP_SIZE_UM``) on
    ``via_layer`` plus a landing-pad square (``_VIA_LANDING_SIZE_UM``, sized
    independently of the route's own trace width so the via's enclosure
    requirement holds regardless) on *both* ``route_layer`` and the pin's own
    layer, all centered on the pin's exact composed-frame position -- the
    same position the backbone's own drawn ``kdb.Path`` already terminates
    at, so the landing pad always overlaps (and merges with) both the
    backbone and the block's own existing pad on that layer.

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

            # Via-drops (#454): each entry drops the backbone (route_layer)
            # down to a target pin's own layer at exactly that pin's own
            # position -- a via square on `via_layer`, plus a landing-pad
            # square on *both* route_layer and the pin's own layer
            # (_VIA_LANDING_SIZE_UM, independent of the route's own trace
            # width) so the via's enclosure requirement holds regardless of
            # how thin routing.width_um is. The backbone's own Path already
            # terminates exactly at this same point (manhattan_backbone's
            # endpoints are the raw pin positions), so the route_layer
            # landing pad always overlaps -- and merges with -- the trace.
            via_half_dbu = int(round((_VIA_DROP_SIZE_UM / 2.0) / dbu))
            landing_half_dbu = int(round((_VIA_LANDING_SIZE_UM / 2.0) / dbu))
            for drop in route.get("via_drops", []):
                via_pair = drop["via_layer"]
                port_pair = drop["port_layer"]
                via_layer_index = layout.layer(via_pair[0], via_pair[1])
                port_layer_index = layout.layer(port_pair[0], port_pair[1])
                cx = int(round(drop["x_um"] / dbu))
                cy = int(round(drop["y_um"] / dbu))
                top.shapes(via_layer_index).insert(
                    kdb.Box(
                        cx - via_half_dbu,
                        cy - via_half_dbu,
                        cx + via_half_dbu,
                        cy + via_half_dbu,
                    )
                )
                landing_box = kdb.Box(
                    cx - landing_half_dbu,
                    cy - landing_half_dbu,
                    cx + landing_half_dbu,
                    cy + landing_half_dbu,
                )
                top.shapes(layer_index).insert(landing_box)  # route_layer side
                top.shapes(port_layer_index).insert(landing_box)  # pin's own side

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
