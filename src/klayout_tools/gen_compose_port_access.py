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
   cuts, and avoids caller-named keep-out layers. The search itself lives in
   :mod:`~.gen_compose_port_search` (moved there from this file in #5608).

The derived port is reported on the net's own layer, so the router's existing
via-drop ladder draws the stack at the chosen point; the search only decides
*where* that is legal. The search is grid-based, bounded and deterministic:
candidates are ranked by distance to the label (ties by ``x`` then ``y``).

Dependency direction mirrors ``gen_compose_routing.py``: ``gen_compose.py``
imports this module, this module imports ``gen_compose`` names only inside
functions.
"""

from __future__ import annotations

from typing import Any

from ._jsonnum import is_int
from .decks import UnknownDeckError, get_extraction_deck
from .gen import GenError
from .gen_compose_port_search import _search_landing
from .gen_compose_routing import _pdk_family

#: Default cap on landing candidates evaluated per port (nearest-first).
DEFAULT_MAX_CANDIDATES = 20000

#: Upper bound on either side of ``access.via_array``.
_MAX_ARRAY_SIDE = 16

_LayerPair = tuple[int, int]


def _err(message: str) -> Exception:
    from .gen_compose import GenComposeError

    return GenComposeError(message)


def _parse_layer_pair(raw: Any, where: str) -> _LayerPair:
    if (
        not isinstance(raw, dict)
        or not is_int(raw.get("layer"))
        or not is_int(raw.get("datatype"))
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
    ``via_array`` (``[rows, cols]`` cuts per via hop), ``keepout_layers``, and the
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
        or not all(is_int(n) and n >= 1 for n in via_array)
    ):
        raise _err(f"{where}.via_array must be [rows, cols] of integers >= 1")
    if max(via_array) > _MAX_ARRAY_SIDE:
        raise _err(f"{where}.via_array sides must be <= {_MAX_ARRAY_SIDE}")
    grid_um = raw.get("grid_um")
    if grid_um is not None and (
        isinstance(grid_um, bool)
        or not isinstance(grid_um, (int, float))
        or grid_um <= 0
    ):
        raise _err(f"{where}.grid_um must be a number > 0")
    max_candidates = raw.get("max_candidates", DEFAULT_MAX_CANDIDATES)
    if not is_int(max_candidates) or max_candidates < 1:
        raise _err(f"{where}.max_candidates must be an integer >= 1")
    return {
        "up_to": up_to,
        "via_array": (int(via_array[0]), int(via_array[1])),
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
        if landing and landing["via_pitch_um"]:
            port["via_array"] = {
                "rows": landing["via_array"][0],
                "cols": landing["via_array"][1],
                "pitch_um": landing["via_pitch_um"],
            }
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
                    "via_array": landing["via_array"],
                    "candidates_evaluated": landing["candidates_evaluated"],
                    "truncated": landing["truncated"],
                },
            }
        )
    return ports, reports, warnings
