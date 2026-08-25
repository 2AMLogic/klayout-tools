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
* **Cross-block bus routing** (issue #1168): via-drop routing (above) moves
  the *whole* composition to a second metal, even for nets that never needed
  it. ``routing.cross_block_layer_role`` instead names a second, higher
  metal role only a same-block self-net leg falls back to when it would
  otherwise short across another of that block's own pads on the primary
  ``routing.layer_role`` -- resolved and via-hop-validated by
  :func:`_resolve_cross_block_route_layer`, and applied per leg by
  :func:`route_two_pin`'s same-drawing-layer-short retry. Every other net in
  the same request keeps drawing on ``routing.layer_role`` unchanged.
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
    raw_waypoints: Any, *, net: str, index: int
) -> list[tuple[float, float]] | None:
    """Parse the optional ``connectivity[<index>].waypoints_um`` field (#634).

    ``None``/absent means "no waypoints" (today's behaviour, unchanged) --
    every other value must be a non-empty array of ``[x_um, y_um]`` number
    pairs, forced through in order by :func:`manhattan_backbone` between the
    two ports' own stubs. Malformed input is an application error (exit 1),
    the same treatment every other ``connectivity[]`` field gets.
    """
    if raw_waypoints is None:
        return None
    where = f"request.connectivity[{index}] (net '{net}').waypoints_um"
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

        waypoints_um = _parse_waypoints_um(
            entry.get("waypoints_um"), net=net, index=index
        )
        # A bundle net (>2 pins, #1073) is routed as a spanning tree of legs
        # (see route_bundle) -- a single caller-supplied path has no
        # unambiguous leg to belong to, so combining the two is an application
        # error rather than a silently ignored field.
        if waypoints_um is not None and len(parsed_pins) != 2:
            raise GenComposeError(
                f"request.connectivity[{index}] (net '{net}').waypoints_um is "
                f"only supported for a 2-pin net -- this net has "
                f"{len(parsed_pins)} pins, which routes as a spanning tree of "
                "two-pin legs, and a single waypoint path cannot be attributed "
                "to one of them; split the net into 2-pin connectivity[] "
                "entries to steer individual legs"
            )
        connectivity.append(
            {"net": net, "pins": parsed_pins, "waypoints_um": waypoints_um}
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


#: ``direction_deg`` values a *stub-widen* (#496) applies to -- a port's own
#: outward-facing north/south stub, never an east/west one. Scoped this
#: narrowly per the issue: an east/west-facing port's horizontal stub is left
#: byte-for-byte unchanged.
_STUB_WIDEN_DIRECTIONS = (90, 270)


def _endpoint_stub_widen_um(
    port: dict[str, Any],
    pos: tuple[float, float],
    direction_deg: int,
    stub_len_um: float,
    width_um: float,
    route_layer: tuple[int, int] | None,
) -> dict[str, Any] | None:
    """Whether ``port``'s own stub (issue #496) needs widening past
    ``width_um``, and if so, the entry :func:`_write_composed_gds` draws for
    it.

    A north/south-facing port's *drawn* pad can be wider than the route's own
    ``width_um`` -- :func:`manhattan_backbone`'s stub still leaves the pad at
    ``width_um``, so the pad's edge outside the stub's narrow footprint (but
    inside the pad's own footprint) can sit closer to the perpendicular jog
    above/below it than the target deck's same-layer spacing rule allows (a
    real DRC violation, not a routability question -- see the issue). Mirrors
    the via-drop landing pad's own precedent (``_VIA_LANDING_SIZE_UM``, sized
    independent of ``width_um`` for the same enclosure reason): widen just the
    stub segment leaving the pad -- from the port out to where the
    un-widened stub already ends (``stub_len_um``, the caller's own
    ``stub_a_um``/``stub_b_um``) -- to the *port's own reported* ``width_um``,
    rather than the whole backbone.

    Only fires when: the port faces north/south (``_STUB_WIDEN_DIRECTIONS`` --
    an east/west-facing port's horizontal stub is untouched, see the issue);
    the port's own reported ``width_um`` actually exceeds the route's
    ``width_um`` (an equal or narrower pad is already a no-op); and the pad is
    actually drawn on ``route_layer`` (a port whose own layer differs needs a
    via-drop instead -- see :func:`_resolve_via_drop_layer` -- and its real
    pad lives on a different layer, where this trace-width mismatch does not
    apply). Not keyed on any port-name convention (``U*_G``, ``TAP_*``, ...)
    -- purely geometric, so it generalizes to any generator's north/south
    port, not just a gate contact's.
    """
    if direction_deg not in _STUB_WIDEN_DIRECTIONS:
        return None
    if route_layer is None or _port_own_layer(port) != route_layer:
        return None
    pad_width_um = port.get("width_um")
    if not isinstance(pad_width_um, (int, float)) or isinstance(pad_width_um, bool):
        return None
    pad_width_um = float(pad_width_um)
    if pad_width_um <= width_um:
        return None
    return {
        "x_um": pos[0],
        "y_um": pos[1],
        "direction_deg": direction_deg,
        "length_um": stub_len_um,
        "width_um": pad_width_um,
    }


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
      ``route_layer`` (the pre-#454 single-metal routing path, unchanged), or
      ``route_layer`` itself is not a member of ``deck.metals`` (a
      ``"poly"``/``"tap"``-role backbone has no metals stack to walk, so it
      draws directly exactly as it always has).
    * ``(via_pair, None)`` -- a drop is needed and resolved; draw a via on
      ``via_pair`` at the pin's own position.
    * ``(None, reason)`` -- a drop is needed but not resolvable (the two
      metals-stack levels are more than one via hop apart, the deck declares
      no via for the hop needed, or the pin sits on the deck's bare ``poly``
      layer, which no via in the metals stack reaches) -- the caller reports
      the net unroutable rather than drawing a disconnected short.

    That last case is issue #492: before it, a metal-role backbone ending on
    a bare-poly gate port fell into the ``(None, None)`` "unrelated role,
    nothing to do" branch and was drawn anyway -- ``"routed": true``, no
    note, and a metal stub sitting *over* the gate poly with no contact
    joining the two. That is an open net (or, where the stub crosses other
    geometry, a short) that only a later ``klt drc``/``klt extract``/``klt
    lvs`` run would surface, with nothing pointing back at the cause. It is
    now an explicit rejection naming the fix.
    """
    if route_layer == port_layer:
        return None, None
    try:
        route_idx = deck.metals.index(route_layer)
    except ValueError:
        # The backbone itself is not on a declared routing-metal level (e.g.
        # routing.layer_role "tap"): there is no metals stack to drop through,
        # so draw directly on route_layer exactly as before #454.
        return None, None
    try:
        port_idx = deck.metals.index(port_layer)
    except ValueError:
        if port_layer == deck.poly:
            return None, (
                "this pin is a bare-poly gate -- gen_compose draws no poly "
                "contact, so the backbone would end as an uncontacted metal "
                "stub over the gate rather than connecting to it. Re-run this "
                "block's generator with params.gate_contact=true so the gate "
                "reports a contacted metal landing pad (issue #492), or name "
                "the gate with pins[] instead of routing to it"
            )
        # Any other non-metals-stack role (e.g. a guard ring's active/tap
        # port, which the ring's own metal already covers at that position)
        # keeps the pre-#454 behavior: drawn directly on route_layer, no via.
        return None, None
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


def _resolve_cross_block_route_layer(
    variant: str, layer_role: str, cross_layer_role: str
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Resolve ``routing.cross_block_layer_role`` to ``(cross_route_layer,
    via_layer)`` (issue #1168).

    ``routing.layer_role`` resolves to exactly one ``(layer, datatype)`` pair
    for the whole composition (:func:`_resolve_route_layer`) -- a net whose
    backbone must cross an *intermediate* block's own same-layer pin (the
    canonical case: a same-block self-net leg bussing two of a matched
    array's terminals together across a third terminal sitting between them,
    see :func:`route_two_pin`'s checks 3/4) has no escape route: the single
    global layer either shorts to that pad or the whole composition has to
    move to a second layer, even for nets that never needed it. This resolves
    an optional *second*, higher metal role -- the one :func:`route_two_pin`
    retries a same-layer-short leg on instead of rejecting it outright -- via
    the *same* ``_PDK_ROLE_LAYERS`` table :func:`_resolve_route_layer` reads
    (never a second, private layer map).

    Reuses :func:`_resolve_via_drop_layer` verbatim to confirm the two
    resolved layers are exactly one via hop apart in the resolved PDK
    family's own ``ExtractionDeck.metals``/``.vias`` stack, and to resolve
    which via connects them -- the identical hop-resolution logic
    :func:`route_two_pin`'s check 6 (via-drop) already relies on, called here
    with the roles reversed (``route_layer`` slot = the cross-block layer,
    ``port_layer`` slot = the primary ``layer_role``) rather than
    reimplemented a second time.

    Raises :class:`GenComposeError` when either role does not resolve (the
    same errors :func:`_resolve_route_layer` raises), when the two roles
    resolve to the identical layer, or when they are not connectable by a
    single via hop (non-adjacent metals-stack levels, one/both roles outside
    the metals stack entirely, or no via declared for that hop).
    """
    route_layer = _resolve_route_layer(variant, layer_role)
    try:
        cross_layer = _resolve_route_layer(variant, cross_layer_role)
    except GenComposeError as exc:
        # _resolve_route_layer's own message names the field "routing.
        # layer_role" regardless of which caller-facing field is actually
        # invalid -- re-labelled here so a bad cross_block_layer_role points
        # a caller at the right request field, not the primary one.
        raise GenComposeError(
            str(exc).replace("routing.layer_role", "routing.cross_block_layer_role")
        ) from exc
    if cross_layer == route_layer:
        raise GenComposeError(
            f"routing.cross_block_layer_role '{cross_layer_role}' resolves to "
            f"the same layer as routing.layer_role '{layer_role}' -- a "
            "cross-block bus layer must be a distinct metal"
        )
    deck = get_extraction_deck(_pdk_family(variant))
    via_layer, error = _resolve_via_drop_layer(deck, cross_layer, route_layer)
    if via_layer is None:
        reason = error or (
            f"'{cross_layer_role}' and '{layer_role}' are not both members of "
            "the PDK's metals/via stack"
        )
        raise GenComposeError(
            f"routing.cross_block_layer_role '{cross_layer_role}' cannot be "
            f"connected to routing.layer_role '{layer_role}' by a single via "
            f"hop: {reason}"
        )
    return cross_layer, via_layer


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
    waypoints: list[tuple[float, float]] | None = None,
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

    ``waypoints`` (#634) is an optional caller-supplied ordered list of
    ``(x, y)`` points the backbone is forced through, between port ``a``'s
    stub and port ``b``'s stub -- the fixed-shape jog logic above is skipped
    entirely when it is given. This is the escape hatch for a pair the
    fixed shape can never route (most notably two ports facing the *same*
    absolute direction in a row placement, where the one-jog shape's jog
    lands inside the upstream block's own bbox): the caller supplies the
    routing knowledge the fixed shape lacks (e.g. a point above the row's
    shared bbox top), and every consecutive pair in ``[sa, *waypoints, sb]``
    that does not already share an x or a y gets exactly one elbow corner
    inserted between them (move in x first, then y -- the same corner
    convention the mixed-orientation case above uses), so the result stays a
    strictly axis-aligned polyline like every other backbone this function
    returns. The caller is responsible for choosing waypoints that actually
    clear whatever obstacle motivated them -- :func:`route_two_pin`'s
    existing routability checks (including the obstacle-overlap check) still
    run against the resulting path and reject it exactly as they would any
    other backbone if it does not.

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

    if waypoints:
        chain = [sa, *waypoints, sb]
        points: list[tuple[float, float]] = [a, chain[0]]
        for p, q in zip(chain, chain[1:], strict=False):
            same_x = abs(p[0] - q[0]) < 1e-9
            same_y = abs(p[1] - q[1]) < 1e-9
            if not (same_x or same_y):
                points.append((q[0], p[1]))  # elbow: x first, then y
            points.append(q)
        points.append(b)
        return _cleanup_points(points)

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


#: How far past a bbox edge a detour lane (:func:`_detour_lane_waypoints_um`)
#: is placed, as a multiple of the route width. The obstacle-overlap check
#: itself only demands ``width_um / 2`` (half the drawn conductor), so a lane
#: at ``2 * width_um`` leaves ``1.5 * width_um`` of clear space between the
#: drawn metal's edge and the block it routes around -- clear by a comfortable
#: margin rather than legal-by-a-hair, which is what keeps ``klt drc``'s
#: same-layer spacing rule satisfied against whatever that block draws at its
#: own bbox edge.
_DETOUR_CLEARANCE_WIDTHS = 2.0

#: How many alternate lanes the bounded detour search tries before reporting
#: the net unroutable (#1167). Two -- one on each side of the obstacles -- is
#: the "1-2 alternate jog points" bound the issue asks for, and is what keeps
#: this a *bounded* search rather than a general maze router: there is no
#: recursion (a lane is never itself detoured around) and no per-obstacle
#: growth (see :func:`_detour_lane_waypoints_um`).
_MAX_DETOUR_LANES = 2


def _detour_escape_um(
    pos: tuple[float, float],
    vec: tuple[int, int],
    bbox_um: dict[str, float],
    width_um: float,
    axis: str,
) -> float:
    """Where one endpoint leaves its own block on its way to a detour lane.

    ``axis`` is the axis the *lane* runs along (``"x"`` for a horizontal lane
    over/under a row, ``"y"`` for a vertical lane left/right of a column), so
    this returns the coordinate of the leg that carries the route from the
    port out to that lane -- the lane's entry column for a horizontal lane,
    its entry row for a vertical one.

    A port facing *along* the lane's own axis leaves sideways past its own
    block's bbox edge, by ``_DETOUR_CLEARANCE_WIDTHS * width_um`` (at least as
    far as its own stub end). Clearing the **bbox** rather than the stub is
    what keeps the turn DRC-clean: a port's own pad is routinely wider than
    ``width_um`` (a ``resistor_strip`` end pad is 0.42um for a 0.17um route),
    so a leg turning at the stub end leaves a slot between the pad's far
    corner and the leg -- same net, but still a ``li1.space``-class spacing
    violation, exactly the sub-spacing slit issue #496 fixes for a north/
    south-facing stub. A port facing *across* the lane leaves straight out
    along its own facing direction instead (its stub already points at the
    lane), where the #496 stub-widen handles the same pad-vs-trace step.

    **Same-block self-net caveat (#1179, investigated and confirmed safe.)**
    For a same-block self-net (``bbox_a is bbox_b`` at the call site), if the
    two ports face *opposite* ways along the lane's axis, each one's escape
    is pushed toward the *opposite* edge of that one shared bbox -- e.g. a
    port facing ``+x`` pushes toward the bbox's east edge regardless of
    which side of the block it actually sits on. The resulting entry/exit
    leg (drawn at the port's own coordinate on the lane's cross-axis, from
    the stub straight out to that far escape point) can then run differently
    from the short hop past the near edge a caller would expect, depending on
    which way the two ports face relative to each other:

    - **Facing each other** (e.g. the west port facing ``+x``, the east port
      facing ``-x``) -- each escape point is pushed *past* the other port,
      so the entry/exit leg runs back across whatever sits between the two
      ports at their own level. This leg is always a superset of the region
      the original (rejected) straight backbone ran through, so any obstacle
      that made the unrouted backbone crossable is also crossed by the
      malformed leg, and :func:`route_two_pin`'s obstacle-overlap check (run
      in full against the detour's actual drawn points, exactly as for any
      other candidate path) rejects it the same way -- "reported unroutable"
      is the accepted terminal behaviour for this sub-case. Verified with a
      targeted fixture
      (``test_route_two_pin_self_net_opposite_facing_detour_rejects_cleanly``
      in ``tests/test_gen_compose.py``) and a 30k-trial randomized sweep
      restricted to this orientation that found zero wrongly-accepted paths.
    - **Facing away from each other** (e.g. the west port facing ``-x``, the
      east port facing ``+x``) -- each escape point is instead pushed *away*
      from the other port, past the bbox's own far edge on its own side, so
      neither entry/exit leg overlaps the original backbone's range at all
      (disjoint, not a superset). The detour lane then draws a full loop
      around the entire block rather than being rejected -- a valid,
      non-crossing (if very indirect) path, not a silently bad one. Verified
      with a targeted fixture
      (``test_route_two_pin_self_net_away_facing_detour_loops_around_cleanly``
      in ``tests/test_gen_compose.py``) and a ~2.8k-trial randomized sweep
      restricted to this orientation that found zero wrongly-crossing paths,
      with about a quarter of trials routing successfully via the loop-around
      rather than being rejected.

    Both outcomes were suspected to still fail safe rather than draw a
    silently bad path, and that has been confirmed for each: no fix is
    needed here, but "reported unroutable" is *not* the terminal behaviour
    for every same-block opposite-facing self-net -- only for the
    facing-each-other sub-case above.
    """
    if axis == "x":
        if vec[0] > 0:
            return max(
                pos[0] + width_um, bbox_um["x1"] + _DETOUR_CLEARANCE_WIDTHS * width_um
            )
        if vec[0] < 0:
            return min(
                pos[0] - width_um, bbox_um["x0"] - _DETOUR_CLEARANCE_WIDTHS * width_um
            )
        return pos[0]
    if vec[1] > 0:
        return max(
            pos[1] + width_um, bbox_um["y1"] + _DETOUR_CLEARANCE_WIDTHS * width_um
        )
    if vec[1] < 0:
        return min(
            pos[1] - width_um, bbox_um["y0"] - _DETOUR_CLEARANCE_WIDTHS * width_um
        )
    return pos[1]


def _detour_lane_waypoints_um(
    end_a: tuple[tuple[float, float], tuple[int, int], dict[str, float]],
    end_b: tuple[tuple[float, float], tuple[int, int], dict[str, float]],
    obstacle_bboxes_um: list[dict[str, float]],
    placed_bboxes_um: dict[str, dict[str, float]],
    width_um: float,
) -> list[list[tuple[float, float]]]:
    """Alternate waypoint paths that route *around* one or more obstacles.

    ``end_a``/``end_b`` are the two endpoints as ``(position, direction
    vector, own placed bbox)``, and ``obstacle_bboxes_um`` are the placed
    bboxes the fixed-shape backbone was rejected for crossing. Each returned
    candidate is a ``waypoints`` list for :func:`manhattan_backbone`
    describing one **lane**: a single straight run, perpendicular to the axis
    the two endpoints are mostly separated along, placed clear of every block
    in the way, reached from each port by one leg out to it
    (:func:`_detour_escape_um`). So the drawn path is ``a -> stub -> out to
    the lane -> along the lane -> back in -> b`` -- every consecutive pair of
    points shares an x or a y, so ``manhattan_backbone`` inserts no elbows of
    its own and the result is exactly the hand-supplied ``waypoints_um``
    shape a caller writes today for the same job (#634).

    The lane is placed past the extreme edge of **every** block whose extent
    the run spans (plus the obstacles themselves, which is what makes the
    result actually clear them), offset by ``_DETOUR_CLEARANCE_WIDTHS *
    width_um``. That single union is why the number of obstacles between the
    pins costs nothing: one lane over the top of the row clears two blocks
    exactly as it clears one. What *is* bounded is the number of candidates --
    at most ``_MAX_DETOUR_LANES``, the two sides of the row -- ordered
    shortest-detour first, with a stable tie-break (over before under, right
    before left) so the composed GDS stays byte-reproducible (#320).

    Returns candidates in try-order; the caller re-runs every routability
    check against each drawn path and keeps the first that passes.
    """
    (a, va, bbox_a), (b, vb, bbox_b) = end_a, end_b
    clearance_um = _DETOUR_CLEARANCE_WIDTHS * width_um
    sa = (a[0] + va[0] * width_um, a[1] + va[1] * width_um)
    sb = (b[0] + vb[0] * width_um, b[1] + vb[1] * width_um)

    if abs(sb[0] - sa[0]) >= abs(sb[1] - sa[1]):
        # Endpoints mostly separated in x -> the connecting run travels in x,
        # so the two ways around are a horizontal lane over or under the row.
        enter = _detour_escape_um(a, va, bbox_a, width_um, "x")
        leave = _detour_escape_um(b, vb, bbox_b, width_um, "x")
        lo, hi = sorted((enter, leave))
        boxes = [
            *obstacle_bboxes_um,
            *(
                bbox
                for bbox in placed_bboxes_um.values()
                if bbox["x0"] <= hi and bbox["x1"] >= lo
            ),
        ]
        over_y = max(bbox["y1"] for bbox in boxes) + clearance_um
        under_y = min(bbox["y0"] for bbox in boxes) - clearance_um
        lanes = [
            [
                (enter, sa[1]),
                (enter, lane_y),
                (leave, lane_y),
                (leave, sb[1]),
            ]
            for lane_y in (over_y, under_y)
        ]
        lanes.sort(key=lambda wp: abs(wp[1][1] - sa[1]) + abs(wp[2][1] - sb[1]))
    else:
        enter = _detour_escape_um(a, va, bbox_a, width_um, "y")
        leave = _detour_escape_um(b, vb, bbox_b, width_um, "y")
        lo, hi = sorted((enter, leave))
        boxes = [
            *obstacle_bboxes_um,
            *(
                bbox
                for bbox in placed_bboxes_um.values()
                if bbox["y0"] <= hi and bbox["y1"] >= lo
            ),
        ]
        right_x = max(bbox["x1"] for bbox in boxes) + clearance_um
        left_x = min(bbox["x0"] for bbox in boxes) - clearance_um
        lanes = [
            [
                (sa[0], enter),
                (lane_x, enter),
                (lane_x, leave),
                (sb[0], leave),
            ]
            for lane_x in (right_x, left_x)
        ]
        lanes.sort(key=lambda wp: abs(wp[1][0] - sa[0]) + abs(wp[2][0] - sb[0]))
    return lanes[:_MAX_DETOUR_LANES]


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
    geometry on the ``(layer, datatype)`` pair the route is drawn on, mirrored/
    rotated per the block's own ``orientation`` (#1166) and translated by the
    block's placed ``offset_um``, in integer database units -- or ``None``
    when the block draws nothing there. The same ``kdb.Trans`` (mirror-then-
    translate) is applied here as :func:`_write_composed_gds` applies to this
    block's actual cell instance, so this obstacle geometry always agrees
    with what the composed GDS actually draws.

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
            f"'{src_cell_name}' (from its {_block_cell_name_source(block)})"
        )

    dbu = src_layout.dbu
    layer_index = src_layout.find_layer(layer[0], layer[1])
    if layer_index is None:
        return None

    region = kdb.Region(src_cell.begin_shapes_rec(layer_index))
    region.merge()
    if region.is_empty():
        return None
    rot, mirrx = _ORIENTATION_KDB_ARGS[block.get("orientation", "none")]
    region.transform(
        kdb.Trans(
            rot,
            mirrx,
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


def _drawn_leg_footprint_region(
    points_um: list[tuple[float, float]],
    width_um: float,
    via_drops: list[dict[str, Any]],
    stub_widen: list[dict[str, Any]],
    dbu: float,
):
    """The full metal one routed leg draws on its own route layer, as a
    ``kdb.Region`` -- the bare backbone (:func:`_drawn_route_region`) plus
    every via-drop landing pad and stub-widen box :func:`_write_composed_gds`
    *also* draws on that same layer (issue #1197).

    The route-vs-route collision check (#1057) originally built its
    comparison region from the bare backbone path alone. That misses two
    kinds of metal :func:`_write_composed_gds` draws wider than the
    backbone itself, on the identical layer:

    * a via-drop's landing pad (``_VIA_LANDING_SIZE_UM``, sized from the
      PDK's own contact-enclosure convention, independent of the route's own
      ``width_um``) -- drawn centered on the leg's own endpoint whenever
      that endpoint's pin sits on a different physical layer than the route;
    * a stub-widen box (:func:`_endpoint_stub_widen_um`, #496) -- drawn
      re-covering the backbone's own first segment at the pin's wider pad
      width, when that pad is wider than the route.

    Either can extend past the bare backbone far enough to land on a
    *different* net's already-accepted route while the two backbones
    themselves never touch -- exactly what two same-block self-nets whose
    via-drop landing pads cross each other's backbones produce: each leg
    individually passes every one of :func:`route_two_pin`'s own checks
    (which only ever compare a leg against its *own* block's geometry), and
    the bare-backbone-only version of this check found no overlap either, so
    both compose ``routed: true`` while `klt extract` merges the two nets
    onto one node through the landing pad's silent overlap. Building this
    region from the *same* drawing primitives :func:`_write_composed_gds`
    actually inserts -- just as :func:`_drawn_route_region` already does for
    the bare path -- is what keeps this check and the composed output from
    ever disagreeing about a leg's real footprint.
    """
    import klayout.db as kdb

    region = _drawn_route_region(points_um, width_um, dbu)

    landing_half_dbu = int(round((_VIA_LANDING_SIZE_UM / 2.0) / dbu))
    for drop in via_drops:
        cx = int(round(drop["x_um"] / dbu))
        cy = int(round(drop["y_um"] / dbu))
        region.insert(
            kdb.Box(
                cx - landing_half_dbu,
                cy - landing_half_dbu,
                cx + landing_half_dbu,
                cy + landing_half_dbu,
            )
        )

    for widen in stub_widen:
        cx = int(round(widen["x_um"] / dbu))
        cy = int(round(widen["y_um"] / dbu))
        half_dbu = int(round((widen["width_um"] / 2.0) / dbu))
        length_dbu = int(round(widen["length_um"] / dbu))
        if widen["direction_deg"] == 90:
            region.insert(kdb.Box(cx - half_dbu, cy, cx + half_dbu, cy + length_dbu))
        else:  # 270
            region.insert(kdb.Box(cx - half_dbu, cy - length_dbu, cx + half_dbu, cy))

    return region


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
    waypoints_um: list[tuple[float, float]] | None = None,
    cross_block_route_layer: tuple[int, int] | None = None,
    cross_block_geometry: dict[str, dict[str, Any] | None] | None = None,
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
       without overlapping a block. Evaluated from the raw port positions
       and the inter-block gap -- i.e. it is a property of the *fixed*
       one-jog shape, not of the drawn path -- so it is skipped entirely
       when ``waypoints_um`` is supplied (#634); see below.
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
       ``conservative_same_dir`` block below. That degenerate-jog fallback is
       likewise a property of the fixed shape rather than of the drawn path,
       so it too is skipped when ``waypoints_um`` is supplied (#634); the
       pad-footprint test itself still runs against the drawn ``points``.
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
       cross in a longer row. Every bbox tested here is inflated by
       ``width_um / 2`` on every side first (#999), so a centerline that
       merely runs close enough alongside a block's edge -- closer than half
       the route's own drawn width -- is caught too, not just one that
       crosses the zero-width centerline through the block's true interior;
       each own-pin's allowance is bumped by the same ``width_um / 2`` so a
       normal approach into that pin's own block is not penalized by the
       inflation. A crossing this check finds is not automatically fatal
       (#1167): when the *only* blocks crossed are ones neither pin sits on,
       the net is retried around them first -- see "Bounded detour search"
       below -- and reported unroutable only if no alternate lane clears
       them either.
    6. **Via-drop resolution** (#454): when ``route_layer`` and a pin's own
       reported layer differ, :func:`_resolve_via_drop_layer` looks up
       whether ``extraction_deck`` connects the two with a single via hop
       (e.g. ``routing.layer_role: "metal2"`` backbone reaching a
       ``"metal"``-role li1 pad via sky130's ``mcon``). A pin whose own layer
       *is* ``route_layer`` needs no drop; a pin on an unrelated role a
       route-layer shape already covers (e.g. a guard ring's active/tap port)
       is left exactly as before #454 (drawn directly on ``route_layer``, no
       via). A pin whose layer is a *different* ``deck.metals`` level than
       ``route_layer`` and more than one via hop away, **or** a pin on the
       deck's bare ``poly`` layer (a gate drawn without
       ``params.gate_contact``, issue #492), is rejected here -- reported
       unroutable rather than drawing a disconnected short or an uncontacted
       stub.

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
    via-drop, exactly as before that issue. ``waypoints_um`` (#634) is
    likewise optional -- ``None``/omitted preserves today's fixed-shape
    backbone exactly (:func:`manhattan_backbone`'s default one-jog/corner
    shape); when given, it is threaded straight into
    :func:`manhattan_backbone`, whose own docstring covers how the path is
    built from it. Every check that measures the *drawn path* runs against
    whatever ``points`` results either way -- a waypoint-supplied path is not
    exempt from the obstacle-overlap check (5), the ring-opening check
    (#434), the pad-footprint half of check 3, check 4, or check 6 just
    because the caller supplied it. What supplying waypoints *does* switch
    off is the handful of heuristics that never look at ``points`` at all
    because they are predictions about the fixed one-jog shape: the
    channel-width check (1), check 3's ``conservative_same_dir``
    degenerate-jog fallback, and the #461 recessed-stub lift. Each of those
    reasons from the raw port positions and block bboxes on the assumption
    that ``manhattan_backbone`` will draw its default shape; once the caller
    supplies a path, that shape is not drawn, so the prediction describes a
    route that does not exist and would reject perfectly good route-arounds
    (the tightly-packed-row case -- inter-block gap narrower than
    ``width_um`` -- being exactly the one waypoints exist to solve). The
    closed guard/collector-ring check (2) is *not* in that group and still
    runs: a closed ring encloses its block completely, so no choice of
    waypoints can reach an interior port without crossing it.

    ``cross_block_route_layer``/``cross_block_geometry`` (issue #1168) name an
    optional *second*, higher metal ``(layer, datatype)`` pair (resolved by
    :func:`_resolve_cross_block_route_layer` from ``routing.
    cross_block_layer_role``) and its own drawn-geometry cache
    (:func:`read_block_layer_geometry` result per block, mirroring
    ``block_geometry`` but for the cross layer) -- both optional and both
    ``None`` unless the caller configured a cross-block bus layer. When
    checks 3/4 above reject a leg because it crosses another same-block port
    or drawn pad *on the primary ``route_layer``*, and a cross-block layer is
    configured, the identical pair of checks is retried against
    ``cross_block_route_layer``/``cross_block_geometry`` instead of failing
    outright; a leg that resolves on the cross layer draws there for its
    *entire* length (not just the crossing segment), with checks 5/6 and the
    stub-widen pass all re-run against that same effective layer, including
    an endpoint via-drop (check 6) wherever a pin's own reported layer is the
    primary ``route_layer`` rather than the cross layer itself -- exactly the
    same via-drop resolution :func:`_resolve_via_drop_layer` already performs
    for a caller-selected ``routing.layer_role: "metal2"``, just decided
    per-leg instead of for the whole composition. A leg that never trips
    checks 3/4 on the primary layer, or that trips them with no cross-block
    layer configured, is entirely unaffected -- this is an additive fallback,
    not a change to any existing single-layer behaviour.

    **Bounded detour search (#1167).** Rejecting every backbone that crosses
    a third block's bbox means only *immediately adjacent* blocks in a row
    can ever be wired -- a real block's netlist is not a Hamiltonian path
    over its devices, so most of its nets never had a chance (issue #1164
    measured 0/8, 0/9 and 0/9 nets routed across three real gf180mcu
    blocks). So when check 5 rejects a path **solely** because of blocks
    neither pin sits on, this function retries the same pin pair around them
    before giving up: :func:`_detour_lane_waypoints_um` proposes up to
    ``_MAX_DETOUR_LANES`` alternate *lanes* (a straight run over/under, or
    left/right of, every block in the way, ordered shortest-detour first),
    and each is routed by a recursive call to this same function with the
    lane as ``waypoints_um``. That is the whole mechanism -- a detour is
    just a route this function generated for itself instead of the caller
    generating it, so **every check above applies to it unchanged**; nothing
    is waived to make a detour fit, and a lane that would cross a block's
    drawn geometry, a ring, or another pad is rejected exactly as a
    caller-supplied path would be. The search is bounded on both sides: the
    retry supplies ``waypoints_um``, and a waypoint-supplied path is never
    itself detoured (one level of recursion, at most two extra attempts per
    net); and the lane is placed clear of *every* block it spans, so two
    obstacles between the pins cost exactly one lane, not two nested
    detours. What is deliberately **not** detoured is a backbone crossing
    one of its own two pins' blocks (the same-facing port pair): that is a
    statement about which way the ports face, and its remedy stays the
    caller-supplied ``waypoints_um`` of #634. When no lane works, the
    reported ``reason`` is check 5's original wording plus a note that the
    detour was tried, so a caller can tell "there was no way around" from
    "no attempt was made".

    Returns ``{"routed": bool, "route_length_um": float | None,
    "points_um": list | None, "via_drops": list, "stub_widen": list,
    "route_layer": tuple[int, int] | None, "reason": str | None}``.
    ``via_drops`` is a list of ``{"x_um", "y_um", "via_layer",
    "port_layer"}`` entries (empty unless a drop was resolved), consumed by
    :func:`_write_composed_gds` to draw each drop's via + landing pads.
    ``stub_widen`` (issue #496, see :func:`_endpoint_stub_widen_um`) is a list
    of ``{"x_um", "y_um", "direction_deg", "length_um", "width_um"}`` entries
    -- one per endpoint whose own reported pad is wider than ``width_um`` and
    faces north/south -- consumed by :func:`_write_composed_gds` to re-draw
    that endpoint's own first backbone segment at the pad's width, closing
    the sub-spacing gap a narrower stub would otherwise leave beside it.
    ``route_layer`` (issue #1168) is the *effective* drawing layer this
    result's geometry actually landed on -- ``route_layer`` (the parameter)
    unless the cross-block retry above fired, in which case it is
    ``cross_block_route_layer`` -- so a caller drawing multiple legs on
    different layers within one net (:func:`route_bundle`) or one composition
    (``compose()``) knows which layer each leg's ``points_um``/``via_drops``
    belong on.
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
    #
    # Skipped entirely when the caller supplies waypoints_um (#634), for the
    # same reason the #461 stub lift below is: this heuristic is a statement
    # about the *fixed one-jog shape*, computed from the raw port positions
    # and the inter-block gap rather than from the path actually drawn. Once
    # waypoints are supplied that jog is never drawn at all -- manhattan_
    # backbone routes through the caller's own points -- so a narrow channel
    # the caller's path deliberately avoids (routing over the row, say) is
    # not a reason to reject the net. Tightly packed rows, where the gap is
    # narrower than the route width, are exactly the case a caller most needs
    # a route-around for, so leaving this check unconditional silently
    # defeated waypoints_um precisely where it is most useful. The path that
    # does get drawn is still fully checked below: the obstacle-overlap check
    # (#199 case 1) rejects any backbone that actually crosses a block's
    # interior, including through this same channel.
    if waypoints_um is None and pin_a["block"] != pin_b["block"]:
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
    # the default stub, so no existing route changes. Skipped entirely when
    # the caller supplies waypoints_um (#634): the automatic jog this lift
    # exists to clear is not drawn at all in that case (manhattan_backbone
    # routes through the caller's own points instead), so stretching the
    # stub here would just move the endpoint the caller's waypoints are
    # relative to, out from under them.
    stub_a_um = stub_b_um = stub_um
    if (
        waypoints_um is None
        and pin_a["block"] != pin_b["block"]
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
        a,
        dir_a,
        b,
        dir_b,
        stub_um,
        stub_a_um=stub_a_um,
        stub_b_um=stub_b_um,
        waypoints=waypoints_um,
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
    #
    # width_um-aware inflation (#999): the check above treats the backbone as
    # a zero-width centerline, but the wire actually drawn is `width_um` wide
    # -- it extends `width_um / 2` past the centerline on every side, the
    # same Minkowski-sum inflation the self-net pad-crossing check (#433,
    # below) and the ring-opening check (`_ring_gap_route_conflict`) already
    # apply. A centerline that clears a block's bbox edge by less than
    # `width_um / 2` therefore still draws metal on top of that block even
    # though the old zero-width test reports no crossing at all -- most
    # commonly a same-facing port pair's connecting jog running parallel to,
    # and just outside, its own origin block's edge. Every bbox this check
    # tests against is inflated by `width_um / 2` on every side before the
    # interior-overlap test runs (see `obstacle_bboxes_um` below); each own-
    # pin's allowance is bumped by the same `width_um / 2` to compensate --
    # without it, the inflated bbox would eat into the own-pin's normal
    # approach margin and reject routes that were always fine (the approach
    # stub's own dip into its own block, `_port_edge_margin_um`, is a
    # statement about *insertion depth*, not about the wire's width).
    own_a, own_b = pin_a["block"], pin_b["block"]
    same_block_self_net = own_a == own_b
    obstacle_half_um = width_um / 2.0
    obstacle_bboxes_um = {
        block_id: {
            "x0": bbox["x0"] - obstacle_half_um,
            "y0": bbox["y0"] - obstacle_half_um,
            "x1": bbox["x1"] + obstacle_half_um,
            "y1": bbox["y1"] + obstacle_half_um,
        }
        for block_id, bbox in placed_bboxes_um.items()
    }
    allowances_um: dict[str, float] = {}
    if not same_block_self_net:
        allowances_um[own_a] = (
            max(0.0, _port_edge_margin_um(a, dir_a, placed_bboxes_um[own_a]))
            + obstacle_half_um
        )
        allowances_um[own_b] = (
            max(0.0, _port_edge_margin_um(b, dir_b, placed_bboxes_um[own_b]))
            + obstacle_half_um
        )

    # Self-net pad-crossing check (#433) and self-net drawn-metal check
    # (#453/#469), factored into a closure parameterised on which layer is
    # under test: issue #1168 retries these two checks (and only these two --
    # the ones the checks' own rejection text below points a caller at a
    # "layer_role with a metal2/via stack") against an optional
    # ``cross_block_route_layer`` when the primary ``route_layer`` rejects, so
    # a same-block bus that must cross an intermediate pad on the same
    # drawing layer can escape to the configured second metal instead of
    # failing outright. See the retry drive code just below this def.
    def _same_layer_short_reason(
        effective_route_layer: tuple[int, int] | None,
        effective_block_geometry: dict[str, dict[str, Any] | None] | None,
    ) -> str | None:
        # Self-net pad-crossing check (#433): the whole-block bbox check below
        # skips a self-net's own block entirely (a same-block net's backbone
        # is, by construction, always inside its own block's bbox -- that
        # check would otherwise reject every self-net). But skipping it also
        # means nothing else was checking whether the backbone runs straight
        # over one of that block's *other* pads -- exactly what happens
        # bussing an array's unit devices (e.g. chaining a bjt_array's
        # emitters): a same-layer pad in the backbone's path shorts to it,
        # silently, since a self-net was never compared against the block's
        # own ports[] at all. Approximate each other port on this block as a
        # square pad footprint (side length its own reported ``width_um``,
        # centered on its position), *inflated* by the route's own trace
        # half-width on every side -- ``_segment_bbox_interior_overlap_um``
        # treats a backbone segment as a zero-width centerline, but the wire
        # actually drawn is ``width_um`` wide, so a centerline that merely
        # passes within ``width_um / 2`` of a pad's edge still draws metal on
        # top of it. This Minkowski-sum inflation is what makes the check
        # actually catch pads much narrower than the route (e.g. a bjt_array
        # base contact's reported ``width_um`` alone is too small to reach a
        # jog stubbed out by the route's own width -- only their sum is).
        # Reject the route if the backbone overlaps this inflated footprint's
        # interior -- mirroring the bbox-interior accounting above, just
        # against a pad instead of a whole block.
        #
        # Same-direction degenerate-jog check (#453): the inflated-footprint
        # test above models each other port as a square of side its
        # *reported* ``width_um``. For an array unit's own pad that badly
        # *under*-estimates the real drawn metal in the port's facing
        # direction -- e.g. a bjt_array base-tie tap draws li1 metal several
        # times taller than its reported ``width_um`` (the reported width is
        # roughly the contact size, not the pad extent). When both pins face
        # the *same* direction and share the coordinate along that facing
        # axis (same row for a vertical facing, same column for a horizontal
        # one), ``manhattan_backbone()`` collapses to a single straight jog
        # lifted just one stub width (``width_um``) to the ports' outward
        # side -- so a route wide enough that the jog clears the under-sized
        # reported square still plows straight through the real pad of any
        # intervening same-facing port. That is exactly the reproduction in
        # #453 (bussing two same-row north-facing bjt_array emitters across
        # the intervening unit's north-facing base-tie pad composed
        # ``routed: true`` and DRC-clean while extraction showed the whole
        # array's shared base node absorbed into the emitter net). Treat any
        # other same-layer port that faces the same direction and sits
        # strictly between the two pins along the perpendicular axis (on the
        # same row/column) as crossed: its pad opens toward the jog, so
        # bussing across it draws a silent short regardless of how small its
        # reported ``width_um`` is.
        if same_block_self_net:
            own_ports = blocks[own_a].get("ports") or {}
            own_offset = offsets_um[own_a]
            route_half_um = width_um / 2.0
            skip_port_names = {pin_a["port"], pin_b["port"]}
            facing_vertical = va[1] != 0
            # A degenerate single-jog backbone only forms when both pins face
            # the same direction *and* share their facing-axis coordinate
            # (same row for a vertical facing, same column for a horizontal
            # one).
            same_line = (
                abs(a[1] - b[1]) < 1e-9 if facing_vertical else abs(a[0] - b[0]) < 1e-9
            )
            # Like the channel-width heuristic and the #461 stub lift above,
            # this conservative fallback is a statement about the
            # *degenerate single-jog shape* -- it rejects on port positions
            # alone, without consulting `points`, precisely because that
            # fixed shape is known to run straight along the ports' own
            # row/column. Supplying waypoints_um (#634) replaces that shape
            # with the caller's own path, so the premise no longer holds and
            # the fallback would reject route-arounds that never go near the
            # intervening pad. The waypoint-drawn path is still checked
            # against the same pads by the inflated-footprint test below and
            # by the drawn-metal check (#453/#469) -- both of which measure
            # the actual `points`, so a caller whose waypoints really do
            # cross a pad is still rejected.
            conservative_same_dir = (
                waypoints_um is None and dir_a == dir_b and same_line
            )
            for other_name, other_port in own_ports.items():
                if other_name in skip_port_names or not _port_has_geometry(other_port):
                    continue
                other_layer = (
                    other_port["layer"]["layer"],
                    other_port["layer"]["datatype"],
                )
                if (
                    effective_route_layer is not None
                    and other_layer != effective_route_layer
                ):
                    continue  # a pad on a different physical layer can't short
                px = float(other_port["x_um"]) + own_offset["x"]
                py = float(other_port["y_um"]) + own_offset["y"]

                # #453: an intervening same-facing pad on the jog's own
                # row/column is crossed no matter how small its reported
                # footprint is.
                other_dir = int(other_port.get("direction_deg", 0)) % 360
                if conservative_same_dir and other_dir == dir_a:
                    if facing_vertical:
                        between = (
                            min(a[0], b[0]) < px < max(a[0], b[0])
                            and abs(py - a[1]) < 1e-9
                        )
                    else:
                        between = (
                            min(a[1], b[1]) < py < max(a[1], b[1])
                            and abs(px - a[0]) < 1e-9
                        )
                    if between:
                        axis = "row" if facing_vertical else "column"
                        return (
                            f"self-net between two same-facing ports on the "
                            f"same {axis} jogs directly over block "
                            f"'{own_a}''s own port '{other_name}' (same "
                            "facing direction, same drawing layer) -- "
                            "bussing this net across the block would draw a "
                            "silent short to that pad's real drawn metal, "
                            "which extends past its reported width_um "
                            "footprint in its facing direction; route to a "
                            "layer_role with a metal2/via stack instead "
                            "(or configure routing.cross_block_layer_role, "
                            "issue #1168), or wire this net externally"
                        )

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
                    return (
                        f"self-net backbone crosses {crossed_um:.4g}um "
                        f"through block '{own_a}''s own port '{other_name}' "
                        "on the same drawing layer -- bussing this net "
                        "across the block would draw a silent short to that "
                        "pad; route to a layer_role with a metal2/via stack "
                        "instead (or configure "
                        "routing.cross_block_layer_role, issue #1168), or "
                        "wire this net externally"
                    )

        # Self-net drawn-metal check (#453/#469): check 3 above models each
        # other port as a square built from its *reported* ``width_um``,
        # which is a port's contact/access size -- not the extent of the pad
        # metal drawn around it. A bjt_array base tie, for instance, reports
        # ``width_um: 0.22`` (``CONTACT_SIZE_UM``) for a pad whose drawn
        # local metal is 0.42um x 0.68um. So the modelled square (and even
        # its same-direction same-row/column fallback, which only fires for a
        # degenerate single-jog backbone) systematically under-states the
        # real obstacle and misses any short outside that narrow shape --
        # notably a same-facing pair on *different* rows/columns, or a route
        # wide enough to reach an adjacent row's pad. Compare the route's
        # *drawn* metal against the block's own *drawn* shapes on the route
        # layer instead: every merged shape except the two the endpoints land
        # on is an obstacle, and overlapping one (positive area -- an edge
        # touch is a spacing question for `klt drc`, not a short) is the same
        # silent short, just measured against geometry the reported port
        # model cannot see. This is independent of, and composes with, check
        # 3: either can catch a case the other misses.
        if same_block_self_net and effective_block_geometry is not None:
            geometry = effective_block_geometry.get(own_a)
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
                    return (
                        f"self-net's drawn {width_um}um metal overlaps "
                        f"{overlap_um2:.4g}um^2 of block '{own_a}''s own "
                        f"drawn pad metal on the route layer ({where}) -- "
                        "bussing this net across the block would draw a "
                        "silent short to that pad (its drawn metal is "
                        "larger than the contact size its port reports); "
                        "route to a layer_role with a metal2/via stack "
                        "instead (or configure "
                        "routing.cross_block_layer_role, issue #1168), or "
                        "wire this net externally"
                    )
        return None

    # Drive the retry (#1168): try the primary route_layer first -- the
    # overwhelmingly common case (no configured cross-block layer, or a leg
    # that never crosses another same-layer pad) resolves here with no
    # behaviour change from before this issue. Only when the primary layer's
    # checks object AND a cross_block_route_layer is configured is the
    # *identical* pair of checks retried against that second layer; if the
    # crossed pad sits on the primary layer (the overwhelmingly common case),
    # the `effective_route_layer != other_layer` guard inside the closure
    # above naturally exempts it on the retry, since a pad on `route_layer`
    # cannot short a backbone now drawn on `cross_block_route_layer`. Any
    # *other* obstacle that also happens to sit on the cross layer is still
    # caught by the same checks, run again in full -- this is not a bypass,
    # it is the identical short-detection logic evaluated against a different
    # drawing layer. Everything downstream (obstacle-overlap check 5,
    # via-drop check 6, stub-widen) uses whichever layer wins here.
    effective_route_layer = route_layer
    short_reason = _same_layer_short_reason(route_layer, block_geometry)
    if short_reason is not None and cross_block_route_layer is not None:
        cross_short_reason = _same_layer_short_reason(
            cross_block_route_layer, cross_block_geometry
        )
        if cross_short_reason is None:
            effective_route_layer = cross_block_route_layer
            short_reason = None
    if short_reason is not None:
        return {
            "routed": False,
            "route_length_um": None,
            "points_um": None,
            "reason": short_reason,
        }

    overlap_by_block_um: dict[str, float] = {}
    for seg_p0, seg_p1 in zip(points, points[1:], strict=False):
        for other_id, other_bbox in obstacle_bboxes_um.items():
            if same_block_self_net and other_id == own_a:
                continue  # a self-net is expected to cross its own block
            length = _segment_bbox_interior_overlap_um(seg_p0, seg_p1, other_bbox)
            if length > 0.0:
                overlap_by_block_um[other_id] = (
                    overlap_by_block_um.get(other_id, 0.0) + length
                )

    blocking_um = {
        other_id: crossed_um
        for other_id, crossed_um in overlap_by_block_um.items()
        if crossed_um > allowances_um.get(other_id, 0.0) + margin_eps_um
    }

    # Bounded detour search (#1167): a backbone rejected *only* for crossing
    # blocks neither of its two pins sits on is not a routing failure -- it is
    # the fixed one-jog shape being the wrong shape. Before reporting the net
    # unroutable, retry it around those obstacles on up to _MAX_DETOUR_LANES
    # alternate lanes (_detour_lane_waypoints_um), shortest first, and keep the
    # first that passes. Each retry goes back through *this same function* with
    # the lane as waypoints_um, so every routability check above -- rings, pad
    # crossings, drawn-metal shorts, this very obstacle check, via drops --
    # applies to the detour exactly as it does to any other path; nothing is
    # waived to make a detour fit. Recursion is bounded at one level by
    # construction: the retry supplies waypoints_um, and a waypoint-supplied
    # path is never itself detoured (a caller who supplies a path owns it --
    # #634 -- and the detour retry is such a caller).
    #
    # Scoped to *unrelated* blocks on purpose. A backbone that plows through
    # one of its own two pins' blocks (the same-facing port pair) is a
    # statement about which port faces which way, not about something sitting
    # in the channel; the remedy there stays the caller-supplied waypoints_um
    # of #634, whose routability the caller -- not this heuristic -- vouches
    # for.
    detour_note = ""
    if blocking_um and waypoints_um is None and not ({own_a, own_b} & set(blocking_um)):
        lanes = _detour_lane_waypoints_um(
            (a, va, placed_bboxes_um[own_a]),
            (b, vb, placed_bboxes_um[own_b]),
            [placed_bboxes_um[other_id] for other_id in blocking_um],
            placed_bboxes_um,
            width_um,
        )
        for lane in lanes:
            retry = route_two_pin(
                pin_a,
                pin_b,
                blocks,
                offsets_um,
                placed_bboxes_um,
                width_um,
                route_layer,
                extraction_deck,
                block_geometry,
                waypoints_um=lane,
                cross_block_route_layer=cross_block_route_layer,
                cross_block_geometry=cross_block_geometry,
            )
            if retry["routed"]:
                return retry
        if lanes:
            detour_note = (
                f" -- a bounded detour around it ({len(lanes)} alternate "
                f"lane{'' if len(lanes) == 1 else 's'} placed clear of every "
                "block between the two pins) was tried first, and each one "
                "still crossed a placed block"
            )

    for other_id, crossed_um in blocking_um.items():
        allowed_um = allowances_um.get(other_id, 0.0)
        if other_id in (own_a, own_b):
            reason = (
                f"backbone's {width_um}um-wide drawn path crosses "
                f"{crossed_um:.4g}um through its own pin's block '{other_id}' "
                f"-- more than that pin's own {allowed_um:.4g}um edge margin "
                f"(including {obstacle_half_um:.4g}um for the route's own "
                "half-width), so the route plows through, or clips the edge "
                "of, the block's interior (e.g. a same-facing port pair "
                "reaching a pin on the block's far side, crossing another "
                "pin on the way, or a connecting jog running close enough "
                "alongside the block's own edge that the drawn wire's width "
                "still overlaps it) rather than approaching the pin cleanly"
            )
        else:
            reason = (
                f"backbone's {width_um}um-wide drawn path crosses "
                f"{crossed_um:.4g}um through unrelated block '{other_id}''s "
                "bbox (including its own edge, within half the route's "
                "width) -- the route is not point-to-point between only the "
                "two connected blocks"
            )
        return {
            "routed": False,
            "route_length_um": None,
            "points_um": None,
            "reason": reason + detour_note,
        }

    # Via-drop resolution (#454, check 5 -- see docstring): only consulted
    # when both a route_layer and an extraction_deck are given (pre-#454
    # callers that pass neither draw exactly as before, no via-drop). For
    # each endpoint whose own reported layer differs from route_layer, either
    # resolve the connecting via (drop needed and available), find nothing to
    # do (not a metals-stack level -- an unrelated role, unchanged legacy
    # behavior), or reject the whole net as unroutable (a drop is needed but
    # not resolvable). Uses ``effective_route_layer`` (#1168): when the
    # same-layer-short retry above switched this leg to
    # ``cross_block_route_layer``, every endpoint whose own pad sits on the
    # *primary* ``route_layer`` now needs exactly the drop this loop already
    # knows how to resolve -- no separate cross-block via-drop mechanism.
    via_drops: list[dict[str, Any]] = []
    if effective_route_layer is not None and extraction_deck is not None:
        for pin, port, pos in ((pin_a, port_a, a), (pin_b, port_b, b)):
            port_layer = _port_own_layer(port)
            if port_layer is None:
                continue  # no reported layer -- draw directly, legacy behavior
            via_layer, drop_error = _resolve_via_drop_layer(
                extraction_deck, effective_route_layer, port_layer
            )
            if drop_error is not None:
                return {
                    "routed": False,
                    "route_length_um": None,
                    "points_um": None,
                    "reason": (
                        f"pin '{pin['port']}' on block '{pin['block']}' is drawn "
                        f"on layer {port_layer}, which routing.layer_role's "
                        f"{effective_route_layer} cannot reach: {drop_error}"
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

    # Stub-widen (#496): see _endpoint_stub_widen_um's own docstring. Computed
    # independently of the via-drop loop above (it needs no ExtractionDeck --
    # only route_layer, to compare against each port's own reported layer),
    # for both endpoints.
    stub_widen: list[dict[str, Any]] = []
    for port, pos, direction, stub_len_um in (
        (port_a, a, dir_a, stub_a_um),
        (port_b, b, dir_b, stub_b_um),
    ):
        widen = _endpoint_stub_widen_um(
            port, pos, direction, stub_len_um, width_um, effective_route_layer
        )
        if widen is not None:
            stub_widen.append(widen)

    return {
        "routed": True,
        "route_length_um": _polyline_length_um(points),
        "points_um": points,
        "via_drops": via_drops,
        "stub_widen": stub_widen,
        "route_layer": effective_route_layer,
        "reason": None,
    }


def _pin_ref(pin: dict[str, Any]) -> str:
    """``"block.port"`` -- the compact form router diagnostics name a pin by."""
    return f"{pin['block']}.{pin['port']}"


def _pin_position_um(
    pin: dict[str, Any],
    blocks: dict[str, dict[str, Any]],
    offsets_um: dict[str, dict[str, float]],
) -> tuple[float, float] | None:
    """A pin's port position in the *composed* frame, or ``None``.

    ``None`` when the block reported no ``ports[]`` entry for it, or the entry
    carries no usable ``x_um``/``y_um`` -- exactly the case
    :func:`route_two_pin` already reports as "one or both ports report no
    position (empty ports[])". Used only to *order* candidate legs by
    Manhattan distance (:func:`route_bundle`); the routability verdict itself
    always comes from :func:`route_two_pin`.
    """
    block = blocks.get(pin["block"])
    if block is None:
        return None
    port = (block.get("ports") or {}).get(pin["port"])
    if not isinstance(port, dict):
        return None
    x_um, y_um = port.get("x_um"), port.get("y_um")
    if isinstance(x_um, bool) or not isinstance(x_um, (int, float)):
        return None
    if isinstance(y_um, bool) or not isinstance(y_um, (int, float)):
        return None
    offset = offsets_um[pin["block"]]
    return (float(x_um) + offset["x"], float(y_um) + offset["y"])


def route_bundle(
    pins: list[dict[str, Any]],
    blocks: dict[str, dict[str, Any]],
    offsets_um: dict[str, dict[str, float]],
    placed_bboxes_um: dict[str, dict[str, float]],
    width_um: float,
    route_layer: tuple[int, int] | None = None,
    extraction_deck: ExtractionDeck | None = None,
    block_geometry_for: Any = None,
    leg_conflict: Any = None,
    waypoints_um: list[tuple[float, float]] | None = None,
    cross_block_route_layer: tuple[int, int] | None = None,
    cross_block_geometry_for: Any = None,
) -> dict[str, Any]:
    """Route one ``connectivity[]`` net of *any* pin count (issue #1073).

    This is the N-pin generalisation the spike's section 5 item 2 named as
    "the natural next increment once two-pin routing is proven against a real
    block" -- a shared supply/ground rail or a fanout node (a bias line, a
    clock, an inverter chain's tap) touches one port on every block it spans,
    so a two-pin-only router cannot wire the *majority* of a real circuit's
    connectivity at all.

    **Algorithm: a routed minimum spanning tree over the net's pins.** Every
    unordered pin pair is a candidate *leg*; candidates are tried in
    increasing Manhattan distance (ties broken by pin index, so the result is
    deterministic and the composed GDS stays byte-reproducible, #320), and a
    leg is accepted only when it joins two so-far-disconnected parts of the
    net (Kruskal). The net is routed when every pin ends up in one component.
    For the canonical rail case -- one north-facing supply port per block
    across a placement row -- the nearest-first ordering yields exactly the
    trunk-and-stub shape the issue asks for: a chain of adjacent-block legs,
    each one an ordinary Manhattan backbone.

    **Every leg is routed by :func:`route_two_pin`**, unchanged. That is the
    point of decomposing into legs rather than inventing a second geometry
    routine: all six of its routability checks (channel width, guard/collector
    ring, self-net pad crossing, self-net drawn-metal short, obstacle overlap,
    via-drop resolution) apply per leg, for free, with no second
    implementation to keep in sync. A leg a check rejects is simply not
    accepted, and the *next* candidate that would join the same two components
    is tried -- so an N-pin net routes around an individually unroutable pair
    whenever another spanning tree exists, rather than failing outright.

    **Partial routing when no spanning tree exists (issue #1169).** A net
    whose pins cannot all be joined into one component still gets every leg
    the spanning-tree search *did* accept drawn -- only the pins left
    stranded (and any candidate leg the router tried and rejected on the way
    to reaching them) are left undrawn. The caller gets the net back in
    ``unrouted_nets[]`` (it is still not *fully* connected) but is not left
    building routable geometry around a fully-blank net -- discarding metal
    that already passed every one of :func:`route_two_pin`'s per-leg checks
    (plus the route-vs-route collision check, #1057) made a real composition
    failure harder to debug than necessary, for no benefit: an individually
    routable leg's metal never introduces a short or a DRC violation just
    because a *different* leg of the same net failed. (Before this, the net
    was all-or-nothing: any spanning-tree failure reset every leg's
    ``routed`` back to ``false``, discarding metal for legs that were
    individually routable -- see issue #1073's original rationale, now
    superseded.) Every returned leg still reports ``routed: true`` **iff its
    metal is drawn** -- so no leg is ever reported as routed when nothing was
    drawn for it -- and the top-level ``status`` field (below) tells the
    caller whether the net ended up fully routed, partially routed, or not
    routed at all, so partial routing is never mistaken for full routing (or
    vice versa) by counting ``legs[]`` alone.

    ``block_geometry_for`` is an optional callable taking a block ``id`` and
    returning the ``{block_id: geometry}`` mapping :func:`route_two_pin`
    takes as ``block_geometry`` (``compose()`` passes its lazily-populated
    per-block cache); it is consulted only for a leg whose two pins sit on the
    same block, exactly as ``compose()`` did for a two-pin self-net before
    this function existed. ``leg_conflict`` is an optional callable taking a
    candidate leg's drawn ``points_um``, ``via_drops``, ``stub_widen``, and
    (issue #1386) its effective ``route_layer`` (the same four fields
    :func:`route_two_pin` returns for it -- issue #1197 widened this from
    ``points_um`` alone, since a leg's via-drop landing pad or stub-widen box
    can extend past its bare backbone far enough to collide with another net
    even when the two backbones never touch; #1386 added the layer so the
    callback can tell two legs on genuinely different physical layers apart
    and, when it has a same-layer minimum-spacing rule to consult, reject a
    leg that comes too *close* to an already-accepted one, not only one that
    literally overlaps it) and returning a rejection reason (or ``None`` to
    accept) -- ``compose()`` passes its route-vs-route collision check
    (#1057) here, so a leg colliding with an *already-routed other net* is
    rejected as a candidate and an alternative leg is tried, rather than
    failing the whole net.

    ``cross_block_route_layer``/``cross_block_geometry_for`` (issue #1168)
    mirror ``route_layer``/``block_geometry_for`` for an optional second,
    higher metal :func:`route_two_pin` retries a same-block self-net leg on
    when it would otherwise short across another same-layer pad on the
    primary ``route_layer`` -- threaded straight through to every candidate
    leg's own :func:`route_two_pin` call; see that function's docstring for
    the retry mechanics. Both default to ``None`` (no cross-block layer
    configured), which reproduces this function's pre-#1168 behaviour
    exactly.

    ``waypoints_um`` (#634) applies to the single backbone of a **2-pin** net;
    supplying it for a >2-pin net raises :class:`GenComposeError` (there is no
    unambiguous leg for a caller-supplied path to belong to -- see
    :func:`_parse_connectivity`, which rejects that combination at request-parse
    time).

    Returns ``{"routed": bool, "route_length_um": float | None, "legs": [...],
    "reason": str | None, "status": str}``, where ``routed`` is ``True`` only
    when *every* pin joined one component (unchanged from before #1169),
    ``route_length_um`` is the summed length of every *drawn* leg (``None``
    only when zero legs were drawn), ``status`` is one of ``"routed"`` (every
    pin connected), ``"partial"`` (at least one leg drawn, but the net is not
    fully connected), or ``"unrouted"`` (no leg was ever accepted) -- and each
    ``legs[]`` entry is ``{"pins": [pin_a, pin_b], "routed": bool,
    "route_length_um": float | None, "reason": str | None, "points_um": list
    | None, "via_drops": list, "stub_widen": list, "route_layer":
    tuple[int, int] | None}`` -- ``points_um``/``via_drops``/``stub_widen``/
    ``route_layer`` being the same per-leg drawing payload
    :func:`route_two_pin` returns, consumed by ``compose()`` (``route_layer``
    is the *effective* layer that leg actually drew on, #1168 -- and it is
    per *leg*, not per net, so a partially-routed net's drawn legs may sit on
    different layers).
    """
    if len(pins) < 2:
        raise GenComposeError("route_bundle() needs at least 2 pins")
    if waypoints_um is not None and len(pins) != 2:
        raise GenComposeError(
            "waypoints_um applies to a 2-pin net's single backbone -- it cannot "
            f"be attributed to any one leg of a {len(pins)}-pin net"
        )

    pin_count = len(pins)
    parent = list(range(pin_count))

    def _find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def _union(left: int, right: int) -> bool:
        root_left, root_right = _find(left), _find(right)
        if root_left == root_right:
            return False
        parent[max(root_left, root_right)] = min(root_left, root_right)
        return True

    components = pin_count

    # A pin listed twice (the same block+port) is the same physical point, so
    # it needs no leg -- pre-union it rather than routing a zero-length route
    # between a port and itself.
    first_seen: dict[tuple[str, str], int] = {}
    for index, pin in enumerate(pins):
        key = (pin["block"], pin["port"])
        if key in first_seen:
            if _union(first_seen[key], index):
                components -= 1
        else:
            first_seen[key] = index

    positions = [_pin_position_um(pin, blocks, offsets_um) for pin in pins]

    def _candidate_distance_um(pair: tuple[int, int]) -> float:
        pos_i, pos_j = positions[pair[0]], positions[pair[1]]
        if pos_i is None or pos_j is None:
            return math.inf
        return abs(pos_i[0] - pos_j[0]) + abs(pos_i[1] - pos_j[1])

    candidates = sorted(
        ((i, j) for i in range(pin_count) for j in range(i + 1, pin_count)),
        key=lambda pair: (_candidate_distance_um(pair), pair[0], pair[1]),
    )

    legs: list[dict[str, Any]] = []
    total_length_um = 0.0
    for i, j in candidates:
        if components == 1:
            break
        if _find(i) == _find(j):
            continue  # already connected through other legs -- no leg needed

        geometry = None
        cross_geometry = None
        if pins[i]["block"] == pins[j]["block"]:
            if block_geometry_for is not None:
                geometry = block_geometry_for(pins[i]["block"])
            if cross_block_geometry_for is not None:
                cross_geometry = cross_block_geometry_for(pins[i]["block"])
        result = route_two_pin(
            pins[i],
            pins[j],
            blocks,
            offsets_um,
            placed_bboxes_um,
            width_um,
            route_layer,
            extraction_deck,
            geometry,
            waypoints_um=waypoints_um,
            cross_block_route_layer=cross_block_route_layer,
            cross_block_geometry=cross_geometry,
        )
        reason = None if result["routed"] else result["reason"]
        if result["routed"] and leg_conflict is not None:
            reason = leg_conflict(
                result["points_um"],
                result.get("via_drops", []),
                result.get("stub_widen", []),
                result.get("route_layer"),
            )

        if reason is None and result["routed"]:
            _union(i, j)
            components -= 1
            total_length_um += result["route_length_um"]
            legs.append(
                {
                    "pins": [pins[i], pins[j]],
                    "pin_indices": (i, j),
                    "routed": True,
                    "route_length_um": result["route_length_um"],
                    "reason": None,
                    "points_um": result["points_um"],
                    "via_drops": result.get("via_drops", []),
                    "stub_widen": result.get("stub_widen", []),
                    "route_layer": result.get("route_layer"),
                }
            )
        else:
            legs.append(
                {
                    "pins": [pins[i], pins[j]],
                    "pin_indices": (i, j),
                    "routed": False,
                    "route_length_um": None,
                    "reason": reason,
                    "points_um": None,
                    "via_drops": [],
                    "stub_widen": [],
                }
            )

    if components == 1:
        return {
            "routed": True,
            "route_length_um": total_length_um,
            # Only the accepted legs: a candidate the router rejected on the
            # way to a working spanning tree is not part of the result.
            "legs": [leg for leg in legs if leg["routed"]],
            "reason": None,
            "status": "routed",
        }

    # --- Unroutable: report which pins were stranded and why ----------------
    groups: dict[int, list[int]] = {}
    for index in range(pin_count):
        groups.setdefault(_find(index), []).append(index)
    # The largest component is "the net"; every pin outside it is stranded.
    main_root = max(groups, key=lambda root: (len(groups[root]), -root))
    stranded = sorted(
        index
        for root, members in groups.items()
        if root != main_root
        for index in members
    )
    stranded_set = set(stranded)

    blocker = next(
        (
            leg
            for leg in legs
            if not leg["routed"] and set(leg["pin_indices"]) & stranded_set
        ),
        None,
    )
    blocker_reason = blocker["reason"] if blocker is not None else None

    if pin_count == 2:
        # A two-pin net has exactly one candidate leg, so the net's own reason
        # *is* that leg's reason -- byte-identical to the message compose()
        # reported before this function generalised the two-pin path.
        reason = blocker_reason or "no routable path between the net's two pins"
    else:
        refs = ", ".join(f"'{_pin_ref(pins[index])}'" for index in stranded)
        reason = (
            f"{len(stranded)} of {pin_count} pins could not be connected into "
            f"one net ({refs}) -- every candidate leg reaching them was "
            "rejected (per-leg detail in nets[].legs[])"
        )
        if blocker is not None:
            reason += (
                f"; nearest rejection ('{_pin_ref(blocker['pins'][0])}' -> "
                f"'{_pin_ref(blocker['pins'][1])}'): {blocker_reason}"
            )

    # Partial routing (issue #1169): a net that could not be fully connected
    # keeps every leg the spanning-tree search *did* accept -- their geometry
    # is real, DRC-checked (route_two_pin's checks 1-6, plus the route-vs-route
    # collision check #1057 via `leg_conflict`) metal, drawn exactly as it
    # would be for a fully-routed net, so `legs[].routed` still means "this
    # leg's metal is in the composed output" at the leg level. Only the pins
    # that ended up stranded -- and any leg the router tried and rejected on
    # the way to reaching them -- are left undrawn. `status` distinguishes the
    # two ways a net can be incomplete: "partial" (some legs drawn) vs.
    # "unrouted" (no leg was ever accepted) -- the caller must not have to
    # infer this by counting `legs[].routed`, since a zero-leg net and a
    # not-fully-spanned net both report `routed: false` at the net level.
    drawn_leg_count = sum(1 for leg in legs if leg["routed"])
    status = "partial" if drawn_leg_count > 0 else "unrouted"

    return {
        "routed": False,
        "route_length_um": total_length_um if drawn_leg_count > 0 else None,
        "legs": legs,
        "reason": reason,
        "status": status,
    }


#: Reason string every declare-only net/leg (#1188) reports -- distinct from
#: any geometry-based rejection reason :func:`route_bundle`/:func:`route_two_pin`
#: produce, so a caller can tell "not requested" apart from "tried and failed"
#: by string alone as well as by ``routing`` being empty in the echoed request.
_DECLARE_ONLY_REASON = "routing not requested"


def _declare_only_bundle_result(pins: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a :func:`route_bundle`-shaped result for a declare-only net (#1188).

    Used by :func:`compose` in place of :func:`route_bundle` when
    ``request.routing`` is absent/``{}``: the net's ``{block, port}`` pins
    were already validated by :func:`_parse_connectivity`, but no routing was
    requested, so nothing is drawn. Reports the same spanning-tree shape a
    routed net would (one leg per pair needed to span every pin, matching a
    repeated ``{block, port}`` pin needing no leg of its own -- see
    :func:`route_bundle`'s docstring), except every leg is ``routed: False``
    with :data:`_DECLARE_ONLY_REASON`, so ``nets[]``/``unrouted_nets[]``
    report a declare-only net exactly like an unroutable one, distinguished
    only by the reason string. Candidate pairs are picked in pin-index order
    (not the nearest-first geometric order :func:`route_bundle` uses) --
    positions play no role here since nothing is drawn, so there is no
    geometry to order by; this still yields the same deterministic,
    byte-reproducible leg list run to run.
    """
    pin_count = len(pins)
    parent = list(range(pin_count))

    def _find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def _union(left: int, right: int) -> bool:
        root_left, root_right = _find(left), _find(right)
        if root_left == root_right:
            return False
        parent[max(root_left, root_right)] = min(root_left, root_right)
        return True

    components = pin_count
    first_seen: dict[tuple[str, str], int] = {}
    for index, pin in enumerate(pins):
        key = (pin["block"], pin["port"])
        if key in first_seen:
            if _union(first_seen[key], index):
                components -= 1
        else:
            first_seen[key] = index

    legs: list[dict[str, Any]] = []
    for i in range(pin_count):
        if components == 1:
            break
        for j in range(i + 1, pin_count):
            if components == 1:
                break
            if _find(i) == _find(j):
                continue
            _union(i, j)
            components -= 1
            legs.append(
                {
                    "pins": [pins[i], pins[j]],
                    "routed": False,
                    "route_length_um": None,
                    "reason": _DECLARE_ONLY_REASON,
                    "points_um": None,
                    "via_drops": [],
                    "stub_widen": [],
                }
            )

    return {
        "routed": False,
        "route_length_um": None,
        "legs": legs,
        "reason": _DECLARE_ONLY_REASON,
        "status": "unrouted",
    }


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

    # Drawn-geometry obstacles for the self-net drawn-metal check (#453/#469),
    # read lazily and cached per block: only a *self*-net needs them (every
    # other net is already covered by the whole-block bbox check), and only
    # once per block however many self-nets land on it. A second such cache,
    # keyed the same way, covers cross_route_layer (#1168) for the same
    # check retried on the cross-block layer.
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

    # dbu for the route-vs-route collision check below (#1057), read lazily
    # (only once connectivity[] actually has a net to check) from any one
    # block's own GDS -- _write_composed_gds is the authoritative place that
    # reads every block's full geometry and requires (and validates) that
    # they all share one dbu, so any single block's value is safe to reuse
    # here ahead of that.
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
    # private threshold table. Feeds the spacing-aware half of
    # `_leg_conflict` below: #1057's original route-vs-route check only ever
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
            silently merged them onto one node.

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
            region = _drawn_leg_footprint_region(
                points_um, width_um, via_drops, stub_widen, _route_dbu()
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
            accepted_route_regions.append(
                (
                    net_label,
                    net_pin_set,
                    leg_route_layer,
                    _drawn_leg_footprint_region(
                        leg["points_um"],
                        width_um,
                        leg["via_drops"],
                        leg["stub_widen"],
                        _route_dbu(),
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
                    "width_um": width_um,
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
        array_placement=array_placement_gds,
    )

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
        if dbu is None:  # no blocks read (can't happen -- blocks[] is non-empty)
            dbu = layout.dbu

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

            # Via-drops (#454): each entry drops the backbone (this entry's
            # own effective layer) down to a target pin's own layer at
            # exactly that pin's own position -- a via square on `via_layer`,
            # plus a landing-pad square on *both* the backbone's own layer
            # and the pin's own layer (_VIA_LANDING_SIZE_UM, independent of
            # the route's own trace width) so the via's enclosure requirement
            # holds regardless of how thin routing.width_um is. The
            # backbone's own Path already terminates exactly at this same
            # point (manhattan_backbone's endpoints are the raw pin
            # positions), so the landing pad always overlaps -- and merges
            # with -- the trace.
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
                top.shapes(layer_index).insert(landing_box)  # backbone's own side
                top.shapes(port_layer_index).insert(landing_box)  # pin's own side

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
