"""FinFET device extraction for ``klt extract`` (issue #2761, ASAP7 first).

``extract.py``'s planar recogniser derives a MOS device from the drawn gate
rectangle alone (``W`` = gate/diffusion edge, ``L`` = gate extent). A
FinFET's drive strength is set by the **number of fins** the gate wraps,
which that measurement cannot see, so a
:class:`~klayout_tools.decks.FinFETExtractionDeck` is dispatched here
instead (``extract.extract_netlist_from_layout`` selects
:func:`extract_netlist_tuple` in place of the planar ``_extract_netlist``).
Nothing in the planar path changes.

Device contract
---------------

Each device class is a ``kdb.DeviceClassMOS4Transistor`` named after the
compact model the PDK's own reference netlist uses (ASAP7: ``nmos_rvt``,
``pmos_lvt``, ...; chosen by the deck's VT marker layers), with two added
parameters:

- ``NFIN`` -- fins crossing the channel, **counted from drawn fin
  geometry**: the connected pieces of ``fin & channel``, where *channel* is
  one gate finger over one select-classified active island. Never derived
  from ``W``.
- ``FINGERS`` -- gate fingers merged into this device.

``L`` is the channel area over half its source/drain edge length (the
planar MOS4 measurement); ``W`` is the drawn active width under the gate.
``AS``/``AD``/``PS``/``PD`` are not measured and stay ``0``; they are
neither reported in JSON nor written to SPICE (see :func:`finfet_card`).

After extraction, fingers that are exactly parallel -- the same gate, bulk
and (unordered) source/drain nets and the same ``L`` -- are merged into one
device with ``NFIN``, ``W`` and ``FINGERS`` summed. That is the reference
CDL's own convention (ASAP7's ``INVx2`` is two 3-fin fingers written as one
``nfin=6`` device), and it is the only combination performed: series stacks
are left exactly as drawn.

Geometry that cannot be extracted faithfully is **refused**, never
approximated as a planar device: a finger with no crossing fin, a fin
clipped by the active edge, a fin that does not cross the channel, a
channel without exactly two source/drain neighbours, a gate length outside
the deck's declared set, active under the gate with no (or both) select
implants, a PMOS channel outside the well or an NMOS channel inside it, a
channel touching two VT markers, or a layout whose database unit differs
from the deck's. Each raises :class:`FinFETGeometryError` (``error.code``
``finfet_malformed_geometry`` / ``finfet_dbu_mismatch`` under
``--format json``) listing every offending location in micrometres.

Scope
-----

Flat extraction over the top cell, like the planar path. Options that
depend on planar-only machinery (``--parasitics``, abstract cells, DEF pin
promotion, ``--subcircuit``, ...) are rejected up front by
:func:`reject_unsupported_options` rather than half-applied.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from .decks.finfet import FinFETExtractionDeck
from .extract_spef import ExtractError

if TYPE_CHECKING:  # pragma: no cover - typing only
    import klayout.db as kdb

#: Device parameter carrying the geometry-counted fin total.
NFIN_PARAM = "NFIN"
#: Device parameter carrying the number of merged gate fingers.
FINGERS_PARAM = "FINGERS"

#: ``error.code`` for refused device geometry (see module docstring).
CODE_MALFORMED = "finfet_malformed_geometry"
#: ``error.code`` for a layout database unit the deck does not accept.
CODE_DBU_MISMATCH = "finfet_dbu_mismatch"

#: At most this many findings are spelled out in one error message.
_MAX_REPORTED = 20


class FinFETGeometryError(ExtractError):
    """A FinFET layout the extractor refuses rather than approximates.

    ``code`` is surfaced as the JSON error envelope's optional
    ``error.code``; ``findings`` is the full list of
    ``{"problem": str, "bbox_um": [l, b, r, t]}`` entries the message
    summarises.
    """

    def __init__(
        self, message: str, code: str, findings: list[dict[str, Any]] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.findings = findings or []


def is_finfet_deck(deck: object) -> bool:
    return isinstance(deck, FinFETExtractionDeck)


def reject_unsupported_options(deck_name: str, **options: Any) -> None:
    """Raise :class:`ExtractError` naming every non-default option the FinFET
    path does not implement. ``options`` maps a CLI-facing option name to its
    value; any truthy value is a request for planar-only machinery."""
    requested = sorted(name for name, value in options.items() if value)
    if requested:
        raise ExtractError(
            f"deck '{deck_name}' is a FinFET extraction deck; "
            f"{', '.join(requested)} "
            f"{'is' if len(requested) == 1 else 'are'} not supported for FinFET "
            "extraction (see docs/cli/extract.md, 'ASAP7 FinFET extraction')"
        )


# --------------------------------------------------------------------------- #
# Device class + combination
# --------------------------------------------------------------------------- #

#: Python-side combiners must outlive every netlist that references them --
#: KLayout holds a raw pointer, and a collected combiner is a hard crash.
_COMBINER_KEEPALIVE: list[Any] = []


def _net_key(device: kdb.Device, terminal_id: int) -> int | None:
    net = device.net_for_terminal(terminal_id)
    return None if net is None else net.cluster_id


def _make_combiner() -> Any:
    import klayout.db as kdb

    class _ParallelFingerCombiner(kdb.GenericDeviceCombiner):
        """Merge exactly-parallel fingers (see module docstring)."""

        def combine_devices(self, a: kdb.Device, b: kdb.Device) -> bool:
            cls = a.device_class()
            s, g, d, bulk = (cls.terminal_id(n) for n in ("S", "G", "D", "B"))
            if _net_key(a, g) != _net_key(b, g):
                return False
            if _net_key(a, bulk) != _net_key(b, bulk):
                return False
            sa, da = _net_key(a, s), _net_key(a, d)
            sb, db = _net_key(b, s), _net_key(b, d)
            if not ((sa == sb and da == db) or (sa == db and da == sb)):
                return False
            if abs(a.parameter("L") - b.parameter("L")) > 1e-9:
                return False
            for name in (NFIN_PARAM, FINGERS_PARAM, "W"):
                a.set_parameter(name, a.parameter(name) + b.parameter(name))
            for terminal in (s, g, d, bulk):
                b.disconnect_terminal(terminal)
            return True

    combiner = _ParallelFingerCombiner()
    _COMBINER_KEEPALIVE.append(combiner)
    return combiner


def finfet_device_class(name: str) -> kdb.DeviceClassMOS4Transistor:
    """A MOS4 device class carrying ``NFIN``/``FINGERS`` and the
    parallel-finger combiner. Also used by tests and future LVS readers so
    both sides of a comparison share one parameter contract."""
    import klayout.db as kdb

    device_class = kdb.DeviceClassMOS4Transistor()
    device_class.name = name
    device_class.add_parameter(
        kdb.DeviceParameterDefinition(NFIN_PARAM, "Fin count", 0.0, True)
    )
    device_class.add_parameter(
        kdb.DeviceParameterDefinition(FINGERS_PARAM, "Gate fingers", 0.0, False)
    )
    device_class.combiner = _make_combiner()
    device_class.supports_parallel_combination = True
    device_class.supports_serial_combination = False
    return device_class


# --------------------------------------------------------------------------- #
# Per-channel measurement
# --------------------------------------------------------------------------- #


def _bbox_um(box: kdb.Box, dbu: float) -> list[float]:
    return [
        round(box.left * dbu, 6),
        round(box.bottom * dbu, 6),
        round(box.right * dbu, 6),
        round(box.top * dbu, 6),
    ]


class _Channel:
    """One gate finger over one active island, measured once."""

    def __init__(
        self, polygon: kdb.Polygon, sd: kdb.Region, fin: kdb.Region, dbu: float
    ) -> None:
        import klayout.db as kdb

        self.polygon = polygon
        self.region = kdb.Region(polygon)
        self.neighbours = sd.interacting(self.region).merged()
        self.width_dbu = (self.region.edges() & self.neighbours.edges()).length() / 2.0
        self.length_um = (
            polygon.area() / self.width_dbu * dbu if self.width_dbu > 0 else 0.0
        )
        self.fins = (fin & self.region).merged()
        self.dbu = dbu


def _neighbour_problem(ch: _Channel, _deck: FinFETExtractionDeck) -> str | None:
    count = ch.neighbours.count()
    if count != 2:
        return f"channel has {count} source/drain neighbours (expected 2)"
    if ch.width_dbu <= 0:
        return "channel shares no edge with its source/drain"
    return None


def _length_problem(ch: _Channel, deck: FinFETExtractionDeck) -> str | None:
    allowed = deck.gate_lengths_um
    if not allowed or any(abs(ch.length_um - v) <= ch.dbu / 2 for v in allowed):
        return None
    declared = ", ".join(f"{v * 1000:g} nm" for v in allowed)
    return (
        f"gate length {ch.length_um * 1000:.3f} nm is not a declared gate "
        f"length ({declared})"
    )


def _fin_presence_problem(ch: _Channel, _deck: FinFETExtractionDeck) -> str | None:
    if ch.fins.is_empty():
        return "channel has no fin crossing it (planar gate over active)"
    return None


def _clipped_fin_problem(ch: _Channel, _deck: FinFETExtractionDeck) -> str | None:
    # A fin touching a channel edge that is *not* shared with source/drain is
    # clipped by the active boundary: a partial fin, not a countable one.
    clipped = ch.fins.interacting(ch.region.edges() - ch.neighbours.edges())
    if clipped.is_empty():
        return None
    return f"channel has {clipped.count()} fin(s) clipped by the active edge"


def _crossing_problem(ch: _Channel, _deck: FinFETExtractionDeck) -> str | None:
    import klayout.db as kdb

    sides = [kdb.Region(piece) for piece in ch.neighbours.each()]
    not_crossing = [
        piece
        for piece in ch.fins.each()
        if not all(kdb.Region(piece).interacting(side).count() for side in sides)
    ]
    if not not_crossing:
        return None
    return (
        f"channel has {len(not_crossing)} fin(s) that do not cross from source to drain"
    )


#: Checked in order; the first problem found is the one reported.
_CHANNEL_CHECKS = (
    _neighbour_problem,
    _length_problem,
    _fin_presence_problem,
    _clipped_fin_problem,
    _crossing_problem,
)


def _channel_problem(ch: _Channel, deck: FinFETExtractionDeck) -> str | None:
    for check in _CHANNEL_CHECKS:
        problem = check(ch, deck)
        if problem is not None:
            return problem
    return None


# --------------------------------------------------------------------------- #
# Extractor
# --------------------------------------------------------------------------- #


def _make_extractor(
    class_name: str,
    deck: FinFETExtractionDeck,
    findings: list[dict[str, Any]],
) -> Any:
    import klayout.db as kdb

    class _FinFETExtractor(kdb.GenericDeviceExtractor):
        """Layers: SD (0), channel seed (1), gate conductor (2), body (3),
        fin (4). One device per merged channel polygon (gate finger)."""

        def __init__(self) -> None:
            self.name = class_name

        def setup(self) -> None:
            self.define_layer("SD", "Source/drain diffusion")
            self.define_layer("G", "Channel (gate finger over active)")
            self.define_layer("P", "Gate conductor (G terminal)")
            self.define_layer("W", "Body (B terminal)")
            self.define_layer("FIN", "Fins (counted, not connected)")
            self.register_device_class(finfet_device_class(class_name))

        def get_connectivity(self, _layout: kdb.Layout, layers: list[int]) -> Any:
            sd, channel, gate, body, fin = layers
            conn = kdb.Connectivity()
            for other in (channel, sd, gate, body, fin):
                conn.connect(channel, other)
            return conn

        def extract_devices(self, layer_geometry: list[kdb.Region]) -> None:
            sd, channels, _gate, _body, fin = layer_geometry
            for polygon in channels.merged().each():
                ch = _Channel(polygon, sd, fin, self.dbu())
                problem = _channel_problem(ch, deck)
                if problem is not None:
                    findings.append(
                        {
                            "problem": f"{class_name} {problem}",
                            "bbox_um": _bbox_um(polygon.bbox(), ch.dbu),
                        }
                    )
                    continue
                self._emit(ch)

        def _emit(self, ch: _Channel) -> None:
            device = self.create_device()
            cls = device.device_class()
            device.set_parameter("L", ch.length_um)
            device.set_parameter("W", ch.width_dbu * ch.dbu)
            device.set_parameter(NFIN_PARAM, float(ch.fins.count()))
            device.set_parameter(FINGERS_PARAM, 1.0)
            source, drain = list(ch.neighbours.each())
            self.define_terminal(device, cls.terminal_id("S"), 0, source)
            self.define_terminal(device, cls.terminal_id("D"), 0, drain)
            self.define_terminal(device, cls.terminal_id("G"), 2, ch.polygon)
            self.define_terminal(device, cls.terminal_id("B"), 3, ch.polygon)

    return _FinFETExtractor()


# --------------------------------------------------------------------------- #
# Layer-level checks
# --------------------------------------------------------------------------- #


def _region_findings(
    region: kdb.Region, problem: str, dbu: float
) -> Iterable[dict[str, Any]]:
    for polygon in region.merged().each():
        yield {"problem": problem, "bbox_um": _bbox_um(polygon.bbox(), dbu)}


def _raise_findings(findings: list[dict[str, Any]], deck_name: str) -> None:
    if not findings:
        return
    shown = "; ".join(
        f"{f['problem']} at {f['bbox_um']}" for f in findings[:_MAX_REPORTED]
    )
    hidden = len(findings) - _MAX_REPORTED
    more = f" (+{hidden} more)" if hidden > 0 else ""
    raise FinFETGeometryError(
        f"deck '{deck_name}': {len(findings)} FinFET device(s) cannot be "
        f"extracted faithfully and were not approximated: {shown}{more}",
        CODE_MALFORMED,
        findings,
    )


def check_dbu(layout: kdb.Layout, deck: FinFETExtractionDeck, deck_name: str) -> None:
    if deck.nominal_dbu_um and abs(layout.dbu - deck.nominal_dbu_um) > 1e-12:
        raise FinFETGeometryError(
            f"deck '{deck_name}' measures FinFET geometry at dbu "
            f"{deck.nominal_dbu_um} um, but the layout's dbu is {layout.dbu} um; "
            "refusing to rescale (a stream written at a different scale would "
            "extract wrong gate lengths and fin pitches)",
            CODE_DBU_MISMATCH,
        )


class _Layers:
    """Every derived region the FinFET flow needs, registered with ``l2n``."""

    def __init__(
        self, l2n: kdb.LayoutToNetlist, layout: kdb.Layout, deck: FinFETExtractionDeck
    ) -> None:
        import klayout.db as kdb

        self._l2n = l2n
        self._layout = layout

        self.well = self.polygons(deck.nwell, "well")
        self.fin = self.polygons(deck.fin, "fin")
        self.active = self.polygons(deck.active, "active")
        self.nselect = self.polygons(deck.nselect, "nselect")
        self.pselect = self.polygons(deck.pselect, "pselect")
        self.lisd = self.polygons(deck.lisd, "lisd")
        self.lig = self.polygons(deck.lig, "lig")
        self.v0 = self.polygons(deck.contact, "v0")
        self.metals = [
            self.polygons(ld, f"metal{i + 1}") for i, ld in enumerate(deck.metals)
        ]
        self.vias = [self.polygons(ld, f"via{i + 1}") for i, ld in enumerate(deck.vias)]
        gate = self.polygons(deck.poly, "gate_drawn")
        if deck.gate_cut is not None:
            gate = gate - self.polygons(deck.gate_cut, "gate_cut")
        self.gate = self.register(gate, "gate")
        self.channel = self.active & self.gate
        ndiff = (self.active & self.nselect) - self.pselect
        pdiff = (self.active & self.pselect) - self.nselect
        self.nchannel = ndiff & self.gate
        self.pchannel = pdiff & self.gate
        self.nsd = self.register(ndiff - self.gate, "nsd")
        self.psd = self.register(pdiff - self.gate, "psd")
        # NMOS body: a synthesized global (the cells draw no substrate tie).
        self.nbody = self.register(kdb.Region(), "nbody")
        self.markers = [
            (flavour, self.polygons(flavour.marker, f"vt_{flavour.name}"))
            for flavour in deck.vt_flavours
        ]

    def polygons(self, ld: tuple[int, int], name: str) -> kdb.Region:
        return self._l2n.make_polygon_layer(self._layout.layer(*ld), name)

    def texts(self, ld: tuple[int, int], name: str) -> kdb.Texts:
        return self._l2n.make_text_layer(self._layout.layer(*ld), name)

    def register(self, region: kdb.Region, name: str) -> kdb.Region:
        self._l2n.register(region, name)
        return region


def _layer_findings(layers: _Layers, dbu: float) -> list[dict[str, Any]]:
    channel = layers.channel
    checks = [
        (
            channel & layers.nselect & layers.pselect,
            "gate over active covered by both Nselect and Pselect",
        ),
        (
            channel - layers.nselect - layers.pselect,
            "gate over active covered by neither Nselect nor Pselect",
        ),
        (layers.pchannel.not_inside(layers.well), "PMOS channel not inside the well"),
        (layers.nchannel & layers.well, "NMOS channel inside the well"),
    ]
    for index, (flavour, marker) in enumerate(layers.markers):
        for other, other_marker in layers.markers[index + 1 :]:
            checks.append(
                (
                    channel.interacting(marker).interacting(other_marker),
                    f"channel touches two VT markers ({flavour.name}, {other.name})",
                )
            )
    findings: list[dict[str, Any]] = []
    for region, problem in checks:
        findings.extend(_region_findings(region, problem, dbu))
    return findings


# --------------------------------------------------------------------------- #
# Devices + connectivity
# --------------------------------------------------------------------------- #


def _vt_parts(
    channel: kdb.Region, layers: _Layers, deck: FinFETExtractionDeck
) -> list[tuple[str, tuple[str, str], kdb.Region]]:
    """``[(flavour tag, (nfet, pfet) class names, channel subset)]`` -- a
    channel belongs to the one VT flavour whose marker it touches (two
    markers were already refused), else to the deck default."""
    parts = []
    remaining = channel
    for flavour, marker in layers.markers:
        parts.append(
            (
                flavour.name,
                (flavour.nfet_class, flavour.pfet_class),
                channel.interacting(marker),
            )
        )
        remaining = remaining.not_interacting(marker)
    parts.append(("default", (deck.nfet_class, deck.pfet_class), remaining))
    return parts


def _extract_devices(
    l2n: kdb.LayoutToNetlist,
    layers: _Layers,
    deck: FinFETExtractionDeck,
    findings: list[dict[str, Any]],
) -> None:
    sides = (
        (0, layers.nchannel, layers.nsd, layers.nbody),
        (1, layers.pchannel, layers.psd, layers.well),
    )
    for polarity, channel, sd, body in sides:
        for tag, classes, part in _vt_parts(channel, layers, deck):
            if part.is_empty():
                continue
            layers.register(part, f"channel_{polarity}_{tag}")
            l2n.extract_devices(
                _make_extractor(classes[polarity], deck, findings),
                {"SD": sd, "G": part, "P": layers.gate, "W": body, "FIN": layers.fin},
            )


def _connect_feol(
    l2n: kdb.LayoutToNetlist, layers: _Layers, deck: FinFETExtractionDeck
) -> None:
    for layer in (layers.gate, layers.nsd, layers.psd, layers.well, layers.lisd):
        l2n.connect(layer)
    l2n.connect(layers.lig)
    l2n.connect(layers.v0)
    # Diffusion reaches LISD through the S/D contact layer when the deck
    # declares one (see FinFETExtractionDeck.sd_contact), else directly.
    contact = layers.lisd
    if deck.sd_contact is not None:
        contact = layers.polygons(deck.sd_contact, "sd_contact")
        l2n.connect(contact)
        l2n.connect(contact, layers.lisd)
    l2n.connect(layers.nsd, contact)
    l2n.connect(layers.psd, contact)
    l2n.connect(layers.gate, layers.lig)
    l2n.connect(layers.lisd, layers.v0)
    l2n.connect(layers.lig, layers.v0)
    if deck.lig_lisd_connected:
        l2n.connect(layers.lig, layers.lisd)
    l2n.connect_global(layers.nbody, deck.substrate_net)


def _connect_beol(
    l2n: kdb.LayoutToNetlist, layers: _Layers, deck: FinFETExtractionDeck
) -> None:
    metals, vias = layers.metals, layers.vias
    for layer in (*metals, *vias):
        l2n.connect(layer)
    if metals:
        l2n.connect(layers.v0, metals[0])
    for index, via in enumerate(vias[: max(len(metals) - 1, 0)]):
        l2n.connect(metals[index], via)
        l2n.connect(via, metals[index + 1])
    labels = [
        (metals[index], label_ld, f"metal{index + 1}_label")
        for index, label_ld in enumerate(deck.metal_labels[: len(metals)])
    ]
    labels.append((layers.well, deck.well_label, "well_label"))
    labels.append((layers.gate, deck.poly_label, "gate_label"))
    for conductor, label_ld, name in labels:
        if label_ld is not None:
            l2n.connect(conductor, layers.texts(label_ld, name))


def extract_finfet_netlist(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    deck: FinFETExtractionDeck,
    deck_name: str = "asap7",
) -> tuple[kdb.Netlist, list[str]]:
    """Extract ``top_cell`` (flattened) with ``deck``; return
    ``(netlist, warnings)``. Raises :class:`FinFETGeometryError` for refused
    geometry (see module docstring)."""
    import klayout.db as kdb

    from .extract import _purge_preserving_named_nets

    check_dbu(layout, deck, deck_name)
    l2n = kdb.LayoutToNetlist(kdb.RecursiveShapeIterator(layout, top_cell, []))
    l2n.threads = 1
    layers = _Layers(l2n, layout, deck)

    findings = _layer_findings(layers, layout.dbu)
    _raise_findings(findings, deck_name)
    _extract_devices(l2n, layers, deck, findings)
    _raise_findings(findings, deck_name)

    _connect_feol(l2n, layers, deck)
    _connect_beol(l2n, layers, deck)
    l2n.extract_netlist()
    netlist = l2n.netlist()
    netlist.combine_devices()
    netlist.make_top_level_pins()
    _purge_preserving_named_nets(netlist)

    has_devices = any(
        True for circuit in netlist.each_circuit() for _ in circuit.each_device()
    )
    warnings = (
        [
            f"deck '{deck_name}': NMOS bulk terminals are tied to the synthesized "
            f"'{deck.substrate_net}' net and PMOS bulk terminals to the drawn well "
            "net; neither is joined to a metal supply net of the same name"
        ]
        if has_devices
        else []
    )
    # `l2n` owns the netlist; detach a copy before it is collected.
    return netlist.dup(), warnings


# --------------------------------------------------------------------------- #
# Integration points used by extract.py / klt lvs
# --------------------------------------------------------------------------- #


def _deck_or_none(deck_name: str) -> object:
    from .decks import UnknownExtractionDeckError, get_extraction_deck

    try:
        return get_extraction_deck(deck_name)
    except UnknownExtractionDeckError:
        return None


def is_finfet_deck_name(deck_name: object) -> bool:
    """Whether ``deck_name`` names a registered FinFET extraction deck."""
    return isinstance(deck_name, str) and is_finfet_deck(_deck_or_none(deck_name))


def refuse_run_options(
    deck_name: str,
    *,
    parasitics: bool,
    pdk_variant: str | None,
    pdk_root: str | None,
) -> None:
    """``run_extract``'s early guard: name ``--parasitics``/``--pdk`` as
    unsupported for a FinFET deck before any PDK lookup or parasitics-deck
    resolution reports a less precise error."""
    if is_finfet_deck_name(deck_name):
        reject_unsupported_options(
            deck_name,
            **{
                "--parasitics": parasitics,
                "--pdk": pdk_variant is not None or pdk_root is not None,
            },
        )


def refuse_layout_options(deck: object, deck_name: str, **options: Any) -> None:
    """``extract_netlist_from_layout``'s guard: :func:`reject_unsupported_options`
    for a FinFET ``deck``, a no-op for any other deck."""
    if is_finfet_deck(deck):
        reject_unsupported_options(deck_name, **options)


def extract_netlist_tuple(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    deck: FinFETExtractionDeck,
    *_a: Any,
    **_k: Any,
) -> tuple[Any, ...]:
    """Drop-in for ``extract._extract_netlist``'s 13-tuple contract. Every
    option it would honour was already refused by :func:`refuse_layout_options`;
    the planar-only result slots are their documented empty values."""
    from .extract import _extraction_deck_name

    netlist, warnings = extract_finfet_netlist(
        layout, top_cell, deck, _extraction_deck_name(deck) or "finfet"
    )
    return (netlist, warnings, None, [], 0, [], [], [], None, {}, {}, None, None)


# --------------------------------------------------------------------------- #
# SPICE card
# --------------------------------------------------------------------------- #


def finfet_card(device: kdb.Device, format_name: Any, net_to_string: Any) -> str | None:
    """``M<name> d g s b <model> L=..U W=..U NFIN=<n>`` for a FinFET device,
    else ``None``.

    The parameter set is exactly the one the PDK's own reference CDL uses
    (ASAP7: ``w=.. l=.. nfin=..``). KLayout's default writer would also emit
    the unmeasured ``AS``/``AD``/``PS``/``PD`` as zeros and the merged-finger
    count as ``FINGERS=``; the latter is dangerous for BSIM-CMG, whose own
    ``NF`` multiplies ``NFIN`` (``NFIN`` here is already the total).
    """
    import klayout.db as kdb

    from .pdk_models import _format_um

    device_class = device.device_class()
    if not isinstance(device_class, kdb.DeviceClassMOS4Transistor):
        return None
    if not device_class.has_parameter(NFIN_PARAM):
        return None
    nets = [
        device.net_for_terminal(device_class.terminal_id(t))
        for t in ("D", "G", "S", "B")
    ]
    if any(net is None for net in nets):
        return None
    return (
        f"M{format_name(device.expanded_name())} "
        f"{' '.join(net_to_string(net) for net in nets)} {device_class.name} "
        f"L={_format_um(device.parameter('L'))} "
        f"W={_format_um(device.parameter('W'))} "
        f"NFIN={int(round(device.parameter(NFIN_PARAM)))}"
    )


def spice_writer_delegate_for(deck_name: str, fallback: Any) -> Any:
    """The SPICE writer delegate for ``deck_name``: a FinFET-card writer for
    a FinFET deck (no ``--pdk`` binding or parasitics can apply to one), else
    ``fallback()`` -- the planar model-binding delegate, unchanged."""
    if not is_finfet_deck_name(deck_name):
        return fallback()
    import klayout.db as kdb

    class _FinFETSpiceWriterDelegate(kdb.NetlistSpiceWriterDelegate):
        def write_device(self, device: kdb.Device) -> None:
            card = finfet_card(device, self.format_name, self.net_to_string)
            if card is None:
                super().write_device(device)
            else:
                self.emit_line(card)

    return _FinFETSpiceWriterDelegate()
