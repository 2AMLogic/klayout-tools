"""Curated junction-diode subcircuit bindings for `klt extract --pdk` and
`klt lvs`'s reference-side ingestion.

Split out of :mod:`klayout_tools.pdk_models` (issue #5491), which owns every
other device family's tables and the SPICE writer delegate itself. The diode
family earns its own module for the same reason
:mod:`klayout_tools._capacitor_mf` does: it is one self-contained curated fact
plus the two builders that read it, and `pdk_models.py` is already the largest
module in the binding layer.

Both builders import their result dataclass from ``pdk_models`` *inside* the
function body -- ``pdk_models`` imports this module at module level, so the
deferred import is what keeps the pair acyclic, exactly as
``_capacitor_mf.capacitor_mf_is_m_alias`` imports ``NormalizeError``.

Two directions, two gates:

- **Ingestion** (:func:`diode_lookup_bindings`, issue #4319) -- every curated
  subcircuit name is recognised when reading a reference netlist back, for
  every deck in :data:`DIODE_MODEL_TABLE`.
- **Writing** (:func:`diode_write_bindings`, issue #5491) -- additionally
  gated on :data:`DIODE_PARAM_STYLE`, which is what records that the family's
  cells state their junction as a call-site ``l``/``w`` rectangle the writer
  can fill from the extractor's measured area+perimeter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .pdk_models import DeviceBinding, DeviceLookup


#: ``(deck_name, family) -> {device-class name: subcircuit name}`` for the PDK
#: junction-diode subcircuits (issue #4319). Each is a two-terminal
#: ``anode cathode`` cell; sky130's take ``area``/``perim`` (``pj``) call-site
#: parameters, sg13g2's take ``l``/``w`` (see :data:`DIODE_PARAM_STYLE`). The
#: sky130 subcircuit name is ``sky130_fd_pr__`` + the deck's ``DiodeDevice``
#: class name (a test iterates ``EXTRACTION_DECK.diodes`` so a new class cannot
#: be silently missed).
DIODE_MODEL_TABLE: dict[tuple[str, str], dict[str, str]] = {
    ("sky130", "sky130"): {
        name: f"sky130_fd_pr__{name}"
        for name in (
            "diode_pw2nd_05v5",
            "diode_pw2nd_05v5_lvt",
            "diode_pw2nd_05v5_nvt",
            "diode_pw2nd_11v0",
            "diode_pd2nw_05v5",
            "diode_pd2nw_05v5_lvt",
            "diode_pd2nw_05v5_hvt",
            "diode_pd2nw_11v0",
        )
    },
    # sg13g2's two antenna diodes (issue #5491). Confirmed against a real
    # fetched IHP-Open-PDK v0.3.0 install (`scripts/fetch-ihp-sg13g2.sh`):
    # `ihp-sg13g2/libs.tech/ngspice/models/diodes.lib` declares
    # `.subckt dantenna 1 2` and `.subckt dpantenna 1 2`, each with
    # `.params l=780n w=780n DEV_A=(l*w)/1p DEV_P=(l+w)/0.5u DEV_C=4` and a
    # body of three `.MODEL`-backed `D` cards (`darea`/`dperim`/`dcorner`,
    # `dparea`/`dpperim`/`dpcorner`). The *only* definitions of these two
    # names in that install are those subcircuits -- there is no
    # `.model dantenna`/`.model dpantenna` anywhere -- so the bare `D` card
    # KLayout's default writer emits for this class ("could not find a valid
    # modelname") cannot be simulated at all, unlike gf180mcu's `bjt` or
    # sg13g2's `res_metal*`, whose bare cards name a token a consumer's own
    # deck can still supply a `.model` for. The subcircuit name is the deck's
    # own `DiodeDevice` class name (the deck transcribed its class names from
    # the same PDK's `diode_extraction.lvs`), and pin 1 is the anode / pin 2
    # the cathode (the subckt's own `(anode cathode)` comment), matching
    # `DeviceClassDiode`'s `A`/`C` terminal order.
    ("sg13g2", "sg13g2"): {
        "dantenna": "dantenna",
        "dpantenna": "dpantenna",
    },
}

#: Per-PDK-family diode subcircuit length/width parameter spellings -- the
#: diode counterpart of ``pdk_models``' own ``_RESISTOR_PARAM_STYLE``/
#: ``_CAPACITOR_PARAM_STYLE``, and the gate on *forward* (``klt extract
#: --pdk``) diode binding (issue #5491).
#:
#: A family listed here states its junction geometry at the call site as a
#: rectangle (``l``/``w``), which :func:`diode_write_bindings` fills from the
#: extractor's measured area+perimeter via ``pdk_models``'
#: ``equivalent_rectangle_um`` -- exactly as the capacitor binding already
#: does for a MiM plate. A family *not* listed here is ingestion-only: its
#: subcircuits are still recognised when reading a reference netlist back,
#: but ``klt extract --pdk`` keeps writing their bare ``D`` card. sky130 is
#: deliberately absent: its diode cells take ``area``/``perim`` rather than
#: ``l``/``w``, and whether its class-named bare ``D`` cards resolve against
#: that PDK was not re-verified here (issue #5491 scoped the forward
#: direction to sg13g2) -- adding a row is all that is needed once it is.
#:
#: Why the equivalent rectangle is exact for sg13g2 rather than an
#: approximation: both subcircuits use ``l``/``w`` *only* through
#: ``DEV_A=(l*w)/1p`` and ``DEV_P=(l+w)/0.5u``, so any ``(l, w)`` pair
#: reproducing the device's measured area and perimeter hands the underlying
#: ``.MODEL`` cards identical ``DEV_A``/``DEV_P``, whatever the drawn shape
#: was. ``DEV_C`` (corner count) stays at the subcircuit's default of 4 --
#: the extractor does not measure it.
DIODE_PARAM_STYLE: dict[str, tuple[str, str]] = {
    "sg13g2": ("l", "w"),
}


def diode_lookup_bindings(deck_name: str, family: str) -> dict[str, DeviceLookup]:
    """``<subckt-name> -> DeviceLookup("diode", ...)`` for every junction-diode
    :data:`DIODE_MODEL_TABLE` curates for one ``(deck_name, family)`` pair;
    empty for a pair the table does not cover.

    Resolved purely by subcircuit name, like bipolar. A family with a
    :data:`DIODE_PARAM_STYLE` row (sg13g2) additionally carries that row's
    ``l``/``w`` spellings, so the ingestion direction can recover the card's
    ``A``/``P`` from a geometry-parameterized call (issue #5491); a family
    without one keeps the ``None``/``None`` pair #4319 established, whose
    calls state ``area``/``perim`` instead.
    """
    from .pdk_models import DeviceLookup

    style: tuple[str, str] | None = DIODE_PARAM_STYLE.get(family)
    length_param: str | None = style[0] if style else None
    width_param: str | None = style[1] if style else None
    return {
        subckt: DeviceLookup("diode", device_class, length_param, width_param)
        for device_class, subckt in DIODE_MODEL_TABLE.get(
            (deck_name, family), {}
        ).items()
    }


def diode_write_bindings(
    deck_name: str,
    family: str,
    deck: Any,
    geometry_style: str,
) -> dict[str, DeviceBinding]:
    """``{device-class name -> DeviceBinding("diode", ...)}`` for every
    junction-diode class ``deck`` declares that this module can write as an
    ``X`` call (issue #5491); empty for every other deck.

    Two curated facts must agree before a class is bound: a
    :data:`DIODE_MODEL_TABLE` row naming its real subcircuit, and a
    :data:`DIODE_PARAM_STYLE` row for the family, which is what says the
    subcircuit states its junction as a call-site ``l``/``w`` rectangle the
    writer can fill from the extractor's measured area+perimeter. sky130 has
    the first and deliberately not the second -- see that table's docstring --
    so its diodes stay ingestion-only and keep their bare ``D`` card here,
    the same "never guess a card shape" discipline the bipolar and
    ``res_metal*`` carve-outs follow.
    """
    from .pdk_models import DeviceBinding

    style = DIODE_PARAM_STYLE.get(family)
    if style is None:
        return {}
    length_param, width_param = style
    table = DIODE_MODEL_TABLE.get((deck_name, family), {})
    return {
        diode.name: DeviceBinding(
            "diode",
            table[diode.name],
            ("A", "C"),
            length_param,
            width_param,
            geometry_style=geometry_style,
        )
        for diode in getattr(deck, "diodes", ())
        if diode.name in table
    }
