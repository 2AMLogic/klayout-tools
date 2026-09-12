"""``well_island`` reference generator PCell for ``klt gen`` (issue #1698).

Split out of ``gen.py`` as one of ten per-family ``_build_<family>_pcell()``
factories relocated into this package -- see ``gen_pcells/__init__.py`` for
the package-level rationale. The PCell class defined here delegates its
actual geometry to :func:`~klayout_tools.gen._ring_layout` (composing the
``guard_ring`` family's own ring-drawing helper) and
:func:`~klayout_tools.gen._well_island_label_point`, both of which stay in
``gen.py`` alongside the other per-family layout/geometry helpers (this
submodule only imports them, never the reverse).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import klayout.db as kdb


def _build_well_island_pcell() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build the ``well_island`` reference generator's PCell -- an isolated well
    tap island.

    Defined inside a function (not at module scope) so importing
    ``klayout_tools.gen`` doesn't pay ``klayout.db``'s load cost until a
    caller actually runs a generator -- the same lazy-import discipline
    ``layers.py``/``render.py`` use for the same reason. See
    :func:`_build_pcell_classes`, the thin composer that merges every
    per-family factory's single-entry dict together.
    """
    import klayout.db as kdb

    from klayout_tools.gen import (
        WELL_ENCLOSURE_MARGIN_UM,
        _insert_boxes,
        _insert_ring,
        _ring_layout,
        _well_box_um,
        _well_island_label_point,
    )

    class _WellIslandPCell(kdb.PCellDeclarationHelper):
        """Named-net, isolated well/tap island (issue #1421).

        ``guard_ring``'s geometry -- the same tap ring + local-metal ring +
        evenly-spaced contacts, enclosed by a well tie -- with the two
        properties that idiom needs and a plain guard ring cannot express:

        1. **The tie carries a caller-named net.** ``params.net`` is drawn as
           a ``kdb.Text`` on the resolved family's *metal* label layer
           (``metal_label`` role) sitting on the ring's own metal band, so
           the name reaches the enclosed well only through the physical tie
           (metal -> contact -> tap -> well). Nothing is ever drawn on the
           deck's ``well_label`` layer, which would name the well polygon
           directly and make ``klt extract``'s ``devices[].nets["b"]`` report
           the intended body net even with the tie broken (the "well-label
           tautology" the issue calls out).
        2. **The island is isolated from a caller-named set of other wells.**
           ``params.isolate_from`` lists the other well regions, in this
           cell's own frame; ``params.separation_um`` optionally raises the
           required clearance above the resolved family's own
           different-potential well rule (:data:`_PDK_WELL_ISOLATION_UM`).
           The check itself lives in :func:`_well_island_isolation`, which
           runs *before* any geometry is produced -- an island that cannot
           clear its neighbours raises :class:`GenError` rather than
           emitting a well that silently merges with one of them.

        ``well_margin_resolved_um`` is the harness-computed well enclosure
        this cell actually draws: ``params.well_margin_um`` when it clears
        every neighbour, otherwise the largest value below it that does (down
        to :data:`WELL_ENCLOSURE_MARGIN_UM`, never below). See
        :func:`_well_island_isolation`.
        """

        def __init__(self) -> None:
            super().__init__()
            self.param(
                "inner_width_um",
                self.TypeDouble,
                "Width of the enclosed inner area (um)",
                default=3.0,
            )
            self.param(
                "inner_height_um",
                self.TypeDouble,
                "Height of the enclosed inner area (um)",
                default=3.0,
            )
            self.param(
                "ring_width_um",
                self.TypeDouble,
                "Tap ring thickness (um)",
                default=0.42,
            )
            self.param(
                "contacts_per_side",
                self.TypeInt,
                "Tap contacts evenly spaced along each ring side -- applied "
                "uniformly to all four sides unless overridden per-axis by "
                "contacts_per_side_ns/contacts_per_side_ew",
                default=4,
            )
            self.param(
                "contacts_per_side_ns",
                self.TypeInt,
                "Tap contacts on the N/S (top/bottom) sides, spaced along "
                "inner_width_um -- 0 (default) inherits contacts_per_side",
                default=0,
            )
            self.param(
                "contacts_per_side_ew",
                self.TypeInt,
                "Tap contacts on the E/W (left/right) sides, spaced along "
                "inner_height_um -- 0 (default) inherits contacts_per_side",
                default=0,
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
                "Length of the ring opening along its side (um), when "
                "ring_gap_side is set",
                default=0.0,
            )
            self.param(
                "ring_gap_offset_um",
                self.TypeDouble,
                "Shift of the ring opening from its side's midpoint (um): "
                "+x on 'N'/'S', +y on 'E'/'W'",
                default=0.0,
            )
            self.param(
                "net",
                self.TypeString,
                "Net name the island's tie carries -- drawn as a text on the "
                "ring's own metal (never on the well label layer) and "
                "reported as ports[].net. '' (default) draws no label",
                default="",
            )
            self.param(
                "well_margin_um",
                self.TypeDouble,
                "Well enclosure of the tap ring (um) -- trimmed towards "
                f"{WELL_ENCLOSURE_MARGIN_UM} when a larger value would "
                "violate the requested separation from isolate_from",
                default=WELL_ENCLOSURE_MARGIN_UM,
            )
            self.param(
                "separation_um",
                self.TypeDouble,
                "Required clearance (um, euclidian) from every isolate_from "
                "well at a different potential -- 0 (default) uses the "
                "resolved PDK family's own different-potential well rule; a "
                "value below that rule is rejected",
                default=0.0,
            )
            self.param(
                "isolate_from",
                self.TypeList,
                "Other well regions this island must stay clear of, as "
                "[x0_um, y0_um, x1_um, y1_um] or "
                "[x0_um, y0_um, x1_um, y1_um, net] entries in this cell's "
                "own coordinate frame -- an entry naming this island's own "
                "net is equipotential and is not isolated from",
                default=[],
            )
            self.param(
                "tap_layer",
                self.TypeLayer,
                "Substrate/well tap drawing layer",
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
                "metal_label_layer",
                self.TypeLayer,
                "Label layer the net name is drawn on (only used when "
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
                "well_layer",
                self.TypeLayer,
                "Well drawing layer (only used when well_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "well_present",
                self.TypeBoolean,
                "Whether well_layer is a real layer for the resolved PDK",
                default=False,
            )
            self.param(
                "well_tap_implant_layer",
                self.TypeLayer,
                "Well-tie implant layer drawn over the tap ring (only used "
                "when well_tap_implant_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "well_tap_implant_present",
                self.TypeBoolean,
                "Whether the resolved PDK needs an implant mask to recognise "
                "the tap ring as a well tie",
                default=False,
            )
            self.param(
                "well_margin_resolved_um",
                self.TypeDouble,
                "Harness-resolved well enclosure actually drawn (see "
                "_well_island_isolation)",
                default=WELL_ENCLOSURE_MARGIN_UM,
            )

        def display_text_impl(self) -> str:
            return f"well_island({self.net or 'unnamed'})"

        def produce_impl(self) -> None:
            dbu = self.layout.dbu
            li_tap = self.layout.layer(self.tap_layer)
            li_contact = self.layout.layer(self.contact_layer)
            li_metal = self.layout.layer(self.metal_layer)
            contacts_ns = self.contacts_per_side_ns or self.contacts_per_side
            contacts_ew = self.contacts_per_side_ew or self.contacts_per_side
            info = _ring_layout(
                self.inner_width_um,
                self.inner_height_um,
                self.ring_width_um,
                (contacts_ns, contacts_ew),
                self.ring_gap_side,
                self.ring_gap_um,
                self.ring_gap_offset_um,
            )
            gap_box = info["gap"]["box_um"] if info["gap"] is not None else None
            _insert_ring(
                self.cell,
                li_tap,
                dbu,
                info["outer_box_um"],
                info["inner_box_um"],
                gap_box,
            )
            _insert_ring(
                self.cell,
                li_metal,
                dbu,
                info["outer_box_um"],
                info["inner_box_um"],
                gap_box,
            )
            _insert_boxes(self.cell, li_contact, dbu, info["contact_boxes_um"])
            if self.well_tap_implant_present:
                # Exactly coincident with the tap ring -- never a blanket over
                # the enclosed area, which would re-dope whatever a caller
                # later places inside the island (see the `well_tap_implant`
                # role's own comment in `_PDK_ROLE_LAYERS`).
                _insert_ring(
                    self.cell,
                    self.layout.layer(self.well_tap_implant_layer),
                    dbu,
                    info["outer_box_um"],
                    info["inner_box_um"],
                    gap_box,
                )
            if self.well_present:
                _insert_boxes(
                    self.cell,
                    self.layout.layer(self.well_layer),
                    dbu,
                    [_well_box_um(info, self.well_margin_resolved_um)],
                )
            if self.net and self.metal_label_present:
                anchor = _well_island_label_point(info)
                if anchor is not None:
                    self.cell.shapes(self.layout.layer(self.metal_label_layer)).insert(
                        kdb.Text(
                            self.net,
                            kdb.Trans(
                                kdb.Vector(
                                    int(round(anchor[0] / dbu)),
                                    int(round(anchor[1] / dbu)),
                                )
                            ),
                        )
                    )

    return {"well_island": _WellIslandPCell}
