"""Compose already-generated ``klt gen`` blocks into one placed circuit.

Pure library: :func:`compose` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints, mirroring ``gen.py``.
Serialisation and human-readable formatting live in the CLI command module
(``cli/gen_compose_cmd.py``).

This is phase 1 of Epic #191 (``klt gen compose``), the build carried by the
accepted spike, ``docs/design/gen-composition-spike.md`` -- read that
document first; its section 2 ("Proposed composition contract") settles the
request/response JSON shape this module implements, and section 5's "Scope
proposal for a first implementing epic" settles this phase's scope:
``placement.strategy: "row"`` only, no routing.

Scope (phase 1, this module's current state): resolve each ``blocks[]``
entry's own ``generator_report`` (a ``klt gen`` response, given as a file
path or inline object -- see :func:`load_generator_report_arg`), compute a
single horizontal row placement from each block's own reported ``bbox_um``
plus ``placement.spacing_um`` (see :func:`compute_row_offsets`), and write a
composed GDS with each block's own top cell instantiated as a translated
sub-cell instance under one new top cell -- no routing metal, no
``connectivity[]``/``routing`` execution. ``connectivity[]`` entries are
still validated (a reference to a nonexistent block ``id``/port name is an
application error, exit code 1, per the acceptance criteria) even though
nothing is routed yet; ``nets[]``/``unrouted_nets[]``/``drc_hints`` are
reserved, empty/null-equivalent placeholders this phase so phase 2 (routing)
doesn't have to change the top-level envelope.

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

from .pdk import PdkNotFoundError, find_pdk

#: Contract identifier for the request envelope (spike section 2).
REQUEST_SCHEMA = "klt.gen_compose.request/1"

#: Bumped only on a non-additive (breaking) change to this command's own
#: response JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: Placement strategies implemented at this phase. ``"grid"`` is spike-scoped
#: for a later phase (see ``docs/design/gen-composition-spike.md`` section 5).
SUPPORTED_PLACEMENT_STRATEGIES = {"row"}


class GenComposeError(Exception):
    """Raised when a composition request cannot be fulfilled.

    Covers an unresolvable PDK, a malformed request shape, a ``blocks[]``/
    ``connectivity[]`` reference to a nonexistent ``id``/port, an
    unsupported ``placement.strategy``, and a GDS read/write failure -- the
    CLI turns this into a clean stderr message + exit code 1, never a
    traceback (see ``docs/cli/gen-compose.md``'s exit code table).
    """


def load_generator_report_arg(value: Any) -> dict[str, Any]:
    """Resolve one ``blocks[].generator_report`` value into a report dict.

    ``value`` is either an inline JSON object (already a ``dict`` -- the
    request document embedded it directly) or a path to a JSON file holding
    one (a ``klt gen`` response captured to disk), mirroring
    ``klt gen --params``'s own path-or-inline duality
    (:func:`klayout_tools.gen.load_params_arg`).

    Raises :class:`GenComposeError` if ``value`` is neither a ``dict`` nor a
    readable JSON file, or the file doesn't decode to a JSON object.
    """
    if isinstance(value, dict):
        return value

    if not isinstance(value, str) or not value:
        raise GenComposeError(
            "blocks[].generator_report must be a JSON object or a path to one"
        )

    if not os.path.isfile(value):
        raise GenComposeError(f"generator_report file not found: '{value}'")

    try:
        with open(value, encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise GenComposeError(
            f"could not read generator_report '{value}': {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise GenComposeError(
            f"generator_report '{value}' is not valid JSON: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise GenComposeError(
            f"generator_report '{value}' must decode to a JSON object"
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


def _parse_blocks(raw_blocks: Any) -> dict[str, dict[str, Any]]:
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

        report = load_generator_report_arg(raw_block.get("generator_report"))
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
        port_names = {
            p["name"]
            for p in ports
            if isinstance(p, dict) and isinstance(p.get("name"), str)
        }

        blocks[block_id] = {
            "id": block_id,
            "generator": generator,
            "cell_name": cell_name,
            "gds_path": gds_path,
            "bbox_um": bbox_um,
            "port_names": port_names,
        }

    return blocks


def _parse_placement(
    raw_placement: Any, block_ids: set[str]
) -> tuple[str, list[str], float]:
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

    spacing_um = raw_placement.get("spacing_um", 0.0)
    if isinstance(spacing_um, bool) or not isinstance(spacing_um, (int, float)):
        raise GenComposeError("request.placement.spacing_um must be a number")
    spacing_um = float(spacing_um)
    if spacing_um < 0:
        raise GenComposeError("request.placement.spacing_um must be >= 0")

    return strategy, order, spacing_um


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
            block = blocks.get(block_id)
            if block is None:
                raise GenComposeError(
                    f"request.connectivity[{index}] (net '{net}') references "
                    f"unknown block id '{block_id}'"
                )
            if block["port_names"] and port not in block["port_names"]:
                raise GenComposeError(
                    f"request.connectivity[{index}] (net '{net}') references "
                    f"unknown port '{port}' on block '{block_id}' -- available: "
                    f"{', '.join(sorted(block['port_names']))}"
                )
            parsed_pins.append({"block": block_id, "port": port})

        connectivity.append({"net": net, "pins": parsed_pins})

    return connectivity


def compose(request: dict[str, Any]) -> dict[str, Any]:
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
    ``connectivity[]`` is validated (every referenced block ``id``/port must
    exist) but not yet routed -- ``routing`` is accepted and otherwise
    ignored this phase (phase 2 implements point-to-point routing; see the
    module docstring). Returns a dict matching the documented response
    schema (see ``docs/cli/gen-compose.md``).

    Raises :class:`GenComposeError` for an unresolvable PDK, a malformed
    request, an unsupported ``placement.strategy``, a ``connectivity[]``
    reference to a nonexistent block ``id``/port, or a GDS read/write
    failure.
    """
    if not isinstance(request, dict):
        raise GenComposeError("request must be a JSON object")

    pdk_request = request.get("pdk") or {}
    if not isinstance(pdk_request, dict):
        raise GenComposeError("request.pdk must be a JSON object")
    try:
        pdk_info = find_pdk(
            variant=pdk_request.get("variant"), root=pdk_request.get("root")
        )
    except PdkNotFoundError as exc:
        raise GenComposeError(str(exc)) from exc

    blocks = _parse_blocks(request.get("blocks"))
    _strategy, order, spacing_um = _parse_placement(
        request.get("placement"), set(blocks)
    )
    connectivity = _parse_connectivity(request.get("connectivity"), blocks)

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
    offsets_um = compute_row_offsets(order, bboxes_um, spacing_um)

    placed_bboxes_um = [
        _translate_bbox(bboxes_um[block_id], offsets_um[block_id]) for block_id in order
    ]
    composed_bbox_um = _union_bbox(placed_bboxes_um)

    _write_composed_gds(blocks, order, offsets_um, cell_name, output_path)

    warnings: list[str] = []
    if connectivity:
        warnings.append(
            "connectivity[] was validated but not routed -- routing is not "
            "implemented until phase 2 (see docs/design/gen-composition-spike.md)"
        )

    response_blocks = [
        {
            "id": block_id,
            "generator": blocks[block_id]["generator"],
            "offset_um": offsets_um[block_id],
            "bbox_um": _translate_bbox(bboxes_um[block_id], offsets_um[block_id]),
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
        "nets": [],
        "unrouted_nets": [],
        "drc_hints": {"min_spacing_um": None, "matched_groups": [], "notes": []},
        "warnings": warnings,
    }


def _write_composed_gds(
    blocks: dict[str, dict[str, Any]],
    order: list[str],
    offsets_um: dict[str, dict[str, float]],
    cell_name: str,
    output_path: str,
) -> None:
    """Write ``output_path``: one new top cell (``cell_name``) instantiating
    every block's own top cell as a translated sub-cell instance.

    Each block's GDS is read into its own scratch :class:`kdb.Layout`, its
    reported top cell (``generator_report.cell_name``) is duplicated
    (:meth:`kdb.Cell.copy_tree`) into a fresh sub-cell of the composed
    layout, and that sub-cell is instantiated into ``cell_name`` at the
    block's computed ``offset_um`` -- geometry is copied exactly once (never
    re-derived), and hierarchy is preserved (each block stays its own cell,
    not flattened into the composed top cell).
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

    try:
        layout.write(output_path)
    except Exception as exc:  # klayout raises RuntimeError for bad formats/paths
        raise GenComposeError(f"could not write output '{output_path}': {exc}") from exc
