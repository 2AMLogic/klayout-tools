"""Resolve the requested top ports of a merged runset PEX leg by net identity."""

from __future__ import annotations

import re
import textwrap
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .pex_errors import PexError
from .pex_net_correspondence import preferred_renames
from .pex_netlist_text import _logical_lines


def _unresolved(name: str, reason: str) -> PexError:
    return PexError(f"merged PEX pin {name!r}: {reason}", code="merged_pin_unresolved")


def _resolve_ports(
    declared: frozenset[str],
    curated_nets: Sequence[Mapping[str, Any]],
    matches: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Bind each drawn pin to one curated cluster and one matched runset net."""
    drawn: dict[str, list[int]] = defaultdict(list)
    for net in curated_nets:
        if net["pin_index"] is None:
            continue
        labels = {point["text"] for point in net["label_positions_um"]}
        for name in labels & declared:
            drawn[name].append(net["net_id"])

    matched = {m["cluster_id"]: m for m in matches}
    ports: dict[int, dict[str, Any]] = {}
    for name in sorted(declared):
        ids = drawn[name]
        if not ids:
            raise _unresolved(name, "missing from the promoted curated nets")
        if len(ids) != 1 or ids[0] in ports:
            raise _unresolved(name, "ambiguous declared interface")
        if ids[0] not in matched:
            raise _unresolved(name, "no unique runset correspondence")
        ports[ids[0]] = {"declared": name, **matched[ids[0]]}
    return sorted(ports.values(), key=lambda p: p["curated"])


def _port_renames(
    ports: Sequence[Mapping[str, Any]],
    renames: dict[str, str],
    runset_nets: Sequence[str],
    global_nodes: set[str],
) -> None:
    """Reserve the curated SPICE tokens without merging nodes or renaming a global."""
    owners: dict[str, set[str]] = defaultdict(set)
    for node in runset_nets:
        owners[renames.get(node, node).casefold()].add(node)
    for p in ports:
        before, target = p["runset"], p["curated"]
        key = target.casefold()
        if not re.fullmatch(
            r"(?:[A-Za-z0-9_]|\\\$)(?:[A-Za-z0-9_$|:/!@~+\[\]-]|\\x[0-9a-fA-F]{2})*",
            target,
        ):
            raise _unresolved(
                p["declared"], f"unsupported SPICE port spelling {target!r}"
            )
        if owners[key] - {before}:
            raise _unresolved(p["declared"], f"ambiguous SPICE node name {target!r}")
        if key != before.casefold() and ({before.casefold(), key} & global_nodes):
            raise _unresolved(p["declared"], "renaming would change a global node")
        owners[key].add(before)
        if before != target:
            renames[before] = target


def _record_ports(
    report: dict[str, Any], ports: list[dict[str, Any]], renames: dict[str, str]
) -> None:
    """Publish the interface and its effective SPICE names with the merged netlist."""
    from .extract_report_canonicalize import _normalized_netlist_sha256

    positions = {p["runset_cluster_id"]: i for i, p in enumerate(ports)}
    for net in report["nets"]:
        net["name"] = renames.get(net["name"], net["name"])
        net["pin_index"] = positions.get(net["net_id"])
        net["pin"] = net["pin_index"] is not None
    report["nets"].sort(key=lambda n: n["name"])
    for device in report["devices"]:
        device["nets"] = {k: renames.get(v, v) for k, v in device["nets"].items()}
    report["pin_count"] = len(ports)
    report["netlist_normalized_sha256"] = _normalized_netlist_sha256(
        report["devices"], report["nets"]
    )


def bind_declared_ports(
    path: Path,
    declared: frozenset[str],
    curated_nets: Sequence[Mapping[str, Any]],
    corr: dict[str, Any],
    report: dict[str, Any],
    runset_nets: Sequence[str],
) -> dict[str, str]:
    """Validate top-port mappings before publishing any netlist bytes."""
    ports = _resolve_ports(declared, curated_nets, corr["matched"])
    renames = preferred_renames(corr["matched"], runset_nets)
    global_nodes = {"0"}
    for line in _logical_lines(path.read_text(encoding="utf-8")):
        words = line.split()
        if words and words[0].upper() == ".GLOBAL":
            global_nodes.update(word.casefold() for word in words[1:])
    _port_renames(ports, renames, runset_nets, global_nodes)
    corr["declared_ports"] = ports
    _record_ports(report, ports, renames)
    return renames


def replace_port_header(
    lines: list[str], start: int, top: str, ports: Sequence[Mapping[str, Any]]
) -> None:
    """Replace only the top circuit's formal pins, preserving parameter defaults."""
    last = start + 1
    while last < len(lines) and lines[last].lstrip().startswith("+"):
        last += 1
    (header,) = _logical_lines("".join(lines[start:last]))
    match = re.search(r"(?:^|\s)(params:|[^\s=]+=)", header, re.IGNORECASE)
    params = header[match.start() :].strip() if match else ""
    tokens = " ".join(p["curated"] for p in ports)
    prefix = f".SUBCKT {top}"
    wrapped = textwrap.wrap(
        tokens,
        width=80,
        initial_indent=prefix + " ",
        subsequent_indent="+ ",
        break_long_words=False,
        break_on_hyphens=False,
    ) or [prefix]
    if params:
        wrapped.append("+ " + params)
    lines[start:last] = [line + "\n" for line in wrapped]
