"""Label-derived ports and via-stack landing search for ``klt gen-compose``
``blocks[].cell`` blocks (issue #2927).

A hand-drawn macro names its ports only by text labels sitting on the block's
own routing, usually interior to the cell. :func:`parse_ports_from_labels`
validates the opt-in ``blocks[].cell.ports_from_labels`` option and
:func:`derive_label_ports` turns it into ordinary ``ports[]`` entries:

1. every named label is resolved to the net under it (``LayoutToNetlist``
   connectivity over the resolved PDK deck's ``metals``/``vias`` stack -- the
   same connectivity model ``klt extract`` and #2210's ``landed_on_block``
   use), and
2. when a port carries an ``access`` block, the port net's own shapes are
   searched for a landing point where the via ladder up to ``access.up_to``
   (the very ladder :func:`~.gen_compose_routing._resolve_via_drop_layer`
   makes the router draw at a pin, sized by the same helpers) keeps rule
   clearance from every other net, via spacing against the macro's existing
   cuts, and avoids caller-named keep-out layers.

The derived port is reported on the net's own layer, so the router's existing
via-drop ladder draws the stack at the chosen point; the search only decides
*where* that is legal. The search is grid-based, bounded and deterministic:
candidates are ranked by distance to the label (ties by ``x`` then ``y``).

Dependency direction mirrors ``gen_compose_routing.py``: ``gen_compose.py``
imports this module, this module imports ``gen_compose`` names only inside
functions.
"""

from __future__ import annotations

import math
from typing import Any

from .decks import UnknownDeckError, get_deck, get_extraction_deck, get_nominal_dbu
from .gen import GenError
from .gen_compose_routing import (
    _landing_pad_side_um_for_layer,
    _min_enclosing_margin_um_for_layer,
    _min_width_um_for_layer,
    _pdk_family,
    _resolve_route_layer,
    _resolve_via_drop_layer,
)
from .gen_layer_params import mfg_grid_um

#: Default cap on landing candidates evaluated per port (nearest-first).
DEFAULT_MAX_CANDIDATES = 20000

#: Hard bound on how many grid points one net's bounding box may enumerate
#: before the caller must coarsen ``access.grid_um``.
_MAX_ENUMERATED_POINTS = 4_000_000

#: Sample rejected candidates kept per category in a no-legal-point error.
_REJECTION_SAMPLES = 3

_LayerPair = tuple[int, int]


def _err(message: str) -> Exception:
    from .gen_compose import GenComposeError

    return GenComposeError(message)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_layer_pair(raw: Any, where: str) -> _LayerPair:
    if (
        not isinstance(raw, dict)
        or not _is_int(raw.get("layer"))
        or not _is_int(raw.get("datatype"))
    ):
        raise _err(
            f"{where} must be a JSON object with integer 'layer'/'datatype' "
            "fields (the same shape `klt layers` reports)"
        )
    return (raw["layer"], raw["datatype"])


def _parse_layer_list(raw: Any, where: str, *, required: bool) -> list[_LayerPair]:
    if raw is None and not required:
        return []
    if not isinstance(raw, list) or (required and not raw):
        raise _err(f"{where} must be a{' non-empty' if required else 'n'} array")
    return [_parse_layer_pair(item, f"{where}[{i}]") for i, item in enumerate(raw)]


def _parse_access(raw: Any, where: str) -> dict[str, Any]:
    """Validate one ``access`` object: ``up_to`` (a routing layer role),
    ``via_array`` (``[1, 1]`` only for now), ``keepout_layers``, and the
    optional search knobs ``grid_um`` / ``max_candidates``."""
    if not isinstance(raw, dict):
        raise _err(f"{where} must be a JSON object")
    unknown = set(raw) - {
        "up_to",
        "via_array",
        "keepout_layers",
        "grid_um",
        "max_candidates",
    }
    if unknown:
        raise _err(f"{where} has unknown field(s): {', '.join(sorted(unknown))}")
    up_to = raw.get("up_to")
    if not isinstance(up_to, str) or not up_to:
        raise _err(
            f"{where}.up_to is required (a routing layer role such as "
            "'metal4', the same names routing.layer_role accepts)"
        )
    via_array = raw.get("via_array", [1, 1])
    if (
        not isinstance(via_array, list)
        or len(via_array) != 2
        or not all(_is_int(n) and n >= 1 for n in via_array)
    ):
        raise _err(f"{where}.via_array must be [rows, cols] of integers >= 1")
    if via_array != [1, 1]:
        raise _err(
            f"{where}.via_array {via_array} is not supported yet -- the "
            "router draws a single cut per via hop, so a landing found for a "
            "larger array could not be drawn; use [1, 1]"
        )
    grid_um = raw.get("grid_um")
    if grid_um is not None and (
        isinstance(grid_um, bool)
        or not isinstance(grid_um, (int, float))
        or grid_um <= 0
    ):
        raise _err(f"{where}.grid_um must be a number > 0")
    max_candidates = raw.get("max_candidates", DEFAULT_MAX_CANDIDATES)
    if not _is_int(max_candidates) or max_candidates < 1:
        raise _err(f"{where}.max_candidates must be an integer >= 1")
    return {
        "up_to": up_to,
        "via_array": (1, 1),
        "keepout_layers": _parse_layer_list(
            raw.get("keepout_layers"), f"{where}.keepout_layers", required=False
        ),
        "grid_um": None if grid_um is None else float(grid_um),
        "max_candidates": max_candidates,
    }


def _parse_label_names(
    raw_names: list[Any], shared_access: dict[str, Any] | None, where: str
) -> list[dict[str, Any]]:
    names: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw_names):
        at = f"{where}.names[{index}]"
        access = shared_access
        name: Any
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, dict):
            name = entry.get("name")
            if entry.get("access") is not None:
                access = _parse_access(entry["access"], f"{at}.access")
        else:
            raise _err(f"{at} must be a label string or an object with 'name'")
        if not isinstance(name, str) or not name:
            raise _err(f"{at} needs a non-empty label name")
        if name in seen:
            raise _err(f"{where}.names contains duplicate name '{name}'")
        seen.add(name)
        names.append({"name": name, "access": access})
    return names


def parse_ports_from_labels(raw: Any, where: str) -> dict[str, Any] | None:
    """Validate ``blocks[].cell.ports_from_labels``.

    ``{"layers": [{layer, datatype}...], "names": [...], "access": {...}}``.
    ``names`` entries are label strings or ``{"name": ..., "access": {...}}``
    objects; a per-name ``access`` overrides the shared top-level one. A name
    with no access resolves to a port at its label position (no landing
    search). Returns ``None`` when ``raw`` is ``None`` (option omitted).
    """
    if raw is None:
        return None
    where = f"{where}.ports_from_labels"
    if not isinstance(raw, dict):
        raise _err(f"{where} must be a JSON object")
    unknown = set(raw) - {"layers", "names", "access"}
    if unknown:
        raise _err(f"{where} has unknown field(s): {', '.join(sorted(unknown))}")
    layers = _parse_layer_list(raw.get("layers"), f"{where}.layers", required=True)
    raw_names = raw.get("names")
    if not isinstance(raw_names, list) or not raw_names:
        raise _err(f"{where}.names must be a non-empty array")
    shared = raw.get("access")
    shared_access = None if shared is None else _parse_access(shared, f"{where}.access")

    names = _parse_label_names(raw_names, shared_access, where)
    return {"layers": layers, "names": names}


# --- label -> net ---------------------------------------------------------


def _label_candidates(
    src_layout: Any, src_cell: Any, layers: list[_LayerPair]
) -> dict[str, list[tuple[_LayerPair, int, int]]]:
    """``{label text: [(layer, x_dbu, y_dbu), ...]}`` for every text shape on
    ``layers`` (flattened to the top cell frame), sorted for determinism."""
    import klayout.db as kdb

    found: dict[str, list[tuple[_LayerPair, int, int]]] = {}
    for pair in layers:
        index = src_layout.find_layer(pair[0], pair[1])
        if index is None:
            continue
        it = src_cell.begin_shapes_rec(index)
        while not it.at_end():
            shape = it.shape()
            if shape.is_text():
                text = shape.text
                point = it.trans() * kdb.Point(text.x, text.y)
                found.setdefault(text.string, []).append((pair, point.x, point.y))
            it.next()
    for entries in found.values():
        entries.sort(key=lambda e: (e[1], e[2], e[0]))
    return found


class _MacroNets:
    """``LayoutToNetlist`` connectivity over a macro's deck metal/via stack,
    plus flat per-layer regions for the clearance search."""

    def __init__(self, src_layout: Any, src_cell: Any, variant: str):
        import klayout.db as kdb

        try:
            self.deck = get_extraction_deck(_pdk_family(variant))
        except (GenError, UnknownDeckError) as exc:
            raise _err(
                f"ports_from_labels: no extraction deck for '{variant}': {exc}"
            ) from exc
        self.layout = src_layout
        self.cell = src_cell
        self.dbu = src_layout.dbu
        self.l2n = kdb.LayoutToNetlist(
            kdb.RecursiveShapeIterator(src_layout, src_cell, [])
        )
        self.l2n_layers: dict[_LayerPair, Any] = {}
        self.flat: dict[_LayerPair, Any] = {}
        stack = list(self.deck.metals) + list(self.deck.vias)
        for pair in stack:
            index = src_layout.find_layer(pair[0], pair[1])
            if index is None:
                continue
            self.l2n_layers[pair] = self.l2n.make_layer(index, f"L{pair[0]}_{pair[1]}")
            flat = kdb.Region(src_cell.begin_shapes_rec(index))
            flat.merge()
            self.flat[pair] = flat
        for region in self.l2n_layers.values():
            self.l2n.connect(region)
        metals, vias = self.deck.metals, self.deck.vias
        for i, via in enumerate(vias):
            if via not in self.l2n_layers:
                continue
            for metal in (metals[i], metals[i + 1]):
                if metal in self.l2n_layers:
                    self.l2n.connect(self.l2n_layers[via], self.l2n_layers[metal])
        self.l2n.extract_netlist()

    def metal_for_label(self, layer: _LayerPair) -> list[_LayerPair]:
        """Deck metals a label on ``layer`` may sit on, best match first: the
        metal itself, the metal whose ``metal_labels`` entry is ``layer``,
        else every metal sharing the GDS layer number."""
        metals = list(self.deck.metals)
        if layer in metals:
            return [layer]
        labels = list(getattr(self.deck, "metal_labels", ()) or ())
        for metal, label in zip(metals, labels, strict=False):
            if label == layer:
                return [metal]
        same_number = [m for m in metals if m[0] == layer[0]]
        return same_number or metals

    def probe(self, metal: _LayerPair, x_dbu: int, y_dbu: int) -> Any:
        import klayout.db as kdb

        region = self.l2n_layers.get(metal)
        if region is None:
            return None
        return self.l2n.probe_net(
            region, kdb.DPoint(x_dbu * self.dbu, y_dbu * self.dbu)
        )

    def net_shapes(self, net: Any, pair: _LayerPair) -> Any:
        import klayout.db as kdb

        region = self.l2n_layers.get(pair)
        if region is None:
            return kdb.Region()
        shapes = self.l2n.shapes_of_net(net, region, True)
        shapes.merge()
        return shapes


def _resolve_label(
    nets: _MacroNets, name: str, entries: list[tuple[_LayerPair, int, int]]
) -> tuple[Any, _LayerPair, int, int]:
    """``(net, metal layer, x_dbu, y_dbu)`` for label ``name`` -- the first
    (lowest x, y) text of that name that sits on a conductor. Every text of
    the name must resolve to the same net, else the name is ambiguous."""
    resolved: list[tuple[Any, _LayerPair, int, int]] = []
    for layer, x, y in entries:
        hits = []
        for metal in nets.metal_for_label(layer):
            net = nets.probe(metal, x, y)
            if net is not None:
                hits.append((net, metal))
        if not hits:
            continue
        if len({net.cluster_id for net, _ in hits}) > 1:
            raise _err(
                f"ports_from_labels: label '{name}' at ({x * nets.dbu}, "
                f"{y * nets.dbu}) um touches several unconnected nets "
                "(overlapping metals) -- cannot tell which one it names"
            )
        resolved.append((hits[0][0], hits[0][1], x, y))
    if not resolved:
        raise _err(
            f"ports_from_labels: label '{name}' sits on no conductor of the "
            "PDK deck's metal stack (checked every text of that name)"
        )
    if len({r[0].cluster_id for r in resolved}) > 1:
        raise _err(
            f"ports_from_labels: label '{name}' appears on more than one "
            "distinct net -- ambiguous"
        )
    return resolved[0]


# --- landing search -------------------------------------------------------


def _min_spacing_um(variant: str, layer: _LayerPair) -> float:
    """Tightest plain ``space`` rule (um) on ``layer``; ``0.0`` if none."""
    try:
        family = _pdk_family(variant)
        rules = get_deck(family)
        dbu_um = get_nominal_dbu(family)
    except (GenError, UnknownDeckError):
        return 0.0
    best = 0.0
    for rule in rules:
        if (
            rule.check == "space"
            and rule.layer == layer
            and rule.other_layer is None
            and rule.derived_layer is None
        ):
            best = max(best, rule.threshold_dbu * dbu_um)
    return best


def _stack_sizes(
    variant: str, ladder: tuple[tuple[_LayerPair, _LayerPair, _LayerPair], ...]
) -> tuple[dict[_LayerPair, float], dict[_LayerPair, float]]:
    """``(pad side per metal, cut side per via)`` in um, sized exactly as
    ``compose()`` sizes the drawn via-drop ladder (#1501/#2072/#2074/#2585)."""
    from .gen_compose import _VIA_DROP_SIZE_UM, _fixed_cut_side_um, _via_fixed_size_um

    cuts: dict[_LayerPair, float] = {}
    for via, _, _ in ladder:
        floor = _min_width_um_for_layer(variant, via)
        cuts[via] = _fixed_cut_side_um(
            max(_VIA_DROP_SIZE_UM, floor[0] if floor is not None else 0.0),
            _via_fixed_size_um(variant, via),
        )
    pads: dict[_LayerPair, float] = {}
    for via, lower, upper in ladder:
        for metal in (lower, upper):
            side = _landing_pad_side_um_for_layer(variant, metal)
            enclosure = _min_enclosing_margin_um_for_layer(variant, metal, via)
            if enclosure is not None:
                side = max(side, cuts[via] + 2 * enclosure[0])
            pads[metal] = max(pads.get(metal, 0.0), side)
    return pads, cuts


def _box(x: int, y: int, half: int, grow: int = 0) -> Any:
    import klayout.db as kdb

    h = half + grow
    return kdb.Region(kdb.Box(x - h, y - h, x + h, y + h))


def _first_rejection(
    x: int,
    y: int,
    pad_half: dict[_LayerPair, int],
    cut_half: dict[_LayerPair, int],
    space_dbu: dict[_LayerPair, int],
    obstacles: dict[_LayerPair, Any],
    keepout: Any,
) -> tuple[str, str] | None:
    """``(category, detail)`` for the first rule the candidate breaks, or
    ``None`` when the whole stack at ``(x, y)`` is legal."""
    if keepout is not None:
        for pair, half in {**pad_half, **cut_half}.items():
            if not (keepout & _box(x, y, half)).is_empty():
                return "keep_out", f"{pair[0]}/{pair[1]} footprint on a keep-out layer"
    for pair, half in cut_half.items():
        if not (obstacles[pair] & _box(x, y, half, space_dbu[pair])).is_empty():
            return (
                "via_spacing",
                f"cut {pair[0]}/{pair[1]} too close to an existing via",
            )
    for pair, half in pad_half.items():
        if not (obstacles[pair] & _box(x, y, half, space_dbu[pair])).is_empty():
            return "clearance", f"pad {pair[0]}/{pair[1]} too close to another net"
    return None


def _grid_candidates(
    net_region: Any, label_xy: tuple[int, int], step: int, cap: int
) -> tuple[list[tuple[int, int]], bool]:
    """Grid points whose centre lies inside ``net_region``, nearest to the
    label first (ties: x then y), at most ``cap`` of them; plus a truncated
    flag. Candidate order is fully determined by geometry, never by dict/set
    iteration."""
    import klayout.db as kdb

    box = net_region.bbox()
    xs = range(-(-box.left // step) * step, box.right + 1, step)
    ys = range(-(-box.bottom // step) * step, box.top + 1, step)
    if len(xs) * len(ys) > _MAX_ENUMERATED_POINTS:
        raise _err(
            "ports_from_labels: the port net spans too many grid points "
            f"({len(xs) * len(ys)}) -- raise access.grid_um to coarsen the search"
        )
    points = [
        (x, y)
        for x in xs
        for y in ys
        if not net_region.interacting(
            kdb.Region(kdb.Box(x, y, x + 1, y + 1))
        ).is_empty()
    ]
    lx, ly = label_xy
    points.sort(key=lambda p: ((p[0] - lx) ** 2 + (p[1] - ly) ** 2, p[0], p[1]))
    return points[:cap], len(points) > cap


def _stack_obstacles(
    nets: _MacroNets,
    net: Any,
    pad_half: dict[_LayerPair, int],
    cut_half: dict[_LayerPair, int],
) -> dict[_LayerPair, Any]:
    """Per-layer regions the landing must stay clear of: every *other* net's
    metal under a pad, and every existing cut under a new cut."""
    import klayout.db as kdb

    obstacles: dict[_LayerPair, Any] = {}
    for pair in pad_half:
        layer_region = nets.flat.get(pair, kdb.Region())
        obstacles[pair] = layer_region - nets.net_shapes(net, pair)
    for pair in cut_half:
        obstacles[pair] = nets.flat.get(pair, kdb.Region())
    return obstacles


def _keepout_region(nets: _MacroNets, layers: list[_LayerPair]) -> Any:
    """Merged region of the caller's keep-out layers, or ``None`` if unset."""
    import klayout.db as kdb

    if not layers:
        return None
    keepout = kdb.Region()
    for pair in layers:
        index = nets.layout.find_layer(pair[0], pair[1])
        if index is not None:
            keepout += kdb.Region(nets.cell.begin_shapes_rec(index))
    keepout.merge()
    return keepout


def _search_landing(
    nets: _MacroNets,
    variant: str,
    name: str,
    net: Any,
    net_layer: _LayerPair,
    label_xy: tuple[int, int],
    access: dict[str, Any],
) -> dict[str, Any]:
    """Find the nearest legal via-stack landing on ``net``; raises a
    ``GenComposeError`` listing per-category rejections when none exists."""
    up_to_layer = _resolve_route_layer(variant, access["up_to"])
    ladder, ladder_error = _resolve_via_drop_layer(nets.deck, up_to_layer, net_layer)
    if ladder_error is not None:
        raise _err(f"ports_from_labels: port '{name}': {ladder_error}")
    dbu = nets.dbu
    ladder = ladder or ()
    pads_um, cuts_um = _stack_sizes(variant, ladder)
    if not ladder:
        pads_um = {net_layer: 0.0}
    to_half = lambda um: max(1, int(math.ceil(um / dbu / 2.0)))  # noqa: E731
    pad_half = {pair: to_half(side) for pair, side in pads_um.items() if side > 0}
    cut_half = {pair: to_half(side) for pair, side in cuts_um.items()}
    space_dbu = {
        pair: int(round(_min_spacing_um(variant, pair) / dbu))
        for pair in {**pad_half, **cut_half}
    }

    obstacles = _stack_obstacles(nets, net, pad_half, cut_half)
    keepout = _keepout_region(nets, access["keepout_layers"])

    mfg = mfg_grid_um(_pdk_family(variant), net_layer) or dbu
    mfg_dbu = max(1, int(round(mfg / dbu)))
    wanted_dbu = int(
        round((access["grid_um"] or pads_um.get(net_layer, 0.0) / 4.0) / dbu)
    )
    step = max(mfg_dbu, (wanted_dbu // mfg_dbu) * mfg_dbu)

    net_region = nets.net_shapes(net, net_layer)
    candidates, truncated = _grid_candidates(
        net_region, label_xy, step, access["max_candidates"]
    )
    counts: dict[str, int] = {"keep_out": 0, "via_spacing": 0, "clearance": 0}
    samples: dict[str, list[str]] = {key: [] for key in counts}
    for x, y in candidates:
        rejection = _first_rejection(
            x, y, pad_half, cut_half, space_dbu, obstacles, keepout
        )
        if rejection is None:
            return {
                "x_um": x * dbu,
                "y_um": y * dbu,
                "layer": {"layer": net_layer[0], "datatype": net_layer[1]},
                "up_to": access["up_to"],
                "via_count": len(ladder),
                "pad_side_um": pads_um.get(net_layer, 0.0),
                "candidates_evaluated": candidates.index((x, y)) + 1,
                "truncated": truncated,
            }
        category, detail = rejection
        counts[category] += 1
        if len(samples[category]) < _REJECTION_SAMPLES:
            samples[category].append(f"({x * dbu:g}, {y * dbu:g}) um: {detail}")
    summary = ", ".join(f"{k}: {v}" for k, v in counts.items())
    detail_lines = "; ".join(s for key in counts for s in samples[key])
    raise _err(
        f"ports_from_labels: no legal landing for port '{name}' on layer "
        f"{net_layer[0]}/{net_layer[1]} up to '{access['up_to']}' -- "
        f"{len(candidates)} candidate(s) rejected ({summary})"
        + (" [candidate cap reached]" if truncated else "")
        + (f". e.g. {detail_lines}" if detail_lines else "")
    )


# --- public entry ---------------------------------------------------------


def derive_label_ports(
    block_id: str,
    gds_path: str,
    cell_name: str,
    spec: dict[str, Any],
    variant: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Resolve ``spec`` (from :func:`parse_ports_from_labels`) against the
    cell in ``gds_path``. Returns ``(ports, reports, warnings)`` in
    ``names`` order, in the block's own (un-oriented, un-translated) frame."""
    from .gen_compose import _load_block_cell

    src_layout, src_cell = _load_block_cell(
        block_id, {"gds_path": gds_path, "cell_name": cell_name}
    )
    labels = _label_candidates(src_layout, src_cell, spec["layers"])
    nets = _MacroNets(src_layout, src_cell, variant)
    ports: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    warnings: list[str] = []
    for entry in spec["names"]:
        name = entry["name"]
        if name not in labels:
            raise _err(
                f"blocks '{block_id}': ports_from_labels name '{name}' matches "
                "no text label on the listed layers"
            )
        net, metal, x, y = _resolve_label(nets, name, labels[name])
        dbu = nets.dbu
        landing = None
        if entry["access"] is not None:
            landing = _search_landing(
                nets, variant, name, net, metal, (x, y), entry["access"]
            )
            if landing["truncated"]:
                warnings.append(
                    f"block '{block_id}' port '{name}': landing search hit its "
                    "candidate cap; a legal point farther from the label may exist"
                )
        port: dict[str, Any] = {
            "name": name,
            "net": name,
            "x_um": landing["x_um"] if landing else x * dbu,
            "y_um": landing["y_um"] if landing else y * dbu,
            "layer": {"layer": metal[0], "datatype": metal[1]},
        }
        if landing and landing["pad_side_um"] > 0:
            port["width_um"] = landing["pad_side_um"]
        ports.append(port)
        reports.append(
            {
                "name": name,
                "label_um": {"x": x * dbu, "y": y * dbu},
                "layer": port["layer"],
                "landing": None
                if landing is None
                else {
                    "x_um": landing["x_um"],
                    "y_um": landing["y_um"],
                    "layer": landing["layer"],
                    "up_to": landing["up_to"],
                    "via_count": landing["via_count"],
                    "candidates_evaluated": landing["candidates_evaluated"],
                    "truncated": landing["truncated"],
                },
            }
        )
    return ports, reports, warnings
