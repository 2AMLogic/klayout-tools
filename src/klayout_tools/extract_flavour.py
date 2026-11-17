"""Per-gate MOS-flavour island claiming for ``klt extract`` (issue #5301).

Split out of ``extract.py`` to keep it under its file-size ceiling. A move,
not a rewrite of the claim test; ``extract.py`` re-exports both names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .decks import MOSFlavour

if TYPE_CHECKING:
    import klayout.db as kdb

    from .decks import ExtractionDeck


def _flavour_claims_island(
    island: kdb.Region,
    flavour: MOSFlavour,
    marker_region: kdb.Region,
    and_regions: list[kdb.Region],
    not_regions: list[kdb.Region],
    nwell: kdb.Region,
) -> bool:
    """Whether ``flavour`` claims the active ``island``: it touches the
    flavour's ``marker`` and every ``and_markers`` layer, touches no
    ``not_markers`` layer, and (``nfet_only``) lies outside the well."""
    return (
        not island.interacting(marker_region).is_empty()
        and all(not island.interacting(r).is_empty() for r in and_regions)
        and all(island.interacting(r).is_empty() for r in not_regions)
        and not (flavour.nfet_only and not island.interacting(nwell).is_empty())
    )


def _flavour_split_island(
    island: kdb.Region,
    gates_region: kdb.Region,
    flavour: MOSFlavour,
    marker_region: kdb.Region,
    and_regions: list[kdb.Region],
    not_regions: list[kdb.Region],
    nwell: kdb.Region,
) -> tuple[kdb.Region, kdb.Region]:
    """Split an active ``island`` into ``(claimed, remaining)`` per *gate*
    (issue #5301). A gate (``poly & island`` component) is claimed by
    ``flavour`` under the :func:`_flavour_claims_island` test applied to the
    gate area, so a marker ending inside diffusion shared with a neighbouring
    gate does not flip that neighbour. Shared source/drain diffusion stays in
    both halves. An island with no gate falls back to the island-level test;
    a wholly covered/uncovered island returns the whole island on one side.

    ``gates_region`` is the caller-precomputed ``active & poly`` gate area
    (issue #5893 -- per-island whole-chip intersections dominated
    ``device_extraction``); its merged components are per-island, so the
    ones interacting with ``island`` are exactly ``island & poly`` merged.
    The other region arguments are used coverage-only, so callers may pass
    them merged (and do).
    """
    import klayout.db as kdb

    # Wholly inside the marker, with a claim test that carries no
    # and/not/`nfet_only` modifier (it reduces to "the gate touches the
    # marker"): every gate of the island touches the marker too, so the
    # per-gate walk below can only return the whole island as claimed --
    # return that directly, before any gate geometry is built. Issue
    # #5893: on gf180mcu the `Dualgate` 55/0 flavour covers a routed
    # block's entire core, and the per-island gate intersection cost ~300
    # of ~360 device-extraction seconds on the 166 KB ttgf0p2
    # PAL_TOP_WRAPPER layout. The flags are tested before the subtract, so
    # flavours with modifiers (e.g. sky130 native-Vt) pay nothing.
    if (
        not flavour.and_markers
        and not flavour.not_markers
        and not flavour.nfet_only
        and (island - marker_region).is_empty()
    ):
        return island, kdb.Region()
    # `gates_region` is the deck's `(active & poly)` gate area, precomputed
    # by the caller (issue #5893): intersecting the whole-chip poly with
    # every island separately repeated one whole-chip boolean per island.
    # The region's components are merged per island already -- islands are
    # disjoint and every gate lies inside one island -- so the components
    # that interact with `island` are exactly `(island & poly).merged()`.
    gates = [kdb.Region(c) for c in gates_region.interacting(island).each()]
    if not gates:
        if _flavour_claims_island(
            island, flavour, marker_region, and_regions, not_regions, nwell
        ):
            return island, kdb.Region()
        return kdb.Region(), island
    claimed_gates = kdb.Region()
    unclaimed_gates = kdb.Region()
    for gate in gates:
        if _flavour_claims_island(
            gate, flavour, marker_region, and_regions, not_regions, nwell
        ):
            claimed_gates += gate
        else:
            unclaimed_gates += gate
    if unclaimed_gates.is_empty() and not claimed_gates.is_empty():
        return island, kdb.Region()
    if claimed_gates.is_empty():
        return kdb.Region(), island
    return island - unclaimed_gates, island - claimed_gates


def _extract_flavour_devices(
    l2n: kdb.LayoutToNetlist,
    deck: ExtractionDeck,
    flavour: MOSFlavour,
    regions: tuple[kdb.Region, kdb.Region, kdb.Region, kdb.Region],
    terminals: tuple[kdb.Region, kdb.Region] | None,
    nfet_body: kdb.Region,
    pfet_body: kdb.Region,
) -> None:
    """One flavour's NMOS then PMOS extraction pass. A ``drain_marker``
    flavour (issue #4570) additionally hands KLayout its ``tS``/``tD`` layers
    so source and drain are distinct, ordered terminals."""
    import klayout.db as kdb

    f_nfet_sd, f_nfet_gate, f_pfet_sd, f_pfet_gate = regions
    extra: dict[str, kdb.Region] = {}
    if terminals is not None:
        extra = {"tS": terminals[0], "tD": terminals[1]}
    if not flavour.pfet_only:
        l2n.extract_devices(
            kdb.DeviceExtractorMOS4Transistor(deck.nfet_class),
            {"SD": f_nfet_sd, "G": f_nfet_gate, "W": nfet_body, **extra},
        )
    if not (flavour.nfet_only and terminals is not None):
        l2n.extract_devices(
            kdb.DeviceExtractorMOS4Transistor(deck.pfet_class),
            {"SD": f_pfet_sd, "G": f_pfet_gate, "W": pfet_body, **extra},
        )


def apportion_shared_sd(
    l2n: kdb.LayoutToNetlist,
    circuit: kdb.Circuit | None,
    passes: list[tuple[kdb.Region, kdb.Region]],
) -> None:
    """Re-divide shared source/drain AS/AD/PS/PD across extraction passes
    (issue #5311).

    KLayout's MOS extractor divides a diffusion polygon's area and perimeter
    by the number of gates *it was given* touching that polygon. The per-gate
    flavour split (#5301) puts a shared S/D polygon in both a flavour pass and
    the default pass, each seeing only its own gates, so the shared area was
    counted in full for every neighbour. ``passes`` is one ``(gate, sd)``
    region pair per pass (default NMOS/PMOS, then each flavour); a polygon
    touched by gates of two or more passes has its terminal parameters scaled
    by ``gates in this pass / gates in all passes`` -- the value an unsplit
    extraction yields. No-op when no S/D polygon straddles passes.
    """
    if circuit is None:
        return
    mixed = _mixed_sd_region(passes)
    if mixed.is_empty():
        return
    dbu = l2n.internal_layout().dbu
    polygons = list(mixed.each())
    pass_gates = [(g.merged() & g.merged().interacting(mixed)) for g, _sd in passes]
    for device in circuit.each_device():
        found = _owning_pass(pass_gates, device, dbu)
        if found is None:
            continue
        owner, gate = found
        defs = {td.name: td.id() for td in device.device_class().terminal_definitions()}
        if "S" not in defs or "D" not in defs:
            continue
        for polygon in polygons:
            _rescale_for_polygon(
                l2n, device, defs, polygon, gate, pass_gates, passes[owner][1], owner
            )


def _mixed_sd_region(passes: list[tuple[kdb.Region, kdb.Region]]) -> kdb.Region:
    """S/D polygons touched by gates of two or more extraction passes."""
    import klayout.db as kdb

    all_sd = kdb.Region()
    for _g, sd in passes:
        all_sd += sd
    all_sd = all_sd.merged()
    touched = [all_sd.interacting(g) for g, _sd in passes]
    mixed = kdb.Region()
    for i in range(len(touched)):
        for j in range(i + 1, len(touched)):
            mixed += touched[i] & touched[j]
    return mixed.merged()


def _owning_pass(
    pass_gates: list[kdb.Region], device: kdb.Device, dbu: float
) -> tuple[int, kdb.Region] | None:
    """Index of the pass whose gate sits at the device centre, plus that gate."""
    import klayout.db as kdb

    centre = device.trans.disp
    cx, cy = round(centre.x / dbu), round(centre.y / dbu)
    probe = kdb.Region(kdb.Box(cx - 1, cy - 1, cx + 1, cy + 1))
    for k, g in enumerate(pass_gates):
        if not g.interacting(probe).is_empty():
            return k, g.interacting(probe)
    return None


def _rescale_for_polygon(
    l2n: kdb.LayoutToNetlist,
    device: kdb.Device,
    defs: dict[str, int],
    polygon: kdb.Polygon,
    gate: kdb.Region,
    pass_gates: list[kdb.Region],
    owner_sd: kdb.Region,
    owner: int,
) -> None:
    """Scale the terminal params of ``device`` for one shared S/D polygon."""
    import klayout.db as kdb

    dbu = l2n.internal_layout().dbu
    region = kdb.Region(polygon)
    if region.interacting(gate).is_empty():
        return
    counts = [g.interacting(region).count() for g in pass_gates]
    total = sum(counts)
    if total <= counts[owner] or counts[owner] == 0:
        return
    vertex = next(iter(polygon.each_point_hull()))
    net = l2n.probe_net(owner_sd, kdb.DPoint(vertex.x * dbu, vertex.y * dbu))
    if net is None:
        return
    factor = counts[owner] / total
    for term, params in (("S", ("AS", "PS")), ("D", ("AD", "PD"))):
        t_net = device.net_for_terminal(defs[term])
        if t_net is not None and t_net.cluster_id == net.cluster_id:
            for name in params:
                device.set_parameter(name, device.parameter(name) * factor)
            return
