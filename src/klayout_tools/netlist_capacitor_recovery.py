"""Recover a capacitor device's real device-class name across a
``klt extract``-written, bare-``C``-card SPICE round trip (issue #1876).

**Background.** Issue #1558 stopped ``klt extract -o netlist.spice`` from
writing an unbound capacitor's device-class name as a trailing (4th) token
on its ``C`` card (``C$1 a b 1.19e-13 <class>``), because ngspice's native
``C``-element parser reads that trailing token as a required capacitor
``.model`` reference -- and no deck's internal capacitor-class label is a
real ``.model``/``.subckt`` name, so the old card could never actually be
simulated. The card is now bare and value-only (``C$1 a b 1.19e-13``), which
is correct for simulatability, but leaves nothing in the card's own text for
``kdb.NetlistSpiceReader`` to recover the capacitor's real class from on
read-back -- it registers the device under KLayout's own generic/anonymous
capacitor class (name ``"CAP"``) instead. That breaks ``klt lvs``'s
name-based device-class correspondence whenever the *other* side of the
compare (a hand-authored/schematic-derived reference, or a layout netlist
extracted by a pre-#1558 ``klt``) names the capacitor's class explicitly.

**The fix.** ``kdb.NetlistSpiceWriter`` itself (not ``klt`` code) already
emits one ``* device instance <name> <transform...> <x,y> [<class>]``
comment line immediately before *every* device card it writes, trailing
class name included -- see
:func:`klayout_tools.pdk_models.create_model_binding_delegate`'s own
docstring and ``tests/test_extract.py``'s
``test_pdk_unbound_mim_flavour_writes_bare_value_only_capacitor_card``,
which locks the exact comment text. This module scans a SPICE netlist's own
text for that comment (:func:`parse_capacitor_class_comments`) and builds a
``kdb.NetlistSpiceReaderDelegate``-backed reader
(:func:`make_capacitor_class_recovery_reader`) that reattaches the recovered
class to an otherwise-bare ``C`` card's device -- instead of falling back to
KLayout's generic ``CAP`` class -- while deferring every other device/card to
KLayout's own default reading behaviour, byte-for-byte unchanged.

A missing or malformed comment (e.g. a hand-edited SPICE file, or a bare
``C`` card KLayout's own default writer -- not ``klt`` -- produced without a
preceding device-instance comment at all) degrades gracefully: the device
simply reads as the generic ``CAP`` class, exactly as it did before this
module existed. This mechanism never changes what is written to disk -- only
how ``klt`` itself re-reads a netlist for ``klt lvs``'s pre-extracted
``layout.netlist`` shape and reference-netlist reads; the bare, value-only
``C`` card format issue #1558 fixed is completely unaffected.

**Custom device classes round-tripped as ``X`` subcircuit calls (issue
#1942).** A device class ``klt extract`` recognises through a custom
``kdb.GenericDeviceExtractor`` -- today, exactly
:class:`~klayout_tools.decks.MomCapacitorDevice` (IHP's ``cap_cmomi``/
``cap_cmomf`` MoM capacitors, issue #1466) -- has no native SPICE element
letter (it is not MOS/resistor/capacitor/bipolar/diode-shaped), so
``kdb.NetlistSpiceWriter`` writes it as an ``X`` subcircuit-call card (e.g.
``XD_$1 A B cap_cmomi PARAMS: W=1 L=2``). Read back through a plain
``kdb.NetlistSpiceReader()`` (no delegate, or this module's own capacitor-
only delegate before this extension), an ``X`` card naming an undefined
subcircuit synthesises an *abstract circuit* whose parameters are baked into
its own mangled name (``CAP_CMOMI(L=2,W=1)``) -- the device is then compared
by that circuit-name string, never as a device: it is invisible in the
device census, ``options.parameter_tolerance`` never reaches it, and any
mismatch degrades to a generic ``circuit could not be matched to a
counterpart`` triple with no device/parameter/net name.

:func:`custom_device_classes_for_deck` reads the same
``ExtractionDeck.mom_capacitors`` table :func:`klayout_tools.extract
._build_mom_capacitor_extractor` (the layout-extraction side) registers its
device classes from, and :func:`make_capacitor_class_recovery_reader`'s
``custom_device_classes`` parameter wires a ``wants_subcircuit``/``element``
override that recognises an ``X`` card naming one of those classes and
creates a real :class:`~klayout.db.Device` of a
:func:`klayout_tools.extract.mom_capacitor_device_class`-shaped
``DeviceClass`` instead -- so both sides of a compare (a pre-extracted
``layout.netlist`` with ``layout.deck`` given, and a ``reference.netlist``
with ``reference.deck`` given) recognise the identical device class for the
identical name, and the device participates in ``klt lvs``'s ordinary
device-level compare (device census, ``parameter_tolerance``, named
``device.unmatched``/``device.property`` mismatches) exactly like an M/R/C/D
card already does. Without a resolved deck on that side (``layout.deck``/
``reference.deck`` omitted), this recognition cannot run -- the ``X`` card
still degrades to the pre-#1942 abstract-circuit fallback described above;
see ``docs/cli/lvs.md``'s "Custom device classes" section.

**Drawn-resistor classes round-tripped as ``X`` subcircuit calls (issue
#1157).** The same reader-side recovery pattern, one deck table over: a
*three-terminal* (bulk-bearing) drawn-resistor class -- sky130's
``res_high_po``/``res_xhigh_po``, gf180mcu's ``ppolyf_u`` family, sg13g2's
``rsil``/``rppd``/``rhigh`` -- is written by ``klt extract`` as an ``X``
subcircuit call (``X$1 A B W <class> r=<ohms> L=<um>U W=<um>U``, see
:func:`klayout_tools.pdk_models.create_model_binding_delegate`'s
docstring), because ngspice's native ``R`` element accepts exactly two
nodes and the pre-#1157 3-net ``R`` card was not a simulatable deck at
all. Read back through a plain ``kdb.NetlistSpiceReader()``, that card
degrades to exactly the mangled abstract-circuit fallback the #1942
paragraph describes -- so :func:`resistor_classes_for_deck` reads the
deck's own ``ExtractionDeck.resistors`` table and
:func:`make_capacitor_class_recovery_reader`'s ``resistor_classes``
parameter recovers the device as a real 3-terminal
``kdb.DeviceClassResistor`` (terminals ``A``/``B``/``W``, parameters
``R``/``L``/``W`` in ohms/micrometres) named with the deck's canonical
class name, byte-compatible with what the pre-#1157 ``R`` card's own
read-back produced. The card's declared ``r=`` parameter keeps the
extracted (offset-corrected, issues #521/#588) resistance on the
round-tripped device -- the value rides the card exactly so this recovery
need not guess it from geometry.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import klayout.db as kdb

    from .decks import ExtractionDeck

#: KLayout's own reader assigns this name to the implicit top-level circuit
#: when a SPICE file has device cards outside any ``.subckt`` block (verified
#: empirically against ``kdb.NetlistSpiceReader``) -- the scanner below starts
#: in this same scope so a (rare) top-level bare capacitor card still
#: recovers correctly, rather than silently never matching because of a
#: circuit-name mismatch between the text scan and the real reader.
_TOP_LEVEL_CIRCUIT_NAME = ".TOP"

_SUBCKT_RE = re.compile(r"^\.subckt\s+(\S+)", re.IGNORECASE)
_ENDS_RE = re.compile(r"^\.ends\b", re.IGNORECASE)
_DEVICE_INSTANCE_COMMENT_RE = re.compile(
    r"^\*\s*device\s+instance\s+(\S+)\s+(.*)$", re.IGNORECASE
)


def _class_name_from_comment_rest(rest: str) -> str | None:
    """The trailing device-class-name token from a `` * device instance
    <name> <rest>`` comment's own ``<rest>`` text, or ``None`` when the
    comment carries no class name at all (an anonymous device class, e.g.
    every ``--parasitics`` ground/coupling capacitor).

    Found by locating the *last* whitespace-separated token containing a
    ``,`` -- KLayout's own ``x,y`` location marker, always comma-joined with
    no internal space, always present -- rather than assuming a fixed token
    count before it. The transform/array-index token(s) between the device
    name and the location are KLayout's own internal writer detail, not part
    of any contract ``klt`` relies on; anchoring on the location token's own
    distinguishing feature (a literal comma no other token in the comment
    carries) keeps this resilient to that detail changing.
    """
    tokens = rest.split()
    location_index = None
    for index, token in enumerate(tokens):
        if "," in token:
            location_index = index
    if location_index is None or location_index == len(tokens) - 1:
        return None
    class_name = " ".join(tokens[location_index + 1 :])
    return class_name or None


def parse_capacitor_class_comments(text: str) -> dict[tuple[str, str], str]:
    """Scan ``text`` (a ``klt extract``-written SPICE netlist, or any SPICE
    text following the same ``kdb.NetlistSpiceWriter`` convention) for
    `` * device instance <name> ... <class>`` comment lines immediately
    followed by that same device's own bare ``C`` card, and return a
    ``{(circuit_name, device_name): class_name}`` map recovering the class
    name for each one found.

    Association is by strict physical adjacency (the comment line
    immediately precedes the device card it describes, exactly as
    ``kdb.NetlistSpiceWriter`` emits it) plus a same-name, same-type sanity
    check against the following card's own leading letter and name --
    deliberately conservative: a mismatch, or no comment at all, simply
    leaves that device out of the returned map (callers degrade gracefully
    to the generic capacitor class for anything not found here, see
    :func:`make_capacitor_class_recovery_reader`).
    """
    recovered: dict[tuple[str, str], str] = {}
    current_circuit = _TOP_LEVEL_CIRCUIT_NAME
    pending: tuple[str, str] | None = None  # (device_name, class_name)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("*"):
            match = _DEVICE_INSTANCE_COMMENT_RE.match(line)
            if match:
                device_name = match.group(1)
                class_name = _class_name_from_comment_rest(match.group(2))
                pending = (device_name, class_name) if class_name else None
            else:
                pending = None
            continue

        subckt_match = _SUBCKT_RE.match(line)
        if subckt_match:
            current_circuit = subckt_match.group(1)
            pending = None
            continue
        if _ENDS_RE.match(line):
            current_circuit = _TOP_LEVEL_CIRCUIT_NAME
            pending = None
            continue

        if pending is not None:
            device_name, class_name = pending
            first_token = line.split(None, 1)[0]
            if first_token[:1].upper() == "C" and first_token[1:] == device_name:
                recovered[(current_circuit, device_name)] = class_name
        pending = None

    return recovered


def custom_device_classes_for_deck(
    deck: ExtractionDeck | None,
) -> dict[str, str]:
    """``{<UPPER-CASED class name>: <canonical class name>}`` for every
    custom (``kdb.GenericDeviceExtractor``-recognised) device class
    ``deck`` declares -- today, exactly its ``mom_capacitors`` entries
    (issue #1466) -- the table :func:`make_capacitor_class_recovery_reader`'s
    ``custom_device_classes`` parameter wants (issue #1942).

    Keyed off the *same* deck object the layout-extraction side resolves
    (``klayout_tools.extract._build_mom_capacitor_extractor``, driven by
    ``ExtractionDeck.mom_capacitors``), so a round-tripped ``X`` card names
    exactly the class this table recognises -- upper-cased because a SPICE
    ``X`` card's own subcircuit-name token case is not guaranteed to survive
    a hand-edit/tool round trip, matched case-insensitively the same way
    ``kdb.NetlistSpiceReader`` case-folds everything else it reads. ``None``
    (no deck resolved for this side of the compare -- ``layout.deck``/
    ``reference.deck`` omitted) returns ``{}``: the pre-#1942 default,
    unchanged (the ``X`` card degrades to KLayout's own abstract-circuit
    fallback, see this module's own docstring).
    """
    if deck is None:
        return {}
    return {
        mom_capacitor.name.upper(): mom_capacitor.name
        for mom_capacitor in deck.mom_capacitors
    }


def resistor_classes_for_deck(
    deck: ExtractionDeck | None,
) -> dict[str, str]:
    """``{<UPPER-CASED class name>: <canonical class name>}`` for every drawn
    resistor class ``deck`` declares -- the table
    :func:`make_capacitor_class_recovery_reader`'s ``resistor_classes``
    parameter wants (issue #1157), built exactly like
    :func:`custom_device_classes_for_deck` builds the MoM-capacitor table
    (issue #1942), from ``ExtractionDeck.resistors`` instead.

    Only the *bulk-bearing* (3-terminal) classes ever receive an ``X`` card
    from ``klt extract`` -- a 2-terminal class keeps its native ``R`` card,
    which ``kdb.NetlistSpiceReader`` reads without any help -- but every
    declared class is listed anyway: recognition is keyed on the card's own
    subcircuit-name token *plus* its 3-net shape (a name match alone never
    hijacks a card), so an unused 2-terminal entry is unreachable dead-table
    at worst. Every declared flavour name (``ResistorDevice.flavours``, e.g.
    gf180mcu's ``ppolyf_u_1k``/``_2k``/``_3k`` sheet-rho variants) is listed
    alongside its base entry's name, so the table is **independent of
    ``deck_options``**: a netlist extracted with ``--deck-option
    poly_res=2k`` round-trips through a ``klt lvs`` request that names no
    ``deck_options`` of its own -- exactly as it did before #1157, when the
    pre-extracted card was a plain ``R`` card needing no deck table at all.
    ``None`` (no deck resolved for this side of the compare) returns ``{}``:
    recognition cannot run, and the ``X`` card degrades to the
    abstract-circuit fallback, the same graceful degradation every other
    unrecognised card takes.
    """
    if deck is None:
        return {}
    table: dict[str, str] = {}
    for resistor in deck.resistors:
        table[resistor.name.upper()] = resistor.name
        for flavour in resistor.flavours:
            table.setdefault(flavour.name.upper(), flavour.name)
    return table


def bulk_resistor_device_class(name: str) -> kdb.DeviceClass:
    """Build the ``kdb.DeviceClass`` one bulk-bearing drawn-resistor class
    named ``name`` round-trips through (issue #1157): KLayout's native
    ``DeviceClassResistorWithBulk`` -- terminals ``A``/``B``/``W``,
    parameters ``R``/``L``/``W``/``A``/``P`` -- the exact class type
    ``kdb.NetlistSpiceReader`` itself synthesises when it reads the
    pre-#1157 3-net ``R`` card (verified against the installed
    ``klayout.db`` module), so a recovered device is parameter-for-
    parameter and terminal-for-terminal what ``klt lvs``'s compare saw
    before the card shape changed -- including ``Netlist.combine_devices``'s
    series-fold behaviour, which a plain ``DeviceClassResistor`` with a
    hand-added third terminal does *not* support (the ``WithBulk`` class is
    what teaches the fold to leave the shared bulk connection alone).
    Shared by every recovery of the same class name in one netlist via
    :func:`_shared_device_class`, the same one-class-object-per-name
    discipline KLayout's own default reading follows.
    """
    import klayout.db as kdb

    device_class = kdb.DeviceClassResistorWithBulk()
    device_class.name = name
    return device_class


def _shared_device_class(
    netlist: kdb.Netlist,
    class_name: str,
    make_class: Callable[[], kdb.DeviceClass],
    registry: dict[str, kdb.DeviceClass],
) -> kdb.DeviceClass:
    """The one ``kdb.DeviceClass`` object every recovery of ``class_name``
    in one read must share -- looked up in ``registry`` first, then in
    ``netlist``'s registered classes by exact name, and only then built via
    ``make_class`` and registered (issue #1157, PR #2336 CI regression).

    Why not ``Netlist.device_class_by_name``: that lookup misses a class
    this recovery registered earlier in the same read twice over (both
    verified against ``klayout==0.30.10``). First, it does not see a class
    ``netlist.add()``-ed from inside a ``NetlistSpiceReaderDelegate
    .element()`` callback for the rest of that read, even though
    ``each_device_class()`` lists it immediately. Second, even after the
    read completes it normalizes its argument through the netlist's own
    case convention (SPICE netlists read case-insensitively, so it
    *uppercases*) but compares against each registered class's stored name
    verbatim -- a deck's canonical lowercase ``res_high_po`` never matches.
    Either miss alone sends a naive create-and-``add`` fallback down the
    per-card path: one fresh class object *per recovered card*.
    ``Netlist.combine_devices()`` groups devices by ``DeviceClass`` object
    identity (``tl::id_of`` of the device's class against the class being
    combined for), so a per-card class object means no two devices ever
    group and the series fold silently never happens (PR #2336: three
    sky130 ``res_high_po`` segments survived as three devices on Linux CI).
    One class object per name -- what this helper guarantees -- is also
    what makes the fold robust against the klayout-side detail that
    ``DeviceClass``'s copy constructor copies an indeterminate
    ``tl::UniqueId`` (verified against ``klayout==0.30.10``: a
    ``Netlist.dup()`` clone's ``id()`` is 0 on macOS but distinct garbage
    on Linux), so the fold no longer depends on which platform's heap
    happens to make the clone ids coincide.

    ``registry`` is the per-reader cache the caller owns (one
    ``make_capacitor_class_recovery_reader`` build serves exactly one
    ``Netlist.read`` call -- every caller in this repo does); the exact-name
    scan covers a class the *default* reader handler already registered
    under the same verbatim name earlier in the same read.
    """
    device_class = registry.get(class_name)
    if device_class is not None:
        return device_class
    for registered in netlist.each_device_class():
        if registered.name == class_name:
            registry[class_name] = registered
            return registered
    device_class = make_class()
    netlist.add(device_class)
    registry[class_name] = device_class
    return device_class


def _recover_bulk_resistor_x_card(
    circuit: kdb.Circuit,
    name: str,
    nets: list,
    params: dict,
    resistor_name: str,
    registry: dict[str, kdb.DeviceClass],
) -> bool:
    """Recover one ``X`` card naming a deck drawn-resistor class as a real
    3-terminal resistor device (issue #1157) -- see this module's docstring
    and :func:`make_capacitor_class_recovery_reader`'s ``resistor_classes``
    parameter. Reports whether the card was handled.

    The writer's own card contract is ``X$name a b w <class> r=<ohms>
    L=<um>U W=<um>U``. The reader's parameter parsing delivers ``L``/``W``
    SI-scaled (``10U`` -> ``1e-5``), so they are converted to the
    micrometre domain ``DeviceClassResistor`` reports. ``L``/``W`` are
    optional on the card (a hand-written call may carry ``r=`` alone);
    ``r=`` itself is required, and a card without it -- or without the
    writer's 3-net shape -- reports ``False`` and falls through to the
    default handler rather than being silently recovered as a zero-ohm
    device or one with a misconnected terminal. All cards naming the same
    class share one ``DeviceClass`` object (:func:`_shared_device_class`)
    -- the one-class-object-per-name discipline the class-identity grouping
    inside ``Netlist.combine_devices()`` requires.
    """
    if len(nets) != 3 or "R" not in params:
        return False
    netlist = circuit.netlist()
    device_class = _shared_device_class(
        netlist,
        resistor_name,
        lambda: bulk_resistor_device_class(resistor_name),
        registry,
    )
    terminals = device_class.terminal_definitions()
    if len(nets) != len(terminals):
        return False
    device = circuit.create_device(device_class, name)
    for net, terminal in zip(nets, terminals, strict=True):
        device.connect_terminal(terminal.name, net)
    # Ids are read back off `parameter_definitions()` because the Python
    # `DeviceClass` binding has no by-name lookup -- the same guarded
    # pattern `extract._parameter_id` performs.
    for param_def in device_class.parameter_definitions():
        if param_def.name == "R":
            device.set_parameter(param_def.id(), params["R"])
        elif param_def.name == "L" and "L" in params:
            device.set_parameter(param_def.id(), params["L"] * 1e6)
        elif param_def.name == "W" and "W" in params:
            device.set_parameter(param_def.id(), params["W"] * 1e6)
    return True


def _recover_mom_x_card(
    circuit: kdb.Circuit,
    name: str,
    model: str,
    nets: list,
    params: dict,
    custom_lookup: Mapping[str, str],
    registry: dict[str, kdb.DeviceClass],
) -> bool:
    """Recover one ``X`` card naming a deck custom (MoM-capacitor) device
    class as a real device of that class (issue #1942) -- see this module's
    docstring. Reports whether the card was handled; a name match whose
    net count does not fit the class's own terminal shape reports ``False``
    (defensive only, never seen in practice) and the caller falls through
    to the default handler rather than silently misconnecting a terminal.
    All cards naming the same class share one ``DeviceClass`` object
    (:func:`_shared_device_class`, the same #1157 discipline the resistor
    path follows)."""

    from .extract import mom_capacitor_device_class

    class_name = custom_lookup.get(model.upper())
    if class_name is None:
        return False
    netlist = circuit.netlist()
    device_class = _shared_device_class(
        netlist,
        class_name,
        lambda: mom_capacitor_device_class(class_name),
        registry,
    )
    terminals = device_class.terminal_definitions()
    if len(nets) != len(terminals):
        return False
    device = circuit.create_device(device_class, name)
    for net, terminal in zip(nets, terminals, strict=True):
        device.connect_terminal(terminal.name, net)
    for param_def in device_class.parameter_definitions():
        if param_def.name in params:
            device.set_parameter(param_def.id(), params[param_def.name])
    return True


def _recover_resistor_x_card(
    circuit: kdb.Circuit,
    name: str,
    model: str,
    nets: list,
    params: dict,
    resistor_lookup: Mapping[str, str],
    registry: dict[str, kdb.DeviceClass],
) -> bool:
    """Recover one ``X`` card naming a deck drawn-resistor class as a real
    3-terminal resistor device (issue #1157). Reports whether the card was
    handled: ``False`` when the card's subcircuit-name token is not in
    ``resistor_lookup``, and for a matching name without the writer's
    3-net shape or declared ``r=`` parameter (the caller falls through to
    the default handler rather than recovering a zero-ohm or misconnected
    device). ``registry`` is threaded straight through to
    :func:`_recover_bulk_resistor_x_card` -- see its docstring for why a
    per-read cache (rather than ``Netlist.device_class_by_name`` alone) is
    required for every recovered device of one class to share the same
    object."""
    resistor_name = resistor_lookup.get(model.upper())
    if resistor_name is None:
        return False
    return _recover_bulk_resistor_x_card(
        circuit, name, nets, params, resistor_name, registry
    )


def make_capacitor_class_recovery_reader(
    recovered: Mapping[tuple[str, str], str],
    *,
    custom_device_classes: Mapping[str, str] | None = None,
    resistor_classes: Mapping[str, str] | None = None,
) -> kdb.NetlistSpiceReader:
    """Build a ``kdb.NetlistSpiceReader`` whose delegate reattaches a
    recovered capacitor device-class name (see
    :func:`parse_capacitor_class_comments`) to a bare ``C`` card's device
    instead of KLayout's own generic/anonymous ``CAP`` class -- for exactly
    the ``(circuit_name, device_name)`` pairs ``recovered`` names. Every
    other device/card (including a bare ``C`` card with no entry in
    ``recovered`` -- a missing or malformed comment) defers to KLayout's
    default reading behaviour, unchanged.

    The recovered device class is looked up in the reader's own
    one-class-object-per-name registry (:func:`_shared_device_class`) --
    which also catches a class the default reader handler already
    registered under the same verbatim name earlier in the same read -- and
    only created when absent, so multiple devices/circuits sharing the
    same recovered class name -- and, moreover, a class name genuinely bound
    elsewhere in the same file via a real ``X``-card model binding --
    correctly share one class object, exactly the way KLayout's own default
    reading already shares one class object per class name. Sharing the
    object (not merely the name) is load-bearing: ``Netlist.combine_devices``
    groups devices by class *object identity*, so KLayout's
    ``device_class_by_name`` -- whose case-insensitive lookup silently
    misses a lowercase stored name -- cannot be used as the shared registry
    (PR #2336, see :func:`_shared_device_class`).

    ``custom_device_classes`` (issue #1942, see :func:`custom_device_classes_for_deck`
    and this module's own docstring) additionally recognises an ``X``
    subcircuit-call card naming one of its values as a *device* of that
    class -- built by :func:`klayout_tools.extract.mom_capacitor_device_class`
    the first time a given class name is seen, then reused by name exactly
    like the capacitor-recovery path above -- instead of letting
    ``kdb.NetlistSpiceReader``'s own default handling synthesise a mangled-
    name abstract circuit for it. ``None``/``{}`` (the default) leaves every
    ``X`` card to that default handling, byte-for-byte unchanged from before
    this parameter existed.

    ``resistor_classes`` (issue #1157, see :func:`resistor_classes_for_deck`
    and this module's own docstring) recognises the same way an ``X`` card
    naming a *drawn-resistor* class as a real 3-terminal
    ``DeviceClassResistor`` device (:func:`bulk_resistor_device_class`),
    carrying the card's declared ``r=``/``L=``/``W=`` parameters onto the
    device (``L``/``W`` arrive SI-scaled by the reader's own parameter
    parsing -- ``10U`` is ``1e-5`` -- and are converted to the micrometre
    domain ``DeviceClassResistor`` reports). ``None``/``{}`` (the default)
    leaves every such card to the default handling, unchanged.
    """
    import klayout.db as kdb

    custom_lookup: dict[str, str] = dict(custom_device_classes or {})
    resistor_lookup: dict[str, str] = dict(resistor_classes or {})
    # One reader instance serves exactly one `Netlist.read` call (every
    # caller in this repo builds it per call), so a plain per-class-name
    # cache is a complete registry of the classes this delegate created or
    # found during that read (PR #2336 -- see `_shared_device_class`).
    class_registry: dict[str, kdb.DeviceClass] = {}

    class _CapacitorClassRecoveringDelegate(kdb.NetlistSpiceReaderDelegate):
        def wants_subcircuit(self, name: str) -> bool:
            if name.upper() in custom_lookup or name.upper() in resistor_lookup:
                return True
            return super().wants_subcircuit(name)

        def element(
            self,
            circuit: kdb.Circuit,
            element_type: str,
            name: str,
            model: str,
            value: float,
            nets: list,
            params: dict,
        ) -> bool:
            if element_type == "C" and not model:
                class_name = recovered.get((circuit.name, name))
                if class_name:

                    def _make_capacitor_class() -> kdb.DeviceClass:
                        device_class = kdb.DeviceClassCapacitor()
                        device_class.name = class_name
                        return device_class

                    netlist = circuit.netlist()
                    device_class = _shared_device_class(
                        netlist, class_name, _make_capacitor_class, class_registry
                    )
                    if not isinstance(device_class, kdb.DeviceClassCapacitor):
                        # A same-named but different-typed class is already
                        # registered in this read (never seen in practice:
                        # recovered capacitor class names come from the
                        # extract-written comments and name no resistor or
                        # custom class). Keep the historical behaviour --
                        # a fresh capacitor class -- rather than connecting
                        # a capacitor onto a foreign class's terminal set.
                        device_class = _make_capacitor_class()
                        netlist.add(device_class)
                    terminals = device_class.terminal_definitions()
                    if len(nets) == len(terminals):
                        device = circuit.create_device(device_class, name)
                        for net, terminal in zip(nets, terminals, strict=True):
                            device.connect_terminal(terminal.name, net)
                        device.set_parameter("C", value)
                        self.apply_parameter_scaling(device)
                        return True
                    # Defensive only -- never seen in practice (a capacitor
                    # card always carries exactly 2 nets/terminals). Falling
                    # through to the default handler below is safer than
                    # silently misconnecting a terminal.
            elif element_type == "X":
                if _recover_mom_x_card(
                    circuit, name, model, nets, params, custom_lookup, class_registry
                ):
                    return True
                if _recover_resistor_x_card(
                    circuit, name, model, nets, params, resistor_lookup, class_registry
                ):
                    return True
            return super().element(
                circuit, element_type, name, model, value, nets, params
            )

    return kdb.NetlistSpiceReader(_CapacitorClassRecoveringDelegate())
