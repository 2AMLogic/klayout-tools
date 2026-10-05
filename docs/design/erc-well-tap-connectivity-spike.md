# Design spike: well/substrate-tap net continuity in `klt erc`

**Status:** design decision only. This note resolves issue
[#2192](https://github.com/2AMLogic/klayout-tools/issues/2192). No runtime
behavior, spec key, or JSON field changes ship with it. Any implementation
is a separate follow-up issue, reviewed through the normal Curator/Champion
flow, whose acceptance criteria come from §9 below.

**Base commit:** every current-code claim below was checked against
`7e047a5df68f94f83d90e0fdc8b0c93f956459f1` (`origin/main` when this spike
was built), with `klayout` 0.30.12. Line numbers refer to that commit.

**Decision:** **keep the primary metal/via graph authoritative and add no
net-merging well conductor**, in the primary graph or in a separate one.
`erc.unconnected_net` goes on reporting metal-routed continuity. The one
follow-up worth building is **reporting only**: an additive per-island
annotation on a multi-island `erc.unconnected_net` finding. It records which
islands land, through a declared non-degenerate `ties[]` tap, in the same
merged well polygon. It changes no finding, verdict, roll-up, or exit code,
and it adds no spec key (§6.2). The existing limitation note and LVS
cross-check in `docs/cli/erc.md` stay as written.

**Contents**

- [1. The question](#1-the-question)
- [2. Current behavior, verified at the base commit](#2-current-behavior-verified-at-the-base-commit)
- [3. Evidence: synthetic reproduction](#3-evidence-synthetic-reproduction)
- [4. Q1: which graph is authoritative for net findings](#4-q1-which-graph-is-authoritative-for-net-findings)
- [5. Q2: what physical continuity is justified](#5-q2-what-physical-continuity-is-justified)
- [6. Q3: the JSON contract](#6-q3-the-json-contract)
- [7. Q4: does the #2180 case demonstrate well conduction?](#7-q4-does-the-2180-case-demonstrate-well-conduction)
- [8. Alternatives compared and the recommendation](#8-alternatives-compared-and-the-recommendation)
- [9. Follow-up: implementation and test outline](#9-follow-up-implementation-and-test-outline)
- [10. When to reopen the net-merging question](#10-when-to-reopen-the-net-merging-question)

## 1. The question

`klt erc` traces connectivity only through the conductors a spec declares
(`stackup` and `vias`). Silicon also conducts through well and substrate
material, and a device-aware LVS deck models some of that. So a supply net
can extract as one node under LVS and still report several
`erc.unconnected_net` islands under `klt erc`.
[#2180](https://github.com/2AMLogic/klayout-tools/issues/2180) reported one
such case: 3 ERC islands against a 43/43 LVS match. It documented the gap
and deferred the mechanism to this issue.

The obvious fix fails. If drawn diffusion becomes an ordinary conductor,
every transistor's source and drain are shorted, because they are one drawn
polygon with the channel. A blanket well conductor also fails. It joins
every contact inside the well, which is the pre-#2169 collapse. The issue
asks four questions, answered in §4–§7:

1. Which extraction graph is authoritative for net findings, and how do
   `gates[]`, antenna results, and the isolation of `ties[]` stay protected?
2. What physical continuity is justified between taps in one well and
   across separate wells? This must keep geometric connectivity assumptions
   apart from resistive or device behavior.
3. What exact JSON declaration, validation, disclosures, and compatibility
   behavior would be needed, given `nets[].islands`, `same_net_as`, device
   cuts, and tie assertions?
4. Does the #2180 case actually demonstrate well conduction?

## 2. Current behavior, verified at the base commit

Each claim below was checked by reading the code at the base commit. The
reproduction in §3 confirms the behavioral claims (B1–B5) by observation.

| # | Behavior | Where (`7e047a5d`) |
|---|---|---|
| B1 | The primary graph registers each `stackup` role (minus its `devices[]`/`--deck` cut) and self-connects it. Each `vias` entry is registered and connected to its two roles. Nothing else is registered: no well, no diffusion unless a spec names it as a role. | `erc.py` `_extract_connectivity`, lines 3854–3871; `_cut_device_bodies` in `_devices.py:263` |
| B2 | `run_erc` builds that primary graph with `ties=[]`. `gates[]`, every antenna level, and every `nets[]` finding (`erc.unconnected_net`, `erc.multiply_driven_net`, `erc.supply_short`, `erc.expected_short_missing`, `erc.unlabelled_conductor`) come from it alone. | `erc.py` lines 4245–4247 (primary build), 4292–4343 (gate loop), 4367–4370 (`_net_connectivity_findings`) |
| B3 | `ties[]` gets a **second** extraction: the same stackup and vias, plus one `<tie>__tap` conductor per tie. That conductor is `tap_layer ∩ every tap_requires ∩ union(tap_boxes) ∩ well`, self-connected and connected to `connect_to`. The well region itself is never registered, so two taps in one well are **not** joined through it. Only `erc.missing_tie` reads this graph. | `erc.py` lines 3897–3910 (tap sites), 4382–4386 (tie graph used only by `_tie_findings`) |
| B4 | A tie whose tap region is "whatever that layer draws inside the well" (not `tap_is_dedicated`, and narrowing removed nothing) is graded as skipped work (`degenerate_tap_declaration`), not as a pass. The same applies to an asserted whole-die well, a non-partitioning class selection, and an empty well layer. | `erc.py` `_degenerate_tie_reasons`, lines 3407–3521 |
| B5 | `erc.unconnected_net` counts **labelled** islands: the clusters whose label set contains the declared name. It grades them against `nets[].islands` (default 1). `islands: N` is an expected count and asserts nothing about electrical continuity. `same_net_as` suppresses the cross-name short between two declared names and requires that the short exist (`erc.expected_short_missing`). It never joins clusters. | `erc.py` `_match_net_clusters` 2663; `_parse_net_islands` 1417; `_net_connectivity_findings` 3157–3324; `_cross_name_short_findings` 3114; `_expected_short_findings` 3069 |
| B6 | Isolation is pinned by a test: `gates[]` and every non-`erc.missing_tie` finding are identical with no ties, correct ties, and a pathological `tap_layer`. | `tests/test_erc.py::test_ties_never_alter_gates_or_net_findings` (line 2970) |
| B7 | For comparison, this repo's **device-aware** extractor does the opposite of B3. It connects `nwell` to itself, `nwell` to `tap`, and `tap` to `contact`, so the well conducts across its whole merged polygon and joins routing only at taps. It ties every substrate tap and NMOS body to one global substrate net with `connect_global`. Diffusion conducts only as device-split `*_sd` regions, never through a channel. | `extract.py` lines 8547–8551 (`nwell`/`tap`/`contact`), 8606/8612 (`connect_global` to `substrate_net`) |

B7 matters for Q4 and for how much the LVS cross-check can tell you. On
geometry a device-aware deck sees, it merges same-well taps. So does a
substrate-tied ground, at whole-die scope.

## 3. Evidence: synthetic reproduction

### 3.1 The original #2180 artifact is unavailable

#2180 was filed generically from a downstream repo's analog block. It
followed that repo's friction protocol, so it attaches no GDS, spec, or LVS
deck. No such artifact exists in this repository, and #2180's thread does
not link one. **The original false positive is therefore not reproduced
here, and this spike does not claim to explain or fix it** (§7). The
evidence below is a set of clearly labelled **synthetic** layouts. They
test the geometric premises the design rests on.

### 3.2 Fixtures and candidate mechanisms

[`erc_well_tap_spike_repro.py`](erc_well_tap_spike_repro.py) (exploratory,
not a shipped fixture or test) draws four layouts. It uses sky130's public
GDS layer numbers so `klt extract --deck sky130` can read the same streams.

| Fixture | Geometry |
|---|---|
| **E1 `same_well`** | One merged n-well. Two n-taps (`tap` + `nsdm`), each strapped through licon/li1/mcon to its **own** met1 `VPWR` fragment. No metal joins the two fragments. A tie-hi poly gate is strapped to fragment A. This is also exactly the geometry of a **severed VPWR rail whose two halves both tap one well**. |
| **E2 `separate_wells`** | Identical to E1, except the n-well is two disjoint polygons, one under each fragment. This is the geometry #2180's reporter described for the two smaller islands. |
| **E3 `cmos_inverter`** | An ordinary inverter. The PMOS sits in an n-well and the NMOS outside it. Each device's source, channel, and drain are one continuous `diff` polygon. One n-tap ties the well to `VPWR`. |
| **E4 `substrate_taps`** | No well. Two p-substrate taps (`tap` + `psdm`), each strapped to its own met1 `VGND` fragment, with no metal between them. A tie-lo gate is strapped to fragment A. |

Each candidate connectivity rule is **emulated with spec keys that already
exist**, so no klt source changes:

| Variant | What it emulates | How |
|---|---|---|
| `baseline` | Today's model | `stackup` poly (gate, `active_layer` diff) / li1 / met1; `vias` licon, mcon |
| `ties` | Today's model plus a correct `ties[]` entry | `baseline` + `{"well_layer": "64/20", "tap_layer": "65/44", "tap_is_dedicated": true, "connect_to": "li1", "net": "VPWR"}` |
| `well_via_tap` | **Candidate**: the well conducts across its merged polygon and joins routing **only at tap sites** (the B7 model) | `baseline` + `stackup` role `nwell` (64/20) + `vias` `{"layer": "65/44", "between": ["nwell", "li1"]}` |
| `well_via_licon` | The pre-#2169 shape, or a degenerate tie: the well joins routing at **every contact** inside it | as above, bridged through licon (66/44) instead of `tap` |
| `diff_conductor` | The naive fix: raw diffusion as an ordinary conductor | `baseline` + `stackup` role `diff` (65/20) + `vias` licon between `diff` and li1 |

E4 skips the `ties` row. With no n-well drawn, that tie could only be
skipped as `empty_well_region` (#2377).

### 3.3 Reproduction commands

From the repository root, at the base commit:

```bash
uv sync --extra dev
uv run python docs/design/erc_well_tap_spike_repro.py --out /tmp/wtspike
```

The script writes `/tmp/wtspike/<fixture>.gds` and
`/tmp/wtspike/<fixture>.<variant>.json`. It runs the unmodified `run_erc` on
every pair and prints one summary per pair as JSON: matched islands per
declared net, findings, `gate_count`, and the met1 antenna level of each
gate. Any row can be re-run through the CLI, for example:

```bash
uv run klt erc /tmp/wtspike/same_well.gds /tmp/wtspike/same_well.baseline.json --format json      # exit 3
uv run klt erc /tmp/wtspike/same_well.gds /tmp/wtspike/same_well.ties.json --format json          # exit 3
uv run klt erc /tmp/wtspike/same_well.gds /tmp/wtspike/same_well.well_via_tap.json --format json  # exit 4 (erc_status clean; no --pdk)
uv run klt erc /tmp/wtspike/cmos_inverter.gds /tmp/wtspike/cmos_inverter.diff_conductor.json --format json  # exit 3
```

The device-aware cross-check (B7) runs on the same streams:

```bash
uv run klt extract /tmp/wtspike/same_well.gds --deck sky130 -o /tmp/wtspike/same_well.sky130.spice --format json
# likewise separate_wells, substrate_taps, cmos_inverter
```

### 3.4 Observed outcomes

`klt erc` (`run_erc`) results, matched labelled islands per declared net:

| Fixture | `baseline` | `ties` | `well_via_tap` | `well_via_licon` | `diff_conductor` |
|---|---|---|---|---|---|
| E1 same well | VPWR **2** → `unconnected_net` | VPWR 2 → `unconnected_net`; `missing_tie` checked and clean | VPWR **1**, no findings | VPWR 1, no findings | VPWR 2 → `unconnected_net` |
| E2 separate wells | VPWR **2** → `unconnected_net` | VPWR 2 → `unconnected_net`; `missing_tie` checked and clean | VPWR **2** → `unconnected_net` | VPWR 2 → `unconnected_net` | VPWR 2 → `unconnected_net` |
| E4 substrate taps | VGND **2** → `unconnected_net` | (skipped, see above) | VGND 2 → `unconnected_net` | VGND 2 → `unconnected_net` | VGND 2 → `unconnected_net` |
| E3 inverter | all four nets 1, **no findings** | no findings; `missing_tie` checked | no findings | **`multiply_driven_net(OUT,VPWR)`** | **`supply_short(VGND,VPWR)`**, `multiply_driven_net(OUT,VGND)`, `multiply_driven_net(OUT,VPWR)` |

Antenna on E1's tie-hi gate (met1 level, no `--pdk`, so the ratio is
computed but ungraded):

| E1 variant | `gate_count` | met1 `cumulative_area_um2` | met1 `antenna_ratio` |
|---|---|---|---|
| `baseline`, `ties` | 1 | 25.95 | 12.975 |
| `well_via_tap`, `well_via_licon` | 1 | **47.95** | **23.975** |

`ties` matches `baseline` everywhere, which is B6 holding. As a negative
control, deleting E2's right-hand tap shape turns the `ties` run into
`erc.missing_tie` ("well/tub region has no 'nwell_tie' tap contact drawn
inside it") alongside the unchanged 2-island `erc.unconnected_net`.

`klt extract --deck sky130` (all exit 0), top-level subcircuit pins:

| Fixture | Extracted supply nets |
|---|---|
| E1 same well | `.SUBCKT TOP VPWR`: **one** VPWR. The two fragments merge through the n-well, and the PMOS body is VPWR. |
| E2 separate wells | `.SUBCKT TOP VPWR VPWR$1`: **two** VPWR nets. The extractor does not merge separate wells or join them by name. |
| E4 substrate taps | `.SUBCKT TOP VGND`: **one** VGND. Both p-taps reach the global substrate net. |
| E3 inverter | `IN OUT VGND VPWR vsubs`: correct NFET and PFET with source and drain separated. No short. |

### 3.5 What the evidence proves, and what it does not

Proved, on these synthetic streams at the base commit:

- **P1.** Today's ERC graph does not join same-well taps (E1 `baseline` and
  `ties`: 2 islands), and a `ties[]` declaration does not change that (B3,
  B6).
- **P2.** A well conductor joined to routing only at tap sites merges E1 to
  1 island. It does **not** merge separate wells (E2) or substrate taps (E4).
  It does **not** short an ordinary CMOS row (E3). So a tap-only well rule is
  geometrically safe against S/D shorting, provided the tap really is a tap.
- **P3.** If the bridge is any contact inside the well, which is the
  degenerate-tie shape, E3 gets a false `multiply_driven_net(OUT,VPWR)`. Raw
  diffusion as a conductor gives E3 a false `supply_short(VGND,VPWR)`. Both
  premises the issue warned about are reproduced.
- **P4.** Putting the well conductor in the **primary** graph changes antenna
  results: E1's tie-hi gate net absorbs the other fragment's metal, so met1
  cumulative area goes 25.95 → 47.95 µm² and the ratio 12.975 → 23.975. That
  is the #2169-class contamination B6 exists to prevent.
- **P5.** This repo's device-aware extractor treats E1 and E4 as one supply
  node each, and E2 as two. So "ERC says N islands, LVS says one net" has at
  least two distinct geometric causes (same-well taps and substrate taps).
  A per-well-polygon ERC rule could address only the first.
- **P6.** E1 is geometrically identical to a severed rail. Under the
  `well_via_tap` rule, and under device-aware LVS, a broken VPWR rail whose
  halves share a well reads as clean.

Not proved or not verified:

- **U1.** What actually happened on #2180's GDS (§7).
- **U2.** Real-PDK sheet resistance and current-carrying behavior. The
  experiments are pure connectivity: they show what joins, not how well.
- **U3.** The behavior of third-party LVS decks. Only this repo's
  `klt extract --deck sky130` was run. The name-joining caveat already in
  `docs/cli/erc.md` comes from reading the code of one open-foundry deck and
  was not re-run here.
- **U4.** Deep n-well, isolated p-well, and triple-well stacks. These are
  not exercised. §5 covers them in reasoning only.

## 4. Q1: which graph is authoritative for net findings

**Answer: the primary `stackup`/`vias` graph stays authoritative for
`gates[]`, for every antenna level, and for every `nets[]`-driven finding.**
No well, substrate, or diffusion conductor enters it.

- **Gates and antenna must not see a well conductor.** The antenna ratio
  measures how much routed conductor is attached to a gate's poly through
  the layers built up during processing. A well path between two metal
  fragments adds no metal to the gate's net at any processing step, so
  counting the other fragment's metal inflates the ratio (P4). Whether a
  diffusion connection *protects* a net, the antenna diode question, is
  separate, and ERC does not model it today. Ratios would also differ
  between a spec with and without the declaration for the same layout. This
  is the silent invalidation #2169 removed, so it must not come back.
- **`ties[]` stays isolated.** Its graph answers only `erc.missing_tie` (B3,
  B6). Nothing in this decision feeds the tie graph back into the primary
  graph.
- **No third, net-findings-only graph either** (§8, option C). That option
  would protect gates and antenna, but it puts a resistive path into the
  supply-continuity verdict. That hides the defect the verdict exists to
  catch (P6), and it still cannot address substrate taps (P5) or the #2180
  geometry (P2, E2).

What the tie graph *can* safely add is **attribution, not merging**: which
primary-graph islands meet a declared tap in a common well polygon. §6.2
specifies that as a reporting-only annotation. It reads both graphs' regions
and writes into neither.

## 5. Q2: what physical continuity is justified

Separate two claims. A *geometric* claim says that one region of a given
doping is continuous between two sites. A *resistive/device* claim says that
current between those sites is acceptable for the circuit. Connectivity
extraction can justify only the first. ERC's supply verdict is used as a
proxy for the second (item 11 of `docs/design-evidence-tiers.md`, structural
power delivery).

| Case | Geometric continuity | Resistive/device reality | Justified in ERC? |
|---|---|---|---|
| Two taps in **one merged well polygon** | Yes: one continuous n-type region (E1; B7 merges it) | A distributed resistor, typically far more resistive than any metal strap, with the PMOS bodies hanging off it. Adequate for **body bias**, where almost no DC current flows. **Not** adequate as the supply path for devices' source current. | As a supply-continuity **merge**: no. It would turn a severed rail (P6) into a pass. As an **attribution** ("these islands share a tapped well"): yes. |
| Taps in **separate well polygons** of the same type | No: the wells are isolated from each other by reverse-biased junctions to the substrate | No DC path. Any coupling is junction leakage and capacitance. | No. Any rule that joins them is wrong (E2). |
| Taps in the **common p-substrate** (no well) | Yes, but nothing marks it: the substrate is the absence of a well, so the "polygon" is the die | Resistive, with the same objection as one well. LVS models it as one global node (E4, B7 `connect_global`), as does an asserted `well_boxes` region (#2255). | No merge. Attribution only for an explicitly asserted `well_boxes` region, flagged as an assertion. |
| **Source/drain diffusion** of a MOSFET | The drawn diffusion is continuous through the channel | Source and drain are separated by the channel. They are distinct device terminals. | Never as a conductor without device recognition (E3 `diff_conductor`). `ties[]` already keeps S/D contacts out of the tap region (`tap_requires`/`tap_is_dedicated`, B4). |
| **Deep n-well / isolated p-well** | Nested regions with their own junctions | The isolated p-well is a separate body node from the substrate. The deep n-well is one more well. | Same answer as wells, per merged polygon, through existing `ties[]` class selection (`well_requires`/`well_excludes`). Unverified here (U4). |

So the only continuity that is geometrically justified *and* safe against
the S/D short is "taps inside one merged well polygon". Even that is a
resistive body-bias path, not a supply route. That is why the
recommendation reports it rather than grading on it.

## 6. Q3: the JSON contract

### 6.1 What a net-merging declaration would need (rejected; recorded for completeness)

If a future demand case reopens merging (§10), the contract has to look like
this. It is written out so that §8 can reject a concrete proposal rather
than a vague one, and so a reopened issue can start from it.

```json
{
  "ties": [
    {
      "name": "nwell_tie",
      "well_layer": "64/20",
      "tap_layer": "65/44",
      "tap_is_dedicated": true,
      "connect_to": "li1",
      "net": "VPWR"
    }
  ],
  "well_continuity": [
    { "tie": "nwell_tie", "scope": "within_well_polygon" }
  ]
}
```

Validation (a spec error is exit 1, raised as `ErcError`, the existing
convention):

- `well_continuity` is optional. Omitted, `null`, or `[]` means no merge and
  a byte-identical report. Anything else must be an array of objects.
- `tie` (required) must name a declared `ties[]` entry. An unknown name is a
  spec error, never a no-op (the `same_net_as` precedent: a typo must not
  read as an honoured declaration). Two entries naming one tie is a spec
  error.
- `scope` (required) accepts exactly `"within_well_polygon"`. The key is
  kept so that an unsupported cross-well or die-wide merge has to be
  *rejected by name* rather than silently defaulted.
- The named tie must have a drawn `well_layer`. A `well_layer: null` +
  `well_boxes` tie is a spec error, because an asserted region would let
  coordinates the caller typed act as a conductor.
- The tie's `net` must be a declared `nets[]` entry, so the merge has a
  graded subject.
- Checked at runtime, not at parse time: a tie that `_degenerate_tie_reasons`
  classifies as degenerate contributes **no** merge. That entry lands in
  `erc_coverage.skipped` with the tie's existing reason, so `erc_status` is
  at best `clean_partial`. It is never silently applied.

Disclosure and compatibility:

- Merges would be computed in a **third** extraction. It would be the
  primary roles plus, per entry, the selected well region (self-connected)
  and the tie's tap sites (connected to the well and to `connect_to`), with
  `devices[]`/`--deck` cuts applied as in the other graphs. Only the
  `nets[]` findings would read it. `gates[]` and antenna would keep reading
  the primary graph, so B6 would extend to cover the new key.
- `provenance.well_continuity[]` would echo each applied entry with its
  measured effect: `{tie, wells_used, islands_before, islands_after}`. A
  declaration that merged nothing must be distinguishable from one that did,
  as with `provenance.devices[].body_area_um2`.
- `erc_coverage` would gain a `checked_by_well_continuity` list, kept apart
  from `checked`, because the verdict rests on a resistive path.
- Interactions: `nets[].islands` grades the post-merge count. A merge that
  joins two *differently named* declared nets reports
  `erc.supply_short`/`erc.multiply_driven_net` exactly as a metal short
  would, unless `same_net_as` declares them one net. Device cuts apply
  first.
- Additive per `docs/json-contract.md`. It needs a new optional request key
  and new report fields, but no `schema_version` bump.

**Why it is rejected:** see §8. In short, it hides severed rails (P6), it
fixes neither E2 nor E4, and `klt extract`/`klt lvs` already answer the
device-aware continuity question (B7).

### 6.2 Recommended: no new spec key; an additive, reporting-only annotation

There is **no new request key**. The annotation is computed from
declarations the spec already makes, `nets[]` plus `ties[]`. That
answers "or explain why no extension is warranted" for the *spec*: no new
declaration is needed. The well side is already declared, and the
annotation must not depend on a key whose only effect is to make a finding
go away.

Output additions, all on the existing `erc.unconnected_net` multi-island
finding (issue #2194 shape). Values below are what E1 with the `ties`
variant would report:

```json
{
  "rule": "erc.unconnected_net",
  "description": "declared net 'VPWR' resolves to 2 disconnected electrical islands (expected exactly one)",
  "net": "VPWR",
  "other_net": null,
  "gate_id": null,
  "layer": null,
  "bbox": {"left": 0, "bottom": 1000, "right": 20000, "top": 8000},
  "islands": [
    {
      "bbox": {"left": 0, "bottom": 1000, "right": 8000, "top": 8000},
      "layer": "met1",
      "shape_count": 4,
      "tapped_wells": [
        {"tie": "nwell_tie", "well_bbox": {"left": 0, "bottom": 0, "right": 20000, "top": 8000}}
      ]
    },
    {
      "bbox": {"left": 12000, "bottom": 1000, "right": 20000, "top": 5000},
      "layer": "met1",
      "shape_count": 2,
      "tapped_wells": [
        {"tie": "nwell_tie", "well_bbox": {"left": 0, "bottom": 0, "right": 20000, "top": 8000}}
      ]
    }
  ],
  "well_bridge_groups": [[0, 1]],
  "well_bridge_basis": [{"tie": "nwell_tie", "well_asserted": false}]
}
```

Every key except the three new ones is copied from today's actual output
for E1 with the `ties` variant (`klt erc /tmp/wtspike/same_well.gds
/tmp/wtspike/same_well.ties.json --format json`). The new fields are
`tapped_wells`, `well_bridge_groups`, and `well_bridge_basis`, and their
values are what the proposed rule would compute (E1's single n-well is
`(0, 0)-(20, 8)` µm). On E2 the two islands name different `well_bbox`es
and `well_bridge_groups` is `[[0], [1]]`.

Semantics:

- `islands[].tapped_wells`: for each basis tie, each merged well polygon
  (after that tie's class selection) whose tap sites geometrically interact
  with this island's primary-graph geometry on the tie's `connect_to` role.
  The same region test `_tie_findings` already uses, applied per island.
  Sorted by `(tie, well_bbox)`. `[]` means measured, and this island reaches
  no tapped well.
- `well_bridge_groups`: a partition of island indices, in `islands[]`
  order, into groups that share at least one `(tie, well polygon)` (the
  transitive closure). Singletons are listed, so the partition is complete.
- `well_bridge_basis`: the ties the measurement used. `well_asserted: true`
  marks a tie whose well side rests on `well_boxes`, `well_requires_boxes`,
  or `well_excludes_boxes` (the existing `well_asserted` predicate), because
  that attribution rests on caller coordinates.
- **Wording contract.** The description string is unchanged. The fields
  state geometric co-location ("these islands each reach a tap of the same
  well polygon"), never electrical continuity. `docs/cli/erc.md` must say
  that a bridge group is **a triage hint**: one group is a candidate for
  either a body-bias-only well path (intended) or a severed metal rail
  joined only through the well (a defect). Telling those apart is a human
  or LVS decision.

Not-measured and invalid-input behavior (the "nothing to measure" states
are `null`, never a guessed `[]`, following the `nets[].unlabelled_*`
precedent):

- No `ties[]` declared, or every declared tie is degenerate or skipped
  (B4): `tapped_wells` is `null` on every island, and
  `well_bridge_groups`/`well_bridge_basis` are `null`.
- A degenerate tie is **excluded** from the basis, even when other ties
  remain. Its reason is already in `erc_coverage.skipped`. Counting it would
  let any S/D contact act as a "tap" (P3).
- Zero-match `erc.unconnected_net` (no labelled geometry, `islands: null`):
  the new fields are `null`.
- No new spec input exists, so no new spec-error path exists. Malformed
  `ties[]` keeps its existing exit-1 errors.

Effects, comparing reports for the **same spec** before and after the
annotation lands. This is not a comparison between a spec with and without
`ties[]`: declaring ties already changes `erc_coverage`, can add
`erc.missing_tie`, and a degenerate tie can move `erc_status` to
`clean_partial` today (B3, B4, B6). §9 tests the two comparisons
separately.

| Output | Effect |
|---|---|
| `erc_findings` set, rule ids, descriptions, `erc_finding_count` | **None**: the annotation only adds keys to an existing finding |
| `status`, `erc_status`, `coverage`, `erc_coverage`, exit code | **None**: not an input to either roll-up |
| `gates[]`, antenna `levels[]` and verdicts | **None**: the primary graph is untouched and no graph gains a conductor |
| `nets[]` report entries | **None** (see §9 for a deliberate non-goal) |
| `ties[]`/`erc.missing_tie` | **None**: the tie graph is read for regions only |
| `provenance` | No new block: the inputs are `nets[]`/`ties[]`, already pinned by `provenance.spec.content_hash` |
| Compatibility | Additive keys on an existing finding object. No `schema_version` bump (`docs/json-contract.md`, "additive envelope"). A run without `ties[]` adds three `null`-valued keys and nothing else. |

## 7. Q4: does the #2180 case demonstrate well conduction?

**No, not on the evidence available, and this spike does not claim it is
solved.**

- The original GDS, spec, and LVS deck are unavailable (§3.1).
- #2180's own investigation found that the n-well under the two smaller
  islands was **not** part of the main island's merged well polygon. E2
  shows that a well-continuity rule leaves exactly that geometry split: 2
  islands with `well_via_tap`, and 2 VPWR nets even under this repo's
  device-aware sky130 extraction. A per-well merge, the only form §5
  justifies, would therefore not have turned #2180's 3 islands into 1.
- Explanations consistent with "ERC 3 islands, LVS 1 net" that do **not**
  involve conduction through one well. All are unverified (U1):
  1. **Global substrate.** If the net is a substrate-tapped ground, a
     device-aware deck joins every substrate tap die-wide (E4, B7
     `connect_global`). No drawn polygon marks the substrate, so a
     per-polygon rule cannot express this.
  2. **Name-joining in the LVS deck.** `connect_implicit`-style rules join
     same-named disjoint islands. `docs/cli/erc.md` already documents this,
     and a name-joined match is not evidence of continuity.
  3. **An undeclared conductor** in the ERC stackup (for example a local
     interconnect or a second contact layer). Since #2389,
     `erc_coverage.layers_in_stream_without_declaration` names any such
     drawn layer. That is the first thing to check on a re-run.
  4. **Device-mediated joining** in the reference comparison (for example a
     body terminal tied to the supply), which a device-aware compare
     tolerates and a wire-only graph cannot see.

**The different, real use case this spike identifies** is E1: islands of
one supply that each tap the same merged well. Device-aware LVS calls them
one node, ERC calls them several, and today nothing in the ERC report says
which islands are like that. The §6.2 annotation answers exactly that per
island, without taking a position on whether the well path is acceptable.

## 8. Alternatives compared and the recommendation

| | A. Retain present model + LVS cross-check | B. Narrow primary-graph extension | C. Separate net-findings graph | **D. A + reporting-only well-bridge annotation (recommended)** |
|---|---|---|---|---|
| Fixes E1 (same well) `unconnected_net` | No | Yes (P2) | Yes (P2) | No, by design. Annotates it as one bridge group. |
| Fixes E2 (#2180's described geometry) | No | No | No | No. Annotates it as separate groups, which is itself useful evidence. |
| Handles E4 (substrate) | No | No | No (cannot without asserting the die as a conductor) | Only with asserted `well_boxes`, flagged as an assertion |
| CMOS S/D safety (E3) | Safe | Safe only if the tap is non-degenerate (P3) | Same as B | Safe: no conductor added, and degenerate ties are excluded |
| `gates[]`/antenna | Unchanged | **Contaminated** (P4) | Unchanged | Unchanged |
| `ties[]` isolation (B6) | Kept | **Broken** | Kept, with a third graph | Kept (read-only regions) |
| Severed rail sharing a well (P6) | Reported | **Hidden** | **Hidden** | Reported, and flagged as a bridge candidate |
| New spec surface | None | New key | New key + validation (§6.1) | None |
| Runtime | — | +0 extractions | +1 extraction when declared | +0 extractions; region ops per island of failing nets only, and only when `ties[]` exists (the tie graph is already built) |
| Overlap with `klt extract`/`klt lvs` | Complementary | Duplicates B7 badly (no device recognition) | Duplicates B7 | Complementary: ERC keeps the metal-route view and points at what LVS merged |

**Recommendation: D.** The primary graph stays authoritative, and nothing
merges. The follow-up adds the §6.2 annotation.

Explicit assumptions:

- `erc.unconnected_net` is meant to report **metal-routed** supply
  continuity (structural power delivery, `docs/design-evidence-tiers.md`
  item 11). Device-aware continuity is `klt lvs`'s job. If the project
  decides ERC should instead mirror LVS node identity, B and C have to be
  re-evaluated (§10).
- Declared `ties[]` taps are the only trustworthy "this is a tap" signal
  available without device recognition (B4).
- Callers who confirm an intended partition keep using `nets[].islands`.
  That is an expected count, not continuity evidence, and the annotation
  supplies the evidence beside it.

Rejected alternatives, and why:

- **A alone.** It is safe, but it leaves triage entirely to an external LVS
  run, and E1 shows that run will "confirm" a severed rail. A is the
  baseline that D extends.
- **B.** It breaks the #2169 isolation and contaminates antenna (P4), hides
  severed rails (P6), and does not fix the motivating #2180 geometry (E2).
- **C.** It is gate/antenna-safe, but it still converts a resistive
  body-bias path into a clean supply verdict (P6). It cannot express
  substrate continuity (E4), costs a third extraction, and duplicates
  `klt extract`'s device-aware answer without its device recognition.
- **Blanket diffusion or well conductors.** Disproved directly (P3; E3
  `diff_conductor` and `well_via_licon`).

Limitations of D, stated plainly:

- It does not make any false `erc.unconnected_net` go away. A caller with a
  confirmed body-bias-only partition still declares `nets[].islands`.
- It says nothing about resistance or current. A bridge group is a hint,
  not a verdict.
- It needs `ties[]`. A spec without ties gets `null` annotations, not an
  inference from undeclared layers.
- It inherits `ties[]`'s geometric limits: no deep-n-well modelling beyond
  class selection (U4), and nothing for designs whose LVS merge comes from
  name-joining or device terminals (§7).

## 9. Follow-up: implementation and test outline

To be filed as a separate issue ("ERC: report well-bridge groups on
multi-island `erc.unconnected_net`"), whose acceptance criteria come from
§6.2. Estimated scope is **small to medium**: about 80–150 lines in
`src/klayout_tools/erc.py`, about 6 tests, and doc updates. There are no
new spec keys, and the risk is low because no graph or verdict changes.

Implementation:

1. In `run_erc`, keep the `tie_layers` returned by the existing tie
   extraction (today discarded after `_tie_findings`). Compute
   `degenerate_ties` before the annotation (already done today) and filter
   it out.
2. Pass the non-degenerate tie layers into `_net_connectivity_findings`, or
   apply a post-pass over the multi-island findings it returns, keyed by the
   matched primary nets. That keeps `_net_connectivity_findings`' signature
   change local. Mind the C901 ratchet: put the work in a new helper,
   `_well_bridge_annotation(l2n, layer_index, matched_nets, tie_layers)`.
3. Per island and per basis tie: `island_conn =
   l2n.polygons_of_net(net, layer_index[tie["connect_to"]])`. For each
   merged `well_poly` of `tie["well_region"]`, record `(tie, well_bbox)`
   when `tie["tap_region"] & well_poly` interacts with `island_conn`. Union
   the islands that share any key into `well_bridge_groups`.
4. Emit `null`s per §6.2 when there are no basis ties. Do not touch
   `erc_coverage`, the roll-ups, `gates[]`, or `provenance`.
5. Docs: in `docs/cli/erc.md`, document the three fields in the JSON schema
   table and in "Locating the islands of a multi-island
   `erc.unconnected_net`". Update "Known false-positive: diffusion/well
   continuity" to point at the annotation as the first triage step, keeping
   the LVS cross-check text. Add a `CHANGELOG.md` entry.

Tests (in `tests/test_erc.py`, each with its expected outcome):

| Test | Fixture | Expected |
|---|---|---|
| same-well bridge | E1 + non-degenerate tie | `erc.unconnected_net` still emitted, 2 islands; `well_bridge_groups == [[0, 1]]`; both islands share one `tapped_wells` entry |
| separate wells | E2 + tie | finding unchanged; `well_bridge_groups == [[0], [1]]`; different `well_bbox`es |
| no ties | E1, no `ties[]` | the three fields are `null`; every other key byte-identical to today's output |
| degenerate tie excluded | E1 + tie with an un-narrowed `tap_layer` naming licon (66/44) | fields `null` (the only tie is excluded); `erc_coverage.skipped` unchanged |
| ties isolation (with vs. without `ties[]`) | extend `test_ties_never_alter_gates_or_net_findings`: E1 and E2, each run with no `ties[]` and with a non-degenerate tie | `gates[]`, antenna `levels[]` and verdicts, and rule/net/description of every finding **except `erc.missing_tie`** are identical across the two runs; the three annotation keys are ignored. `erc_finding_count`, `status`, `erc_status`, `erc_coverage` and the exit code are deliberately **not** compared: declaring ties already changes them today (`_connectivity_coverage` takes the tie lists, `erc.py` ~4494–4501; `_tie_findings` can add `erc.missing_tie`, ~4385; a degenerate tie can move `erc_status` to `clean_partial`, B4) |
| annotation non-regression (same spec, before vs. after) | one spec per case, `ties[]` included: E1 + correct tie, E1 + a tie the layout does not satisfy (negative control: `erc.missing_tie` emitted), E1 + degenerate tie (un-narrowed licon `tap_layer`) | the report with the annotation, after deleting `tapped_wells`, `well_bridge_groups` and `well_bridge_basis` from every finding, is byte-identical to the pre-change report for the same spec: every finding, `erc_finding_count`, `status`, `erc_status`, `erc_coverage`, `gates[]`/antenna, and the exit code. Pin the pre-change reports as golden JSON in the same PR |
| CMOS non-regression | E3 + tie | no `erc.unconnected_net`, so no annotation; zero findings, as today |

Explicit non-goals for the follow-up: no merge in any graph, no new spec
key, no change to `nets[]` report entries (a passing net with declared
`islands: N` carries no `islands[]` to annotate; extend it only if a
consumer asks), and no resistance estimate.

## 10. When to reopen the net-merging question

Reopen §6.1 (option C, never B) only if one of these appears with a
reproducible artifact:

- A real layout where the islands of a supply are joined **only** through
  one well polygon, the designer asserts that this is intended, and
  `nets[].islands` plus the annotation is shown to be insufficient for that
  consumer's signoff flow.
- A project decision that `erc.unconnected_net` should mirror device-aware
  LVS node identity rather than metal-routed continuity. That changes the
  assumption in §8 and needs its own architecture note.

Until then, `docs/cli/erc.md`'s "Known false-positive: diffusion/well
continuity is not modeled (issue #2180)" limitation and its LVS cross-check
guidance remain the documented behavior.
