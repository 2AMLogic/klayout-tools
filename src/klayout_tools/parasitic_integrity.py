"""Per-net parasitic RC network integrity checker (issue #2764).

A small **pure** module: it consumes a fully resolved description of the
parasitic network ``klt extract --parasitics``/``klt pex`` emitted (nodes,
resistors, inductors, capacitors, per-net required terminals and declared
capacitance -- :class:`ParasiticNetworkRecord`) and returns the per-net
integrity diagnostics attached to the JSON report. It never touches klayout,
never repairs the network, and never changes any verdict -- the diagnostics
are additive status values an agent branches on
(``docs/cli/extract.md``'s "Parasitic network integrity" section).

**What this checks -- and what it cannot.** The checker validates the
*emitted RC model* (the ``R``/``C``/``L`` cards injection actually wrote and
the bookkeeping that produced them): opens in the distributed-ladder
connectivity, floating islands, orphan nodes, malformed resistor values,
cross-net short candidates, and declared-vs-emitted ground capacitance. It
cannot certify geometric opens or shorts -- a layout-level discontinuity the
extractor measured faithfully is a *good* model of a bad layout, and this
checker's ``pass`` says nothing about it.

**Node identity.** Nodes are opaque ``uid`` integers; each carries a display
``name`` (the netlist spelling) and an ``owner_net_id`` -- the
circuit-scoped identity (``Net.cluster_id``) of the original net the node
belongs to, or :data:`GROUND_OWNER` (``None``) for the shared
substrate/global ground. Two nets may share a display name (issue #765);
owners, not names, decide whether an edge is within-net or cross-net, so
same-named nets keep distinct identities.

**Connectivity model.** Resistors and inductors establish DC connectivity;
capacitors never do (an open at DC is not bridged by a coupling cap). A
legitimate capacitor endpoint therefore counts as *attached* for the
orphan-node check but bridges nothing for the open/island checks.

All numeric detail values pass through :func:`_finite_or_none`, so a
malformed input value (``NaN``/``±Inf``) is diagnosed, never propagated --
the emitted block serializes as strict finite JSON (no ``NaN``/``Infinity``
literals) even for non-finite input. Detail lists are deterministically
ordered and capped at :data:`MAX_DETAIL_ITEMS` entries with
``details_truncated`` set; the companion ``count`` fields always carry the
complete count.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

#: Cross-net short-candidate threshold (ohms). An injected parasitic
#: resistor whose two endpoints resolve to *different* original nets at or
#: below this value is reported as a short candidate. The comparison is
#: **inclusive** (``<=``): a resistor measuring exactly the threshold is a
#: candidate. 1 ohm sits far above the extractor's own minimum series
#: resistance (``1e-3`` ohm, the clamp every *within-net* segment receives)
#: and far below any intentionally emitted cross-net resistance -- the
#: substrate DC tie's ``1e12`` ohm shunt, which is excluded from the check
#: entirely as an intentional tie. Within-net resistors are never short
#: candidates regardless of value, and functional (drawn) devices are not
#: part of the checked network at all.
DEFAULT_SHORT_THRESHOLD_OHM = 1.0

#: Absolute ground-capacitance reconciliation tolerance (fF). The emitted
#: capacitor values ride through a Farad round-trip and a per-node split, so
#: the sum can differ from the declared total by floating-point noise; the
#: report itself rounds capacitance to 6 decimal places, so anything below
#: ``1e-6`` fF is below the report's own resolution.
CAP_TOLERANCE_ABS_FF = 1e-6

#: Relative ground-capacitance reconciliation tolerance. ``pass`` requires
#: ``|computed - declared| <= max(CAP_TOLERANCE_ABS_FF, CAP_TOLERANCE_REL *
#: |declared|)`` -- the relative term keeps large-picofarad nets from
#: tripping on round-trip noise that scales with the value.
CAP_TOLERANCE_REL = 1e-9

#: Maximum entries in any ``details`` list. Counts stay complete even when
#: the list truncates (``details_truncated: true``) -- bounded output, full
#: accounting.
MAX_DETAIL_ITEMS = 20

#: Owner sentinel for nodes shared by every net: the substrate/global
#: ground (and SPICE's node ``0``). ``None`` rather than a string so the
#: owner slot keeps the ``int`` net-id type for every real net.
GROUND_OWNER = None


@dataclass(frozen=True)
class IntegrityNode:
    """One node of the emitted parasitic network.

    ``uid`` is the record-unique identity every element endpoint
    references; ``name`` is the netlist spelling detail lists display;
    ``owner_net_id`` is the original net (``Net.cluster_id``) this node
    belongs to, or :data:`GROUND_OWNER` for the shared ground.
    """

    uid: int
    name: str
    owner_net_id: int | None


@dataclass(frozen=True)
class IntegrityResistor:
    """One injected parasitic resistor (series or shunt).

    ``owner_net_id`` is the original net whose model emitted this resistor;
    ``intentional_dc_tie`` marks the substrate DC-reference shunts
    (``R<net>_dctie <net> 0 1e12``, issue #1263), which are excluded from
    every resistor check -- they are intentional cross-net (net-to-ground)
    elements, not bookkeeping defects.
    """

    name: str
    node_a: int
    node_b: int
    resistance_ohm: float
    owner_net_id: int | None
    intentional_dc_tie: bool = False


@dataclass(frozen=True)
class IntegrityInductor:
    """One injected series inductor (the ``--mom-rlc-net`` splice).

    Inductors conduct at DC, so they join the connectivity graph exactly
    like resistors; only the ``--mom-rlc-net`` splice emits any.
    """

    name: str
    node_a: int
    node_b: int
    owner_net_id: int | None


@dataclass(frozen=True)
class IntegrityCapacitor:
    """One injected capacitor.

    ``kind`` is ``"ground"`` (net-to-ground; the only capacitors the
    declared-total reconciliation sums -- each counted exactly once) or
    ``"coupling"`` (net-to-net; kept separate from the ground
    reconciliation and never summed into it). ``owner_net_id`` is the
    emitting net for ground caps, :data:`GROUND_OWNER` for coupling caps
    (whose endpoints already carry their own owners).
    """

    name: str
    node_a: int
    node_b: int
    capacitance_ff: float
    kind: str
    owner_net_id: int | None


@dataclass(frozen=True)
class IntegrityNet:
    """The per-net declaration the per-net checks need.

    ``required_terminal_uids`` are the emitted terminal/pin nodes
    (injection's per-terminal leg nodes, in emission order) -- the nodes
    the open check requires to end up in one connected component.
    ``declared_capacitance_ff`` is the net's declared total ground
    capacitance (the ``parasitics.nets[].capacitance_ff`` value). A net
    whose ground-capacitance total was produced by a replacement model
    that does not emit comparable ground capacitors sets
    ``capacitance_checkable=False`` with a ``capacitance_reason`` -- the
    reconciliation then reports ``not_applicable`` instead of comparing
    unequal quantities. (The current MoM substitution stays checkable: it
    rewrites the declared entry *before* injection, so declared and
    emitted still describe the same capacitance.) ``network_dropped``
    (``--parasitic-variant``, issue #3056) marks a net whose series R and
    ground C were both deliberately omitted: its lone node then has no
    emitted element by design, so it is not reported as an orphan.
    """

    net_id: int
    net: str
    rc_model: str
    required_terminal_uids: tuple[int, ...] = ()
    declared_capacitance_ff: float = 0.0
    capacitance_checkable: bool = True
    capacitance_reason: str | None = None
    network_dropped: bool = False


@dataclass
class ParasiticNetworkRecord:
    """Everything the checker needs about one extraction's emitted network.

    Built once per ``--parasitics`` run by :func:`klayout_tools.
    extract_parasitics._inject_parasitics` (which knows every node it
    minted and every device it created) and consumed exactly once by
    :func:`check_parasitic_network`.
    """

    nodes: list[IntegrityNode] = field(default_factory=list)
    resistors: list[IntegrityResistor] = field(default_factory=list)
    inductors: list[IntegrityInductor] = field(default_factory=list)
    capacitors: list[IntegrityCapacitor] = field(default_factory=list)
    nets: list[IntegrityNet] = field(default_factory=list)
    short_threshold_ohm: float = DEFAULT_SHORT_THRESHOLD_OHM
    cap_tolerance_abs_ff: float = CAP_TOLERANCE_ABS_FF
    cap_tolerance_rel: float = CAP_TOLERANCE_REL
    max_detail_items: int = MAX_DETAIL_ITEMS


class IntegrityRecordBuilder:
    """Accumulates a :class:`ParasiticNetworkRecord` during injection.

    Lives here rather than as inline list-appends in
    :func:`klayout_tools.extract_parasitics._inject_parasitics` so the
    wiring adds no branches to that (complexity-baselined) function: every
    emission site is one straight-line builder call. Uids are assigned
    sequentially in registration order -- deterministic, because the
    injection pass itself is deterministic.
    """

    def __init__(self) -> None:
        self._nodes: list[IntegrityNode] = []
        self._resistors: list[IntegrityResistor] = []
        self._inductors: list[IntegrityInductor] = []
        self._capacitors: list[IntegrityCapacitor] = []
        self._nets: list[IntegrityNet] = []

    def add_node(self, name: str, owner_net_id: int | None) -> int:
        """Register ``name`` (netlist spelling) as owned by
        ``owner_net_id`` and return its uid."""
        uid = len(self._nodes)
        self._nodes.append(IntegrityNode(uid=uid, name=name, owner_net_id=owner_net_id))
        return uid

    def add_resistor(
        self,
        name: str,
        node_a: int,
        node_b: int,
        resistance_ohm: float,
        owner_net_id: int | None,
        *,
        intentional_dc_tie: bool = False,
    ) -> None:
        self._resistors.append(
            IntegrityResistor(
                name=name,
                node_a=node_a,
                node_b=node_b,
                resistance_ohm=resistance_ohm,
                owner_net_id=owner_net_id,
                intentional_dc_tie=intentional_dc_tie,
            )
        )

    def add_inductor(
        self, name: str, node_a: int, node_b: int, owner_net_id: int | None
    ) -> None:
        self._inductors.append(
            IntegrityInductor(
                name=name, node_a=node_a, node_b=node_b, owner_net_id=owner_net_id
            )
        )

    def add_ground_capacitor(
        self,
        name: str,
        node: int,
        ground_uid: int,
        capacitance_ff: float,
        owner_net_id: int | None,
    ) -> None:
        """One net-to-ground capacitor; ``node`` is the net-side endpoint
        and ``ground_uid`` the shared ground node's uid."""
        self._capacitors.append(
            IntegrityCapacitor(
                name=name,
                node_a=node,
                node_b=ground_uid,
                capacitance_ff=capacitance_ff,
                kind="ground",
                owner_net_id=owner_net_id,
            )
        )

    def add_coupling_capacitor(
        self, name: str, node_a: int, node_b: int, capacitance_ff: float
    ) -> None:
        self._capacitors.append(
            IntegrityCapacitor(
                name=name,
                node_a=node_a,
                node_b=node_b,
                capacitance_ff=capacitance_ff,
                kind="coupling",
                owner_net_id=GROUND_OWNER,
            )
        )

    def add_net(
        self,
        net_id: int,
        net: str,
        rc_model: str,
        required_terminal_uids: tuple[int, ...],
        declared_capacitance_ff: float,
        *,
        capacitance_checkable: bool = True,
        capacitance_reason: str | None = None,
        network_dropped: bool = False,
    ) -> None:
        self._nets.append(
            IntegrityNet(
                net_id=net_id,
                net=net,
                rc_model=rc_model,
                required_terminal_uids=tuple(required_terminal_uids),
                declared_capacitance_ff=declared_capacitance_ff,
                capacitance_checkable=capacitance_checkable,
                capacitance_reason=capacitance_reason,
                network_dropped=network_dropped,
            )
        )

    def build(self, record: ParasiticNetworkRecord) -> ParasiticNetworkRecord:
        """Fill ``record`` (caller-supplied, so the caller controls the
        threshold/tolerance fields) with the accumulated elements."""
        record.nodes.extend(self._nodes)
        record.resistors.extend(self._resistors)
        record.inductors.extend(self._inductors)
        record.capacitors.extend(self._capacitors)
        record.nets.extend(self._nets)
        return record


def _finite_or_none(value: float, digits: int | None = None) -> float | None:
    """Pass a finite ``value`` through (optionally rounded), collapsing any
    non-finite input to ``None`` -- the only shape a strict-JSON encoder
    accepts for "this number is garbage".
    """
    if not math.isfinite(value):
        return None
    if digits is not None:
        return round(value, digits)
    return value


def _cap_details(
    details: list[dict[str, Any]], max_items: int
) -> tuple[list[dict[str, Any]], bool]:
    """Deterministically cap ``details`` at ``max_items`` entries, returning
    ``(capped, truncated)``. The caller keeps the full count separately."""
    return details[:max_items], len(details) > max_items


def _node_name(record: ParasiticNetworkRecord, uid: int) -> str:
    """The display name for ``uid``, or a placeholder when the uid is out
    of range (a malformed record) -- diagnostics name what they can and
    never crash on what they can't."""
    if 0 <= uid < len(record.nodes):
        return record.nodes[uid].name
    return f"<unknown uid {uid}>"


def _endpoints_known(record: ParasiticNetworkRecord, uid_a: int, uid_b: int) -> bool:
    """Whether both endpoint uids resolve to registered nodes. An element
    touching an unregistered uid is malformed bookkeeping; the resistor
    checks skip it (its endpoints cannot be named or owned) instead of
    crashing."""
    count = len(record.nodes)
    return 0 <= uid_a < count and 0 <= uid_b < count


def _union_find_connectivity(
    uids: list[int], edges: list[tuple[int, int]]
) -> dict[int, int]:
    """Union-find over ``uids`` joined by undirected ``edges``; returns a
    ``uid -> representative`` map. Only endpoints listed in ``uids`` are
    tracked (an edge touching an unlisted uid is ignored -- callers pass
    the node set of one net, and edges are pre-filtered to it).
    """
    parent = {uid: uid for uid in uids}

    def find(uid: int) -> int:
        root = uid
        while parent[root] != root:
            root = parent[root]
        while parent[uid] != root:
            parent[uid], uid = root, parent[uid]
        return root

    for a, b in edges:
        if a in parent and b in parent:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
    return {uid: find(uid) for uid in uids}


@dataclass
class _RecordIndex:
    """One-pass index over a :class:`ParasiticNetworkRecord`, so every
    per-net check reads only its own net's slice of the record.

    Built once per :func:`check_parasitic_network` call: without it every
    net rescanned every node and element (``O(nets x elements)`` -- seconds
    at a few thousand nets, on every ``--parasitics`` run). Each bucket
    preserves record order, so the per-net results are identical to the
    full-scan formulation they replace.
    """

    #: ``owner_net_id -> [uid, ...]`` in node-registration order.
    nodes_by_owner: dict[int | None, list[int]]
    #: ``owner_net_id -> [(node_a, node_b), ...]``: the DC-conducting
    #: elements (resistors minus substrate DC ties, then inductors) whose
    #: *both* endpoints that owner's node universe contains -- regardless
    #: of which net emitted the element.
    dc_edges_by_owner: dict[int | None, list[tuple[int, int]]]
    #: ``owner_net_id -> [resistor, ...]``: resistors by *emitting* net.
    resistors_by_owner: dict[int | None, list[IntegrityResistor]]
    #: ``owner_net_id -> [capacitance_ff, ...]``: ground caps by emitting net.
    ground_caps_by_owner: dict[int | None, list[float]]
    #: Every uid any R/L/C with two registered endpoints touches.
    attached: set[int]


def _bucket_dc_edge(
    edges_by_owner: dict[int | None, list[tuple[int, int]]],
    owners_by_uid: dict[int, list[int | None]],
    node_a: int,
    node_b: int,
) -> None:
    """File the DC edge ``(node_a, node_b)`` under every owner whose node
    universe contains *both* endpoints (exactly the nets whose
    connectivity it joins); an edge touching an unregistered uid, or
    spanning two owners, joins no net's graph."""
    owners_b = owners_by_uid.get(node_b, ())
    for owner in owners_by_uid.get(node_a, ()):
        if owner in owners_b:
            edges_by_owner.setdefault(owner, []).append((node_a, node_b))


def _mark_attached(
    attached: set[int], record: ParasiticNetworkRecord, node_a: int, node_b: int
) -> None:
    """Add both endpoints of one R/L/C to ``attached`` when both resolve to
    registered nodes (a malformed endpoint attaches nothing)."""
    if _endpoints_known(record, node_a, node_b):
        attached.add(node_a)
        attached.add(node_b)


def _index_record(record: ParasiticNetworkRecord) -> _RecordIndex:
    """Bucket ``record`` by owner in a single pass over each element list
    (see :class:`_RecordIndex`)."""
    nodes_by_owner: dict[int | None, list[int]] = {}
    # A malformed record may register one uid under several owners; keeping
    # every owner per uid keeps the per-owner set semantics exact.
    owners_by_uid: dict[int, list[int | None]] = {}
    for n in record.nodes:
        nodes_by_owner.setdefault(n.owner_net_id, []).append(n.uid)
        owners_by_uid.setdefault(n.uid, []).append(n.owner_net_id)

    dc_edges_by_owner: dict[int | None, list[tuple[int, int]]] = {}
    attached: set[int] = set()
    resistors_by_owner: dict[int | None, list[IntegrityResistor]] = {}
    for r in record.resistors:
        resistors_by_owner.setdefault(r.owner_net_id, []).append(r)
        if not r.intentional_dc_tie:
            _bucket_dc_edge(dc_edges_by_owner, owners_by_uid, r.node_a, r.node_b)
        _mark_attached(attached, record, r.node_a, r.node_b)
    for i in record.inductors:
        _bucket_dc_edge(dc_edges_by_owner, owners_by_uid, i.node_a, i.node_b)
        _mark_attached(attached, record, i.node_a, i.node_b)
    ground_caps_by_owner: dict[int | None, list[float]] = {}
    for c in record.capacitors:
        if c.kind == "ground":
            ground_caps_by_owner.setdefault(c.owner_net_id, []).append(c.capacitance_ff)
        _mark_attached(attached, record, c.node_a, c.node_b)
    return _RecordIndex(
        nodes_by_owner=nodes_by_owner,
        dc_edges_by_owner=dc_edges_by_owner,
        resistors_by_owner=resistors_by_owner,
        ground_caps_by_owner=ground_caps_by_owner,
        attached=attached,
    )


def _net_connectivity(
    index: _RecordIndex, net_id: int
) -> tuple[set[int], dict[int, int]]:
    """The node universe of original net ``net_id`` and its DC connectivity
    (union-find representatives) over the DC-conducting elements -- the
    record's resistors (substrate DC ties excluded) and inductors -- whose
    *both* endpoints the net owns. Capacitors establish no DC connectivity
    and join no edge here. Cross-net and net-to-ground elements correctly
    join no within-net edge: their other endpoint is not in the universe.
    Reads only ``net_id``'s buckets of the pre-built ``index``.
    """
    uids = index.nodes_by_owner.get(net_id, [])
    return set(uids), _union_find_connectivity(
        uids, index.dc_edges_by_owner.get(net_id, [])
    )


def _check_open(
    record: ParasiticNetworkRecord, net: IntegrityNet, reps: dict[int, int]
) -> dict[str, Any]:
    """The per-net ``open`` block: union-find over the net's required
    terminals through its emitted segment resistors (``reps`` -- the net's
    connectivity from :func:`_net_connectivity`, computed once per net).

    ``distributed`` nets get the real check. ``lumped`` nets report
    ``not_applicable``: the star/Gamma-shunt topology hangs every terminal
    leg off the hub with its own dedicated resistor *by construction*, so
    the emitted model carries no measurable terminal-to-terminal evidence
    for a union-find to evaluate (and a missing-leg bug there is out of
    this model's contract -- see ``docs/cli/extract.md``'s model-limits
    note). Zero required terminals cannot support the check either
    (nothing to connect); a single required terminal is vacuously
    connected (``pass`` -- there is no other terminal to be disconnected
    from).
    """
    if net.rc_model != "distributed":
        return {
            "status": "not_applicable",
            "reason": "lumped model: terminal legs attach to the hub by "
            "construction; no distributed segments to check connectivity "
            "over",
            "disconnected_count": 0,
            "disconnected_terminals": [],
            "details_truncated": False,
        }
    if not net.required_terminal_uids:
        return {
            "status": "not_applicable",
            "reason": "no required terminals emitted for this net",
            "disconnected_count": 0,
            "disconnected_terminals": [],
            "details_truncated": False,
        }
    # `reps.get` (not `[...]`): a required terminal uid the connectivity
    # pass never resolved -- out of range, or not owned by this net, i.e. a
    # malformed record -- counts as disconnected rather than crashing.
    reps_by_uid = {uid: reps.get(uid) for uid in net.required_terminal_uids}
    reference = next(iter(reps_by_uid.values()))
    disconnected = [
        _node_name(record, uid) for uid, rep in reps_by_uid.items() if rep != reference
    ]
    if not disconnected:
        return {
            "status": "pass",
            "reason": None,
            "disconnected_count": 0,
            "disconnected_terminals": [],
            "details_truncated": False,
        }
    capped, truncated = _cap_details(disconnected, record.max_detail_items)
    return {
        "status": "open",
        "reason": "not all required terminals are connected through the "
        "emitted segment resistors",
        "disconnected_count": len(disconnected),
        "disconnected_terminals": capped,
        "details_truncated": truncated,
    }


def _check_islands(
    record: ParasiticNetworkRecord,
    net: IntegrityNet,
    uid_set: set[int],
    reps: dict[int, int],
) -> dict[str, Any]:
    """Floating islands: connected components of the net's DC graph that
    contain none of its required terminals.

    ``not_applicable`` when the net has no required terminals at all (the
    Gamma-shunt stub shape): with no main body to detach from, *every*
    component would be an island and the count would carry no finding
    value.
    """
    if not net.required_terminal_uids:
        return {
            "count": 0,
            "details": [],
            "details_truncated": False,
            "reason": "no required terminals: no main body for an island "
            "to be detached from",
        }
    terminal_reps = {reps.get(uid) for uid in net.required_terminal_uids}
    members: dict[int, list[str]] = {}
    for uid in uid_set:
        members.setdefault(reps[uid], []).append(record.nodes[uid].name)
    islands = sorted(
        (
            sorted(names)  # deterministic member order within one island
            for rep, names in members.items()
            if rep not in terminal_reps
        ),
        key=lambda names: names[0],  # deterministic island order
    )
    capped, truncated = _cap_details(
        [{"nodes": names} for names in islands], record.max_detail_items
    )
    return {
        "count": len(islands),
        "details": capped,
        "details_truncated": truncated,
        "reason": None,
    }


def _check_orphans(
    record: ParasiticNetworkRecord,
    uid_set: set[int],
    attached: set[int],
    net: IntegrityNet,
) -> dict[str, Any]:
    """Orphan nodes: nodes of the net's universe with no element attached
    at all. A node attached even only by a capacitor is a legitimate
    capacitor endpoint, not an orphan -- capacitors count as attachment
    here even though they establish no DC connectivity. ``attached`` is
    the record-wide set :func:`_index_record` builds once.
    """
    orphans = sorted(
        record.nodes[uid].name
        for uid in uid_set
        if uid not in attached and not net.network_dropped
    )
    capped, truncated = _cap_details(
        [{"node": name} for name in orphans], record.max_detail_items
    )
    return {
        "count": len(orphans),
        "details": capped,
        "details_truncated": truncated,
        "reason": None,
    }


def _resistor_issues(resistor: IntegrityResistor) -> list[str]:
    """The malformed-value categories one resistor exhibits, checked before
    any graph arithmetic (a malformed value must diagnose, not crash):
    ``non_finite``, ``negative``, ``zero``, ``self_loop``. Sorted for a
    stable detail shape.
    """
    issues: list[str] = []
    if not math.isfinite(resistor.resistance_ohm):
        issues.append("non_finite")
    else:
        if resistor.resistance_ohm < 0.0:
            issues.append("negative")
        if resistor.resistance_ohm == 0.0:
            issues.append("zero")
    if resistor.node_a == resistor.node_b:
        issues.append("self_loop")
    return sorted(issues)


def _check_suspicious_resistors(
    record: ParasiticNetworkRecord, own_resistors: list[IntegrityResistor]
) -> dict[str, Any]:
    """Malformed resistors this net emitted: negative, zero, non-finite and
    self-loop values, diagnosed before graph arithmetic. Intentional
    substrate DC ties are excluded (a fixed ``1e12`` positive shunt is
    none of these anyway, but the exclusion is explicit). ``own_resistors``
    is the net's emitted resistors, in record order.
    """
    findings: list[dict[str, Any]] = []
    for r in own_resistors:
        if r.intentional_dc_tie:
            continue
        if not _endpoints_known(record, r.node_a, r.node_b):
            continue
        issues = _resistor_issues(r)
        if not issues:
            continue
        findings.append(
            {
                "device": r.name,
                "node_a": _node_name(record, r.node_a),
                "node_b": _node_name(record, r.node_b),
                "resistance_ohm": _finite_or_none(r.resistance_ohm, 6),
                "issues": issues,
            }
        )
    findings.sort(key=lambda f: f["device"])
    capped, truncated = _cap_details(findings, record.max_detail_items)
    return {
        "count": len(findings),
        "details": capped,
        "details_truncated": truncated,
        "reason": None,
    }


def _check_short_candidates(
    record: ParasiticNetworkRecord, own_resistors: list[IntegrityResistor]
) -> dict[str, Any]:
    """Cross-net short candidates: injected parasitic resistors (substrate
    DC ties excluded) whose endpoints resolve to different owners -- a
    different original net, or the shared ground -- at or below the
    record's threshold (inclusive).

    Owners, not display names, decide: two same-named nets stay distinct,
    and a within-net resistor clamped to the extractor minimum is never a
    candidate. Functional (drawn) devices are not in the record at all.
    Only this net's *own* resistors are attributed here; the same finding
    appears once, under the net that emitted it.
    """
    findings: list[dict[str, Any]] = []

    def owner_label(uid: int) -> int | str:
        owner = record.nodes[uid].owner_net_id
        return "ground" if owner is GROUND_OWNER else owner

    for r in own_resistors:
        if r.intentional_dc_tie:
            continue
        if not _endpoints_known(record, r.node_a, r.node_b):
            continue
        owner_a = record.nodes[r.node_a].owner_net_id
        owner_b = record.nodes[r.node_b].owner_net_id
        if owner_a == owner_b:
            continue
        if not (
            math.isfinite(r.resistance_ohm)
            and r.resistance_ohm <= record.short_threshold_ohm
        ):
            continue
        findings.append(
            {
                "device": r.name,
                "node_a": _node_name(record, r.node_a),
                "node_b": _node_name(record, r.node_b),
                "resistance_ohm": _finite_or_none(r.resistance_ohm, 6),
                "owner_a": owner_label(r.node_a),
                "owner_b": owner_label(r.node_b),
            }
        )
    findings.sort(key=lambda f: f["device"])
    capped, truncated = _cap_details(findings, record.max_detail_items)
    return {
        "count": len(findings),
        "details": capped,
        "details_truncated": truncated,
        "reason": None,
    }


def _check_capacitance(
    record: ParasiticNetworkRecord, net: IntegrityNet, ground_caps_ff: list[float]
) -> dict[str, Any]:
    """Ground-capacitance reconciliation: the net's declared total ground
    capacitance against the sum of its emitted ground-capacitor values,
    each capacitor counted exactly once. Coupling capacitance is kept
    separate and never enters this sum.

    ``pass`` requires ``|computed - declared| <= max(abs, rel * |declared|)``
    with the record's tolerances. ``mismatch`` fires when the emitted
    capacitors disagree with the declared total -- including when either
    side is non-finite (a NaN total can never satisfy the tolerance
    comparison, and its value serializes as ``null``). A replacement model
    that cannot be compared fairly reports ``not_applicable`` with its
    reason instead of a verdict. ``ground_caps_ff`` is the net's emitted
    ground-capacitor values, in record order.
    """
    computed_ff = math.fsum(ground_caps_ff)
    tolerance = {
        "absolute_ff": _finite_or_none(record.cap_tolerance_abs_ff, 12),
        "relative": _finite_or_none(record.cap_tolerance_rel, 12),
    }
    declared = net.declared_capacitance_ff
    if not net.capacitance_checkable:
        return {
            "status": "not_applicable",
            "declared_ff": _finite_or_none(declared, 6),
            "computed_ff": _finite_or_none(computed_ff, 6),
            "delta_ff": None,
            "reason": net.capacitance_reason,
            "tolerance": tolerance,
        }
    delta = computed_ff - declared
    relative_limit = (
        record.cap_tolerance_rel * abs(declared) if math.isfinite(declared) else 0.0
    )
    limit = max(record.cap_tolerance_abs_ff, relative_limit)
    ok = math.isfinite(delta) and math.isfinite(declared) and abs(delta) <= limit
    return {
        "status": "pass" if ok else "mismatch",
        "declared_ff": _finite_or_none(declared, 6),
        "computed_ff": _finite_or_none(computed_ff, 6),
        "delta_ff": _finite_or_none(delta, 9),
        "reason": None
        if ok
        else "emitted ground capacitance differs from the "
        "declared total beyond the documented tolerance",
        "tolerance": tolerance,
    }


def check_parasitic_network(record: ParasiticNetworkRecord) -> dict[str, Any]:
    """Run every per-net integrity check over ``record`` and return the
    additive ``integrity`` block: the summary fields (threshold, tolerance,
    bounded-detail cap, aggregate counts) plus one entry per net in
    ``record.nets`` order (the wiring keeps that identical to
    ``parasitics.nets[]`` order).

    Never raises on malformed input values: every check diagnoses and
    moves on, and every emitted number passes through
    :func:`_finite_or_none`, so the block serializes as strict finite
    JSON even for non-finite input.

    Runs in ``O(nodes + elements)`` overall: the record is indexed by owner
    once (:func:`_index_record`) and each net's checks read only their own
    buckets.
    """
    index = _index_record(record)
    nets_report: list[dict[str, Any]] = []
    open_nets = 0
    island_components = 0
    orphan_nodes = 0
    suspicious_resistors = 0
    short_candidates = 0
    capacitance_mismatches = 0
    nets_with_findings = 0
    for net in record.nets:
        uid_set, reps = _net_connectivity(index, net.net_id)
        own_resistors = index.resistors_by_owner.get(net.net_id, [])
        open_block = _check_open(record, net, reps)
        islands_block = _check_islands(record, net, uid_set, reps)
        orphans_block = _check_orphans(record, uid_set, index.attached, net)
        suspicious_block = _check_suspicious_resistors(record, own_resistors)
        shorts_block = _check_short_candidates(record, own_resistors)
        capacitance_block = _check_capacitance(
            record, net, index.ground_caps_by_owner.get(net.net_id, [])
        )
        findings = (
            open_block["status"] == "open"
            or islands_block["count"] > 0
            or orphans_block["count"] > 0
            or suspicious_block["count"] > 0
            or shorts_block["count"] > 0
            or capacitance_block["status"] == "mismatch"
        )
        if findings:
            nets_with_findings += 1
        if open_block["status"] == "open":
            open_nets += 1
        island_components += islands_block["count"]
        orphan_nodes += orphans_block["count"]
        suspicious_resistors += suspicious_block["count"]
        short_candidates += shorts_block["count"]
        if capacitance_block["status"] == "mismatch":
            capacitance_mismatches += 1
        nets_report.append(
            {
                "net": net.net,
                "net_id": net.net_id,
                "rc_model": net.rc_model,
                "open": open_block,
                "islands": islands_block,
                "orphan_nodes": orphans_block,
                "suspicious_resistors": suspicious_block,
                "short_candidates": shorts_block,
                "capacitance": capacitance_block,
            }
        )
    return {
        "short_threshold_ohm": _finite_or_none(record.short_threshold_ohm, 6),
        "cap_tolerance": {
            "absolute_ff": _finite_or_none(record.cap_tolerance_abs_ff, 12),
            "relative": _finite_or_none(record.cap_tolerance_rel, 12),
        },
        "max_detail_items": record.max_detail_items,
        "nets_checked": len(record.nets),
        "nets_with_findings": nets_with_findings,
        "open_nets": open_nets,
        "island_components": island_components,
        "orphan_nodes": orphan_nodes,
        "suspicious_resistors": suspicious_resistors,
        "short_candidates": short_candidates,
        "capacitance_mismatches": capacitance_mismatches,
        "nets": nets_report,
    }


def empty_integrity_summary(
    short_threshold_ohm: float = DEFAULT_SHORT_THRESHOLD_OHM,
    cap_tolerance_abs_ff: float = CAP_TOLERANCE_ABS_FF,
    cap_tolerance_rel: float = CAP_TOLERANCE_REL,
    max_detail_items: int = MAX_DETAIL_ITEMS,
) -> dict[str, Any]:
    """The summary half of :func:`check_parasitic_network`'s result for a
    run that injected no nets -- the zero-net branch of ``run_extract``
    still reports the block (schema stability), with every count at zero.

    Like the checked branch's summary (whose per-net entries
    ``_attach_network_integrity`` inlines onto ``parasitics.nets[]``), this
    carries no per-net list -- the summary is net-name-free, so rerun
    canonicalization has nothing to reorder."""
    return {
        "short_threshold_ohm": _finite_or_none(short_threshold_ohm, 6),
        "cap_tolerance": {
            "absolute_ff": _finite_or_none(cap_tolerance_abs_ff, 12),
            "relative": _finite_or_none(cap_tolerance_rel, 12),
        },
        "max_detail_items": max_detail_items,
        "nets_checked": 0,
        "nets_with_findings": 0,
        "open_nets": 0,
        "island_components": 0,
        "orphan_nodes": 0,
        "suspicious_resistors": 0,
        "short_candidates": 0,
        "capacitance_mismatches": 0,
    }
