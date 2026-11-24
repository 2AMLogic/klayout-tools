"""Resolve route trees on separate pads of an abstracted pin after probing."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import klayout.db as kdb


class AbstractPinJoins:
    """Remember extracted cluster IDs so no probe refers to an absorbed net.

    Issue #7561: a group also records which of its candidates **landed** on
    the access point's own declared conductor (the point's role-layer probe
    hit, versus the cross-layer fallback that takes whatever overlapping
    conductor is nearby). A join that would absorb an anonymous routed net
    into a differently named one -- silently shorting a driven signal into
    a rail -- is only allowed when both sides of the pair carry that
    landing evidence; otherwise the pair is kept separate and reported.
    """

    def __init__(self) -> None:
        self.groups: list[tuple[str, str, list[int], frozenset[int]]] = []
        self.nets: dict[int, kdb.Net] = {}
        self.parents: dict[int, int] = {}
        self.def_names: dict[int, set[str]] = {}

    def record(
        self,
        instance: str,
        pin: str,
        winner: kdb.Net,
        candidates: dict[int, kdb.Net],
        landed: frozenset[int] | set[int] = frozenset(),
    ) -> None:
        """Remember one pin's candidate nets and which of their probes
        resolved on the access point's own declared conductor (``landed``,
        a subset of ``candidates``' keys). An empty ``landed`` keeps the
        pre-#7561 record shape: joins still happen for equally named or
        mutually anonymous pairs, and mixed pairs are kept separate."""
        if len(candidates) > 1:
            self.nets.update(candidates)
            others = [key for key in candidates if key != winner.cluster_id]
            self.groups.append(
                (instance, pin, [winner.cluster_id, *others], frozenset(landed))
            )

    def read_def_names(
        self,
        l2n: kdb.LayoutToNetlist,
        layers: list[tuple[str, kdb.Region]],
        probes: dict[str, list[tuple[int, kdb.Point]]],
    ) -> None:
        """Recover DEF identity even when a different island owns the SPICE name."""
        if not self.groups:
            return
        for name, points in probes.items():
            for metal, point in points:
                net = l2n.probe_net(layers[metal][1], point)
                if net is not None and net.cluster_id in self.nets:
                    self.def_names.setdefault(net.cluster_id, set()).add(name)

    def _names(self, key: int) -> set[str]:
        names = self.def_names.get(key, set())
        name = self.nets[key].name
        return names | {name} if name else names

    def _root(self, key: int) -> int:
        path = []
        while key in self.parents:
            path.append(key)
            key = self.parents[key]
        for item in path:
            self.parents[item] = key
        return key

    def apply(self, circuit: kdb.Circuit) -> list[str]:
        # A single bare pad with no other pin, device or name has no route
        # tree in the emitted netlist. A common candidate of two different
        # pin groups can connect the groups even when neither chose it first.
        uses = Counter(key for _, _, keys, _landed in self.groups for key in keys)
        routed = {
            key
            for key, net in self.nets.items()
            if self._names(key)
            or net.subcircuit_pin_count()
            or net.terminal_count()
            or uses[key] > 1
        }
        joined: list[str] = []
        conflicts: list[str] = []
        unsupported: list[str] = []
        for instance, pin, keys, landed in self.groups:
            root = self._root(keys[0])
            for key in keys[1:]:
                other = self._root(key)
                if root == other or key not in routed:
                    continue
                winner, loser = self.nets[root], self.nets[other]
                pair = (
                    f"{instance}.{pin}: {winner.expanded_name()} and "
                    f"{loser.expanded_name()}"
                )
                root_names = self._names(root)
                other_names = self._names(other)
                names = root_names | other_names
                if len(names) > 1:
                    conflicts.append(f"{instance}.{pin}: {', '.join(sorted(names))}")
                    continue
                # One named and one anonymous side (#7561): without a
                # declared-conductor landing on both sides the anonymous net
                # is only known to overlap the pin's footprint, which cannot
                # justify absorbing it into the named net. Decided per pair
                # before any join mutates the circuit or the parent map, so
                # a skipped pair leaves no join entry and no indirect path
                # for a later group to merge through.
                if bool(root_names) != bool(other_names) and not (
                    keys[0] in landed and key in landed
                ):
                    unsupported.append(pair)
                    continue
                circuit.join_nets(winner, loser)
                self.parents[other] = root
                self.def_names[root] = names
                joined.append(pair)
        warnings = []
        if joined:
            warnings.append(
                "--abstract-cells joined routed pads of the same pin: "
                + "; ".join(joined)
            )
        if conflicts:
            warnings.append(
                "--abstract-cells pin pads have conflicting net names, kept separate: "
                + "; ".join(conflicts)
            )
        if unsupported:
            warnings.append(
                "--abstract-cells pin pads without connection evidence, "
                "kept separate: " + "; ".join(unsupported)
            )
        return warnings


def tied_instances(
    wired: list[tuple[kdb.SubCircuit, dict[str, int]]],
) -> list[tuple[str, str, str, list[str], int]]:
    """Read final connectivity after KLayout transfers absorbed nets' pins."""
    tied = []
    for instance, pins in wired:
        by_net: dict[str, list[str]] = {}
        for name, pin_id in pins.items():
            net = instance.net_for_pin(pin_id)
            by_net.setdefault(net.expanded_name(), []).append(name)
        for net_name, names in by_net.items():
            if len(names) > 1:
                tied.append(
                    (
                        instance.name,
                        instance.circuit_ref().name,
                        net_name,
                        names,
                        len(pins),
                    )
                )
    return tied
