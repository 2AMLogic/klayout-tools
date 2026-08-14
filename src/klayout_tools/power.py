"""Extract a routed layout's power grid as a resistive network and solve it
for its static (DC) IR drop (``klt power``, issues #844/#845, Phases 1a/1b of
the power/IR-drop + EM signoff epic #712).

Pure library: :func:`run_power` returns plain Python data (a JSON-
serialisable ``dict``) and never prints -- serialisation and human-readable
formatting live in ``cli/power_cmd.py``, matching every other ``klt`` verb
(see ``layers.py``'s docstring on the same convention).

Scope of this module (see ``docs/cli/power.md`` for the full picture):
``klt power`` is defined end to end as JSON in (a routed layout + a
power-net definition + a per-instance current model) / JSON out (an IR-drop
map + worst-case droop + a per-net EM verdict). Two phases have landed:

- **#844, Phase 1a** -- the interface and the resistive-network extraction:
  turning a routed layout's named power/ground nets into a graph of nodes +
  segment resistances (``networks[]`` below).
- **#845, Phase 1b** -- the static IR-drop solve: given ``pads`` (where the
  supply is delivered) and a ``current_model`` (what each instance draws),
  solve that network for its DC node voltages, and report an IR-drop map +
  the worst-case droop (``ir_drop_map``/``worst_case_droop_mv``). The linear
  algebra lives in :mod:`klayout_tools.ir_solver`, deliberately geometry-free
  so it can be validated against closed-form resistive networks with no
  layout involved; this module is the geometry-to-network binding.

The per-net EM verdict (#846, Phase 1c) consumes this module's output
additively -- in particular ``ir_drop_map``'s per-edge branch currents -- and
is not implemented here.

Connectivity: geometry is traced with ``klayout.db.LayoutToNetlist`` used
purely for wire/via connectivity (no device recognition registered) --
exactly the same API ``extract.py``'s ``_extract_netlist`` uses for its
metal/via connectivity graph, scoped down to only the caller-declared power-
grid layers. A net's *name* comes from text labels on a caller-declared
``label_layer`` per metal role (the routed layout's own pin/net-name text,
e.g. sky130's ``met1.pin``/``68/5`` datatype convention) -- there is no
naming without at least one labelled layer in the spec's ``stackup``.

A named power net commonly resolves to **several disconnected islands**, not
one connected mesh: ``klt par`` (issue #700) deliberately does not run PDN
(power-grid) generation (see ``place_and_route.py``'s own "Deliberately out
of scope for this v1" note), so a routed design's power/ground geometry is
whatever the standard-cell rows themselves contribute -- typically many
un-strapped per-row rail segments. This is not a bug to paper over: each
island is reported separately (an ``island_id`` per electrically distinct
cluster), because that is exactly the real electrical topology a later
IR-drop solve (#845) needs to reason about (an un-strapped island has no
path to a pad and cannot be solved for droop without saying so).

Resistor-network model (MVP, stated plainly):

- Each layer's net geometry, read via ``LayoutToNetlist.polygons_of_net``
  and merged into maximal polygons, becomes **one metal edge per merged
  polygon** between two endpoint nodes at the polygon's bounding-box ends
  along its longer axis -- ``resistance_ohm = sheet_resistance_ohm_per_sq *
  length / width``. Every merged polygon in this module's own real-fixture
  validation (``tests/corpus/place_and_route/gcd.gds.gz``, a genuine ``klt
  par`` output) is an axis-aligned box, matching this model exactly; a
  non-rectangular polygon is still accepted, approximated by its bounding
  box, with a ``warnings`` entry -- never a silent misrepresentation.
- Each via layer's net geometry becomes **one via edge** per merged via
  polygon, connecting the *nearest* existing node (by straight-line
  distance) on each of the two metal layers the via spec declares it
  bridges -- not a true T-junction split of the rail it taps. This is a
  documented v1 simplification (see ``docs/cli/power.md``'s "Scope and
  limitations"): precise enough to preserve every island's real connectivity
  and rail resistance, at the cost of a small positional error in exactly
  where along a rail a tap lands.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

from ._layout import load_layout, select_top_cells
from ._layout import region as _region
from ._layout import texts as _texts
from .ir_solver import solve_ir_drop

if TYPE_CHECKING:
    import klayout.db as kdb

#: `1` -- the resistive-network extraction (issue #844, Phase 1a of epic
#: #712) plus the static IR-drop solve (#845, Phase 1b). 1b added
#: `ir_drop_map`/`worst_case_droop_mv` **additively** -- every field 1a
#: documented is unchanged -- so no bump was needed (see docs/cli/power.md
#: and docs/json-contract.md's additive-envelope design). A future phase adds
#: `em_verdict` the same way.
SCHEMA_VERSION = 1


class PowerError(Exception):
    """Raised when ``klt power`` cannot run: a bad layout/spec file, a
    malformed stackup/via/power-net declaration, an unresolvable top cell,
    or a request whose declared power nets match no geometry at all.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- see ``docs/json-contract.md``.
    """


def _load_spec(spec_path: str) -> dict[str, Any]:
    if not os.path.exists(spec_path):
        raise PowerError(f"spec file not found: {spec_path}")
    if os.path.isdir(spec_path):
        raise PowerError(f"not a file: {spec_path}")
    try:
        with open(spec_path) as f:
            spec = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise PowerError(f"could not read spec '{spec_path}': {exc}") from exc
    if not isinstance(spec, dict):
        raise PowerError(f"spec '{spec_path}' must be a JSON object")
    return spec


def _parse_layer_datatype(raw: str, spec_path: str, field: str) -> tuple[int, int]:
    parts = raw.split("/")
    malformed = PowerError(
        f"spec '{spec_path}': {field} must be '<layer>/<datatype>' with "
        f"integer layer/datatype (got {raw!r})"
    )
    if len(parts) != 2:
        raise malformed
    try:
        return int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise malformed from exc


def _validate_power_nets(spec: dict[str, Any], spec_path: str) -> list[str]:
    raw = spec.get("power_nets")
    if not isinstance(raw, list) or not raw:
        raise PowerError(f"spec '{spec_path}' must have a non-empty 'power_nets' array")
    names: list[str] = []
    for entry in raw:
        name = str(entry).strip()
        if not name:
            raise PowerError(
                f"spec '{spec_path}': 'power_nets' entries must be non-empty strings"
            )
        if name not in names:
            names.append(name)
    return names


def _validate_stackup(spec: dict[str, Any], spec_path: str) -> list[dict[str, Any]]:
    raw = spec.get("stackup")
    if not isinstance(raw, list) or not raw:
        raise PowerError(f"spec '{spec_path}' must have a non-empty 'stackup' array")

    entries: list[dict[str, Any]] = []
    names: list[str] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise PowerError(f"spec '{spec_path}': stackup[{i}] must be a JSON object")
        for key in ("name", "layer", "sheet_resistance_ohm_per_sq"):
            if key not in entry:
                raise PowerError(f"spec '{spec_path}': stackup[{i}] missing {key!r}")
        name = str(entry["name"])
        if name in names:
            raise PowerError(f"spec '{spec_path}': duplicate stackup name {name!r}")
        names.append(name)

        layer = _parse_layer_datatype(
            str(entry["layer"]), spec_path, f"stackup[{i}].layer"
        )
        label_layer = None
        if entry.get("label_layer") is not None:
            label_layer = _parse_layer_datatype(
                str(entry["label_layer"]), spec_path, f"stackup[{i}].label_layer"
            )

        try:
            sheet_r = float(entry["sheet_resistance_ohm_per_sq"])
        except (TypeError, ValueError) as exc:
            raise PowerError(
                f"spec '{spec_path}': stackup[{i}].sheet_resistance_ohm_per_sq "
                f"must be a number (got {entry['sheet_resistance_ohm_per_sq']!r})"
            ) from exc
        if sheet_r < 0:
            raise PowerError(
                f"spec '{spec_path}': stackup[{i}].sheet_resistance_ohm_per_sq "
                f"must be >= 0 (got {sheet_r!r})"
            )

        entries.append(
            {
                "name": name,
                "layer": layer,
                "label_layer": label_layer,
                "sheet_resistance_ohm_per_sq": sheet_r,
            }
        )

    if not any(entry["label_layer"] is not None for entry in entries):
        raise PowerError(
            f"spec '{spec_path}': at least one 'stackup' entry must set "
            "'label_layer' -- otherwise no net in the layout can ever be "
            "matched by name"
        )
    return entries


def _validate_vias(
    spec: dict[str, Any], spec_path: str, stackup_names: list[str]
) -> list[dict[str, Any]]:
    raw = spec.get("vias", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise PowerError(f"spec '{spec_path}': 'vias' must be an array")

    entries: list[dict[str, Any]] = []
    names: list[str] = list(stackup_names)
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise PowerError(f"spec '{spec_path}': vias[{i}] must be a JSON object")
        for key in ("layer", "between", "resistance_ohm"):
            if key not in entry:
                raise PowerError(f"spec '{spec_path}': vias[{i}] missing {key!r}")

        layer = _parse_layer_datatype(
            str(entry["layer"]), spec_path, f"vias[{i}].layer"
        )

        between = entry["between"]
        if (
            not isinstance(between, list)
            or len(between) != 2
            or str(between[0]) == str(between[1])
            or any(str(n) not in stackup_names for n in between)
        ):
            raise PowerError(
                f"spec '{spec_path}': vias[{i}].between must name two distinct "
                f"'stackup' entries (got {between!r})"
            )

        try:
            resistance_ohm = float(entry["resistance_ohm"])
        except (TypeError, ValueError) as exc:
            raise PowerError(
                f"spec '{spec_path}': vias[{i}].resistance_ohm must be a "
                f"number (got {entry['resistance_ohm']!r})"
            ) from exc
        if resistance_ohm < 0:
            raise PowerError(
                f"spec '{spec_path}': vias[{i}].resistance_ohm must be >= 0 "
                f"(got {resistance_ohm!r})"
            )

        name = str(entry.get("name", f"via{i}"))
        if name in names:
            raise PowerError(f"spec '{spec_path}': duplicate via/stackup name {name!r}")
        names.append(name)

        entries.append(
            {
                "name": name,
                "layer": layer,
                "between": (str(between[0]), str(between[1])),
                "resistance_ohm": resistance_ohm,
            }
        )
    return entries


def _require_number(
    entry: dict[str, Any], key: str, spec_path: str, field: str
) -> float:
    if key not in entry:
        raise PowerError(f"spec '{spec_path}': {field} missing {key!r}")
    try:
        return float(entry[key])
    except (TypeError, ValueError) as exc:
        raise PowerError(
            f"spec '{spec_path}': {field}.{key} must be a number (got {entry[key]!r})"
        ) from exc


def _validate_pads(
    spec: dict[str, Any], spec_path: str, power_nets: list[str]
) -> list[dict[str, Any]]:
    """Validate the (optional) ``pads`` array: where each power net's supply
    is actually delivered. Without at least one pad on a net, that net has no
    DC path to a source and no operating point to solve for -- see
    ``docs/cli/power.md``."""
    raw = spec.get("pads", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise PowerError(f"spec '{spec_path}': 'pads' must be an array")

    pads: list[dict[str, Any]] = []
    names: list[str] = []
    for i, entry in enumerate(raw):
        field = f"pads[{i}]"
        if not isinstance(entry, dict):
            raise PowerError(f"spec '{spec_path}': {field} must be a JSON object")
        if "net" not in entry:
            raise PowerError(f"spec '{spec_path}': {field} missing 'net'")
        net = str(entry["net"])
        if net not in power_nets:
            raise PowerError(
                f"spec '{spec_path}': {field}.net {net!r} is not one of "
                f"'power_nets' ({', '.join(power_nets)})"
            )
        name = str(entry.get("name", f"pad{i}"))
        if name in names:
            raise PowerError(f"spec '{spec_path}': duplicate pad name {name!r}")
        names.append(name)
        pads.append(
            {
                "name": name,
                "net": net,
                "x_um": _require_number(entry, "x_um", spec_path, field),
                "y_um": _require_number(entry, "y_um", spec_path, field),
                "voltage_v": _require_number(entry, "voltage_v", spec_path, field),
            }
        )
    return pads


def _validate_current_model(
    spec: dict[str, Any], spec_path: str, power_nets: list[str]
) -> list[dict[str, Any]] | None:
    """Validate the (optional) ``current_model``: what each instance draws,
    and from/to which nets. Returns ``None`` when the spec declares no
    current model at all (extraction-only, the Phase 1a behaviour), or a list
    of normalised instance records."""
    raw = spec.get("current_model")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PowerError(f"spec '{spec_path}': 'current_model' must be a JSON object")

    def _default_net(key: str) -> str | None:
        value = raw.get(key)
        if value is None:
            return None
        net = str(value)
        if net not in power_nets:
            raise PowerError(
                f"spec '{spec_path}': current_model.{key} {net!r} is not one of "
                f"'power_nets' ({', '.join(power_nets)})"
            )
        return net

    default_supply = _default_net("supply_net")
    default_ground = _default_net("ground_net")

    instances_raw = raw.get("instances")
    if not isinstance(instances_raw, list) or not instances_raw:
        raise PowerError(
            f"spec '{spec_path}': 'current_model' must have a non-empty "
            "'instances' array"
        )

    instances: list[dict[str, Any]] = []
    names: list[str] = []
    for i, entry in enumerate(instances_raw):
        field = f"current_model.instances[{i}]"
        if not isinstance(entry, dict):
            raise PowerError(f"spec '{spec_path}': {field} must be a JSON object")

        name = str(entry.get("name", f"inst{i}"))
        if name in names:
            raise PowerError(f"spec '{spec_path}': duplicate instance name {name!r}")
        names.append(name)

        supply_net = (
            str(entry["supply_net"]) if "supply_net" in entry else default_supply
        )
        if supply_net is None:
            raise PowerError(
                f"spec '{spec_path}': {field} has no 'supply_net' and "
                "'current_model.supply_net' sets no default"
            )
        ground_net = (
            str(entry["ground_net"]) if "ground_net" in entry else default_ground
        )
        for key, net in (("supply_net", supply_net), ("ground_net", ground_net)):
            if net is not None and net not in power_nets:
                raise PowerError(
                    f"spec '{spec_path}': {field}.{key} {net!r} is not one of "
                    f"'power_nets' ({', '.join(power_nets)})"
                )
        if ground_net == supply_net:
            raise PowerError(
                f"spec '{spec_path}': {field} draws from and returns to the "
                f"same net {supply_net!r}"
            )

        current_a = _require_number(entry, "current_a", spec_path, field)
        if current_a < 0:
            raise PowerError(
                f"spec '{spec_path}': {field}.current_a must be >= 0 (got "
                f"{current_a!r}) -- an instance's draw is a magnitude; the "
                "supply/ground sign convention is set by 'supply_net'/"
                "'ground_net'"
            )

        instances.append(
            {
                "name": name,
                "supply_net": supply_net,
                "ground_net": ground_net,
                "x_um": _require_number(entry, "x_um", spec_path, field),
                "y_um": _require_number(entry, "y_um", spec_path, field),
                "current_a": current_a,
            }
        )
    return instances


def _nearest_endpoint(
    rails: list[dict[str, Any]], x_um: float, y_um: float
) -> str | None:
    """The id of the node (among every rail endpoint recorded for one net on
    one metal layer) nearest to ``(x_um, y_um)`` -- the via-tap
    approximation this module's docstring documents. ``None`` when the net
    has no rail geometry at all on that layer."""
    best_id: str | None = None
    best_dist: float | None = None
    for rail in rails:
        for node_id, nx, ny in rail["endpoints"]:
            dist = (nx - x_um) ** 2 + (ny - y_um) ** 2
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best_id = node_id
    return best_id


def _build_island_network(
    l2n: kdb.LayoutToNetlist,
    net: kdb.Net,
    stackup: list[dict[str, Any]],
    vias: list[dict[str, Any]],
    layer_index: dict[str, int],
    via_layer_index: dict[str, int],
    dbu: float,
    net_name: str,
    island_id: str,
    warnings: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build one electrically-connected island's node/edge lists -- see this
    module's docstring for the resistor-network model. Returns ``(nodes,
    edges)``, both empty when ``net`` has no geometry on any declared
    ``stackup`` layer (the caller decides whether that is worth a warning)."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    rails_by_layer: dict[str, list[dict[str, Any]]] = {
        entry["name"]: [] for entry in stackup
    }
    counters = {"node": 0, "edge": 0}

    def add_node(layer_name: str, x_um: float, y_um: float) -> str:
        node_id = f"n{counters['node']}"
        counters["node"] += 1
        nodes.append(
            {
                "id": node_id,
                "layer": layer_name,
                "x_um": round(x_um, 6),
                "y_um": round(y_um, 6),
            }
        )
        return node_id

    def add_edge(
        kind: str, layer_name: str, frm: str, to: str, resistance_ohm: float
    ) -> None:
        edge_id = f"e{counters['edge']}"
        counters["edge"] += 1
        edges.append(
            {
                "id": edge_id,
                "kind": kind,
                "layer": layer_name,
                "from": frm,
                "to": to,
                "resistance_ohm": round(resistance_ohm, 9),
            }
        )

    for entry in stackup:
        region = l2n.polygons_of_net(net, layer_index[entry["name"]]).merged()
        for polygon in region.each():
            box = polygon.bbox()
            x0, y0, x1, y1 = (
                box.left * dbu,
                box.bottom * dbu,
                box.right * dbu,
                box.top * dbu,
            )
            width_um = x1 - x0
            height_um = y1 - y0
            if width_um <= 0 or height_um <= 0:
                continue

            if width_um >= height_um:
                y_mid = (y0 + y1) / 2
                a_x, a_y, b_x, b_y = x0, y_mid, x1, y_mid
                length_um, cross_um = width_um, height_um
            else:
                x_mid = (x0 + x1) / 2
                a_x, a_y, b_x, b_y = x_mid, y0, x_mid, y1
                length_um, cross_um = height_um, width_um

            n_a = add_node(entry["name"], a_x, a_y)
            n_b = add_node(entry["name"], b_x, b_y)
            resistance_ohm = entry["sheet_resistance_ohm_per_sq"] * length_um / cross_um
            add_edge("metal", entry["name"], n_a, n_b, resistance_ohm)
            rails_by_layer[entry["name"]].append(
                {"endpoints": [(n_a, a_x, a_y), (n_b, b_x, b_y)]}
            )

            if not polygon.is_box():
                warnings.append(
                    f"power net {net_name!r} island {island_id!r}: a "
                    f"non-rectangular {entry['name']} segment was approximated "
                    "by its bounding box"
                )

    for via in vias:
        region = l2n.polygons_of_net(net, via_layer_index[via["name"]]).merged()
        for polygon in region.each():
            box = polygon.bbox()
            cx = (box.left + box.right) / 2 * dbu
            cy = (box.bottom + box.top) / 2 * dbu
            layer_a, layer_b = via["between"]
            node_a = _nearest_endpoint(rails_by_layer[layer_a], cx, cy)
            node_b = _nearest_endpoint(rails_by_layer[layer_b], cx, cy)
            if node_a is None or node_b is None:
                missing = layer_a if node_a is None else layer_b
                warnings.append(
                    f"power net {net_name!r} island {island_id!r}: a "
                    f"{via['name']!r} via at ({cx:.3f}, {cy:.3f}) um has no "
                    f"matching {missing!r} rail on this net -- skipped"
                )
                continue
            add_edge("via", via["name"], node_a, node_b, via["resistance_ohm"])

    return nodes, edges


def _nearest_node(
    net_nodes: list[tuple[int, dict[str, Any]]], x_um: float, y_um: float
) -> tuple[int, dict[str, Any]] | None:
    """``(island_index, node)`` of the node nearest ``(x_um, y_um)`` among
    one net's whole extracted geometry, or ``None`` when the net has no nodes
    at all. Ties resolve to the first node in extraction order, so a given
    layout+spec always attaches a pad/instance to the same node.

    This is the same "snap to the nearest existing node" approximation
    ``_nearest_endpoint`` already uses for via taps (see this module's
    docstring and ``docs/cli/power.md``'s "Scope and limitations"): a pad or
    an instance is wired to the closest *extracted* node, not spliced into a
    rail at its exact position."""
    best: tuple[int, dict[str, Any]] | None = None
    best_dist: float | None = None
    for island_index, node in net_nodes:
        dist = (node["x_um"] - x_um) ** 2 + (node["y_um"] - y_um) ** 2
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best = (island_index, node)
    return best


def _solve_ir_drop(
    networks: list[dict[str, Any]],
    pads: list[dict[str, Any]],
    instances: list[dict[str, Any]] | None,
    warnings: list[str],
) -> dict[str, Any]:
    """Solve every extracted island for its DC operating point and build the
    ``ir_drop_map`` response block. See ``docs/cli/power.md`` for the shape
    and :mod:`klayout_tools.ir_solver` for the numerics."""
    by_net = {entry["net"]: entry for entry in networks}
    nodes_by_net: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for net_name, entry in by_net.items():
        nodes_by_net[net_name] = [
            (island_index, node)
            for island_index, island in enumerate(entry["islands"])
            for node in island["nodes"]
        ]

    # (net, island_index) -> {node_id: value}
    pad_voltages: dict[tuple[str, int], dict[str, float]] = {}
    injections: dict[tuple[str, int], dict[str, float]] = {}
    pad_counts: dict[tuple[str, int], int] = {}
    instance_counts: dict[tuple[str, int], int] = {}
    pad_reports: list[dict[str, Any]] = []

    def _attach(
        net_name: str, x_um: float, y_um: float
    ) -> tuple[int, dict[str, Any]] | None:
        return _nearest_node(nodes_by_net.get(net_name, []), x_um, y_um)

    for pad in pads:
        target = _attach(pad["net"], pad["x_um"], pad["y_um"])
        report = dict(pad)
        if target is None:
            report["island_id"] = None
            report["node_id"] = None
            pad_reports.append(report)
            warnings.append(
                f"pad {pad['name']!r} on net {pad['net']!r} has no extracted "
                "geometry on that net to attach to -- skipped"
            )
            continue
        island_index, node = target
        key = (pad["net"], island_index)
        island_id = by_net[pad["net"]]["islands"][island_index]["island_id"]
        report["island_id"] = island_id
        report["node_id"] = node["id"]
        pad_reports.append(report)
        existing = pad_voltages.setdefault(key, {})
        existing[node["id"]] = pad["voltage_v"]
        pad_counts[key] = pad_counts.get(key, 0) + 1

    def _inject(net_name: str, instance: dict[str, Any], current_a: float) -> None:
        target = _attach(net_name, instance["x_um"], instance["y_um"])
        if target is None:
            warnings.append(
                f"current_model instance {instance['name']!r} has no extracted "
                f"{net_name!r} geometry to attach to -- its {abs(current_a) * 1e3:g} "
                "mA is not modelled on that net"
            )
            return
        island_index, node = target
        key = (net_name, island_index)
        bucket = injections.setdefault(key, {})
        bucket[node["id"]] = bucket.get(node["id"], 0.0) + current_a
        instance_counts[key] = instance_counts.get(key, 0) + 1

    for instance in instances or []:
        # An instance *draws* current off its supply net (a negative
        # injection) and *returns* the same current to its ground net (a
        # positive injection) -- the sign convention `ir_solver` documents.
        _inject(instance["supply_net"], instance, -instance["current_a"])
        if instance["ground_net"] is not None:
            _inject(instance["ground_net"], instance, instance["current_a"])

    net_reports: list[dict[str, Any]] = []
    overall_worst: dict[str, Any] | None = None
    overall_worst_droop = 0.0
    unsolved_current_a = 0.0
    solved_node_total = 0
    unsolved_node_total = 0
    quiet_no_pad_islands: list[str] = []

    for entry in networks:
        net_name = entry["net"]
        net_worst: dict[str, Any] | None = None
        net_worst_droop = 0.0
        island_reports: list[dict[str, Any]] = []
        net_current = 0.0
        solved_islands = 0

        for island_index, island in enumerate(entry["islands"]):
            key = (net_name, island_index)
            island_pads = pad_voltages.get(key, {})
            island_injections = injections.get(key, {})
            net_current += sum(abs(v) for v in island_injections.values())

            solution = solve_ir_drop(
                [node["id"] for node in island["nodes"]],
                island["edges"],
                pads=island_pads,
                injections=island_injections,
            )

            solved_components = [c for c in solution["components"] if c["solved"]]
            unsolved_components = [c for c in solution["components"] if not c["solved"]]
            solved_nodes = sum(c["node_count"] for c in solved_components)
            unsolved_nodes = sum(c["node_count"] for c in unsolved_components)
            solved_node_total += solved_nodes
            unsolved_node_total += unsolved_nodes

            island_worst_id: str | None = None
            island_worst_droop = 0.0
            for node in island["nodes"]:
                deviation = solution["deviations"][node["id"]]
                if deviation is None:
                    continue
                if island_worst_id is None or abs(deviation) > island_worst_droop:
                    island_worst_id = node["id"]
                    island_worst_droop = abs(deviation)

            reason = unsolved_components[0]["reason"] if unsolved_components else None
            if unsolved_components:
                stranded = sum(
                    abs(c["injected_current_a"]) for c in unsolved_components
                )
                unsolved_current_a += stranded
                if stranded or reason != "no_pad":
                    # Worth naming individually: either the current model
                    # puts real current somewhere with no return path to a
                    # pad, or the solver itself did not converge.
                    warnings.append(
                        f"island {island['island_id']!r}: {unsolved_nodes} of "
                        f"{len(island['nodes'])} node(s) not solved ({reason})"
                        + (
                            f" -- {stranded * 1e3:g} mA of the current model "
                            "lands there with no pad to source it"
                            if stranded
                            else ""
                        )
                    )
                else:
                    quiet_no_pad_islands.append(island["island_id"])

            island_report = {
                "island_id": island["island_id"],
                "solved": bool(solved_components),
                "unsolved_reason": reason,
                "pad_count": pad_counts.get(key, 0),
                "instance_count": instance_counts.get(key, 0),
                "current_a": round(
                    float(sum(abs(v) for v in island_injections.values())), 12
                ),
                "reference_voltage_v": (
                    solved_components[0]["reference_voltage_v"]
                    if solved_components
                    else None
                ),
                "pad_current_a": (
                    round(float(sum(c["pad_current_a"] for c in solved_components)), 12)
                    if solved_components
                    else None
                ),
                "solved_node_count": solved_nodes,
                "unsolved_node_count": unsolved_nodes,
                "worst_case_droop_mv": (
                    round(island_worst_droop * 1e3, 9)
                    if island_worst_id is not None
                    else None
                ),
                "worst_case_node_id": island_worst_id,
                "iterations": max(
                    (c["iterations"] for c in solution["components"]), default=0
                ),
                "residual": max(
                    (c["residual"] for c in solution["components"]), default=0.0
                ),
                "nodes": [
                    {
                        "id": node["id"],
                        "voltage_v": _round_or_none(
                            solution["voltages"][node["id"]], 9
                        ),
                        "droop_mv": _round_or_none(
                            _abs_or_none(solution["deviations"][node["id"]]), 9, 1e3
                        ),
                        # What the current model + pad snapping actually
                        # attached here (signed: negative = drawn off this
                        # net). Reported so a reader can reconstruct the exact
                        # network that was solved -- see
                        # tests/test_power_ir_cross_check.py, which rebuilds
                        # it for ngspice from this field.
                        "injected_current_a": round(
                            float(island_injections.get(node["id"], 0.0)), 12
                        ),
                        "pad_voltage_v": island_pads.get(node["id"]),
                    }
                    for node in island["nodes"]
                ],
                "edges": [
                    {
                        "id": edge["id"],
                        "current_a": _round_or_none(
                            solution["edge_currents"][edge["id"]], 12
                        ),
                    }
                    for edge in island["edges"]
                ],
            }
            island_reports.append(island_report)
            if solved_components:
                solved_islands += 1

            if island_worst_id is not None and (
                net_worst is None or island_worst_droop > net_worst_droop
            ):
                node = next(n for n in island["nodes"] if n["id"] == island_worst_id)
                net_worst_droop = island_worst_droop
                net_worst = {
                    "net": net_name,
                    "island_id": island["island_id"],
                    "node_id": island_worst_id,
                    "layer": node["layer"],
                    "x_um": node["x_um"],
                    "y_um": node["y_um"],
                    "voltage_v": _round_or_none(
                        solution["voltages"][island_worst_id], 9
                    ),
                    "droop_mv": round(island_worst_droop * 1e3, 9),
                }

        net_reports.append(
            {
                "net": net_name,
                "pad_count": sum(1 for pad in pads if pad["net"] == net_name),
                "instance_count": sum(
                    instance_counts.get((net_name, i), 0)
                    for i in range(len(entry["islands"]))
                ),
                "current_a": round(float(net_current), 12),
                "solved_island_count": solved_islands,
                "unsolved_island_count": len(entry["islands"]) - solved_islands,
                "worst_case_droop_mv": (
                    round(net_worst_droop * 1e3, 9) if net_worst else None
                ),
                "worst_case_node": net_worst,
                "islands": island_reports,
            }
        )

        if net_worst and (
            overall_worst is None or net_worst_droop > overall_worst_droop
        ):
            overall_worst_droop = net_worst_droop
            overall_worst = net_worst

    if quiet_no_pad_islands:
        warnings.append(
            f"{len(quiet_no_pad_islands)} island(s) have no pad and no modelled "
            "current, so they have no DC operating point and were not solved "
            f"(first: {quiet_no_pad_islands[0]!r}) -- see "
            "ir_drop_map.nets[].islands[].unsolved_reason for the full list"
        )

    return {
        "pads": pad_reports,
        "instance_count": len(instances or []),
        "total_current_a": round(
            float(sum(instance["current_a"] for instance in instances or [])), 12
        ),
        "unsolved_current_a": round(float(unsolved_current_a), 12),
        "solved_node_count": solved_node_total,
        "unsolved_node_count": unsolved_node_total,
        "worst_case": overall_worst,
        "nets": net_reports,
    }


def _abs_or_none(value: float | None) -> float | None:
    return None if value is None else abs(value)


def _round_or_none(
    value: float | None, digits: int, scale: float = 1.0
) -> float | None:
    return None if value is None else round(value * scale, digits)


def run_power(
    file: str,
    spec_path: str,
    *,
    top: str | None = None,
) -> dict[str, Any]:
    """Run ``klt power``'s resistive-network extraction (and, when the spec
    asks for one, the static IR-drop solve) end to end.

    ``file`` is a routed GDSII/OASIS layout (e.g. a ``klt par`` output);
    ``spec_path`` is a JSON file with:

    - ``power_nets`` (required, non-empty array of strings): net names to
      extract, e.g. ``["VPWR", "VGND"]``.
    - ``stackup`` (required, non-empty array): each entry declares one
      metal role -- ``{"name", "layer": "<layer>/<datatype>",
      "label_layer": "<layer>/<datatype>" (optional),
      "sheet_resistance_ohm_per_sq"}``. At least one entry must set
      ``label_layer`` (the routed layout's own pin/net-name text on that
      layer), or no net can ever be matched by name.
    - ``vias`` (optional array, default ``[]``): each entry bridges two
      ``stackup`` names -- ``{"name" (optional, defaults to "via<index>"),
      "layer": "<layer>/<datatype>", "between": ["<metal>", "<metal>"],
      "resistance_ohm"}``.
    - ``pads`` (optional array, default ``[]``): where each net's supply is
      delivered -- ``{"name" (optional), "net", "x_um", "y_um",
      "voltage_v"}``.
    - ``current_model`` (optional object): what each instance draws --
      ``{"supply_net"/"ground_net" (optional per-model defaults),
      "instances": [{"name" (optional), "x_um", "y_um", "current_a",
      "supply_net"/"ground_net" (optional per-instance overrides)}]}``.

    ``top`` selects the top cell to analyse when the stream has more than
    one (required in that case, matching ``select_top_cells``'s convention
    -- see ``docs/cli/layers.md``'s ``--top``).

    Returns a dict matching the documented ``klt power`` JSON schema (see
    ``docs/cli/power.md``), including ``schema_version``. ``ir_drop_map`` and
    ``worst_case_droop_mv`` are ``None`` when the spec declares neither
    ``pads`` nor a ``current_model`` (extraction only -- there is nothing to
    solve). Raises :class:`PowerError` for a malformed spec, an unresolvable
    layout/top cell, or a request whose declared power nets match no geometry
    at all (every requested net individually unmatched is reported in
    ``warnings`` rather than raised, as long as at least one net resolved
    to at least one island).
    """
    spec = _load_spec(spec_path)
    power_nets = _validate_power_nets(spec, spec_path)
    stackup = _validate_stackup(spec, spec_path)
    stackup_names = [entry["name"] for entry in stackup]
    vias = _validate_vias(spec, spec_path, stackup_names)
    pads = _validate_pads(spec, spec_path, power_nets)
    instances = _validate_current_model(spec, spec_path, power_nets)

    layout = load_layout(file, PowerError)
    top_cells = select_top_cells(layout, top, PowerError)
    if len(top_cells) != 1:
        raise PowerError(
            f"klt power needs exactly one top cell to analyse ({len(top_cells)} "
            f"found in '{file}'); pass --top to select one"
        )
    top_cell = top_cells[0]
    dbu = layout.dbu

    # Imported lazily, matching `load_layout`'s own lazy `klayout.db` import.
    import klayout.db as kdb

    l2n = kdb.LayoutToNetlist(top_cell.name, dbu)

    layer_index: dict[str, int] = {}
    metal_regions: dict[str, Any] = {}
    for entry in stackup:
        metal_region = _region(layout, top_cell, entry["layer"])
        metal_regions[entry["name"]] = metal_region
        layer_index[entry["name"]] = l2n.register(metal_region, entry["name"])
        l2n.connect(metal_region)
        if entry["label_layer"] is not None:
            label_texts = _texts(layout, top_cell, entry["label_layer"])
            l2n.register(label_texts, f"{entry['name']}_label")
            l2n.connect(metal_region, label_texts)

    via_layer_index: dict[str, int] = {}
    for via in vias:
        via_region = _region(layout, top_cell, via["layer"])
        via_layer_index[via["name"]] = l2n.register(via_region, via["name"])
        l2n.connect(via_region)
        layer_a, layer_b = via["between"]
        l2n.connect(metal_regions[layer_a], via_region)
        l2n.connect(via_region, metal_regions[layer_b])

    try:
        l2n.extract_netlist()
    except Exception as exc:  # KLayout raises a bare RuntimeError on internal failure
        raise PowerError(f"connectivity extraction failed: {exc}") from exc

    netlist = l2n.netlist()
    circuit = netlist.circuit_by_name(top_cell.name)
    if circuit is None:
        raise PowerError(
            f"no circuit named '{top_cell.name}' in the extracted connectivity graph"
        )

    warnings: list[str] = []
    networks: list[dict[str, Any]] = []
    total_islands = 0

    for net_name in power_nets:
        matched = sorted(
            (
                candidate
                for candidate in circuit.each_net()
                if candidate.cluster_id != 0 and candidate.expanded_name() == net_name
            ),
            key=lambda candidate: candidate.cluster_id,
        )
        if not matched:
            warnings.append(
                f"power net {net_name!r} matches no labelled net in this layout -- "
                "check 'power_nets' against the layout's own pin/label text, and "
                "that at least one 'stackup' entry's 'label_layer' actually "
                "carries it"
            )
            networks.append(
                {
                    "net": net_name,
                    "island_count": 0,
                    "node_count": 0,
                    "edge_count": 0,
                    "islands": [],
                }
            )
            continue

        islands: list[dict[str, Any]] = []
        for island_index, net in enumerate(matched):
            island_id = f"{net_name}#{island_index}"
            nodes, edges = _build_island_network(
                l2n,
                net,
                stackup,
                vias,
                layer_index,
                via_layer_index,
                dbu,
                net_name,
                island_id,
                warnings,
            )
            if not nodes:
                warnings.append(
                    f"power net {net_name!r} island {island_id!r} (net id "
                    f"{net.cluster_id}) has a matching label but no geometry on "
                    "the declared 'stackup' layers -- skipped"
                )
                continue
            islands.append(
                {
                    "island_id": island_id,
                    "node_count": len(nodes),
                    "edge_count": len(edges),
                    "nodes": nodes,
                    "edges": edges,
                }
            )

        total_islands += len(islands)
        networks.append(
            {
                "net": net_name,
                "island_count": len(islands),
                "node_count": sum(island["node_count"] for island in islands),
                "edge_count": sum(island["edge_count"] for island in islands),
                "islands": islands,
            }
        )

    if total_islands == 0:
        raise PowerError(
            "none of the requested 'power_nets' matched any geometry in "
            f"'{file}' against this spec's 'stackup' -- see the per-net "
            "explanation this run would have reported in 'warnings', or check "
            "the layer/datatype numbers against the layout's own resolved PDK "
            "layer map"
        )

    ir_drop_map = None
    worst_case_droop_mv = None
    if pads or instances is not None:
        ir_drop_map = _solve_ir_drop(networks, pads, instances, warnings)
        worst_case = ir_drop_map["worst_case"]
        worst_case_droop_mv = worst_case["droop_mv"] if worst_case else None

    return {
        "schema_version": SCHEMA_VERSION,
        "file": file,
        "spec": spec_path,
        "power_nets": power_nets,
        "networks": networks,
        "node_count": sum(net_entry["node_count"] for net_entry in networks),
        "edge_count": sum(net_entry["edge_count"] for net_entry in networks),
        "island_count": total_islands,
        "ir_drop_map": ir_drop_map,
        "worst_case_droop_mv": worst_case_droop_mv,
        "warnings": warnings,
    }
