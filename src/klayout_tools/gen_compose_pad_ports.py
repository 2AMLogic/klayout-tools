"""Locate electrically identified foreign port metal inside an endpoint block."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import klayout.db as kdb

from ._unionfind import UnionFind
from .gen_compose_base import _port_has_geometry

PortLocations = dict[tuple[int, int], list[tuple[str, float, float]]]


def endpoint_pad_ports(
    connectivity: list[dict[str, Any]], blocks: dict, offsets: dict
) -> list[dict[str, PortLocations]]:
    """Index other nets' declared access points by endpoint block and GDS layer.

    A shared block/port joins connectivity entries into one electrical net.
    Use the complete request so another net's port is visible before routing it.
    Port coordinates in normalized blocks already include their orientation.
    """
    nets = UnionFind()
    pins_by_block: dict[str, set[str]] = defaultdict(set)
    for index, entry in enumerate(connectivity):
        for pin in entry["pins"]:
            block, name = pin["block"], pin["port"]
            nets.union(index, (block, name))
            pins_by_block[block].add(name)

    result = []
    for index, entry in enumerate(connectivity):
        own_net = nets.find(index)
        endpoints = {}
        for block in {pin["block"] for pin in entry["pins"]}:
            foreign: PortLocations = defaultdict(list)
            for name in sorted(pins_by_block[block]):
                port = blocks[block]["ports"].get(name)
                if nets.find((block, name)) == own_net or not _port_has_geometry(port):
                    continue
                layer = port["layer"]
                foreign[layer["layer"], layer["datatype"]].append(
                    (
                        name,
                        port["x_um"] + offsets[block]["x"],
                        port["y_um"] + offsets[block]["y"],
                    )
                )
            endpoints[block] = foreign
        result.append(endpoints)
    return result


def contacted_port(
    metal: kdb.Region, dbu: float, ports: list[tuple[str, float, float]]
) -> str | None:
    """Name the foreign port anchoring one of the merged polygons hit by the pad."""
    for name, x, y in ports:
        point = kdb.Point(round(x / dbu), round(y / dbu))
        if any(polygon.inside(point) for polygon in metal.each()):
            return name
    return None
