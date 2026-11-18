"""Spiral-inductor recognition and extraction for ``klt extract`` (issue #6726).

IHP's ``inductor2`` PyCell is plain top metal under an ``IND`` marker, so a
generic extraction shorts its two terminals through the winding. This module
adds what the metal stack cannot: it recognises the winding
(:func:`cut_inductor_windings`), removes it from the conductor layers so the
terminals stay separate nets, and extracts it as a three-terminal device
(:func:`connect_inductors`) between the two port nets and the substrate or
well beneath it. See :class:`~klayout_tools.decks.InductorDevice` for the
recognition rule and what is not transcribed from the PDK.

The two entry points bracket the metal registration in ``extract.py``'s
``_extract_netlist`` -- the cut must precede it (the cut metal is what gets
registered), the wiring follows it (the ports connect to registered layers).
Inductance, EM and the winding's geometry parameters (``w``/``s``/``d``/
turns) are not measured: the device carries connectivity only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

from ._layout import region as _region
from ._layout import texts as _texts
from .erc_islands import partition_region_by_islands
from .extract_spef import ExtractError

if TYPE_CHECKING:
    import klayout.db as kdb

    from .decks import ExtractionDeck, InductorDevice


def inductor_device_class(name: str) -> kdb.DeviceClass:
    """The ``kdb.DeviceClass`` one :class:`~klayout_tools.decks.InductorDevice`
    named ``name`` registers: terminals ``A``/``B`` (the winding's two ports,
    declared equivalent -- an inductor is symmetric) and ``SUB`` (the
    substrate or well beneath it), and no parameters.

    Shared by the extractor's ``setup()`` and by the reference-netlist reader
    (:mod:`klayout_tools.netlist_inductor_recovery`), so both sides of a
    compare register a structurally identical class for the same name.
    """
    import klayout.db as kdb

    device_class = kdb.DeviceClass()
    device_class.name = name
    terminal_a = kdb.DeviceTerminalDefinition("A", "Terminal A")
    device_class.add_terminal(terminal_a)
    terminal_b = kdb.DeviceTerminalDefinition("B", "Terminal B")
    device_class.add_terminal(terminal_b)
    device_class.add_terminal(
        kdb.DeviceTerminalDefinition("SUB", "Substrate or well beneath the winding")
    )
    device_class.equivalent_terminal_id(terminal_a.id(), terminal_b.id())
    return device_class


@dataclass(frozen=True)
class InductorRecognition:
    """What one :class:`~klayout_tools.decks.InductorDevice` entry recognised
    on one layout: the marker polygons that became devices, their winding
    metal and the two labelled port regions (all empty when nothing was
    recognised), plus the diagnostics for inductor-labelled markers that were
    not."""

    entry: InductorDevice
    markers: kdb.Region
    core: kdb.Region
    port_a: kdb.Region
    port_b: kdb.Region
    shrink_dbu: int
    warnings: tuple[str, ...] = ()


def _case_insensitive_glob(glob: str) -> str:
    """``glob`` with every letter widened to a ``[Xx]`` class -- KLayout's
    ``Texts.with_match`` is case-sensitive and the PDK rule matches labels
    case-insensitively (``glob_to_case_insensitive_glob`` upstream)."""
    return "".join(f"[{c.upper()}{c.lower()}]" if c.isalpha() else c for c in glob)


def _labelled(texts: kdb.Texts, glob: str) -> kdb.Texts:
    return texts.with_match(_case_insensitive_glob(glob), False)


def _check_entry(deck: ExtractionDeck, entry: InductorDevice) -> None:
    """Deck-authoring validation, run unconditionally like the MoM-capacitor
    loop's: ``windings`` and ``port_metal`` must be ``deck.metals`` levels."""
    for layer in (*entry.windings, entry.port_metal):
        if layer not in deck.metals:
            raise ExtractError(
                f"inductor '{entry.name}': layer {layer[0]}/{layer[1]} is not "
                "one of the deck's metals[] layers"
            )


class _Found(NamedTuple):
    """What the label and pin tests found on the whole layout: ``text`` the
    device-text labels matching the entry's glob, ``centre`` the centre-tap
    pins, ``port_a``/``port_b`` the labelled port pins and ``core`` the
    winding components touching both."""

    text: kdb.Texts
    centre: kdb.Region
    port_a: kdb.Region
    port_b: kdb.Region
    core: kdb.Region


def _reject_reason(
    entry: InductorDevice, polygon: kdb.Region, found: _Found
) -> str | None:
    """Why the ``polygon`` marker (a single-polygon ``Region``) is not an
    ``entry.name``, or ``None`` when it is."""
    if polygon.interacting(found.text).is_empty():
        return (
            f"its device text does not match '{entry.device_text_glob}' "
            "(a different inductor family)"
        )
    if not found.centre.interacting(polygon).is_empty():
        return (
            f"it has a '{entry.centre_tap_label}' (centre-tap) pin, and only "
            "the two-terminal winding is modelled"
        )
    count_a = found.port_a.interacting(polygon).count()
    count_b = found.port_b.interacting(polygon).count()
    if (count_a, count_b) != (1, 1):
        return (
            f"it needs exactly one '{entry.port_a_label}' and one "
            f"'{entry.port_b_label}' pin on its winding at the marker edge "
            f"(found {count_a} and {count_b})"
        )
    if found.core.interacting(polygon).is_empty():
        return "no winding metal touches both pins"
    return None


def _recognise(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    deck: ExtractionDeck,
    entry: InductorDevice,
    metals: Sequence[kdb.Region],
) -> InductorRecognition:
    import klayout.db as kdb

    _check_entry(deck, entry)
    shrink = max(1, round(0.001 / layout.dbu))
    empty = kdb.Region()
    marker = _region(layout, top_cell, entry.marker).merged()
    winding = kdb.Region()
    for layer in entry.windings:
        winding += metals[deck.metals.index(layer)]
    core_all = (winding & marker).merged()
    if core_all.is_empty():
        return InductorRecognition(entry, empty, empty, empty, empty, shrink)

    ports = (_region(layout, top_cell, entry.pin) & core_all).merged()
    ports = ports.interacting(marker.edges())
    pin_labels = _texts(layout, top_cell, entry.pin_label)
    port_a = ports.interacting(_labelled(pin_labels, entry.port_a_label))
    port_b = ports.interacting(_labelled(pin_labels, entry.port_b_label))
    device_texts = _texts(layout, top_cell, entry.device_text)
    found = _Found(
        text=_labelled(device_texts, entry.device_text_glob),
        centre=ports.interacting(_labelled(pin_labels, entry.centre_tap_label)),
        port_a=port_a,
        port_b=port_b,
        core=core_all.interacting(port_a).interacting(port_b),
    )
    labelled_any = _labelled(device_texts, entry.any_family_text_glob)
    valid = kdb.Region()
    warnings: list[str] = []
    for polygon in marker.interacting(labelled_any).each():
        single = kdb.Region(polygon)
        if (single & core_all).is_empty():
            continue  # no winding metal under the label: nothing is shorted
        reason = _reject_reason(entry, single, found)
        if reason is None:
            valid += single
            continue
        centre = polygon.bbox().center()
        warnings.append(
            f"{entry.marker[0]}/{entry.marker[1]} marker near "
            f"({centre.x * layout.dbu:.3f}, {centre.y * layout.dbu:.3f}) um "
            f"carries an inductor label but is not extracted as an "
            f"'{entry.name}': {reason}. Its winding metal extracts as plain "
            "wire, which joins the winding's terminals into one net."
        )
    return InductorRecognition(
        entry,
        valid,
        found.core.interacting(valid),
        port_a.interacting(valid),
        port_b.interacting(valid),
        shrink,
        tuple(warnings),
    )


def cut_inductor_windings(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    deck: ExtractionDeck,
    metals: list[kdb.Region],
) -> tuple[list[kdb.Region], tuple[InductorRecognition, ...]]:
    """Recognise every ``deck.inductors`` entry's windings and remove each
    recognised winding's metal from its ``windings`` layers (the same
    cut-then-extract order ``_resolve_resistors`` uses for a resistor body),
    so the two port nets are no longer joined through the spiral. Returns the
    cut ``metals`` (the same list when nothing is recognised) and one
    :class:`InductorRecognition` per entry, for :func:`connect_inductors`.

    Only *recognised* windings are cut: metal under an inductor label that
    fails recognition stays as drawn (and is reported), so a bad marker can
    never open a net.
    """
    if not deck.inductors:
        return metals, ()
    recognitions = tuple(
        _recognise(layout, top_cell, deck, entry, metals) for entry in deck.inductors
    )
    cut = list(metals)
    for recognition in recognitions:
        for layer in recognition.entry.windings:
            level = deck.metals.index(layer)
            cut[level] = cut[level] - recognition.core
    return cut, recognitions


def _build_inductor_extractor(
    name: str, sub_layer_count: int
) -> kdb.GenericDeviceExtractor:
    """A fresh ``kdb.GenericDeviceExtractor`` that turns each recognised
    marker (with its two labelled port polygons and the substrate/well
    placeholder under it) into one three-terminal device named ``name``.

    Layers, in definition order: ``marker``, ``port_a``, ``port_b``, then
    ``sub_layer_count`` substrate/well layers in priority order -- the
    ``SUB`` terminal lands on the first one the marker has shape on, so an
    inductor straddling substrate and well resolves to the substrate
    deterministically. The caller has already validated the marker (exactly
    one pin per port), so the guards here only protect against a layer
    geometry this module did not hand over.
    """
    import klayout.db as kdb

    class _InductorExtractor(kdb.GenericDeviceExtractor):
        def setup(self) -> None:
            self.name = name
            self.define_layer("marker", f"{name} recognition marker")
            self.define_layer("port_a", f"{name} terminal A pin")
            self.define_layer("port_b", f"{name} terminal B pin")
            for index in range(sub_layer_count):
                self.define_layer(f"sub{index}", f"{name} substrate/well {index}")
            device_class = inductor_device_class(name)
            definitions = device_class.terminal_definitions()
            ids = [definition.id() for definition in definitions]
            self._terminal_a, self._terminal_b, self._terminal_sub = ids
            self.register_device_class(device_class)

        def get_connectivity(
            self, layout: kdb.Layout, layers: list[int]
        ) -> kdb.Connectivity:
            conn = kdb.Connectivity()
            marker = layers[0]
            conn.connect(marker, marker)
            for layer in layers[1:]:
                conn.connect(layer, marker)
            return conn

        def extract_devices(self, layer_geometry: list[kdb.Region]) -> None:
            marker, port_a, port_b, *subs = layer_geometry
            component = marker.merged()
            if port_a.count() != 1 or port_b.count() != 1:
                self.error(
                    f"{name}: expected exactly one pin per port under its "
                    f"marker, found {port_a.count()} and {port_b.count()}. "
                    "The device is not extracted.",
                    kdb.Polygon(component.bbox()),
                )
                return
            sub = next(
                ((3 + i, r) for i, r in enumerate(subs) if not r.is_empty()), None
            )
            if sub is None:
                self.error(
                    f"{name}: no substrate or well region under its marker. "
                    "The device is not extracted.",
                    kdb.Polygon(component.bbox()),
                )
                return
            device = self.create_device()
            self.define_terminal(device, self._terminal_a, 1, next(port_a.each()))
            self.define_terminal(device, self._terminal_b, 2, next(port_b.each()))
            polygons = sorted(
                sub[1].each(),
                key=lambda p: (p.bbox().left, p.bbox().bottom, p.bbox().right),
            )
            self.define_terminal(device, self._terminal_sub, sub[0], polygons[0])

    return _InductorExtractor()


def _log_warning(l2n: kdb.LayoutToNetlist, message: str) -> None:
    import klayout.db as kdb

    l2n.add_log_entry(kdb.LogEntryData(kdb.Severity.Warning, message))


def _substrate_layers(
    recognition: InductorRecognition,
    deck: ExtractionDeck,
    nwell: kdb.Region,
    isolation_islands: list[kdb.Region],
) -> list[tuple[kdb.Region, str | None]]:
    """The ``SUB`` candidates under the recognised markers, highest priority
    first, as ``(region, global net name)`` -- ``None`` meaning "joins the
    well net": the substrate outside every isolation island (the deck's
    ``substrate_net``), one per island (``<substrate_net>_iso<n>``, the same
    identities the MOS bodies there take), then the well (shrunk by 1 nm so
    a marker that merely abuts a well does not join it)."""
    plain = recognition.markers - nwell
    outside, per_island = partition_region_by_islands(plain, isolation_islands)
    layers: list[tuple[kdb.Region, str | None]] = [(outside, deck.substrate_net)]
    layers += [
        (island, f"{deck.substrate_net}_iso{index}")
        for index, island in enumerate(per_island)
    ]
    layers.append(((recognition.markers & nwell).sized(-recognition.shrink_dbu), None))
    return layers


def connect_inductors(
    l2n: kdb.LayoutToNetlist,
    deck: ExtractionDeck,
    recognitions: Sequence[InductorRecognition],
    metals: Sequence[kdb.Region],
    metal_labels: Sequence[Any],
    nwell: kdb.Region,
    isolation_islands: list[kdb.Region],
) -> None:
    """Wire and extract the recognised inductors into ``l2n`` (after the
    ``metals``/``metal_labels`` layers are registered).

    Each port pin is conductor: it joins the external routing on the entry's
    ``port_metal`` and any net label drawn on that metal's label layer, so a
    terminal is named and routed like any other net. The winding itself is
    deliberately never connected. Also logs the diagnostics of every
    inductor-labelled marker that was not recognised.
    """
    for recognition in recognitions:
        for message in recognition.warnings:
            _log_warning(l2n, message)
        if recognition.markers.is_empty():
            continue
        entry = recognition.entry
        level = deck.metals.index(entry.port_metal)
        label = metal_labels[level] if level < len(metal_labels) else None
        regions = {
            "marker": recognition.markers,
            "port_a": recognition.port_a,
            "port_b": recognition.port_b,
        }
        for key, region in regions.items():
            l2n.register(region, f"{entry.name}_{key}")
        for port in (recognition.port_a, recognition.port_b):
            l2n.connect(port)
            l2n.connect(port, metals[level])
            if label is not None:
                l2n.connect(port, label)
        substrates = _substrate_layers(recognition, deck, nwell, isolation_islands)
        for index, (region, net) in enumerate(substrates):
            l2n.register(region, f"{entry.name}_sub{index}")
            regions[f"sub{index}"] = region
            if net is None:
                l2n.connect(region, nwell)
            else:
                l2n.connect_global(region, net)
        geometry: dict[str, kdb.ShapeCollection] = dict(regions)
        l2n.extract_devices(
            _build_inductor_extractor(entry.name, len(substrates)), geometry
        )
