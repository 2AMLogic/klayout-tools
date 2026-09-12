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
  (``"row"``, see :func:`compute_row_offsets`), a caller-declared
  ``placement.origins_um`` per block id (``"explicit"``, see
  :func:`resolve_explicit_offsets`, #321), or a repeated single-block R rows x
  C cols regular tiling (``"array"``, see :func:`_parse_array_placement`,
  #1053) -- and write a composed GDS with each block's own top cell
  instantiated as a translated sub-cell instance under one new top cell (an
  ``"array"``-placed block instead gets one hierarchical
  ``kdb.CellInstArray`` row/column-vector instance covering every tile, never
  ``rows*cols`` separate inserts). Each ``blocks[]`` entry also carries an
  optional ``orientation`` (``"none"`` (default) / ``"mirror_x"`` /
  ``"mirror_y"`` / ``"rotate_180"``, #1166) applied about that block's own
  local origin *before* ``offset_um`` translates it -- this is what lets two
  same-facing blocks (e.g. a CMOS inverter's nfet/pfet pair, both drawn with
  their drain on the same edge) be mirrored to face each other so their
  shared net can route at all; see :func:`_apply_orientation_um` and
  :data:`_ORIENTATION_KDB_ARGS`. It composes with every placement strategy
  identically (a per-block attribute, not a placement-strategy one) --
  ``"explicit"`` still performs no overlap validation of its own (see
  :func:`resolve_explicit_offsets`'s docstring), and ``"array"`` still takes
  exactly one ``blocks[]`` entry and applies that one entry's orientation
  uniformly to every tile (no *per-tile* override), and leaves per-tile
  ``connectivity[]``/``pins[]`` routing as a follow-on question this module
  does not answer -- see :func:`_parse_array_placement`'s docstring.
* **Routing** (new this phase): for every 2-pin ``connectivity[]`` net, draw
  a Manhattan metal path (backbone -> corner bends -> straight fill; see
  :func:`manhattan_backbone`) between the two named ports on the resolved
  ``routing.layer_role`` layer at ``routing.width_um`` width, built natively
  as a ``pya.Path``. ``nets[]`` reports ``routed`` and ``route_length_um``
  per net; a net the router cannot connect -- a required jog through an
  inter-block channel narrower than ``routing.width_um``, or a route that
  would cross a guard/collector ring's own tap loop or plow through a
  block's interior (e.g. a same-facing port pair reaching a pin on a block's
  far side -- :func:`route_two_pin`, #199) -- is reported in
  ``unrouted_nets[]`` rather than failing the whole request or silently
  drawing a short. Unrelated blocks sitting between the two pins in a longer
  row are *routed around* rather than reported: the backbone retries on up
  to two alternate lanes clear of them before the net is called unroutable
  (#1167, see :func:`route_two_pin`'s "Bounded detour search"). A non-empty
  ``unrouted_nets[]`` with every block placed is a *partial success* (exit
  code 3; see ``cli/gen_compose_cmd.py`` and the spike's "Proposed exit
  codes").
* **Bundle (>2-pin) routing** (issue #1073, the increment the spike's section
  5 item 2 reserved for "once two-pin routing is proven against a real
  block"): a ``connectivity[]`` net with three or more pins -- a shared
  supply/ground rail, a bias line, a clock, any fanout node, i.e. the
  majority of a real circuit's connectivity -- is routed as a spanning tree
  of two-pin legs by :func:`route_bundle`, nearest pair first. Every leg goes
  through :func:`route_two_pin` unchanged, so all of its routability checks
  apply per leg; a leg one of them rejects is skipped in favour of the next
  candidate that would join the same two parts of the net. ``nets[].legs[]``
  reports every drawn leg (and, for a net that could not be fully connected,
  every attempted one with its own rejection reason). A net whose pins cannot
  all be joined still gets every leg the search *did* accept drawn (issue
  #1169) -- only the stranded pins (and any rejected candidate reaching them)
  are left undrawn; ``nets[].status`` (``"routed"``/``"partial"``/
  ``"unrouted"``) tells the caller which case it is, since both a partial and
  a fully-unrouted net report ``routed: false``.
* **Via-drop routing** (issue #454, re-raising #433's Ask options 1/2; a
  multi-level ladder since issue #1567): a family whose curated extraction
  deck declares a second (or third, ...) routing-metal level (e.g. sky130's
  ``"metal2"``/met1) can be selected as ``routing.layer_role`` even though
  every ``klt gen`` block's own pads are drawn on the base ``"metal"`` role
  -- :func:`route_two_pin` drops the backbone back down to each target pin's
  own layer via the connecting via (sky130's ``"via1"``/mcon), or the full
  chain of connecting vias when the two are more than one metals-stack level
  apart, exactly at that pin's position, so the backbone itself never runs
  across another pad on the pad layer. This is what makes a same-block bus
  (e.g. chaining a matched array's unit terminals) routable without either
  accepting a same-layer short or failing #433's self-net pad-crossing
  rejection -- see :func:`_resolve_via_drop_layer`.
* **Cross-block bus routing** (issue #1168): via-drop routing (above) moves
  the *whole* composition to a second metal, even for nets that never needed
  it. ``routing.cross_block_layer_role`` instead names a second, higher
  metal role only a same-block self-net leg falls back to when it would
  otherwise short across another of that block's own pads on the primary
  ``routing.layer_role`` -- resolved and via-hop-validated by
  :func:`_resolve_cross_block_route_layer`, and applied per leg by
  :func:`route_two_pin`'s same-drawing-layer-short retry. Every other net in
  the same request keeps drawing on ``routing.layer_role`` unchanged. Since
  issue #1393 a *second* same-block self-net that also needs that fallback
  is no longer rejected for colliding with the first: the leg is retried on
  a bounded set of lanes looped clear of the block's own bbox
  (:func:`_self_net_cross_layer_lane_waypoints_um`), the same pattern the
  #1167 detour search applies to an unrelated block in the channel.
* **Blocks this command did not generate** (issue #1189): a ``blocks[]``
  entry names its geometry source in exactly one of two ways. ``generator_report``
  is a ``klt`` verb's own JSON response (``klt gen``, ``klt draw``, or -- since
  #1189 -- ``klt gen-compose`` itself, whose response now reports
  ``generator: "gen-compose"`` and a ``ports[]`` promoted from its own
  ``pins[]``, which is what makes composition *nest* rather than being one
  flat level; see :func:`promote_composed_ports`). ``cell`` instead names a
  cell that **already exists** in a stream -- a PDK standard cell, a
  hand-drawn library cell -- as ``{"gds_path", "cell_name", "ports": [...],
  "bbox_um": {...}}``, with ``bbox_um`` read straight from the stream when
  omitted (:func:`read_cell_bbox_um`, the one deliberate exception to the
  "never re-derive placement math from the stream" rule below: a cell nobody
  generated never *reported* a bbox to copy). See :func:`_parse_cell_block`.
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
block, and never touches placement. A ``blocks[].cell`` entry (#1189) is the
one narrow exception on the placement side: when (and only when) it declares
no ``bbox_um`` of its own, its bbox is read from the stream's own cell
(:func:`read_cell_bbox_um`) -- there is no report to consume for a cell no
``klt`` verb generated, and requiring the caller to hand-transcribe one is
exactly the ergonomics gap #1189 filed. A ``generator_report`` block's bbox is
still never read from its stream.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any

from ._layout import write_layout
from ._paths import _resolve_relative
from .decks import (
    ExtractionDeck,
    UnknownDeckError,
    get_deck,
    get_extraction_deck,
    get_nominal_dbu,
)
from .gen import (
    CONTACT_SIZE_UM,
    ENCLOSURE_MARGIN_UM,
    GenError,
    _pdk_family,
)

# The rest of this module never calls these directly -- they are re-exported
# purely so `klayout_tools.gen_compose.<name>` keeps working (the test suite
# imports/monkeypatches several of them by that path from before this split,
# issue #1708). The redundant `X as X` aliases mark them as intentional
# re-exports (mirrors `extract.py`'s `_sanitize_instance_name`/`ExtractError`/
# `def_net_instance_pins` re-exports from `extract_abstract.py`/
# `extract_spef.py`).
from .gen_compose_routing import (
    _MAX_SAME_BLOCK_CROSS_LAYER_LANES as _MAX_SAME_BLOCK_CROSS_LAYER_LANES,
)
from .gen_compose_routing import (
    _RING_GAP_PORT_PREFIX,
    _declare_only_bundle_result,
    _drawn_leg_footprint_region,
    _min_width_um_for_layer,
    _pad_self_notch_violation_um,
    _polyline_midpoint_um,
    _resolve_cross_block_route_layer,
    _resolve_label_layer,
    _resolve_route_layer,
    _ring_port_side,
    read_block_layer_geometry,
    route_bundle,
)
from .gen_compose_routing import _cleanup_points as _cleanup_points
from .gen_compose_routing import _endpoint_stub_widen_um as _endpoint_stub_widen_um
from .gen_compose_routing import _pin_ref as _pin_ref
from .gen_compose_routing import _resolve_via_drop_layer as _resolve_via_drop_layer
from .gen_compose_routing import (
    _segment_bbox_interior_overlap_um as _segment_bbox_interior_overlap_um,
)
from .gen_compose_routing import (
    _self_net_cross_layer_lane_waypoints_um as _self_net_cross_layer_lane_waypoints_um,
)
from .gen_compose_routing import manhattan_backbone as manhattan_backbone
from .gen_compose_routing import route_two_pin as route_two_pin
from .pdk import PdkNotFoundError, find_pdk

#: Contract identifier for the request envelope (spike section 2).
REQUEST_SCHEMA = "klt.gen_compose.request/1"

#: Bumped only on a non-additive (breaking) change to this command's own
#: response JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: Placement strategies implemented at this phase. ``"row"`` computes a
#: single horizontal row from each block's own ``bbox_um`` plus a shared
#: ``spacing_um``; ``"explicit"`` instead takes a caller-declared per-block
#: origin (``placement.origins_um``, #321) -- see :func:`resolve_explicit_offsets`;
#: ``"array"`` repeats one block on a regular R rows x C cols grid
#: (``placement.rows``/``cols``/``row_pitch_um``/``col_pitch_um``/
#: ``origin_um``, #1053) -- see :func:`_parse_array_placement`.
#: ``"grid"`` is a *different*, still-unimplemented feature reserved by the
#: accepted spike for a later phase (a row-wrap layout of *distinct* blocks,
#: see ``docs/design/gen-composition-spike.md`` section 5) -- deliberately
#: not the name used for this module's repeated-single-block array strategy,
#: to avoid colliding with that reservation.
SUPPORTED_PLACEMENT_STRATEGIES = {"row", "explicit", "array"}

#: Unit outward vector (dx, dy) for each orthogonal ``direction_deg`` a
#: ``klt gen`` port reports. Ports only ever face an axis (0/90/180/270 --
#: see ``gen.py``'s generators), so the router never has to snap a diagonal.
_DIRECTION_VECTORS: dict[int, tuple[int, int]] = {
    0: (1, 0),
    90: (0, 1),
    180: (-1, 0),
    270: (0, -1),
}

#: Supported ``blocks[].orientation`` values (#1166) -- a block's own
#: mirror/rotation, applied about that block's own local (pre-translation)
#: origin *before* ``offset_um`` translates it into the composed frame.
#: ``"mirror_x"`` negates local ``x`` (a horizontal flip -- a block's
#: right-edge port moves to its left edge, and vice versa; this is the
#: minimum case that unblocks a CMOS inverter's shared-drain net, per
#: #1164's root cause #1), ``"mirror_y"`` negates local ``y`` (a vertical
#: flip), ``"rotate_180"`` negates both. See :func:`_apply_orientation_um`
#: for the point transform and :data:`_ORIENTATION_KDB_ARGS` for the
#: equivalent ``kdb.Trans`` construction every geometry-writing consumer
#: (:func:`_write_composed_gds`, :func:`read_block_layer_geometry`) applies
#: to actually-drawn shapes, so a block's reported metadata (``bbox_um``,
#: ``ports[]``) never disagrees with its drawn geometry.
_ORIENTATIONS = frozenset({"none", "mirror_x", "mirror_y", "rotate_180"})

#: ``kdb.Trans(rot, mirrx, x, y)`` arguments -- ``rot`` a 0..3 count of
#: 90-degree CCW rotation steps, ``mirrx`` whether to mirror at the x-axis
#: *before* that rotation is applied -- producing the identical point
#: transform as :func:`_apply_orientation_um` for each orientation. Verified
#: against ``klayout.db.Trans``'s own semantics: ``rot=0, mirrx=True`` maps
#: ``(x, y) -> (x, -y)`` (``"mirror_y"``), ``rot=2, mirrx=True`` maps
#: ``(x, y) -> (-x, y)`` (``"mirror_x"``), and ``rot=2, mirrx=False`` maps
#: ``(x, y) -> (-x, -y)`` (``"rotate_180"``).
_ORIENTATION_KDB_ARGS: dict[str, tuple[int, bool]] = {
    "none": (0, False),
    "mirror_x": (2, True),
    "mirror_y": (0, True),
    "rotate_180": (2, False),
}

#: ``direction_deg`` -> ``direction_deg`` remap for each orientation
#: (#1166): a mirrored/rotated block's ports face a different absolute
#: direction even though the port's own name/role is unchanged (e.g. a
#: ``mirror_x``'d block's drain, still named ``D``, now faces ``-x`` instead
#: of ``+x``). Applied once, in :func:`_parse_blocks`, to every port's own
#: ``direction_deg`` -- every downstream consumer (:func:`_DIRECTION_VECTORS`
#: lookups, ring-side classification, stub-widen) then reads an
#: already-correct direction without repeating this remap itself.
_ORIENTATION_DIRECTION_MAP: dict[str, dict[int, int]] = {
    "none": {0: 0, 90: 90, 180: 180, 270: 270},
    "mirror_x": {0: 180, 90: 90, 180: 0, 270: 270},
    "mirror_y": {0: 0, 90: 270, 180: 180, 270: 90},
    "rotate_180": {0: 180, 90: 270, 180: 0, 270: 90},
}


def _apply_orientation_um(x: float, y: float, orientation: str) -> tuple[float, float]:
    """Transform one local-frame point per a block's own ``orientation``
    (#1166), applied about that block's own origin -- *before* ``offset_um``
    translates it into the composed frame. See :data:`_ORIENTATIONS`'s
    docstring for the exact per-value semantics.
    """
    if orientation == "mirror_x":
        return -x, y
    if orientation == "mirror_y":
        return x, -y
    if orientation == "rotate_180":
        return -x, -y
    return x, y


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


def array_placement_bbox_um(
    bbox_um: dict[str, float], array_params: dict[str, Any]
) -> dict[str, float]:
    """Bounding box of the whole placed array for ``placement.strategy:
    "array"`` (#1053) -- every one of ``rows * cols`` tile instances, not
    just the base (first) tile.

    Every tile shares the array-placed block's own (pre-translation)
    ``bbox_um`` (translated only, no rotation -- exactly like ``"row"``/
    ``"explicit"``), growing ``cols`` steps of ``col_pitch_um`` along ``+x``
    and ``rows`` steps of ``row_pitch_um`` along ``+y`` from ``origin_um``
    (the base tile's own ``offset_um`` -- row 0, col 0). This is the closed
    form of unioning every tile's own translated bbox (mirrors
    :func:`_union_bbox`, without materialising ``rows * cols`` intermediate
    boxes): the array only ever grows in the ``+x``/``+y`` direction from
    ``origin_um`` -- a caller wanting the array to grow the other way must
    adjust ``origin_um`` itself, mirroring ``"row"``/``"explicit"``'s own
    translation-only semantics (no auto-centering).

    ``array_params`` is the dict :func:`_parse_array_placement` returns
    (``rows``, ``cols``, ``row_pitch_um``, ``col_pitch_um``, ``origin_um``).
    """
    origin = array_params["origin_um"]
    rows = array_params["rows"]
    cols = array_params["cols"]
    row_pitch_um = array_params["row_pitch_um"]
    col_pitch_um = array_params["col_pitch_um"]
    return {
        "x0": bbox_um["x0"] + origin["x"],
        "y0": bbox_um["y0"] + origin["y"],
        "x1": bbox_um["x1"] + origin["x"] + (cols - 1) * col_pitch_um,
        "y1": bbox_um["y1"] + origin["y"] + (rows - 1) * row_pitch_um,
    }


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


def _bbox_clearance_um(bbox_a: dict[str, float], bbox_b: dict[str, float]) -> float:
    """Axis-aligned clearance between two placed bboxes (#692).

    ``0.0`` when the two bboxes overlap or touch on *both* axes (so there is
    no gap to report at all). When they're separated on exactly one axis, the
    clearance is the plain gap along that axis. When they're separated
    diagonally -- neither bbox's x-range nor y-range overlaps the other's --
    the nearest points are the two facing corners, so the clearance is the
    Euclidean distance between them rather than either axis gap alone.

    Used only by the ``"explicit"`` placement clearance advisory below;
    :func:`_ring_gap_route_conflict` computes a related but distinct
    route-vs-ring-opening clearance and is not reused here.
    """
    gap_x = max(bbox_a["x0"] - bbox_b["x1"], bbox_b["x0"] - bbox_a["x1"], 0.0)
    gap_y = max(bbox_a["y0"] - bbox_b["y1"], bbox_b["y0"] - bbox_a["y1"], 0.0)
    if gap_x > 0.0 and gap_y > 0.0:
        return math.hypot(gap_x, gap_y)
    return max(gap_x, gap_y)


def _explicit_placement_clearance_warnings(
    order: list[str],
    blocks: dict[str, dict[str, Any]],
    placed_bboxes_um: dict[str, dict[str, float]],
) -> list[str]:
    """Advisory-only clearance check for ``placement.strategy: "explicit"``
    (#692).

    For every ordered pair of distinct blocks ``(A, B)`` where ``A``'s own
    ``generator_report.drc_hints.min_spacing_um`` (parsed by
    :func:`_parse_blocks` into ``blocks[block_id]["min_spacing_um"]``) is
    greater than zero, compares that declared minimum against the actual
    placed clearance between ``A`` and ``B`` (:func:`_bbox_clearance_um`). A
    caller who places a block flush against (or overlapping) a
    ``guard_ring``-generated neighbour gets a composed GDS that passes `klt
    drc` clean -- two same-layer shapes placed with zero clearance merge into
    one polygon, which is not an illegal *shape* by any spacing rule -- and
    the resulting short only otherwise surfaces later via `klt extract`'s
    `merged_net_labels` diagnostic. This warning surfaces it at compose time
    instead.

    Never raises and never blocks composition -- geometry stays advisory here
    exactly as :func:`resolve_explicit_offsets`'s docstring describes;
    ``"row"`` placement is intentionally out of scope (its own uniform
    ``spacing_um`` does not have the same silently-flush ergonomics trap).
    """
    clearance_warnings: list[str] = []
    for owner_id in order:
        min_spacing_um = blocks[owner_id].get("min_spacing_um", 0.0)
        if not min_spacing_um > 0.0:
            continue
        owner_bbox = placed_bboxes_um[owner_id]
        for other_id in order:
            if other_id == owner_id:
                continue
            clearance_um = _bbox_clearance_um(owner_bbox, placed_bboxes_um[other_id])
            if clearance_um < min_spacing_um:
                clearance_warnings.append(
                    f"block '{other_id}' is placed {clearance_um:.2f}um from "
                    f"block '{owner_id}' (strategy: explicit), closer than "
                    f"{owner_id}'s own declared drc_hints.min_spacing_um of "
                    f"{min_spacing_um:.2f}um"
                )
    return clearance_warnings


# Tolerance (um) for comparing a block's declared ``bbox_um`` against its own
# real, stream-read ``kdb.Cell.dbbox()`` in
# :func:`_declared_bbox_overlap_warnings` below -- large enough to absorb dbu
# quantization noise between an analytically-computed declared bbox and the
# same geometry's dbu-rounded drawn extent (dbu is typically 0.001um, so
# quantization alone can differ by up to half a dbu step), far too small to
# ever mask a real "declared bbox undershoots the block's own guard ring/seal
# ring" discrepancy (#1679), which is always at least tens of nm.
_BBOX_REALITY_TOLERANCE_UM = 1e-4


def _bbox_contains(outer: dict[str, float], inner: dict[str, float]) -> bool:
    """Whether ``outer`` fully contains ``inner`` (within
    :data:`_BBOX_REALITY_TOLERANCE_UM`)."""
    tol = _BBOX_REALITY_TOLERANCE_UM
    return (
        inner["x0"] >= outer["x0"] - tol
        and inner["y0"] >= outer["y0"] - tol
        and inner["x1"] <= outer["x1"] + tol
        and inner["y1"] <= outer["y1"] + tol
    )


def _block_real_placed_bbox_um(
    block_id: str, block: dict[str, Any], offset_um: dict[str, float]
) -> dict[str, float] | None:
    """A block's own *real* placed bbox -- read straight from its stream via
    :func:`read_cell_bbox_um`, oriented and translated exactly like its
    declared ``bbox_um`` already is -- or ``None`` when it cannot be read
    (bad ``gds_path``/``cell_name``, or an empty cell).

    Never raises: this is an advisory cross-check (#1679), and a block whose
    stream genuinely cannot be read fails identically -- loudly, as a
    :class:`GenComposeError` -- later in :func:`compose` regardless (when its
    geometry is actually copied into the composed output), so silently
    skipping the advisory here for that block costs nothing.
    """
    try:
        real_bbox_um = read_cell_bbox_um(
            block["gds_path"], block["cell_name"], where=f"block '{block_id}'"
        )
    except GenComposeError:
        return None
    orientation = block.get("orientation", "none")
    if orientation != "none":
        real_bbox_um = _orient_bbox_um(real_bbox_um, orientation)
    return _translate_bbox(real_bbox_um, offset_um)


def _load_block_cell(block_id: str, block: dict[str, Any]) -> tuple[Any, Any]:
    """Load ``block``'s GDS and resolve its cell, raising ``GenComposeError``
    on a bad path or a missing cell name.

    Shared by :func:`read_block_layer_geometry` and
    :func:`_read_all_block_layers_geometry` -- both need the same
    read-then-resolve step before diverging on which layer(s) to read.
    Returns ``(src_layout, src_cell)`` as a ``(kdb.Layout, kdb.Cell)`` pair.
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
            f"'{src_cell_name}' (from its {_block_cell_name_source(block)})"
        )
    return src_layout, src_cell


def _read_all_block_layers_geometry(
    block_id: str, block: dict[str, Any], offset_um: dict[str, float]
) -> dict[tuple[int, int], dict[str, Any]]:
    """Every layer ``block`` draws on, read into the composed frame -- the
    same per-layer read :func:`read_block_layer_geometry` performs for one
    caller-named layer, generalised to every layer the block's own stream
    actually has (issue #1679's real per-layer overlap check below needs to
    compare *whichever* layer a leaky block's excess geometry and a
    neighbour's placement happen to share, not one route/obstacle layer
    named in advance).

    Returns ``{(layer, datatype): {"region": kdb.Region, "dbu": float}}``,
    omitting any layer with no shapes on ``block``'s own cell (empty after
    ``region.merge()``) -- mirrors :func:`read_block_layer_geometry`'s own
    ``None`` return for an absent/empty layer, just keyed by every present
    layer instead of gated on one.
    """
    import klayout.db as kdb

    src_layout, src_cell = _load_block_cell(block_id, block)

    dbu = src_layout.dbu
    rot, mirrx = _ORIENTATION_KDB_ARGS[block.get("orientation", "none")]
    trans = kdb.Trans(
        rot,
        mirrx,
        int(round(offset_um["x"] / dbu)),
        int(round(offset_um["y"] / dbu)),
    )

    geometry: dict[tuple[int, int], dict[str, Any]] = {}
    for layer_index in src_layout.layer_indexes():
        region = kdb.Region(src_cell.begin_shapes_rec(layer_index))
        region.merge()
        if region.is_empty():
            continue
        region.transform(trans)
        info = src_layout.get_info(layer_index)
        geometry[(info.layer, info.datatype)] = {"region": region, "dbu": dbu}
    return geometry


def _declared_bbox_overlap_warnings(
    order: list[str],
    blocks: dict[str, dict[str, Any]],
    offsets_um: dict[str, dict[str, float]],
    placed_bboxes_um: dict[str, dict[str, float]],
) -> list[str]:
    """Real per-layer geometry overlap advisory for a block whose declared
    ``bbox_um`` understates its own real drawn extent (#1679).

    :func:`_explicit_placement_clearance_warnings` above only ever compares
    *declared* ``bbox_um`` values -- accurate for a ``klt gen`` block (which
    reports its own bbox from the same geometry it just drew), but a
    ``generator_report`` block's ``bbox_um`` is trusted verbatim
    (:func:`_parse_blocks`) and never cross-checked against its own stream.
    A block whose real drawn geometry extends past its declared bbox (e.g. a
    ``klt place-and-route``-produced macro's guard/seal ring, under-reported
    by the tool that produced it) can have that excess geometry physically
    overlap a neighbour placed just outside the *declared* bbox but still
    inside the *real* one -- composing a `klt drc`-clean same-layer merge
    (not an illegal shape by any spacing rule) that only surfaces later via
    `klt extract`'s ``merged_net_labels`` diagnostic, corrupting the macro's
    own extracted connectivity.

    Scoped to blocks whose real placed bbox (:func:`_block_real_placed_bbox_um`)
    is not contained in their own declared ``placed_bboxes_um`` entry -- the
    (hopefully rare) "declared bbox lies" case this bug depends on. A block
    whose declared bbox already matches (or exceeds) its own real geometry
    contributes nothing here, so two same-footprint blocks placed flush or
    fully overlapping (a caller-declared, advisory-tolerated choice --
    :func:`resolve_explicit_offsets`'s docstring) never gains a new warning
    from this check merely for the declared overlap itself -- only an
    *undeclared* one (real geometry the caller's own bbox_um never admitted
    to) does. Applies regardless of ``placement.strategy`` (unlike the
    declared-bbox clearance check above, which is ``"explicit"``-only): the
    "row"/"array" placement math is itself computed from the same
    unreliable declared ``bbox_um``, so it is no less exposed.

    Never raises and never blocks composition, matching every other check in
    this module's "geometry is advisory" philosophy.
    """
    warnings: list[str] = []
    real_placed_bbox: dict[str, dict[str, float]] = {}
    for block_id in order:
        real_bbox = _block_real_placed_bbox_um(
            block_id, blocks[block_id], offsets_um[block_id]
        )
        if real_bbox is not None:
            real_placed_bbox[block_id] = real_bbox

    leaky_ids = [
        block_id
        for block_id, real_bbox in real_placed_bbox.items()
        if not _bbox_contains(placed_bboxes_um[block_id], real_bbox)
    ]
    if not leaky_ids:
        return warnings

    geometry_cache: dict[str, dict[tuple[int, int], dict[str, Any]]] = {}

    def _geometry_for(block_id: str) -> dict[tuple[int, int], dict[str, Any]]:
        if block_id not in geometry_cache:
            geometry_cache[block_id] = _read_all_block_layers_geometry(
                block_id, blocks[block_id], offsets_um[block_id]
            )
        return geometry_cache[block_id]

    reported_pairs: set[frozenset[str]] = set()
    for leaky_id in leaky_ids:
        leaky_real_bbox = real_placed_bbox[leaky_id]
        for other_id in order:
            if other_id == leaky_id:
                continue
            pair_key = frozenset((leaky_id, other_id))
            if pair_key in reported_pairs:
                continue
            # Cheap pre-filter: only reach for real per-layer geometry when
            # the leaky block's real bbox even overlaps/touches the other
            # block's own declared bbox -- distant blocks never do.
            if _bbox_clearance_um(leaky_real_bbox, placed_bboxes_um[other_id]) > 0.0:
                continue
            leaky_geometry = _geometry_for(leaky_id)
            other_geometry = _geometry_for(other_id)
            for layer in sorted(set(leaky_geometry) & set(other_geometry)):
                overlap = (
                    leaky_geometry[layer]["region"] & other_geometry[layer]["region"]
                )
                if overlap.is_empty():
                    continue
                declared = placed_bboxes_um[leaky_id]
                warnings.append(
                    f"block '{leaky_id}' draws real geometry on layer "
                    f"{layer[0]}/{layer[1]} that extends past its own "
                    f"declared bbox_um ({declared['x0']:.3f}, {declared['y0']:.3f})-"
                    f"({declared['x1']:.3f}, {declared['y1']:.3f}) -- that excess "
                    f"geometry overlaps block '{other_id}' there. `klt drc` will "
                    "not flag this (a zero-clearance same-layer merge is not an "
                    "illegal shape by any spacing rule), but `klt extract` will "
                    "report a spurious merged_net_labels short once the composed "
                    "output is extracted"
                )
                reported_pairs.add(pair_key)
                break
    return warnings


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


def _orient_bbox_um(bbox_um: dict[str, float], orientation: str) -> dict[str, float]:
    """``bbox_um``, transformed by a block's own ``orientation`` (#1166),
    still pre-translation (in the block's own local frame).

    Every supported orientation is an axis-aligned flip (never a diagonal
    rotation), so transforming the two opposite corners
    ``(x0, y0)``/``(x1, y1)`` and re-sorting into ``min``/``max`` is enough
    to get the new axis-aligned bbox -- e.g. ``"mirror_x"`` negates both
    corners' ``x``, which swaps which one is now the smaller (``x0``).
    """
    x0, y0 = _apply_orientation_um(bbox_um["x0"], bbox_um["y0"], orientation)
    x1, y1 = _apply_orientation_um(bbox_um["x1"], bbox_um["y1"], orientation)
    return {
        "x0": min(x0, x1),
        "y0": min(y0, y1),
        "x1": max(x0, x1),
        "y1": max(y0, y1),
    }


def _orient_port(port: dict[str, Any], orientation: str) -> dict[str, Any]:
    """One ``ports[]`` entry, transformed by a block's own ``orientation``
    (#1166): its ``x_um``/``y_um`` (if both are usable numbers -- mirrors
    :func:`_port_has_geometry`'s own tolerance for a port that reports none)
    via :func:`_apply_orientation_um`, and its ``direction_deg`` (if present)
    via :data:`_ORIENTATION_DIRECTION_MAP`. Every other field (``name``,
    ``width_um``, ``layer``, ...) is copied unchanged -- orientation moves a
    port's *position*, it never changes its *identity* or contact size.
    """
    oriented = dict(port)
    x_um, y_um = port.get("x_um"), port.get("y_um")
    if (
        not isinstance(x_um, bool)
        and isinstance(x_um, (int, float))
        and not isinstance(y_um, bool)
        and isinstance(y_um, (int, float))
    ):
        oriented["x_um"], oriented["y_um"] = _apply_orientation_um(
            float(x_um), float(y_um), orientation
        )
    direction_deg = port.get("direction_deg")
    if not isinstance(direction_deg, bool) and isinstance(direction_deg, int):
        oriented["direction_deg"] = _ORIENTATION_DIRECTION_MAP[orientation].get(
            direction_deg, direction_deg
        )
    return oriented


def _block_cell_name_source(block: dict[str, Any]) -> str:
    """Which request field a block's ``cell_name`` came from (#1189) -- used
    only to make a "no such cell in that stream" error name the field the
    caller actually wrote."""
    if block.get("source") == "cell":
        return "cell.cell_name"
    return "generator_report.cell_name"


def read_cell_bbox_um(gds_path: str, cell_name: str, where: str) -> dict[str, float]:
    """Read one existing cell's bounding box straight out of its stream (#1189).

    Returns ``{"x0", "y0", "x1", "y1"}`` -- this module's (and ``klt gen``'s)
    own bbox convention -- from ``kdb.Cell.dbbox()``, which reports the same
    box ``klt cells`` does under its ``{left, bottom, right, top}`` field
    names. Doing the translation *here* is the point: a caller placing a PDK
    library cell should never have to re-key one tool's bbox report into
    another tool's field names by hand.

    Used only for a ``blocks[].cell`` entry that declares no ``bbox_um`` of
    its own. A ``generator_report`` block's placement math is still never
    re-derived from its stream (see the module docstring) -- an
    already-generated block *reported* its bbox, so there is nothing to read;
    a pre-existing library cell never reported one to anybody, so the stream
    is the only source there is.

    Raises :class:`GenComposeError` (prefixed with ``where``) when the stream
    cannot be read, holds no cell of that name, or that cell is empty (an
    empty cell has no bounding box to read, so the caller must declare one).
    """
    import klayout.db as kdb

    layout = kdb.Layout()
    try:
        layout.read(gds_path)
    except Exception as exc:  # klayout raises RuntimeError for bad formats/paths
        raise GenComposeError(
            f"{where}: could not read cell.gds_path '{gds_path}': {exc}"
        ) from exc

    cell = layout.cell(cell_name)
    if cell is None:
        available = sorted(c.name for c in layout.each_cell())
        shown = ", ".join(available[:10]) or "(none)"
        suffix = f" (and {len(available) - 10} more)" if len(available) > 10 else ""
        raise GenComposeError(
            f"{where}: stream '{gds_path}' has no cell named '{cell_name}' -- "
            f"available: {shown}{suffix}"
        )

    dbbox = cell.dbbox()
    if dbbox.empty():
        raise GenComposeError(
            f"{where}: cell '{cell_name}' in '{gds_path}' draws no geometry, so "
            "its bounding box cannot be read from the stream -- declare "
            "cell.bbox_um explicitly"
        )
    return {
        "x0": dbbox.left,
        "y0": dbbox.bottom,
        "x1": dbbox.right,
        "y1": dbbox.top,
    }


def _parse_cell_ports(raw_ports: Any, where: str) -> list[dict[str, Any]]:
    """Validate a ``blocks[].cell.ports[]`` array (#1189).

    A ``generator_report`` block's ``ports[]`` came out of a ``klt`` verb and
    is trusted as-is (:func:`_parse_blocks` only filters for a string
    ``name``); a ``cell`` block's ports are **hand-declared** by the caller,
    who has no tool checking them, so they are validated here against the
    exact same shape ``klt gen`` emits (``docs/cli/gen.md``, "``ports[]``
    entries"): ``name`` (required, unique), and optional ``x_um``/``y_um``
    (both or neither), ``width_um`` (> 0), ``direction_deg`` (an orthogonal
    0/90/180/270 -- every consumer in this module assumes an axis-facing
    port), ``layer`` (``{layer, datatype}`` integers), ``net``.

    A port may legitimately carry no geometry at all (name only): that port
    simply cannot be routed to or labelled, exactly as an under-reported
    ``klt gen`` port cannot (see :func:`_port_has_geometry`). Returns the
    parsed port dicts in declaration order, with ``direction_deg`` normalised
    to ``int`` so :data:`_ORIENTATION_DIRECTION_MAP` can remap it.
    """
    if raw_ports is None:
        return []
    if not isinstance(raw_ports, list):
        raise GenComposeError(f"{where}.ports must be an array")

    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_port in enumerate(raw_ports):
        at = f"{where}.ports[{index}]"
        if not isinstance(raw_port, dict):
            raise GenComposeError(f"{at} must be a JSON object")
        name = raw_port.get("name")
        if not isinstance(name, str) or not name:
            raise GenComposeError(f"{at}.name is required (a non-empty string)")
        if name in seen:
            raise GenComposeError(f"{where}.ports contains duplicate name '{name}'")
        seen.add(name)

        port = dict(raw_port)
        x_um, y_um = raw_port.get("x_um"), raw_port.get("y_um")
        has_x = not isinstance(x_um, bool) and isinstance(x_um, (int, float))
        has_y = not isinstance(y_um, bool) and isinstance(y_um, (int, float))
        if (x_um is not None or y_um is not None) and not (has_x and has_y):
            raise GenComposeError(
                f"{at} (name '{name}') must declare both x_um and y_um as "
                "numbers, or neither"
            )
        if has_x and has_y:
            port["x_um"], port["y_um"] = float(x_um), float(y_um)

        width_um = raw_port.get("width_um")
        if width_um is not None:
            if (
                isinstance(width_um, bool)
                or not isinstance(width_um, (int, float))
                or width_um <= 0
            ):
                raise GenComposeError(
                    f"{at} (name '{name}').width_um must be a number > 0"
                )
            port["width_um"] = float(width_um)

        direction_deg = raw_port.get("direction_deg")
        if direction_deg is not None:
            if (
                isinstance(direction_deg, bool)
                or not isinstance(direction_deg, (int, float))
                or float(direction_deg) not in (0.0, 90.0, 180.0, 270.0)
            ):
                raise GenComposeError(
                    f"{at} (name '{name}').direction_deg must be one of 0, 90, "
                    "180, 270 (a port faces an axis, never a diagonal)"
                )
            port["direction_deg"] = int(direction_deg)

        layer = raw_port.get("layer")
        if layer is not None:
            if (
                not isinstance(layer, dict)
                or isinstance(layer.get("layer"), bool)
                or not isinstance(layer.get("layer"), int)
                or isinstance(layer.get("datatype"), bool)
                or not isinstance(layer.get("datatype"), int)
            ):
                raise GenComposeError(
                    f"{at} (name '{name}').layer must be a JSON object with "
                    "integer 'layer'/'datatype' fields (the same shape "
                    "`klt gen` and `klt layers` report)"
                )

        parsed.append(port)
    return parsed


def _parse_cell_block(
    raw_cell: Any, where: str, request_dir: str | None
) -> dict[str, Any]:
    """Parse one ``blocks[].cell`` entry -- an **existing** cell in a stream
    this command did not generate (#1189).

    ``{"gds_path": ..., "cell_name": ..., "ports": [...], "bbox_um": {...}}``.
    ``gds_path``/``cell_name`` are required; a relative ``gds_path`` resolves
    against ``request_dir`` exactly like a ``generator_report`` *path string*
    does. ``ports[]`` (:func:`_parse_cell_ports`) is optional and defaults to
    ``[]`` -- a cell with no declared ports can be placed but not wired.
    ``bbox_um`` is optional: when omitted it is read from the stream
    (:func:`read_cell_bbox_um`), which is the whole point of this block kind
    -- a PDK library cell never produced a ``klt gen`` report to copy a bbox
    out of.

    Returns ``{"cell_name", "gds_path", "bbox_um", "ports"}``.
    """
    if not isinstance(raw_cell, dict):
        raise GenComposeError(f"{where} must be a JSON object")

    cell_name = raw_cell.get("cell_name")
    if not isinstance(cell_name, str) or not cell_name:
        raise GenComposeError(f"{where}.cell_name is required")
    gds_path = raw_cell.get("gds_path")
    if not isinstance(gds_path, str) or not gds_path:
        raise GenComposeError(f"{where}.gds_path is required")
    gds_path = _resolve_relative(gds_path, request_dir or os.getcwd())

    ports = _parse_cell_ports(raw_cell.get("ports"), where)

    raw_bbox = raw_cell.get("bbox_um")
    if raw_bbox is None:
        bbox_um = read_cell_bbox_um(gds_path, cell_name, where)
    else:
        bbox_um = _require_bbox(raw_bbox, where=where)

    return {
        "cell_name": cell_name,
        "gds_path": gds_path,
        "bbox_um": bbox_um,
        "ports": ports,
    }


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

        # A block is sourced *either* from a klt verb's own response
        # (`generator_report` -- klt gen / klt draw / this command's own
        # output, #1189) *or* from a cell that already exists in a stream
        # (`cell` -- a PDK library cell, #1189). Exactly one, never both:
        # they answer the same question (where does this block's geometry
        # come from) two different ways, so accepting both would leave the
        # winner undefined.
        raw_report = raw_block.get("generator_report")
        raw_cell = raw_block.get("cell")
        if raw_report is not None and raw_cell is not None:
            raise GenComposeError(
                f"request.blocks[{index}] (id '{block_id}') declares both "
                "'generator_report' and 'cell' -- a block has exactly one "
                "source of geometry, name one or the other"
            )
        if raw_report is None and raw_cell is None:
            raise GenComposeError(
                f"request.blocks[{index}] (id '{block_id}') must declare either "
                "'generator_report' (a klt gen / klt draw / klt gen-compose "
                "JSON response, inline or as a path) or 'cell' (an existing "
                "cell in a GDS/OASIS stream: {gds_path, cell_name, ports})"
            )

        if raw_cell is not None:
            source = "cell"
            generator: str | None = None
            parsed_cell = _parse_cell_block(
                raw_cell,
                f"blocks[{index}] (id '{block_id}').cell",
                request_dir,
            )
            cell_name = parsed_cell["cell_name"]
            gds_path = parsed_cell["gds_path"]
            bbox_um = parsed_cell["bbox_um"]
            ports = parsed_cell["ports"]
            report: dict[str, Any] = {}
        else:
            source = "generator_report"
            report = load_generator_report_arg(raw_report, request_dir)
            raw_generator = report.get("generator")
            cell_name = report.get("cell_name")
            gds_path = report.get("gds_path")
            if not isinstance(raw_generator, str) or not raw_generator:
                raise GenComposeError(
                    f"blocks[{index}] (id '{block_id}'): generator_report.generator "
                    "is required -- a generator_report is a klt verb's own JSON "
                    "response (klt gen, klt draw, or klt gen-compose itself, which "
                    "reports generator: 'gen-compose'). To place a cell no klt verb "
                    "generated (e.g. a PDK library cell), use blocks[].cell "
                    "instead of hand-forging a report"
                )
            generator = raw_generator
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

        # blocks[].orientation (#1166) -- a placement decision, so it lives
        # on the request-level blocks[] entry, not inside generator_report
        # (which is immutable klt gen output). Defaults to "none" (today's
        # translation-only behaviour, unchanged). Applied here, once, to
        # this block's own bbox_um/ports[] -- every downstream consumer
        # (placement math, routing, GDS write) then reads already-oriented
        # (but still pre-translation) metadata and never repeats this
        # transform itself; see _ORIENTATIONS's docstring.
        orientation = raw_block.get("orientation", "none")
        if orientation not in _ORIENTATIONS:
            allowed = ", ".join(sorted(_ORIENTATIONS))
            raise GenComposeError(
                f"blocks[{index}] (id '{block_id}').orientation "
                f"'{orientation}' is not supported -- allowed: {allowed}"
            )
        if orientation != "none":
            bbox_um = _orient_bbox_um(bbox_um, orientation)
            ports_by_name = {
                name: _orient_port(port, orientation)
                for name, port in ports_by_name.items()
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
            "source": source,
            "generator": generator,
            "cell_name": cell_name,
            "gds_path": gds_path,
            "bbox_um": bbox_um,
            "port_names": set(ports_by_name),
            "ports": ports_by_name,
            "matched_group_id": matched_group_id,
            "min_spacing_um": min_spacing_um,
            "orientation": orientation,
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


def _parse_array_placement(raw_placement: dict[str, Any]) -> dict[str, Any]:
    """Parse and validate the ``"array"``-only placement fields (#1053):
    ``rows``, ``cols``, ``row_pitch_um``, ``col_pitch_um``, and an optional
    ``origin_um``.

    Mirrors :func:`klayout.db.CellInstArray`'s own row-vector/column-vector/
    row-count/column-count parameterization -- this is what lets
    :func:`compose` emit **one** hierarchical array instance for the whole
    ``rows * cols`` tiling instead of expanding it into that many flattened
    ``"explicit"``-style placements (see the module docstring).

    ``rows``/``cols`` must be positive (``>= 1``) integers; ``row_pitch_um``/
    ``col_pitch_um`` must be positive (``> 0``) numbers -- a zero or negative
    pitch is rejected even for a degenerate single-row/single-column array
    (``rows == 1`` or ``cols == 1``), where the corresponding pitch is
    otherwise unused geometrically, so the request document always carries a
    well-formed value regardless of which axis degenerates. ``origin_um``
    (the base tile's own ``offset_um`` -- row 0, col 0) is optional, defaulting
    to ``{"x": 0.0, "y": 0.0}``, mirroring ``"row"`` placement's own implicit
    first-block origin.

    Returns ``{"rows": int, "cols": int, "row_pitch_um": float,
    "col_pitch_um": float, "origin_um": {"x": float, "y": float}}``.
    """

    def _positive_int(value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise GenComposeError(
                f"request.placement.{field} must be a positive integer when "
                "strategy is 'array'"
            )
        return value

    def _positive_number(value: Any, field: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise GenComposeError(
                f"request.placement.{field} must be a positive number when "
                "strategy is 'array'"
            )
        return float(value)

    rows = _positive_int(raw_placement.get("rows"), "rows")
    cols = _positive_int(raw_placement.get("cols"), "cols")
    row_pitch_um = _positive_number(raw_placement.get("row_pitch_um"), "row_pitch_um")
    col_pitch_um = _positive_number(raw_placement.get("col_pitch_um"), "col_pitch_um")

    raw_origin = raw_placement.get("origin_um", {"x": 0.0, "y": 0.0})
    if not isinstance(raw_origin, dict):
        raise GenComposeError(
            "request.placement.origin_um must be a JSON object with numeric x/y fields"
        )
    x = raw_origin.get("x", 0.0)
    y = raw_origin.get("y", 0.0)
    if (
        isinstance(x, bool)
        or isinstance(y, bool)
        or not isinstance(x, (int, float))
        or not isinstance(y, (int, float))
    ):
        raise GenComposeError(
            "request.placement.origin_um must have numeric x/y fields"
        )

    return {
        "rows": rows,
        "cols": cols,
        "row_pitch_um": row_pitch_um,
        "col_pitch_um": col_pitch_um,
        "origin_um": {"x": float(x), "y": float(y)},
    }


def _parse_placement(
    raw_placement: Any, block_ids: set[str]
) -> tuple[
    str,
    list[str],
    float,
    dict[str, dict[str, float]] | None,
    dict[str, Any] | None,
]:
    """Parse and validate ``request.placement``.

    Returns ``(strategy, order, spacing_um, origins_um, array_params)``.
    ``spacing_um`` is ``0.0`` (unused) and ``origins_um``/``array_params`` are
    ``None`` for ``strategy: "row"``; ``origins_um`` is a parsed dict (and
    ``spacing_um``/``array_params`` unused) for ``strategy: "explicit"``
    (#321) -- ``placement.spacing_um`` alongside an ``"explicit"`` strategy is
    simply ignored, not rejected; ``array_params`` is a parsed dict (and
    ``spacing_um``/``origins_um`` unused) for ``strategy: "array"`` (#1053,
    see :func:`_parse_array_placement`).
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

    if strategy == "array" and len(block_ids) != 1:
        raise GenComposeError(
            "request.placement.strategy 'array' takes exactly one blocks[] "
            "entry -- the single block repeated at every tile -- but "
            f"{len(block_ids)} were given"
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
        return strategy, order, 0.0, origins_um, None

    if strategy == "array":
        array_params = _parse_array_placement(raw_placement)
        return strategy, order, 0.0, None, array_params

    spacing_um = raw_placement.get("spacing_um", 0.0)
    if isinstance(spacing_um, bool) or not isinstance(spacing_um, (int, float)):
        raise GenComposeError("request.placement.spacing_um must be a number")
    spacing_um = float(spacing_um)
    if spacing_um < 0:
        raise GenComposeError("request.placement.spacing_um must be >= 0")

    return strategy, order, spacing_um, None, None


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


def _parse_waypoints_um(
    raw_waypoints: Any, *, net: str, index: int, field: str = "waypoints_um"
) -> list[tuple[float, float]] | None:
    """Parse the optional ``connectivity[<index>].waypoints_um`` field (#634).

    ``None``/absent means "no waypoints" (today's behaviour, unchanged) --
    every other value must be a non-empty array of ``[x_um, y_um]`` number
    pairs, forced through in order by :func:`manhattan_backbone` between the
    two ports' own stubs. Malformed input is an application error (exit 1),
    the same treatment every other ``connectivity[]`` field gets.

    ``field`` (#1529) overrides the field name in error messages -- used by
    :func:`_parse_legs` so a malformed per-leg waypoint path is reported as
    ``legs[<i>].waypoints_um``, not the bare top-level field name shared by
    every leg.
    """
    if raw_waypoints is None:
        return None
    where = f"request.connectivity[{index}] (net '{net}').{field}"
    if not isinstance(raw_waypoints, list) or not raw_waypoints:
        raise GenComposeError(
            f"{where} must be a non-empty array of [x_um, y_um] pairs"
        )

    parsed: list[tuple[float, float]] = []
    for wp_index, waypoint in enumerate(raw_waypoints):
        if (
            not isinstance(waypoint, list)
            or len(waypoint) != 2
            or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) for v in waypoint
            )
        ):
            raise GenComposeError(
                f"{where}[{wp_index}] must be a [x_um, y_um] pair of numbers"
            )
        parsed.append((float(waypoint[0]), float(waypoint[1])))
    return parsed


#: Allowed keys in a ``connectivity[].legs[]`` entry (#1529). Any other key
#: is an application error rather than a silently dropped no-op field --
#: see #1548: a request written for a newer ``klt`` build (e.g. a
#: not-yet-supported field name) would otherwise be accepted and simply
#: ignored, with no indication the caller's intent was only partially
#: honored.
_LEG_ENTRY_KEYS = {"from_pin", "to_pin", "waypoints_um"}

#: Allowed keys in a ``connectivity[].legs[]`` entry's ``from_pin``/``to_pin``
#: endpoint object (#1529). Same rationale as :data:`_LEG_ENTRY_KEYS` (#1548).
_ENDPOINT_KEYS = {"block", "port"}


def _parse_legs(
    raw_legs: Any,
    *,
    net: str,
    index: int,
    parsed_pins: list[dict[str, str]],
) -> list[dict[str, Any]] | None:
    """Parse the optional ``connectivity[<index>].legs`` field (#1529).

    ``waypoints_um`` steers a 2-pin net's single backbone; a bundle (>2-pin)
    net has no single backbone for that path to belong to, so the only way
    to hand-route part of one was to decompose the net into N separate
    2-pin ``connectivity[]`` entries -- which then had to be pin-adjacent
    (chained) to avoid the accepted-leg overlap check (see
    :func:`compose`'s ``_leg_conflict``) mistaking two legs of the *same*
    net for a short between *different* nets. ``legs[]`` removes the need
    for that decomposition: each entry names one caller-steered leg of
    *this* connectivity entry's own net, seeded into
    :func:`route_bundle`'s spanning tree ahead of its automatic
    nearest-first search, so the rest of a large bundle net can still route
    itself.

    ``None``/absent means "no explicit legs" (today's behaviour, unchanged).
    Every other value must be a non-empty array of
    ``{"from_pin": {block, port}, "to_pin": {block, port},
    "waypoints_um": [[x_um, y_um], ...] | omitted}`` objects:

    - ``from_pin``/``to_pin`` must each match one of ``parsed_pins`` --
      *this* connectivity entry's own already-validated ``pins[]`` -- by
      ``(block, port)``, not merely be a valid port anywhere in the request;
      a leg can only steer a connection its own net's ``pins[]`` already
      declares. They must also name two different pins.
    - ``waypoints_um`` is optional *per leg*: omitting it still forces that
      specific pin pair into the spanning tree (skipping whatever pair the
      nearest-first search would otherwise have picked for them) while
      leaving the path itself to :func:`route_two_pin`'s default backbone;
      supplying it steers that leg's path exactly as the top-level
      ``waypoints_um`` field steers a 2-pin net's only leg.

    Malformed input is an application error (exit 1), the same treatment
    every other ``connectivity[]`` field gets.
    """
    if raw_legs is None:
        return None
    where = f"request.connectivity[{index}] (net '{net}').legs"
    if not isinstance(raw_legs, list) or not raw_legs:
        raise GenComposeError(f"{where} must be a non-empty array of leg objects")

    pin_lookup = {(pin["block"], pin["port"]) for pin in parsed_pins}

    def _parse_endpoint(
        raw_endpoint: Any, field: str, leg_index: int
    ) -> dict[str, str]:
        endpoint_where = f"{where}[{leg_index}].{field}"
        if not isinstance(raw_endpoint, dict):
            raise GenComposeError(f"{endpoint_where} must be a JSON object")
        unknown_endpoint_keys = set(raw_endpoint) - _ENDPOINT_KEYS
        if unknown_endpoint_keys:
            raise GenComposeError(
                f"{endpoint_where} has unrecognized key(s): "
                f"{sorted(unknown_endpoint_keys)} -- allowed: "
                f"{sorted(_ENDPOINT_KEYS)}"
            )
        block_id = raw_endpoint.get("block")
        port = raw_endpoint.get("port")
        if not isinstance(block_id, str) or not isinstance(port, str):
            raise GenComposeError(
                f"{endpoint_where} must have string 'block'/'port' fields"
            )
        if (block_id, port) not in pin_lookup:
            raise GenComposeError(
                f"{endpoint_where} (block '{block_id}', port '{port}') must "
                "match one of this connectivity entry's own pins[] entries"
            )
        return {"block": block_id, "port": port}

    legs: list[dict[str, Any]] = []
    for leg_index, raw_leg in enumerate(raw_legs):
        if not isinstance(raw_leg, dict):
            raise GenComposeError(f"{where}[{leg_index}] must be a JSON object")
        unknown_leg_keys = set(raw_leg) - _LEG_ENTRY_KEYS
        if unknown_leg_keys:
            raise GenComposeError(
                f"{where}[{leg_index}] has unrecognized key(s): "
                f"{sorted(unknown_leg_keys)} -- allowed: {sorted(_LEG_ENTRY_KEYS)}"
            )
        from_pin = _parse_endpoint(raw_leg.get("from_pin"), "from_pin", leg_index)
        to_pin = _parse_endpoint(raw_leg.get("to_pin"), "to_pin", leg_index)
        if from_pin == to_pin:
            raise GenComposeError(
                f"{where}[{leg_index}].from_pin and .to_pin must name different pins"
            )
        leg_waypoints = _parse_waypoints_um(
            raw_leg.get("waypoints_um"),
            net=net,
            index=index,
            field=f"legs[{leg_index}].waypoints_um",
        )
        legs.append(
            {"from_pin": from_pin, "to_pin": to_pin, "waypoints_um": leg_waypoints}
        )
    return legs


#: Allowed keys in a ``connectivity[]`` entry. Any other key is an
#: application error (exit 1) rather than a silently dropped no-op field --
#: see #1548: a request written for a newer ``klt`` build (e.g.
#: ``legs[]`` before #1529/#1536 added support for it) would otherwise
#: compose "successfully" against a stale build while the field's own
#: intent -- e.g. a caller-steered route -- was simply never read.
_CONNECTIVITY_ENTRY_KEYS = {"net", "pins", "waypoints_um", "legs"}


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
        unknown_keys = set(entry) - _CONNECTIVITY_ENTRY_KEYS
        if unknown_keys:
            raise GenComposeError(
                f"request.connectivity[{index}] has unrecognized key(s): "
                f"{sorted(unknown_keys)} -- allowed: "
                f"{sorted(_CONNECTIVITY_ENTRY_KEYS)}"
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

        waypoints_um = _parse_waypoints_um(
            entry.get("waypoints_um"), net=net, index=index
        )
        # A bundle net (>2 pins, #1073) is routed as a spanning tree of legs
        # (see route_bundle) -- a single caller-supplied path has no
        # unambiguous leg to belong to, so combining the two is an application
        # error rather than a silently ignored field. `legs[]` (#1529, below)
        # is the escape hatch: a per-leg waypoints_um that *is* unambiguous,
        # because each leg names its own two pins.
        if waypoints_um is not None and len(parsed_pins) != 2:
            raise GenComposeError(
                f"request.connectivity[{index}] (net '{net}').waypoints_um is "
                f"only supported for a 2-pin net -- this net has "
                f"{len(parsed_pins)} pins, which routes as a spanning tree of "
                "two-pin legs, and a single waypoint path cannot be attributed "
                "to one of them; use 'legs[]' to steer one or more individual "
                "legs by name instead"
            )

        legs = _parse_legs(
            entry.get("legs"), net=net, index=index, parsed_pins=parsed_pins
        )
        if legs is not None and waypoints_um is not None:
            raise GenComposeError(
                f"request.connectivity[{index}] (net '{net}') cannot set both "
                "'waypoints_um' and 'legs' -- 'waypoints_um' steers a 2-pin "
                "net's single backbone, 'legs' steers one or more named legs "
                "of any net (2-pin or bundle); use a single-entry 'legs' "
                "instead of 'waypoints_um' if a 2-pin net also needs a named "
                "from_pin/to_pin leg"
            )

        connectivity.append(
            {
                "net": net,
                "pins": parsed_pins,
                "waypoints_um": waypoints_um,
                "legs": legs,
            }
        )

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


#: Allowed keys in ``request.pdk`` (spike section 2). Any other key is an
#: application error rather than a silent fallback -- see #328: a typo such
#: as ``{"pdk": {"name": "gf180mcuD"}}`` (``name`` being what ``klt gen``'s
#: own response calls this field) would otherwise be silently treated as
#: ``request.pdk == {}`` and resolve whatever ``$PDK``/the default search
#: order picks, with no indication the request's own value was never read.
_ALLOWED_PDK_KEYS = {"variant", "root"}

#: The value this command reports as its response's own ``generator`` field
#: (#1189) -- the marker that makes a ``klt gen-compose`` response a valid
#: ``blocks[].generator_report`` input to *another* ``klt gen-compose`` run,
#: i.e. what makes composition nest. Named after the verb, exactly as
#: ``klt draw`` reports ``generator: "draw"`` for the same reason.
COMPOSE_GENERATOR = "gen-compose"


def promote_composed_ports(
    promoted_pins: list[dict[str, str]],
    blocks: dict[str, dict[str, Any]],
    offsets_um: dict[str, dict[str, float]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build the composed cell's own ``ports[]`` from the request's ``pins[]``
    (#1189), in the composed (post-placement) coordinate frame.

    ``pins[]`` (#210) is already exactly the statement "promote this
    sub-block port to a top-level pin of the composed cell" -- it is what
    writes the port's ``kdb.Text`` net label into the composed GDS. So it is
    also the right, *declared* source for the composed cell's own ports: a
    port a caller named in ``pins[]`` is addressable by name from the level
    above (``connectivity[].pins[].port``), and nothing else is silently
    exposed. Auto-promoting every sub-block port instead would both flood the
    parent with internal terminals and collide names across blocks (two
    ``mos_array`` blocks both report ``U0_D``).

    Each promoted port carries the port's own ``layer``/``width_um``/
    ``direction_deg`` (already orientation-corrected by :func:`_parse_blocks`)
    with its ``x_um``/``y_um`` translated by its block's ``offset_um``, so the
    shape matches ``klt gen``'s own ``ports[]`` entries exactly and needs no
    translation to be consumed as a block one level up. ``name`` (and ``net``)
    is the ``pins[]`` entry's own ``net`` -- the same string labelled into the
    GDS, so the composed cell's port name, its drawn label, and the name
    ``klt extract`` recovers all agree. ``block``/``port`` additionally record
    which sub-block port each one came from.

    A port with no reported ``{x_um, y_um, layer}`` geometry
    (:func:`_port_has_geometry`) cannot be promoted -- there is no position to
    report -- and is skipped with a note; the same note the label path already
    emits covers why. Returns ``(ports, notes)``.
    """
    ports: list[dict[str, Any]] = []
    notes: list[str] = []
    seen: set[str] = set()
    for entry in promoted_pins:
        net_label = entry["net"]
        block_id = entry["block"]
        port_name = entry["port"]
        port = blocks[block_id]["ports"].get(port_name)
        if not _port_has_geometry(port):
            continue  # the pins[] label path already noted the missing geometry
        if net_label in seen:
            notes.append(
                f"pin '{net_label}' (block '{block_id}' port '{port_name}') "
                "repeats a net name already promoted into the composed cell's "
                "own ports[] -- only the first is addressable by name if this "
                "response is reused as a blocks[].generator_report"
            )
        seen.add(net_label)
        offset = offsets_um[block_id]
        ports.append(
            {
                "name": net_label,
                "net": net_label,
                "layer": port["layer"],
                "x_um": port["x_um"] + offset["x"],
                "y_um": port["y_um"] + offset["y"],
                "width_um": port.get("width_um"),
                "direction_deg": port.get("direction_deg"),
                "block": block_id,
                "port": port_name,
            }
        )
    return ports, notes


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
    search order (#328). ``connectivity[]`` is always validated (every
    referenced block ``id``/port must exist). Whether it is also *routed*
    depends on ``routing`` (#1188): when ``routing`` is absent or ``{}``, this
    is a **declare-only** request -- every net lands in the response with
    ``status: "unrouted"``/``reason: "routing not requested"`` and no metal is
    drawn, which still exercises the ``{block, port}`` validation and reports
    the intended net list without requiring the caller to also draw routing
    metal. Supplying ``routing.layer_role``/``routing.width_um`` opts into
    point-to-point routing instead -- both become required once any key of
    ``routing`` is given at all. Returns a dict matching the documented
    response schema (see ``docs/cli/gen-compose.md``).

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
    strategy, order, spacing_um, origins_um, array_params = _parse_placement(
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
    elif strategy == "array":
        assert array_params is not None  # guaranteed by _parse_placement for "array"
        # The single blocks[] entry's own offset_um is the base (row 0, col 0)
        # tile's origin -- every other tile is expressed only via the
        # kdb.CellInstArray row/column vectors _write_composed_gds emits, not
        # as a separate offsets_um entry (#1053; see the module docstring).
        offsets_um = {order[0]: dict(array_params["origin_um"])}
    else:
        assert origins_um is not None  # guaranteed by _parse_placement for "explicit"
        offsets_um = resolve_explicit_offsets(order, origins_um)

    if strategy == "array":
        assert array_params is not None
        placed_bboxes_um = {
            order[0]: array_placement_bbox_um(bboxes_um[order[0]], array_params)
        }
    else:
        placed_bboxes_um = {
            block_id: _translate_bbox(bboxes_um[block_id], offsets_um[block_id])
            for block_id in order
        }
    composed_bbox_um = _union_bbox([placed_bboxes_um[block_id] for block_id in order])

    warnings: list[str] = []
    notes: list[str] = []

    # #692: "explicit" placement performs no overlap validation of its own
    # (resolve_explicit_offsets's docstring) -- but a caller that places a
    # block closer to a neighbour than that neighbour's own declared
    # drc_hints.min_spacing_um can end up with a silent same-layer short that
    # `klt drc` won't catch (a zero-clearance merge isn't an illegal shape by
    # any spacing rule). Advisory-only, scoped to "explicit" -- "row"
    # placement's own uniform spacing_um does not have this ergonomics trap.
    if strategy == "explicit":
        warnings.extend(
            _explicit_placement_clearance_warnings(order, blocks, placed_bboxes_um)
        )

    # #1679: every check above (and every placement strategy's own math)
    # trusts a block's *declared* bbox_um -- accurate for a `klt gen` block,
    # but a `generator_report` block's bbox_um is taken verbatim
    # (_parse_blocks) and never cross-checked against its own stream. A
    # block whose real drawn geometry (e.g. a guard/seal ring) extends past
    # its declared bbox can silently overlap a neighbour placed just outside
    # the *declared* box -- a `klt drc`-clean same-layer merge that only
    # surfaces later via `klt extract`'s merged_net_labels diagnostic.
    # Real-geometry-based, so (unlike the declared-bbox check above) it
    # applies to every strategy -- "row"/"array" place from the same
    # unreliable declared bbox_um and are no less exposed; "array" is a
    # no-op here in practice (exactly one blocks[] entry, so no pairwise
    # comparison is possible).
    warnings.extend(
        _declared_bbox_overlap_warnings(order, blocks, offsets_um, placed_bboxes_um)
    )

    # --- Routing (phase 2) --------------------------------------------------
    # A connectivity[] net is routed only when routing.layer_role/width_um are
    # given. #1188: routing being entirely absent/{} is *not* an error -- it
    # is a declare-only request (every net still validated against blocks'
    # ports, none of it drawn; see declare_only below). Supplying routing
    # with only *some* keys set is still an application error (there is no
    # unambiguous "partial" routing spec) -- unchanged from before #1188.
    declare_only = connectivity and not routing
    route_layer: tuple[int, int] | None = None
    label_layer: tuple[int, int] | None = None
    extraction_deck: ExtractionDeck | None = None
    width_um = 0.0
    # routing.cross_block_layer_role (#1168): an optional second, higher metal
    # a same-block self-net leg falls back to when the primary route_layer
    # would draw it as a silent short across another of the block's own pads
    # (route_two_pin's checks 3/4) -- see _resolve_cross_block_route_layer.
    # None unless the caller configures it; every variable below stays None
    # (and every leg behaves exactly as before #1168) in that case.
    cross_route_layer: tuple[int, int] | None = None
    cross_label_layer: tuple[int, int] | None = None
    # routing.cross_block_width_um (#1620): the width a leg draws at once it
    # actually falls back to cross_route_layer -- distinct from width_um so
    # naming a cross_block_layer_role never forces the *primary* plane's
    # width up to satisfy the cross layer's own (typically stricter) deck
    # minimum. None unless a cross layer is configured; resolved below.
    cross_block_width_um: float | None = None
    if connectivity and not declare_only:
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
        # #1501: routing.width_um must clear the resolved PDK deck's own
        # minimum-width rule for route_layer -- the same deck `klt drc`
        # judges the drawn backbone with. Without this, a request that omits
        # `routing.width_um` (or names one below the deck's own floor, e.g.
        # gf180mcu's metal1.width.1 = 0.23um vs. the documented 0.17um
        # default) draws a guaranteed-illegal backbone with no error or
        # warning at generation time, and the resulting `klt drc` violation
        # count is silently mis-attributed to placement/routing rather than
        # this units mismatch.
        route_width_floor = _min_width_um_for_layer(pdk_info["variant"], route_layer)
        if route_width_floor is not None and width_um < route_width_floor[0] - 1e-9:
            floor_um, rule_id = route_width_floor
            raise GenComposeError(
                f"request.routing.width_um ({width_um}um) is narrower than "
                f"the resolved PDK deck's own minimum width for "
                f"routing.layer_role '{layer_role}' -- '{rule_id}' requires "
                f">= {floor_um}um"
            )
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

        cross_layer_role = routing.get("cross_block_layer_role")
        if cross_layer_role is not None:
            if not isinstance(cross_layer_role, str) or not cross_layer_role:
                raise GenComposeError(
                    "request.routing.cross_block_layer_role must be a "
                    "non-empty layer role string when given"
                )
            cross_route_layer, _cross_via_layer = _resolve_cross_block_route_layer(
                pdk_info["variant"], layer_role, cross_layer_role
            )
            # #1620: a leg that falls back to cross_route_layer draws at its
            # own routing.cross_block_width_um -- a second, independent width
            # -- rather than being forced to share routing.width_um with the
            # primary plane. Defaults to the cross layer's own deck minimum
            # when omitted, so a caller that only wants "minimum pitch on
            # each plane" never has to spell either width out; explicitly
            # supplying one is still validated against that layer's own
            # floor, exactly as width_um is against route_layer's above.
            cross_width_floor = _min_width_um_for_layer(
                pdk_info["variant"], cross_route_layer
            )
            raw_cross_width = routing.get("cross_block_width_um")
            if raw_cross_width is None:
                cross_block_width_um = (
                    cross_width_floor[0] if cross_width_floor is not None else width_um
                )
            else:
                if (
                    isinstance(raw_cross_width, bool)
                    or not isinstance(raw_cross_width, (int, float))
                    or raw_cross_width <= 0
                ):
                    raise GenComposeError(
                        "request.routing.cross_block_width_um must be a "
                        "positive number when given"
                    )
                cross_block_width_um = float(raw_cross_width)
            if (
                cross_width_floor is not None
                and cross_block_width_um < cross_width_floor[0] - 1e-9
            ):
                floor_um, rule_id = cross_width_floor
                raise GenComposeError(
                    f"request.routing.cross_block_width_um "
                    f"({cross_block_width_um}um) is narrower than the "
                    "resolved PDK deck's own minimum width for "
                    f"routing.cross_block_layer_role '{cross_layer_role}' -- "
                    f"'{rule_id}' requires >= {floor_um}um"
                )
            cross_label_layer = _resolve_label_layer(
                pdk_info["variant"], cross_route_layer
            )
            if cross_label_layer is None:
                notes.append(
                    f"routing.cross_block_layer_role '{cross_layer_role}' has "
                    "no PDK label-layer convention `klt extract` recognises "
                    "-- a net that falls back to this layer will not carry a "
                    "net label, so it will not survive as a named .SUBCKT "
                    "pin after extraction"
                )

    # Drawn-geometry obstacles for the self-net drawn-metal check (#453/#469)
    # and the own-block escape check (#1527), read lazily and cached per
    # block, once per block however many legs land on it. Originally read
    # only for a same-block self-net (every *inter*-block net was covered by
    # the whole-block bbox check alone) -- but that bbox check only ever
    # modelled each pin's own block by its bbox, with an unavoidable margin
    # exempting the port's own approach stub from being flagged, and nothing
    # else looked at what that stub might actually cross on its way out
    # (#1527). So every leg's *own* endpoint block(s) are read here now, not
    # only a same-block self-net's shared one -- see route_bundle()'s own
    # updated docstring. A second such cache, keyed the same way, covers
    # cross_route_layer (#1168) for the same checks retried on the
    # cross-block layer.
    block_geometry_cache: dict[str, dict[str, Any] | None] = {}
    cross_block_geometry_cache: dict[str, dict[str, Any] | None] = {}

    def _block_geometry_for(block_id: str) -> dict[str, dict[str, Any] | None]:
        if route_layer is not None and block_id not in block_geometry_cache:
            block_geometry_cache[block_id] = read_block_layer_geometry(
                block_id, blocks[block_id], offsets_um[block_id], route_layer
            )
        return block_geometry_cache

    def _cross_block_geometry_for(
        block_id: str,
    ) -> dict[str, dict[str, Any] | None]:
        if cross_route_layer is not None and block_id not in cross_block_geometry_cache:
            cross_block_geometry_cache[block_id] = read_block_layer_geometry(
                block_id, blocks[block_id], offsets_um[block_id], cross_route_layer
            )
        return cross_block_geometry_cache

    # Own-block pad self-notch check (#1520): unlike the two caches above --
    # each keyed by block_id alone, for exactly one fixed layer
    # (route_layer/cross_route_layer) -- a via-drop's landing pad or a
    # stub-widen box can land on *any* declared port's own layer, which
    # varies per pin (a via-drop exists precisely because that layer differs
    # from route_layer). So this cache is keyed by (block_id, layer) instead,
    # read lazily the same way, and reused by every leg that drops onto the
    # same block/layer pair.
    own_block_layer_geometry_cache: dict[
        tuple[str, tuple[int, int]], dict[str, Any] | None
    ] = {}

    def _own_block_layer_geometry(
        block_id: str, layer: tuple[int, int]
    ) -> dict[str, Any] | None:
        key = (block_id, layer)
        if key not in own_block_layer_geometry_cache:
            own_block_layer_geometry_cache[key] = read_block_layer_geometry(
                block_id, blocks[block_id], offsets_um[block_id], layer
            )
        return own_block_layer_geometry_cache[key]

    # dbu for the route-vs-route collision check below (#1057), read lazily
    # (only once connectivity[] actually has a net to check) from any one
    # block's own GDS -- this pre-flight check only ever compares continuous
    # micron-space geometry quantized at *some* fine resolution, so any
    # single block's own dbu is precise enough to reuse here even though (as
    # of #1514) it need not be identical to every other block's dbu, nor to
    # the composed layout's own dbu `_write_composed_gds` resolves below
    # (the finest dbu across all blocks, reconciling any coarser block onto
    # it) -- a difference of, at most, one block's own dbu step is far below
    # any DRC-relevant tolerance.
    _route_dbu_cache: list[float] = []

    def _route_dbu() -> float:
        if not _route_dbu_cache:
            import klayout.db as kdb

            probe_block_id = order[0]
            probe_gds_path = blocks[probe_block_id]["gds_path"]
            probe_layout = kdb.Layout()
            try:
                probe_layout.read(probe_gds_path)
            except Exception as exc:  # klayout raises RuntimeError for bad paths
                raise GenComposeError(
                    f"block '{probe_block_id}': could not read gds_path "
                    f"'{probe_gds_path}': {exc}"
                ) from exc
            _route_dbu_cache.append(probe_layout.dbu)
        return _route_dbu_cache[0]

    nets: list[dict[str, Any]] = []
    unrouted_nets: list[str] = []
    routed_geometry: list[dict[str, Any]] = []
    # Regions already accepted into routed_geometry so far in this request,
    # kept in lock-step with it (#1057) -- a route-vs-block collision is
    # covered by route_two_pin's own checks 1-6, but nothing previously
    # compared one connectivity[] entry's drawn backbone against *another*
    # entry's already-accepted one, so two distinct nets on the same
    # routing.layer_role could be drawn crossing each other, both reporting
    # routed: true, with nothing to flag the silent short. Each entry also
    # keeps the pin set that produced it (``{(block_id, port_name), ...}``)
    # -- two connectivity[] entries that share a literal pin (e.g. bussing
    # three ports into one node via two chained 2-pin nets, as
    # test_compose_via_drop_routes_self_net_that_pure_metal_would_reject
    # already exercises) are, by construction, the same electrical node at
    # that shared pin: both backbones' approach stubs necessarily converge on
    # the identical point, from the identical direction, drawing a real
    # positive-area overlap there that is the caller's intended merge, not an
    # accidental short -- so a pair sharing a pin is exempt from this check
    # entirely (see the "shared pin" skip below). kdb.Region objects are not
    # JSON-serialisable, so this stays a private side list rather than living
    # on routed_geometry/nets[] themselves.
    accepted_route_regions: list[
        tuple[str, frozenset[tuple[str, str]], tuple[int, int] | None, Any]
    ] = []

    # Minimum same-layer spacing cache (issue #1386): looked up at most once
    # per distinct effective route layer actually used, from the *same*
    # curated DRC deck `klt drc --deck <family>` runs -- never a second,
    # private threshold table. Feeds both the spacing-aware route-vs-route
    # half of `_leg_conflict` below and its own-block pad self-notch check
    # (#1520, added later in the same function): #1057's original
    # route-vs-route check only ever
    # caught a literal footprint *overlap*, which is a strict subset of what
    # a same-layer minimum-spacing rule (e.g. sky130's `li1.space.1`/
    # `met1.space.1`) actually forbids -- two legs whose footprints never
    # touch can still sit closer together than that rule allows, and #1057
    # reported both `routed: true` for it. A layer with no matching `"space"`
    # rule in the resolved deck (or an unresolvable PDK family) caches
    # ``None`` and this check degrades to exactly its pre-#1386 overlap-only
    # behaviour for that layer.
    _min_spacing_um_cache: dict[tuple[int, int], tuple[float, str] | None] = {}

    def _min_spacing_um_for_layer(
        layer: tuple[int, int] | None,
    ) -> tuple[float, str] | None:
        if layer is None:
            return None
        if layer not in _min_spacing_um_cache:
            try:
                family = _pdk_family(pdk_info["variant"])
                deck_rules = get_deck(family)
                nominal_dbu_um = get_nominal_dbu(family)
            except (GenError, UnknownDeckError):
                _min_spacing_um_cache[layer] = None
                return None
            best: tuple[float, str] | None = None
            for rule in deck_rules:
                if (
                    rule.check == "space"
                    and rule.layer == layer
                    and rule.other_layer is None
                    and rule.derived_layer is None
                ):
                    threshold_um = rule.threshold_dbu * nominal_dbu_um
                    if best is None or threshold_um > best[0]:
                        best = (threshold_um, rule.id)
            _min_spacing_um_cache[layer] = best
        return _min_spacing_um_cache[layer]

    for entry in connectivity:
        net_label = entry["net"]
        pins = entry["pins"]
        net_pin_set = frozenset((pin["block"], pin["port"]) for pin in pins)

        def _leg_conflict(
            points_um: list[tuple[float, float]],
            via_drops: list[dict[str, Any]],
            stub_widen: list[dict[str, Any]],
            layer: tuple[int, int] | None,
            _net_pin_set: frozenset[tuple[str, str]] = net_pin_set,
        ) -> str | None:
            """Route-vs-route collision check (#1057, spacing-aware since
            #1386) for one candidate leg.

            ``route_two_pin``'s checks 1-6 only ever compare a leg's own
            backbone against *block* geometry -- a conflict with a route
            already accepted earlier in this same request is caught here
            instead, the one place with visibility across nets. Nets sharing
            a literal pin are exempt (an intended merge, not a short); the
            *current* net's own other legs are never in
            ``accepted_route_regions`` yet, since a net is committed only
            once every one of its legs is accepted -- so two legs of one
            bundle net converging on their shared node are never compared
            against each other either.

            ``layer`` (issue #1386) is the *candidate*'s own effective
            drawing layer -- an already-accepted leg on a genuinely different
            physical layer (e.g. one leg fell back to
            ``routing.cross_block_layer_role`` while another stayed on the
            primary ``routing.layer_role``) can neither overlap nor violate a
            same-layer spacing rule against this one, so it is skipped
            entirely rather than compared. ``None`` (a caller that predates
            #1386, or a candidate whose layer genuinely could not be
            resolved) falls back to comparing against every accepted region
            regardless of its layer, preserving this check's pre-#1386
            behaviour exactly.

            The compared region is built by :func:`_drawn_leg_footprint_region`,
            not the bare backbone alone (issue #1197): a via-drop's landing
            pad or a stub-widen box can extend past the backbone far enough
            to land on another net's already-accepted route even when the
            two backbones themselves never touch -- exactly the shape two
            same-block self-nets whose via-drop landing pads cross each
            other's backbones take. Leaving those out of both sides of this
            comparison (candidate and ``accepted_route_regions`` alike) is
            what let that pair compose ``routed: true`` while `klt extract`
            silently merged them onto one node. Scoped to ``layer`` (issue
            #1567): a multi-hop via-drop ladder's intermediate/far landing
            pads sit on layers *other* than this leg's own primary ``layer``
            -- :func:`_drawn_leg_footprint_region` excludes them here, the
            same as it always excluded a single-hop drop's *far* (port-side)
            pad. Those intermediate pads are still checked against the
            *same block's* own other drawn geometry by the own-block pad
            self-notch check below (#1520) -- catching a cross-*net* short on
            an intermediate level (two unrelated nets' ladders both landing
            on, say, the same ``"metal2"`` role at overlapping points) is
            left to `klt drc`, this module's own stated backstop for
            anything beyond these heuristics (see :func:`route_two_pin`'s
            docstring).

            A literal positive-area overlap is still always rejected first
            (mirrors check 4's "positive area only, not a mere edge touch"
            rule: a ``kdb.Region`` boolean AND between two backbones already
            yields an empty region for a mere edge touch). When the two
            regions do *not* overlap but sit closer together than the
            resolved deck's own same-layer ``"space"`` rule for ``layer``
            (:func:`_min_spacing_um_for_layer`), one side is grown by that
            threshold (``kdb.Region.sized`` -- the standard "distance < d"
              Minkowski-sum test, symmetric regardless of which side grows)
            before the same intersection test -- catching the class of
            violation issue #1386 reported (`nets[].legs[].routed: true`
            legs that still failed `klt drc`'s `li1.space.1`/`met1.space.1`)
            that the overlap-only version of this check could not see. A
            layer with no known ``"space"`` rule keeps the overlap-only test.
            """
            # candidate_width_um (#1620): this candidate's own drawn width --
            # cross_block_width_um when it resolved on cross_route_layer,
            # width_um otherwise -- so the compared footprint matches what
            # _write_composed_gds actually draws for it, not the primary
            # plane's width for a leg that never drew on that plane.
            candidate_width_um = (
                cross_block_width_um
                if cross_route_layer is not None
                and layer == cross_route_layer
                and cross_block_width_um is not None
                else width_um
            )
            region = _drawn_leg_footprint_region(
                points_um,
                candidate_width_um,
                via_drops,
                stub_widen,
                _route_dbu(),
                layer,
            )
            spacing = _min_spacing_um_for_layer(layer)
            inflated_region = None
            if spacing is not None:
                spacing_um, _rule_id = spacing
                spacing_dbu = int(round(spacing_um / _route_dbu()))
                if spacing_dbu > 0:
                    inflated_region = region.sized(spacing_dbu)
            for (
                other_net,
                other_pins,
                other_layer,
                other_region,
            ) in accepted_route_regions:
                if _net_pin_set & other_pins:
                    continue  # shared pin -- an intended merge, not a short
                if (
                    layer is not None
                    and other_layer is not None
                    and layer != other_layer
                ):
                    continue  # different physical layers can't touch or short
                if not (region & other_region).is_empty():
                    return f"crosses already-routed net '{other_net}'"
                if (
                    inflated_region is not None
                    and not (inflated_region & other_region).is_empty()
                ):
                    spacing_um, rule_id = spacing  # type: ignore[misc]
                    return (
                        f"comes within {spacing_um:.4g}um of already-routed "
                        f"net '{other_net}' -- closer than the resolved "
                        f"deck's own '{rule_id}' minimum same-layer spacing "
                        "rule (no literal overlap, but still a real `klt "
                        "drc` violation on the composed layout)"
                    )

            # Own-block pad self-notch check (#1520): everything above
            # compares this leg's drawn footprint against a *different*
            # net's already-accepted geometry. Nothing yet checks a
            # via-drop's landing pad or a stub-widen box -- both drawn
            # independent of routing.width_um, at a fixed size sized from
            # the PDK's own contact-enclosure convention -- against the
            # *same* block's own other drawn shapes on the pad's own layer.
            # A pad legitimately lands on (merges with) the wire at its own
            # declared port; that touch is never a violation. But when that
            # port was hand-declared on a pre-existing `blocks[].cell`
            # stream's own internal wire (the only way to declare a port on
            # a cell no `klt` verb generated -- see the module docstring's
            # "blocks[].cell" note), the pad can still land close enough to
            # a *different* part of that same wire (e.g. a perpendicular leg
            # near a corner the port sits close to) to violate the resolved
            # deck's own same-layer spacing rule -- a real `klt drc` finding
            # even though both shapes are the same electrical node, since a
            # rule-deck space check is net-agnostic.
            # :func:`_pad_self_notch_violation_um` catches exactly this via
            # ``kdb.Region.notch_check`` (a same-*polygon* self-space check),
            # which the overlap/inflate comparisons above cannot see -- the
            # pad and the wire it lands on merge into one polygon, so there
            # is no *other* shape here to intersect against.
            pad_boxes: list[
                tuple[tuple[int, int], str, tuple[float, float, float, float]]
            ] = []
            landing_half_um = _VIA_LANDING_SIZE_UM / 2.0
            for drop in via_drops:
                block_id = drop.get("block_id")
                if block_id is None:
                    continue  # pre-#1520 caller-built via_drops -- skip, not an error
                cx, cy = drop["x_um"], drop["y_um"]
                box = (
                    cx - landing_half_um,
                    cy - landing_half_um,
                    cx + landing_half_um,
                    cy + landing_half_um,
                )
                # Each hop's landing pad is drawn on *both* of its own
                # ``landing_layers`` (see _write_composed_gds) -- issue
                # #1567 generalized this from a single hop's fixed
                # (port_layer, this leg's own route layer) pair to whatever
                # pair *that hop* actually lands on, so a multi-level
                # ladder's intermediate pads are checked here too, not just
                # the one nearest the backbone or the one nearest the pin.
                # dict.fromkeys dedupes the common case where both layers of
                # a hop happen to be identical.
                for pad_layer in dict.fromkeys(drop.get("landing_layers", ())):
                    pad_boxes.append((pad_layer, block_id, box))
            for widen in stub_widen:
                block_id = widen.get("block_id")
                if block_id is None or layer is None:
                    continue  # pre-#1520 caller-built stub_widen -- skip
                cx, cy = widen["x_um"], widen["y_um"]
                half_um = widen["width_um"] / 2.0
                length_um = widen["length_um"]
                if widen["direction_deg"] == 90:
                    box = (cx - half_um, cy, cx + half_um, cy + length_um)
                else:  # 270
                    box = (cx - half_um, cy - length_um, cx + half_um, cy)
                pad_boxes.append((layer, block_id, box))

            for pad_layer, block_id, box in pad_boxes:
                pad_spacing = _min_spacing_um_for_layer(pad_layer)
                if pad_spacing is None:
                    continue
                pad_spacing_um, pad_rule_id = pad_spacing
                pad_geometry = _own_block_layer_geometry(block_id, pad_layer)
                if pad_geometry is None:
                    continue
                violation_um = _pad_self_notch_violation_um(
                    box, pad_geometry, pad_spacing_um
                )
                if violation_um is not None:
                    return (
                        f"draws a pad ({box[0]:.4g}, {box[1]:.4g}) - "
                        f"({box[2]:.4g}, {box[3]:.4g}) on layer {pad_layer} "
                        f"that comes within {violation_um:.4g}um of block "
                        f"'{block_id}''s own drawn geometry on that layer -- "
                        f"closer than the resolved deck's own '{pad_rule_id}' "
                        "minimum same-layer spacing rule (no literal overlap "
                        "with a *different* shape -- the pad merges with the "
                        "wire it lands on -- but a real `klt drc` finding "
                        "against a *different* part of that same wire, e.g. "
                        "a perpendicular leg near a corner the declared port "
                        "sits close to; move the declared port further along "
                        "its own wire, away from the corner)"
                    )
            return None

        # Bundle (>2-pin) nets route as a spanning tree of two-pin legs
        # (#1073); a 2-pin net is the degenerate one-leg case of exactly the
        # same path, so both go through route_bundle() -- unless this is a
        # declare-only request (#1188: routing absent/{}), in which case no
        # metal is drawn for any net and _declare_only_bundle_result() reports
        # every net "unrouted"/"routing not requested" instead.
        if declare_only:
            result = _declare_only_bundle_result(pins)
        else:
            result = route_bundle(
                pins,
                blocks,
                offsets_um,
                placed_bboxes_um,
                width_um,
                route_layer,
                extraction_deck,
                block_geometry_for=_block_geometry_for,
                leg_conflict=_leg_conflict,
                waypoints_um=entry.get("waypoints_um"),
                cross_block_route_layer=cross_route_layer,
                cross_block_geometry_for=_cross_block_geometry_for,
                cross_block_width_um=cross_block_width_um,
                explicit_legs=entry.get("legs"),
            )
        nets.append(
            {
                "net": net_label,
                "pins": pins,
                "routed": result["routed"],
                "route_length_um": result["route_length_um"],
                # "routed"/"partial"/"unrouted" -- distinguishes a net that
                # drew some but not all of its legs from one that drew none
                # at all, since both report `routed: false` above (#1169).
                "status": result["status"],
                "legs": [
                    {
                        "pins": leg["pins"],
                        "routed": leg["routed"],
                        "route_length_um": leg["route_length_um"],
                        "reason": leg["reason"],
                    }
                    for leg in result["legs"]
                ],
            }
        )
        # Draw every leg the router actually accepted, whether or not the net
        # ended up fully connected (#1169) -- a partially-routed net's drawn
        # legs are real, checked metal (route_two_pin's checks 1-6 plus the
        # route-vs-route collision check #1057 already passed), so they must
        # also feed `accepted_route_regions` the same as a fully-routed net's
        # legs do, or a later net's own collision check would miss them.
        drawn_legs = [leg for leg in result["legs"] if leg["routed"]]
        for leg_index, leg in enumerate(drawn_legs):
            # route_layer (#1168): the effective layer this leg actually
            # drew on -- route_layer (the primary) unless the leg fell
            # back to cross_route_layer (route_two_pin's same-layer-short
            # retry). label_layer follows the same choice, so a leg's net
            # label lands on the layer klt extract actually expects it on.
            # Resolved per *leg*, which is what makes this correct for a
            # partially-routed net too (#1169): the drawn legs of one net
            # need not all have landed on the same layer. Recorded here
            # (rather than after the `accepted_route_regions.append` below)
            # so `_leg_conflict`'s own layer-aware comparison (#1386) has it
            # for every accepted entry.
            leg_route_layer = leg.get("route_layer") or route_layer
            # leg_width_um (#1620): the width this leg actually drew at --
            # route_two_pin/route_bundle report it per leg since it can
            # differ from the primary plane's own width_um whenever the leg
            # fell back to cross_route_layer (drawn at cross_block_width_um
            # instead). Falls back to width_um for a leg that predates this
            # field (defensive; every leg compose() produces itself sets it).
            leg_width_um = leg.get("width_um", width_um)
            accepted_route_regions.append(
                (
                    net_label,
                    net_pin_set,
                    leg_route_layer,
                    _drawn_leg_footprint_region(
                        leg["points_um"],
                        leg_width_um,
                        leg["via_drops"],
                        leg["stub_widen"],
                        _route_dbu(),
                        leg_route_layer,
                    ),
                )
            )
            leg_label_layer = (
                cross_label_layer
                if cross_route_layer is not None
                and leg_route_layer == cross_route_layer
                else label_layer
            )
            routed_geometry.append(
                {
                    "net": net_label,
                    "points_um": leg["points_um"],
                    "width_um": leg_width_um,
                    "via_drops": leg["via_drops"],
                    "stub_widen": leg["stub_widen"],
                    "route_layer": leg_route_layer,
                    "label_layer": leg_label_layer,
                    # One kdb.Text per *net*, not per leg: the drawn legs of
                    # one net are one conductor (#1073) -- even when only a
                    # subset of the net's legs drew (#1169), the label lands
                    # on the first drawn leg's island only; any other drawn
                    # island of the same partially-routed net is left
                    # unlabelled, exactly as an individually-unroutable
                    # candidate leg always was.
                    "label": leg_index == 0,
                }
            )
        if not result["routed"]:
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

    # The composed cell's own ports[] (#1189) -- promoted from the pins[]
    # entries above, in the composed frame, so this whole response can be fed
    # straight back into another gen-compose run's blocks[].generator_report
    # and its top-level nets addressed by name from the level above.
    composed_ports, promote_notes = promote_composed_ports(
        promoted_pins, blocks, offsets_um
    )
    notes.extend(promote_notes)

    array_placement_gds = (
        {
            "block_id": order[0],
            "rows": array_params["rows"],
            "cols": array_params["cols"],
            "row_pitch_um": array_params["row_pitch_um"],
            "col_pitch_um": array_params["col_pitch_um"],
        }
        if strategy == "array"
        else None
    )
    # #1501: `_VIA_DROP_SIZE_UM` (`gen.CONTACT_SIZE_UM`, a PDK-independent
    # constant) is a guaranteed `viaN.width.1` violation on any family whose
    # via-width minimum exceeds it (e.g. gf180mcu's `via1.width.1` = 0.26um
    # vs. the 0.22um constant). Derive a per-via_layer floor from the same
    # resolved deck `klt drc` judges the drawn via square with -- one lookup
    # per *distinct* via_layer actually used across every drawn via-drop,
    # never a private threshold. A layer with no matching "width" rule (or an
    # unresolvable PDK family) keeps exactly `_VIA_DROP_SIZE_UM`, unchanged.
    via_drop_size_um: dict[tuple[int, int], float] = {}
    for route in routed_geometry:
        for drop in route.get("via_drops", []):
            via_pair = drop["via_layer"]
            if via_pair not in via_drop_size_um:
                floor = _min_width_um_for_layer(pdk_info["variant"], via_pair)
                via_drop_size_um[via_pair] = max(
                    _VIA_DROP_SIZE_UM, floor[0] if floor is not None else 0.0
                )
    composed_dbu_um, dbu_rescale_warnings = _write_composed_gds(
        blocks,
        order,
        offsets_um,
        cell_name,
        output_path,
        routed_geometry,
        route_layer,
        label_layer,
        pin_placements,
        array_placement=array_placement_gds,
        via_drop_size_um=via_drop_size_um,
    )
    warnings.extend(dbu_rescale_warnings)

    # --- drc_hints: matched-group echo + tightest spacing used --------------
    matched_groups = _collect_matched_groups(blocks, order)

    min_spacing_um: float | None = None
    if routed_geometry and strategy == "row":
        # "row" placement always applies spacing_um between adjacent blocks;
        # routing adds no spacing tighter than that at this phase (routes run
        # through the placed channels), so the tightest spacing actually used
        # is the placement gap. Left null when nothing was routed (phase-1
        # behaviour) -- checked via `routed_geometry` (populated only by legs
        # that actually drew metal, #1198), not mere `connectivity[]`
        # presence: a declare-only request (`routing` absent/{}, #1188) or an
        # all-unroutable `connectivity[]` populates `connectivity[]` without
        # ever drawing anything, and must still report `null` here. Also
        # left null for "explicit" (#321) and "array" (#1053) placement --
        # neither has a single shared spacing value to report ("explicit"'s
        # per-pair separation is exactly what a caller-declared origin
        # expresses; "array" has two independent pitches,
        # row_pitch_um/col_pitch_um, not one).
        min_spacing_um = spacing_um

    response_blocks = [
        {
            "id": block_id,
            # "generator_report" (a klt verb's own response) or "cell" (an
            # existing cell in a stream, #1189) -- which of the two request
            # forms sourced this block's geometry.
            "source": blocks[block_id]["source"],
            "generator": blocks[block_id]["generator"],
            "cell_name": blocks[block_id]["cell_name"],
            "offset_um": offsets_um[block_id],
            "bbox_um": placed_bboxes_um[block_id],
            "orientation": blocks[block_id].get("orientation", "none"),
        }
        for block_id in order
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        # #1189: this response is itself a valid blocks[].generator_report for
        # another gen-compose run -- `generator` is the required marker that
        # says so (mirroring `klt draw`'s own `generator: "draw"`), and
        # `ports[]` below is what lets the composed cell's top-level nets be
        # addressed by name from the level above.
        "generator": COMPOSE_GENERATOR,
        "cell_name": cell_name,
        "gds_path": output_path,
        "pdk": {
            "name": pdk_info["variant"],
            "variant": pdk_info["variant"],
            "version": pdk_info["version"],
        },
        "dbu_um": composed_dbu_um,
        "bbox_um": composed_bbox_um,
        "ports": composed_ports,
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


#: Relative tolerance for :func:`_integer_dbu_ratio`'s float-division check.
#: Every dbu value this module ever sees comes from ``1.0 / DATABASE
#: MICRONS`` (an integer denominator, #1512) or a hardcoded decimal
#: constant (``klt draw``'s ``0.001``), so a genuine integer ratio lands
#: within float noise of the nearest whole number -- this tolerance is many
#: orders of magnitude looser than that noise while still being far tighter
#: than any *non*-integer ratio a real DATABASE MICRONS mismatch could
#: produce (e.g. 1000 vs. 1500 -> ratio 1.5).
_DBU_RATIO_TOLERANCE = 1e-6


def _integer_dbu_ratio(coarse_dbu: float, fine_dbu: float) -> int | None:
    """Whether ``coarse_dbu`` is an exact whole-number multiple of
    ``fine_dbu`` -- i.e. every ``fine_dbu``-grid coordinate maps onto the
    ``coarse_dbu`` grid with zero remainder -- returning that integer
    multiplier, or ``None`` when it is not (issue #1514).

    Order matters: this only ever answers "how many ``fine_dbu`` steps make
    up one ``coarse_dbu`` step" -- callers must pass the larger dbu value as
    ``coarse_dbu``. Scaling a block's own integer database-unit coordinates
    *up* by this returned integer (coarser grid -> finer grid) is always
    exact; the reverse direction (finer -> coarser) would require *dividing*
    those coordinates, which is only exact when every single coordinate
    happens to already be a multiple of the ratio -- not guaranteed for
    arbitrary drawn geometry -- so this module never attempts it.
    """
    if fine_dbu <= 0:
        return None
    ratio = coarse_dbu / fine_dbu
    nearest = round(ratio)
    if nearest < 1:
        return None
    if abs(ratio - nearest) > _DBU_RATIO_TOLERANCE * max(1, nearest):
        return None
    return int(nearest)


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
    array_placement: dict[str, Any] | None = None,
    via_drop_size_um: dict[tuple[int, int], float] | None = None,
) -> tuple[float, list[str]]:
    """Write ``output_path``: one new top cell (``cell_name``) instantiating
    every block's own top cell as a translated sub-cell instance, plus any
    routed metal. Returns ``(dbu, rescale_warnings)`` -- ``dbu`` is the
    composed layout's own dbu (:func:`compose` echoes it as the response's
    ``dbu_um``, issue #1496), resolved as the *finest* dbu among all blocks
    (see the reconciliation logic below, issue #1514); ``rescale_warnings``
    is one string per block whose own dbu differed and was rescaled onto it,
    which :func:`compose` folds into the response's top-level ``warnings``.

    Each block's GDS is read into its own scratch :class:`kdb.Layout`, its
    reported top cell (``generator_report.cell_name``) is duplicated
    (:meth:`kdb.Cell.copy_tree`) into a fresh sub-cell of the composed
    layout, and that sub-cell is instantiated into ``cell_name`` at the
    block's computed ``offset_um`` -- geometry is copied exactly once (never
    re-derived), and hierarchy is preserved (each block stays its own cell,
    not flattened into the composed top cell).

    ``array_placement`` (``placement.strategy: "array"``, #1053), when given,
    is ``{"block_id": str, "rows": int, "cols": int, "row_pitch_um": float,
    "col_pitch_um": float}`` naming the one ``order`` entry to instantiate as
    a **single** hierarchical :class:`kdb.CellInstArray` -- ``cols`` steps of
    ``col_pitch_um`` along ``+x`` (the array's ``a`` vector/count) and
    ``rows`` steps of ``row_pitch_um`` along ``+y`` (its ``b`` vector/count),
    based at that block's own ``offset_um`` (the row-0/col-0 tile) -- instead
    of the one-``kdb.Trans``-per-block insert every other block gets. This is
    the mechanism that keeps a large regular tiling (e.g. an R rows x C cols
    bitcell array) to **one** composed-layout instance rather than
    ``rows * cols`` flattened inserts (see the module docstring); confirmed
    by inspecting ``layout.cell(cell_name).each_inst()``'s count in the test
    suite below.

    Routed nets (``routed_geometry``: a list of ``{net, points_um, width_um,
    via_drops, stub_widen, route_layer, label_layer}``) are drawn as native
    :class:`kdb.Path` shapes directly on the composed top cell, top-level
    metal, not inside any block's sub-cell. Each entry's own ``route_layer``
    (falling back to this function's own ``route_layer`` parameter when the
    key is absent) is the ``(layer, datatype)`` pair it draws on -- almost
    always identical across every entry in one request, except a leg that
    fell back to a configured ``routing.cross_block_layer_role`` (issue
    #1168, see :func:`route_two_pin`), which carries that second layer
    instead. When an entry's own ``label_layer`` (falling back to this
    function's ``label_layer`` parameter, resolved by
    :func:`_resolve_label_layer`) is not ``None``, that routed net also gets
    one :class:`kdb.Text` label -- named after its own ``net`` field -- on
    that layer, at the arc-length midpoint of its drawn path
    (:func:`_polyline_midpoint_um`), so `klt extract`'s label-recognition
    convention (``metals[i]``/``metal_labels[i]`` -- see
    :class:`klayout_tools.decks.ExtractionDeck`) promotes the net to a named
    ``.SUBCKT`` pin instead of an anonymous one (#200).

    Each entry's ``via_drops`` (resolved by :func:`route_two_pin`'s check 5,
    issue #454; generalized to a multi-hop ladder by issue #1567) is a list
    of ``{x_um, y_um, via_layer, landing_layers, block_id}`` -- one per via
    *hop* the backbone needs on its way from ``route_layer`` down to its own
    pin's layer. A pin whose own layer is exactly one via hop from
    ``route_layer`` (the pre-#1567 case) gets exactly one entry, with
    ``landing_layers == (route_layer, pin's own layer)``; a pin further down
    the metals stack gets one entry per hop, all at the identical ``(x_um,
    y_um)``, chaining through every intermediate level in between. Each drop
    draws a via square on ``via_layer``, sized to
    ``via_drop_size_um.get(via_layer, _VIA_DROP_SIZE_UM)`` (issue #1501: the
    caller -- :func:`compose` -- resolves this per distinct ``via_layer``
    against the same curated deck ``klt drc`` judges the drawn square with,
    so a family whose ``viaN.width.1`` exceeds the PDK-independent
    ``_VIA_DROP_SIZE_UM`` constant still draws a DRC-clean via; a caller that
    omits ``via_drop_size_um`` -- e.g. a pre-#1501 unit test constructing
    ``routed_geometry`` directly -- keeps exactly ``_VIA_DROP_SIZE_UM`` for
    every drop, unchanged), plus a landing-pad square (``_VIA_LANDING_SIZE_UM``, sized
    independently of the route's own trace width so the via's enclosure
    requirement holds regardless) on *both* of that hop's ``landing_layers``,
    all centered on the pin's exact composed-frame position -- the same
    position the backbone's own drawn ``kdb.Path`` already terminates at, so
    the first hop's landing pad always overlaps (and merges with) both the
    backbone and the block's own existing pad on that layer, and every
    subsequent hop's landing pad merges with the one before it at the same
    point, chaining the full stack down to the pin's own layer.

    Each entry's ``stub_widen`` (:func:`route_two_pin`'s own
    :func:`_endpoint_stub_widen_um`, issue #496) is a list of ``{x_um, y_um,
    direction_deg, length_um, width_um}`` -- one per endpoint whose own
    reported pad is wider than the route's ``width_um``. Each draws one
    :class:`kdb.Box` re-covering the backbone's own first segment out of that
    endpoint (from the pin's position, ``length_um`` along ``direction_deg``
    -- the same span the narrow backbone path already runs) at the *pad's*
    width instead of the route's, on ``route_layer`` -- merging into one
    shape with both the backbone and the block's own pad, so no sub-spacing
    gap is left beside the pad.

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

    # Read every block's own GDS once, up front (#1514). The composed
    # layout's own dbu is resolved as the *finest* (smallest-valued) dbu
    # among all blocks -- not simply the first block processed, as before
    # #1514 -- so every coarser block's own dbu (expected, per DATABASE
    # MICRONS convention, to be a whole-number multiple of the finest one)
    # can be rescaled *up* onto it losslessly: multiplying already-integer
    # database-unit coordinates by an exact integer factor is exact, whereas
    # dividing a finer grid's coordinates *down* onto a coarser one is the
    # direction that can silently drop precision, so that direction is never
    # attempted -- see `_integer_dbu_ratio` below.
    src_layouts: dict[str, Any] = {}
    for block_id in order:
        gds_path = blocks[block_id]["gds_path"]
        src_layout = kdb.Layout()
        try:
            src_layout.read(gds_path)
        except Exception as exc:  # klayout raises RuntimeError for bad formats/paths
            raise GenComposeError(
                f"block '{block_id}': could not read gds_path '{gds_path}': {exc}"
            ) from exc
        src_layouts[block_id] = src_layout

    dbu = min(src_layouts[block_id].dbu for block_id in order)
    layout.dbu = dbu
    dbu_rescale_warnings: list[str] = []

    for block_id in order:
        block = blocks[block_id]
        gds_path = block["gds_path"]
        src_layout = src_layouts[block_id]

        if abs(src_layout.dbu - dbu) > 1e-12:
            # #1514: klt draw (always 0.001) and pre-#1512 `klt gen` output
            # composed against a #1512-and-later `klt gen` block resolved
            # against a finer-grid PDK (e.g. gf180mcu's 0.0005) used to hit
            # a hard refusal here. Reconcile it instead, whenever the ratio
            # is an exact integer, by rescaling this block's geometry onto
            # `dbu` -- the same scale/transform pattern `_merge_gds_view`
            # (place_and_route.py) already uses for the analogous DEF/LEF
            # merge problem (#1090). `kdb.Layout.transform()` rescales every
            # shape *and* every instance array vector in the layout, so a
            # block's own internal hierarchy (and any array pitch inside it)
            # survives the rescale intact.
            ratio = _integer_dbu_ratio(src_layout.dbu, dbu)
            if ratio is None:
                raise GenComposeError(
                    f"block '{block_id}': gds '{gds_path}' has dbu={src_layout.dbu}, "
                    f"which does not evenly divide the composed cell's dbu={dbu} "
                    "-- every block's dbu must be an exact integer multiple of "
                    "the finest block's own dbu to be losslessly reconciled"
                )
            original_dbu = src_layout.dbu
            src_layout.transform(kdb.ICplxTrans(float(ratio)))
            src_layout.dbu = dbu
            dbu_rescale_warnings.append(
                f"block '{block_id}' gds '{gds_path}' was rescaled from its own "
                f"dbu={original_dbu} onto the composed cell's dbu={dbu} "
                f"(exact integer ratio {ratio})"
            )

        src_cell_name = block["cell_name"]
        src_cell = src_layout.cell(src_cell_name)
        if src_cell is None:
            raise GenComposeError(
                f"block '{block_id}': gds '{gds_path}' has no cell named "
                f"'{src_cell_name}' (from its {_block_cell_name_source(block)})"
            )

        sub_cell = layout.create_cell(f"{block_id}__{src_cell_name}")
        sub_cell.copy_tree(src_cell)

        offset = offsets_um[block_id]
        ox = int(round(offset["x"] / dbu))
        oy = int(round(offset["y"] / dbu))
        # blocks[].orientation (#1166): the same kdb.Trans(rot, mirrx, x, y)
        # this block's own bbox_um/ports[] were already conceptually
        # transformed by (_orient_bbox_um/_orient_port, in _parse_blocks) --
        # applying it to the actual cell instance here is what keeps the
        # drawn geometry consistent with that reported metadata.
        rot, mirrx = _ORIENTATION_KDB_ARGS[block.get("orientation", "none")]
        if array_placement is not None and array_placement["block_id"] == block_id:
            # "array" placement (#1053): one hierarchical instance covering
            # every rows*cols tile, not one insert per tile -- the row-0/
            # col-0 tile sits at this block's own offset_um (`ox`/`oy`
            # above), and every other tile is expressed purely through the
            # array's own a/b vectors and counts. The block's own
            # orientation (#1166) is shared by every tile -- verified by
            # klayout.db.CellInstArray's own semantics: its a/b step vectors
            # are added in the *parent* frame, after the instance's own
            # rot/mirrx is applied, exactly like this array's own bbox math
            # (array_placement_bbox_um) already assumes.
            a_vector = kdb.Vector(int(round(array_placement["col_pitch_um"] / dbu)), 0)
            b_vector = kdb.Vector(0, int(round(array_placement["row_pitch_um"] / dbu)))
            top.insert(
                kdb.CellInstArray(
                    sub_cell.cell_index(),
                    kdb.Trans(rot, mirrx, ox, oy),
                    a_vector,
                    b_vector,
                    array_placement["cols"],
                    array_placement["rows"],
                )
            )
        else:
            top.insert(
                kdb.CellInstArray(sub_cell.cell_index(), kdb.Trans(rot, mirrx, ox, oy))
            )

    if routed_geometry and route_layer is not None:
        # Per-entry drawing layer (#1168): each `routed_geometry` entry may
        # carry its own "route_layer"/"label_layer" -- the *effective* layer
        # that particular leg drew on, which can differ from the request's
        # primary `route_layer`/`label_layer` when a same-block self-net leg
        # fell back to a configured `routing.cross_block_layer_role` (see
        # route_two_pin's docstring). `.get(..., route_layer)`/`.get(...,
        # label_layer)` fall back to the primary pair for any entry that
        # omits the key, so a pre-#1168 caller's hand-built entries (or a
        # unit test constructing `routed_geometry` directly) draw exactly as
        # before. `kdb.Layout.layer()` is itself idempotent for a repeated
        # `(layer, datatype)` pair, but resolving it once per distinct pair
        # here avoids a redundant lookup per entry on the common single-layer
        # path.
        layer_indices: dict[tuple[int, int], int] = {}
        label_layer_indices: dict[tuple[int, int], int] = {}

        def _layer_index(pair: tuple[int, int]) -> int:
            index = layer_indices.get(pair)
            if index is None:
                index = layout.layer(pair[0], pair[1])
                layer_indices[pair] = index
            return index

        def _label_layer_index(pair: tuple[int, int]) -> int:
            index = label_layer_indices.get(pair)
            if index is None:
                index = layout.layer(pair[0], pair[1])
                label_layer_indices[pair] = index
            return index

        for route in routed_geometry:
            points = route["points_um"]
            if not points or len(points) < 2:
                continue
            entry_route_layer = route.get("route_layer", route_layer) or route_layer
            entry_label_layer = route.get("label_layer", label_layer)
            layer_index = _layer_index(entry_route_layer)
            path_points = [
                kdb.Point(int(round(x / dbu)), int(round(y / dbu))) for (x, y) in points
            ]
            width_dbu = int(round(route["width_um"] / dbu))
            top.shapes(layer_index).insert(kdb.Path(path_points, width_dbu))

            # `label` is False for every leg of a multi-leg (bundle) net after
            # its first (#1073): the legs are one conductor, so one kdb.Text
            # names it -- exactly as a two-pin net's single path gets exactly
            # one label. Absent/True keeps the pre-#1073 one-label-per-entry
            # behaviour for any other caller.
            if entry_label_layer is not None and route.get("label", True):
                lx_um, ly_um = _polyline_midpoint_um(points)
                label_point = kdb.Point(
                    int(round(lx_um / dbu)), int(round(ly_um / dbu))
                )
                top.shapes(_label_layer_index(entry_label_layer)).insert(
                    kdb.Text(route["net"], kdb.Trans(label_point))
                )

            # Via-drops (#454; generalized to a multi-hop ladder by issue
            # #1567): each entry drops one via hop at exactly the target
            # pin's own position -- a via square on `via_layer`, plus a
            # landing-pad square on *both* of that hop's own
            # `landing_layers` (_VIA_LANDING_SIZE_UM, independent of the
            # route's own trace width) so the via's enclosure requirement
            # holds regardless of how thin routing.width_um is. A single-hop
            # drop's two `landing_layers` are exactly (this leg's own
            # effective layer, the pin's own layer) -- the pre-#1567 shape --
            # and the backbone's own Path already terminates exactly at this
            # same point (manhattan_backbone's endpoints are the raw pin
            # positions), so the landing pad always overlaps -- and merges
            # with -- the trace. A multi-hop ladder instead carries one
            # `via_drops` entry per hop, all at the identical (x, y): drawing
            # each one exactly the same way as the single-hop case builds
            # the full stack -- the backbone's own landing pad from the first
            # hop, the pin's own landing pad from the last hop, and one
            # shared landing pad per intermediate level in between (drawn
            # twice, once by each of its two neighbouring hops -- the same
            # position and size both times, so the duplicate insert is
            # harmless).
            #
            # The via square's own side (#1501) is looked up per `via_layer`
            # in `via_drop_size_um` -- resolved by `compose()` against the
            # same curated deck `klt drc` judges the drawn square with --
            # falling back to the PDK-independent `_VIA_DROP_SIZE_UM`
            # constant for any layer the caller didn't resolve a floor for
            # (an unresolvable PDK family, or a pre-#1501 caller that never
            # populates `via_drop_size_um` at all).
            landing_half_dbu = int(round((_VIA_LANDING_SIZE_UM / 2.0) / dbu))
            for drop in route.get("via_drops", []):
                via_pair = drop["via_layer"]
                via_layer_index = layout.layer(via_pair[0], via_pair[1])
                cx = int(round(drop["x_um"] / dbu))
                cy = int(round(drop["y_um"] / dbu))
                via_size_um = (via_drop_size_um or {}).get(via_pair, _VIA_DROP_SIZE_UM)
                via_half_dbu = int(round((via_size_um / 2.0) / dbu))
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
                for landing_pair in drop.get("landing_layers", ()):
                    top.shapes(layout.layer(landing_pair[0], landing_pair[1])).insert(
                        landing_box
                    )

            # Stub-widen (#496): each entry (route_two_pin's own
            # _endpoint_stub_widen_um) re-draws the backbone's own first
            # segment out of one endpoint -- from that pin's exact position,
            # `length_um` along its own `direction_deg`, exactly the same
            # span the narrow backbone above already covers -- at the pin's
            # own reported `width_um` instead of the route's. Drawn on this
            # entry's own effective layer, the same layer/cell the
            # backbone's own Path is already on, so it merges into one shape
            # with both the backbone and (when the pin's own pad is also on
            # that layer, which is the only case this fires for -- see the
            # docstring) the block's own pad underneath.
            for widen in route.get("stub_widen", []):
                cx = int(round(widen["x_um"] / dbu))
                cy = int(round(widen["y_um"] / dbu))
                half_dbu = int(round((widen["width_um"] / 2.0) / dbu))
                length_dbu = int(round(widen["length_um"] / dbu))
                if widen["direction_deg"] == 90:
                    widen_box = kdb.Box(
                        cx - half_dbu, cy, cx + half_dbu, cy + length_dbu
                    )
                else:  # 270
                    widen_box = kdb.Box(
                        cx - half_dbu, cy - length_dbu, cx + half_dbu, cy
                    )
                top.shapes(layer_index).insert(widen_box)

    if pin_placements:
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
    return layout.dbu, dbu_rescale_warnings
