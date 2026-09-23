# Golden-pair manifest (`klt drc` rules)

Declarative, rule-id-keyed golden violate/clean fixture manifest for
`sky130.py`'s width/space `DrcRule` entries (29, +1 for issue #1420's
`nwell.width.1` and +1 for issue #2321's `tap.width.1`; `nwell.space.1`
-- issue #1420's other addition -- uses the
`"isolated"` check kind as of issue #1654, so it no longer falls within this
manifest's width/space-only scope), `sg13g2.py`'s width/space
`DrcRule` entries (32, issue #905/#911 initial 14, grown to 32 by issue
#1247), and **all** of `gf180mcu.py`'s
`DrcRule` entries (46: 14 width, 15 space, 15 enclosing, 2 separation --
recounted for issue #1110, which both added two rules and corrected a stale
width/space/separation split in this line, then grown further by issue
#1688's `metal4.width.1`/`metal4.space.1` pair) --
issue #747 (the first, narrow-go increment of
[`docs/design/deck-compiler-proposal.md`](../../docs/design/deck-compiler-proposal.md)
§5/§6, width/space only, sky130+gf180mcu) extended to gf180mcu's full DRC
deck by issue #904 (Epic #711 Phase 3a, the gf180mcu PDK-generality proof).
Formalises the informal `_violation`/`_clean` test-function pairs
`tests/test_drc.py` already carried for some of these rules into a single,
coverage-checked artifact, distinct from (not a replacement for)
`tests/test_drc.py`'s own broader regression coverage.

## Why width/space only for sky130/sg13g2, but full coverage for gf180mcu

Per the proposal's §6, issue #747's pilot scoped sky130+gf180mcu to
`check in {"width", "space"}` -- the only two check kinds (of
`width`/`space`/`notch`/`separation`/`enclosing`/`enclosed`/`overlap`)
requiring **zero new check primitives** and no second drawn layer, keeping
the manifest's fixture geometry uniform (a single layer, one or two boxes)
across every entry. Issue #904 (Epic #711 Phase 3a's "every compiled rule
ships a golden pair" acceptance criterion) widens gf180mcu's own coverage to
also include `enclosing`/`separation` (see `generate_golden_deck.py`'s
`_enclosing_pair`/`_separation_pair` builders) -- gf180mcu's DRC deck has no
`area`/`density`/`antenna` rules to omit, so `enclosing`+`separation`
completes its full 46-rule `DECK`. sky130's own manifest is deliberately left
at its original width/space pilot scope (`generate_golden_deck.py`'s
`ALLOWED_CHECKS` is per-deck): extending sky130's enclosure (or, since issue
#1955, area) coverage is a separate, unscoped follow-on -- issue #904 (a
gf180mcu-focused phase) does not fold either one in as a side effect of
sharing one generator. sg13g2 (issue #905/#911, Epic #711 Phase 3b, landed
independently of #904) made the same "width/space via this manifest,
enclosing/separation as hand-written `test_drc.py` pairs" choice for its own
deck, so `ALLOWED_CHECKS["sg13g2"]` is `("width", "space")` too. The
`area`/`density`/`antenna` check kinds exist in this engine (issue #812) --
sky130's `DECK` uses `"area"` as of issue #1955 (see `tests/test_drc.py`'s
own hand-written violate/clean pairs for those five rules) -- but none of
the three is covered by this manifest for any deck yet; `generate_golden_deck.py`
has no `_area_pair`/`_density_pair`/`_antenna_pair` builder.

## File layout

```
tests/golden_deck/
  __init__.py            # package marker (tests/ import convention, see below)
  manifest.py            # load_manifest() / build_layout() / write_layout()
  generate_golden_deck.py  # regeneration script (see "Regenerating" below)
  sky130/manifest.json     # 29 entries (11 at issue #747, width/space-only,
                            # grown since -- latest +1 is issue #2321's
                            # tap.width.1)
  gf180mcu/manifest.json   # 46 entries (full DRC deck, issue #904;
                            # +2 for issue #1110's DF.1a/DF.3a _LV/_MV split,
                            # +2 for issue #1688's metal4.width.1/
                            # metal4.space.1 pair)
  sg13g2/manifest.json     # 32 entries (issue #905/#911, Epic #711 Phase 3b
                            # -- see that deck's own module docstring; width/
                            # space only, same split as sky130 -- its 5
                            # enclosing/separation rules ship as hand-written
                            # pairs in tests/test_drc.py instead; grown from
                            # 14 to 32 by issue #1247's extended
                            # metals/vias-to-TopMetal2 stack)
  sg13cmos5l/manifest.json # 27 entries (issue #1400, width/space/enclosing
                            # on Activ/GatPoly/Metal1; issue #1417 extends
                            # coverage to the Metal2-TopMetal1 stack's width/
                            # space rules plus every via level's width/space/
                            # enclosing rules -- see that deck's own module
                            # docstring)
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
  results" below, which enumerates every rule that carries one and derives
  the count from the manifest rather than restating it here. All of them
  are sky130 rules, and each was established empirically against a real
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
chosen once and verified safe for every rule in the original 37-rule
width/space pilot (issue #747: 11 sky130 + 26 gf180mcu rules, before
gf180mcu's own coverage widened to `enclosing`/`separation` in issue #904) --
see `generate_golden_deck.py`'s own module docstring for the exact reasoning
(including sky130's "huge metal" >=3um spacing-exception boundary, which the
2000 dbu space-fixture bar width was chosen to stay clear of). The per-rule
margin formula below scales with each rule's own `threshold_dbu`, so this
remains safe for every rule added since, including the largest threshold in
either pilot deck at the time (`mim.space.1`, 1200 dbu) and today's widest
count across all four decks. The violate/clean margin below/above each
rule's `threshold_dbu` is `max(10, min(100, threshold_dbu // 3))` dbu --
proportional but capped, so it's never zero/negative for this pilot's
smallest threshold (140 dbu) and never so large it swamps the real geometry
for its largest (1200 dbu).

`"expected_disagreement"` is the one field the regenerator **preserves**
across a re-run rather than deriving -- a deliberate, human-authored
annotation, the same "refresh the derived value, keep the human-authored
band" split `tests/golden_metrics/generate_golden_metrics.py` uses for its
own `tol_pct`.

"Deterministic" above is enforced, not just asserted: CI's `Golden artifacts
(hash seed + path varied)` job regenerates this manifest from two checkouts
at different filesystem paths under two different `PYTHONHASHSEED` values and
byte-compares -- see
[`docs/guides/golden-artifact-determinism.md`](../../docs/guides/golden-artifact-determinism.md)
(issue #2225).

## Test coverage (`tests/test_golden_deck.py`)

Three tiers:

1. **Coverage** -- `test_golden_manifest_covers_every_width_space_rule`:
   `set(manifest) == {rule.id for rule in DECK if rule.check in
   ALLOWED_CHECKS[deck_name]}` per deck (sky130: width/space; gf180mcu:
   width/space/enclosing/separation, issue #904), and every entry has
   non-empty `violate`/`clean` shapes. `test_piloted_rules_have_provenance_
   populated` additionally confirms every piloted rule's `DrcRule.provenance`
   (see `src/klayout_tools/decks/__init__.py`) is populated and distinct from
   its own `DrcRule.id`.
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
   skips cleanly, never fails, in an environment with neither. gf180mcu's own
   `--engine klayout` **DRC** cross-check remains explicitly deferred as of
   issue #904 -- re-verified unchanged: gf180mcu's native *DRC* deck still
   has no single runnable file (see `docs/cli/drc.md`'s "Engine" ->
   "klayout" limitation); tiers 1 and 2 cover all 46 of its rules in full.
   **Unlike the DRC side, gf180mcu's native *LVS* deck is single-file and
   directly runnable** -- issue #904 cross-checks gf180mcu's LVS
   device-extraction rules against it separately; see
   `tests/test_lvs_native_extraction_cross_check_gf180mcu.py` and
   `docs/cli/extract.md`'s "gf180mcu native-deck LVS device-extraction
   cross-check" section (not this DRC-side manifest).

## Cross-check results (sky130)

**`sky130/manifest.json` is the single source of truth for this section.**
Every count and every rule name below is *derived* from it, not maintained
by hand -- the hand-maintained counts this section used to carry (issues
#747/#1420's "12/12 rules, 6 documented approximations") silently went stale
by more than 2x as the manifest grew, which is exactly what issue #2356
corrected. Re-derive them yourself before trusting any number here:

```bash
python3 - <<'PY'
import json
m = json.load(open("tests/golden_deck/sky130/manifest.json"))
agree = sorted(k for k, v in m.items() if v["expected_disagreement"] is None)
approx = sorted(k for k, v in m.items() if v["expected_disagreement"])
print(f"{len(m)} rules: {len(agree)} agree outright, "
      f"{len(approx)} documented approximations")
print("agree:  ", ", ".join(agree))
print("approx: ", ", ".join(approx))
PY
```

Tier 3 has been run against a real `volare`-fetched sky130A install
(`open_pdks c6d73a35f524070e85faff4a6a9eef49553ebc2b`, the same commit
`sky130.py`'s own provenance notes cite) and a real KLayout 0.30.10 binary,
first for issue #747's original 11 width/space rules, then again as the
manifest grew: PR #782 (`met3-5.*`, `via2-4.*`, `capm.*`, `capm2.*`), issue
#1420 (`nwell.width.1`), issue #2321 (`tap.width.1`, cross-checked later
under issue #2343). Manifest counts as of `bdfc4585` (2026-09-23), its most
recent change:

**29/29 rules verified against the native deck, 17 documented
approximations, 0 unexplained disagreements** (58 fixtures: 29 violate + 29
clean).

**Twelve of the 29 agree outright** on both fixtures and carry
`"expected_disagreement": null` -- `li1.width.1`, `li1.space.1`, and the
`width`/`space` pair for each of `met1` through `met5` (ten rules). Each
violate fixture trips the corresponding native BEOL rule (e.g.
`met1.width.1` -> `m1.1`, `met2.width.1` -> `m2.1`, `met2.space.1` ->
`m2.2`) and each clean fixture is clean under both engines. Unlike
gf180mcu's `null`s (see below), these sky130 `null`s are *positive*
cross-check results from real runs, not "not cross-checked".

**The remaining seventeen** genuinely disagree on exactly one of their two
fixtures, and each carries a non-null `expected_disagreement` in
`sky130/manifest.json` recording why. These are documented approximations,
not unexplained failures -- the tier-3 test tolerates exactly these and
still fails loudly on any *other* disagreement. They fall into two classes,
and every one of the seventeen belongs to one of them:

**Class A -- the native rule is FEOL-gated (8 rules).** The `violate`
fixture disagrees: the curated engine reports violations, the native deck
reports clean. `sky130A.lydrc` hard-codes `FEOL = false` (its own "do not
change" comment, verified against a real `volare`-fetched install) with no
`-rd`-settable override this engine's invocation can flip, so these rules
sit inside an `if FEOL ... end` block `run_drc_klayout_engine` never
evaluates *at all*, regardless of the input layout.

**Class B -- an isolated single-layer fixture trips unrelated native rules
(9 rules).** The `clean` fixture disagrees: the curated engine reports
clean, the native deck reports violations. A bare, isolated shape is the
only geometry this single-rule, single-layer pilot can build, and it trips
native rules that have nothing to do with the transcribed width/space
threshold -- exact-size (min *and* max) via checks such as `via.1a`, and
enclosure rules requiring a landing pad or a covering layer this fixture
never draws. No fixture can satisfy both engines' notion of "clean" at
once; reconciling them needs a realistic multi-layer stack, out of scope for
this pilot's geometry.

| Rule id | Class | Fixture that disagrees | Curated | Native `sky130A.lydrc` | Why (abridged -- full text in the manifest) |
|---|---|---|---|---|---|
| `diff.width.1` | A | `violate` | violations | clean | `difftap.1` sits inside the `if FEOL ... end` block the native run never evaluates. |
| `poly.width.1` | A | `violate` | violations | clean | Same `FEOL = false` gate as `diff.width.1`. |
| `nwell.width.1` | A | `violate` | violations | clean | Same `FEOL = false` gate -- `nwell.1a` also lives inside the script's `if FEOL ... end` block. |
| `tap.width.1` | A | `violate` | violations | clean | Same `FEOL = false` gate -- `difftap.1`, the single native rule this tap-side half transcribes, is the *same* gated rule `diff.width.1` halves (issue #2343). |
| `capm.width.1` | A | `violate` | violations | clean | `sky130A_mr.drc`'s whole CAPM/CAP2M section (`capm.*`/`cap2m.*`) is inside the same top-level `if FEOL ... end` block. |
| `capm.space.1` | A | `violate` | violations | clean | Same FEOL-gated CAPM/CAP2M section as `capm.width.1`. |
| `capm2.width.1` | A | `violate` | violations | clean | Same FEOL-gated CAPM/CAP2M section as `capm.width.1`. |
| `capm2.space.1` | A | `violate` | violations | clean | Same FEOL-gated CAPM/CAP2M section as `capm.width.1`. |
| `mcon.space.1` | B | `clean` | clean | violations (`ct.1`, `ct.4`) | A bare, isolated mcon shape trips `ct.1` (mcon edges must be exactly 0.17um -- this deck has no `mcon.width.1` to size a fixture against) and `ct.4` (mcon must be covered by li), neither of which this pilot's geometry can satisfy. |
| `via.width.1` | B | `clean` | clean | violations (`via.1a`, `m2.via`, `via.4c.5c`) | The native `via.1a` demands an *exact* 0.15um square (`edges.without_length(0.15)`), not a minimum width; any fixture wide enough to pass this engine's min-only `width_check` is by construction wider than the native max. |
| `via.space.1` | B | `clean` | clean | violations (`m2.5`, `m2.via`, `via.1a`, `via.4c.5c`) | Same exact-size `via.1a` mismatch, plus the native deck's met2-enclosure rules that this single-layer fixture never draws. |
| `via2.width.1` | B | `clean` | clean | violations (`via2.1a`, `via2`, `m3.via2`, `via2.5`) | Same exact-size mismatch (`via2.1a`), plus met2/met3 enclosure rules and `via2.5`'s 2-adjacent-edges-relaxed refinement this curated deck does not model. |
| `via2.space.1` | B | `clean` | clean | violations (`via2.1a`, `via2`, `m3.via2`, `via2.5`) | Same as `via2.width.1`. |
| `via3.width.1` | B | `clean` | clean | violations (`via3.1a`, `via3`, `m4.via3`, `via3.5`) | Same exact-size + enclosure mismatch one layer up (`via3.1a`, met3/met4 enclosure, `via3.5`). |
| `via3.space.1` | B | `clean` | clean | violations (`via3.1a`, `via3`, `m4.via3`, `via3.5`) | Same as `via3.width.1`. |
| `via4.width.1` | B | `clean` | clean | violations (`via4.1a`, `via4`, `m5.via4`) | Same exact-size mismatch (`via4.1a`) plus met4/met5 enclosure rules this single-layer fixture never draws a landing pad for. |
| `via4.space.1` | B | `clean` | clean | violations (`via4.1a`, `via4`, `m5.via4`) | Same as `via4.width.1`. |

Note in particular that `diff.width.1`'s disagreement is **not** about the
`difftap = diff.or(tap)` approximation noted in its own docstring (this
pilot's fixtures draw only `diff.drawing`, so that union is exactly
`diff.drawing` and would agree) -- it is the `FEOL = false` gate above,
which suppresses the native check entirely regardless of the input layout.
`nwell.width.1` disagrees for the identical reason. (`nwell.space.1` was
part of this same `FEOL = false`-gated disagreement before issue #1654 --
now that it uses `"isolated"` instead of `"space"`, it has left this
width/space manifest entirely rather than resolving the disagreement.)

`tap.width.1` is worth keeping in mind as the worked example of what a
`null` does and does not mean. Issue #2321 added it carrying
`"expected_disagreement": null` because no `klayout` binary or resolvable
sky130A install was available at the time -- null recorded "not
cross-checked", not a claim of agreement, and the expected FEOL-gated
disagreement was deliberately left unasserted until an environment could
establish it empirically. Issue #2343 is that environment's result: run
against a real `volare`-fetched sky130A (`open_pdks
c6d73a35f524070e85faff4a6a9eef49553ebc2b`) and KLayout 0.30.10, the
`violate` fixture disagrees exactly as predicted, because
`difftap.width(0.15, euclidian).output("difftap.1", ...)` sits inside the
same `if FEOL ... end` block that suppresses
`diff.width.1`/`poly.width.1`/`nwell.width.1`. It therefore now carries a
non-null `expected_disagreement` and appears as a class-A row above.

If you add a rule to `sky130/manifest.json`, add it to the class-A/class-B
table above (or state the new class in prose) in the same change -- the
tier-1 coverage test enforces that the manifest matches the deck, but
nothing enforces that this section matches the manifest.

gf180mcu's 46 rules have no native-*DRC*-deck cross-check verdict either
way (deferred per the scope above -- no single runnable native DRC deck
exists to cross-check against, re-verified unchanged as of issue #904); their
`expected_disagreement` fields are all `null` because none has been
cross-checked, which is **not** a claim of agreement. This is explicitly
noted, not a silent skip: gf180mcu's DRC rules are still validated by tiers 1
and 2 above (coverage + curated-engine self-consistency) on all 46 rules, and
issue #904 additionally cross-checks gf180mcu's *LVS* device-extraction
rules against a real, directly-runnable native deck (see the tier-3 note
above) -- the DRC-side gap and the LVS-side result are two different
questions with two different answers, not one deferred item.
