"""Translate a labelled BEOL layout region and a resolved PDK stack to Geode v1."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import klayout.db as kdb

from ._digest import sha256_file_required
from ._em_geometry import ConductorLayer
from ._geode import EmError
from ._jsonnum import is_finite_number
from ._layout import clip_box, load_layout, resolve_top_cell
from .decks import get_extraction_deck
from .pdk_stackup import PdkNotFoundError, PdkStackupError, stackup

LayerPair = tuple[int, int]
LayerStack = dict[LayerPair, dict[str, Any]]
NetGeometry = dict[LayerPair, kdb.Region]


@dataclass
class LayoutInput:
    document: dict[str, Any]
    terminals: list[dict[str, str]]
    environment: dict[str, Any]
    diagnostics: list[str]


def _region_window(region: dict[str, Any], dbu: float) -> kdb.Region | None:
    bbox = region.get("bbox_um")
    if bbox is None:
        return None
    if not (
        isinstance(bbox, list)
        and len(bbox) == 4
        and all(is_finite_number(v) and -(2**31) <= v / dbu < 2**31 for v in bbox)
        and bbox[0] < bbox[2]
        and bbox[1] < bbox[3]
    ):
        raise EmError(
            "region.bbox_um must be a positive-area [left, bottom, right, top] "
            "within the layout coordinate range (micrometres)"
        )
    return kdb.Region(clip_box(kdb, tuple(bbox), dbu))


def _read_layers(
    layout: kdb.Layout, cell: kdb.Cell, levels: LayerStack, family: str
) -> tuple[kdb.LayoutToNetlist, dict[LayerPair, ConductorLayer], dict[LayerPair, int]]:
    deck = get_extraction_deck(family)
    labels = dict(zip(deck.metals, deck.metal_labels, strict=True))
    layers = {
        pair: ConductorLayer.read(layout, cell, pair, labels.get(pair))
        for pair in levels
    }
    graph = kdb.LayoutToNetlist(cell.name, layout.dbu)
    indices = {
        pair: graph.register(layer.shapes, str(pair)) for pair, layer in layers.items()
    }
    for layer in layers.values():
        graph.connect(layer.shapes)
    for i, via in enumerate(deck.vias):
        for metal in deck.metals[i : i + 2]:
            if via in layers and metal in layers:
                graph.connect(layers[via].shapes, layers[metal].shapes)
    graph.extract_netlist()
    return graph, layers, indices


def _selected_nets(
    layout: kdb.Layout,
    cell: kdb.Cell,
    levels: LayerStack,
    family: str,
    window: kdb.Region | None,
) -> dict[str, NetGeometry]:
    graph, layers, indices = _read_layers(layout, cell, levels, family)
    circuit = graph.netlist().circuit_by_name(cell.name)
    nets = {}
    for net in circuit.each_net() if circuit else ():
        parts = {}
        labels: set[str] = set()
        for pair, layer in layers.items():
            full = graph.polygons_of_net(net, indices[pair])
            if full.is_empty():
                continue
            labels.update(name for name in layer.names_on(full) if name.strip())
            clipped = full if window is None else full & window
            if not clipped.is_empty():
                parts[pair] = clipped
        if not parts:
            continue
        if not labels:
            box = next(iter(parts.values())).bbox()
            raise EmError(f"unlabeled conductor in cell {cell.name!r} at {box} (dbu)")
        if len(labels) != 1:
            raise EmError(
                f"ambiguous terminal in cell {cell.name!r}: labels {sorted(labels)}"
            )
        name = labels.pop()
        if name in nets:
            raise EmError(
                f"ambiguous terminal {name!r}: label names disconnected conductors"
            )
        nets[name] = parts
    if not nets:
        raise EmError("layout region contains no stackup conductors")
    return nets


def _rings(shapes: kdb.Region, dbu: float) -> Iterator[list[list[float]]]:
    for polygon in shapes.each_merged():
        if not polygon.is_rectilinear():
            raise EmError("geode mesh requires rectilinear conductor polygons")
        # Geode v1 has no interior rings. Tile a rectilinear ring with
        # rectangles on the same net, preserving the opening.
        if polygon.holes():
            rings = [
                piece.each_point()
                for piece in polygon.decompose_trapezoids(kdb.Polygon.TD_htrapezoids)
            ]
        else:
            rings = [polygon.each_point_hull()]
        for points in rings:
            yield [[p.x * dbu, p.y * dbu] for p in points]


def _slabs(stack: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    slabs = []
    diagnostics = []
    for slab in stack["dielectrics"]:
        if slab["permittivity"] is None:
            diagnostics.append(
                f"Grounded top boundary at {slab['z0_um']} um: {slab['name']} "
                "and higher layers omitted because permittivity is unknown."
            )
            break
        slabs.append(
            {
                "name": slab["name"],
                "z_bottom": slab["z0_um"],
                "thickness": slab["thickness_um"],
                "eps_r": [slab["permittivity"], 0.0],
            }
        )
    if not slabs:
        raise EmError("stackup has no dielectric slabs with known permittivity")
    return slabs, diagnostics


def _conductors(
    levels: LayerStack,
    nets: dict[str, NetGeometry],
    surfaces: dict[str, str],
    top: float,
    dbu: float,
) -> list[dict[str, Any]]:
    conductors = []
    for pair, entry in levels.items():
        polygons = [
            {"net": surface, "outer": ring}
            for name, surface in surfaces.items()
            if pair in nets[name]
            for ring in _rings(nets[name][pair], dbu)
        ]
        if polygons:
            if entry["z1_um"] >= top or math.isclose(entry["z1_um"], top):
                raise EmError(
                    f"{entry['name']} reaches the grounded top of the known "
                    f"dielectric stack ({top:g} um)"
                )
            conductors.append(
                {
                    "name": entry["name"],
                    "z_bottom": entry["z0_um"],
                    "thickness": entry["thickness_um"],
                    "polygons": polygons,
                }
            )
    return conductors


def prepare_layout(request: dict[str, Any], request_dir: str) -> LayoutInput:
    for key in ("layout", "stackup"):
        if not isinstance(request.get(key), str) or not request[key].strip():
            raise EmError(f"layout requests require a non-empty '{key}' string")
    region = request.get("region")
    if not isinstance(region, dict) or set(region) - {"cell", "bbox_um"}:
        raise EmError("region must be an object with optional cell and bbox_um")
    cell_name = region.get("cell")
    if cell_name is not None and (not isinstance(cell_name, str) or not cell_name):
        raise EmError("region.cell must name a cell")
    path = str(Path(request_dir) / request["layout"])
    layout = load_layout(path, EmError)
    cell = resolve_top_cell(layout, cell_name, EmError, path=path)
    try:
        stack = stackup(request["stackup"])
    except (PdkNotFoundError, PdkStackupError) as exc:
        raise EmError(str(exc)) from exc
    levels: LayerStack = {}
    for entry in stack["conductors"]:
        layer, datatype = entry["gds_layer"].split("/")
        levels[int(layer), int(datatype)] = entry
    nets = _selected_nets(
        layout, cell, levels, stack["family"], _region_window(region, layout.dbu)
    )
    # Physical group names have a restricted alphabet. Preserve arbitrary GDS
    # label text (including bus indices) in the caller-facing terminal names.
    surfaces = {name: f"net_{i}" for i, name in enumerate(sorted(nets))}
    ground = request.get("ground", [])
    if not isinstance(ground, list) or not all(
        isinstance(n, str) and n in nets for n in ground
    ):
        raise EmError(
            "layout ground must be an array of GDS labels in the selected region"
        )
    if len(set(ground)) != len(ground) or len(ground) == len(nets):
        raise EmError("ground labels must be unique and leave at least one terminal")
    slabs, diagnostics = _slabs(stack)
    height = slabs[-1]["z_bottom"] + slabs[-1]["thickness"]
    conductors = _conductors(levels, nets, surfaces, height, layout.dbu)
    document = {
        "schema_version": 1,
        "length_unit_m": 1e-6,
        "dielectrics": slabs,
        "conductors": conductors,
        "ground": [surfaces[name] for name in ground],
        "margin": height,
        "mesh": {
            "size_max": height / 2,
            "size_conductor": min(c["thickness"] for c in conductors),
        },
    }
    stack_bytes = json.dumps(stack, sort_keys=True, separators=(",", ":")).encode()
    return LayoutInput(
        document,
        [
            {"name": name, "surface": surface}
            for name, surface in surfaces.items()
            if name not in ground
        ],
        {
            "layout_sha256": sha256_file_required(path),
            "stackup": stack["pdk"],
            "stackup_sha256": hashlib.sha256(stack_bytes).hexdigest(),
        },
        diagnostics,
    )
