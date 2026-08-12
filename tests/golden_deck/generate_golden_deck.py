"""Regenerate the golden width/space manifest for both decks (issue #747).

Run this whenever a piloted `DrcRule`'s `layer`/`threshold_dbu` changes, or a
width/space rule is added to/removed from `sky130.py`/`gf180mcu.py`:

    python tests/golden_deck/generate_golden_deck.py

For every `check in {"width", "space"}` `DrcRule` in each deck's `DECK`,
derives a violate/clean box-pair fixture spec directly from that rule's own
`layer`/`threshold_dbu` -- deterministic, so re-running this script with no
deck change reproduces byte-identical manifests (mirrors
`tests/corpus/generate_golden.py`/`tests/golden_metrics/generate_golden_metrics.py`'s
"committed fixture + generator script" pattern). Every rule with `check not
in {"width", "space"}` (e.g. this pilot's out-of-scope `enclosing`/
`separation` rules) is skipped, not merely left unpopulated.

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
    sg13g2,
    sky130,
)

DECKS: dict[str, list[DrcRule]] = {
    "sky130": sky130.DECK,
    "gf180mcu": gf180mcu.DECK,
    "sg13g2": sg13g2.DECK,
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


def build_manifest(
    deck_rules: list[DrcRule], existing: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    manifest: dict[str, dict[str, Any]] = {}
    for rule in deck_rules:
        if rule.check not in ("width", "space"):
            continue
        if rule.check == "width":
            violate, clean = _width_pair(rule.layer, rule.threshold_dbu)
        else:
            violate, clean = _space_pair(rule.layer, rule.threshold_dbu)
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
        manifest = build_manifest(deck_rules, existing)
        out_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"wrote {out_path.relative_to(REPO_ROOT)} ({len(manifest)} rules)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
