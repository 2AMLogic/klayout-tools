# Proposal: a compiled, provenance-traced, regenerable deck layer

**Status:** research/proposal, no implementation. Filed for the re-scoped
Epic #711 ("extend the shipped `klt deck`") per issue #738. Nothing here adds
a dependency or changes `src/klayout_tools/decks/*.py`, `drc.py`, `lvs.py`,
or `deck_cmd.py`. Recommendation: **narrow go** — build the provenance +
golden-pair validation layer now; do **not** build a native-source-to-`DrcRule`
parser/compiler as the first increment (see "Recommendation" below for why).

## 0. Scope correction carried over from curation

The issue's item 1 asks to characterize "what `klt deck` does today." Read
literally that is `klt deck resolve` (issue #623,
`src/klayout_tools/cli/deck_cmd.py`, documented in
[`docs/cli/deck.md`](../cli/deck.md)) — and it answers a completely different
question than this proposal is about. `klt deck resolve` is a **version
lookup only**: given a `content_hash` or a `(deck, version)` pair, it tells
you which klayout-tools git tag/PyPI release shipped that exact deck byte
content, via a generated table (`decks/_history.json`,
`scripts/generate_deck_history.py`). It never fetches, parses, checks out, or
regenerates rule content — see `deck.md`'s own "Resolve-only, not
fetch-or-build" section. It has nothing to do with DRC/LVS rule content.

The rule *content* this issue is actually asking about — the thing a
"compiled, regenerable" layer would replace or augment — is
`src/klayout_tools/decks/sky130.py` and `gf180mcu.py`: two hand-authored
Python modules, each a `DECK: list[DrcRule]` plus an `EXTRACTION_DECK`
(LVS device-recognition bindings), consumed by `klt drc --deck <name>` and
`klt lvs`. `klt deck resolve` and these two files happen to share the word
"deck" and live in the same package; they are otherwise unrelated
capabilities. The rest of this document is about `sky130.py`/`gf180mcu.py`,
never `klt deck resolve`.

## 1. What the hand-curated decks do today

**Size, as of `e0bdcda` (2026-08-11):**

| Deck | File | Lines | Rules (`DrcRule` entries) | Check kinds used |
| --- | --- | --- | --- | --- |
| sky130 | `decks/sky130.py` | 1,005 | 17 | `width` (6), `space` (5), `enclosing` (6) |
| gf180mcu | `decks/gf180mcu.py` | 1,489 | 42 | `width` (12), `space` (14), `enclosing` (15), `separation` (1) |

Both decks explicitly self-describe as "curated starter subset[s], not the
full [official] design rule manual (which spans hundreds of rules)" — this is
stated in each module's own docstring and repeated in
[`docs/cli/drc.md`](../cli/drc.md)'s "Coverage" section, not a claim this
proposal is introducing. Every rule in both files carries a docstring/comment
citing its exact source: for sky130, the rule id and one-line comment from a
live `.lydrc` script line (e.g. `poly.1a`); for gf180mcu, the DRM section
number, CSV table, and rule id (e.g. `DF.3a`, "7.5 Comp"). That per-rule
prose citation is real provenance — it is just not machine-readable today
(see §3).

**`DrcRule`'s vocabulary is deliberately narrow** (`decks/__init__.py`):
single-layer checks are `width`/`space`/`notch`; two-layer checks are
`separation`/`enclosing`/`enclosed`/`overlap`. That is the complete list —
confirmed against `drc.py`'s `_SINGLE_LAYER_CHECKS = {"width", "space",
"notch"}` / `_TWO_LAYER_CHECKS = {"separation", "enclosing", "enclosed",
"overlap"}`. **There is no `area`, `density`, or `antenna` check kind at
all**, in either the vocabulary or the engine that runs it. This matters
directly for §5's rule-class recommendation.

**Two cross-check mechanisms already exist**, both worth knowing before
proposing a third:

1. **`--engine klayout` (issue #565).** `klt drc --engine klayout` wraps the
   real, standalone `klayout` binary and runs the PDK's *actual* native
   `.lydrc`/`.drc` script as a subprocess, parsing its `.lyrdb` report into
   the same `violations[]` shape as the curated engine. This is a genuine
   ground-truth oracle, already shipped — but it runs a whole layout through
   the whole native deck; it does not (today) run *per-rule* comparisons, and
   its own documented limitation is `coverage` is always empty (an external
   deck's rule set is opaque to it) — see `drc.md`'s "Engine" section.
   `sky130A.lydrc` resolves cleanly (a single self-contained file); gf180mcu's
   native deck ships as topic fragments under `drc/rule_decks/*.drc` with a
   Python assembly wrapper (`run_drc.py`) and has **no single ready-to-run
   file** the resolver can find without driving that wrapper's own bespoke
   CLI — documented as an explicit, unresolved gap in `drc.md`, not a
   hypothetical this proposal is raising for the first time.
2. **Ad hoc golden violate/pass test pairs already exist, per rule, informally.**
   `tests/test_drc.py` (2,899 lines) already contains a `_violation`/`_clean`
   test pair for most individual rules (e.g.
   `test_run_drc_gf180mcu_metal2_width_violation` /
   `test_run_drc_gf180mcu_metal2_clean`), each hand-building a tiny layout
   that trips or clears exactly one rule. This is precisely the "golden
   violate/pass pairs per rule" item 2 of the issue asks about — it already
   exists, just as unstructured Python test functions rather than a
   declarative, rule-keyed manifest, and with no enforcement that *every*
   `DECK` entry has one (some rules only have a `_violation` test, not both;
   confirmed by scanning the file's `def test_` list).

## 2. How the three PDKs' rule sources are actually structured

This is where "regenerable, compiled from source" runs into real
heterogeneity — the three PDKs this repo targets do not ship the same kind
of machine-readable source at all:

| PDK | Native DRC form | Machine-parseable today? | LVS/device-recognition form |
| --- | --- | --- | --- |
| sky130 | One self-contained `sky130A.lydrc` (KLayout's Ruby-embedded DRC-DSL) from `fossi-foundation/open-pdks`, plus `sky130.lyt` for the layer map | In principle yes (single file, `--engine klayout` already runs it directly) — but it is a general-purpose scripting language (Ruby), not a declarative rule table; a real parser would need to handle arbitrary control flow, not just `.output(...)` statement extraction | Separate repo (`efabless/sky130_klayout_pdk`, `sky130.lvs`) |
| gf180mcu | **No single runnable deck as of the `999a6ff` snapshot cited in `gf180mcu.py`'s own provenance note** — fragments under `klayout/drc/rule_decks/*.drc` (per-feature files: `drc_bjt.drc`, `dualgate.drc`, ...) plus a Python assembly CLI (`run_drc.py`) that concatenates them at invocation time. The core FEOL/BEOL width/space/enclosure rules this deck curates are **not present as executable `.output(...)` statements** in that snapshot at all — they were transcribed from the published DRM's numeric CSV tables instead (`tables_clear/*.csv`, e.g. `14_COMP33_1.csv`) | The DRM tables (CSV) are the *more* parseable source here, ironically — but the executable deck is not | `globalfoundries-pdk-libs-gf180mcu_fd_pv`'s `klayout/lvs/*.lvs` (a different repo again) |
| SG13G2 (IHP-Open-PDK) | No deck exists in this repo yet. Issue #524 (curating a hand-transcribed SG13G2 DRC/LVS deck, the same way sky130/gf180mcu were built) is still **open**, carrying `loom:operator-only` + `loom:curated`, not merged — `decks/sg13g2.py` does not exist as of this proposal (`find src/klayout_tools/decks/` confirms only `sky130.py`/`gf180mcu.py`). The only SG13G2 artifact that has landed is `scripts/fetch-ihp-sg13g2.sh` (issue #522/#525) — a PDK-install *fetcher*, not a rule source | N/A yet | N/A yet |

Three consequences follow directly from this table:

- **A single "native deck parser" does not generalize across the two PDKs
  that exist today**, let alone a third. sky130's source is one file in a
  general-purpose scripting language; gf180mcu's source is CSV tables plus a
  *different*, non-executable-at-transcription-time fragment set. A compiler
  aimed at "parse the PDK's own deck and regenerate `DrcRule` entries" would
  need two structurally different front ends for the two PDKs this repo
  already supports, and a third, entirely unknown shape for SG13G2 once #524
  lands (IHP-Open-PDK's KLayout deck shape has not been surveyed in this
  repo at all — out of scope to newly survey it live for this proposal, since
  #524 itself, which would do that survey as a side effect, has not merged).
- **SG13G2 cannot be used as this proposal's third data point yet.** Issue
  #738's own item 1 asks this proposal to read "the SG13G2 work from issue
  #524" — but that work has not landed on `origin/main` (#524 is still open,
  unbuilt, and itself has a documented Champion-rejection history over being
  too large to build in one pass — see its own comment thread). This
  proposal treats SG13G2 as a **named future third case**, not a second
  concrete example alongside sky130/gf180mcu; the "how PDK rule sources are
  structured" survey above is two data points, not three, until #524 merges.
- **The published DRM tables (gf180mcu's CSVs) are the one source shape in
  this survey that is genuinely mechanical to compile against** — a flat
  numeric table keyed by rule id is a far easier regeneration target than a
  general-purpose DRC-DSL script. If a native-source compiler is ever built,
  gf180mcu's CSV tables — not sky130's `.lydrc` — are the natural pilot,
  which cuts directly against reading the issue's rule-class ordering
  (width/spacing/enclosure/area/density/antenna) as implying "start with
  sky130" or "start wherever coverage is thinnest."

## 3. What a compiled/provenance-traced/regenerable layer would add — and what it wouldn't

Weighing each of the issue's three named benefits against what already
exists (§1):

- **Golden violate/pass pairs per rule.** Real gap, but a *formalization*
  gap, not a from-scratch one: the pairs already exist as ad hoc pytest
  functions; what's missing is (a) a declarative, rule-id-keyed manifest so
  "does every `DECK` entry have both a violate and a clean fixture" is a
  mechanical check rather than a manual audit of a 2,899-line test file, and
  (b) a shared fixture format the two engines (curated + `--engine klayout`)
  can both consume, enabling §3's third item below.
- **Cross-check against the existing deck on the #520 corpus.** The #520
  corpus (Epic: "the Tiny Tapeout corpus as a regression and optimization
  benchmark") is itself still an open, unbuilt epic — Champion has rejected
  it three times over missing phase decomposition and is currently escalated
  to the operator (`gh issue view 520`, checked live for this proposal). A
  cross-check plan can *target* that corpus once it exists, but nothing in
  this repo can run it today; treat this as a forward-looking validation
  target, not a data source available now. The mechanism that *would* run
  such a cross-check already exists in miniature: `--engine klayout` running
  the same corpus layout through both the curated deck and the PDK's native
  deck, diffing violation counts per rule id. That is a real, buildable
  regression harness independent of #520 landing — it can run today against
  `tests/corpus/sky130/` and `tests/corpus/gf180mcu/` (already checked into
  this repo, four+three cells respectively), and should not wait on #520.
- **Each rule tracing back to its PDK-source line.** This is the one item
  that is genuinely absent today, not just informal. Every rule's provenance
  is prose in a comment — real, specific, and re-verifiable by a human
  (confirmed by reading both files: every rule cites a rule id, a source
  file, and often a commit hash), but not queryable. `klt drc`'s JSON
  contract has no field surfacing it; a caller (or a coverage-audit script)
  cannot ask "which rules trace to `open-pdks` commit X" without grepping
  Python source comments by hand.

**What a native-source-parsing compiler would *not* solve**, based on
reading both decks' own extensive "approximation" notes (§4): the actual
ceiling on deck coverage today is not transcription effort, it is
**check-primitive expressiveness**. Both `sky130.py` and `gf180mcu.py`
document, rule by rule, cases where the *official* rule cannot be
represented at all in `DrcRule`'s vocabulary — not "we haven't gotten to it
yet."

## 4. The real bottleneck: checker vocabulary, not source transcription

Both deck modules' docstrings catalogue, in detail, categories of official
rules that are structurally inexpressible in today's `(layer, other_layer,
check, threshold)` vocabulary, independent of how the source is obtained:

- **Compound/boolean layer expressions.** sky130's `difftap.1` is really
  `diff.or(tap)`; gf180mcu's `poly2.width.1` gate-vs-general split is really
  `poly2 AND comp`. `Region` check primitives here run against one drawn
  layer or one pair, never an arbitrary boolean expression.
- **Connectivity/netlist-context splits.** gf180mcu's `NW.2a`/`NW.2b`
  Nwell-spacing rule depends on whether two wells are at the *same*
  electrical potential — a netlist question this purely-geometric engine
  cannot answer, so only the looser bound is enforced. `BJT.3`'s "unrelated
  COMP" exclusion is the same shape.
- **Array-density context.** gf180mcu's `Vn.2a`/`Vn.2b` via-spacing rule
  tightens inside a >=4x4 via array; `space_check` has no array-density
  parameter.
- **Min-and-max bound rules.** `CO.1`/`Vn.1` specify vias as a fixed square
  (min *and* max), but `width_check` only enforces a lower bound.
- **No `area`/`density`/`antenna` check kind exists at all** (§1) — sky130's
  `m2.6` (minimum met2 area) is a real, cited, un-transcribed rule for
  exactly this reason, called out explicitly in `sky130.py`'s own docstring
  as "no `\"area\"` check primitive exists... out of scope... see #513 for a
  candidate follow-on."

Every one of these is an **engine-vocabulary gap**, independent of whether
the rule that hits it was hand-transcribed or mechanically parsed from a
`.lydrc` file. A compiler that ingests `sky130A.lydrc` verbatim would either
(a) silently drop exactly the same rules the hand-curated deck already
drops, for exactly the same reason, or (b) require first extending `DrcRule`
and `drc.py`'s check dispatch with new primitives (area/density/antenna
checks, compound layer boolean expressions, a connectivity-aware check
variant) — which is deck-independent engine work that benefits hand-curated
decks exactly as much as a compiled one, and would need to land *before* a
compiler could regenerate any rule in these categories anyway.

## 5. Recommendation: narrow go

**Do not build a native-source-to-`DrcRule` parser/compiler as the first
increment.** Two of the three target PDKs have source shapes (`.lydrc`
general-purpose script; fragmented `.drc` files with no single
executable deck) that don't share a front end, the third (SG13G2) hasn't
even had its source shape surveyed yet (#524 unmerged), and — per §4 — the
actual ceiling on rule coverage is check-primitive expressiveness, which a
parser does nothing to raise. Building a compiler now would either
regenerate the same 59 rules that already exist (no net gain over hand
curation) or silently hit the identical "can't express this" wall the
curated decks already documented, for the same underlying reason.

**Do build, now, the two items that are genuinely missing and don't depend
on solving §4:**

1. **A declarative, rule-id-keyed golden-pair manifest.** Formalize the
   violate/pass pairs that already exist informally in `test_drc.py` into a
   structured fixture set (one violate + one clean tiny layout per `DrcRule`
   id, keyed by id) with a coverage check: every `DECK` entry must have both.
   This costs nothing new conceptually — it's promoting existing ad hoc test
   functions to a first-class, auditable artifact — and immediately answers
   "which of our 59 rules currently lack a negative control," a real,
   present gap (confirmed: not every rule in `test_drc.py` has both a
   `_violation` and a `_clean` test today).
2. **A machine-readable provenance field**, alongside the existing prose
   citation each rule already carries: `DrcRule.provenance` (or similar) —
   source repo, path, commit/citation, and official rule id — populated from
   the same information already in each rule's comment. This turns "does
   this rule trace to its PDK-source line" from a human grep into a
   queryable field, and is a mechanical, low-risk addition (a new optional
   dataclass field, backfilled per existing rule) independent of any parser.

**Use `--engine klayout` (already shipped) as the cross-check oracle for
both**, rather than building a third comparison mechanism: for each golden
pair from item 1, run the fixture through both the curated engine and
`--engine klayout` against the PDK's real native deck (sky130 today; gf180mcu
once its `run_drc.py` assembly gap is separately resolved — see `drc.md`'s
own noted limitation) and assert agreement on violate/clean status. This
delivers the issue's "cross-check against the existing deck" goal without a
parser, and is exactly the harness that would later validate a compiler's
output *if* one is ever built, once §4's vocabulary gaps are closed enough
to make that worthwhile.

## 6. First rule class: width/spacing (retrofit, not new coverage)

Per the issue's candidate list (width/spacing/enclosure/area/density/antenna),
start with **width and spacing** — not because they're the highest-value gap,
but because they're the only classes in that list requiring **zero new check
primitives** (§1: `width`/`space` are both already fully supported,
unlike `area`/`density`/`antenna`, which don't exist as check kinds at all,
and unlike `enclosing`, which already has a documented false-negative
carve-out for zero-overlap escapes that a golden-pair harness should also
exercise but is more subtle to get right first). This makes width/spacing the
right **pilot** for the golden-pair manifest and provenance field from §5:
validate the schema and the `--engine klayout` cross-check harness against
rules that already work end-to-end (18 width rules + 19 space rules across
both decks — sky130: 6 width, 5 space; gf180mcu: 12 width, 14 space — 37 of
the 59 total) before extending either the manifest or the harness to check
kinds that don't exist yet.

**Golden-pair validation plan for this slice:**

1. For each of the 37 width/space `DrcRule` entries in `sky130.py` and
   `gf180mcu.py`, author (or promote from `test_drc.py`, where one already
   exists) a minimal GDS/OASIS fixture pair: one layout that trips the rule
   by exactly the smallest documented margin, one that clears it — mirroring
   the existing `_violation`/`_clean` naming convention already in use.
2. Store the pair keyed by rule id in a manifest (e.g.
   `tests/golden_deck/<deck>/<rule_id>.{violate,clean}.gds` +  a small JSON
   index), not scattered as same-named Python functions, so a coverage
   script can assert `len(manifest) == len(DECK)` per deck restricted to
   width/space rules.
3. Run each pair through the curated engine (`run_drc`) and assert the
   expected violate/clean status — this is the existing regression-test
   behavior, just re-homed onto the manifest.
4. Run each pair through `--engine klayout` against the real
   `sky130A.lydrc` (gf180mcu deferred until its native-deck assembly gap is
   resolved — see §5) and assert the same violate/clean status agrees
   between engines. Disagreement is either a bug in the curated
   transcription (fix the rule) or a documented, intentional approximation
   (§4) — in the latter case the manifest entry should carry a
   `expected_disagreement: "<reason, cross-ref to the rule's docstring>"`
   flag rather than silently failing, since several width/space
   approximations are already known and accepted (e.g. sky130's
   `difftap.1`).
5. Report agreement as a measurable summary (`N/37 rules verified against
   the native deck, M documented approximations, 0 unexplained
   disagreements`) — the concrete, falsifiable "measure agreement with the
   current decks" criterion the issue's Definition of Done asks for.

Only after this pilot proves out the manifest schema and the
`--engine klayout` cross-check mechanism should enclosure rules (next
simplest — already supported, but with the zero-overlap-escape subtlety
noted in `drc.md`) be folded in, followed by a *separate*, explicitly-scoped
follow-on to add `area`/`density`/`antenna` check primitives to `DrcRule`
and `drc.py` before any golden pairs for those classes can be authored at
all.

## Out of scope for this proposal

- Building the golden-pair manifest, the provenance field, or any new check
  primitive — this is a research/recommendation document, not the
  implementation.
- Surveying IHP-Open-PDK/SG13G2's native DRC deck shape — that belongs to
  #524 landing first (or a dedicated follow-on), not to this proposal
  inventing it secondhand.
- Resolving gf180mcu's `--engine klayout` "no single runnable deck" gap
  (`run_drc.py` assembly) — a real, separately-scoped prerequisite for
  extending §5's cross-check harness to gf180mcu, already documented as a
  known limitation in `drc.md`, not new information from this proposal.
- Waiting on or building Epic #520's Tiny Tapeout corpus harness — noted as
  a forward-looking validation target in §3, not a blocker for the
  recommendation above.

## Linked from

- Epic #711 (parent, re-scoped: "extend the shipped `klt deck`")
- Issue #738 (this proposal's own tracking issue)
- Issue #524 (SG13G2 deck, still open — cited in §2 as the reason SG13G2 is
  a named future case, not a third surveyed data point)
- Issue #520 (Tiny Tapeout corpus epic, still open/escalated — cited in §3
  as the forward-looking cross-check target)
- Issue #565 (`--engine klayout`, the cross-check oracle this proposal reuses
  rather than duplicating)
