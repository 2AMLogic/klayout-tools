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
  sky130/manifest.json     # 27 entries (11 at issue #747, grown since)
  gf180mcu/manifest.json   # 26 entries
  sg13g2/manifest.json     # 14 entries (issue #905, Epic #711 Phase 3b --
                            # see that deck's own module docstring; this
                            # manifest mechanism is unchanged, only DECK_NAMES/
                            # DECKS grew a third entry)
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
  "li1.width.1": {
    "check": "width",
    "layer": [67, 20],
    "threshold_dbu": 170,
    "violate": {
      "shapes": [{"layer": [67, 20], "box": [0, 0, 115, 4000]}]
    },
    "clean": {
      "shapes": [{"layer": [67, 20], "box": [0, 0, 225, 4000]}]
    },
    "expected_disagreement": null
  }
}
```

(`li1.width.1` is shown because its `expected_disagreement` really is
`null`. Five of sky130's eleven entries carry a **non-null** annotation --
see "Cross-check results" below -- so do not read this example as
representative of the whole manifest.)

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
  results" below. **Five** of the 37 piloted rules carry one today, all in
  sky130 (`diff.width.1`, `mcon.space.1`, `poly.width.1`, `via.space.1`,
  `via.width.1`); each was established empirically against a real
  `sky130A.lydrc`, not assumed -- see that section.

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

**11/11 rules verified against the native deck, 5 documented
approximations, 0 unexplained disagreements.**

**Six** of the eleven agree outright on both fixtures -- `li1.width.1`,
`li1.space.1`, `met1.width.1`, `met1.space.1`, `met2.width.1`,
`met2.space.1`: each violate fixture trips the corresponding native BEOL
rule (e.g. `met1.width.1` -> `m1.1`, `met2.width.1` -> `m2.1`,
`met2.space.1` -> `m2.2`) and each clean fixture is clean under both
engines. These six carry `"expected_disagreement": null`.

**Five** genuinely disagree on one of their two fixtures, and each carries a
non-null `expected_disagreement` in `sky130/manifest.json` recording why.
These are documented approximations, not unexplained failures -- the tier-3
test tolerates exactly these and still fails loudly on any *other*
disagreement:

| Rule id | Fixture that disagrees | Curated | Native `sky130A.lydrc` | Why (abridged -- full text in the manifest) |
|---|---|---|---|---|
| `diff.width.1` | `violate` | violations | clean | The script hard-codes `FEOL = false` ("do not change"), with no `-rd`-settable override, so this rule sits inside an `if FEOL ... end` block the native run never evaluates. |
| `poly.width.1` | `violate` | violations | clean | Same `FEOL = false` gate as `diff.width.1`. |
| `mcon.space.1` | `clean` | clean | violations (`ct.1`, `ct.4`) | A bare, isolated mcon shape -- all this single-layer pilot can build -- trips the native deck's `ct.1` (mcon edges must be exactly 0.17um) and `ct.4` (mcon must be covered by li), neither of which this pilot's geometry can satisfy. |
| `via.width.1` | `clean` | clean | violations (`via.1a`, `m2.via`, `via.4c.5c`) | The native `via.1a` demands an *exact* 0.15um square (`edges.without_length(0.15)`), not a minimum width; any fixture wide enough to pass this engine's min-only `width_check` is by construction wider than the native max. |
| `via.space.1` | `clean` | clean | violations (`m2.5`, `m2.via`, `via.1a`, `via.4c.5c`) | Same exact-size `via.1a` mismatch, plus the native deck's met2-enclosure rules that this single-layer fixture never draws. |

Note in particular that `diff.width.1`'s disagreement is **not** about the
`difftap = diff.or(tap)` approximation noted in its own docstring (this
pilot's fixtures draw only `diff.drawing`, so that union is exactly
`diff.drawing` and would agree) -- it is the `FEOL = false` gate above,
which suppresses the native check entirely regardless of the input layout.

gf180mcu's 26 rules are deferred per the scope above, so they carry no
cross-check verdict either way yet -- their `expected_disagreement` fields
are all `null` because none has been cross-checked, which is **not** a claim
of agreement.
