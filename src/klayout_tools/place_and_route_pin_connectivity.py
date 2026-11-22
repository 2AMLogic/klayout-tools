"""Compare routed DEF terminal groups with conductor islands in the final GDS."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from klayout_tools._layout import region
from klayout_tools.decks import (
    ExtractionDeck,
    UnknownExtractionDeckError,
    get_extraction_deck,
)
from klayout_tools.extract_abstract import stream_metal_pin_points
from klayout_tools.extract_spef import DefName, DefNetConnections, def_net_instance_pins
from klayout_tools.lef_header import read_lef_header
from klayout_tools.place_and_route_reports import (
    build_congestion,
    detailed_route_convergence,
    parse_placement_routability,
)

if TYPE_CHECKING:
    import klayout.db as kdb

Terminal = tuple[str | None, str]


class _Stream:
    """A flat electrical graph for wires and vias, indexed by physical terminals."""

    def __init__(self, path: str, top_name: str, deck: ExtractionDeck):
        import klayout.db as kdb

        self.layout = kdb.Layout()
        self.layout.read(path)
        top = self.layout.cell(top_name)
        if top is None:
            raise ValueError(f"top cell {top_name} is absent from the stream")
        self.deck = deck
        self.graph = kdb.LayoutToNetlist(top.name, self.layout.dbu)
        self.metals = [region(self.layout, top, layer) for layer in deck.metals]
        for index, metal in enumerate(self.metals):
            self.graph.register(metal, f"metal{index}")
            self.graph.connect(metal)
        for index, layer in enumerate(deck.vias):
            via = region(self.layout, top, layer)
            self.graph.connect(via)
            self.graph.connect(self.metals[index], via)
            self.graph.connect(via, self.metals[index + 1])
        self.graph.extract_netlist()
        self.instances: dict[str, kdb.Instance | None] = {}
        for ref in top.each_inst():
            name = ref.property(1)  # instance identifier retained by the DEF/GDS merge
            if name is not None:
                self.instances[name] = ref if name not in self.instances else None
        self.masters: dict[str, dict[str, list[tuple[kdb.Point, int]]]] = {}
        self.ports: dict[str, list[tuple[kdb.Point, int]]] = defaultdict(list)
        for index, label_layer in enumerate(deck.metal_labels):
            label = self.layout.find_layer(*label_layer) if label_layer else None
            if label is not None:
                for text in kdb.Texts(top.shapes(label)).each():
                    self.ports[DefName(text.string)].append(
                        (kdb.Point(text.x, text.y), index)
                    )

    def terminal(self, inst: str | None, pin: str) -> set[int]:
        if inst is None:
            points = self.ports.get(pin, [])
        else:
            ref = self.instances.get(inst)
            if ref is None or ref.is_regular_array():
                return set()
            cell = ref.cell
            if cell.name not in self.masters:
                self.masters[cell.name] = stream_metal_pin_points(
                    self.layout, cell, self.deck
                )
            points = [
                (ref.cplx_trans * point, layer)
                for point, layer in self.masters[cell.name].get(pin, [])
            ]
        islands = set()
        for point, layer in points:
            net = self.graph.probe_net(self.metals[layer], point)
            if net is not None:
                islands.add(net.cluster_id)
        # Repeated top text may be a route annotation. A unique physical island
        # identifies the port unambiguously. Do not use text to bridge islands.
        return set() if inst is None and len(islands) != 1 else islands


def _components(access: dict[Terminal, set[int]]) -> list[list[Terminal]]:
    """Union all metal islands reachable through each proven physical pin."""
    parent: dict[int, int] = {}

    def root(i: int) -> int:
        while i != parent.setdefault(i, i):
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for islands in access.values():
        head, *others = islands
        for other in others:
            parent[root(other)] = root(head)
    groups: dict[int, list[Terminal]] = defaultdict(list)
    for term, islands in access.items():
        groups[root(next(iter(islands)))].append(term)
    return sorted(groups.values(), key=lambda group: -len(group))


def _pin(term: Terminal) -> dict[str, str]:
    inst, pin = term
    return {"instance": inst, "pin": pin} if inst is not None else {"port": pin}


def _open_net(name: str, groups: list[list[Terminal]], drivers: set[Terminal]) -> dict:
    # With no identifiable driver, report all equally plausible islands.
    anchors = [group for group in groups if drivers.intersection(group)]
    anchor = anchors[0] if len(anchors) == 1 else None
    if not anchors and len(groups[0]) > len(groups[1]):
        anchor = groups[0]
    return {
        "net": name,
        "affected_pins": [
            _pin(t)
            for group in groups
            if group is not anchor
            for t in group
            if t[0] is not None
        ],
        "islands": [[_pin(t) for t in group] for group in groups],
    }


def _measure(
    stream: _Stream, refs: DefNetConnections, outputs: set[tuple[str, str]]
) -> dict:
    opens, missing = [], []
    checked = 0
    for name in sorted(refs.routed_signals):
        terminals: list[Terminal] = list(refs.get(name, ()))
        terminals.extend((None, pin) for pin in refs.port_pins.get(name, ()))
        if len(terminals) < 2:
            continue  # intentionally unused terminal, including tie-cell outputs
        access = {term: stream.terminal(*term) for term in terminals}
        absent = [term for term, islands in access.items() if not islands]
        if absent:
            missing.append({"net": name, "pins": [_pin(term) for term in absent]})
            continue
        checked += 1
        groups = _components(access)
        if len(groups) > 1:
            drivers = set()
            for inst, pin in terminals:
                ref = stream.instances.get(inst) if inst is not None else None
                if ref is not None and (ref.cell.name, pin) in outputs:
                    drivers.add((inst, pin))
            if not drivers:
                drivers = {term for term in terminals if term[0] is None}
            opens.append(_open_net(name, groups, drivers))
    result = {
        "status": "open" if opens else "checked",
        "checked_net_count": checked,
        "open_nets": opens,
    }
    if missing:
        result.update(
            status="unchecked",
            reason="No unambiguous stream conductor for intended terminals.",
            unresolved_nets=missing,
        )
    return result


def audit_pin_connectivity(
    def_path: str, gds_path: str, top: str, deck_name: str | None, cell_lef: str
) -> dict:
    """Return confirmed opens and evidence gaps separately."""
    try:
        if deck_name is None:
            raise ValueError("no conductor extraction deck for this cell library")
        refs = def_net_instance_pins(def_path)
        if not refs.complete:
            raise ValueError("missing or incomplete routed DEF NETS section")
        stream = _Stream(gds_path, top, get_extraction_deck(deck_name))
        outputs = {
            (macro["name"], pin["name"])
            for macro in read_lef_header(cell_lef)["macros"]
            for pin in macro["pins"]
            if pin["direction"] == "OUTPUT"
        }
        check = _measure(stream, refs, outputs)
        check["def_wired_signal_net_count"] = refs.wired_signal_count
        return check
    except (OSError, RuntimeError, ValueError, UnknownExtractionDeckError) as exc:
        return {
            "status": "unchecked",
            "reason": str(exc),
            "checked_net_count": 0,
            "open_nets": [],
        }


def apply_pin_connectivity(stage: dict, check: dict) -> bool:
    """Attach the physical result and return whether a confirmed open exists."""
    congestion = stage.get("congestion")
    if congestion is None:
        # Stage receipts written before the congestion block was introduced.
        stage["congestion"] = congestion = build_congestion(
            global_route=None,
            completeness=None,
            placement=parse_placement_routability(""),
            detailed=detailed_route_convergence({}),
        )
    comp = congestion["completeness"]
    total, unrouted = comp["signal_net_count"], comp["unrouted_net_count"]
    wired = check.get("def_wired_signal_net_count")
    if (
        wired is not None
        and total is not None
        and unrouted is not None
        and wired != total - unrouted
    ):
        check.update(
            status="unchecked",
            reason=(
                f"Routed DEF contains {wired} wired multi-terminal signal nets. "
                f"OpenDB counted {total - unrouted}."
            ),
        )
    comp["pin_connectivity"] = check
    reason_key = "completeness.partially_routed_net_count"
    if check["status"] == "unchecked":
        comp["partially_routed_net_count"] = None
        congestion["null_reasons"][reason_key] = check["reason"]
    else:
        comp["partially_routed_net_count"] = len(check["open_nets"])
        congestion["null_reasons"].pop(reason_key, None)
    return bool(check["open_nets"] or comp["unrouted_net_count"])
