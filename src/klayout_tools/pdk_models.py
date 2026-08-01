"""Curated MOS device-model binding table + SPICE writer delegate for
`klt extract --pdk ...` (see ``docs/cli/extract.md`` and issue #209).

**Problem this solves**: `klt extract`'s default (no ``--pdk``) SPICE output
writes each extracted MOS device as a bare ``M`` card whose model name is the
curated deck's own device-class label (``nfet``/``pfet``, see
``klayout_tools.decks.ExtractionDeck``) -- that label does not correspond to
any model a real PDK ships, so the netlist cannot bind the real PDK model
library at all (neither sky130 nor gf180mcu defines a ``.model nfet``; both
ship the real primitive device as a ``.subckt``, not a built-in ``nmos``/
``pmos`` model -- see the module docstring's "Card shape" note below). When
``klt extract`` is given ``--pdk``/``--pdk-root`` and the PDK resolves, this
module supplies the pieces that rewrite each MOS device onto the resolved
PDK's real device library instead: a small curated
``(deck_name, pdk_variant_family) -> {"nfet": ..., "pfet": ...}`` lookup
table (:func:`resolve_mos_model_table`) and a
``kdb.NetlistSpiceWriterDelegate`` subclass
(:func:`create_model_binding_delegate`) that emits an ``X`` subcircuit call
per bound device instead of KLayout's default ``M`` card.

That delegate is also `klt extract`'s **only** device-card override point
(KLayout accepts exactly one delegate per writer), so it additionally writes
the plain ``R``/``C`` cards of ``--parasitics``' generated elements -- see
:func:`create_model_binding_delegate`'s own docstring and
:mod:`klayout_tools.parasitics`.

**Scope** (deliberately narrower than the general PDK-device-metadata
resolver ``docs/design/pdk-device-corner-metadata-spike.md`` proposes as a
follow-up epic -- see issue #209's Curator enhancement): MOS family only, one
voltage flavor per PDK family (the only flavor the curated extraction decks'
device recognition distinguishes -- see ``klayout_tools.extract``'s module
docstring), two PDK families (``sky130``, ``gf180mcu``). This table is
intentionally a single small module, not scattered inline literals, so a
future ``klt pdk device`` resolver can absorb/replace it without touching the
delegate/writer plumbing.

**Card shape**: sky130 and gf180mcu both ship their primitive MOS device as a
SPICE ``.subckt`` (taking ``d g s b`` terminals plus ``l``/``w`` geometry
params), never a built-in ``nmos``/``pmos`` model -- so binding a real model
means switching the *card shape* from ``M`` (built-in device) to ``X``
(subcircuit call), not just renaming the ``M`` card's model field (KLayout's
``NetlistSpiceWriter`` only ever writes the ``M``-card shape for a
``DeviceClassMOS4Transistor`` device; ``NetlistSpiceWriterDelegate`` --
confirmed directly against the installed ``klayout.db`` module -- is
KLayout's documented escape hatch for "using subcircuits rather than the
built-in devices").

**Subcircuit names + parameter convention, verified against real fetched PDK
installs** (not guessed from this issue's own suggested names):

- ``sky130_fd_pr__nfet_01v8`` / ``sky130_fd_pr__pfet_01v8`` -- confirmed via
  ``scripts/fetch-pdks.sh``'s lambdapdk mirror, whose real SkyWater sky130
  SRAM macro netlist
  (``pdks/lambdapdk/lambdapdk/sky130/libs/sky130sram/sky130_sram_1rw1r_64x256_8/spice/sky130_sram_1rw1r_64x256_8.sp``)
  instantiates both subcircuits directly, e.g.
  ``X1000 a_511_725# a_n8_115# VDD VDD sky130_fd_pr__pfet_01v8 W=3 L=0.15`` --
  confirming both the subcircuit name and a ``d g s b <model> W=... L=...``
  terminal order matching KLayout's own default ``M``-card terminal order
  (see below).
- ``nfet_03v3`` / ``pfet_03v3`` (gf180mcu's 3.3V core-voltage flavor,
  gf180mcu's analogue to sky130's "01v8" designation -- gf180mcu has no
  ``gf180mcu_fd_pr__`` naming prefix convention the way sky130 does; verified
  by grepping the real, no-prefix subcircuit names throughout the installed
  tree) -- confirmed via a real gf180mcuA install (volare,
  ``~/.volare/gf180mcuA/libs.tech/ngspice/sm141064.ngspice``, ``.subckt
  nfet_03v3 d g s b w=1e-5 l=2.8e-7 ...``) and a real analog-IP SPICE view
  instantiating it
  (``~/.volare/gf180mcuA/libs.ref/gf180mcu_fd_io/spice/gf180mcu_fd_io.spice``,
  e.g. ``X5 n56 IE VSS VSS nfet_06v0 m=1.0 w=1.5e-6 l=700e-9 ...`` for the
  sibling 6V flavor -- same subcircuit family, confirming the no-prefix
  naming and the ``d g s b <model>`` terminal order). gf180mcu's real
  in-the-wild geometry values are written in raw SI (metres, e.g.
  ``w=1.5e-6``), never bare micrometre literals -- unlike the sky130 SRAM
  netlist's bare-micrometre convention above (which only round-trips
  correctly under an external ``.option scale=1e-6``, e.g.
  ``scripts/gallery_signals.py``'s own sim wrapper sets exactly that for its
  sky130 corner deck). :func:`create_model_binding_delegate` sidesteps this
  cross-PDK inconsistency entirely by always writing an explicit
  micrometre-unit-suffixed literal (``L=0.5U W=8U``, the same convention
  `klt extract`'s own default ``M``-card writer already uses -- see
  ``docs/cli/lvs.md``'s "Unit suffixes matter" note) rather than a bare
  number: an explicit SI unit suffix is parsed identically by ngspice
  regardless of any ``.option scale`` a downstream caller's testbench may or
  may not set, so it is correct under both PDKs' real-world conventions
  without needing to special-case either one.

Both real installs instantiate their MOS subcircuit with only ``l``/``w``
supplied -- no ``nf``/``mult``/``par`` override at the call site -- relying
on the subcircuit's own defaults (confirmed ``nf=1``/``par=1``-equivalent in
both fetched installs). The curated extraction decks' device extractor never
models multi-finger/multiplied devices either (one flat
``DeviceExtractorMOS4Transistor`` device per drawn gate -- see
``klayout_tools.extract``'s module docstring), so
:func:`create_model_binding_delegate` matches this real-world convention and
the extractor's own scope by emitting only ``L``/``W`` and omitting
``nf``/``mult``/``par`` (left at the resolved subcircuit's own default of 1).
Source/drain area+perimeter (``AS``/``AD``/``PS``/``PD``, present on the bare
``M``-card form for parasitic-capacitance modelling) are likewise not carried
onto the ``X`` card -- consistent with `klt extract`'s documented
schematic-equivalent, no-parasitics scope (``klayout_tools.extract``'s module
docstring).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import klayout.db as kdb

#: Decimal places L/W (in micrometres) are rounded to before formatting onto
#: an emitted `X` card -- mirrors `extract.py`'s `_PARAM_PRECISION_UM` (same
#: value, kept as a separate constant to avoid a cross-module import cycle:
#: `extract.py` imports this module, not the reverse).
_PARAM_PRECISION_UM = 6

#: (deck_name, pdk_variant_family) -> {"nfet": <subckt-name>, "pfet": <subckt-name>}
#: See the module docstring for how each entry's subcircuit name and
#: parameter convention were verified against a real fetched PDK install.
_MOS_MODEL_TABLE: dict[tuple[str, str], dict[str, str]] = {
    ("sky130", "sky130"): {
        "nfet": "sky130_fd_pr__nfet_01v8",
        "pfet": "sky130_fd_pr__pfet_01v8",
    },
    ("gf180mcu", "gf180mcu"): {
        "nfet": "nfet_03v3",
        "pfet": "pfet_03v3",
    },
}

#: PDK families this table knows how to classify a resolved `--pdk` variant
#: name into (e.g. "sky130A"/"sky130B" -> "sky130", "gf180mcuA".."D" ->
#: "gf180mcu") -- the same family prefixes `klayout_tools.decks`' registry
#: uses as deck names. Order matters only in that no family here is a prefix
#: of another.
_KNOWN_PDK_FAMILIES: tuple[str, ...] = ("sky130", "gf180mcu")


class ModelBindingError(Exception):
    """Raised when a resolved PDK variant has no curated MOS device-model
    table entry for the extraction deck in use.

    Covers both an unrecognised PDK family (a variant name not matching any
    known family prefix) and a recognised family with no curated entry for
    the requesting deck (e.g. running the ``sky130`` deck against a resolved
    ``gf180mcuA`` install) -- both are a deck/PDK mismatch the caller should
    see as a named, actionable error, never a silent fallback to the bare
    ``M``-card form (see ``docs/cli/extract.md``).
    """


def _pdk_variant_family(variant: str) -> str:
    """The PDK-family portion of a resolved ``--pdk`` variant name (e.g.
    ``"sky130A"`` -> ``"sky130"``, ``"gf180mcuC"`` -> ``"gf180mcu"``).

    Returns ``variant`` unchanged when it does not match any known family
    prefix -- :func:`resolve_mos_model_table` turns that into a named
    :class:`ModelBindingError` rather than this helper raising, so the
    error message can report the full attempted ``(deck, variant)`` pair.
    """
    for family in _KNOWN_PDK_FAMILIES:
        if variant.startswith(family):
            return family
    return variant


def resolve_mos_model_table(deck_name: str, pdk_variant: str) -> dict[str, str]:
    """Resolve the curated ``{"nfet": ..., "pfet": ...}`` subcircuit-name
    table for ``deck_name`` against ``pdk_variant``'s PDK family.

    Raises :class:`ModelBindingError` (which ``extract.py`` turns into an
    :class:`~klayout_tools.extract.ExtractError`) when ``pdk_variant``'s
    family is unrecognised, or is recognised but has no curated entry for
    ``deck_name`` -- never returns a partial/guessed table.
    """
    family = _pdk_variant_family(pdk_variant)
    table = _MOS_MODEL_TABLE.get((deck_name, family))
    if table is None:
        available = ", ".join(
            f"deck '{d}' + PDK family '{f}'" for d, f in sorted(_MOS_MODEL_TABLE)
        )
        raise ModelBindingError(
            f"no curated PDK device-model binding for deck '{deck_name}' + "
            f"PDK variant '{pdk_variant}' (resolved family '{family}'); "
            f"available bindings: {available}"
        )
    return table


def _format_um(value: float) -> str:
    """Format a micrometre value the same way KLayout's own default
    ``M``-card writer formats ``L``/``W`` (e.g. ``8.0`` -> ``"8U"``, ``0.5``
    -> ``"0.5U"``) -- an explicit unit suffix, no unnecessary trailing zeros.
    """
    rounded = round(value, _PARAM_PRECISION_UM)
    text = f"{rounded:.{_PARAM_PRECISION_UM}f}".rstrip("0").rstrip(".")
    return f"{text or '0'}U"


def create_model_binding_delegate(
    class_to_subckt: dict[str, str],
    parasitic_classes: frozenset[str] = frozenset(),
) -> kdb.NetlistSpiceWriterDelegate:
    """Build a ``kdb.NetlistSpiceWriterDelegate`` instance that writes any
    device whose class name is a key of ``class_to_subckt`` as an ``X``
    subcircuit call against the mapped subcircuit name, and defers every
    other device (there is currently no other extracted device class, but a
    future deck could add one) to KLayout's default ``M``-card behavior.

    ``parasitic_classes`` names the device classes generated by
    ``klt extract --parasitics`` (:mod:`klayout_tools.parasitics`), which get
    a **plain two-terminal card with no trailing model name**. This delegate
    is ``klt extract``'s single device-card override point, so the parasitics
    case lives here rather than in a second, unreachable delegate: KLayout
    accepts exactly one delegate per writer, and both overrides can be active
    at once (``--pdk`` *and* ``--parasitics``).

    The parasitics case is not cosmetic. KLayout's default writer appends the
    device-class name to every generic device card (``Rp1 Y Y_par 12.5
    parasitic_r``), which SPICE reads as a *semiconductor* resistor's model
    name -- ngspice rejects the netlist outright with ``can't find model
    'parasitic_r'``, which would break this command's hard "feeds ``klt sim``
    unmodified" bar. Emitting ``R<name> <a> <b> <ohms>`` / ``C<name> <a> <b>
    <farads>`` keeps the cards primitive and portable.

    ``import klayout.db`` is deferred to call time (mirrors every other
    KLayout import in ``extract.py``) so importing this module never pays
    the cost of loading the KLayout database module.
    """
    import klayout.db as kdb

    class _ModelBindingSpiceWriterDelegate(kdb.NetlistSpiceWriterDelegate):
        def __init__(self, mapping: dict[str, str], parasitics: frozenset[str]) -> None:
            super().__init__()
            self._class_to_subckt = mapping
            self._parasitic_classes = parasitics

        def write_device(self, device: kdb.Device) -> None:
            device_class = device.device_class()
            if device_class.name in self._parasitic_classes:
                self._write_parasitic(device, device_class)
                return

            subckt = self._class_to_subckt.get(device_class.name)
            if subckt is None:
                super().write_device(device)
                return

            terminal_ids = {
                terminal.name: terminal.id()
                for terminal in device_class.terminal_definitions()
            }

            def net_str(terminal_name: str) -> str:
                net = device.net_for_terminal(terminal_ids[terminal_name])
                return self.net_to_string(net)

            l_um: float | None = None
            w_um: float | None = None
            for param in device_class.parameter_definitions():
                if param.name == "L":
                    l_um = device.parameter(param.id())
                elif param.name == "W":
                    w_um = device.parameter(param.id())

            name = self.format_name(device.expanded_name())
            self.emit_line(
                f"X{name} {net_str('D')} {net_str('G')} {net_str('S')} "
                f"{net_str('B')} {subckt} "
                f"L={_format_um(l_um or 0.0)} W={_format_um(w_um or 0.0)}"
            )

        def _write_parasitic(
            self, device: kdb.Device, device_class: kdb.DeviceClass
        ) -> None:
            """A bare ``R``/``C`` card (see this factory's docstring): two
            terminals, one value, no trailing model name."""
            terminal_ids = {
                terminal.name: terminal.id()
                for terminal in device_class.terminal_definitions()
            }
            parameter_ids = {
                param.name: param.id() for param in device_class.parameter_definitions()
            }

            if "R" in parameter_ids:
                letter, value = "R", device.parameter(parameter_ids["R"])
            else:
                letter, value = "C", device.parameter(parameter_ids["C"])

            name = self.format_name(device.expanded_name())
            terminal_a = self.net_to_string(device.net_for_terminal(terminal_ids["A"]))
            terminal_b = self.net_to_string(device.net_for_terminal(terminal_ids["B"]))
            self.emit_line(f"{letter}{name} {terminal_a} {terminal_b} {value:.6g}")

    return _ModelBindingSpiceWriterDelegate(class_to_subckt, parasitic_classes)
