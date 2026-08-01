"""First-order lumped RC parasitic extraction for ``klt extract --parasitics``.

This is the implementation issue (#217) of the interface decision recorded in
``docs/design/lvs-extraction-spike.md`` -> "Addendum (#216): parasitic (RC)
extraction interface decision" (#216). Read that addendum first: it fixes the
interface (a flag on ``klt extract``, not a new verb), the scope of the first
cut (lumped series R + lumped C-to-ground, **no** net-to-net coupling), the
accuracy posture (fixed, not tunable), and the output rule (additive only --
one SPICE circuit body, still directly consumable by ``klt sim``).

Engine survey (the decision the addendum deferred to this issue)
================================================================

The addendum left one question open: build a first-order lumped reduction on
top of ``LayoutToNetlist``'s per-net geometry (its option 1), or wrap an
existing open-source PEX layer (its option 2). Surveyed live against the pip
``klayout`` 0.30.10 this repo already pins, and against the candidate
external engines, the answer is **option 1**:

- **KLayout ``db``: no interconnect PEX to wrap.** Re-confirmed by direct
  API audit: ``db.LayoutToNetlist`` has no ``extract_rc``-shaped call, and
  ``db.DeviceExtractorResistor``/``...Capacitor`` recognise *drawn* resistor
  and capacitor **devices** (a PDK's poly resistor, a MiM cap), not the
  parasitics of ordinary interconnect. Exactly as the addendum found.
- **KLayout ``pex``: a resistance-network extractor, but not a fit for a
  *lumped* first cut.** Newer than the addendum's audit (which only probed
  ``db``): pip ``klayout`` >= 0.30.2 ships a separate ``klayout.pex`` module
  (``RNetExtractor``/``RExtractorTech``/``RNetwork``) that builds a
  *distributed* per-net resistor mesh by square counting or triangulation,
  given per-conductor sheet resistance and per-via resistance. It is
  recorded here because it is the natural engine for a **later**, distributed
  increment -- but it is not what this issue asks for: it solves only the R
  half (there is no capacitance counterpart), it emits a many-node network
  rather than the lumped reduction #216 fixed as the first cut, and it would
  raise this repo's ``klayout>=0.29`` floor. The curated sheet-resistance
  table this module introduces is exactly the input that engine needs, so
  adopting it later is additive, not a rewrite.
- **KLayout-PEX (``iic-jku/klayout-pex``, `pip install klayout-pex`) --
  the strongest external candidate, and still out.** A genuine, actively
  maintained (2026-08 commits), headless-scriptable PEX tool built on
  KLayout. Three disqualifiers, in order: (1) **licence** -- GPL-3.0-or-later,
  failing the MIT/Apache/BSD bar #217 sets for a wrapped PEX layer (this repo
  is MIT and imports GPL KLayout as a separate process-boundary-free
  dependency already; adding a second, GPL-only *Python* dependency is not a
  call this issue authorises); (2) **its engines are not self-contained** --
  the FasterCap and MAGIC engines each require a non-pip external binary
  (and MAGIC drags in the second geometry engine + Tcl runtime that
  ``lvs-extraction-spike.md`` section 1 already rejected), while its native
  "2.5D" engine is marked *under development*; (3) **shape mismatch** -- it
  is a capacitance-first tool that emits its own artifacts, where this issue
  needs R **and** C folded into the one ``.subckt`` body ``klt sim``
  consumes. Worth re-surveying when a friction log demands field-solver
  accuracy; not the first cut.
- **magic ``extresist``/``ext2spice`` and OpenROAD's OpenRCX** -- both real,
  both rejected for reasons this repo has already settled: magic is a second
  geometry engine driven by Tcl (``lvs-extraction-spike.md`` section 1),
  and OpenRCX (BSD-3-Clause, inside the OpenROAD application) operates on
  OpenDB/LEF-DEF with a calibrated ``extRules`` file -- i.e. it needs a
  routed DEF and a full OpenROAD install, not a GDS stream, so it cannot sit
  behind ``klt extract <file.gds>`` at all.

So: build it, from the per-net geometry KLayout already gives us, over a
curated per-PDK coefficient table -- the same "curate what the PDK doesn't
hand us as a native KLayout deck" pattern as the DRC decks and
``pdk_models``' device-model table. The coefficients themselves are *not*
invented: they are transcribed from each PDK's public magic technology file
(see ``decks/sky130.py`` / ``decks/gf180mcu.py``, which cite file, section
and units).

The model (deliberately first-order, and its limits)
====================================================

For every net in the extracted top circuit, and every conductor layer with
curated coefficients (poly + each metal level -- diffusion is excluded
because the device models already carry it):

- **Capacitance to ground.** ``C = area_um2 * area_cap + perimeter_um *
  perimeter_cap``, summed over layers, using the net's own merged shapes on
  each layer. Area/fringe capacitance to the substrate only.
- **Resistance.** ``R = squares * sheet_rho``, summed over layers, where the
  square count of each merged polygon is taken from its *equivalent
  rectangle*: the ``L``/``W`` pair with the polygon's own area and perimeter
  (``L*W = A``, ``2*(L+W) = P``), giving ``squares = L/W``. A shape too
  compact for that system to have a real solution (a pad or a via landing,
  where ``P^2 < 16*A``) counts as one square. Polygons on a layer sum in
  series.

Both are attached as **one lumped RC load per net**: a resistor from the net
to a new internal node, and a capacitor from that internal node to the deck's
substrate net::

    <net> --[ R = total wire resistance ]-- <net>_par --[ C = total wire cap ]-- vsubs

This shape is chosen because it is *additive in the strict sense the design
record requires*: no existing net is split and no device terminal is
re-parented, so every device-to-net relationship, every net name, and every
pin in the schematic-equivalent netlist is bit-for-bit what it was without
``--parasitics``. What it models is the net's own RC charging behaviour (a
driver sees the net's total wire capacitance through its total wire
resistance).

Stated limits, so no caller mistakes this for sign-off-grade PEX:

- The series R is **not** in the driver-to-receiver signal path, so IR drop
  along a wire and receiver-side wire delay are not modelled. Placing R in
  the signal path requires partitioning a net's terminals into "before" and
  "after" groups, and a lumped, single-R model has no non-arbitrary basis
  for that partition -- a distributed increment (see ``klayout.pex`` above)
  is the honest way to get it.
- **Net-to-net coupling capacitance is not modelled at all** -- explicitly
  deferred by #216 to a second increment.
- Contact/via resistance is not modelled.
- One fixed accuracy/runtime point, no ``fast``/``accurate`` selector (#216).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .decks import ExtractionDeck, ParasiticLayer, ParasiticTech

if TYPE_CHECKING:
    import klayout.db as kdb

#: Device-class name of the generated series resistors. Reported in the JSON
#: response's `parasitics` block, and used by `extract.py` to keep parasitic
#: elements *out* of the `devices[]`/`device_counts` view (whose meaning is
#: unchanged by `--parasitics`, per docs/cli/extract.md).
RESISTOR_CLASS = "parasitic_r"

#: Device-class name of the generated capacitors-to-ground (see above).
CAPACITOR_CLASS = "parasitic_c"

#: Name of the model this module implements, echoed as `parasitics.model` in
#: the JSON response so a consumer can tell which reduction produced the
#: numbers if a second one is ever added.
MODEL_NAME = "lumped_rc_load"

#: Suffix appended to a net's name to build its internal RC node (uniquified
#: with a numeric suffix if a net of that name already exists).
_INTERNAL_NET_SUFFIX = "_par"

#: Decimal places the reported ohm/femtofarad values are rounded to. Keeps
#: JSON output byte-stable across platforms without losing meaningful
#: precision (1e-6 fF is an attofarad-scale rounding, far below the model's
#: own accuracy).
_VALUE_PRECISION = 6


@dataclass
class NetParasitics:
    """The lumped values computed for one net."""

    name: str
    resistance_ohm: float
    capacitance_ff: float


@dataclass
class ParasiticsResult:
    """What :func:`annotate_parasitics` added to a netlist.

    ``per_net`` maps a net's expanded name to its lumped values (nets with no
    geometry on any curated layer are absent). ``internal_net_names`` is the
    set of nodes this pass created, which ``extract.py`` filters out of the
    response's ``nets[]`` view so that view keeps its documented meaning.
    """

    per_net: dict[str, NetParasitics] = field(default_factory=dict)
    internal_net_names: set[str] = field(default_factory=set)
    resistor_count: int = 0
    capacitor_count: int = 0
    ground_net: str = ""
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """The response's ``parasitics`` block (see ``docs/cli/extract.md``)."""
        return {
            "model": MODEL_NAME,
            "ground_net": self.ground_net,
            "net_count": len(self.per_net),
            "resistor_count": self.resistor_count,
            "capacitor_count": self.capacitor_count,
            "total_resistance_ohm": round(
                sum(entry.resistance_ohm for entry in self.per_net.values()),
                _VALUE_PRECISION,
            ),
            "total_capacitance_ff": round(
                sum(entry.capacitance_ff for entry in self.per_net.values()),
                _VALUE_PRECISION,
            ),
        }


def build_parasitic_layers(
    deck: ExtractionDeck,
    tech: ParasiticTech,
    poly_region: kdb.Region,
    metal_regions: list[kdb.Region],
    gate_regions: list[kdb.Region],
) -> list[tuple[str, kdb.Region, ParasiticLayer, kdb.Region | None]]:
    """Pair each curated coefficient set with the ``LayoutToNetlist`` layer it
    describes.

    Returns ``(role, region, coefficients, exclusion)`` tuples, where
    ``region`` is the registered layer to ask for per-net shapes and
    ``exclusion`` is geometry to subtract from a net's shapes before measuring
    it (used for poly, see below), or ``None``.

    The poly exclusion is the transistor gate area: both PDKs' own extraction
    rules exclude ``fet`` types from *parasitic* capacitance because the
    device model already carries the gate capacitance (and the gate is over
    the channel, not over the substrate the curated coefficients describe).
    Subtracting it here is the geometric equivalent, done on the per-net
    ``Region`` rather than by registering another ``LayoutToNetlist`` layer,
    so the connectivity graph the schematic-equivalent extraction produced is
    untouched.

    Raises :class:`ValueError` if ``tech.metals`` is not positionally aligned
    with ``deck.metals`` -- a deck/tech drift that must fail loudly rather
    than silently attribute one metal level's coefficients to another.
    """
    import klayout.db as kdb

    if len(tech.metals) != len(deck.metals):
        raise ValueError(
            f"parasitics table has {len(tech.metals)} metal entries but the "
            f"extraction deck has {len(deck.metals)} metal layers"
        )

    layers: list[tuple[str, kdb.Region, ParasiticLayer, kdb.Region | None]] = []
    if tech.poly is not None:
        gates = kdb.Region()
        for region in gate_regions:
            gates += region
        layers.append(("poly", poly_region, tech.poly, gates.merged()))
    for index, coefficients in enumerate(tech.metals):
        if coefficients is None:
            continue
        layers.append((f"metal{index}", metal_regions[index], coefficients, None))
    return layers


def annotate_parasitics(
    l2n: kdb.LayoutToNetlist,
    netlist: kdb.Netlist,
    circuit_name: str,
    layers: list[tuple[str, kdb.Region, ParasiticLayer, kdb.Region | None]],
    dbu: float,
    ground_net_name: str,
) -> ParasiticsResult:
    """Compute the lumped RC of every net in ``circuit_name`` and add the
    matching R/C elements to ``netlist`` in place.

    ``l2n`` must still be the live extractor that produced ``netlist`` (the
    per-net shape lookup goes through it), and ``layers`` comes from
    :func:`build_parasitic_layers`. ``ground_net_name`` is the deck's
    substrate net, i.e. what every generated capacitor returns to; when the
    extraction produced no such net (a layout with no NMOS never creates the
    substrate global) it is created *and* promoted to a pin, with a
    ``warnings`` entry recording that -- see the inline comment for why.

    Nets are processed in sorted name order and elements are named after
    their net, so repeated runs against the same layout produce a
    byte-identical netlist -- the same determinism guarantee the rest of
    ``klt extract``'s output makes.
    """
    import klayout.db as kdb

    result = ParasiticsResult(ground_net=ground_net_name)

    circuit = netlist.circuit_by_name(circuit_name)
    if circuit is None:  # nothing survived `purge()` -- nothing to annotate
        return result

    measured: list[tuple[str, kdb.Net, float, float]] = []
    for net in circuit.each_net():
        resistance_ohm, capacitance_ff = _net_rc(l2n, net, layers, dbu)
        if resistance_ohm <= 0.0 and capacitance_ff <= 0.0:
            continue
        measured.append(
            (
                net.expanded_name(),
                net,
                round(resistance_ohm, _VALUE_PRECISION),
                round(capacitance_ff, _VALUE_PRECISION),
            )
        )
    if not measured:
        return result
    measured.sort(key=lambda entry: entry[0])

    resistor_class = kdb.DeviceClassResistor()
    resistor_class.name = RESISTOR_CLASS
    capacitor_class = kdb.DeviceClassCapacitor()
    capacitor_class.name = CAPACITOR_CLASS
    netlist.add(resistor_class)
    netlist.add(capacitor_class)

    ground_net = circuit.net_by_name(ground_net_name)
    if ground_net is None:
        ground_net = circuit.create_net(ground_net_name)
    # Every generated capacitor returns to this net, so it must be tieable
    # from a testbench. It normally already is (`make_top_level_pins()`
    # promotes the deck's substrate global, which any layout with an NMOS
    # has); when it is not -- e.g. a PMOS-only layout, where no NMOS body
    # terminal ever created the global -- promote it, and say so, rather
    # than leaving the capacitance hanging on an unreachable internal node
    # that would make the netlist unsimulatable.
    if not _is_pin(circuit, ground_net):
        pin = circuit.create_pin(ground_net_name)
        circuit.connect_pin(pin.id(), ground_net)
        result.warnings.append(
            f"parasitics: substrate net '{ground_net_name}' was not a "
            "top-level pin; promoted so the extracted capacitance has a "
            "tieable ground reference (this adds one pin to the .SUBCKT "
            "interface, only under --parasitics)"
        )

    existing_names = {net.expanded_name() for net in circuit.each_net()}
    for name, net, resistance_ohm, capacitance_ff in measured:
        internal_name = _unique_net_name(name, existing_names)
        existing_names.add(internal_name)
        internal_net = circuit.create_net(internal_name)
        result.internal_net_names.add(internal_name)

        resistor = circuit.create_device(resistor_class, name)
        resistor.set_parameter("R", resistance_ohm)
        resistor.connect_terminal("A", net)
        resistor.connect_terminal("B", internal_net)
        result.resistor_count += 1

        capacitor = circuit.create_device(capacitor_class, name)
        # `C` is in farads on KLayout's capacitor device class; the model's
        # own unit is the femtofarad (what the JSON reports).
        capacitor.set_parameter("C", capacitance_ff * 1e-15)
        capacitor.connect_terminal("A", internal_net)
        capacitor.connect_terminal("B", ground_net)
        result.capacitor_count += 1

        result.per_net[name] = NetParasitics(
            name=name,
            resistance_ohm=resistance_ohm,
            capacitance_ff=capacitance_ff,
        )

    return result


def _is_pin(circuit: kdb.Circuit, net: kdb.Net) -> bool:
    """Whether ``net`` is exposed as a pin of ``circuit``."""
    for pin in circuit.each_pin():
        pin_net = circuit.net_for_pin(pin.id())
        if pin_net is not None and pin_net.expanded_name() == net.expanded_name():
            return True
    return False


def _net_rc(
    l2n: kdb.LayoutToNetlist,
    net: kdb.Net,
    layers: list[tuple[str, kdb.Region, ParasiticLayer, kdb.Region | None]],
    dbu: float,
) -> tuple[float, float]:
    """``(resistance_ohm, capacitance_ff)`` for one net, summed over layers."""
    resistance_ohm = 0.0
    capacitance_af = 0.0

    for _role, layer_region, coefficients, exclusion in layers:
        shapes = l2n.shapes_of_net(net, layer_region, True).merged()
        if exclusion is not None:
            shapes = shapes - exclusion
        if shapes.is_empty():
            continue

        area_um2 = shapes.area() * dbu * dbu
        perimeter_um = shapes.perimeter() * dbu
        capacitance_af += (
            area_um2 * coefficients.area_cap_af_per_um2
            + perimeter_um * coefficients.perimeter_cap_af_per_um
        )
        resistance_ohm += _squares(shapes, dbu) * coefficients.sheet_ohm_per_sq

    return resistance_ohm, capacitance_af / 1000.0


def _squares(region: kdb.Region, dbu: float) -> float:
    """Equivalent square count of a merged ``Region`` (see the module
    docstring's "The model").

    Each polygon is reduced to the rectangle with its own area ``A`` and
    perimeter ``P``: ``L``/``W`` are the roots of ``x^2 - (P/2)x + A``, and
    the polygon contributes ``L/W`` squares. When ``P^2 < 16A`` (a shape too
    compact to be any rectangle -- a contact landing pad, say) there is no
    real solution and the polygon counts as a single square. Polygons on the
    same layer are summed, i.e. treated as being in series, which is the
    conservative reading for a wire that a lumped model cannot trace.
    """
    total = 0.0
    for polygon in region.each_merged():
        area_um2 = polygon.area() * dbu * dbu
        perimeter_um = polygon.perimeter() * dbu
        if area_um2 <= 0.0 or perimeter_um <= 0.0:
            continue
        half = perimeter_um / 2.0
        discriminant = half * half - 4.0 * area_um2
        if discriminant <= 0.0:
            total += 1.0
            continue
        root = math.sqrt(discriminant)
        length = (half + root) / 2.0
        width = (half - root) / 2.0
        if width <= 0.0:
            total += 1.0
            continue
        total += length / width
    return total


def _unique_net_name(net_name: str, taken: set[str]) -> str:
    """``<net_name>_par``, uniquified against ``taken`` so a layout that
    already labels a net ``FOO_par`` cannot collide with the internal node
    generated for ``FOO``."""
    candidate = f"{net_name}{_INTERNAL_NET_SUFFIX}"
    if candidate not in taken:
        return candidate
    index = 2
    while f"{candidate}{index}" in taken:
        index += 1
    return f"{candidate}{index}"
