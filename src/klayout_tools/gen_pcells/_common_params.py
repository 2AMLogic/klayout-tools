"""Shared ``self.param`` declaration helpers for the ``gen_pcells`` factories
(issue #5713).

The per-family factories relocated here from ``gen.py`` (issue #1698) carried
byte-identical copies of the harness-computed plumbing declarations with them
-- every PDK-resolved layer flag and geometry floor the harness sets by name
at ``Layout.add_pcell_variant`` time. This module is where those copies live
once each; a factory declares them by calling the helper for its cluster at
the position the first removed declaration occupied.

Every param declared by a helper here is a member of
``klayout_tools.gen._HIDDEN_PARAMS`` -- the set ``klt gen --list`` filters out
of its emitted ``params`` array and the request validator rejects from caller
input -- so neither the shared description text nor the helper call's position
inside ``__init__`` is observable through the JSON contract. Public params
must stay declared inline in their factory: their descriptions and array
position are the contract.

Helpers take the declaration object and the ``klayout.db`` module as plain
``Any`` arguments (and never import ``klayout.db`` at module scope) so a
factory's lazy-import discipline is preserved and the dynamic ``param`` /
``Type*`` attributes add no ``attr-defined`` diagnostics -- the same convention
``esd_device``'s own ``_declare_flavor_mark_params`` established.
"""

from __future__ import annotations

from typing import Any


def declare_cut_and_metal_params(pcell: Any, kdb: Any) -> None:
    """Declare the cut/metal plumbing every geometry PCell shares: the
    contact and local-routing metal drawing layers, plus the two
    harness-computed cut knobs resolved per PDK family and cut layer."""
    pcell.param(
        "contact_layer",
        pcell.TypeLayer,
        "Contact drawing layer",
        default=kdb.LayerInfo(0, 0),
    )
    pcell.param(
        "metal_layer",
        pcell.TypeLayer,
        "Local routing metal drawing layer",
        default=kdb.LayerInfo(0, 0),
    )
    # Fixed drawn side of every cut on contact_layer (issue #2585) --
    # harness-computed by `_cut_fixed_size_um` from the resolved PDK
    # family and layer. `0.0` (no upper size bound) leaves the cuts
    # exactly as laid out; see `gen._clamp_cut_boxes`.
    pcell.param(
        "contact_fixed_size_um",
        pcell.TypeDouble,
        "Fixed drawn side (um) every cut on contact_layer is clamped "
        "down to on the resolved PDK family -- 0.0 leaves the "
        "generator's own cut size unchanged",
        default=0.0,
    )
    pcell.param(
        "mfg_grid_um",
        pcell.TypeDouble,
        "Manufacturing grid (um) every cut on contact_layer is "
        "snapped onto (issue #2648) -- 0.0 leaves cut positions "
        "unchanged",
        default=0.0,
    )


def declare_tap_and_well_layers(pcell: Any, kdb: Any) -> None:
    """Declare the substrate/well tap and well drawing layers with the
    neutral wording the two frame-shaped PCells (``guard_ring``,
    ``well_island``) share."""
    pcell.param(
        "tap_layer",
        pcell.TypeLayer,
        "Substrate/well tap drawing layer",
        default=kdb.LayerInfo(0, 0),
    )
    pcell.param(
        "well_layer",
        pcell.TypeLayer,
        "Well drawing layer (only used when well_present)",
        default=kdb.LayerInfo(0, 0),
    )


def declare_well_present(pcell: Any) -> None:
    """Declare ``well_present``, the harness-computed flag telling the
    generator whether its ``well_layer`` param resolved to a real,
    DRC-checked layer for the PDK family."""
    pcell.param(
        "well_present",
        pcell.TypeBoolean,
        "Whether well_layer is a real, DRC-checked layer for the resolved PDK",
        default=False,
    )


def declare_ring_implant_params(pcell: Any, kdb: Any) -> None:
    """Declare the guard-ring implant plumbing (issue #1580): the implant
    drawing layer tracking the tap ring's band, the flag saying the resolved
    PDK needs that mask, and how far the implant extends past the band
    (issue #2369). ``mos_array``/``diff_pair``/``esd_device`` share this
    wording; ``bjt_array`` (collector ring) and ``guard_ring`` (no
    ``add_guard_ring`` gate) keep family-specific declarations."""
    pcell.param(
        "ring_implant_layer",
        pcell.TypeLayer,
        "Guard-ring implant drawing layer, tracking the tap ring's "
        "own band with ring_implant_margin_um of extension beyond "
        "every edge of it (only used when add_guard_ring and "
        "ring_implant_present)",
        default=kdb.LayerInfo(0, 0),
    )
    pcell.param(
        "ring_implant_present",
        pcell.TypeBoolean,
        "Whether the resolved PDK needs an implant mask to recognise "
        "the guard ring's own shape (see _ring_tap_implant_layer)",
        default=False,
    )
    pcell.param(
        "ring_implant_margin_um",
        pcell.TypeDouble,
        "Harness-resolved margin the ring implant extends beyond the "
        "ring's own band on every edge (only used when "
        "ring_implant_present)",
        default=0.0,
    )


def declare_ring_well_margin(pcell: Any, default_margin_um: float) -> None:
    """Declare ``ring_well_margin_um``; the default is the caller's
    ``WELL_ENCLOSURE_MARGIN_UM`` (passed in, like ``esd_device``'s own
    flavour-mark helper, so this module never imports ``gen`` at scope)."""
    pcell.param(
        "ring_well_margin_um",
        pcell.TypeDouble,
        "Harness-resolved nwell enclosure of the n-tap guard ring "
        "(see _PDK_RING_WELL_MARGIN_UM)",
        default=default_margin_um,
    )


def declare_mos_stack_params(pcell: Any, kdb: Any) -> None:
    """Declare the base MOS layer stack and its harness-resolved gate
    geometry floors (issue #1577), shared by ``mos_array``,
    ``diff_pair`` and ``esd_device``."""
    pcell.param(
        "active_layer",
        pcell.TypeLayer,
        "Active/diffusion drawing layer",
        default=kdb.LayerInfo(0, 0),
    )
    pcell.param(
        "poly_layer",
        pcell.TypeLayer,
        "Poly gate drawing layer",
        default=kdb.LayerInfo(0, 0),
    )
    pcell.param(
        "contact_gate_offset_um",
        pcell.TypeDouble,
        "Harness-resolved extra S/D-contact-to-gate offset floor "
        "(see _PDK_CONTACT_GATE_EXTRA_OFFSET_UM)",
        default=0.0,
    )
    pcell.param(
        "bottom_endcap_um",
        pcell.TypeDouble,
        "Harness-resolved gate-stripe bottom-edge endcap extension "
        "(see _PDK_GATE_BOTTOM_ENDCAP_UM)",
        default=0.0,
    )
    pcell.param(
        "top_endcap_um",
        pcell.TypeDouble,
        "Harness-resolved gate-stripe top-edge endcap neck "
        "(see _PDK_GATE_TOP_ENDCAP_UM)",
        default=0.0,
    )
    pcell.param(
        "sd_implant_layer",
        pcell.TypeLayer,
        "Source/drain implant drawing layer (only used when sd_implant_present)",
        default=kdb.LayerInfo(0, 0),
    )


def declare_mos_array_extra_params(pcell: Any, kdb: Any) -> None:
    """Declare the harness-resolved extras the two MOS array generators
    (``mos_array``, ``diff_pair``) add beyond :func:`declare_mos_stack_params`:
    the source/drain implant box knobs (issue #1577), the gate-pad
    clearance floor, and the npc plumbing. ``esd_device`` declares its own
    singular-wording ``sd_implant_present``/``sd_implant_margin_um`` and
    has no ``sd_implant_min_space_um``/``gate_pad_clearance_um``/npc params."""
    pcell.param(
        "sd_implant_present",
        pcell.TypeBoolean,
        "Whether sd_implant_layer is a real layer this PDK family "
        "needs over each unit device's own active body",
        default=False,
    )
    pcell.param(
        "sd_implant_margin_um",
        pcell.TypeDouble,
        "Harness-resolved margin the source/drain implant box grows "
        "beyond each unit device's own active box (only used when "
        "sd_implant_present)",
        default=0.0,
    )
    pcell.param(
        "sd_implant_min_space_um",
        pcell.TypeDouble,
        "Harness-resolved minimum spacing between separate source/"
        "drain implant shapes; closer ones are merged (only used "
        "when sd_implant_present)",
        default=0.0,
    )
    pcell.param(
        "gate_pad_clearance_um",
        pcell.TypeDouble,
        "Harness-resolved clearance the gate-poly landing pad keeps "
        "off the diffusion edge (see _PDK_GATE_PAD_ACTIVE_CLEARANCE_UM)",
        default=0.0,
    )
    pcell.param(
        "npc_layer",
        pcell.TypeLayer,
        "npc drawing layer (only used when npc_present)",
        default=kdb.LayerInfo(0, 0),
    )
    pcell.param(
        "npc_present",
        pcell.TypeBoolean,
        "Whether npc_layer is a real layer this PDK family needs "
        "over each poly-contact licon",
        default=False,
    )
    pcell.param(
        "npc_margin_um",
        pcell.TypeDouble,
        "Harness-resolved margin npc grows beyond each poly-contact "
        "licon (only used when npc_present)",
        default=0.0,
    )
    pcell.param(
        "npc_min_space_um",
        pcell.TypeDouble,
        "Harness-resolved minimum npc spacing; closer shapes merge "
        "(only used when npc_present)",
        default=0.0,
    )


def declare_voltage_flavor_mark_params(pcell: Any, kdb: Any) -> None:
    """Declare the ``voltage_flavor`` marker-layer flags the two array
    generators share (issue #1577). The ``voltage_flavor_mark_layer`` and
    ``voltage_flavor_mark_margin_um`` descriptions name each family's own
    footprint, so those two stay declared inline in each factory."""
    pcell.param(
        "voltage_flavor_mark_present",
        pcell.TypeBoolean,
        "Whether voltage_flavor_mark_layer is a real layer resolved "
        "for params.voltage_flavor on this PDK family",
        default=False,
    )
    pcell.param(
        "voltage_flavor_extra_mark_layer",
        pcell.TypeLayer,
        "Second voltage_flavor marker layer drawn over the same box "
        "(sky130 native-Vt: lvtn alongside hvi; only used when "
        "voltage_flavor_extra_mark_present)",
        default=kdb.LayerInfo(0, 0),
    )
    pcell.param(
        "voltage_flavor_extra_mark_present",
        pcell.TypeBoolean,
        "Whether voltage_flavor_extra_mark_layer is a real layer "
        "resolved for params.voltage_flavor on this PDK family",
        default=False,
    )


def declare_dummy_marker_params(pcell: Any) -> None:
    """Declare ``dummy_present``, the harness-computed flag for the PDK's
    dummy-device marker layer (``ExtractionDeck.dummy``). The
    ``dummy_layer`` description names each family's own footprint, so it
    stays declared inline in each factory."""
    pcell.param(
        "dummy_present",
        pcell.TypeBoolean,
        "Whether dummy_layer is a real layer this PDK's extraction "
        "deck declares (see ExtractionDeck.dummy)",
        default=False,
    )


def declare_metal_label_present(pcell: Any) -> None:
    """Declare ``metal_label_present``; the ``metal_label_layer``
    description names each family's own label text, so it stays declared
    inline in each factory."""
    pcell.param(
        "metal_label_present",
        pcell.TypeBoolean,
        "Whether metal_label_layer is a real label layer for the resolved PDK",
        default=False,
    )


def declare_vpp_gds_path(pcell: Any) -> None:
    """Declare ``vpp_gds_path``, the harness-resolved GDS file holding the
    PDK fixed cell (``cap_vpp_array`` and ``pdk_cell``)."""
    pcell.param(
        "vpp_gds_path",
        pcell.TypeString,
        "Resolved PDK GDS holding the cell (harness-computed)",
        default="",
    )
