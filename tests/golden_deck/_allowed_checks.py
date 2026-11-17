"""Per-deck `DrcRule.check` kinds the golden-deck manifest covers (data only).

Holds `ALLOWED_CHECKS`, split out of :mod:`generate_golden_deck` for the
per-file size ratchet; the generator re-exports it, so
``from golden_deck.generate_golden_deck import ALLOWED_CHECKS`` is unaffected.
"""

from __future__ import annotations

#: Per-deck set of `DrcRule.check` kinds the golden-deck manifest covers --
#: see `generate_golden_deck.py`'s module docstring "deliberately per-deck"
#: note.
ALLOWED_CHECKS: dict[str, tuple[str, ...]] = {
    "sky130": ("width", "space"),
    # `"enclosed"` joins the set with issue #2369's Nplus/Pplus implant
    # rules -- the first rules in any deck to use it (the direction the PDK's
    # own `ngate.enclosed(nplus, ...)`/`ncomp.edges.enclosed(nplus, ...)`
    # statements are written in). All four are `DerivedLayer`-scoped, so all
    # four are hand-authored in `_DERIVED_LAYER_FIXTURES` in `generate_golden_deck.py`.
    "gf180mcu": (
        "width",
        "space",
        "enclosing",
        "separation",
        "enclosed",
        "comp_coverage",
        "tap_distance",
    ),
    # sg13g2 (issue #905/#911, Epic #711 Phase 3b): width/space rules go
    # through this manifest mechanism (14 entries); its 5 enclosing/
    # separation rules ship as hand-written violate/clean pairs directly in
    # `tests/test_drc.py` instead -- see that deck's own module docstring.
    "sg13g2": ("width", "space"),
    # sg13cmos5l (issue #1400, width/space on Activ/GatPoly/Metal1; issue
    # #1417 extends coverage to the Metal2-TopMetal1 stack's width/space
    # rules plus every via level's width/space/enclosing rules) -- see that
    # deck's own module docstring.
    "sg13cmos5l": ("width", "space", "enclosing"),
}
