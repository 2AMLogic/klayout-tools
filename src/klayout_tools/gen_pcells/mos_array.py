"""``mos_array`` reference generator PCell for ``klt gen`` (issue #1698).

Split out of ``gen.py`` as one of ten per-family ``_build_<family>_pcell()``
factories relocated into this package -- see ``gen_pcells/__init__.py`` for
the package-level rationale. The PCell class defined here delegates its
actual geometry to :func:`~klayout_tools.gen._mos_array_layout`, which stays
in ``gen.py`` alongside the other per-family layout/geometry helpers (this
submodule only imports it, never the reverse).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import klayout.db as kdb


def _build_mos_array_pcell() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build the ``mos_array`` reference generator's PCell -- a matched MOS
    transistor array.

    Defined inside a function (not at module scope) so importing
    ``klayout_tools.gen`` doesn't pay ``klayout.db``'s load cost until a
    caller actually runs a generator -- the same lazy-import discipline
    ``layers.py``/``render.py`` use for the same reason. See
    :func:`_build_pcell_classes`, the thin composer that merges every
    per-family factory's single-entry dict together.
    """
    import klayout.db as kdb

    from klayout_tools.gen import (
        _MOS_ARRAY_WELL_TAP_NET_LABEL,
        GATE_LENGTH_SAFE_MIN_UM,
        GUARD_RING_DEFAULT_PADDING_UM,
        WELL_ENCLOSURE_MARGIN_UM,
        _insert_boxes,
        _insert_ring,
        _mos_array_layout,
        _shift_box,
    )

    class _MosArrayPCell(kdb.PCellDeclarationHelper):
        """Matched MOS transistor array (spike section 4's family 1): a
        ``rows`` x ``cols`` grid of identical unit devices (see
        :func:`_mos_unit_layout`), with ``dummy`` extra unit-device columns
        flanking each side and a ``topology``-selected port-numbering order
        (see :func:`_centroid_order`). ``flavor="pfet"`` additionally
        encloses every unit device (real and dummy) in a shared well on PDK
        families whose curated deck checks one (see ``well_present``);
        ``flavor="nfet"`` (the default) draws no well shape at all (#208).

        ``finger_topology`` (#777) selects what a ``fingers > 1`` unit
        device *is*: ``"parallel"`` (the default) straps the alternating
        S/D segments and ties every gate, so the unit is one folded device
        of width ``fingers * w_um``; ``"series"`` draws the bare, unstrapped
        stripes -- a chain of transistors whose interior terminals are
        neither reported nor contactable (warned about by
        :func:`_mos_array_describe`).

        ``voltage_flavor`` (issue #1054) optionally draws a PDK
        medium-voltage/thick-oxide device-class marker (e.g. gf180mcu's
        ``Dualgate``) sized to enclose every unit device (real and dummy) --
        the same shared box ``flavor="pfet"``'s well shape already encloses.
        The default, an empty string, draws nothing (byte-for-byte unchanged
        geometry). See :data:`_PDK_VOLTAGE_FLAVOR_LAYERS` for the flavour
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
                "fingers", self.TypeInt, "Gate fingers per unit device", default=1
            )
            self.param(
                "finger_topology",
                self.TypeString,
                "How a multi-finger unit device is wired: 'parallel' "
                "(default -- alternating S/D segments strapped and every "
                "gate tied, i.e. one folded device of width fingers*w_um) "
                "or 'series' (bare stripes, no straps: fingers transistors "
                "chained source-to-drain on independent, uncontactable "
                "gates). No effect when fingers is 1",
                default="parallel",
            )
            self.param("rows", self.TypeInt, "Array rows", default=2)
            self.param("cols", self.TypeInt, "Array columns", default=2)
            self.param(
                "topology",
                self.TypeString,
                "Port-numbering topology: 'array' (row-major) or "
                "'common_centroid' (centroid-symmetric pairing)",
                default="common_centroid",
            )
            self.param(
                "dummy",
                self.TypeInt,
                "Dummy unit-device columns added on each side of the array",
                default=1,
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
                "on each unit device's gate landing pad and report U<i>_G on "
                "the metal role (symmetric with U<i>_S/U<i>_D) instead of "
                "bare poly. Raises the landing pad clear of the S/D metal, "
                "so the unit device grows taller",
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
                "well_layer",
                self.TypeLayer,
                "Well drawing layer (only used when flavor is 'pfet' and well_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "well_present",
                self.TypeBoolean,
                "Whether well_layer is a real, DRC-checked layer for the resolved PDK",
                default=False,
            )
            self.param(
                "dummy_layer",
                self.TypeLayer,
                "PDK dummy-device marker drawing layer, covering each dummy "
                "unit device's gate footprint (only used when dummy_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "dummy_present",
                self.TypeBoolean,
                "Whether dummy_layer is a real layer this PDK's extraction "
                "deck declares (see ExtractionDeck.dummy)",
                default=False,
            )
            self.param(
                "well_tap_implant_layer",
                self.TypeLayer,
                "Well-tie implant layer drawn over the well-tap pad (only "
                "used when flavor is 'pfet', well_present, and "
                "well_tap_implant_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "well_tap_implant_present",
                self.TypeBoolean,
                "Whether the resolved PDK needs an implant mask to recognise "
                "a tap pad on the shared active role as a well tie",
                default=False,
            )
            self.param(
                "metal_label_layer",
                self.TypeLayer,
                "Label layer the well-tap pad's fixed net-name text is drawn "
                "on (only used when well_tap_implant_present and "
                "metal_label_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "metal_label_present",
                self.TypeBoolean,
                "Whether metal_label_layer is a real label layer for the resolved PDK",
                default=False,
            )
            self.param(
                "voltage_flavor_mark_layer",
                self.TypeLayer,
                "Medium-voltage/thick-oxide device-class marker drawing "
                "layer, sized to enclose every unit device (only used when "
                "voltage_flavor_mark_present)",
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
                "beyond the array's own shared active footprint (see "
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
            self.param(
                "add_guard_ring",
                self.TypeBoolean,
                "Enclose the array in an automatically-sized tap/guard ring "
                "(issue #1493)",
                default=False,
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
                "Padding between the array's own shared-footprint box "
                "(well_box_um) and the guard ring's inner edge (um), when "
                "add_guard_ring is set",
                default=GUARD_RING_DEFAULT_PADDING_UM,
            )
            self.param(
                "tap_layer",
                self.TypeLayer,
                "Guard ring tap drawing layer (only used when add_guard_ring is set)",
                default=kdb.LayerInfo(0, 0),
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

        def display_text_impl(self) -> str:
            return f"mos_array({self.rows}x{self.cols},w={self.w_um},l={self.l_um})"

        def produce_impl(self) -> None:
            dbu = self.layout.dbu
            li_active = self.layout.layer(self.active_layer)
            li_poly = self.layout.layer(self.poly_layer)
            li_contact = self.layout.layer(self.contact_layer)
            li_metal = self.layout.layer(self.metal_layer)
            # Well-tie tap pad (issue #1473): only meaningful for a drawn
            # well (`flavor == 'pfet'` and `well_present`) on a family whose
            # curated deck needs an implant mask to recognise a tap pad on
            # the shared `active` role as a well tie (see
            # `_mos_array_well_tap_layout`'s own docstring). Resolved once,
            # up front, so `_mos_array_layout` computes (and
            # `well_box_um` encloses) the exact same geometry this
            # `produce_impl` draws below.
            draw_well_tap = (
                self.flavor == "pfet"
                and self.well_present
                and self.well_tap_implant_present
            )
            info = _mos_array_layout(
                self.w_um,
                self.l_um,
                self.fingers,
                self.rows,
                self.cols,
                self.dummy,
                self.topology,
                self.gate_contact,
                self.finger_topology,
                self.gate_pad_clearance_um,
                draw_well_tap,
                self.add_guard_ring,
                self.ring_gap_side,
                self.ring_gap_um,
                self.ring_gap_offset_um,
                self.ring_padding_um,
                self.contact_gate_offset_um,
                self.bottom_endcap_um,
                self.sd_implant_margin_um if self.sd_implant_present else 0.0,
                self.voltage_flavor_mark_margin_um,
            )
            unit_boxes = info["unit"]["boxes_um"]
            for c in info["cells"] + info["dummy_cells"]:
                for role, li in (
                    ("active", li_active),
                    ("poly", li_poly),
                    ("contact", li_contact),
                    ("metal", li_metal),
                ):
                    _insert_boxes(
                        self.cell, li, dbu, unit_boxes[role], c["x0_um"], c["y0_um"]
                    )

            # Source/drain implant (issue #1577): drawn over every real *and*
            # dummy unit device's own active body -- DF.12-style coverage
            # rules apply to every drawn Comp shape, dummy columns included.
            if self.sd_implant_present:
                li_sd_implant = self.layout.layer(self.sd_implant_layer)
                for c in info["cells"] + info["dummy_cells"]:
                    _insert_boxes(
                        self.cell,
                        li_sd_implant,
                        dbu,
                        unit_boxes["sd_implant"],
                        c["x0_um"],
                        c["y0_um"],
                    )

            if self.flavor == "pfet" and self.well_present:
                li_well = self.layout.layer(self.well_layer)
                _insert_boxes(self.cell, li_well, dbu, [info["well_box_um"]])

            if draw_well_tap and info["well_tap"] is not None:
                well_tap = info["well_tap"]
                _insert_boxes(self.cell, li_active, dbu, [well_tap["active"]])
                _insert_boxes(self.cell, li_contact, dbu, [well_tap["contact"]])
                _insert_boxes(self.cell, li_metal, dbu, [well_tap["metal"]])
                # Exactly coincident with the tap pad's own active box --
                # never a blanket over anything else -- so only the tap pad
                # itself is n+ implanted, mirroring `well_island`'s own
                # `well_tap_implant` ring precedent.
                _insert_boxes(
                    self.cell,
                    self.layout.layer(self.well_tap_implant_layer),
                    dbu,
                    [well_tap["active"]],
                )
                # Fixed net-name label (issue #1473, see
                # `_MOS_ARRAY_WELL_TAP_NET_LABEL`'s own docstring): without
                # *some* drawn label, `klt extract` always synthesizes an
                # anonymous `$<n>` net for this tie regardless of how well it
                # is physically connected, so `unbiased_pmos_body_nets[]`
                # would still fire even though the well is genuinely tied.
                if self.metal_label_present:
                    tap_cx, tap_cy = well_tap["xy"]
                    self.cell.shapes(self.layout.layer(self.metal_label_layer)).insert(
                        kdb.Text(
                            _MOS_ARRAY_WELL_TAP_NET_LABEL,
                            kdb.Trans(
                                kdb.Vector(
                                    int(round(tap_cx / dbu)), int(round(tap_cy / dbu))
                                )
                            ),
                        )
                    )

            # Automatically-sized tap/guard ring (issue #1493): composed the
            # same way `diff_pair`/`esd_device`/`bjt_array` already compose
            # `_ring_layout`, sized off `well_box_um` (the array's own
            # shared-footprint box, computed above regardless of `flavor`).
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
                # The ring's own well tie (independent of the device-array
                # well drawn above, which already merges with it on the same
                # `well_layer` -- mirrors `_diff_pair_layout`'s identical
                # "both land on the same layer, so they simply merge" ring
                # well-tie composition): only for `flavor == 'pfet'`, so the
                # default `flavor='nfet'` case never encloses an NMOS array
                # in a well the way #421's diff_pair regression test guards
                # against.
                if self.well_present and self.flavor == "pfet":
                    li_well = self.layout.layer(self.well_layer)
                    margin = WELL_ENCLOSURE_MARGIN_UM
                    ring_well_box = (
                        -margin,
                        -margin,
                        ring["outer_w_um"] + margin,
                        ring["outer_h_um"] + margin,
                    )
                    _insert_boxes(
                        self.cell, li_well, dbu, [_shift_box(ring_well_box, ox, oy)]
                    )
                if self.ring_implant_present:
                    # Exactly coincident with the tap ring (issue #1580,
                    # mirrors `well_island`'s own `well_tap_implant` ring
                    # precedent) -- never a blanket over the enclosed array.
                    _insert_ring(
                        self.cell,
                        self.layout.layer(self.ring_implant_layer),
                        dbu,
                        _shift_box(ring["outer_box_um"], ox, oy),
                        _shift_box(ring["inner_box_um"], ox, oy),
                        gap_box,
                    )

            # Medium-voltage/thick-oxide device-class marker (issue #1054):
            # sized to enclose every unit device (real and dummy) --
            # independent of `flavor`, so a caller can request both a well
            # (pfet) and a voltage-flavor marker (or either alone). Its own
            # box (`voltage_flavor_mark_box_um`) is *not* the same box the
            # well shape above encloses (issue #1577): gf180mcu's real
            # signoff deck needs the marker to reach further than the well's
            # own margin does -- see `_PDK_VOLTAGE_FLAVOR_MARK_MARGIN_UM`'s
            # own docstring.
            if self.voltage_flavor_mark_present:
                li_voltage_flavor_mark = self.layout.layer(
                    self.voltage_flavor_mark_layer
                )
                _insert_boxes(
                    self.cell,
                    li_voltage_flavor_mark,
                    dbu,
                    [info["voltage_flavor_mark_box_um"]],
                )

            # Dummy-device marker (issue #491): drawn only over
            # dummy_cells' gate footprint (unit_boxes["poly"], the exact
            # geometry `klt extract`'s nfet_gate/pfet_gate regions are built
            # from) -- never over a real cell -- so the deck's existing
            # dummy-suppression guards (#295/#462) drop these gates from the
            # extracted netlist instead of reporting them as unmatched
            # devices under `klt lvs`.
            if self.dummy_present and info["dummy_cells"]:
                li_dummy = self.layout.layer(self.dummy_layer)
                for c in info["dummy_cells"]:
                    _insert_boxes(
                        self.cell,
                        li_dummy,
                        dbu,
                        unit_boxes["poly"],
                        c["x0_um"],
                        c["y0_um"],
                    )

    return {"mos_array": _MosArrayPCell}
