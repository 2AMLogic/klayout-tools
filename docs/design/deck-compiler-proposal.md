# Survey & proposal: what a regenerable deck layer adds over the shipped `klt deck`

**Status:** research / proposal, no implementation. Filed for issue #738, a
research-and-propose task under the re-scoped
[Epic #711](https://github.com/2AMLogic/klayout-tools/issues/711) ("DRC/LVS
deck compiler for klt"). Epic #711's own most recent comment re-scopes it
away from a parallel deck-compiler build and toward: "research how PDK rule
sources are structured across sky130 / gf180mcu / SG13G2 (#524) and what a
*regenerable, provenance-traced* deck path would add over the current
hand-curated decks... Research-first: come back with fresh eyes on what the
shipped `klt deck` does today and where a compilation/generation layer earns
its place, measurable against the current decks." This document is that
input. It does **not** authorize implementation of anything below.

**Evidence tiers**, following `docs/design/place-and-route-improvements-survey.md`'s
convention (itself citing `docs/design-evidence-tiers.md`'s broader ladder):

- **[REPO]** — read directly from this repo's own source/docs, cited by file
  and line/section.
- **[UPSTREAM]** — read directly from the cited upstream PDK repository's own
  files (rule-source structure claims), cited by path.
- **[PROPOSAL]** — this document's own reasoning/recommendation, not a claim
  about the world.

All `[REPO]` file citations below are verified against this worktree at
commit `e0bdcda` (2026-08-11); re-verify before acting on any of them if this
document is read materially later, per the "re-verify date-stamped facts"
discipline.

## 0. Terminology correction (load-bearing — read first)

**`klt deck` (issue #623, `src/klayout_tools/cli/deck_cmd.py`) is not the
rule-deck content this issue is about.** **[REPO]** Per
[`docs/cli/deck.md`](../cli/deck.md), `klt deck resolve` is a **version
resolver**: given a deck content hash or a `(name, version)` pair, it answers
"which klayout-tools git tag/PyPI release shipped this exact deck content" by
looking the query up in a generated table
(`src/klayout_tools/decks/_history.json`, rebuilt by
`scripts/generate_deck_history.py` from this repo's own tag history). It
"never clones, checks out, or builds a historical klayout-tools revision
in-process" (`docs/cli/deck.md`, "Resolve-only, not fetch-or-build"). It has
no opinion on rule content and nothing to say about PDK rule sources.

The actual hand-curated DRC/LVS rule decks — the thing epic #711 and this
issue are asking whether to replace with a compiler — live in
`src/klayout_tools/decks/sky130.py` and `src/klayout_tools/decks/gf180mcu.py`,
consumed by `klt drc` (`drc_cmd.py` → `drc.py::run_drc`) and `klt lvs`
(`lvs_cmd.py`). A third deck, for SG13G2 (IHP-Open-PDK), is scoped by #524
but **does not exist yet** — #524 is open, unimplemented, carrying
`loom:operator-only`/`loom:operator-decision`/`loom:curated` as of this
writing; `grep -r sg13g2 src/klayout_tools/decks/` returns nothing. The rest
of this document is about those two (soon, if #524 ships, three) `decks/*.py`
modules, not about `klt deck resolve`.

## 1. What the two shipped decks do today

**[REPO]** Both `sky130.py` (1005 lines, module docstring + `DECK: list[DrcRule]`
+ `EXTRACTION_DECK`) and `gf180mcu.py` (1489 lines, same shape) are **hand-transcribed
Python literals**: each `DrcRule` names a rule id, one or two `(layer, datatype)`
pairs, a `check` kind, and a `threshold_dbu`, with a comment or docstring citing
the exact upstream rule id, source file, and (where available) the source line's
own text. `ExtractionDeck` entries (MOS/bipolar/capacitor/resistor device
recognition, for `klt lvs`) follow the same "one Python object per device,
cited to its upstream derivation" shape.

**Rule-vocabulary ceiling.** `DrcRule.check` supports exactly seven check
kinds — `width`/`space`/`notch` (single-layer) and
`separation`/`enclosing`/`enclosed`/`overlap` (two-layer) — mapping 1:1 onto
`klayout.db.Region`'s own native check primitives
(`src/klayout_tools/drc.py:60-62`, `_SINGLE_LAYER_CHECKS`/`_TWO_LAYER_CHECKS`).
**There is no `"area"` check kind and no `"density"` check kind anywhere in
the engine today.** This is not an oversight the deck authors missed — it is
called out explicitly and repeatedly in both decks' own provenance notes: the
official sky130 deck's `m2.6` (minimum met2 area, 0.0676 µm²) is "**not**
transcribed: no `"area"` check primitive exists anywhere in this engine's
`DrcRule` vocabulary" (`sky130.py:170-179`, echoed in
[`docs/cli/drc.md`](../cli/drc.md) "Coverage"); gf180mcu's array-density-scoped
via-spacing splits (`Vn.2a`/`Vn.2b`) are approximated to their less-strict
uniform bound because "our `space_check` primitive has no array-density
context" (`gf180mcu.py:177-181`, and four further "no array-density" notes at
lines 453/509/555/603). **Nothing in either deck models antenna ratios** — a
repo-wide `grep -rn antenna src/` finds zero hits outside two unrelated design
docs (`em-field-sim-spike.md`, `lambdalib-survey.md`). Antenna is not an
oversight either: it is the explicit subject of a **separate, already-scoped
epic**, #713 ("antenna + ERC signoff for klt"), whose own "reality-grounding
discipline" section proposes exactly the golden-pair/PDK-sourced/corpus-
cross-checked discipline this issue asks about, scoped to antenna
specifically. **This proposal's rule-class recommendation therefore excludes
antenna as out of scope — it is already owned elsewhere**, and treats "area"
and "density" as blocked on a check-primitive addition neither deck currently
has, regardless of whether the rule table backing it is hand-curated or
compiled.

**Coverage today**, per `docs/cli/drc.md` "Coverage" and a direct count
(`grep -c 'DrcRule(' src/klayout_tools/decks/{sky130,gf180mcu}.py`):

| Deck | Rule count | Check kinds present | Explicitly-declined rule classes |
|---|---|---|---|
| sky130 | 17 | width, space, enclosing | area (`m2.6`), compound-layer-expression rules (2), max-size/periphery-scoped refinements (4) |
| gf180mcu | 42 | width, space, enclosing, overlap | array-density-scoped spacing splits (per-note approximations), 5V/6V (Dualgate) threshold variants |

Both decks' own docstrings describe themselves as a **"curated starter
subset,"** not the full design rule manual — sky130's official deck spans
"hundreds of rules" (`docs/cli/drc.md:198-199`); the curated deck covers 17.

## 2. How the three PDKs' rule sources are actually structured

This is the crux of the "regenerable deck path" question: a compiler's
feasibility and cost depend entirely on whether its input is **structured
data** (a table a parser can walk deterministically) or a **DSL script**
(a general-purpose rule-deck language that must be interpreted, not merely
parsed, to know what a rule means).

### sky130 — DSL script, no structured table

**[UPSTREAM], cited via `sky130.py`'s own module docstring (lines 1–23):**
sky130's DRC rules are transcribed from
`fossi-foundation/open-pdks`'s `sky130/klayout/sky130.lydrc` — a **KLayout
DRC-DSL script** (a Ruby-embedded domain-specific language executed by
KLayout's own script runner), plus `sky130.lyt` for the layer-map. This is
the same file `klt drc --engine klayout` already runs directly and
unmodified as a subprocess (`docs/cli/drc.md` "Engine" → `"klayout"`,
issue #565) — see §3 below. A `.lydrc` script is executable code with
control flow (`not_in_cell5_li`, `non_huge_m1`, edge-set operations like
`first_edges`/`second_edges`/`extended_in`), not a data table; several rules
this deck could not transcribe exactly are described as depending on
"per-edge-pair conditionals" or "compound layer expressions... our engine
does not evaluate" (`sky130.py:139-145`, `docs/cli/drc.md:214-231`). There is
no separate machine-readable rule table alongside the DSL script for sky130.

### gf180mcu — partially structured (CSV tables) + DSL fragments

**[UPSTREAM], cited via `gf180mcu.py`'s own module docstring (lines 1–70):**
gf180mcu is the one PDK of the three with a genuinely structured numeric
source: `google/gf180mcu-pdk`'s Design Rule Manual publishes its rule tables
as **CSV files** under `docs/physical_verification/design_manual/tables_clear/`
(e.g. `16_Poly2_42.csv`, `21_Contact_56.csv`, `22_Metaln_58.csv`), one CSV per
DRM section, each row a rule id + numeric threshold. Most of the 42 curated
rules cite these CSVs directly by table and rule id (`"DF.1a"`, `"PL.1"`,
`"Mn.1"`, ...). **But not all of it**: the nine conductor-over-cut enclosure
rules (contact/via family, issue #551) were instead re-derived from
`libs.tech/klayout/drc/rule_decks/{contact,via1,...}.drc` — a **separate**
DSL-fragment source, because the companion KLayout-runnable deck repo's own
per-feature rule files "cover specialised devices, not the core FEOL/BEOL
width/space/enclosure checks this deck curates — those aren't present as
executable `.output(...)` statements in this snapshot of that repo"
(`gf180mcu.py:74-88`; `docs/cli/drc.md:270-278`). And `--engine klayout`
cannot run gf180mcu's native deck at all today: it ships as "~60 topic
fragments under `drc/rule_decks/*.drc` plus a Python assembly/CLI wrapper
(`drc/run_drc.py`)" with its own bespoke CLI contract that
`pdk.drc_deck_file()` "deliberately does not attempt to drive generically"
(`src/klayout_tools/pdk.py:403-416`) — it returns `None` for this variant
shape, so gf180mcu has no dual-engine cross-check available today (§3).

### SG13G2 — DSL script, same category as sky130, deck doesn't exist yet

**[UPSTREAM], per #524's own scope description:** SG13G2 (IHP-Open-PDK)
ships its DRC ruleset as `ihp-sg13g2/libs.tech/klayout/tech/drc/ihp-sg13g2.drc`
— a KLayout DRC-DSL script, the same category as sky130, not gf180mcu's
partially-CSV shape. #524 explicitly states there is "currently no mechanism
to execute it" and scopes a hand-curated `klayout_tools/decks/sg13g2.py`
module "comparable in size to the existing sky130/gf180mcu decks... each
needing verification against the real PDK's own documented behavior" — i.e.
the same hand-transcription labor this issue is asking whether to replace.
As of this writing #524 has not been built.

### What this means for "compile from a machine-readable rule source"

**[PROPOSAL]** Two of the three PDKs' rule sources are DSL scripts, not
structured data. "Compiling" from a `.lydrc`/`.drc` script is not a table
walk — it requires either (a) writing and validating a DRC-DSL
parser/interpreter capable of resolving the language's control flow
(named-cell exclusions, compound layer expressions, edge-set operations) well
enough to emit correct `DrcRule` entries, which is a large undertaking
whose correctness is exactly as hard to validate as the thing it replaces, or
(b) falling back to executing the DSL script as-is (which `klt drc --engine
klayout` already does, per #565 — not a compilation step at all, see §3).
gf180mcu is the **only** PDK of the three with a source format (CSV numeric
tables) a straightforward parser can walk deterministically without
interpreting a general-purpose language — and even there, one rule family
(conductor-over-cut enclosure) falls outside the CSVs and back into DSL
territory.

## 3. What already exists today that a compiler would seem to add

The epic's three promised properties — golden violate/pass pairs, corpus
cross-check against the hand deck, and per-rule source-line provenance — are
worth checking against what already exists, because a compiler's marginal
value is only the *gap*, not the whole feature.

### 3.1 Golden violate/pass pairs: substantially present, informally

**[REPO]** `tests/test_drc.py` (2899 lines) already contains
per-rule-id violate/clean test pairs for a majority of both decks' rules —
e.g. for sky130: `test_run_drc_sky130_li1_enclosing_licon1_violation` +
`_clean` + `_flush_edges_clean` (lines 2122–2174), `test_run_drc_sky130_met2_width_violation`
+ `test_run_drc_sky130_met2_clean` (2546–2604), `via_width`/`via_space`
violation+clean pairs (2605–2670), `met1_enclosing_via`/`met2_enclosing_via`
pairs (2669–2757); `poly.width.1` has a violate case
(`test_run_drc_reports_seeded_violation`, line 73) plus corpus-level clean
coverage. gf180mcu has a substantially larger set of the same shape (`grep -c`
on `def test_run_drc_.*(violation|clean)` in that file returns ~51 matches
total across both decks). **Gap, not absence**: a spot check finds roughly
7 of sky130's 17 rules with an *explicit*, rule-id-named violate+clean pair;
the rest rely on the generic seeded-violation fixture or corpus-derived
counts rather than a dedicated pair per rule. This is a real, fixable gap —
but it is a **testing-discipline gap**, orthogonal to whether the deck is
hand-curated or compiled: a structured golden-pair manifest (rule id → violate
GDS → clean GDS → source citation) is buildable today, against the existing
hand-curated decks, with zero compiler work. **[PROPOSAL]**: build this
regardless of the go/no-go below (see §5).

### 3.2 Cross-check against the hand deck: already partially wired, for sky130

**[REPO]** `klt drc` already ships **two engines** against the same input:
the default `"curated"` engine (`run_drc`, the `Region`-primitive `DrcRule`
tables) and an opt-in `"klayout"` engine (`run_drc_klayout_engine`, issue
#565), which subprocess-invokes the real `klayout` binary running the PDK's
own **unmodified, native** `.lydrc`/`.drc` script and parses its `.lyrdb`
report into the identical `violations[]`/`rule_counts` shape
(`docs/cli/drc.md` "Engine"). For sky130, whose native deck is a single
self-contained `<variant>.lydrc` file, `pdk.drc_deck_file()` already resolves
it (`pdk.py:391-401`). **This is precisely the "cross-check against the
existing deck" comparison the epic asks a compiler to earn** — except it
already exists, requires no compiler, and the reference side is the PDK's own
*real, unmodified* rule source rather than a compiled approximation of it.
**No automated diff currently runs it**: `tests/test_drc_klayout_engine.py`
exercises the `"klayout"` engine in isolation (mocked and real-binary tests)
but nothing in this repo runs both engines against the same corpus file and
diffs `rule_counts`/`violations`. That harness — curated vs. native, on the
existing corpus, per rule id — is a small, immediately actionable increment
that produces the epic's "agreement measured, disagreements triaged"
acceptance signal for sky130 **today**, with no new compiler code (see §5).
It does **not** work for gf180mcu (native deck needs an assembly step
`drc_deck_file()` deliberately declines to drive, §2) or SG13G2 (no curated
deck exists yet to cross-check against, and `drc_deck_file()`'s naming
convention against a real SG13G2 install has not been verified in this repo).

**The #520 corpus this issue's item 2 asks to cross-check against does not
exist yet.** **[REPO]** Issue #520 ("Epic: the Tiny Tapeout corpus...") is
itself open, unimplemented, and carries `loom:operator-only`/
`loom:operator-decision`; its Phase 1 ("ingest and mass regression") has not
shipped. What exists today is `tests/corpus/`: 4 sky130 standard cells + 3
gf180mcu standard cells (hand-picked from two upstream cell libraries,
`tests/corpus/README.md`), plus one machine-generated macro
(`tests/corpus/place_and_route/gcd.gds.gz`, ~3600 instances, the only
corpus file exercising machine-generated hierarchy at any scale). Any
cross-check work done now runs against this ~8-file corpus, not the
4,572-design Tiny Tapeout set the epic's language implies — a much weaker
oracle, worth stating plainly rather than borrowing #520's scale by
implication.

### 3.3 Per-rule source-line provenance: already the deck's own standard, by hand

**[REPO]** Every `DrcRule` in both decks already cites its upstream rule id,
source file, and (for sky130) the literal `.lydrc` source line's comment
text, or (for gf180mcu) the DRM section/table/rule-id, inline in a Python
comment or docstring immediately above the rule — this is not an occasional
practice, it is the deck's structural convention throughout both 1000+/1500+
line files (§1's examples are representative, not cherry-picked). **A
compiled deck's provenance would be an automated version of what is already
done by hand, rule by rule, with citations** — so "each rule traces to its
PDK source line" is **not** a property a compiler uniquely adds relative to
the status quo. **[PROPOSAL] The one property hand-citation genuinely
cannot match, and a compiler pointed at a *live* fetched PDK source
structurally can, is provenance drift detection**: a hand-written citation
is pinned at the moment of authorship and nothing in this repo currently
re-checks that the cited upstream line still reads the same way after a PDK
update — sky130's `m1.4`/`li.5`/etc. citations could go stale silently if
`open-pdks` changes those rules upstream, and no test would notice. This is
the one differentiator worth weighing seriously in the go/no-go call below,
not the provenance-recording claim itself.

## 4. Go / no-go

**No-go, for a general-purpose "PDK rule source → generated DRC/LVS deck"
compiler across all three PDKs uniformly, right now.**

1. **Two of three rule sources are DSL scripts, not structured data (§2).**
   Compiling from them means first building and separately validating a
   DRC-DSL parser/interpreter — a large, high-risk prerequisite epic #711
   does not budget, and one that duplicates work KLayout's own DSL runner
   already does and that `klt drc --engine klayout` already wraps (§3.2).
   Only gf180mcu has a genuinely structured (CSV) numeric source, and even
   there one rule family falls back to DSL fragments.
2. **The claimed unique payoffs are mostly already present or cheaply
   reachable without compiling (§3).** Golden pairs exist for a majority of
   rules already (gap: formalize into a manifest, not "build golden pairs
   for the first time"). Cross-check against the trusted native deck already
   exists as a wired but undiffed capability for sky130 (gap: write the diff
   harness, not "invent a comparison mechanism"). Source-line provenance is
   already every rule's standard citation practice by hand (the one real gap
   — drift detection against a live source — is narrow and specific, not "no
   provenance exists today").
3. **The epic's own validation oracle (#520's Tiny Tapeout corpus) does not
   exist yet.** Cross-checking a compiler's output against a corpus this
   epic's own acceptance criteria depend on is not possible at the scale
   implied until #520 Phase 1 ships; the interim corpus is ~8 files.
4. **The two rule classes item 3 of this issue names that the engine cannot
   express today (area, density) are blocked on a `DrcRule.check`-primitive
   addition, not on a compiler.** A compiler emitting `DrcRule` entries
   still cannot represent an area or density rule until that vocabulary gap
   is closed — the gap is in `drc.py`'s check-kind dispatch, not in how the
   deck's data got authored. Antenna is explicitly excluded (owned by epic
   #713).

**Partial go, narrowly scoped, only if pursued at all: gf180mcu's structured
DRM CSV tables.** **[PROPOSAL]** gf180mcu is the one PDK where a small,
low-risk tool is genuinely justified: a script that parses the
already-cited `tables_clear/*.csv` files for the width/space/enclosure
sections this deck already curates from (`16_Poly2_42.csv`,
`21_Contact_56.csv`, `22_Metaln_58.csv`, `23_Vian_59.csv`,
`24_MetalTop_61.csv`, `13_Nwell31.csv`, `14_COMP33_1.csv`), maps each row to
a candidate `DrcRule` via the layer-name↔(layer,datatype) mapping the module
docstring already documents, and **diffs** the result against the existing
hand-curated `gf180mcu.py::DECK` — not replaces it. This does not require a
DSL parser, is scoped to a real structured source, and directly produces a
measurable disagreement report (§5) rather than an unvalidated new deck.

**Recommended first step regardless of the go/no-go above, because it is
near-zero marginal cost and produces the epic's actual acceptance signal
today: build the curated-vs-`--engine klayout` diff harness for sky130
(§3.2).** It requires no new compiler code, exercises capability that
already shipped (#565), and answers the epic's real underlying question —
"does the hand-curated deck actually agree with the PDK's own rules, and
where does it not?" — for 17 real rules today, before any compilation
investment is made. If it finds material disagreement, that is direct
evidence a compiler (or at minimum an expanded hand deck) is worth pursuing;
if it finds close agreement (expected, given how the deck's own approximation
notes already document every known divergence), that is evidence against
urgency.

## 5. If pursued: first rule class + golden-pair validation plan

**Rule class: gf180mcu's structured (CSV) width/spacing/enclosure numeric
tables — not area, not density (no check primitive exists for either, §1),
not antenna (owned by epic #713), and not sky130/SG13G2 (DSL sources, §2).**

1. **Ingestion tool** (`scripts/`, mirroring the style of the existing
   `scripts/generate_deck_history.py`): parse the cited `tables_clear/*.csv`
   files, resolve each row's layer name(s) via the layer map already
   documented in `gf180mcu.py`'s module docstring, and emit a candidate
   `DrcRule` per row with the source CSV file + row cited automatically
   (structurally guaranteeing the "traces to its PDK source line" property,
   rather than relying on it being remembered by hand for future additions).
2. **Diff, not replace**: compare the candidate table against the shipped
   `gf180mcu.py::DECK` by rule id — report exact matches, threshold/scope
   mismatches, CSV rows with no curated counterpart (candidate net-new
   rules), and curated rules with no CSV counterpart (the DSL-derived
   conductor-over-cut family, §2 — expected, not a bug in the tool).
3. **Golden-pair requirement**: every rule the diff flags as mismatched or
   proposed-new must ship a violate/clean GDS pair (reusing the
   `tests/test_drc.py` per-rule pattern already established for most
   gf180mcu rules, §3.1) before it is allowed to change the shipped deck. A
   rule with no golden pair is not merged, per the epic's own discipline.
4. **Corpus cross-check**: run both the existing hand-curated deck and the
   CSV-candidate deck against the real corpus available today
   (`tests/corpus/gf180mcu/*.gds` — 3 files — plus any gf180mcu material
   #520 Phase 1 adds if it lands first) and require **matching
   `rule_counts` on every file** before any hand-curated rule is replaced by
   its compiled counterpart; a mismatch is triaged and documented in the
   ingestion tool's own diff report, not silently accepted or silently
   dropped.
5. **Explicit, measurable agreement criterion** (answering this issue's
   "how to measure agreement... before replacing them"): 100% `rule_counts`
   parity across the full available corpus for every rule the CSV tool
   claims to supersede, plus a clean pass/fail on that rule's own golden
   pair, are both required before a `gf180mcu.py` rule is replaced —
   partial or "close enough" agreement is a documented open finding, not a
   passing bar.

## Summary

- `klt deck` (§0) is a version resolver, unrelated to rule content; the real
  subject is `decks/sky130.py` and `decks/gf180mcu.py` (SG13G2's `#524` deck
  does not exist yet).
- Rule sources are structurally uneven across PDKs (§2): gf180mcu has a real
  structured (CSV) numeric source; sky130 and SG13G2 are DSL scripts a
  compiler would have to interpret, not merely parse.
- The properties a compiler promises — golden pairs, corpus cross-check,
  source-line provenance — already substantially exist today by other means
  (§3); the marginal, compiler-unique value is narrower than the epic
  assumed, chiefly provenance-drift detection against a live source.
- **Recommendation: no-go on a general cross-PDK compiler now; partial go on
  a narrow gf180mcu CSV-ingestion diff tool if pursued at all; and, ahead of
  either, build the sky130 curated-vs-native-engine diff harness (§3.2/§4) —
  it answers the epic's real question today at near-zero cost using
  capability that already shipped.**
