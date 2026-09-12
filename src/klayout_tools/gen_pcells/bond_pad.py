"""``bond_pad`` reference generator PCell for ``klt gen`` (issue #1698).

Split out of ``gen.py`` as one of ten per-family ``_build_<family>_pcell()``
factories relocated into this package -- see ``gen_pcells/__init__.py`` for
the package-level rationale. Unlike most of its siblings, this family draws
its own geometry directly (no separate ``_bond_pad_layout`` helper exists in
``gen.py``) -- it only needs ``gen.py``'s shared ``_insert_boxes`` primitive
and its own margin constants.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import klayout.db as kdb


def _build_bond_pad_pcell() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build the ``bond_pad`` reference generator's PCell -- a top-metal bond pad.

    Defined inside a function (not at module scope) so importing
    ``klayout_tools.gen`` doesn't pay ``klayout.db``'s load cost until a
    caller actually runs a generator -- the same lazy-import discipline
    ``layers.py``/``render.py`` use for the same reason. See
    :func:`_build_pcell_classes`, the thin composer that merges every
    per-family factory's single-entry dict together.
    """
    import klayout.db as kdb

    from klayout_tools.gen import (
        PAD_OPENING_GUIDELINE_MIN_UM,
        PAD_TOP_METAL_ENCLOSURE_MIN_UM,
        _insert_boxes,
    )

    class _BondPadPCell(kdb.PCellDeclarationHelper):
        """Chip-boundary bond pad (issue #568): a passivation opening (``pad``
        role) enclosed by the resolved PDK family's own topmost routing metal
        (``top_metal`` role) overlapping it by ``enclosure_um`` on every side
        -- gf180mcu's only hard, DRC-coded bond-pad rule (DRM 9.1 "PAD.4",
        ``decks/gf180mcu.py``'s ``pad.enclosing.metal5.1``). Unlike every
        other generator in this family, a bond pad's strap is *not* the
        shared ``metal`` role (li1/Metal1) `mos_array`/`res_array`/
        `guard_ring`/`diff_pair`/`bjt_array` all draw on -- see
        :data:`_PDK_ROLE_LAYERS`'s ``"top_metal"`` entries.

        ``down_to`` currently supports only its default, ``"top_metal"`` --
        this generator does not yet draw a via stack connecting the pad down
        to a lower metal level (a real via4/via3/.../via1 chain neither
        curated deck models this far up the stack; see the ``"top_metal"``
        role's own docstring). ``via_style`` is validated but has no
        geometric effect until ``down_to`` supports a lower level -- both are
        reported back via ``drc_hints.notes`` (see
        :func:`_bond_pad_describe`), never silently ignored.
        """

        def __init__(self) -> None:
            super().__init__()
            self.param(
                "opening_um",
                self.TypeDouble,
                "Pad opening (passivation window) side length (um)",
                default=PAD_OPENING_GUIDELINE_MIN_UM["wedge"],
            )
            self.param(
                "bond_type",
                self.TypeString,
                "Bond process: 'wedge' (default, wire bond without CUP), "
                "'ball_cup' (wire bond with CUP) or 'bump' (gold bump) -- "
                "selects the PAD.1 guideline minimum opening size",
                default="wedge",
            )
            self.param(
                "enclosure_um",
                self.TypeDouble,
                "Top-metal overlap of the pad opening on every side (um)",
                default=PAD_TOP_METAL_ENCLOSURE_MIN_UM,
            )
            self.param(
                "down_to",
                self.TypeString,
                "Lowest metal level the pad straps to -- only 'top_metal' "
                "(default) is supported today",
                default="top_metal",
            )
            self.param(
                "via_style",
                self.TypeString,
                "Via arrangement once down_to supports a level below "
                "top_metal: 'ring' (default, peripheral) or 'array' -- "
                "currently has no geometric effect (see drc_hints.notes)",
                default="ring",
            )
            self.param(
                "pad_layer",
                self.TypeLayer,
                "Pad opening (passivation) drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "top_metal_layer",
                self.TypeLayer,
                "Top-metal strap drawing layer",
                default=kdb.LayerInfo(0, 0),
            )

        def display_text_impl(self) -> str:
            return f"bond_pad({self.opening_um}um,{self.bond_type})"

        def produce_impl(self) -> None:
            dbu = self.layout.dbu
            li_pad = self.layout.layer(self.pad_layer)
            li_top_metal = self.layout.layer(self.top_metal_layer)
            half_open = self.opening_um / 2.0
            half_strap = half_open + self.enclosure_um
            _insert_boxes(
                self.cell,
                li_pad,
                dbu,
                [(-half_open, -half_open, half_open, half_open)],
            )
            _insert_boxes(
                self.cell,
                li_top_metal,
                dbu,
                [(-half_strap, -half_strap, half_strap, half_strap)],
            )

    return {"bond_pad": _BondPadPCell}
