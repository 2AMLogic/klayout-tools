# Golden-pair manifest (`klt drc` width/space rules)

Declarative, rule-id-keyed golden violate/clean fixture manifest for the 37
width/space `DrcRule` entries across `sky130.py` (11) and `gf180mcu.py` (26)
-- issue #747, the first, narrow-go increment of
[`docs/design/deck-compiler-proposal.md`](../../docs/design/deck-compiler-proposal.md)
§5/§6. Formalises the informal `_violation`/`_clean` test-function pairs
`tests/test_drc.py` already carried for some of these rules into a single,
coverage-checked artifact, distinct from (not a replacement for)
`tests/test_drc.py`'s own broader regression coverage.

## Why width/space only

Per the proposal's §6, this pilot is scoped to `check in {"width", "space"}`
rules -- the only two check kinds (of `width`/`space`/`notch`/`separation`/
`enclosing`/`enclosed`/`overlap`) requiring **zero new check primitives** and
no second drawn layer, keeping the manifest's fixture geometry uniform (a
single layer, one or two boxes) across every entry. `enclosing`/`separation`
rules and the `area`/`density`/`antenna` check kinds that don't exist yet in
this engine at all (see `DrcRule`'s docstring) are explicitly out of scope
here -- named follow-ons per the proposal's own sequencing, not something
this manifest silently skips.

## File layout

```
tests/golden_deck/
  __init__.py            # package marker (tests/ import convention, see below)
  manifest.py            # load_manifest() / build_layout() / write_layout()
  generate_golden_deck.py  # regeneration script (see "Regenerating" below)
  sky130/manifest.json     # 11 entries
  gf180mcu/manifest.json   # 26 entries
```

`tests/test_golden_deck.py` is the consuming test module (not under this
directory, alongside every other `tests/test_*.py`) -- it imports
`golden_deck.manifest` the same way `tests/test_metrics_regression.py`
imports `helpers.metrics_regression` (both rely on pytest's default
rootdir-insertion `sys.path` behaviour for a package with no `__init__.py`
directly under `tests/`).

## Manifest schema

Each `manifest.json` is a JSON object keyed by `DrcRule.id`:

```json
{
  "poly.width.1": {
    "check": "width",
    "layer": [66, 20],
    "threshold_dbu": 150,
    "violate": {
      "shapes": [{"layer": [66, 20], "box": [0, 0, 100, 4000]}]
    },
    "clean": {
      "shapes": [{"layer": [66, 20], "box": [0, 0, 200, 4000]}]
    },
    "expected_disagreement": null
  }
}
```

- `check`/`layer`/`threshold_dbu` echo the rule's own fields (for readability
  and debugging -- the deck itself, not this manifest, remains the source of
  truth for those values).
- `violate`/`clean` are each `{"shapes": [{"layer": [layer, datatype], "box":
  [x0, y0, x1, y1]}, ...]}` -- one or two boxes on the rule's own single
  drawn layer (database units), built into a one-`TOP`-cell layout by
  `manifest.build_layout`/`write_layout`. A width fixture is one box (bar
  narrower/wider than `threshold_dbu`); a space fixture is two parallel bars
  separated by a gap narrower/wider than `threshold_dbu`.
- `expected_disagreement` is `null` unless a rule's own documented
  approximation (see its `DrcRule.description`/inline comment) is known to
  produce a genuine, reviewed disagreement between the curated engine and
  the real PDK-native deck for *this specific fixture* -- see "Cross-check
  results" below. None of the 37 piloted rules need one today (verified
  empirically, not assumed -- see that section).

## Regenerating

Fixture geometry is derived deterministically from each rule's own
`layer`/`threshold_dbu` -- a deliberate, reviewed act, not something to
hand-edit:

```bash
python tests/golden_deck/generate_golden_deck.py
git diff tests/golden_deck/
```

Bar dimensions (4000 dbu bar length; 2000 dbu bar width for space-check
pairs) are fixed constants independent of any individual rule's threshold,
chosen once and verified safe for every rule in this 37-rule pilot -- see
`generate_golden_deck.py`'s own module docstring for the exact reasoning
(including sky130's "huge metal" >=3um spacing-exception boundary, which the
2000 dbu space-fixture bar width was chosen to stay clear of). The
violate/clean margin below/above each rule's `threshold_dbu` is `max(10,
min(100, threshold_dbu // 3))` dbu -- proportional but capped, so it's never
zero/negative for this pilot's smallest threshold (140 dbu) and never so
large it swamps the real geometry for its largest (1200 dbu).

`"expected_disagreement"` is the one field the regenerator **preserves**
across a re-run rather than deriving -- a deliberate, human-authored
annotation, the same "refresh the derived value, keep the human-authored
band" split `tests/golden_metrics/generate_golden_metrics.py` uses for its
own `tol_pct`.

## Test coverage (`tests/test_golden_deck.py`)

Three tiers:

1. **Coverage** -- `test_golden_manifest_covers_every_width_space_rule`:
   `set(manifest) == {rule.id for rule in DECK if rule.check in ("width",
   "space")}` per deck, and every entry has non-empty `violate`/`clean`
   shapes. `test_piloted_rules_have_provenance_populated` additionally
   confirms every piloted rule's `DrcRule.provenance` (see
   `src/klayout_tools/decks/__init__.py`) is populated and distinct from its
   own `DrcRule.id`.
2. **Curated-engine agreement** -- `test_golden_pair_curated_engine_agrees`:
   each `"violate"` fixture trips exactly its own rule id under `run_drc`
   (the curated engine); each `"clean"` fixture is fully clean. No `klayout`
   binary needed -- runs in ordinary CI.
3. **Native-deck cross-check** (sky130 only) --
   `test_golden_pair_sky130_native_deck_cross_check`: each fixture is
   additionally run through `run_drc_klayout_engine` against a real,
   `volare`/`ciel`-resolved `sky130A.lydrc`, asserting the same
   violate/clean *status* (not exact rule id -- the native deck's own rule
   ids differ from this repo's) agrees between engines, honouring any
   `expected_disagreement`. Gated `skipif` on both a real `klayout` binary
   on `$PATH` **and** a resolvable sky130A install (mirrors
   `tests/test_drc_klayout_engine.py`'s own `HAVE_KLAYOUT_BINARY` gate) --
   skips cleanly, never fails, in an environment with neither. gf180mcu's
   own `--engine klayout` cross-check is explicitly deferred (its native
   deck has no single runnable file -- see `docs/cli/drc.md`'s "Engine" ->
   "klayout" limitation); tiers 1 and 2 still cover its 26 rules in full.

## Cross-check results (issue #747, verified 2026-08-11)

Ran tier 3 against a real `volare`-fetched sky130A install
(`open_pdks c6d73a35f524070e85faff4a6a9eef49553ebc2b`, the same commit
`sky130.py`'s own provenance notes cite) and a real KLayout 0.30.10 binary,
covering all 11 sky130 width/space rules (22 fixtures: 11 violate + 11
clean):

**11/11 rules verified against the native deck, 0 documented
approximations needed, 0 unexplained disagreements.**

Every violate fixture reported a violation under both the curated engine and
the real `sky130A.lydrc`; every clean fixture reported none under both --
including `diff.width.1`, whose own docstring notes the *general*
`difftap = diff.or(tap)` approximation (checking `diff.drawing` alone
understates the real `difftap.1` rule's scope when `tap` is also drawn):
this pilot's fixtures draw only `diff.drawing`, so the union with an empty
`tap` region is exactly `diff.drawing` and the two engines agree on this
specific fixture, correctly not needing an `expected_disagreement` entry for
it. No `expected_disagreement` entries were needed for any of the 37 piloted
rules (all 11 sky130 rules empirically cross-checked directly; gf180mcu's 26
rules are deferred per the scope above, so they carry no cross-check verdict
either way yet -- not a claim of agreement).
