"""Regenerate the golden manifest for both decks (issue #747; issue #904,
Epic #711 Phase 3a extends this to gf180mcu's `enclosing`/`separation`
rules).

Run this whenever a piloted `DrcRule`'s `layer`/`threshold_dbu`/
`derived_layer` changes, or a rule matching a deck's `ALLOWED_CHECKS` is
added to/removed from `sky130.py`/`gf180mcu.py`:

    python tests/golden_deck/generate_golden_deck.py

For every `DrcRule` in each deck's `DECK` whose `check` is in that deck's own
`ALLOWED_CHECKS` entry below, derives a violate/clean fixture spec directly
from that rule's own `layer`/`other_layer`/`threshold_dbu` -- deterministic,
so re-running this script with no deck change reproduces byte-identical
manifests (mirrors
`tests/corpus/generate_golden.py`/`tests/golden_metrics/generate_golden_metrics.py`'s
"committed fixture + generator script" pattern). Every other rule is skipped,
not merely left unpopulated.

`ALLOWED_CHECKS` is deliberately **per-deck**: issue #747 piloted `width`/
`space` on *both* decks; issue #904 widens gf180mcu's own set to also cover
`enclosing`/`separation` (completing gf180mcu's full DRC deck -- 42 rules at
that issue, 44 as of issue #1110's `DF.1a`/`DF.3a` `_LV`/`_MV` split, per
Epic #711 Phase 3a's "every rule ships a golden pair" acceptance criterion),
but deliberately leaves sky130's set at its original `width`/`space` pilot
scope -- extending sky130's own enclosure coverage is a separate, unscoped
follow-on (`docs/design/deck-compiler-proposal.md`'s own "narrow go"
recommendation), not something issue #904 (a gf180mcu-focused phase) should
fold in as a side effect of a shared generator.

Four rules are a documented exception to the generic `width`/`space`/
`enclosing`/`separation`-pair builders below, all of them because they scope
their checked region via a `DerivedLayer` (real drawn layers read outside the
plain `layer`/`other_layer` shape every other rule uses), so all four
fixtures are hand-authored in `_DERIVED_LAYER_FIXTURES` below rather than
derived generically:

- gf180mcu's `mim.enclosing.via4.1` and `mim.space.1` (issue #1033) --
  `mim.enclosing.via4.1` reads three layers (FuseTop/Metal4/Via4);
  `mim.space.1` reads two (FuseTop/Metal4) but its `other_layer` is *also*
  Metal4, so a naive `_separation_pair` fixture would draw two bare Metal4
  bars with no FuseTop at all and never trip the rule.
- gf180mcu's `comp.width.mv.1` and `comp.space.mv.1` (issue #1110) -- the
  `_MV` halves of the `DF.1a`/`DF.3a` voltage-domain rule pairs, whose
  checked region is `Comp` polygons *overlapping* `Dualgate` (55/0). A
  generic single-layer `_width_pair`/`_space_pair` fixture draws no
  `Dualgate` at all, so the derived region would be empty and the
  `"violate"` case would report clean.

Their `_LV` counterparts (`comp.width.1`/`comp.space.1`) need no such
exception: their `"not_interacting"` derivation over a fixture that draws no
`Dualgate` is the full `Comp` layer, i.e. exactly the generic fixture's own
geometry (and exactly what those two rules checked before #1110 split them).

`"expected_disagreement"` is a deliberately **hand-authored** annotation (see
`README.md`), never derived from geometry -- regeneration preserves each
entry's existing value across a re-run rather than resetting it to `null`,
the same "refresh the derived value, keep the human-authored band" split
`generate_golden_metrics.py` uses for its own `tol_pct`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

GOLDEN_DECK_DIR = Path(__file__).resolve().parent
REPO_ROOT = GOLDEN_DECK_DIR.parent.parent

sys.path.insert(0, str(REPO_ROOT / "src"))

from klayout_tools.decks import (  # noqa: E402
    DrcRule,  # noqa: E402
    gf180mcu,
    sg13cmos5l,
    sg13g2,
    sky130,
)

DECKS: dict[str, list[DrcRule]] = {
    "sky130": sky130.DECK,
    "gf180mcu": gf180mcu.DECK,
    "sg13g2": sg13g2.DECK,
    "sg13cmos5l": sg13cmos5l.DECK,
}

#: Per-deck set of `DrcRule.check` kinds this manifest covers -- see the
#: module docstring's "deliberately per-deck" note above.
ALLOWED_CHECKS: dict[str, tuple[str, ...]] = {
    "sky130": ("width", "space"),
    "gf180mcu": ("width", "space", "enclosing", "separation"),
    # sg13g2 (issue #905/#911, Epic #711 Phase 3b): width/space rules go
    # through this manifest mechanism (14 entries); its 5 enclosing/
    # separation rules ship as hand-written violate/clean pairs directly in
    # `tests/test_drc.py` instead -- see that deck's own module docstring.
    "sg13g2": ("width", "space"),
    # sg13cmos5l (issue #1400): a MOS-only starter, width/space rules only
    # (Activ/GatPoly/Metal1) -- see that deck's own module docstring.
    "sg13cmos5l": ("width", "space"),
}

#: Fixed bar dimensions (database units), independent of any individual
#: rule's own threshold -- chosen once, verified safe for every rule in this
#: 37-rule pilot (largest threshold: gf180mcu's `mim.space.1`, 1200 dbu;
#: largest width threshold: `metaltop.width.1`, 360 dbu):
#: - Width-check bar length: long enough that only the bar's own width (not
#:   its end caps) trips `width_check`, for every threshold in this pilot.
#: - Space-check bar width (ordinary conductor layers): wide enough to
#:   clear every width threshold in this pilot (so a space fixture never
#:   *also* trips a width violation on the same layer) and narrow enough
#:   (< 3000 dbu / 3um) to stay outside sky130's own "huge metal" >=3um
#:   morphological-opening exception (`sky130A.lydrc`'s `huge_m1`/
#:   `huge_m2`, which reroutes met1/met2 spacing to a *different* rule id
#:   -- "m1.3ab"/"m2.3ab" -- at a different, 0.28um threshold, not this
#:   pilot's `met1.space.1`/`met2.space.1`) -- verified against a real
#:   sky130A install for this issue (see `README.md`'s cross-check notes).
_WIDTH_BAR_LENGTH_DBU = 4000
_SPACE_BAR_WIDTH_DBU = 2000
_SPACE_BAR_LENGTH_DBU = 4000

#: sky130A.lydrc's own manufacturing-grid check (`OFFGRID` section,
#: `*.ongrid(0.005)`, i.e. every vertex must land on a 0.005um / 5 dbu grid)
#: fires as an *independent* violation on any shape whose coordinates are
#: not a multiple of this grid -- unrelated to the width/space rule under
#: test, but counted by the cross-check's "any violation present" agreement
#: test all the same. Every piloted rule's `threshold_dbu` is itself a
#: multiple of 5 (verified for this issue), so keeping the margin below/
#: above threshold *also* a multiple of 5 keeps every generated coordinate
#: on-grid, avoiding a spurious cross-check disagreement that has nothing
#: to do with the rule being piloted.
_GRID_DBU = 5


def _margin_dbu(threshold_dbu: int) -> int:
    """Violate/clean margin below/above `threshold_dbu`: proportional but
    capped at 100 dbu (0.1um) so it never swamps the real geometry, floored
    at `_GRID_DBU` so it's never zero/negative for this pilot's smallest
    threshold (140 dbu, sky130's met1/met2 width/space), and rounded to a
    multiple of `_GRID_DBU` so every fixture coordinate stays on sky130's
    manufacturing grid (see `_GRID_DBU`'s own comment above)."""
    raw = max(_GRID_DBU, min(100, threshold_dbu // 3))
    return (raw // _GRID_DBU) * _GRID_DBU


def _width_pair(
    layer: tuple[int, int], threshold_dbu: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    margin = _margin_dbu(threshold_dbu)
    violate_w = max(threshold_dbu - margin, 1)
    clean_w = threshold_dbu + margin
    layer_list = list(layer)
    violate = {
        "shapes": [
            {"layer": layer_list, "box": [0, 0, violate_w, _WIDTH_BAR_LENGTH_DBU]}
        ]
    }
    clean = {
        "shapes": [{"layer": layer_list, "box": [0, 0, clean_w, _WIDTH_BAR_LENGTH_DBU]}]
    }
    return violate, clean


def _space_pair(
    layer: tuple[int, int], threshold_dbu: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    margin = _margin_dbu(threshold_dbu)
    violate_gap = max(threshold_dbu - margin, 1)
    clean_gap = threshold_dbu + margin
    layer_list = list(layer)

    def pair(gap: int) -> dict[str, Any]:
        return {
            "shapes": [
                {
                    "layer": layer_list,
                    "box": [0, 0, _SPACE_BAR_WIDTH_DBU, _SPACE_BAR_LENGTH_DBU],
                },
                {
                    "layer": layer_list,
                    "box": [
                        _SPACE_BAR_WIDTH_DBU + gap,
                        0,
                        2 * _SPACE_BAR_WIDTH_DBU + gap,
                        _SPACE_BAR_LENGTH_DBU,
                    ],
                },
            ]
        }

    return pair(violate_gap), pair(clean_gap)


#: Fixed size (database units) of the "enclosed"/"other_layer" shape an
#: `"enclosing"` fixture draws (e.g. a contact/via/pad-opening) -- large
#: enough to clear every width/space threshold in either deck's DECK on its
#: own (so drawing it never *also* trips an unrelated width/space rule on the
#: same layer, see `_enclosing_pair`'s own docstring), independent of the
#: enclosing rule's own threshold.
_ENCLOSED_SHAPE_SIZE_DBU = 2000

#: The width (database units) of the "escape" strip an `"enclosing"`
#: fixture's `"violate"` case leaves uncovered by the enclosing layer -- see
#: `_enclosing_pair`'s own docstring for why every `"violate"` fixture here
#: uses this "escape" mechanism (`_run_check`'s `outside_region` term, #318)
#: rather than a marginal-distance violation.
_ENCLOSING_ESCAPE_WIDTH_DBU = 500


def _enclosing_pair(
    layer: tuple[int, int], other_layer: tuple[int, int], threshold_dbu: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a violate/clean pair for an `"enclosing"` `DrcRule` (`layer`
    encloses `other_layer`, e.g. metal encloses a contact/via).

    `"clean"`: `other_layer` is a fixed `_ENCLOSED_SHAPE_SIZE_DBU` square,
    `layer` fully surrounds it with a margin of `threshold_dbu +
    _margin_dbu(threshold_dbu)` on every side -- clears the rule for both a
    positive threshold (a `Region.enclosing_check` marginal-distance pass)
    and a zero threshold (`metal1.enclosing.via1.1`'s `0.0um`, where any
    positive margin already satisfies the check).

    `"violate"`: `layer` covers only part of `other_layer` (a
    `_ENCLOSING_ESCAPE_WIDTH_DBU`-wide strip is left uncovered but still
    touching `layer` elsewhere), tripping `_run_check`'s `outside_region`
    zero-overlap-escape term under this rule's own id -- chosen uniformly
    (rather than a marginal-distance violation) because it is the one
    mechanism that reliably trips regardless of whether `threshold_dbu` is
    zero or positive (a `0.0um` threshold's own `enclosing_check` never
    reports a marginal violation at all, see `metal1.enclosing.via1.1`'s own
    docstring in `gf180mcu.py`)."""
    margin = threshold_dbu + _margin_dbu(threshold_dbu)
    other_box = [0, 0, _ENCLOSED_SHAPE_SIZE_DBU, _ENCLOSED_SHAPE_SIZE_DBU]
    clean_layer_box = [
        -margin,
        -margin,
        _ENCLOSED_SHAPE_SIZE_DBU + margin,
        _ENCLOSED_SHAPE_SIZE_DBU + margin,
    ]
    violate_layer_box = [
        0,
        0,
        _ENCLOSED_SHAPE_SIZE_DBU - _ENCLOSING_ESCAPE_WIDTH_DBU,
        _ENCLOSED_SHAPE_SIZE_DBU,
    ]
    other_list = list(other_layer)
    layer_list = list(layer)
    violate = {
        "shapes": [
            {"layer": other_list, "box": other_box},
            {"layer": layer_list, "box": violate_layer_box},
        ]
    }
    clean = {
        "shapes": [
            {"layer": other_list, "box": other_box},
            {"layer": layer_list, "box": clean_layer_box},
        ]
    }
    return violate, clean


def _separation_pair(
    layer: tuple[int, int], other_layer: tuple[int, int], threshold_dbu: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a violate/clean pair for a `"separation"` `DrcRule` (`layer`
    must keep `threshold_dbu` away from `other_layer`) -- the two-layer
    sibling of `_space_pair` above (same bar dimensions/margin, one bar per
    layer instead of two bars on the same layer)."""
    margin = _margin_dbu(threshold_dbu)
    violate_gap = max(threshold_dbu - margin, 1)
    clean_gap = threshold_dbu + margin
    layer_list = list(layer)
    other_list = list(other_layer)

    def pair(gap: int) -> dict[str, Any]:
        return {
            "shapes": [
                {
                    "layer": layer_list,
                    "box": [0, 0, _SPACE_BAR_WIDTH_DBU, _SPACE_BAR_LENGTH_DBU],
                },
                {
                    "layer": other_list,
                    "box": [
                        _SPACE_BAR_WIDTH_DBU + gap,
                        0,
                        2 * _SPACE_BAR_WIDTH_DBU + gap,
                        _SPACE_BAR_LENGTH_DBU,
                    ],
                },
            ]
        }

    return pair(violate_gap), pair(clean_gap)


#: Hand-authored fixtures for rules whose checked region is not the plain
#: `(layer, other_layer)` pair `_enclosing_pair`/`_separation_pair` above
#: assume -- see the module docstring's "documented exception" note.
#: gf180mcu's `mim.enclosing.via4.1` scopes its "enclosing" side to a
#: `DerivedLayer` (`FuseTop.sized(1.06um) & Metal4.interacting(FuseTop)`,
#: `decks/gf180mcu.py`'s own `derived_layer` field): drawing a
#: comfortably-oversized `Metal4` box (`(46, 0)`) fully covering a `FuseTop`
#: box (`(75, 0)`) makes the derived region exactly `FuseTop.sized(1.06um)`
#: (`[-1060, -1060, 7060, 7060]` for a `[0, 0, 6000, 6000]` FuseTop box);
#: `Via4` (`(41, 0)`) is then placed with a `threshold_dbu(400) +
#: _margin_dbu(400)(100) = 500` dbu margin inside that derived region for
#: `"clean"`, or straddling its right edge (an `outside_region` escape, the
#: same mechanism `_enclosing_pair` uses) for `"violate"`. Verified (this
#: issue) to not incidentally trip any other gf180mcu `DrcRule` -- neither
#: fixture draws Via1/Via2/Via3/MetalTop/Pad/Nwell/Comp/Poly2/Contact, the
#: only other layers any *other* rule in this deck reads, and every
#: single-layer width/space rule on Metal4/FuseTop/Via4 clears its own
#: threshold by construction (a lone polygon has no `space_check` neighbour;
#: `_ENCLOSED_SHAPE_SIZE_DBU`-plus geometry clears every width threshold).
#: gf180mcu's `mim.space.1` (issue #1033) scopes its "separation" check's
#: `region` side the same way, but its `other_layer` is *also* `Metal4` --
#: `run_drc()` additionally excludes any `Metal4` polygon that itself
#: overlaps the raw (unsized) `FuseTop` from the "other" side (see
#: `drc.py`'s own comment on this), so a single Metal4 bar fully covered by a
#: comfortably-enclosed FuseTop box becomes the entire derived plate (not
#: split into a plate fragment plus a same-polygon "other" leftover), and a
#: second, genuinely separate Metal4 bar -- never touching FuseTop at all --
#: is placed `threshold_dbu(1200) +/- _margin_dbu(1200)(100)` away from the
#: first bar's own edge for `"clean"`/`"violate"`. The FuseTop box keeps a
#: 700 dbu margin inside its own Metal4 bar (>= the 600 dbu
#: `mim.enclosing.fusetop.1` threshold) so neither fixture incidentally trips
#: that rule too.
_DERIVED_LAYER_FIXTURES: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "mim.enclosing.via4.1": (
        {  # violate: Via4 straddles the derived region's right edge (x=7060)
            "shapes": [
                {"layer": [75, 0], "box": [0, 0, 6000, 6000]},  # FuseTop
                {"layer": [46, 0], "box": [-3000, -3000, 9000, 9000]},  # Metal4
                {"layer": [41, 0], "box": [6800, 0, 8000, 2000]},  # Via4 (escapes)
            ]
        },
        {  # clean: Via4 sits >= 500 dbu inside the derived region on every side
            "shapes": [
                {"layer": [75, 0], "box": [0, 0, 6000, 6000]},  # FuseTop
                {"layer": [46, 0], "box": [-3000, -3000, 9000, 9000]},  # Metal4
                {"layer": [41, 0], "box": [-560, -560, 6560, 6560]},  # Via4
            ]
        },
    ),
    "mim.space.1": (
        {  # violate: second Metal4 bar 1100 dbu (< 1200) from the plate
            "shapes": [
                {"layer": [46, 0], "box": [0, 0, 2000, 4000]},  # bottom plate
                {"layer": [75, 0], "box": [700, 700, 1300, 3300]},  # FuseTop
                {"layer": [46, 0], "box": [3100, 0, 5100, 4000]},  # other Metal4
            ]
        },
        {  # clean: second Metal4 bar 1300 dbu (> 1200) from the plate
            "shapes": [
                {"layer": [46, 0], "box": [0, 0, 2000, 4000]},  # bottom plate
                {"layer": [75, 0], "box": [700, 700, 1300, 3300]},  # FuseTop
                {"layer": [46, 0], "box": [3300, 0, 5300, 4000]},  # other Metal4
            ]
        },
    ),
    # `comp.width.mv.1` (DF.1a_MV, 300 dbu): the generic `_width_pair`
    # geometry (a `threshold -/+ _margin_dbu(300)(100)` = 200/400 dbu wide
    # Comp bar, `_WIDTH_BAR_LENGTH_DBU` long) plus a `Dualgate` box
    # comfortably covering it, which is what moves the bar out of
    # `comp.width.1`'s `"not_interacting"` region and into this rule's
    # `"overlapping"` one. Note the 200 dbu violate bar is *also* below the
    # 220 dbu `_LV` threshold: covering it with `Dualgate` is what proves
    # the split works, since the `_LV` rule must NOT report it.
    "comp.width.mv.1": (
        {  # violate: 200 dbu wide (< 300), fully inside Dualgate
            "shapes": [
                {"layer": [22, 0], "box": [0, 0, 200, 4000]},  # Comp
                {"layer": [55, 0], "box": [-1000, -1000, 1200, 5000]},  # Dualgate
            ]
        },
        {  # clean: 400 dbu wide (> 300), fully inside Dualgate
            "shapes": [
                {"layer": [22, 0], "box": [0, 0, 400, 4000]},  # Comp
                {"layer": [55, 0], "box": [-1000, -1000, 1400, 5000]},  # Dualgate
            ]
        },
    ),
    # `comp.space.mv.1` (DF.3a_MV, 360 dbu): the generic `_space_pair`
    # geometry (two `_SPACE_BAR_WIDTH_DBU`-wide bars separated by
    # `threshold -/+ _margin_dbu(360)(100)` = 260/460 dbu) plus a `Dualgate`
    # box covering both bars. The 260 dbu violate gap is also below the
    # 280 dbu `_LV` threshold, so -- as for the width pair above -- a clean
    # `comp.space.1` verdict on this fixture is itself part of what the
    # split is asserting.
    "comp.space.mv.1": (
        {  # violate: 260 dbu gap (< 360), both bars inside Dualgate
            "shapes": [
                {"layer": [22, 0], "box": [0, 0, 2000, 4000]},  # Comp
                {"layer": [22, 0], "box": [2260, 0, 4260, 4000]},  # Comp
                {"layer": [55, 0], "box": [-1000, -1000, 5260, 5000]},  # Dualgate
            ]
        },
        {  # clean: 460 dbu gap (> 360), both bars inside Dualgate
            "shapes": [
                {"layer": [22, 0], "box": [0, 0, 2000, 4000]},  # Comp
                {"layer": [22, 0], "box": [2460, 0, 4460, 4000]},  # Comp
                {"layer": [55, 0], "box": [-1000, -1000, 5460, 5000]},  # Dualgate
            ]
        },
    ),
}


def build_manifest(
    deck_name: str,
    deck_rules: list[DrcRule],
    existing: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    allowed_checks = ALLOWED_CHECKS[deck_name]
    manifest: dict[str, dict[str, Any]] = {}
    for rule in deck_rules:
        if rule.check not in allowed_checks:
            continue
        if rule.id in _DERIVED_LAYER_FIXTURES:
            violate, clean = _DERIVED_LAYER_FIXTURES[rule.id]
        elif rule.check == "width":
            violate, clean = _width_pair(rule.layer, rule.threshold_dbu)
        elif rule.check == "space":
            violate, clean = _space_pair(rule.layer, rule.threshold_dbu)
        elif rule.check == "enclosing":
            assert rule.other_layer is not None, rule.id
            violate, clean = _enclosing_pair(
                rule.layer, rule.other_layer, rule.threshold_dbu
            )
        else:  # "separation"
            assert rule.other_layer is not None, rule.id
            violate, clean = _separation_pair(
                rule.layer, rule.other_layer, rule.threshold_dbu
            )
        prior = existing.get(rule.id, {})
        manifest[rule.id] = {
            "check": rule.check,
            "layer": list(rule.layer),
            "threshold_dbu": rule.threshold_dbu,
            "violate": violate,
            "clean": clean,
            # Hand-authored, preserved across regeneration -- see the
            # module docstring and README.md.
            "expected_disagreement": prior.get("expected_disagreement"),
        }
    return manifest


def main() -> int:
    for deck_name, deck_rules in DECKS.items():
        out_dir = GOLDEN_DECK_DIR / deck_name
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "manifest.json"
        existing: dict[str, dict[str, Any]] = {}
        if out_path.exists():
            existing = json.loads(out_path.read_text(encoding="utf-8"))
        manifest = build_manifest(deck_name, deck_rules, existing)
        out_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"wrote {out_path.relative_to(REPO_ROOT)} ({len(manifest)} rules)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
