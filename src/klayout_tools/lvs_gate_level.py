"""Gate-level power-pin evidence, supply-net spelling, port aliasing and
power-only circuit pruning for ``reference.form: "gate-level-verilog"``
(issues #1622, #2021, #2244).

Split out of :mod:`klayout_tools.lvs` as a pure move. ``LvsError``/
``CATEGORY_*``/``POWER_PINS_RULE_*`` constants stay defined in ``lvs`` and are
imported *inside* the functions that need them, the same dependency direction
as :mod:`klayout_tools.lvs_power`; ``lvs`` re-imports every name defined here.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from .extract import spice_safe_net_name
from .lvs_combine import _purge_emptied_nets
from .lvs_mismatch import _mismatch, _SupplyPinUniverse


def _name_or_none(obj: Any) -> str | None:
    """The reported name of a KLayout ``Net``/``Device`` (or a test double
    implementing the same protocol), or ``None`` for a missing side.

    A ``Net``'s ``expanded_name()`` (the branch every real net/device object
    hits) is passed through :func:`spice_safe_net_name` (issue #696) so a
    label-merged net's name matches the exact spelling `klt extract`'s
    ``nets[]``/``merged_net_labels[]`` and the written SPICE netlist already
    use (`|`-joined) -- not KLayout's own un-escaped, comma-joined
    ``Net.expanded_name()`` string. A no-op for every other name (device
    names, and the ``.name`` fallback below never carry a comma in
    practice).
    """
    if obj is None:
        return None
    if hasattr(obj, "expanded_name"):
        return spice_safe_net_name(obj.expanded_name())
    if hasattr(obj, "name"):
        name = obj.name
        name = name() if callable(name) else name
        return spice_safe_net_name(name) if name is not None else None
    return None


def _subcircuit_parent_name(obj: Any) -> str | None:
    """The name of the ``kdb.Circuit`` that contains subcircuit instance
    ``obj`` (a ``kdb.SubCircuit``, or ``None`` for a missing side), i.e.
    ``obj.circuit()`` -- **not** the circuit ``obj`` refers to (that is
    :func:`_subcircuit_ref_name`). Used to populate an unmatched-subcircuit
    ``mismatches[]`` entry's ``circuit`` field (issue #1132) so the finding
    names which module the missing instance lives in. ``getattr``-guarded so
    a stand-in test double that only implements ``expanded_name()`` (like
    the rest of this module's ``_FakeLogger`` fixtures) degrades to ``None``
    rather than raising.
    """
    if obj is None:
        return None
    method = getattr(obj, "circuit", None)
    if not callable(method):
        return None
    return _name_or_none(method())


def _subcircuit_ref_name(obj: Any) -> str | None:
    """The name of the ``kdb.Circuit`` subcircuit instance ``obj`` refers to
    (``obj.circuit_ref()``) -- its "cell type", as opposed to
    :func:`_subcircuit_parent_name`'s containing circuit. Populates an
    unmatched-subcircuit ``mismatches[]`` entry's ``subcircuit`` field (issue
    #1132). ``getattr``-guarded for the same reason as
    :func:`_subcircuit_parent_name`."""
    if obj is None:
        return None
    method = getattr(obj, "circuit_ref", None)
    if not callable(method):
        return None
    return _name_or_none(method())


def _circuit_pin_names(circuit: Any) -> list[str]:
    """``circuit``'s declared pin names, upper-cased, skipping unnamed pins.

    Upper-cased because SPICE is case-insensitive and the two sides of the
    comparison reach this module through different readers:
    ``NetlistSpiceReader`` normalises what it reads to upper case, while
    :func:`~klayout_tools.verilog_netlist.parse_subckt_pin_orders` reports a
    library file's ``.subckt`` header verbatim. Folding both to one case is
    what lets a library pin order line up with the reference circuit read
    back from the converted SPICE (issue #1622).
    """
    each_pin = getattr(circuit, "each_pin", None)
    if not callable(each_pin):
        return []
    return [name.upper() for pin in each_pin() if (name := pin.name())]


def _reference_signal_pin_names(netlist: Any | None) -> frozenset[str] | None:
    """Every pin name declared on any circuit in ``netlist`` (upper-cased),
    or ``None`` when ``netlist`` itself is unavailable.

    Issue #1622: a ``reference.form: "gate-level-verilog"`` reference
    netlist never carries power/ground pins at all -- its conversion only
    ever emits the *signal* subset of a standard cell's real PDK pin order
    (``verilog_netlist.py``'s own "No power/ground pins" docstring note;
    see also ``docs/cli/lvs.md``'s "No power/ground pins" section). So
    every pin name that appears *anywhere* in the reference netlist is, by
    construction, a genuine **signal** pin.

    That makes this the *subtrahend* in :func:`_gate_level_power_pin_names`'s
    derivation, never a power-pin test on its own: "this pin name is absent
    from the reference" says nothing by itself, because a reference that
    never instantiates a given master says nothing at all about that
    master's pins (see that function's docstring).

    **And it is only half the guard** (issue #2076). Subtracting this set
    excludes a signal pin that *some* instance in the design connects --
    it cannot exclude one that the design's only carrier of that pin name
    leaves dangling, because then the conversion emits no pin of that name
    anywhere and this set does not contain it at all. The complementary
    half -- requiring every instantiated master to declare a pin before
    admitting it -- lives in :func:`_gate_level_power_pin_evidence`.
    """
    if netlist is None:
        return None
    each_circuit = getattr(netlist, "each_circuit", None)
    if not callable(each_circuit):
        return None
    names: set[str] = set()
    for circuit in each_circuit():
        names.update(_circuit_pin_names(circuit))
    return frozenset(names)


def _gate_level_power_pin_evidence(
    reference_netlist: Any | None,
    library_pin_orders: Mapping[str, list[str]] | None,
) -> tuple[frozenset[str] | None, list[str], bool]:
    """``(power_pin_names, masters, corroborated)`` -- the derived
    power/ground pin universe (issue #1622) together with the evidence it
    rests on: the sorted names of the library cells (masters) the reference
    instantiates, and whether that evidence is genuinely cross-corroborated
    (issue #2076).

    ``power_pin_names`` is the set of pin names (upper-cased) that are
    demonstrably **power/ground** pins of this PDK's standard-cell library,
    derived from data ``run_lvs`` has already read -- or ``None`` when there
    is not enough evidence to derive one at all.

    The derivation, and why each part of it is load-bearing:

    * ``library_pin_orders`` is the standard-cell library's own
      ``.subckt`` data (see :func:`_resolve_gate_level_pin_orders`), i.e.
      each cell's **full** PDK pin order -- signal *and* power/ground.
    * :func:`~klayout_tools.verilog_netlist.convert_gate_level_verilog`
      emits, for each cell the Verilog instantiates, only the pins that
      Verilog actually connects -- structurally never a power/ground pin.

    So a pin the library declares but the conversion did not carry is a
    *candidate* power/ground pin. Two independent conditions must both hold
    before one is admitted, and the second is what issue #2076 added:

    1. **No reference circuit carries the name.**
       :func:`_reference_signal_pin_names` is subtracted, so a pin that any
       instance of any cell connects is a signal pin.
    2. **Every library cell the reference instantiates declares the name.**
       A genuine supply is declared by every standard cell in the library's
       logic set -- the conversion drops it on *all* of them, so it survives
       the intersection. A signal pin is declared by only some of them.

    Condition 1 alone is the pre-#2076 rule, and it does not hold when the
    design's **only carrier of a pin name leaves that pin dangling**: CTS
    emits clock-load cells with unconnected outputs (``sky130_fd_sc_hd__inv_1
    clkload0 (.A(clknet));`` -- ``Y`` dangling), so if no other instantiated
    master declares ``Y``, the conversion emits no ``Y`` pin anywhere, the
    subtrahend is empty for it, and a signal output is admitted as a supply.
    That inflated ``power_pins``, and -- once CTS put two clock loads on two
    different leaf nets -- produced a spurious
    :data:`RULE_POWER_INCONSISTENT_PIN_NET` finding on a layout whose
    supplies were perfectly connected. Condition 2 excludes it: the
    flip-flops declare ``Q``, the clock buffers ``X``, and neither declares
    ``Y``, while all three declare the four supplies.

    Both conditions keep the universe free of any hardcoded per-PDK name
    table (sky130's ``VPWR``/``VGND``/``VPB``/``VNB`` vs. gf180mcu's
    ``VDD``/``VSS``/``VNW``/``VPW``) and of any cell-name glob
    (``fill_*``/``tap*``, the superseded ``docs/cli/lvs.md`` workaround).

    **Restricting the candidate set to cells the reference instantiates is
    the whole point, not an optimisation.** The obvious wider version --
    "every pin name anywhere in the library that is not in the reference's
    signal-pin universe" -- is unsound in exactly the direction this
    function exists to prevent: in a library containing ``dfxtp_1``
    (``CLK D Q VGND VNB VPB VPWR``), a reference that only instantiates
    inverters and buffers mentions ``CLK``/``D``/``Q`` nowhere, so they
    would be admitted as "power" pins and a stray, genuinely
    signal-bearing ``dfxtp_1`` master in the layout would be pruned as
    power-only -- masking a real missing-cell defect. A cell the reference
    never instantiates contributes nothing here, so its signal pins can
    never be mistaken for power pins.

    **Where the evidence genuinely runs out, and why it is reported rather
    than guessed at.** Condition 2 is a cross-master corroboration, so it
    can only corroborate when there is more than one *genuinely distinct*
    master: a reference instantiating a single library cell -- or several
    drive-strength variants of the same one -- that leaves one of its pins
    dangling offers nothing that separates that pin from the cell's
    supplies -- both are declared identically by every instantiated master
    and carried by none. Counting *names* (``len(masters) > 1``) is not
    enough: ``mylib__inv_1``/``mylib__inv_2`` are two masters by name but
    declare the identical pin order, so their intersection is no more
    informative than either alone -- a dangling ``Y`` on both survives
    exactly as it would with only one of them instantiated. ``corroborated``
    is therefore true only when the instantiated masters' full declared pin
    sets are not all identical, i.e. there are at least two distinct
    *shapes* among them (the flip-flop's ``CLK``/``D``/``Q`` vs. the
    inverter's ``A``/``Y``, not merely two differently-named inverters).
    ``masters`` is returned alongside the universe -- and alongside
    ``corroborated`` -- precisely so a same-shape-only case is visible in
    the report (``power_connectivity.power_pins_derivation``, see
    :func:`_power_pins_derivation`) instead of being silently indistinguishable
    from a genuinely corroborated one. Narrowing further -- e.g. demanding
    that a supply be declared by every cell in the *whole* library -- was
    measured against both supported PDKs and rejected: it admits only
    ``VGND``/``VPWR`` for sky130 (9 of 437 cells omit a well tie) and only
    ``VDD``/``VSS`` for gf180mcu, dropping the well-tie pins out of the
    checked universe and out of :func:`_is_power_only_circuit`'s tap-cell
    classification -- real supply-defect coverage traded away to fix a case
    the intersection above already fixes.

    ``power_pin_names`` is ``None`` when either input is missing/unusable,
    and an empty set when nothing qualifies; :func:`_is_power_only_circuit`
    treats both as "no evidence" and prunes nothing. ``masters`` is ``[]``
    whenever the universe is ``None`` or the reference instantiates no cell
    of this library at all.
    """
    return _gate_level_power_pin_evidence_detail(reference_netlist, library_pin_orders)[
        :3
    ]


def _gate_level_power_pin_evidence_detail(
    reference_netlist: Any | None,
    library_pin_orders: Mapping[str, list[str]] | None,
) -> tuple[frozenset[str] | None, list[str], bool, list[dict[str, Any]] | None]:
    """:func:`_gate_level_power_pin_evidence`'s three values plus a fourth,
    ``libraries`` (issue #2707): ``None`` when the single-library rule
    (:data:`POWER_PINS_RULE_EVERY_INSTANTIATED_MASTER`) applied, otherwise one
    record per pin-order source library the instantiated masters came from
    (:data:`POWER_PINS_RULE_EVERY_MASTER_PER_LIBRARY`).

    **The per-library rule.** Library attribution comes from
    ``library_pin_orders.cell_libraries`` (see :class:`_AttributedPinOrders`).
    When the instantiated masters span two or more libraries, the
    intersection is taken *within* each library and the results are unioned,
    then the reference's signal pins are subtracted (condition 1, which is
    library-independent). ``corroborated`` is true only when **every**
    contributing library has at least two distinct declared-pin shapes.

    **Single-master library decision (issue #2707).** A library contributing
    exactly one master (typically one SRAM macro) is *admitted* to the union,
    with ``corroborated: false`` for that library. Not admitting it would
    silently drop the power check for exactly the designs (macro-bearing
    blocks) that most need it. Admission costs little: the layout-side
    power-pin connections only report pins actually observed on layout
    instances, and a macro signal pin left dangling in the reference could
    only be mistaken for a supply -- the same disclosed limitation the
    single-library single-master case already carries. No second source of
    evidence is required; instead the per-library ``corroborated`` and the
    top-level ``reason`` name each uncorroborated library so a reader can
    discount it.
    """
    if not library_pin_orders:
        return None, [], False, None
    signal_pin_names = _reference_signal_pin_names(reference_netlist)
    if signal_pin_names is None:
        return None, [], False, None
    # Case-folded once: library cell names are verbatim from the `.subckt`
    # header (lower case, in both supported libraries), while the reference
    # circuit names come back from `NetlistSpiceReader` upper-cased.
    by_upper_name = {
        str(cell).upper(): pins for cell, pins in library_pin_orders.items()
    }
    cell_libraries: Mapping[str, str | None] = getattr(
        library_pin_orders, "cell_libraries", {}
    )
    masters: set[str] = set()
    declared_per_master: list[set[str]] = []
    groups: dict[str | None, list[tuple[str, set[str]]]] = {}
    for circuit in reference_netlist.each_circuit():
        name = str(circuit.name).upper()
        pins = by_upper_name.get(name)
        if pins is None or name in masters:
            continue
        masters.add(name)
        declared = {pin.upper() for pin in pins if pin}
        declared_per_master.append(declared)
        groups.setdefault(cell_libraries.get(name), []).append((name, declared))
    if not declared_per_master:
        return frozenset(), [], False, None

    if len(groups) <= 1:
        corroborated_pins = set.intersection(*declared_per_master)
        # Genuine corroboration requires at least two distinct declared-pin
        # *shapes* -- not just two master names -- see the docstring above.
        distinct_shapes = {frozenset(pins) for pins in declared_per_master}
        corroborated = len(distinct_shapes) > 1
        return (
            frozenset(corroborated_pins - signal_pin_names),
            sorted(masters),
            corroborated,
            None,
        )

    universe: set[str] = set()
    libraries: list[dict[str, Any]] = []
    all_corroborated = True
    for library in sorted(groups, key=lambda value: (value is None, value or "")):
        members = groups[library]
        declared_sets = [declared for _, declared in members]
        library_pins = set.intersection(*declared_sets) - signal_pin_names
        universe |= library_pins
        library_corroborated = len({frozenset(d) for d in declared_sets}) > 1
        all_corroborated = all_corroborated and library_corroborated
        libraries.append(
            {
                "library": library,
                "masters": sorted(name for name, _ in members),
                "master_count": len(members),
                "corroborated": library_corroborated,
                "power_pins": sorted(library_pins),
            }
        )
    return frozenset(universe), sorted(masters), all_corroborated, libraries


def _gate_level_power_pin_names(
    reference_netlist: Any | None,
    library_pin_orders: Mapping[str, list[str]] | None,
) -> frozenset[str] | None:
    """Just the power/ground pin universe half of
    :func:`_gate_level_power_pin_evidence` -- the form
    :func:`_prune_power_only_circuits` wants, which needs the
    classification but has no report block to disclose the evidence in."""
    return _gate_level_power_pin_evidence(reference_netlist, library_pin_orders)[0]


def _is_power_only_circuit(
    circuit: Any, power_pin_names: frozenset[str] | None
) -> bool:
    """True when every pin ``circuit`` declares is a known power/ground pin
    of this PDK's standard-cell library (see
    :func:`_gate_level_power_pin_names`) -- i.e. ``circuit`` is a power-only
    cell (a filler/tap cell, whose real PDK pin list is
    ``VPWR``/``VGND``/``VPB``/``VNB`` and nothing else) that is out of
    scope for a signal-only ``gate-level-verilog`` compare (issue #1622).

    Structural, not name-pattern: this never special-cases a cell-name glob
    like ``fill_*``/``tap*`` -- it looks only at ``circuit``'s own declared
    pins, so it also catches an equally power-only cell instantiated under
    an unrelated name (e.g. sky130's ``tapvpwrvgnd_1`` tap cell, which the
    superseded ``[!f]*`` glob workaround in ``docs/cli/lvs.md`` did not
    exclude).

    It is deliberately a *positive* test ("every pin is a known power pin")
    rather than the absence test it replaced ("no pin appears in the
    reference"). The absence test could not tell a power-only master apart
    from a signal-bearing master the reference simply never instantiates --
    the false negative this direction rules out by construction.

    A falsy/``None`` ``power_pin_names`` (no derivable universe) and a
    circuit with zero declared pins both return ``False`` -- neither is
    evidence this circuit is power-only, and the safe default on missing
    evidence is to leave the circuit in place and keep reporting the
    existing topology mismatch rather than risk masking a real one.
    """
    if circuit is None or not power_pin_names:
        return False
    pin_names = _circuit_pin_names(circuit)
    if not pin_names:
        return False
    return all(name in power_pin_names for name in pin_names)


def _boundary_supply_nets(
    circuit: Any, power_pin_names: frozenset[str]
) -> Iterator[Any]:
    """The **boundary-side** half of :func:`_layout_supply_net_names`'s two
    traversals: for every pin ``circuit`` itself declares whose name is a
    known power pin, the net that pin resolves to (issue #2136).

    This is the cell-interior view -- an extracted ``..._inv_1``'s own
    ``VPWR``/``VGND`` nets -- which :func:`_instance_supply_nets` alone
    never reaches, because the instance-side walk only ever sees nets in a
    *parent* circuit.
    """
    for pin in circuit.each_pin():
        pin_name = pin.name()
        if pin_name and pin_name.upper() in power_pin_names:
            yield circuit.net_for_pin(pin.id())


def _instance_supply_nets(
    circuit: Any, power_pin_names: frozenset[str]
) -> Iterator[Any]:
    """The **instance-side** half of :func:`_layout_supply_net_names`'s two
    traversals: for every subcircuit instance in ``circuit``, and every pin
    its master declares whose name is a known power pin, the net ``circuit``
    wires that pin to (issue #2136).

    This is the top-level power grid (the same edge set
    :func:`_power_pin_connections` walks for the ``power_connectivity``
    report), and it is the only evidence available in a circuit that does
    not itself declare a supply pin -- the case
    :func:`_boundary_supply_nets` cannot cover.
    """
    for sub in circuit.each_subcircuit():
        ref = sub.circuit_ref()
        if ref is None:
            continue
        for pin in ref.each_pin():
            pin_name = pin.name()
            if pin_name and pin_name.upper() in power_pin_names:
                yield sub.net_for_pin(pin.id())


def _supply_net_spellings(net: Any) -> set[str]:
    """Every upper-cased spelling under which ``net`` should be recognised
    as a supply net, or an empty set for an unnamed/``None`` net.

    Net names go through :func:`_name_or_none`, so a label-merged net is
    spelled exactly as ``net_correspondence[]``/``mismatches[].net`` spell
    it (``VPWR|VDD``); both the joined spelling and each ``|``-separated
    alias are returned, so a correspondence entry matches whichever of the
    two the compare-time net object reports.
    """
    name = _name_or_none(net)
    if not name:
        return set()
    spellings = {name.upper()}
    spellings.update(alias.upper() for alias in name.split("|") if alias)
    return spellings


def _layout_supply_net_names(
    layout_netlist: Any | None, power_pin_names: frozenset[str] | None
) -> frozenset[str] | None:
    """Every layout net name (upper-cased) that is demonstrably carrying a
    standard-cell **power/ground** pin, or ``None`` when there is not enough
    evidence to derive one (issue #2136).

    Structural, not name-pattern -- the same discipline
    :func:`_is_power_only_circuit` applies, and for the same
    PDK-independence reason (sky130's ``VPWR``/``VGND``/``VPB``/``VNB`` vs.
    gf180mcu's ``VDD``/``VSS``/``VNW``/``VPW``): a net qualifies only
    because something the *library* declares to be a power pin
    (``power_pin_names``, derived by :func:`_gate_level_power_pin_names`)
    actually lands on it. Two traversals, one helper each, both needed --
    neither subsumes the other, which is why this is a union and not a
    choice:

    * **Boundary side** -- :func:`_boundary_supply_nets`, the cell-interior
      view an instance-side walk never reaches.
    * **Instance side** -- :func:`_instance_supply_nets`, the top-level
      power grid, the only evidence in a circuit that declares no supply
      pin of its own.

    Each yielded net is recorded under every spelling
    :func:`_supply_net_spellings` gives it (the ``|``-joined label-merged
    form *and* each alias), so a correspondence entry matches whichever of
    the two the compare-time net object reports.

    Returns ``None`` for a falsy/``None`` ``power_pin_names`` (no derivable
    universe) or an unusable netlist, and an empty set when nothing
    qualifies -- callers treat both as "no evidence" and flag nothing, the
    same missing-evidence discipline :func:`_is_power_only_circuit` follows.
    """
    if layout_netlist is None or not power_pin_names:
        return None
    each_circuit = getattr(layout_netlist, "each_circuit", None)
    if not callable(each_circuit):
        return None
    names: set[str] = set()
    for circuit in each_circuit():
        for net in _boundary_supply_nets(circuit, power_pin_names):
            names.update(_supply_net_spellings(net))
        for net in _instance_supply_nets(circuit, power_pin_names):
            names.update(_supply_net_spellings(net))
    return frozenset(names)


def _supply_pin_universe(
    layout_netlist: Any | None,
    reference_netlist: Any | None,
    library_pin_orders: Mapping[str, list[str]] | None,
) -> _SupplyPinUniverse | None:
    """Bundle the two name sets :func:`_build_net_correspondence` needs to
    tell a verified net correspondence apart from a supply-net fallback
    (issue #2136), or ``None`` when either side yields no evidence.

    ``None`` is the answer for every ``reference.form`` other than
    ``"gate-level-verilog"`` (nothing licenses calling any pin name a power
    pin there -- the same restriction that scopes
    :func:`_prune_power_only_circuits` and the
    ``power_connectivity`` report), and for a ``gate-level-verilog`` run
    whose library pin orders could not be resolved. A ``None`` universe
    leaves ``net_correspondence[]`` byte-identical to what it was before
    this issue.
    """
    power_pin_names = _gate_level_power_pin_names(reference_netlist, library_pin_orders)
    if not power_pin_names:
        return None
    layout_supply_nets = _layout_supply_net_names(layout_netlist, power_pin_names)
    if not layout_supply_nets:
        return None
    return _SupplyPinUniverse(power_pin_names, layout_supply_nets)


def _apply_gate_level_port_aliases(
    port_aliases: Mapping[str, Mapping[str, str]],
    reference_netlist: Any,
) -> list[dict[str, Any]]:
    """Join a reference-side port's net onto its ``assign``-alias target's
    net, for every module a ``reference.form: "gate-level-verilog"``
    conversion carried a port-to-port (or port-to-internal-net) alias for
    (issue #2021, the other half of issue #1994's 77 false LVS errors).

    **The gap this closes.** ``convert_gate_level_verilog`` resolves an
    ``assign <alias> = <target>;`` statement transparently for every
    *instance* connection (see ``verilog_netlist.py``'s own docstring), but
    never for a module's own declared port list -- a ``.SUBCKT`` boundary
    pin whose only Verilog-level connection was an ``assign`` is emitted as
    its own pin, with nothing inside the body ever referencing its name
    (every instance that would have used it was rewritten to the alias's
    *target* instead). Reading that SPICE back with ``NetlistSpiceReader``
    creates exactly the isolated, zero-device/zero-terminal net that
    implies, which ``NetlistComparer`` then reports as an unmatched pin/net
    even when the layout is completely correct -- gate-level Verilog
    routinely carries a port-to-port alias like ``assign dbg_uart_byte[i] =
    rx_byte[i];``, where the layout has exactly one physical net serving
    both names.

    **Fix, applied here rather than in the SPICE text.** SPICE has no
    pin-alias primitive, and repeating one net name across two ``.SUBCKT``
    header positions would just drop the other, distinct declared name --
    so this runs *after* ``NetlistSpiceReader`` has built the real
    ``kdb.Circuit``/``kdb.Net``/``kdb.Pin`` objects, using
    ``Circuit.connect_pin()`` to move the alias port's declared pin onto its
    canonical target's net. Deliberately not ``Circuit.join_nets()``: that
    method merges the two nets' *pins* into one (renamed to a
    comma-joined string), which loses the alias port's own name entirely --
    verified directly against a real ``kdb.Netlist`` in this issue's own
    investigation. ``connect_pin`` instead leaves both declared pins in
    place, each still individually named, now both pointing at the same
    net -- the exact "one net, two named pins" shape a correctly-wired
    layout already has, so the two sides compare cleanly on both the
    pin-name match and the net match. The alias port's now-disconnected
    original net (zero terminals, zero pins, zero subcircuit pins once its
    pin is moved off it) is left for :func:`_purge_emptied_nets` to remove,
    exactly like every other emptied net this module already cleans up.

    ``port_aliases`` is
    :func:`klayout_tools.verilog_netlist.collect_gate_level_port_aliases`'s
    own return shape, ``{<module name>: {<alias port>: <canonical net>}}``
    -- every module ``_read_reference_netlist`` parsed with at least one
    port-to-port/port-to-net alias, empty (the common case) for every
    reference this fix does not apply to.

    A multi-hop alias chain (``assign c = b; assign b = a;``) collapses
    every alias port for one canonical target into a single
    :func:`_mismatch` entry and is applied correctly regardless of
    dict-iteration order: every alias in a group is looked up and moved
    onto the *same*, already-resolved ``canonical_net`` object, never
    re-resolved by name after an earlier move in the same group could have
    changed what that name refers to.

    Never raises: a module name that does not resolve to a circuit in
    ``reference_netlist``, or an alias/canonical port name that does not
    resolve to a net inside that circuit (should not happen in practice --
    the mapping was derived from the exact same parse
    ``convert_gate_level_verilog`` used to build the SPICE this netlist was
    read from -- but this is an internal consistency hook, not a
    caller-facing contract like ``hints.same_nets``), is silently skipped
    rather than treated as a request error.

    Returns one ``severity: "warning"``
    :data:`CATEGORY_TOPOLOGY_REFERENCE_PORT_ALIAS_JOINED` entry per joined
    canonical net (naming every alias port folded into it), so a
    ``"match"`` reached this way stays auditable -- the same disclosure
    discipline :func:`_apply_reference_device_bulk`/
    :func:`_apply_reference_placeholder_values` follow for their own
    reference-side fixups. Empty when ``port_aliases`` is empty (the common
    case).
    """

    from .lvs import CATEGORY_TOPOLOGY_REFERENCE_PORT_ALIAS_JOINED

    entries: list[dict[str, Any]] = []
    any_joined = False
    for module_name in sorted(port_aliases):
        aliases = port_aliases[module_name]
        if not aliases:
            continue
        circuit = reference_netlist.circuit_by_name(module_name)
        if circuit is None:
            continue
        # Group by canonical target so a multi-hop chain joins every alias
        # for one target in a single pass -- see the docstring above.
        groups: dict[str, list[str]] = {}
        for alias_name, canonical_name in sorted(aliases.items()):
            if alias_name == canonical_name:
                continue
            groups.setdefault(canonical_name, []).append(alias_name)
        for canonical_name in sorted(groups):
            alias_names = groups[canonical_name]
            canonical_net = circuit.net_by_name(canonical_name)
            if canonical_net is None:
                continue
            joined: list[str] = []
            for alias_name in alias_names:
                alias_pin = circuit.pin_by_name(alias_name)
                alias_net = circuit.net_by_name(alias_name)
                if alias_pin is None or alias_net is None or alias_net is canonical_net:
                    continue
                circuit.connect_pin(alias_pin, canonical_net)
                joined.append(alias_name)
            if not joined:
                continue
            any_joined = True
            entries.append(
                _mismatch(
                    CATEGORY_TOPOLOGY_REFERENCE_PORT_ALIAS_JOINED,
                    "warning",
                    f"reference circuit '{module_name}' declared port(s) "
                    f"{', '.join(sorted(joined))} via a plain 'assign "
                    f"<port> = ...;' alias (request.reference.form: "
                    f'"gate-level-verilog") -- joined onto the same net as '
                    f"'{canonical_name}' before comparing, matching a "
                    f"layout with a single physical net for these names, "
                    f"instead of reporting them as unmatched (see "
                    f"docs/cli/lvs.md, "
                    '"topology.reference_port_alias_joined")',
                    "reference",
                    circuit={"layout": None, "reference": module_name},
                    details={
                        "canonical_net": canonical_name,
                        "aliased_ports": sorted(joined),
                    },
                )
            )
    if any_joined:
        _purge_emptied_nets(reference_netlist)
    return entries


def _purge_named_circuits(netlist: Any, removed_names: Sequence[str]) -> None:
    """Remove every circuit named in ``removed_names`` from ``netlist``,
    along with every subcircuit instance of it, then purge any net left
    empty as a result -- the mechanical removal step shared by the
    layout-side and reference-side halves of
    :func:`_prune_power_only_circuits` (issues #1622, #2244). A no-op for an
    empty ``removed_names``.

    Every instance of a to-be-removed circuit type must be detached first --
    ``Netlist.purge_circuit`` refuses (leaves a dangling reference behind)
    if the circuit still has callers.
    """
    removed_set = set(removed_names)
    if not removed_set:
        return
    for circuit in list(netlist.each_circuit()):
        for sub in list(circuit.each_subcircuit()):
            ref = sub.circuit_ref()
            if ref is not None and ref.name in removed_set:
                circuit.remove_subcircuit(sub)
    for name in removed_names:
        target = netlist.circuit_by_name(name)
        if target is not None:
            netlist.purge_circuit(target)
    # Issue #500's precedent: a net left with no terminals/pins/subcircuit
    # pins once its only connection (the just-removed instance) is gone is
    # not part of the netlist's topology by any definition -- see
    # `_purge_emptied_nets`'s own docstring.
    _purge_emptied_nets(netlist)


def _reference_power_only_masters(
    reference_netlist: Any,
    library_pin_orders: Mapping[str, list[str]] | None,
    power_pin_names: frozenset[str] | None,
    *,
    keep_circuit: Any | None = None,
) -> list[str]:
    """The sorted names of every circuit in ``reference_netlist`` that is a
    power-only library master -- the reference-side counterpart to
    :func:`_is_power_only_circuit` (issue #2244).

    **Cannot reuse `_is_power_only_circuit` directly.** That function
    classifies a circuit by its *own* declared pins, which is correct for a
    layout-side abstraction (whose declared pin list is the real PDK pin
    order, e.g. ``klt extract --abstract-cells``' output) but wrong for a
    ``gate-level-verilog`` reference's own ``.SUBCKT`` stub for the same
    cell: :func:`~klayout_tools.verilog_netlist.convert_gate_level_verilog`
    only ever emits the pins the Verilog actually connects (see that
    module's "No power/ground pins" note), so a power-only master's
    reference-side stub can declare **zero** pins at all when every instance
    leaves every pin unconnected -- exactly the shape a DEF-derived
    ``write_verilog`` reference produces for a filler/tap/endcap instance
    (``library__fill_4 FILLER_0_10 ();``, an empty connection list, because
    the cell has no signal pins to connect in the first place). Zero
    declared pins is what :func:`_is_power_only_circuit` treats as "no
    evidence" and refuses to prune (the same discipline that keeps a
    genuinely unresolvable circuit from being masked).

    So this checks the *library's* full declared pin order for the same
    cell name instead -- the same ``library_pin_orders`` lookup
    :func:`_gate_level_power_pin_evidence` already performs to find which
    masters the reference instantiates at all, reused here rather than
    re-derived. A circuit qualifies when its name resolves to a library
    cell whose full declared pin order is non-empty and is a subset of
    ``power_pin_names``; ``keep_circuit`` (the caller's own resolved
    reference top circuit, when given) is excluded by identity, mirroring
    :func:`_prune_power_only_circuits`'s layout-side guard for the same
    "never prune the compare target itself" reason.
    """
    if not library_pin_orders or not power_pin_names:
        return []
    by_upper_name = {
        str(cell).upper(): pins for cell, pins in library_pin_orders.items()
    }
    names: set[str] = set()
    for circuit in reference_netlist.each_circuit():
        if circuit == keep_circuit:
            continue
        pins = by_upper_name.get(str(circuit.name).upper())
        if not pins:
            continue
        pin_set = {pin.upper() for pin in pins if pin}
        if pin_set and pin_set <= power_pin_names:
            names.add(str(circuit.name))
    return sorted(names)


def _power_only_pruned_mismatch(
    removed_layout_names: Sequence[str], removed_reference_names: Sequence[str]
) -> dict[str, Any]:
    """Build the single ``topology.power_only_pruned`` disclosure for
    :func:`_prune_power_only_circuits`'s layout-side removals,
    reference-side removals, or both (issue #2244) -- always exactly one
    entry regardless of how many circuits were removed or from which
    side(s), so a ``"match"`` reached this way stays auditable without the
    entry count itself becoming a second signal.
    """

    from .lvs import CATEGORY_TOPOLOGY_POWER_ONLY_PRUNED

    total = len(removed_layout_names) + len(removed_reference_names)
    if removed_layout_names and removed_reference_names:
        side = "both"
        description = (
            "removed circuit(s) whose every declared pin is a power/ground "
            "pin of the reference library, and every instance of them, from "
            "both the layout and the reference before comparing -- the "
            "reference itself instantiated a power-only master (e.g. a "
            "DEF-derived write_verilog filler/tap/endcap instance with no "
            f"signal connections) -- {total} circuit(s): layout "
            f"{', '.join(removed_layout_names)}; reference "
            f"{', '.join(removed_reference_names)} (see docs/cli/lvs.md, "
            '"topology.power_only_pruned")'
        )
    elif removed_layout_names:
        side = "layout"
        description = (
            "removed layout-side circuit(s) whose every declared pin is a "
            "power/ground pin of the reference library (a pin the library "
            "declares but the gate-level-Verilog reference never carries), "
            f"and every instance of them, before comparing -- {total} "
            f"circuit(s): {', '.join(removed_layout_names)} (see "
            'docs/cli/lvs.md, "topology.power_only_pruned")'
        )
    else:
        side = "reference"
        description = (
            "removed reference-side circuit(s) whose every declared pin "
            "(per the reference library's own .subckt declaration) is a "
            "power/ground pin -- the reference itself instantiated a "
            "power-only master (e.g. a DEF-derived write_verilog "
            "filler/tap/endcap instance with no signal connections) that "
            "has no counterpart on the layout side -- and every instance "
            f"of them, before comparing -- {total} circuit(s): "
            f"{', '.join(removed_reference_names)} (see docs/cli/lvs.md, "
            '"topology.power_only_pruned")'
        )
    return _mismatch(CATEGORY_TOPOLOGY_POWER_ONLY_PRUNED, "warning", description, side)


def _prune_power_only_circuits(
    layout_netlist: Any,
    reference_netlist: Any,
    library_pin_orders: Mapping[str, list[str]] | None,
    *,
    layout_keep_name: str | None = None,
    reference_keep_name: str | None = None,
) -> dict[str, Any] | None:
    """Issue #1622 (layout side) / #2244 (reference side): remove every
    circuit whose entire declared pin list is power/ground -- a filler/
    tap-cell master, e.g. sky130's unconditionally-inserted
    ``tapvpwrvgnd_1`` tap cell (``place_and_route.py``'s ``_TAPCELL_CELLS``)
    or a ``fill_*`` row-gap filler (issue #1442) -- along with every
    subcircuit instance of it, from **whichever side(s) actually
    instantiate it** before the compare runs.

    **Why removal, not just suppressing its own mismatch report.** A
    ``klt place-and-route`` ``verilog_path`` reference conversion never
    instantiates a cell with no logic function at all (see
    ``verilog_netlist.py``'s "No power/ground pins" note), so a power-only
    layout circuit has nothing on that reference to describe it. Filtering
    the resulting finding out of ``mismatches[]`` after the fact (the
    obvious first instinct, and what a ``_build_mismatches``-level check
    would do) fixes nothing: ``status`` is always derived from
    ``compare()``'s own boolean result, never re-derived from
    ``mismatches[]`` (see this module's docstring and ``run_lvs``), so the
    verdict would stay ``"mismatch"``. It is also not the only finding --
    left in place, the *containing* circuit fails to verify too:
    ``NetlistComparer`` cannot pair the container's subcircuit-instance list
    against the other side's (one side has an extra instance the other
    cannot describe), and reports a second, consequential ``circuit could
    not be matched to a counterpart`` finding for the *container* (KLayout's
    own ``circuit_skipped`` event, both sides present). Removing the circuit
    and its instances before ``compare()`` ever runs is the only thing that
    makes the container's own comparison clean and the verdict ``"match"``.

    **Not every ``write_verilog`` consumer honors the "never instantiates a
    power-only cell" assumption** (issue #2244). A gate-level netlist
    produced by reading a routed DEF back through ``write_verilog`` (or any
    other DEF-derived netlist writer, as opposed to ``klt
    place-and-route``'s own ``verilog_path`` writer) contains a
    filler/tap/endcap instantiation line per placed physical-only component,
    each with an empty connection list (``library__fill_4 FILLER_0_10
    ();``). Read back through
    :func:`~klayout_tools.verilog_netlist.convert_gate_level_verilog`, each
    such cell type becomes its own zero-pin ``.SUBCKT`` stub on the
    *reference* side -- a power-only master the freshly-layout-pruned
    layout side can no longer match, surfacing as a ``topology`` "circuit
    could not be matched to a counterpart" error cascade (one per
    reference type, one per reference instance) stranded around an
    otherwise-clean ``power_connectivity: "match"`` verdict. This function
    therefore prunes symmetrically: a master's type and every instance of it
    are removed from the layout side, the reference side, or both,
    whichever actually instantiate it -- never just one side unconditionally.

    Mirrors the known-safe downstream workaround issue #1622 cites
    (2AMLogic/sky130-fpga issue #20: stripping a layout-side ``.SUBCKT``
    whose pin list is a non-empty subset of ``{VPWR, VGND, VPB, VNB}``, plus
    its instance lines, from the extracted SPICE netlist before calling
    ``klt lvs``) -- implemented natively here, and structurally
    (:func:`_is_power_only_circuit`/:func:`_reference_power_only_masters`'s
    pin-list checks against the power-pin universe
    :func:`_gate_level_power_pin_names` derives from the PDK library's own
    pin-order data) rather than a hardcoded, per-PDK power-pin name table or
    a cell-name glob.

    **Scoped to ``reference.form: "gate-level-verilog"`` only** -- the caller
    invokes this for that form alone, and that restriction is load-bearing,
    not caution. ``library_pin_orders`` (the resolved standard-cell
    library's full ``.subckt`` pin orders) exists only for that form, and
    the power-pin derivation it feeds is sound only because a
    ``gate-level-verilog`` reference is a *conversion of the same design*
    through that known library, guaranteed never to carry a power pin's
    *connection* (whether or not it emits the pin itself). A
    ``"plain-element"``/``"subckt-call"`` reference is arbitrary SPICE with
    no library behind it, so nothing there licenses calling any pin name a
    power pin -- applying an unscoped version of this masked a genuine
    unmatched signal circuit (a layout-only ``.SUBCKT`` with pins
    ``A``/``Y`` against a flat reference declaring only ``IN``/``OUT``
    vanished from the report), which
    ``test_run_lvs_power_only_pruning_is_scoped_to_gate_level_verilog``
    now guards against.

    Note this classification is broader than "filler/tap" by design: it
    also catches the other *purely* physical cells a P&R flow inserts
    without the logic netlist knowing -- decoupling capacitors, whose PDK
    pin list is supplies and well ties only -- which are equally invisible
    to a signal-only compare and equally not a topology defect. It stops
    exactly where the evidence does: a physical-only cell that declares
    any non-power pin (e.g. sky130's antenna diode, whose pin list
    includes ``DIODE``) is **not** pruned, because nothing in the data
    establishes that pin as power/ground. That is the intended direction of
    error -- an un-pruned cell costs a reported topology mismatch, a
    wrongly-pruned one masks a real defect. Every removal is named in the
    returned disclosure, so a ``"match"`` that depended on one stays
    auditable from the report alone.

    ``layout_keep_name``/``reference_keep_name`` (the caller's own
    ``layout.top``/``reference.top``, when given) are never pruned even if
    they happen to classify as power-only -- this runs before
    ``_select_circuit`` resolves the actual compare targets, and removing
    the very circuit the caller asked to compare would turn a
    classification edge case into a confusing "circuit not found" error
    instead of a clean no-op. Not expected to matter in practice (a real
    design's top circuit always has genuine signal I/O), but cheap to guard
    against.

    Returns a single ``severity: "warning"`` ``mismatches[]`` entry
    (``category: "topology.power_only_pruned"``, built by
    :func:`_power_only_pruned_mismatch`) naming every circuit removed from
    every side pruned, so a ``"match"`` reached this way is never silently
    indistinguishable from one reached against either netlist's original,
    unpruned shape -- or ``None`` when nothing was power-only on either side
    (the common case, and always the case when there is no derivable
    power-pin universe, e.g. a ``reference_netlist`` with no library-cell
    circuits at all), in which case there is nothing to disclose.
    """
    power_pin_names = _gate_level_power_pin_names(reference_netlist, library_pin_orders)
    if not power_pin_names:
        return None
    # Resolved through the netlist's own `circuit_by_name` (whatever
    # case-folding convention it applies -- e.g. a `NetlistSpiceReader`
    # netlist matches `"top"` against a circuit actually named `"TOP"`) and
    # compared by object equality -- a plain name-string comparison against
    # `circuit.name` would miss that case-folding and could let this guard
    # silently fail to protect the real top circuit. Deliberately *not*
    # `cell_index`-based (unlike `_prune_extra_top_circuits`'s own
    # object-identity check): a `kdb.Netlist` read straight from SPICE/
    # Verilog text, with no backing `kdb.Layout`, reports `cell_index == 0`
    # for every circuit -- a real, pre-existing gap in that comparison,
    # tracked separately (issue #1657) rather than fixed here (out of this
    # issue's own scope) -- so this guard uses `==`, which compares
    # correctly in that same no-Layout-backing case.
    layout_keep_circuit = (
        layout_netlist.circuit_by_name(layout_keep_name)
        if layout_keep_name is not None
        else None
    )
    removed_layout_names = sorted(
        {
            circuit.name
            for circuit in layout_netlist.each_circuit()
            if circuit != layout_keep_circuit
            and _is_power_only_circuit(circuit, power_pin_names)
        }
    )
    # Only resolved (and only matters) when there is something to guard --
    # keeps this call site usable against a lightweight `each_circuit()`-only
    # test double that does not implement `circuit_by_name` when no reference
    # top name is given (see `_reference_power_only_masters`'s own tests).
    reference_keep_circuit = (
        reference_netlist.circuit_by_name(reference_keep_name)
        if reference_keep_name is not None
        else None
    )
    removed_reference_names = _reference_power_only_masters(
        reference_netlist,
        library_pin_orders,
        power_pin_names,
        keep_circuit=reference_keep_circuit,
    )
    if not removed_layout_names and not removed_reference_names:
        return None
    _purge_named_circuits(layout_netlist, removed_layout_names)
    _purge_named_circuits(reference_netlist, removed_reference_names)
    return _power_only_pruned_mismatch(removed_layout_names, removed_reference_names)
