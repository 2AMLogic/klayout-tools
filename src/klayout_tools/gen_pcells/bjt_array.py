"""``bjt_array`` reference generator PCell for ``klt gen`` (issue #1698).

Split out of ``gen.py`` as one of ten per-family ``_build_<family>_pcell()``
factories relocated into this package -- see ``gen_pcells/__init__.py`` for
the package-level rationale. The PCell class defined here delegates its
actual geometry to :func:`~klayout_tools.gen._bjt_array_layout`, which stays
in ``gen.py`` alongside the other per-family layout/geometry helpers (this
submodule only imports it, never the reverse).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import klayout.db as kdb


def _build_bjt_array_pcell() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build the ``bjt_array`` reference generator's PCell -- a matched BJT
    array.

    Defined inside a function (not at module scope) so importing
    ``klayout_tools.gen`` doesn't pay ``klayout.db``'s load cost until a
    caller actually runs a generator -- the same lazy-import discipline
    ``layers.py``/``render.py`` use for the same reason. See
    :func:`_build_pcell_classes`, the thin composer that merges every
    per-family factory's single-entry dict together.
    """
    import klayout.db as kdb

    from klayout_tools.gen import (
        _bjt_array_layout,
        _insert_boxes,
        _insert_ring,
        _shift_box,
    )

    class _BjtArrayPCell(kdb.PCellDeclarationHelper):
        """Matched vertical-bipolar (PNP/BJT) array (Epic #152 phase 4): a
        ``rows`` x ``cols`` common-centroid grid of identical unit devices
        (see :func:`_bjt_unit_layout`), sharing one base well, surrounded by
        a collector guard ring, with each unit individually covered by a
        device-mark layer (and a base-tie ``tap`` shape) on every PDK family
        whose extraction deck declares a bipolar marker -- gf180mcu's
        ``DRC_BJT`` (also DRC-checked) and sky130's ``pnp.drawing``
        (extraction-only, issue #432).

        Draws from base layers rather than instantiating a vendor library
        cell (e.g. gf180mcu ``pnp_05p00x05p00``) -- the design note
        ``docs/design/gen-bjt-array-spike.md`` records why: this repo's
        ``find_pdk`` has no library-GDS-instantiation path, CI never has a
        real PDK installed, and CLAUDE.md forbids vendoring PDK cell data.
        The trade-off (a DRC-clean matching floorplan, not a SPICE-model-
        exact device) is recorded there too.
        """

        def __init__(self) -> None:
            super().__init__()
            self.param(
                "emitter_um",
                self.TypeDouble,
                "Emitter diffusion side length (um)",
                default=0.6,
            )
            self.param("rows", self.TypeInt, "Array rows", default=3)
            self.param("cols", self.TypeInt, "Array columns", default=3)
            self.param(
                "topology",
                self.TypeString,
                "Placement topology: 'array' (row-major) or "
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
                "ratio",
                self.TypeInt,
                "Intended emitter matching ratio (e.g. 8 for a bandgap's "
                "8:1 group) -- documented in the matched-group id and hints",
                default=8,
            )
            self.param(
                "add_collector_ring",
                self.TypeBoolean,
                "Surround the array with a collector/substrate guard ring",
                default=True,
            )
            self.param(
                "ring_gap_side",
                self.TypeString,
                "Cut one routing opening through the collector ring on this "
                "side: '' (default, closed ring), 'N', 'S', 'E' or 'W'",
                default="",
            )
            self.param(
                "ring_gap_um",
                self.TypeDouble,
                "Length of the collector-ring opening along its side (um), when "
                "ring_gap_side is set",
                default=0.0,
            )
            self.param(
                "ring_gap_offset_um",
                self.TypeDouble,
                "Shift of the collector-ring opening from its side's midpoint "
                "(um): +x on 'N'/'S', +y on 'E'/'W'",
                default=0.0,
            )
            self.param(
                "active_layer",
                self.TypeLayer,
                "Emitter/base/collector diffusion (COMP) drawing layer",
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
                "Base-tie tap drawing layer, covering each unit's base-tie "
                "contact so the base terminal resolves to a real net",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "well_layer",
                self.TypeLayer,
                "Shared base well drawing layer (only used when well_present)",
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
                "Collector-ring implant drawing layer, exactly coincident "
                "with the collector ring (only used when add_collector_ring "
                "and ring_implant_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "ring_implant_present",
                self.TypeBoolean,
                "Whether the resolved PDK needs an implant mask to recognise "
                "the collector ring's own shape (see _ring_tap_implant_layer)",
                default=False,
            )
            self.param(
                "bjt_mark_layer",
                self.TypeLayer,
                "Per-unit bipolar device-mark drawing layer, enclosing only "
                "the emitter pad (only used when bjt_mark_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "bjt_mark_present",
                self.TypeBoolean,
                "Whether bjt_mark_layer is a real layer this PDK's extraction "
                "deck (and, on some families, its DRC deck too) keys off",
                default=False,
            )
            self.param(
                "dummy_layer",
                self.TypeLayer,
                "PDK dummy-device marker drawing layer, covering each dummy "
                "unit device's recognised base-mark footprint (only used "
                "when dummy_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "dummy_present",
                self.TypeBoolean,
                "Whether dummy_layer is a real layer this PDK's extraction "
                "deck declares (see ExtractionDeck.dummy)",
                default=False,
            )

        def display_text_impl(self) -> str:
            return f"bjt_array({self.rows}x{self.cols},e={self.emitter_um})"

        def produce_impl(self) -> None:
            dbu = self.layout.dbu
            li_active = self.layout.layer(self.active_layer)
            li_contact = self.layout.layer(self.contact_layer)
            li_metal = self.layout.layer(self.metal_layer)
            li_tap = self.layout.layer(self.tap_layer)
            info = _bjt_array_layout(
                self.emitter_um,
                self.rows,
                self.cols,
                self.dummy,
                self.topology,
                self.add_collector_ring,
                self.ring_gap_side,
                self.ring_gap_um,
                self.ring_gap_offset_um,
            )
            unit_boxes = info["unit"]["boxes_um"]
            all_cells = info["cells"] + info["dummy_cells"]
            for c in all_cells:
                for role, li in (
                    ("active", li_active),
                    ("contact", li_contact),
                    ("metal", li_metal),
                    ("tap", li_tap),
                ):
                    _insert_boxes(
                        self.cell, li, dbu, unit_boxes[role], c["x0_um"], c["y0_um"]
                    )

            if self.well_present:
                li_well = self.layout.layer(self.well_layer)
                _insert_boxes(self.cell, li_well, dbu, [info["well_box_um"]])

            if info["ring"] is not None and self.add_collector_ring:
                ring = info["ring"]
                ox, oy = info["ring_offset_um"]
                gap_box = (
                    _shift_box(ring["gap"]["box_um"], ox, oy)
                    if ring["gap"] is not None
                    else None
                )
                _insert_ring(
                    self.cell,
                    li_active,
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
                if self.ring_implant_present:
                    # Exactly coincident with the collector ring's own
                    # active-layer shape (issue #1580, mirrors
                    # `well_island`'s own `well_tap_implant` ring precedent)
                    # -- never a blanket over the enclosed base well/array.
                    _insert_ring(
                        self.cell,
                        self.layout.layer(self.ring_implant_layer),
                        dbu,
                        _shift_box(ring["outer_box_um"], ox, oy),
                        _shift_box(ring["inner_box_um"], ox, oy),
                        gap_box,
                    )

            # Per-unit bipolar device-mark, drawn on every unit (dummies
            # included -- they are structurally real unit devices too,
            # mirroring `res_array`'s marker precedent). Deliberately *not*
            # an array-wide box (issue #432): see `_bjt_unit_layout`'s
            # `boxes_um["marker"]` docstring for why that would misrecognise
            # every base-tie pad as a second emitter.
            if self.bjt_mark_present:
                li_mark = self.layout.layer(self.bjt_mark_layer)
                marker_boxes = unit_boxes["marker"]
                for c in all_cells:
                    _insert_boxes(
                        self.cell, li_mark, dbu, marker_boxes, c["x0_um"], c["y0_um"]
                    )

                # Dummy-device marker (issue #491): drawn only over
                # dummy_cells' bipolar device-mark footprint (the same
                # marker_boxes span the bjt_mark drawing above uses) --
                # never over a real cell -- so `klt extract`'s existing
                # dummy-suppression guards (#295/#462) drop these dummy
                # bipolar units from the extracted netlist instead of
                # reporting them as unmatched devices under `klt lvs`.
                if self.dummy_present and info["dummy_cells"]:
                    li_dummy = self.layout.layer(self.dummy_layer)
                    for c in info["dummy_cells"]:
                        _insert_boxes(
                            self.cell,
                            li_dummy,
                            dbu,
                            marker_boxes,
                            c["x0_um"],
                            c["y0_um"],
                        )

    return {"bjt_array": _BjtArrayPCell}
