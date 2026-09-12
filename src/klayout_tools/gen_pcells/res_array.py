"""``res_array`` reference generator PCell for ``klt gen`` (issue #1698).

Split out of ``gen.py`` as one of ten per-family ``_build_<family>_pcell()``
factories relocated into this package -- see ``gen_pcells/__init__.py`` for
the package-level rationale. The PCell class defined here delegates its
actual geometry to :func:`~klayout_tools.gen._res_array_layout`, which stays
in ``gen.py`` alongside the other per-family layout/geometry helpers (this
submodule only imports it, never the reverse).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import klayout.db as kdb


def _build_res_array_pcell() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build the ``res_array`` reference generator's PCell -- a resistor array.

    Defined inside a function (not at module scope) so importing
    ``klayout_tools.gen`` doesn't pay ``klayout.db``'s load cost until a
    caller actually runs a generator -- the same lazy-import discipline
    ``layers.py``/``render.py`` use for the same reason. See
    :func:`_build_pcell_classes`, the thin composer that merges every
    per-family factory's single-entry dict together.
    """
    import klayout.db as kdb

    from klayout_tools.gen import (
        _DEFAULT_RES_FLAVOR,
        _MAX_RES_FLAVOR_LAYERS,
        _insert_boxes,
        _res_array_layout,
    )

    class _ResArrayPCell(kdb.PCellDeclarationHelper):
        """Unit resistor/capacitor array (spike section 4's family 2): a row
        of ``num`` matched unit elements (see :func:`_res_unit_layout`) with
        ``dummy`` dummy elements at each end, per the
        ``kb/entries/sky130-bandgap-reference.json`` resistor-array idiom."""

        def __init__(self) -> None:
            super().__init__()
            self.param(
                "length_um",
                self.TypeDouble,
                "Unit resistor body length (um)",
                default=2.0,
            )
            self.param(
                "width_um", self.TypeDouble, "Unit resistor width (um)", default=0.42
            )
            self.param(
                "spacing_um",
                self.TypeDouble,
                "Spacing between unit resistors (um)",
                default=0.5,
            )
            self.param(
                "num", self.TypeInt, "Number of matched unit resistors", default=4
            )
            self.param(
                "dummy",
                self.TypeInt,
                "Dummy unit resistors added at each end of the row",
                default=1,
            )
            self.param(
                "rows",
                self.TypeInt,
                "Fold the num unit resistors into this many parallel rows "
                "(boustrophedon order) instead of one long row -- keeps a "
                "long resistor string's bounding box roughly square. Must "
                "be >= 1; default 1 reproduces the original single-row "
                "layout.",
                default=1,
            )
            self.param(
                "flavor",
                self.TypeString,
                "Poly-resistor flavour selecting the recognised device class "
                "by which implant/precision-resistor masks cover each body: "
                "'generic' (default, the base sheet-rho flavour -- "
                "res_generic_po on sky130, ppolyf_u on gf180mcu, rsil on "
                "sg13g2), or, on sky130, 'high' (res_high_po) / 'xhigh' "
                "(res_xhigh_po), or, on gf180mcu, '1k' / '2k' / '3k' "
                "(ppolyf_u_1k/_2k/_3k, selected at extraction time via "
                "'klt extract --deck-option poly_res='), or, on sg13g2, "
                "'rppd' (260 ohm/sq) / 'rhigh' (1360 ohm/sq) for the "
                "higher-sheet-rho flavours (issues #463/#1451/#1550)",
                default=_DEFAULT_RES_FLAVOR,
            )
            self.param(
                "metal_level",
                self.TypeInt,
                "Draw a metal-layer resistor body instead of poly: 0 "
                "(default) draws the original poly-body resistor selected "
                "by 'flavor'; 1..5 on sky130 draws that level's "
                "res_generic_mN device (met1..met5, its own resistor-ID "
                "marker, and an end via/landing-pad stack down to the metal "
                "level below) instead, ignoring 'flavor' entirely (issue "
                "#1639)",
                default=0,
            )
            self.param(
                "poly_layer",
                self.TypeLayer,
                "Resistor body drawing layer",
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
                "res_mark_layer",
                self.TypeLayer,
                "Resistor-ID (device-mark) drawing layer covering each unit's "
                "body segment (only used when res_mark_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "res_mark_present",
                self.TypeBoolean,
                "Whether res_mark_layer is a real, DRC-checked layer for the "
                "resolved PDK",
                default=False,
            )
            # One static slot per mask the *widest* flavour in
            # `_PDK_RES_FLAVOR_LAYERS` requires (issue #1451). A KLayout
            # PCell's parameter list is fixed at declaration time, so the
            # count comes from `_MAX_RES_FLAVOR_LAYERS` rather than the
            # per-request flavour; a narrower flavour simply leaves its tail
            # slots absent. Before #1451 these were two fixed
            # `res_implant`/`res_block` slots, which could not express
            # sg13g2's three-mask `rppd` / four-mask `rhigh` classes.
            for i in range(_MAX_RES_FLAVOR_LAYERS):
                self.param(
                    f"res_flavor_{i}_layer",
                    self.TypeLayer,
                    f"Flavour requires-mask #{i} (implant / precision-resistor "
                    "/ salicide-block layer, e.g. gf180mcu's Pplus or SAB) "
                    "drawn over the body segment for device recognition "
                    f"(only used when res_flavor_{i}_present)",
                    default=kdb.LayerInfo(0, 0),
                )
                self.param(
                    f"res_flavor_{i}_present",
                    self.TypeBoolean,
                    f"Whether res_flavor_{i}_layer is a real, DRC-checked "
                    "layer the resolved PDK's requested flavour requires",
                    default=False,
                )
            self.param(
                "dummy_layer",
                self.TypeLayer,
                "PDK dummy-device marker drawing layer, covering each dummy "
                "unit resistor's recognised body segment (only used when "
                "dummy_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "dummy_present",
                self.TypeBoolean,
                "Whether dummy_layer is a real layer this PDK's extraction "
                "deck declares (see ExtractionDeck.dummy)",
                default=False,
            )
            # `metal_level`'s own per-level geometry floors (issue #1639) --
            # harness-computed from the resolved PDK family/level exactly
            # like `cap_array`'s own `cap_top_via_min_w_um`/friends. `0.0`
            # (the poly-body default) leaves `_res_unit_layout`'s generic
            # CONTACT_SIZE_UM/ENCLOSURE_MARGIN_UM/MIN_SAME_LAYER_SPACING_UM
            # geometry unchanged -- see that function's own docstring.
            self.param(
                "metal_res_via_min_w_um",
                self.TypeDouble,
                "Minimum drawn side (um) for metal_level's end via on the "
                "resolved PDK family -- 0.0 leaves the generic "
                "CONTACT_SIZE_UM cut unchanged",
                default=0.0,
            )
            self.param(
                "metal_res_via_enclosure_min_um",
                self.TypeDouble,
                "Minimum enclosure (um) of metal_level's end via by the body "
                "and landing-pad layers on the resolved PDK family -- 0.0 "
                "leaves the generic ENCLOSURE_MARGIN_UM unchanged",
                default=0.0,
            )
            self.param(
                "metal_res_via_space_min_um",
                self.TypeDouble,
                "Minimum spacing (um) between metal_level's end vias on "
                "adjacent unit resistors on the resolved PDK family -- 0.0 "
                "draws exactly the requested spacing_um",
                default=0.0,
            )

        def display_text_impl(self) -> str:
            return (
                f"res_array(l={self.length_um},w={self.width_um},"
                f"n={self.num},rows={self.rows},flavor={self.flavor},"
                f"metal_level={self.metal_level})"
            )

        def produce_impl(self) -> None:
            dbu = self.layout.dbu
            li_poly = self.layout.layer(self.poly_layer)
            li_contact = self.layout.layer(self.contact_layer)
            li_metal = self.layout.layer(self.metal_layer)
            info = _res_array_layout(
                self.length_um,
                self.width_um,
                self.spacing_um,
                self.num,
                self.dummy,
                self.rows,
                self.metal_res_via_min_w_um,
                self.metal_res_via_enclosure_min_um,
                self.metal_res_via_space_min_um,
            )
            unit_boxes = info["unit"]["boxes_um"]
            all_cells = info["cells"] + info["dummy_cells"]
            for c in all_cells:
                for role, li in (
                    ("poly", li_poly),
                    ("contact", li_contact),
                    ("metal", li_metal),
                ):
                    _insert_boxes(
                        self.cell, li, dbu, unit_boxes[role], c["x0_um"], c["y0_um"]
                    )

            # Draw the PDK resistor-ID marker, plus every requires-mask the
            # requested flavour needs (none on sky130's `res_generic_po`, two
            # on gf180mcu's `ppolyf_u` and sg13g2's `rsil`, three on sg13g2's
            # `rppd`, four on its `rhigh` -- issue #1451), over every unit's
            # body segment -- dummies included, since they are structurally
            # real unit resistors too (mirrors `mos_array`'s dummy gates,
            # which are drawn identically to real ones) -- see
            # :func:`_res_unit_layout` for why the marker box excludes the
            # contacted end pads.
            marker_boxes = unit_boxes["marker"]
            for present, layer_param in [
                (self.res_mark_present, self.res_mark_layer),
            ] + [
                (
                    getattr(self, f"res_flavor_{i}_present"),
                    getattr(self, f"res_flavor_{i}_layer"),
                )
                for i in range(_MAX_RES_FLAVOR_LAYERS)
            ]:
                if not present:
                    continue
                li_mark = self.layout.layer(layer_param)
                for c in all_cells:
                    _insert_boxes(
                        self.cell, li_mark, dbu, marker_boxes, c["x0_um"], c["y0_um"]
                    )

            # Dummy-device marker (issue #491): drawn only over
            # dummy_cells' recognised body footprint (the same marker_boxes
            # span the resistor-ID marker above uses) -- never over a real
            # cell -- so `klt extract`'s existing dummy-suppression guards
            # (#295/#462) drop these dummy resistor bodies from the
            # extracted netlist instead of reporting them as unmatched
            # devices under `klt lvs`.
            if self.dummy_present and info["dummy_cells"]:
                li_dummy = self.layout.layer(self.dummy_layer)
                for c in info["dummy_cells"]:
                    _insert_boxes(
                        self.cell, li_dummy, dbu, marker_boxes, c["x0_um"], c["y0_um"]
                    )

    return {"res_array": _ResArrayPCell}
