# Design note: substrate continuity for `erc.supply_short` in `klt erc`

**Status:** design only (phase 1 of
[#5334](https://github.com/2AMLogic/klayout-tools/issues/5334), tracked by
[#5335](https://github.com/2AMLogic/klayout-tools/issues/5335)). No runtime
behavior, spec key, or JSON field changes ship with this note. The
implementation is [#5336](https://github.com/2AMLogic/klayout-tools/issues/5336),
to be re-scoped from §9.

**Base commit:** every current-code claim below was checked against
`7d676aa2b583505508e31d22a8c90e42b8bc99e2` (`origin/main` when this note was
built). Line numbers refer to that commit. The spike's own line numbers
(`7e047a5df`) have drifted, so §2 re-derives them.

**Decision:** `klt erc` evaluates `erc.supply_short` **through the body**
(substrate and wells), but only along drawn, non-degenerate tap geometry, in
a **derived post-pass over the existing `ties[]` graph**. No graph gains a
conductor, so `gates[]`, antenna, and `erc.unconnected_net` are untouched.
The body view can only *add* `erc.supply_short` findings, never remove one.
Isolation (deep n-well, isolated p-well) comes from PDK layers where the
extraction deck declares them, from a new asserted `ties[].isolation_boxes`
key where it cannot, and is otherwise disclosed with the missing information
named.

**Contents**

- [1. Why the spike's decision is reopened](#1-why-the-spikes-decision-is-reopened)
- [2. Current behavior, re-verified](#2-current-behavior-re-verified)
- [3. Q1: substrate representation without the blanket collapse](#3-q1-substrate-representation-without-the-blanket-collapse)
- [4. Q2: how isolation is expressed, per PDK family](#4-q2-how-isolation-is-expressed-per-pdk-family)
- [5. Q3: reuse of `klt lvs` / `klt extract` merged-net extraction](#5-q3-reuse-of-klt-lvs--klt-extract-merged-net-extraction)
- [6. Q4: declaring an intended join](#6-q4-declaring-an-intended-join)
- [7. Answers to the spike's objections](#7-answers-to-the-spikes-objections)
- [8. Output contract](#8-output-contract)
- [9. Implementation outline and fixture acceptance criteria](#9-implementation-outline-and-fixture-acceptance-criteria)
- [10. Out of scope](#10-out-of-scope)

## 1. Why the spike's decision is reopened

[`erc-well-tap-connectivity-spike.md`](erc-well-tap-connectivity-spike.md)
(#2192) kept the metal/via graph authoritative and rejected every
net-merging option (§8). Its §10 named two reopen conditions. The second is:
*"A project decision that `erc.unconnected_net` should mirror device-aware
LVS node identity rather than metal-routed continuity."* The operator ruling
of 2026-11-11 (#5334) is a decision of that kind, but narrower: **only
`erc.supply_short` must see the substrate.** Today the report discloses the
gap (`erc_coverage.inapplicable`, reason `substrate_continuity_not_modelled`,
#4384; `src/klayout_tools/erc.py:733-739`) while `klt lvs` sees the merged net
and needs `hints.short_nets`. A disclosure is not a check, and two supply
rails sharing a p-substrate is a real defect class.

The narrowing is what makes the spike's objections answerable. The spike
rejected merging because it would turn a *severed rail* into a pass (P6) and
contaminate antenna (P4). A merge that only feeds a cross-name **short**
check does neither: it cannot hide an open, and it never touches the graphs
`gates[]`/antenna read. §7 walks each objection.

## 2. Current behavior, re-verified

| # | Fact | Where (`7d676aa2b`) |
|---|---|---|
| F1 | The primary graph is built with `ties=[]`. `gates[]`, antenna, and every `nets[]` finding come from it alone. | `erc.py:2932-2941` |
| F2 | `ties[]` gets a second extraction (`verb=None`). Its only consumer is `erc.missing_tie`; degenerate ties are graded from the same layers. | `erc.py:3085-3090`; `erc_ties.py:89` (`_tie_findings`), `erc_ties.py:171` (`_degenerate_tie_reasons`) |
| F3 | In that graph a tie contributes exactly one conductor, its tap sites `(tap_region & well_region).merged()`, self-connected and connected to `connect_to`. The well region is never registered, so taps in one well are not joined. | `erc.py:2492-2495`; rationale in the `_extract_connectivity` docstring (#2169), `erc.py:2330-2420` |
| F4 | `erc.supply_short` is the cross-name short between two declared `kind: "supply"` nets that share a cluster, skipped when the pair is a declared `same_net_as` group. | `erc.py:2001-2041` (`_cross_name_short_findings`) |
| F5 | `same_net_as` is parsed per net entry, grouped transitively, and **never joins clusters**. Its absence-of-short companion is `erc.expected_short_missing`. | `erc_spec.py:334-370`; `erc.py:1910-1960` |
| F6 | With two or more supply nets declared, `erc.supply_short` is moved to `inapplicable` with reason `substrate_continuity_not_modelled`. | `erc.py:733-739`; constant at `erc.py:326` |
| F7 | The device-aware extractor joins every substrate tap and NMOS body to one global net with `connect_global` (`vsubs` by default), and splits it per isolation island. | `extract.py:9773` (`nfet_body`), `9779` (`tap_substrate_outside`), `9791-9795` (per-island `{substrate_net}_iso{n}`) |
| F8 | The substrate tap region there is `tap - nwell_body_cover`, i.e. drawn tap geometry **outside** every well. The substrate itself has no polygon. | `extract.py:9075` |
| F9 | Isolation islands are the merged components of `deck.substrate_isolation`, sorted by bounding box. Only gf180mcu declares it (`DNWELL`, 12/0). | `extract.py:7548` (`_partition_region_by_islands`), `8806-8826`; `decks/extraction.py:665-700`; `decks/gf180mcu.py:3423` |
| F10 | `test_ties_never_alter_gates_or_net_findings` pins that `gates[]` and every non-`erc.missing_tie` finding are identical with no ties, correct ties, and a pathological `tap_layer`. | `tests/test_erc.py:3349` |

F3 + F8 are the key observation: **both engines already agree that the only
drawn thing that touches the body is a tap.** What differs is that
`klt extract` then equates all substrate taps through a global net, and
`klt erc` does not.

## 3. Q1: substrate representation without the blanket collapse

**Recommended: a derived body-merge post-pass over the existing tie graph.**

1. The tie graph (F2/F3) already has one cluster per electrical net,
   including each tie's `<tie>__tap` conductor wired to `connect_to`. No new
   `LayoutToNetlist` extraction is needed.
2. For every **non-degenerate** tie (`_degenerate_tie_reasons` empty for it),
   assign each merged tap site to a **body region**:
   - *n-type tie:* the merged, class-selected well polygon the site lies in
     (the same polygon `_tie_findings` already iterates per well).
   - *p-type / native-substrate tie:* the substrate body: the single shared
     body, unless the site lies in an **isolated body** (§4), in which case
     that body's id.
3. Union-find the tie-graph clusters over "contains a tap site of body B".
   Each merged group is one *body-connected net*.
4. Re-run the F4 rule (`erc.supply_short` between two declared
   `kind: "supply"` names) over the merged groups, **only for pairs the
   primary graph did not already report**. A pair already short on metal
   keeps its metal finding unchanged.

Properties this gives, each load-bearing:

- **Join only through drawn tap geometry.** Nothing is joined because a
  polygon overlaps a well. Two cells' contacts inside one blanket well are
  *not* joined unless each is a declared, non-degenerate tap site. The
  #2169 collapse cannot recur: the post-pass has no conductor to collapse
  with.
- **Monotone.** The pass adds findings and never deletes or edits one, so it
  cannot convert a defect into a pass (P6).
- **No graph mutation.** F1 and F10 hold by construction: `gates[]`,
  antenna, `erc.unconnected_net`, `erc.multiply_driven_net`,
  `erc.missing_tie` are computed before the pass and read nothing from it.
- **Deterministic and cheap.** Union-find over clusters that already exist;
  no extra extraction. It runs only when two or more supply nets and at
  least one tie are declared.

Rejected alternatives (one line each):

- **Register a well/substrate conductor in the primary graph.** Reintroduces
  #2169 and contaminates antenna (spike P4).
- **Add the body conductor to the tie graph.** `erc.missing_tie` reads that
  graph, so a joined body would make a tap pass for the wrong reason (B6).
- **A third `LayoutToNetlist` extraction (spike option C).** Same verdict
  with one more extraction; the post-pass gets it from clusters we already
  have.
- **Diffusion as a conductor.** Shorts every transistor's source and drain
  (spike P3, E3).
- **A global "all p-taps are `vsubs`" net.** Cannot express isolation
  (spike E4 / U4) and is exactly the LVS behavior §5 argues against copying
  wholesale.

## 4. Q2: how isolation is expressed, per PDK family

**Rule: isolation is derived from PDK layers when the extraction deck
declares them, asserted in the spec when it cannot, and otherwise the
substrate check stays disclosed.**

*Derived.* A body region is an isolated body when the extraction deck
declares an isolation layer and the tap site lies in a merged component of
it (F9). The island partition must be the same code `klt extract` uses
(§5), so the island numbering and bounding-box ordering agree.

*Asserted.* New optional key `ties[].isolation_boxes` (µm boxes, same shape
and validation as `well_boxes`/`tap_boxes`, #2255/#2234): a tap site inside
a box belongs to that box's own body. It is graded
`checked_by_assertion`, and its measured effect is echoed in `provenance`
exactly as `well_boxes` is, because that attribution rests on caller
coordinates. Rejected: a per-net `isolated: true` flag (it can hide a short
by decree, with no geometry to measure) and inferring isolation from net
names.

*Decidable-or-disclosed.* The substrate check is **evaluated** for a stack
when one of these holds, else it is **disclosed**:

1. the PDK family has no isolation layer the check needs (single
   p-substrate), or
2. the layout draws **zero** shapes on every isolation layer of the family
   (then the substrate is provably one body), or
3. every drawn isolation layer is either derived (deck declares it) or the
   isolated bodies are covered by `isolation_boxes`.

The disclosed case keeps today's `inapplicable` entry but with a refined
reason (`substrate_isolation_undecidable`) and a new `missing` field naming
the information needed (see the table). A PDK where isolation cannot be
decided therefore never silently passes.

| PDK family (`klt erc` support) | Isolation layer(s) | Today | After phase 2 | Missing information for the disclosed part |
|---|---|---|---|---|
| **gf180mcu** | `DNWELL` (12/0), already `deck.substrate_isolation` | disclosed (#4384) | **evaluated**, per-island bodies derived (F9) | none for DNWELL; an isolated p-well bounded by `LVPWELL`/`DNWELL` needs a layout that draws it, covered by the same island rule |
| **sky130** | `dnwell` (64/18) | disclosed | **evaluated** (#5344): the deck declares `substrate_isolation` (dnwell) with `substrate_isolation_needs_nwell_ring`, so a dnwell island isolates its p-well only when an n-well ring closes its outline (`erc_islands.is_ring_closed`); an unclosed island stays on shared `vsubs` | the deep-pwell marker is still not modelled |
| **sg13g2** / **sg13cmos5l** (`ihp-sg13g2` antenna table) | buried n-layer (`nbulay_drw`, 32/0) | disclosed | **evaluated** (#5345, #5359): the deck declares `substrate_isolation` (nBuLay) with `substrate_isolation_needs_nwell_ring`, so an nBuLay island isolates its p-well only when an NWell (31/0) ring closes its outline (the PDK `isolbox` pycell draws nBuLay plus an NWell ring); an unclosed island stays on shared `vsubs` | taps are derived from `Activ` (`tap=None`, `decks/sg13g2.py:1952-1962`); deep-pwell marker not modelled |
| **asap7** | none; the cells draw no substrate or well tie (`decks/asap7.py:44-52`) | not applicable (not a `--pdk` table family) | **disclosed** | there is no drawn tap geometry to join through, so the check has nothing to evaluate; the body net is the extractor's synthesized `vsubs` |

Layer numbers other than the cited gf180mcu `DNWELL` are deliberately not
stated here: phase 2 must take them from the decks, not from this note.
Rule 2 relies on the layer set that
`erc_coverage.layers_in_stream_without_declaration` (#2389) already measures
for undeclared drawn layers, so "the layout draws no isolation layer" is a
measured fact, not an assumption.

## 5. Q3: reuse of `klt lvs` / `klt extract` merged-net extraction

**Recommended: share the body-identity *rules*, not the extraction engine,
and use `klt lvs` as the agreement oracle.**

Verified against the code:

- `klt lvs` obtains its nets from `extract_netlist_from_layout`
  (`lvs.py:153-159`, call at `lvs.py:3398`). That path needs an
  `ExtractionDeck`, runs device recognition and the full layer derivation
  (`extract.py:8243`, `_extract_netlist`), and is the expensive step of
  `klt lvs`. `klt erc` takes a spec and a GDS and has no deck dependency
  (`--deck` is optional and only selects device cuts).
- The extractor's body identity is `connect_global` of NMOS bodies and
  substrate taps (F7). Its NMOS body terminals are **recognized devices**;
  ERC has no device recognition (spike B4 stands). Only the tap half of the
  mechanism is reusable by ERC.
- The island partition helper is the one place both must agree exactly:
  `extract._partition_region_by_islands` (`extract.py:7548`, referenced from
  `decks/extraction.py:672`, used at `extract.py:8826`/`9103`).

Therefore:

1. **Move the island partition into a neutral module** both `klt extract`
   and `klt erc` import (pure move, no behavior change). That makes
   "ERC and LVS number isolation islands identically" a property of the code.
2. **Do not call `extract_netlist_from_layout` from `klt erc`.** It would
   couple ERC to deck availability and device recognition, and (per
   `docs/cli/erc.md`, "not automatically an independent confirmation")
   some LVS decks name-join, which would make ERC inherit false agreement.
3. **Agreement is a tested invariant, not a shared engine.** Phase 2 adds a
   fixture comparing ERC substrate shorts against `klt lvs` `hints.short_nets`
   (and the extractor's `vsubs`/`vsubs_iso{n}` net identity) on small
   synthetic cases, plus one live run on `2AMLogic/sg13g2-ldo` (§9). Where
   they disagree the report must say why, not stay silent: ERC reports only
   what tap geometry supports, so an LVS-only merge via a device body
   terminal stays an LVS finding.

Rejected: calling the extractor and diffing node names (couples ERC to
decks; slow; inherits name-joining); duplicating the island code (the exact
drift this note exists to prevent).

## 6. Q4: declaring an intended join

**Recommended: `nets[].same_net_as` (#2463) is the mechanism, with one
semantic extension and no new key.**

Today it suppresses `erc.supply_short` for the pair and requires the short
to exist on the primary graph, else `erc.expected_short_missing` (F4/F5).
A rail pair that is deliberately one body (for example `VSS` and an
analog-ground label sharing a p-substrate on purpose) needs the same
treatment for the substrate path. So:

- A declared `same_net_as` pair **suppresses** the substrate-path
  `erc.supply_short` for that pair, exactly as it does for metal.
- `erc.expected_short_missing` is satisfied when the short exists on the
  primary graph **or** on the body-merged groups (§3). Without this, a
  substrate-only intended join would wrongly report "expected short missing".
- The finding or its absence records which path satisfied it (`via`, §8),
  so a declaration can never silently stand in for geometry that is not
  there.

Rejected: a new `nets[].join_through: "substrate"` key (a second way to say
what `same_net_as` already says; the *path* is evidence, not declaration);
a global `allow_substrate_short` flag (hides every pair at once, the
unfalsifiable pass #2199 rejected on the tap side).

An intended isolation, as opposed to an intended join, is declared through
`ties[].isolation_boxes` (§4), never through `same_net_as`.

## 7. Answers to the spike's objections

Spike §§3.5, 8, 10; each answered with the section that carries the answer.

| Spike item | Objection | Answer here |
|---|---|---|
| **P1** | Today's graph does not join same-well taps | True and unchanged for the primary graph. The body view is a separate derived pass (§3), so P1's graph stays as is. |
| **P2** | A tap-only conductor fixes same-well, not separate wells or substrate taps | Per-body assignment (§3 step 2) keeps separate wells separate (each merged polygon is its own body) and handles substrate taps through the shared substrate body, which P2's per-well rule could not. Deep/isolated bodies split via §4. |
| **P3** | Any contact in the well, or raw diffusion, falsely shorts a CMOS row | Only non-degenerate ties contribute (§3 step 2); `_degenerate_tie_reasons` already excludes the "any contact in the well" shape (F2), and diffusion is never a conductor. |
| **P4** | A well conductor in the primary graph inflates antenna ratios | No conductor is added to any graph (§3, "No graph mutation"); F1/F10 hold by construction. |
| **P5** | ERC says N islands, LVS says one: at least two geometric causes | Both are covered: same-well taps by per-body union, substrate taps by the shared substrate body (§3). |
| **P6** | A severed rail sharing a well reads as clean | The pass is monotone: it adds shorts and never removes or edits a finding (§3). `erc.unconnected_net` is not an input, so a severed rail is still reported on the primary graph. This objection was about merging into the *continuity* verdict; the ruling is scoped to the *short* verdict. |
| **U4** | Deep n-well / isolated p-well not modelled | §4: derived from the deck layer where declared (gf180mcu), asserted via `isolation_boxes` elsewhere, disclosed with named missing information otherwise. |
| **§8 assumption** | `erc.unconnected_net` is metal-routed continuity; ERC need not mirror LVS node identity | Still true for `erc.unconnected_net` and unchanged. §10's reopen condition is met only for `erc.supply_short`, per the operator ruling. |
| **§8 reject B** | Primary-graph extension breaks #2169 isolation | Not done (§3). |
| **§8 reject C** | Separate graph hides severed rails, cannot express substrate, costs a third extraction | Hiding: monotone, scoped to shorts. Substrate: shared substrate body. Cost: no third extraction (§3, cluster union-find). |
| **§8 reject blanket diffusion/well conductors** | Disproved (P3, E3) | Still rejected (§3). |
| **§10 reopen, cond. 1** | A designer-asserted well-only join | Not the driver here; the annotation from spike §6.2 remains independently valid (§10 below). |
| **§10 reopen, cond. 2** | Project decision that ERC mirror device-aware LVS identity | Met for `erc.supply_short` only, by the 2026-11-11 operator ruling (#5334). |

## 8. Output contract

All additive per `docs/json-contract.md` (no `schema_version` bump):

- Substrate-path `erc.supply_short` findings reuse the existing rule id and
  shape and add `"via": "substrate"` and `"body"`: `{"kind": "well"|"substrate",
  "id": <well bbox | "vsubs" | "vsubs_iso<n>">, "ties": [<tie names>]}`.
  Metal-path findings gain `"via": "metal"`. Absent `via` is read as metal.
- `erc_coverage.inapplicable[erc.supply_short]` keeps its slot but the reason
  becomes `substrate_isolation_undecidable` with a `missing` list (§4) only
  when disclosed; when evaluated the id moves to `checked`.
- Substrate-derived work is listed separately from metal in `checked` (id
  `erc.supply_short.substrate`), because its verdict rests on tap
  geometry plus, where asserted, caller coordinates
  (`checked_by_assertion`).
- `erc_status` for single-supply specs is unchanged (out of scope, §10).

## 9. Implementation outline and fixture acceptance criteria

Proposed split of #5336 into independently buildable pieces, in order:

| Piece | Scope | Size |
|---|---|---|
| **A. Shared island partition** | Move `_partition_region_by_islands` into a neutral module used by `extract.py` and `erc.py`. Pure move. | small |
| **B. Body-merge post-pass (gf180mcu first)** | Body-region assignment, union-find over tie-graph clusters, substrate-path `erc.supply_short`, `via`/`body` fields. Verify labels resolve in the tie graph (it is built with `verb=None` and no label-layer collector, `erc.py:3086-3090`) before relying on it. | medium |
| **C. Spec + coverage** | `ties[].isolation_boxes` parser and assertion grading; `same_net_as` extension (§6); refined disclosure and `missing` field; docs (`docs/cli/erc.md`, replace the #4384 paragraph), `CHANGELOG.md`. | medium |
| **D. Deck isolation layers** | Declare `substrate_isolation` for sky130 and sg13g2/sg13cmos5l from the decks, with the closure check named in §4. Separate PR per PDK. | small each |
| **E. Live agreement** | ERC vs `klt lvs` on `2AMLogic/sg13g2-ldo`; signoff item 11 wiring (`docs/design-evidence-tiers.md`). Needs the chip repo checkout. | small, out-of-band |

Fixture acceptance criteria (each is the observable the PR must show):

1. **Substrate-only short -> finding.** Two supply nets (`VSS`, `VSS2`), each
   with its own metal rail and its own non-degenerate p-tap tie into one
   p-substrate, **no metal between them**: `erc.supply_short` with
   `via: "substrate"`, exit code per the existing finding contract. The same
   layout with `VSS2` declared `same_net_as VSS` -> no short and no
   `erc.expected_short_missing`.
2. **Deep-well isolated -> clean.** The same two rails with the second tap
   inside a `DNWELL` island (gf180mcu) -> no `erc.supply_short`, and the
   report names the isolated body (`vsubs_iso0`) in provenance. A variant
   with an undrawn-but-asserted `isolation_boxes` on sky130 passes with
   `checked_by_assertion`. A sky130 layout that **draws** `dnwell` with no
   assertion -> disclosed with `missing` naming the undeclared isolation
   layer, **not** a pass.
3. **#2169 regression.** A routed standard-cell-row layout with one blanket
   well polygon and many ordinary contacts: zero `erc.supply_short`, and
   `gates[]` byte-identical to the pre-change report (extend
   `test_ties_never_alter_gates_or_net_findings`, `tests/test_erc.py:3349`,
   to the body pass: reports with and without `ties[]`/the pass agree on
   `gates[]`, antenna `levels[]`, and every non-`erc.missing_tie`,
   non-substrate-`supply_short` finding).
4. **Degenerate tie contributes nothing.** A tie with an un-narrowed
   `tap_layer` (spike B4 shape) produces no body join and no new finding.
5. **ERC/LVS agreement.** For fixtures 1 and 2, `klt lvs` `hints.short_nets`
   and `vsubs`/`vsubs_iso{n}` identity agree with the ERC result. On
   `2AMLogic/sg13g2-ldo`, a live run records ERC substrate result and LVS
   result side by side; any disagreement carries a stated reason. **This
   live step is not performable in this repo's CI** and must be reported as
   such, with the run tracked in the implementation issue, not asserted from
   the synthetic fixtures.
6. **Undecidable PDK keeps disclosure.** For asap7 (no drawn taps) and any
   family where rule 2/3 of §4 do not hold, the report keeps
   `inapplicable` with the refined reason and a non-empty `missing` list.

## 10. Out of scope

- Any code change in this phase.
- Relaxing assertions in chip repos.
- `erc_status` semantics for single-supply specs.
- The reporting-only well-bridge annotation of spike §6.2: still valid and
  independent (it concerns `erc.unconnected_net`, which this note leaves on
  the primary graph). It can land before or after #5336.
- Resistance or current estimates through the body; a body join is a
  geometric claim about drawn taps, not a resistive one (spike §5).
