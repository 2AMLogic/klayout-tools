"""``cap_array`` reference generator PCell for ``klt gen`` (issue #1698).

Split out of ``gen.py`` as one of ten per-family ``_build_<family>_pcell()``
factories relocated into this package -- see ``gen_pcells/__init__.py`` for
the package-level rationale. The PCell class defined here delegates its
actual geometry to :func:`~klayout_tools.gen._cap_array_layout`, which stays
in ``gen.py`` alongside the other per-family layout/geometry helpers (this
submodule only imports it, never the reverse).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import klayout.db as kdb


def _build_cap_array_pcell() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build the ``cap_array`` reference generator's PCell -- a capacitor array.

    Defined inside a function (not at module scope) so importing
    ``klayout_tools.gen`` doesn't pay ``klayout.db``'s load cost until a
    caller actually runs a generator -- the same lazy-import discipline
    ``layers.py``/``render.py`` use for the same reason. See
    :func:`_build_pcell_classes`, the thin composer that merges every
    per-family factory's single-entry dict together.
    """
    import klayout.db as kdb

    from klayout_tools.gen import (
        _MAX_CAP_TOP_PLATE_REQUIRES,
        _cap_array_layout,
        _insert_boxes,
    )

    class _CapArrayPCell(kdb.PCellDeclarationHelper):
        """Unit MiM capacitor array: a row of ``num`` matched unit cells
        (see :func:`_cap_unit_layout`), each a top-plate-metal-over-
        bottom-plate-metal MiM stack with a top-plate via + local-metal
        landing pad -- the capacitor sibling of ``res_array`` (issue
        #1117)."""

        def __init__(self) -> None:
            super().__init__()
            self.param(
                "plate_w_um",
                self.TypeDouble,
                "Unit top-plate width (um)",
                default=5.0,
            )
            self.param(
                "plate_h_um",
                self.TypeDouble,
                "Unit top-plate height (um)",
                default=5.0,
            )
            self.param(
                "spacing_um",
                self.TypeDouble,
                "Spacing between unit capacitors (um)",
                default=0.5,
            )
            self.param(
                "num", self.TypeInt, "Number of matched unit capacitors", default=4
            )
            self.param(
                "cap_top_plate_layer",
                self.TypeLayer,
                "MiM top-plate drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "cap_bottom_plate_layer",
                self.TypeLayer,
                "MiM bottom-plate drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "cap_top_via_layer",
                self.TypeLayer,
                "Top-plate via drawing layer (only used when cap_top_via_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "cap_top_via_present",
                self.TypeBoolean,
                "Whether cap_top_via_layer is a real, DRC-checked layer for "
                "the resolved PDK",
                default=False,
            )
            self.param(
                "cap_top_via_metal_layer",
                self.TypeLayer,
                "Top-plate via landing-metal drawing layer (only used when "
                "cap_top_via_metal_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "cap_top_via_metal_present",
                self.TypeBoolean,
                "Whether cap_top_via_metal_layer is a real, DRC-checked "
                "layer for the resolved PDK",
                default=False,
            )
            self.param(
                "cap_top_via_metal_min_w_um",
                self.TypeDouble,
                "Minimum drawn width (um) for the top-plate via landing pad "
                "on the resolved PDK family (issue #1455) -- 0.0 for a "
                "family with no override, leaving the generic landing-pad "
                "size unchanged",
                default=0.0,
            )
            self.param(
                "cap_top_via_min_w_um",
                self.TypeDouble,
                "Minimum drawn side (um) for the top-plate via itself on the "
                "resolved PDK family (issue #1555) -- 0.0 leaves the generic "
                "CONTACT_SIZE_UM cut unchanged",
                default=0.0,
            )
            self.param(
                "cap_bottom_plate_margin_min_um",
                self.TypeDouble,
                "Minimum distance (um) the bottom plate extends past the top "
                "plate on the resolved PDK family (issue #1555) -- 0.0 "
                "leaves the generic CAP_BOTTOM_PLATE_MARGIN_UM unchanged",
                default=0.0,
            )
            self.param(
                "cap_min_spacing_um",
                self.TypeDouble,
                "Minimum spacing (um) between adjacent unit capacitors on "
                "the resolved PDK family (issue #1555) -- 0.0 draws exactly "
                "the requested spacing_um",
                default=0.0,
            )
            # One static slot per mask the *widest* family in
            # `_PDK_CAP_TOP_PLATE_REQUIRES` needs (issue #1555), mirroring
            # `_ResArrayPCell`'s own `res_flavor_<i>_*` slots: a KLayout
            # PCell's parameter list is fixed at declaration time, so the
            # count comes from `_MAX_CAP_TOP_PLATE_REQUIRES` rather than the
            # resolved family, and a family needing fewer (or no) masks
            # simply leaves its tail slots absent.
            for i in range(_MAX_CAP_TOP_PLATE_REQUIRES):
                self.param(
                    f"cap_top_requires_{i}_layer",
                    self.TypeLayer,
                    f"Top-plate requires-mask #{i} (e.g. gf180mcu's CAP_MK "
                    "or MIM_L_MK) drawn over the top plate for device "
                    f"recognition (only used when cap_top_requires_{i}_present)",
                    default=kdb.LayerInfo(0, 0),
                )
                self.param(
                    f"cap_top_requires_{i}_present",
                    self.TypeBoolean,
                    f"Whether cap_top_requires_{i}_layer is a real layer the "
                    "resolved PDK family's own MiM device class requires over "
                    "the top plate",
                    default=False,
                )

        def display_text_impl(self) -> str:
            return f"cap_array(w={self.plate_w_um},h={self.plate_h_um},n={self.num})"

        def produce_impl(self) -> None:
            dbu = self.layout.dbu
            li_top = self.layout.layer(self.cap_top_plate_layer)
            li_bottom = self.layout.layer(self.cap_bottom_plate_layer)
            info = _cap_array_layout(
                self.plate_w_um,
                self.plate_h_um,
                self.spacing_um,
                self.num,
                self.cap_top_via_metal_min_w_um,
                self.cap_top_via_min_w_um,
                self.cap_bottom_plate_margin_min_um,
                self.cap_min_spacing_um,
            )
            unit_boxes = info["unit"]["boxes_um"]
            # Every extra mask this family's own MiM device class requires
            # over the top plate (issue #1555) -- drawn exactly coincident
            # with the top plate, since `extract.py` intersects them with it
            # (`_capacitor_plate_regions`) and any shortfall would narrow the
            # recognised plate. Empty on sky130/sg13g2, whose curated decks
            # set no `top_plate_requires`; `CAP_MK`/`MIM_L_MK` on gf180mcu.
            top_requires_layers = [
                getattr(self, f"cap_top_requires_{i}_layer")
                for i in range(_MAX_CAP_TOP_PLATE_REQUIRES)
                if getattr(self, f"cap_top_requires_{i}_present")
            ]
            for c in info["cells"]:
                _insert_boxes(
                    self.cell,
                    li_bottom,
                    dbu,
                    unit_boxes["bottom_plate"],
                    c["x0_um"],
                    c["y0_um"],
                )
                _insert_boxes(
                    self.cell,
                    li_top,
                    dbu,
                    unit_boxes["top_plate"],
                    c["x0_um"],
                    c["y0_um"],
                )
                for layer_param in top_requires_layers:
                    _insert_boxes(
                        self.cell,
                        self.layout.layer(layer_param),
                        dbu,
                        unit_boxes["top_plate"],
                        c["x0_um"],
                        c["y0_um"],
                    )
                if self.cap_top_via_present:
                    li_via = self.layout.layer(self.cap_top_via_layer)
                    _insert_boxes(
                        self.cell,
                        li_via,
                        dbu,
                        unit_boxes["top_via"],
                        c["x0_um"],
                        c["y0_um"],
                    )
                if self.cap_top_via_metal_present:
                    li_via_metal = self.layout.layer(self.cap_top_via_metal_layer)
                    _insert_boxes(
                        self.cell,
                        li_via_metal,
                        dbu,
                        unit_boxes["top_via_metal"],
                        c["x0_um"],
                        c["y0_um"],
                    )

    return {"cap_array": _CapArrayPCell}
