"""``klt extract`` diode substrate-label divergence diagnostic (issue #5736).

Split out of `extract.py` to keep it under its file-size ceiling. A move, not
a rewrite: the code below is byte-for-byte what lived inline there. It reads
``layout`` / ``top_cell`` / ``deck`` geometry after the extraction pass and
only appends to ``warnings[]``. `extract.py` re-exports
``_is_synthesized_substrate_net_name`` (used by `extract_parasitics.py`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .decks import DiodeDevice, ExtractionDeck
from .extract_parasitics import spice_safe_net_name

if TYPE_CHECKING:
    import klayout.db as kdb


def _is_synthesized_substrate_net(name: str, deck: ExtractionDeck) -> bool:
    """Whether ``name`` is one of this deck's *synthesized* substrate
    identities rather than a real, drawn-and-labelled net name: the deck-wide
    ``substrate_net`` global itself, or one of the per-isolated-region
    ``f"{substrate_net}_iso{n}"`` identities issue #1128 mints (see
    ``ExtractionDeck.substrate_isolation``'s docstring).

    Neither is ever a name a layout can draw: ``connect_global`` invents them,
    so a net carrying one of these names is one that earned no label of its
    own anywhere in the layout.
    """
    return _is_synthesized_substrate_net_name(name, deck.substrate_net)


def _is_synthesized_substrate_net_name(name: str, substrate_net: str) -> bool:
    """:func:`_is_synthesized_substrate_net`'s predicate, expressed against a
    bare ``substrate_net`` string rather than a whole
    :class:`ExtractionDeck`.

    Split out for :func:`_tie_substrate_nets_to_ground` (issue #1263), which
    runs inside :func:`_inject_parasitics` -- a function that is handed the
    deck's ``substrate_net`` name (as ``ground_net_name``) but not the deck
    object. Keeping the one-line rule in a single place stops the two call
    sites drifting apart the next time the synthesized-identity naming
    convention grows a variant (it already grew ``_iso<n>`` once, issue
    #1128).
    """
    return name == substrate_net or name.startswith(f"{substrate_net}_iso")


def _region_probe_points(region: kdb.Region) -> list[kdb.Point]:
    """One point *strictly inside* each connected component of ``region``,
    suitable for ``LayoutToNetlist.probe_net``.

    A merged component's bbox centre is inside it for any convex (in
    practice: rectangular) shape, which is what a diffusion tap cut out of a
    device-mark footprint almost always is. A concave component (an L, a
    ring) is decomposed into rectilinear trapezoids first and one point per
    part is returned instead -- probing more points than strictly necessary
    is harmless (every part of one component resolves to the same net), while
    probing a point in the notch of an L would silently resolve to nothing.
    """
    import klayout.db as kdb

    points: list[kdb.Point] = []
    for polygon in region.merged().each():
        box = polygon.bbox()
        centre = kdb.Point((box.left + box.right) // 2, (box.bottom + box.top) // 2)
        if polygon.inside(centre):
            points.append(centre)
            continue
        for part in polygon.decompose_trapezoids():
            part_box = part.bbox()
            points.append(
                kdb.Point(
                    (part_box.left + part_box.right) // 2,
                    (part_box.bottom + part_box.top) // 2,
                )
            )
    return points


def _detect_diode_substrate_label_divergence(
    l2n: kdb.LayoutToNetlist,
    circuit: kdb.Circuit | None,
    deck: ExtractionDeck,
    diode_regions: list[tuple[DiodeDevice, kdb.Region, kdb.Region]],
    contact: kdb.Region,
    poly: kdb.Region,
    mos_source_drain: kdb.Region,
) -> list[str]:
    """Issue #1196: flag a diode whose *substrate-formed* terminal (declared
    ``None``, tied to the deck's synthesized ``substrate_net`` global by
    :func:`_extract_netlist`'s diode connectivity block) resolved to that
    synthesized name **while real, drawn, labelled tie geometry sits inside
    the very footprint that terminal is formed from**.

    Both halves of that condition matter:

    - A substrate-formed terminal usually *should* land on the synthesized
      global -- that is the documented fallback for a PDK that draws no
      p-substrate mask, and the common case for every gf180mcu corpus cell
      today. Warning on it unconditionally would be noise, so this never
      fires on a layout that draws no tie into the terminal's footprint at
      all.
    - When a tie *is* drawn and the deck's own substrate-tap derivation
      claims it (``tap``/``tap_nplus``/``tap_pplus``, issues #490/#1084),
      ``connect_global`` merges the two into one net and KLayout names the
      result from the drawn label -- so the terminal resolves to the real
      name and this never fires either (verified: a p+/Comp tap contacted up
      to a ``VSS``-labelled Metal1 gives the diode anode net ``VSS``, not
      ``vsubs``).

    What is left is exactly the silent case the issue reports: drawn,
    contacted, *labelled* geometry inside the terminal's footprint that the
    deck's tap derivation does **not** claim (an unimplanted diffusion tie, a
    tie the deck models as belonging to a different substrate identity, ...).
    Nothing joins the two, so the labelled net exists in the netlist beside
    the device while the terminal keeps the synthesized global -- previously
    with no trace anywhere in ``klt extract``'s output.

    The probe area is the deck's ``contact`` cuts landing inside the
    terminal's own footprint: a tie can only carry a *name* if it is
    contacted and routed up to a labelled conductor, and ``contact`` is one
    of the layers :func:`_extract_netlist` registers with ``l2n``, which is
    what ``probe_net`` requires (the deck's raw ``active`` region is not
    registered, and is in any case already split by well/gate/flavour by the
    time this runs). Subtracted from it first, so a *device's* own contacted
    terminal inside the same mark is never mistaken for a substrate tie:

    - every drawn (non-substrate-formed) diode terminal region of every
      entry -- e.g. the nd2ps cathode inside its own ``diode_mk`` footprint,
      and a sibling entry's terminals, since two entries may share one
      device-mark layer;
    - ``mos_source_drain``, the deck's MOS source/drain diffusion that
      actually *touches a gate* (i.e. belongs to a recognised transistor) --
      not the deck's whole ``active - poly`` split, which is every diffusion
      shape in the layout including a substrate tie;
    - ``poly``, so a poly contact inside the mark reports the gate's net
      rather than a tie.

    One aggregate warning per (diode entry, terminal), counting devices
    rather than listing them, mirroring this module's other aggregate
    ``warnings[]`` entries. Returns ``[]`` for every deck that declares no
    substrate-formed diode terminal at all.
    """
    import klayout.db as kdb

    if circuit is None:
        return []

    substrate_terminals = [
        (diode, terminal, region, sibling)
        for diode, anode_region, cathode_region in diode_regions
        for terminal, layer, region, sibling in (
            ("A", diode.anode, anode_region, cathode_region),
            ("C", diode.cathode, cathode_region, anode_region),
        )
        if layer is None
    ]
    if not substrate_terminals:
        return []

    # Every *drawn* diode terminal region, of every entry -- a diode's own
    # drawn terminal is a device terminal, never a substrate tie, and two
    # entries can share one device-mark layer (gf180mcu's two diodes both use
    # `diode_mk`), so a sibling entry's terminal can land inside this one's
    # footprint too. Substrate-formed (`None`-declared) terminals are
    # deliberately *not* collected: those regions are the device's own mark
    # footprint, which is exactly the area being probed here.
    drawn_terminals = kdb.Region()
    for diode, anode_region, cathode_region in diode_regions:
        if diode.anode is not None:
            drawn_terminals += anode_region
        if diode.cathode is not None:
            drawn_terminals += cathode_region

    warnings: list[str] = []
    for diode, terminal, region, sibling in substrate_terminals:
        unresolved = 0
        for device in circuit.each_device():
            device_class = device.device_class()
            if device_class.name != diode.name:
                continue
            for definition in device_class.terminal_definitions():
                if definition.name.upper() != terminal:
                    continue
                net = device.net_for_terminal(definition.id())
                if net is not None and _is_synthesized_substrate_net(
                    net.expanded_name(), deck
                ):
                    unresolved += 1
        if not unresolved:
            continue

        # `sibling` is subtracted explicitly as well as via `drawn_terminals`
        # to keep the intent readable: the other terminal of *this* junction
        # is the one most likely to sit inside this footprint.
        probe_region = (
            ((region & contact) - sibling) - drawn_terminals - mos_source_drain - poly
        )
        if probe_region.is_empty():
            continue

        labels: list[str] = []
        for point in _region_probe_points(probe_region):
            probed = l2n.probe_net(contact, point)
            if probed is None:
                continue
            name = probed.expanded_name()
            # `Net.name` is empty for a net that earned no drawn label, so
            # `expanded_name()` returns the anonymous `$n` spelling -- there
            # is no real name to have been discarded in that case. A label
            # spelled exactly like the deck's own synthesized substrate name
            # is likewise not a divergence (issue #1196's own no-false-
            # positive requirement).
            if not probed.name or _is_synthesized_substrate_net(name, deck):
                continue
            safe = spice_safe_net_name(name)
            if safe not in labels:
                labels.append(safe)
        if not labels:
            continue

        shown = ", ".join(sorted(labels)[:5])
        more = len(labels) - 5
        warnings.append(
            f"{unresolved} {diode.name} '{terminal.lower()}' terminal(s) "
            f"resolved to the deck-synthesized '{deck.substrate_net}' "
            "substrate net, but drawn, labelled tie geometry inside the same "
            f"device footprint resolves to a different net ({shown}"
            + (f", +{more} more" if more > 0 else "")
            + ") -- this deck's substrate-tap derivation does not claim that "
            "drawn shape, so the synthesized global was substituted for the "
            "drawn net name; check the tie's implant/tap layers (see "
            'docs/cli/extract.md, "Coverage")'
        )
    return warnings
