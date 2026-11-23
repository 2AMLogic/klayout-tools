"""Variant-keyed top-metal landing-pad floor tables for ``klt gen-compose``
(issues #6612, #6645).

Relocated verbatim out of ``gen_compose_layer_rules.py`` (which the #6645
MT.4 area floor pushed past the 500-line per-file size limit): the two
variant-keyed floor tables and the rule identities they report under. They
are pure data -- no imports, so no load-time dependency on any other
module -- and ``gen_compose_layer_rules`` reads them *through this module's
attributes* (``_top_metal_floors._TOP_METAL_LANDING_..``) rather than
re-importing the names, so rebinding a table here (as the tests do with
``monkeypatch.setattr``) is seen by the lookups that consume it.
"""

from __future__ import annotations

#: Variant-keyed top-metal landing-pad *width* floors in um (issue #6612) --
#: the one size gen-compose floors off a rule its curated decks *cannot*
#: express, so it is keyed by exact variant string here rather than resolved
#: from a deck. gf180mcu's curated deck carries no MetalTop thickness
#: option: its only width rules touching Metal5 ``(81, 0)`` are
#: ``metal5.width.1`` = 0.28um (below the 0.42um ``_VIA_LANDING_SIZE_UM``
#: baseline, so a deck-driven width floor is a no-op) and
#: ``metaltop.width.1`` = 0.36um on MetalTop ``(53, 0)`` -- a layer no 5LM
#: composition draws -- while the 0.44um DRM value is documented there as
#: not modeled. The *native* runset both shipped 5LM variants select does
#: model it: its variant table maps ``gf180mcuC`` -> ``5LM, 9K`` and
#: ``gf180mcuD`` -> ``5LM, 11K`` (5LM being the only stack where Metal5
#: *is* the top metal, the stack this package's role table resolves), and
#: ``metaltop.drc`` applies ``MT.1`` = 0.44um for both ``'9K'`` and
#: ``'11K'`` -- 0.36um only for ``'6K'``, which no shipped variant selects.
#: gf180mcuA/B (3LM/4LM) are deliberately absent: Metal5 is not their top
#: metal, and their landing sizes stay exactly as before (#6612 scope).
_TOP_METAL_LANDING_WIDTH_FLOOR_UM: dict[str, float] = {
    "gf180mcuC": 0.44,
    "gf180mcuD": 0.44,
}

#: The rule identity the width floor reports itself under in
#: ``gen_compose_layer_rules._landing_pad_floor_um``'s ``floors`` -- the
#: native runset's own rule id, so a ``routing.landing_pad_size_um``
#: rejection (#5803) names the binding rule instead of a bare threshold.
_TOP_METAL_LANDING_WIDTH_FLOOR_RULE_ID = "MT.1 (native runset, 9K/11K)"

#: Variant-keyed top-metal landing-pad *area* floors in um^2 (issue
#: #6645) -- keyed beside the #6612 width floor above, for the same
#: reason: the native runset's ``MT.4`` minimum *area* (0.5625um^2,
#: applied for ``'9K'``/``'11K'`` via the same ``with_area(nil,
#: 0.5625.um)`` branch) is likewise absent from the curated deck -- it
#: sits in ``decks/gf180mcu.py``'s ``UNTRANSCRIBED`` list -- so
#: ``_min_area_um2_for_layer`` returns ``None`` for Metal5 ``(81, 0)``
#: and an isolated landing pad drawn at the 0.44um width floor is only
#: 0.1936um^2, below the native floor (issue #6645's reproduced MT.4
#: finding on both shipped 5LM variants). sqrt(0.5625) = 0.75um, so the
#: area floor subsumes the width floor on these variants; both are kept
#: so each rule stays separately nameable in a
#: ``routing.landing_pad_size_um`` rejection and the floors keep
#: composing as independent maxima.
_TOP_METAL_LANDING_AREA_FLOOR_UM2: dict[str, float] = {
    "gf180mcuC": 0.5625,
    "gf180mcuD": 0.5625,
}

#: The rule identity the area floor reports itself under -- same shape
#: as :data:`_TOP_METAL_LANDING_WIDTH_FLOOR_RULE_ID` above.
_TOP_METAL_LANDING_AREA_FLOOR_RULE_ID = "MT.4 (native runset, 9K/11K)"
