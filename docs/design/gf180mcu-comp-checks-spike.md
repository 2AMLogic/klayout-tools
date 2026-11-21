# GF180MCU DF.12 / DF.13 / DF.14 -- executable design spike

Issue #2371, part of #2351. Status: spike complete; **production support for DF.12-DF.14
remains outstanding on #2351** (no rule is enabled, no deck history regenerated, nothing
under `src/` changes in this PR).

Deliverables: this note, `tests/helpers/gf180mcu_comp_spike.py` (test-only prototype +
reference harness), `tests/test_gf180mcu_comp_spike.py` (fixtures + differential tests).

## 1. Outcome

- The issue's original algorithm ("violation = active minus tap sized by D, applied to
  every active point") is **measurably rejected**. It disagrees with the pinned upstream
  rule deck on 9 named counterexample fixtures at both DBU scales, plus most boundary probes (section 6): it rejects
  layouts upstream accepts (long active with one end near a tap; the exact boundary
  distance; DF.12 marker exemptions) and accepts layouts upstream rejects (a tap across
  a legal well gap; a tap in a notch of a U-shaped well; a dnwell-only well).
- The recommended replacement, a bounded typed descriptor plus dedicated evaluator that
  is a `klayout.db.Region` translation of the *upstream operation sequence* (section 7),
  **agrees with the real upstream Ruby expressions, executed under `klayout -b`, on every
  fixture, rule, and DBU scale** that was run (section 6: 0 mismatches over 2 x 63 fixture
  instances x 5 rules, compared as symmetric-difference of violation geometry).
- Agreement is with the *upstream deck*, including its quirks (section 5). Quirks are
  recorded as discrepancies, not silently "fixed".

## 2. Pinned sources

| Role | Source | Pin |
|---|---|---|
| Executable reference | `efabless/globalfoundries-pdk-libs-gf180mcu_fd_pv`, `klayout/drc/rule_decks/comp.drc` L294-364 (DF.12, DF.13, DF.14) | `05e7b6adf19edf942969c1c9625f02fd87874f06` |
| Shared derivations | same repo, `main.drc` L183-202 | same commit |
| Layer numbers | same repo, `layers_def.drc` (`get_polygons(...)`) | same commit |
| Numeric spec | `google/gf180mcu-pdk`, `docs/physical_verification/design_manual/tables_clear/14_COMP33_1.csv` rows DF.12-DF.14 | `de3240d` |

All three upstream files were re-read from the pin with `gh api .../contents/...?ref=<sha>`
while writing this note; the layer numbers below and the Ruby excerpts embedded in the
helper were diffed against them (only comments, `logger.info`, and `.forget` memory-release
lines are omitted from the excerpts; every expression is verbatim). Excerpts keep upstream
attribution (Apache-2.0, GlobalFoundries PDK Authors). DRM: DF.12 implant-coverage
requirement; DF.13/DF.14 20 um (3.3 V) / 15 um (5 V/6 V). The numbers are cross-checked
against the CSV; the spike uses the same 20/15 values as the upstream deck.

Layer map used (GDS layer/datatype, from `layers_def.drc`): comp 22/0, dnwell 12/0,
nwell 21/0, lvpwell 204/0, dualgate 55/0, nplus 32/0, pplus 31/0, schottky_diode 241/0,
res_mk 110/5, v5_xtor 112/1.

## 3. Exact upstream region derivations

From `main.drc` (`get_polygons` merges each layer in flat mode):

```
dnwell_n  = dnwell.not(lvpwell)
all_nwell = dnwell_n.join(nwell)
ncomp     = comp.and(nplus)             pcomp = comp.and(pplus)
nactive   = ncomp.not(all_nwell)        pactive = pcomp.and(all_nwell)
ptap      = pcomp.not(all_nwell).not(res_mk)
ntap      = ncomp.and(all_nwell).not(res_mk)
```

So "active" is *not* the issue's simplified `COMP & implant & Nwell`: the well is
`all_nwell` (Nwell plus DNWELL not under LVPWELL), taps exclude resistor-marker
geometry (`res_mk`), and substrate-domain active (`nactive`) is NCOMP outside
`all_nwell`.

### DF.12
```
comp.not_interacting(schottky_diode).not(nplus).not(pplus)
```
Whole COMP polygons that interact with (overlap *or touch*) the Schottky marker are
removed first; the remainder minus Nplus minus Pplus is reported. Partial coverage
reports only the uncovered part; joint Nplus/Pplus coverage is clean.

### DF.13 (Nwell tap, "inside")
```
sized = ntap; repeat 0.5 um steps up to 15 um / 20 um:
    sized = sized.sized(0.5.um, octagon_limit).and(nwell)    # clipped EVERY step
LV: pactive.not_interacting(v5_xtor).not_interacting(dualgate).not_interacting(sized_by20)
MV: pactive.overlapping(dualgate).not_interacting(sized_by15)
```
Violation = whole PCOMP-in-well *polygons* that do not interact with the progressively
well-clipped octagon-grown tap region.

### DF.14 (substrate tap, "outside")
```
LV: cand = nactive.not_interacting(v5_xtor).not_interacting(dualgate)
                  .not_interacting(ptap.sized(20.um, diamond_limit))
    good = cand.sep(ptap, 20.um).polygons
    violation = cand.not_interacting(good)
MV: same with nactive.overlapping(dualgate) and 15.um
```
A diamond-sized candidate filter, then a Euclidean `sep` (separation) refinement; selection
is again of whole polygons.

### LV / MV selection
LV = `not_interacting(v5_xtor)` and `not_interacting(dualgate)`; MV = `overlapping(dualgate)`
(area overlap, not touch). The two are not complements (section 5, D3).

## 4. Distance / DBU semantics

- Distances are physical (um) in the DRM; the evaluator converts with
  `round(D_um / layout.dbu)`. Both scales tested (1 nm and 0.5 nm DBU) give integral DBU
  for 20 um, 15 um and the 0.5 um step. Production must reject (skip with the existing
  `grid_not_representable` reason, `SKIP_REASON_GRID_NOT_REPRESENTABLE`) any layout DBU for
  which `D/dbu` or `step/dbu` is not an integer, instead of silently rounding the boundary.
- Boundary behaviour is *derived from the reference, not assumed* (section 6, table B):
  - Axial gap, DF.13 and DF.14, LV and MV: legal at D-1 and D (a touch interacts), violating
    at D+1 DBU.
  - Diagonal (3-4-5 corner offset): **DF.14 is strictly Euclidean**: legal at D-1 DBU,
    violating at exactly D (separation refinement requires distance < D). **DF.13 is
    more permissive**: the cumulative octagon sizing keeps the polygon legal at D-1, D and
    D+1 DBU on this diagonal and only flags it by 104% of D. The two rules therefore do not
    share one metric; there is no universal threshold rule to encode.
- Results are identical at 1 nm and 0.5 nm DBU for every fixture (offsets in DBU scale
  with the grid; DBU-relative offsets D-1/D/D+1 flip at the same place).

## 5. Discrepancies (source vs DRM text vs issue proposal)

- **D1. Whole-polygon selection, not point coverage.** DF.13/DF.14 select polygons that do
  not *interact* with the tap reach region; a 40 um active with one end 5 um from a tap is
  legal upstream. The DRM text ("max distance of tap") reads like a per-point rule. The
  original algorithm flags the far 23 um of such a polygon.
- **D2. DF.13 clips with `nwell`, derivations use `all_nwell`.** In a DNWELL-only well
  (no Nwell drawn) the expansion is empty, so a tap 5 um away does not count and the
  PCOMP is flagged (measured). A port clipping with `all_nwell` (the prototype's plain
  `evaluate`) disagrees. The recommended evaluator reproduces upstream; whether to follow
  the quirk or deviate is a deck-policy call to be recorded in the deck's
  known-approximations list (the spike recommends faithful-to-upstream by default).
- **D3. LV/MV partition gaps.** v5_xtor *without* dualgate, and dualgate that only touches
  the active, fall into neither column: a 25 um-remote active is accepted for both LV and
  MV (measured).
- **D4. Well-islands.** Per-step `.and(nwell)` stops growth crossing a legal 0.6 um well gap
  and stops it short-cutting through a notch of a U-shaped well; a one-shot `sized(D)`
  (original) leaks across and accepts both layouts (measured).
- **D5. DF.12 marker exemption** removes the whole COMP polygon when it touches the marker
  anywhere, not just the marker overlap (measured: area subtraction leaves a 2 um sliver
  and, for an edge-touch marker, the whole polygon).
- **D6. Zero taps / empty layers.** Upstream flags active with an empty tap region (sized
  empty -> not interacting). The curated engine's current "absent secondary layer ->
  rule skipped" behaviour would hide exactly this case; see section 8.
- **D7. Scope of comparison.** The Ruby harness runs in upstream's `flat` mode (merged
  polygons). Upstream's `deep`/tiled modes and deep-well *context* beyond the cases below
  (DNWELL-only, DNWELL+Nwell, DNWELL under LVPWELL) were **not executed** and are not
  claimed.

## 6. Experiments (executed)

Environment: macOS, Python 3.14.5, `klayout` Python module 0.30.12 (`klayout.dbcore`),
KLayout application `KLayout 0.30.12` (`/Applications/KLayout/klayout.app/Contents/MacOS/klayout`,
used for the oracle via `klayout -b -r`), pytest 9.1.1, ruff 0.16.9.

Commands (from the issue worktree):

```
uv run python tests/helpers/gf180mcu_comp_spike.py          # matrix dump, 87 s wall
uv run pytest tests/test_gf180mcu_comp_spike.py -q -rs      # 98 passed, 0 skipped, 31.6 s wall
uv run ruff check tests/helpers/gf180mcu_comp_spike.py tests/test_gf180mcu_comp_spike.py \
    tests/test_drc.py tests/test_golden_deck.py             # All checks passed
uv run ruff format --check tests/helpers/gf180mcu_comp_spike.py tests/test_gf180mcu_comp_spike.py
uv run pytest tests/test_drc.py tests/test_golden_deck.py -q   # see section 10
```

(One earlier run of the spike test module took ~10 min wall on a loaded machine; the
steady-state figure is the 31.6 s above. The oracle is one `klayout -b` launch per DBU
scale.)

**Oracle independence.** Expected geometry comes from the verbatim upstream expressions
executed by the real KLayout DRC engine (`run_upstream_batch`). Nothing in the
oracle path calls the candidate evaluator. A second, hand-derived tier of assertions
(geometric reasoning per fixture) runs without a KLayout binary. When no binary is found
(`KLT_KLAYOUT_BIN`, `PATH`, or the macOS app path) the differential tier is **skipped**;
a skip is not agreement.

**In repository CI the differential tier skips.** The `Tests (Python 3.x)` jobs install
only the `klayout` Python wheel, which ships no `klayout` executable, and no workflow
installs the KLayout application or sets `KLT_KLAYOUT_BIN`. CI therefore runs the
hand-derived tier only; the "98 passed, 0 skipped" figure above is from a local run with
the KLayout app installed, and it is the only evidence that the oracle agrees. Making the
differential tier run in CI is step 8 of section 9.

**Fixture matrix** (`build_fixtures(dbu)`, run at DBU 0.001 and 0.0005; "port" =
`evaluate_port_upstream_clip`, "orig" = the original proposal):

| Family | Fixtures | port vs upstream | orig vs upstream |
|---|---|---|---|
| DF.12 coverage | absent implants, full Nplus, partial Nplus, joint N+P, partial marker, marker edge-touch | == | `!=` on partial marker and marker edge-touch |
| Zero/near/remote tap, no active | DF.13 and DF.14 each | == | == |
| Long active, one end near tap | DF.13, DF.14 | == (clean) | `!=` (flags 23 um remainder) |
| Well islands | 0.6 um gap, joined control, U-notch | == | `!=` on gap and U-notch |
| Resistor marker over only tap | DF.13, DF.14 | == (flagged) | == |
| Deep-well context | dnwell only, dnwell+nwell, dnwell under lvpwell | == | `!=` on dnwell-only |
| Voltage selection | none, dualgate, v5 only, dualgate+v5, dualgate edge-touch | == | == |
| Boundary table B | axial/diag x {D-1, D, D+1} x {DF.13, DF.14} x {LV, MV} | == | `!=` everywhere except axial D+1 (it flags the legal axial D-1/D and mis-sizes diagonal cases) |
| Diagonal over-reach | 104% and 110% of D, both rules, both columns | == | == |
| Hierarchy | child cell x2 vs flat twin | == (and upstream hier == flat) | == |

Result: **0 port-vs-upstream mismatches** (`test_port_matches_upstream_geometry_on_every_fixture`,
2 scales). The matrix dump has 110 data rows (fixture/rule pairs where any implementation reports a violation); every row reads `port===`.

Table B detail (measured upstream verdicts; identical at both DBU scales):

| Case | DF.13 LV | DF.13 MV | DF.14 LV | DF.14 MV |
|---|---|---|---|---|
| axial D-1 | legal | legal | legal | legal |
| axial D | legal | legal | legal | legal |
| axial D+1 | violation | violation | violation | violation |
| diag D-1 | legal | legal | legal | legal |
| diag D | legal | legal | **violation** | **violation** |
| diag D+1 | legal | legal | violation | violation |
| diag 104% | violation | violation | violation | violation |
| diag 110% | violation | violation | violation | violation |

The original proposal's failures are reproducible counterexamples, asserted in
`test_original_proposal_is_measurably_rejected` against the upstream results:
`df13_long_active_one_end`, `df14_long_active_one_end`, `df13_island_gap_0p6`,
`df13_u_well_notch`, `df13_lv_axial_d0`, `df14_mv_axial_d0`, `df13_dnwell_only`,
`df12_partial_marker`, `df12_marker_edge_touch`.

Hierarchical vs flat: compared on fixtures where the tap is a sub-cell instance and the
active is in TOP vs a flat twin; Region extraction flattens (`begin_shapes_rec`), upstream
runs in flat mode. Deep mode is not compared (D7).

## 7. Chosen interface

**Recommendation: a bounded typed rule descriptor evaluated by a dedicated evaluator,
carried on `DrcRule` as one new optional field; do not extend `DerivedLayer`.**

Why not `DerivedLayer`: it derives a single *input layer* from `(base, intersect_with, mode)`
and the result is then fed to a generic width/space/enclosure check. DF.13/DF.14 are not an
input-layer derivation: they need the full `all_nwell`/`ntap`/`ptap` derivation chain, an
iterative grow-and-clip loop, a voltage partition, a separation refinement, and
*polygon selection* output. Forcing that into `DerivedLayer` modes would grow it into the
arbitrary region-expression language the issue rules out, and the drawn-layer tuple
inputs (`_rule_input_layer_tuples`) cannot name a derived intermediate.

Prototype (in `tests/helpers/gf180mcu_comp_spike.py`):

```python
CompCoverageRule(rule_id, checked="comp", covered_by_any=("nplus","pplus"),
                 exempt_markers=("schottky_diode",))
TapDistanceRule(rule_id, well_domain="inside"|"outside", voltage="lv"|"mv",
                max_distance_um, step_um=0.5)
evaluate(rule, regions, dbu) -> Region     # empty layers are empty Regions, never skipped
```

Example declarations (the prototype's `RULES`):

```python
"DF.12":    CompCoverageRule("DF.12")
"DF.13_LV": TapDistanceRule("DF.13_LV", "inside",  "lv", 20.0)
"DF.13_MV": TapDistanceRule("DF.13_MV", "inside",  "mv", 15.0)
"DF.14_LV": TapDistanceRule("DF.14_LV", "outside", "lv", 20.0)
"DF.14_MV": TapDistanceRule("DF.14_MV", "outside", "mv", 15.0)
```

`well_domain`/`voltage` select the derivation chain and LV/MV partition (section 3); the
descriptor is validated at construction (unknown layer, empty `covered_by_any`,
non-positive distance/step, distance not a multiple of the step, unknown domain/voltage
-> `ValueError`; tested). Production maps these to `DrcRule(check="comp_coverage" |
"tap_distance", region_spec=<descriptor>)`, keeping one rule list so ids, provenance,
history and coverage work unchanged.

Evaluator fidelity: `evaluate_port_upstream_clip` is the upstream-faithful variant (DF.13
clips with raw `nwell`); `evaluate` clips with `all_nwell`. Production should ship the
faithful variant, with `clip_with` as an explicit descriptor field only if the deck later
chooses to deviate (D2).

## 8. Empty-layer semantics

- Empty implants, taps, wells, markers (`res_mk`, `schottky_diode`, `dualgate`, `v5_xtor`)
  are **empty Regions, never a reason to skip**: zero taps with checked active present is a
  violation (test: `test_empty_taps_never_hide_a_check`; fixtures `df13/14_zero_taps`).
- Absent or empty *checked* region (`comp` absent; derived active empty) is a separate,
  **inapplicable** case: no violation, and not counted in `rules_checked`. Tested by
  `test_absent_checked_active_is_inapplicable` and the `*_no_active` fixtures.
- This deliberately differs from `run_drc`, which skips on any absent `other_layer`
  before recording `rules_checked`.

## 9. Production integration plan (sequenced, for #2351)

Integration points traced at `origin/main` (`18fda2be`): `decks/rules.py` (`DrcRule`,
`DerivedLayer`), `drc.py` (`run_drc` layer resolution and `rules_checked`/`rules_skipped`
bookkeeping, `_rule_vacuity_layers`, `_rule_input_layer_tuples`,
`_rule_outside_voltage_gate`, deck-layer enumeration for `coverage.deck_layers`),
`decks/gf180mcu.py`, `docs/cli/drc.md`, `scripts/generate_deck_history.py`,
`decks/_history.json`, `tests/golden_deck/*`.

1. **Descriptor + validation** (`decks/rules.py`): promote the two descriptors, add
   `DrcRule.check` values `comp_coverage`/`tap_distance` and the `region_spec` field;
   validate at deck load; extend `ALLOWED_CHECKS` and the rule serializer so
   `klt drc rules`/provenance carry the spec. Unit tests for malformed descriptors.
2. **Evaluator** (`drc.py`): a `_run_region_rule` dispatched before generic layer
   resolution. Resolve each named input by drawn layer, missing -> empty Region. Convert
   distances with the layout DBU; non-integral -> skip with
   `SKIP_REASON_GRID_NOT_REPRESENTABLE`. Evaluate the faithful variant (section 7).
3. **Coverage bookkeeping**: `rules_checked` iff `comp` present and derived checked
   region non-empty; otherwise `rules_skipped` with a new reason
   `inapplicable_empty_checked_region` (comp absent keeps `absent_input_layer`). Extend
   `_rule_input_layer_tuples` (all layers the spec names, including those allowed to be
   empty) so `deck_layers` and the layers-in-stream-without-rules report stay correct;
   `_rule_vacuity_layers` returns only the checked layer(s) (comp), so an absent tap
   layer is *not* vacuous (same exclusion pattern as `antenna`).
4. **Voltage warnings**: treat `dualgate`/`v5_xtor` in the spec as marker reads in
   `_rule_outside_voltage_gate` (the rule is scoped by the marker; no "wrong column"
   warning), and record D3 (partition gap) in the deck's known-approximations list.
5. **Reporting**: one violation per selected polygon (bbox + area; whole polygons for
   DF.13/14, residual pieces for DF.12); additive JSON (`check` vocabulary,
   `docs/cli/drc.md`), no change to existing fields.
6. **Rules + provenance** (`decks/gf180mcu.py`): declare the five rules above with
   `_gf180mcu_klayout_deck_provenance("comp", "DF.12" ...)`; document D1-D3 and the
   hierarchical/deep limitation (D7) under "Known approximations".
7. **Golden manifest + history**: add manifest entries (violate/clean pair per rule, incl.
   the zero-tap case) in `tests/golden_deck/generate_golden_deck.py` and regenerate
   `manifest.json`; run `scripts/generate_deck_history.py` to regenerate
   `decks/_history.json` **only in the PR that actually adds the rules**
   (`test_golden_manifest_covers_every_width_space_rule` enforces coverage).
8. **Differential gate in CI**: keep the oracle tests; where a KLayout binary is available
   they must run (not skip) as a release gate. Current CI has no binary (see section 6), so
   this step needs a CI job that installs the KLayout application (or sets
   `KLT_KLAYOUT_BIN`) and fails if the oracle tests skip.

Suggested split: PR-A steps 1-3 + DF.12 (no distance semantics); PR-B DF.13/DF.14 + steps
4-5; PR-C step 7 consolidation if the golden regeneration is noisy. Each PR is
`Part of #2351`.

## 10. Regression checks

`uv run ruff check` on the two new files and `tests/test_drc.py`/`tests/test_golden_deck.py`:
All checks passed. `uv run pytest tests/test_drc.py tests/test_golden_deck.py`:
exit code 0 (3:48 wall); the `-q` output shows no failures and 31 skipped tests in the final tests (the repository's pytest config suppresses the summary line; the skip reasons were not individually inspected, and none are in the new spike module).
