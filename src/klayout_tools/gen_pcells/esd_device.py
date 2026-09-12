"""``esd_device`` reference generator PCell for ``klt gen`` (issue #1698).

Split out of ``gen.py`` as one of ten per-family ``_build_<family>_pcell()``
factories relocated into this package -- see ``gen_pcells/__init__.py`` for
the package-level rationale. The PCell class defined here delegates its
actual geometry to :func:`~klayout_tools.gen._esd_device_layout` (composing
the ``guard_ring`` family's own ring-drawing helper), which stays in
``gen.py`` alongside the other per-family layout/geometry helpers (this
submodule only imports it, never the reverse).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import klayout.db as kdb


def _build_esd_device_pcell() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build the ``esd_device`` reference generator's PCell -- an ESD protection
    device.

    Defined inside a function (not at module scope) so importing
    ``klayout_tools.gen`` doesn't pay ``klayout.db``'s load cost until a
    caller actually runs a generator -- the same lazy-import discipline
    ``layers.py``/``render.py`` use for the same reason. See
    :func:`_build_pcell_classes`, the thin composer that merges every
    per-family factory's single-entry dict together.
    """
    import klayout.db as kdb

    from klayout_tools.gen import (
        GATE_LENGTH_SAFE_MIN_UM,
        GUARD_RING_DEFAULT_PADDING_UM,
        _esd_device_layout,
        _insert_boxes,
        _insert_ring,
        _shift_box,
    )

    class _EsdDevicePCell(kdb.PCellDeclarationHelper):
        """Grounded-gate multi-finger ESD protection MOS (issue #569): one
        multi-finger unit device (see :func:`_mos_unit_layout` -- ``fingers``
        gate stripes across a shared diffusion, the standard ggNMOS ESD-clamp
        layout idiom), optionally enclosed in an automatically-sized tap ring
        composing ``guard_ring``'s own ring-drawing helper (family 3), the
        same composition mechanism ``diff_pair`` already uses for families 1
        and 3 (see :func:`_esd_device_layout`).

        Always an NMOS-style device (no ``flavor``/well option, unlike
        ``mos_array``/``diff_pair``, and -- unlike every other ring-composing
        generator -- the tap ring itself draws **no** well tie either): a
        grounded-gate ESD clamp is conventionally NMOS, and enclosing it in
        an Nwell the way ``guard_ring``'s own ``add_well`` default would
        silently misclassifies the device as ``pfet`` under ``klt
        extract``'s ``active & nwell`` test (see
        :func:`_esd_device_layer_params`'s docstring for the full
        explanation -- the same well ``diff_pair`` suppresses for its own
        default ``flavor="nfet"`` case).

        Draws two optional marker layers, neither DRC-checked by either
        curated deck (see :data:`_PDK_ROLE_LAYERS`'s ``esd_mark``/
        ``salicide_block`` role comments for citations and the sky130 gap):
        ``esd_mark`` (always drawn when the resolved family has one -- an
        unconditional device-class marker, mirroring ``bjt_mark``'s own
        always-on-when-present precedent) and ``salicide_block`` (drawn only
        when ``params.salicide_block`` opts in -- a ballast-style unsalicided
        region over the whole finger footprint, the same "floorplan
        fidelity, not a process-exact cross-section" approximation
        `_bjt_unit_layout` already documents for its own device).
        """

        def __init__(self) -> None:
            super().__init__()
            self.param(
                "finger_width_um",
                self.TypeDouble,
                "Width of each gate finger (um)",
                default=2.0,
            )
            self.param(
                "l_um",
                self.TypeDouble,
                "Gate length (um)",
                default=GATE_LENGTH_SAFE_MIN_UM,
            )
            self.param("fingers", self.TypeInt, "Gate fingers", default=4)
            self.param(
                "add_guard_ring",
                self.TypeBoolean,
                "Enclose the device in an automatically-sized tap ring",
                default=True,
            )
            self.param(
                "ring_gap_side",
                self.TypeString,
                "Cut one routing opening through the tap ring on this side: "
                "'' (default, closed ring), 'N', 'S', 'E' or 'W'",
                default="",
            )
            self.param(
                "ring_gap_um",
                self.TypeDouble,
                "Length of the tap-ring opening along its side (um), when "
                "ring_gap_side is set",
                default=0.0,
            )
            self.param(
                "ring_gap_offset_um",
                self.TypeDouble,
                "Shift of the tap-ring opening from its side's midpoint (um): "
                "+x on 'N'/'S', +y on 'E'/'W'",
                default=0.0,
            )
            self.param(
                "ring_padding_um",
                self.TypeDouble,
                "Padding between the finger array and the tap ring's inner "
                "edge (um), when add_guard_ring is set",
                default=GUARD_RING_DEFAULT_PADDING_UM,
            )
            self.param(
                "gate_contact",
                self.TypeBoolean,
                "Finish the gate stack: draw a contact and a local-metal pad "
                "on the gate landing pad and report M1_G on the metal role "
                "instead of bare poly -- see mos_array's equivalent note",
                default=False,
            )
            self.param(
                "salicide_block",
                self.TypeBoolean,
                "Draw the PDK's salicide-block layer over the finger array "
                "on families that curate one (see the esd_mark/salicide_block "
                "role comments in _PDK_ROLE_LAYERS)",
                default=False,
            )
            self.param(
                "active_layer",
                self.TypeLayer,
                "Active/diffusion drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "poly_layer",
                self.TypeLayer,
                "Poly gate drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "contact_layer",
                self.TypeLayer,
                "Contact drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "metal_layer",
                self.TypeLayer,
                "Local routing metal drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "tap_layer",
                self.TypeLayer,
                "Tap ring drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "esd_mark_layer",
                self.TypeLayer,
                "ESD device-class marker drawing layer (only used when "
                "esd_mark_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "esd_mark_present",
                self.TypeBoolean,
                "Whether esd_mark_layer is a real layer this PDK family cites",
                default=False,
            )
            self.param(
                "salicide_block_layer",
                self.TypeLayer,
                "Salicide-block drawing layer (only used when "
                "salicide_block_present and params.salicide_block)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "salicide_block_present",
                self.TypeBoolean,
                "Whether salicide_block_layer is a real layer this PDK family cites",
                default=False,
            )
            self.param(
                "contact_gate_offset_um",
                self.TypeDouble,
                "Harness-resolved extra S/D-contact-to-gate offset floor "
                "(see _PDK_CONTACT_GATE_EXTRA_OFFSET_UM)",
                default=0.0,
            )
            self.param(
                "bottom_endcap_um",
                self.TypeDouble,
                "Harness-resolved gate-stripe bottom-edge endcap extension "
                "(see _PDK_GATE_BOTTOM_ENDCAP_UM)",
                default=0.0,
            )
            self.param(
                "sd_implant_layer",
                self.TypeLayer,
                "Source/drain implant drawing layer (only used when "
                "sd_implant_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "sd_implant_present",
                self.TypeBoolean,
                "Whether sd_implant_layer is a real layer this PDK family "
                "needs over the unit device's own active body",
                default=False,
            )
            self.param(
                "sd_implant_margin_um",
                self.TypeDouble,
                "Harness-resolved margin the source/drain implant box grows "
                "beyond the unit device's own active box (only used when "
                "sd_implant_present)",
                default=0.0,
            )
            self.param(
                "ring_implant_layer",
                self.TypeLayer,
                "Tap-ring implant drawing layer, exactly coincident with the "
                "tap ring (only used when add_guard_ring and "
                "ring_implant_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "ring_implant_present",
                self.TypeBoolean,
                "Whether the resolved PDK needs an implant mask to recognise "
                "the tap ring's own shape (see _ring_tap_implant_layer)",
                default=False,
            )

        def display_text_impl(self) -> str:
            return (
                f"esd_device(fingers={self.fingers},w={self.finger_width_um},"
                f"l={self.l_um})"
            )

        def produce_impl(self) -> None:
            dbu = self.layout.dbu
            li_active = self.layout.layer(self.active_layer)
            li_poly = self.layout.layer(self.poly_layer)
            li_contact = self.layout.layer(self.contact_layer)
            li_metal = self.layout.layer(self.metal_layer)
            info = _esd_device_layout(
                self.finger_width_um,
                self.l_um,
                self.fingers,
                self.add_guard_ring,
                self.ring_gap_side,
                self.ring_gap_um,
                self.ring_gap_offset_um,
                self.ring_padding_um,
                self.gate_contact,
                self.contact_gate_offset_um,
                self.bottom_endcap_um,
                self.sd_implant_margin_um if self.sd_implant_present else 0.0,
            )
            unit_boxes = info["unit"]["boxes_um"]
            for role, li in (
                ("active", li_active),
                ("poly", li_poly),
                ("contact", li_contact),
                ("metal", li_metal),
            ):
                _insert_boxes(self.cell, li, dbu, unit_boxes[role])

            # Source/drain implant (issue #1577): drawn over the unit
            # device's own active body, mirroring mos_array's own treatment.
            if self.sd_implant_present:
                li_sd_implant = self.layout.layer(self.sd_implant_layer)
                _insert_boxes(self.cell, li_sd_implant, dbu, unit_boxes["sd_implant"])

            # ESD device-class marker (issue #569): unconditional whenever the
            # resolved family cites one, mirroring `bjt_mark`'s own
            # always-on-when-present precedent (`res_mark`/`bjt_mark` are
            # never behind their own boolean opt-in -- only `salicide_block`
            # below is, matching its own "optional process option" nature).
            if self.esd_mark_present:
                li_mark = self.layout.layer(self.esd_mark_layer)
                _insert_boxes(self.cell, li_mark, dbu, unit_boxes["active"])

            if self.salicide_block and self.salicide_block_present:
                li_block = self.layout.layer(self.salicide_block_layer)
                _insert_boxes(self.cell, li_block, dbu, unit_boxes["active"])

            if info["ring"] is not None and self.add_guard_ring:
                li_tap = self.layout.layer(self.tap_layer)
                ox, oy = info["ring_offset_um"]
                ring = info["ring"]
                gap_box = (
                    _shift_box(ring["gap"]["box_um"], ox, oy)
                    if ring["gap"] is not None
                    else None
                )
                _insert_ring(
                    self.cell,
                    li_tap,
                    dbu,
                    _shift_box(ring["outer_box_um"], ox, oy),
                    _shift_box(ring["inner_box_um"], ox, oy),
                    gap_box,
                )
                _insert_ring(
                    self.cell,
                    li_metal,
                    dbu,
                    _shift_box(ring["outer_box_um"], ox, oy),
                    _shift_box(ring["inner_box_um"], ox, oy),
                    gap_box,
                )
                _insert_boxes(
                    self.cell, li_contact, dbu, ring["contact_boxes_um"], ox, oy
                )
                # Deliberately no well tie drawn around the ring here (unlike
                # `_GuardRingPCell`'s own `add_well` default, and unlike
                # `diff_pair`'s `flavor="pfet"` case) -- see this class's own
                # docstring and `_esd_device_layer_params`'s for why: it
                # would enclose this always-NMOS device in an Nwell and
                # misclassify it as `pfet` under `klt extract`.
                if self.ring_implant_present:
                    # Exactly coincident with the tap ring (issue #1580,
                    # mirrors `well_island`'s own `well_tap_implant` ring
                    # precedent) -- always the substrate-tie doping (never
                    # the well-tie one), since this ring never encloses a
                    # well (see the no-well-tie note directly above).
                    _insert_ring(
                        self.cell,
                        self.layout.layer(self.ring_implant_layer),
                        dbu,
                        _shift_box(ring["outer_box_um"], ox, oy),
                        _shift_box(ring["inner_box_um"], ox, oy),
                        gap_box,
                    )

    return {"esd_device": _EsdDevicePCell}
