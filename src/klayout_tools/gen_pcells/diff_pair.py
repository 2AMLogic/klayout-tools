"""``diff_pair`` reference generator PCell for ``klt gen`` (issue #1698).

Split out of ``gen.py`` as one of ten per-family ``_build_<family>_pcell()``
factories relocated into this package -- see ``gen_pcells/__init__.py`` for
the package-level rationale. The PCell class defined here delegates its
actual geometry to :func:`~klayout_tools.gen._diff_pair_layout`, which stays
in ``gen.py`` alongside the other per-family layout/geometry helpers (this
submodule only imports it, never the reverse).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import klayout.db as kdb


def _build_diff_pair_pcell() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build the ``diff_pair`` reference generator's PCell -- a differential
    pair / current mirror cell.

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
        MIN_SAME_LAYER_SPACING_UM,
        WELL_ENCLOSURE_MARGIN_UM,
        _diff_pair_layout,
        _insert_boxes,
        _insert_ring,
        _shift_box,
    )

    class _DiffPairPCell(kdb.PCellDeclarationHelper):
        """Differential pair / current mirror cell (spike section 4's
        family 4): two matched devices, each split into ``splits``
        sub-instances, interleaved in a true common-centroid cross-quad
        pattern (see :func:`_diff_pair_layout`) -- composes ``mos_array``'s
        unit-device drawing (family 1) and ``guard_ring``'s ring drawing
        (family 3). ``flavor="pfet"`` encloses the device pair's own active
        footprint in a well (independent of ``add_guard_ring``'s own,
        separate well tie -- see ``guard_ring``); ``flavor="nfet"`` (the
        default) draws no additional well shape (#208).

        ``voltage_flavor`` (issue #1054) optionally draws a PDK
        medium-voltage/thick-oxide device-class marker over the same device
        pair footprint the well shape above encloses, independent of
        ``flavor``. The default, an empty string, draws nothing (byte-for-
        byte unchanged geometry). See ``mos_array``'s own ``voltage_flavor``
        docstring and :data:`_PDK_VOLTAGE_FLAVOR_LAYERS` for the flavour
        names each PDK family recognises."""

        def __init__(self) -> None:
            super().__init__()
            self.param("w_um", self.TypeDouble, "Unit device width (um)", default=0.42)
            self.param(
                "l_um",
                self.TypeDouble,
                "Gate length (um)",
                default=GATE_LENGTH_SAFE_MIN_UM,
            )
            self.param(
                "splits",
                self.TypeInt,
                "Interleaved sub-instances per device (cross-quad splits). "
                "The two legs share one x column per terminal: every column "
                "of the 2-row checkerboard holds one Q1 and one Q2 "
                "sub-instance at the same x, so Q1_<n>_S/_D/_G and that "
                "column's Q2_* ports report identical x_um and differ only "
                "in y_um by one row pitch. A one-column-per-pin floorplan "
                "(one vertical routing column per pin, one horizontal track "
                "per net) therefore cannot give the two legs' distinct nets "
                "separate columns -- see docs/cli/gen.md's diff_pair section "
                "for why this is inherent to the common-centroid interleave, "
                "and for the routing styles that do work",
                default=2,
            )
            self.param(
                "add_guard_ring",
                self.TypeBoolean,
                "Enclose the pair in an automatically-sized guard ring",
                default=True,
            )
            self.param(
                "ring_gap_side",
                self.TypeString,
                "Cut one routing opening through the guard ring on this side: "
                "'' (default, closed ring), 'N', 'S', 'E' or 'W'",
                default="",
            )
            self.param(
                "ring_gap_um",
                self.TypeDouble,
                "Length of the guard-ring opening along its side (um), when "
                "ring_gap_side is set",
                default=0.0,
            )
            self.param(
                "ring_gap_offset_um",
                self.TypeDouble,
                "Shift of the guard-ring opening from its side's midpoint (um): "
                "+x on 'N'/'S', +y on 'E'/'W'",
                default=0.0,
            )
            self.param(
                "ring_padding_um",
                self.TypeDouble,
                "Padding between the device core and the guard ring's inner "
                "edge (um), when add_guard_ring is set",
                default=GUARD_RING_DEFAULT_PADDING_UM,
            )
            self.param(
                "row_spacing_um",
                self.TypeDouble,
                "Spacing between the two interleaved device rows (um)",
                default=MIN_SAME_LAYER_SPACING_UM,
            )
            self.param(
                "mirror",
                self.TypeBoolean,
                "Label devices M1/M2 (current mirror) instead of Q1/Q2 "
                "(differential pair)",
                default=False,
            )
            self.param(
                "flavor",
                self.TypeString,
                "Device flavor: 'nfet' (default, no well drawn) or 'pfet' "
                "(unit devices enclosed in a well on PDK families that check one)",
                default="nfet",
            )
            self.param(
                "voltage_flavor",
                self.TypeString,
                "Optional medium-voltage/thick-oxide device-class marker: "
                "'' (default, no marker drawn) or a name the resolved PDK "
                "family's role-layer table recognises (e.g. 'medium_voltage' "
                "on gf180mcu, drawing its Dualgate marker). A name the family "
                "doesn't recognise draws nothing and is reported via "
                "drc_hints.notes, never silently dropped",
                default="",
            )
            self.param(
                "gate_contact",
                self.TypeBoolean,
                "Finish the gate stack: draw a contact and a local-metal pad "
                "on each unit device's gate landing pad and report *_G on the "
                "metal role (symmetric with *_S/*_D) instead of bare poly. "
                "Raises the landing pad clear of the S/D metal, so the unit "
                "device (and any automatically-sized guard ring) grows taller",
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
                "Guard ring tap drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "well_layer",
                self.TypeLayer,
                "Guard ring well drawing layer (only used when well_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "well_present",
                self.TypeBoolean,
                "Whether well_layer is a real, DRC-checked layer for the resolved PDK",
                default=False,
            )
            self.param(
                "ring_implant_layer",
                self.TypeLayer,
                "Guard-ring implant drawing layer, exactly coincident with "
                "the tap ring (only used when add_guard_ring and "
                "ring_implant_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "ring_implant_present",
                self.TypeBoolean,
                "Whether the resolved PDK needs an implant mask to recognise "
                "the guard ring's own shape (see _ring_tap_implant_layer)",
                default=False,
            )
            self.param(
                "voltage_flavor_mark_layer",
                self.TypeLayer,
                "Medium-voltage/thick-oxide device-class marker drawing "
                "layer, sized to enclose the device pair's own footprint "
                "(only used when voltage_flavor_mark_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "voltage_flavor_mark_present",
                self.TypeBoolean,
                "Whether voltage_flavor_mark_layer is a real layer resolved "
                "for params.voltage_flavor on this PDK family",
                default=False,
            )
            self.param(
                "voltage_flavor_mark_margin_um",
                self.TypeDouble,
                "Harness-resolved margin the voltage_flavor marker box grows "
                "beyond the device pair's own shared active footprint (see "
                "_PDK_VOLTAGE_FLAVOR_MARK_MARGIN_UM)",
                default=WELL_ENCLOSURE_MARGIN_UM,
            )
            self.param(
                "gate_pad_clearance_um",
                self.TypeDouble,
                "Harness-resolved clearance the gate-poly landing pad keeps "
                "off the diffusion edge (see _PDK_GATE_PAD_ACTIVE_CLEARANCE_UM)",
                default=0.0,
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
                "needs over each unit device's own active body",
                default=False,
            )
            self.param(
                "sd_implant_margin_um",
                self.TypeDouble,
                "Harness-resolved margin the source/drain implant box grows "
                "beyond each unit device's own active box (only used when "
                "sd_implant_present)",
                default=0.0,
            )

        def display_text_impl(self) -> str:
            return f"diff_pair(w={self.w_um},l={self.l_um},splits={self.splits})"

        def produce_impl(self) -> None:
            dbu = self.layout.dbu
            li_active = self.layout.layer(self.active_layer)
            li_poly = self.layout.layer(self.poly_layer)
            li_contact = self.layout.layer(self.contact_layer)
            li_metal = self.layout.layer(self.metal_layer)
            info = _diff_pair_layout(
                self.w_um,
                self.l_um,
                self.splits,
                self.add_guard_ring,
                self.ring_gap_side,
                self.ring_gap_um,
                self.ring_gap_offset_um,
                self.ring_padding_um,
                self.row_spacing_um,
                self.gate_contact,
                self.gate_pad_clearance_um,
                self.contact_gate_offset_um,
                self.bottom_endcap_um,
                self.sd_implant_margin_um if self.sd_implant_present else 0.0,
            )
            unit_boxes = info["unit"]["boxes_um"]
            for c in info["cells"]:
                for role, li in (
                    ("active", li_active),
                    ("poly", li_poly),
                    ("contact", li_contact),
                    ("metal", li_metal),
                ):
                    _insert_boxes(
                        self.cell, li, dbu, unit_boxes[role], c["x0_um"], c["y0_um"]
                    )

            # Source/drain implant (issue #1577): drawn over every unit
            # device's own active body, mirroring mos_array's own treatment.
            if self.sd_implant_present:
                li_sd_implant = self.layout.layer(self.sd_implant_layer)
                for c in info["cells"]:
                    _insert_boxes(
                        self.cell,
                        li_sd_implant,
                        dbu,
                        unit_boxes["sd_implant"],
                        c["x0_um"],
                        c["y0_um"],
                    )

            # Shared device-pair-footprint box (well margin), used by the
            # `flavor="pfet"` well shape below -- computed once, independent
            # of `voltage_flavor`, so a caller can request either, both, or
            # neither.
            margin = WELL_ENCLOSURE_MARGIN_UM
            device_well_box = (
                -margin,
                -margin,
                info["core_w_um"] + margin,
                info["core_h_um"] + margin,
            )

            if self.flavor == "pfet" and self.well_present:
                # Enclose the device pair's own active footprint in a well,
                # independent of the (optional) automatically-sized guard
                # ring's own well tie drawn below -- both land on the same
                # well_layer, so they simply merge into one region on any
                # boolean/DRC pass over it.
                li_well = self.layout.layer(self.well_layer)
                _insert_boxes(self.cell, li_well, dbu, [device_well_box])

            # Medium-voltage/thick-oxide device-class marker (issue #1054):
            # its *own* box (issue #1577), not the well shape's -- gf180mcu's
            # real signoff deck needs the marker to reach further than the
            # well's own margin does, see
            # `_PDK_VOLTAGE_FLAVOR_MARK_MARGIN_UM`'s own docstring.
            if self.voltage_flavor_mark_present:
                li_voltage_flavor_mark = self.layout.layer(
                    self.voltage_flavor_mark_layer
                )
                mark_margin = self.voltage_flavor_mark_margin_um
                mark_box = (
                    -mark_margin,
                    -mark_margin,
                    info["core_w_um"] + mark_margin,
                    info["core_h_um"] + mark_margin,
                )
                _insert_boxes(self.cell, li_voltage_flavor_mark, dbu, [mark_box])

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
                if self.well_present and self.flavor == "pfet":
                    li_well = self.layout.layer(self.well_layer)
                    margin = WELL_ENCLOSURE_MARGIN_UM
                    well_box = (
                        -margin,
                        -margin,
                        ring["outer_w_um"] + margin,
                        ring["outer_h_um"] + margin,
                    )
                    _insert_boxes(
                        self.cell, li_well, dbu, [_shift_box(well_box, ox, oy)]
                    )
                if self.ring_implant_present:
                    # Exactly coincident with the tap ring (issue #1580,
                    # mirrors `well_island`'s own `well_tap_implant` ring
                    # precedent) -- never a blanket over the enclosed pair.
                    _insert_ring(
                        self.cell,
                        self.layout.layer(self.ring_implant_layer),
                        dbu,
                        _shift_box(ring["outer_box_um"], ox, oy),
                        _shift_box(ring["inner_box_um"], ox, oy),
                        gap_box,
                    )

    return {"diff_pair": _DiffPairPCell}
