"""``guard_ring`` reference generator PCell for ``klt gen`` (issue #1698).

Split out of ``gen.py`` as one of ten per-family ``_build_<family>_pcell()``
factories relocated into this package -- see ``gen_pcells/__init__.py`` for
the package-level rationale. The PCell class defined here delegates its
actual geometry to :func:`~klayout_tools.gen._ring_layout`, which stays in
``gen.py`` alongside the other per-family layout/geometry helpers (this
submodule only imports it, never the reverse).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import klayout.db as kdb


def _build_guard_ring_pcell() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build the ``guard_ring`` reference generator's PCell -- a substrate/well
    tap ring.

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
    )

    class _GuardRingPCell(kdb.PCellDeclarationHelper):
        """Substrate/well tap guard ring (spike section 4's family 3): a
        tap ring + local-metal ring with evenly-spaced contacts (see
        :func:`_ring_layout`), optionally enclosed by a well tie on PDK
        families whose curated deck checks one (see ``well_present``)."""

        def __init__(self) -> None:
            super().__init__()
            self.param(
                "inner_width_um",
                self.TypeDouble,
                "Width of the protected inner area (um)",
                default=3.0,
            )
            self.param(
                "inner_height_um",
                self.TypeDouble,
                "Height of the protected inner area (um)",
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
                "inner_width_um -- 0 (default) inherits contacts_per_side, "
                "so existing single-scalar callers are unaffected",
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
                "Cut one routing opening through the ring on this side: "
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
                "add_well",
                self.TypeBoolean,
                "Enclose the ring in a well tie when the resolved PDK checks one",
                default=True,
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
                "well_layer",
                self.TypeLayer,
                "Well drawing layer (only used when well_present)",
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
                "Tap-ring implant drawing layer, exactly coincident with the "
                "tap ring (only used when ring_implant_present)",
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
            return f"guard_ring({self.inner_width_um}x{self.inner_height_um})"

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
            if self.add_well and self.well_present:
                li_well = self.layout.layer(self.well_layer)
                well_box = _well_box_um(info, WELL_ENCLOSURE_MARGIN_UM)
                _insert_boxes(self.cell, li_well, dbu, [well_box])
            if self.ring_implant_present:
                # Exactly coincident with the tap ring -- never a blanket
                # over the enclosed area, which would re-dope whatever a
                # caller later places inside the ring (issue #1580, mirrors
                # `well_island`'s own `well_tap_implant` ring precedent).
                _insert_ring(
                    self.cell,
                    self.layout.layer(self.ring_implant_layer),
                    dbu,
                    info["outer_box_um"],
                    info["inner_box_um"],
                    gap_box,
                )

    return {"guard_ring": _GuardRingPCell}
