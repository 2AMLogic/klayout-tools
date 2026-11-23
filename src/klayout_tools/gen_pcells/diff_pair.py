"""``diff_pair`` reference generator PCell for ``klt gen`` (issue #1698).

Split out of ``gen.py`` as one of ten per-family ``_build_<family>_pcell()``
factories relocated into this package -- see ``gen_pcells/__init__.py`` for
the package-level rationale. The PCell class defined here delegates its
actual geometry to :func:`~klayout_tools.gen._diff_pair_layout`, which stays
in ``gen.py`` alongside the other per-family layout/geometry helpers (this
submodule only imports it, never the reverse).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import klayout.db as kdb

from ._common_params import (
    declare_cut_and_metal_params,
    declare_mos_array_extra_params,
    declare_mos_stack_params,
    declare_ring_implant_params,
    declare_ring_well_margin,
    declare_voltage_flavor_mark_params,
    declare_well_present,
    union_box,
)


def _ring_well_mark_box(
    pcell: Any, info: dict[str, Any]
) -> tuple[float, float, float, float] | None:
    """The guard ring's own well tie box, or ``None`` when no ring well is
    drawn (issue #6458)."""
    from klayout_tools.gen import _shift_box

    if (
        info["ring"] is None
        or not pcell.add_guard_ring
        or not pcell.well_present
        or pcell.flavor != "pfet"
    ):
        return None
    margin = pcell.ring_well_margin_um
    return _shift_box(
        (
            -margin,
            -margin,
            info["ring"]["outer_w_um"] + margin,
            info["ring"]["outer_h_um"] + margin,
        ),
        *info["ring_offset_um"],
    )


def _diff_pair_pcell_layout(pcell: Any) -> dict[str, Any]:
    """Build the same resolved pair and ring geometry for validation and drawing."""
    from klayout_tools.gen import _diff_pair_layout, _pcell_tap_reach_um

    return _diff_pair_layout(
        pcell.w_um,
        pcell.l_um,
        pcell.splits,
        pcell.add_guard_ring,
        pcell.ring_gap_side,
        pcell.ring_gap_um,
        pcell.ring_gap_offset_um,
        pcell.ring_padding_um,
        pcell.row_spacing_um,
        pcell.gate_contact,
        pcell.gate_pad_clearance_um,
        pcell.contact_gate_offset_um,
        pcell.bottom_endcap_um,
        pcell.sd_implant_margin_um if pcell.sd_implant_present else 0.0,
        pitch_x_um=pcell.pitch_x_um,
        top_endcap_um=pcell.top_endcap_um,
        tap_reach_um=_pcell_tap_reach_um(pcell),
        strap_splits=pcell.strap_splits,
        strap_grid_um=pcell.mfg_grid_um,
    )


def _draw_pcell_strap(
    pcell: Any,
    strap: dict[str, Any] | None,
    li_strap_metal: Any,
    li_strap_via: Any,
) -> None:
    """Draw ``_diff_pair_layout``'s strap result (issue #5557): met1 rails
    and trunks on ``li_strap_metal``, fixed-size mcons on ``li_strap_via``.
    ``strap`` is ``None`` unless ``strap_splits`` was requested, in which
    case this draws nothing -- the same always-call/empty-input shape
    ``_draw_pcell_tap_pads`` uses. Box coordinates arrive pre-snapped to the
    family grid from the layout math, so ``_insert_boxes``' per-edge dbu
    rounding is exact.
    """
    if strap is None:
        return
    from klayout_tools.gen import _insert_boxes

    dbu = pcell.layout.dbu
    _insert_boxes(pcell.cell, li_strap_metal, dbu, strap["metal_boxes_um"])
    _insert_boxes(pcell.cell, li_strap_via, dbu, strap["via_boxes_um"])


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
        _clamp_cut_boxes,
        _close_implant_gaps,
        _draw_pcell_npc,
        _draw_pcell_tap_pads,
        _insert_boxes,
    )
    from klayout_tools.gen_voltage_flavor import _draw_voltage_flavor_marks

    from ._ring_shapes import _draw_composed_guard_ring

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

        # Declared via ``self.param("mfg_grid_um", ...)`` below; the
        # PCellDeclarationHelper stubs cannot see it, so annotate for mypy.
        mfg_grid_um: float

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
            declare_mos_array_extra_params(self, kdb)
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
                "pitch_x_um",
                self.TypeDouble,
                "Inter-unit column pitch override (um), clamped up to the "
                "legal minimum (unit extent + min same-layer spacing) "
                "(issue #5764). 0.0 (default) = no override",
                default=0.0,
            )
            self.param(
                "mirror",
                self.TypeBoolean,
                "Label devices M1/M2 (current mirror) instead of Q1/Q2 "
                "(differential pair)",
                default=False,
            )
            self.param(
                "strap_splits",
                self.TypeBoolean,
                "Strap each device's splits together in-cell on the second "
                "routing plane (met1 via mcon on sky130) and report one "
                "S/D/G port triple per device on the strap rails instead of "
                "one triple per sub-instance. Requires gate_contact and a "
                "device width the S/D strap tracks fit; verified on sky130 "
                "only -- every other family is rejected (issue #5557)",
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
                "Optional medium-voltage/thick-oxide device-class marker: '' "
                "(default, no marker drawn) or a name the resolved PDK family's "
                "role-layer table recognises (e.g. 'medium_voltage' on gf180mcu, "
                "drawing its Dualgate marker; 'lvt' on sky130, drawing lvtn.drawing "
                "for the low-Vt device). A name the family doesn't recognise draws "
                "nothing and is reported via drc_hints.notes, never silently dropped",
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
            declare_mos_stack_params(self, kdb)
            declare_cut_and_metal_params(self, kdb)
            self.param(
                "tap_layer",
                self.TypeLayer,
                "Guard ring tap drawing layer",
                default=kdb.LayerInfo(0, 0),
                hidden=True,
            )
            self.param(
                "strap_metal_layer",
                self.TypeLayer,
                "Second routing metal (metal2 role) the split straps draw on "
                "(hidden harness-resolved param, issue #5557)",
                default=kdb.LayerInfo(0, 0),
                hidden=True,
            )
            self.param(
                "strap_via_layer",
                self.TypeLayer,
                "Via joining the local-metal pads to the strap metal (via1 "
                "role; hidden harness-resolved param, issue #5557)",
                default=kdb.LayerInfo(0, 0),
                hidden=True,
            )
            self.param(
                "well_layer",
                self.TypeLayer,
                "Guard ring well drawing layer (only used when well_present)",
                default=kdb.LayerInfo(0, 0),
                hidden=True,
            )
            declare_well_present(self)
            declare_ring_implant_params(self, kdb)
            self.param(
                "voltage_flavor_mark_layer",
                self.TypeLayer,
                "Medium-voltage/thick-oxide device-class marker drawing "
                "layer, sized to enclose the device pair's own footprint "
                "(only used when voltage_flavor_mark_present)",
                default=kdb.LayerInfo(0, 0),
                hidden=True,
            )
            declare_voltage_flavor_mark_params(self, kdb)
            self.param(
                "voltage_flavor_mark_margin_um",
                self.TypeDouble,
                "Harness-resolved margin the voltage_flavor marker box grows "
                "beyond the device pair's own shared active footprint (see "
                "_PDK_VOLTAGE_FLAVOR_MARK_MARGIN_UM)",
                default=WELL_ENCLOSURE_MARGIN_UM,
                hidden=True,
            )
            declare_ring_well_margin(self, WELL_ENCLOSURE_MARGIN_UM)

        def display_text_impl(self) -> str:
            return f"diff_pair(w={self.w_um},l={self.l_um},splits={self.splits})"

        def produce_impl(self) -> None:
            dbu = self.layout.dbu
            li_active = self.layout.layer(self.active_layer)
            li_poly = self.layout.layer(self.poly_layer)
            li_contact = self.layout.layer(self.contact_layer)
            li_metal = self.layout.layer(self.metal_layer)
            li_strap_metal = self.layout.layer(self.strap_metal_layer)
            li_strap_via = self.layout.layer(self.strap_via_layer)
            info = _diff_pair_pcell_layout(self)
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

            # Split straps (issue #5557): met1 rails + trunks and the
            # mcons landing them on each unit's S/D/gate pads -- no-op when
            # strap_splits is off (`info["strap"]` is None).
            _draw_pcell_strap(self, info["strap"], li_strap_metal, li_strap_via)

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
                _close_implant_gaps(
                    self.cell,
                    li_sd_implant,
                    dbu,
                    getattr(self, "sd_implant_min_space_um"),  # noqa: B009
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

            # The ring's own well tie box (flavor == "pfet" only), computed
            # up front so the marker below can be grown over it (issue
            # #6458, the `diff_pair` sibling of #6025) and the well shape
            # drawn further down is the very same box -- the two cannot
            # drift apart.
            ring_well_mark_box = _ring_well_mark_box(self, info)

            # Medium-voltage/thick-oxide device-class marker (issue #1054):
            # its *own* box (issue #1577), not the well shape's -- gf180mcu's
            # real signoff deck needs the marker to reach further than the
            # well's own margin does, see
            # `_PDK_VOLTAGE_FLAVOR_MARK_MARGIN_UM`'s own docstring. Issue
            # #6458: also grown over the guard ring's well tie when one is
            # drawn, so an HV-nwell enclosure rule (sky130 nwell.9) holds;
            # one unioned box keeps it a single polygon per marker layer.
            if self.voltage_flavor_mark_present:
                mark_margin = self.voltage_flavor_mark_margin_um
                mark_box = (
                    -mark_margin,
                    -mark_margin,
                    info["core_w_um"] + mark_margin,
                    info["core_h_um"] + mark_margin,
                )
                _draw_voltage_flavor_marks(
                    self, dbu, union_box(mark_box, ring_well_mark_box)
                )

            if info["ring"] is not None and self.add_guard_ring:
                if ring_well_mark_box is not None:
                    li_well = self.layout.layer(self.well_layer)
                    _insert_boxes(self.cell, li_well, dbu, [ring_well_mark_box])
                _draw_composed_guard_ring(
                    self,
                    self.layout.layer(self.tap_layer),
                    li_metal,
                    li_contact,
                    dbu,
                    info,
                )

            # Tap pads (issue #3648): see `_MosArrayPCell.produce_impl`.
            _draw_pcell_tap_pads(self, info["tap_pads"], li_metal, li_contact)

            # Issue #2585: clamp every cut on a fixed-size cut/via layer down
            # to that size, inside the unchanged generic-budget layout.
            _clamp_cut_boxes(
                self.cell,
                li_contact,
                dbu,
                self.contact_fixed_size_um,
                self.mfg_grid_um,
            )

            _draw_pcell_npc(self, li_contact, li_poly, dbu)

    return {"diff_pair": _DiffPairPCell}
