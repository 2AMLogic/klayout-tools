# Resource library

A standing index of external resources (papers, courses, upstream repos,
threads) this project has mined for adoptable practice — and, for each one,
what actually changed here because of it. This is not a bookmarks folder:
an entry exists because a doc, a `klt` check, a review rubric line, a
canary convention, or a `spec-review` reference file traces back to it. An
entry that never produced a practice change is a pruning candidate.

**Why this exists.** Operator direction on
[#1014](https://github.com/2AMLogic/klayout-tools/issues/1014)
(2026-08-16) established that mining the outside world (see
[`../ARCHITECTURE.md`](../ARCHITECTURE.md) → "Mining the outside world")
should feed a *standing* library that grows with the tools, not one-off
survey documents with no shared index. That direction landed 11 minutes
before #1014's own PR
([#1019](https://github.com/2AMLogic/klayout-tools/pull/1019)) merged, so
that PR shipped a standalone survey and this document is the follow-up that
builds the library structure and backfills it from every survey filed
before it existed.

## How to read an entry

The index (§4) is grouped by originating survey — each group's heading
names the `docs/design/*-survey.md` document plus its own filing issue and
shipping PR, so the survey's own provenance is stated once per group rather
than repeated per row. Within a group, every resource row has the same
shape:

| Field | Meaning |
| --- | --- |
| **Resource** | Title/author (or project name), linked to its canonical public location if one exists. |
| **Provenance & licensing** | Where it was actually fetched/read from, and its license or access posture — see "Licensing rules" below. An informally-shared resource (a personal cloud-storage link, an unlicensed forum post) is marked **informal provenance** and is never cited as if it were the canonical source; the entry attributes findings to primary, citable literature instead wherever one exists. |
| **Informs** | Which toolkit area(s) the resource is relevant to (e.g. analog sizing, digital P&R, corpus fixtures). |
| **Landing spot(s)** | One or more values from the fixed vocabulary in §3, each with a concrete link — the actual doc, `klt` field, rubric file, canary fixture, or spec-review reference file the resource produced. **"No landing spot yet"** is a valid, honest value — most surveys propose more than they ship in one pass; a landing spot only appears here once it is actually merged, not once it is proposed. |

## 1. Intake convention

How a new resource enters this library, end to end:

1. **Propose it.** File an issue using this short template (unlabeled is
   fine — the normal curation pipeline promotes it):

   ```markdown
   ## Resource
   <title, author/org, link if public>

   ## Why mine it
   <what toolkit area this might inform, and why now>

   ## Access
   <public URL / how you obtained it — flag informal provenance up front>
   ```

2. **A mining pass delivers three things, not just a write-up:**
   - A `docs/design/<slug>-survey.md` document — what was actually read
     (state evidence tier: fetched/run directly vs. read from memory vs.
     inferred from a title), what it covers, and which findings are
     adoptable practice vs. "not adopted, here is why."
   - One or more entries in **this file** (§4), added or updated in the
     same PR as the survey — every survey earns at least one entry the day
     it lands, even if every landing spot starts as "no landing spot yet."
   - Follow-up issues for each adoptable finding, filed unlabeled per the
     normal pipeline (a survey does not itself authorize implementation —
     see `../ARCHITECTURE.md` → "How capabilities arrive").
3. **Landing spots get backfilled as they ship.** When a follow-up issue's
   PR merges, update the entry's landing spot from "no landing spot yet"
   (or add a new landing-spot row) to the concrete doc/check/rubric/
   convention/reference-file link, citing the merging PR. This is the step
   that keeps the library a *working* index instead of a snapshot frozen at
   survey time — see "Pruning" below for the other half of that discipline.

### Pruning

An entry with **no landing spot after a reasonable review window** (its
follow-up issues are closed as not-planned, or nothing was ever filed) is a
pruning candidate, not a permanent fixture — remove it or mark it "declined:
\<reason\>" rather than leaving it to accumulate as unexamined bookmarks.
"Not adopted, here is why" is itself a legitimate, keepable outcome (see the
lambdalib entry in §4.2) — pruning targets entries with *no stated outcome
at all*, not entries that were considered and declined with a rationale.

### Licensing rules

- **Own words, always.** Extract methodology, findings, and rules of thumb
  in this repo's own words — never quote a source at length or reproduce
  its figures/tables verbatim.
- **Attribution always.** Cite the resource by title/author (and a public
  URL when one exists) wherever a finding is drawn from it, even when the
  substance is re-derived from primary literature instead of the resource
  itself (see the analog-resource entries in §4.1 for the concrete pattern:
  attribute to Silveira/Flandre/Jespers 1996 and Pelgrom et al. 1989, not to
  an informally-shared course PDF that happens to teach the same material).
- **No vendored copyrighted material.** Never commit a source PDF, a
  verbatim slide excerpt, or any other copyrighted file into this repo.
- **Informal-provenance sources are never cited as canonical.** A personal
  cloud-storage share, an unlicensed forum/Reddit link, or anything without
  a stable public URL and a clear license is marked **informal provenance**
  in its entry and is *not* treated as the citable source for a finding —
  attribute to primary open literature or the resource's own institution's
  public offering instead, when one exists (see §4.1's Krishnapura/NPTEL
  row for the "independently confirmed public" case, and its Reddit-thread
  row for the "confirmed inaccessible, described honestly" case).

## 2. Where the surveys themselves live

The ten seed surveys backfilled below (and every survey filed after them)
stay at `docs/design/*-survey.md` — **not moved under `docs/library/
surveys/`.** This library indexes them rather than relocating them: at
backfill time, the ten survey files are referenced by path from 30+
other repo files (`README.md`, `ROADMAP.md`, `docs/README.md`,
`docs/cli/*.md`, plus several `.py`/`.rs` module docstrings and test
fixtures — 58 occurrences across 34 files, not counting `docs/design/`
itself) and cross-reference *each other* another 20+ times (each later
Epic #700/#704 phase survey explicitly requires reading its predecessor
first — see e.g. `native-routing-survey.md`'s "Required prior art" section).
Moving them would mean rewriting all of those links for no functional
gain — the index below is exactly as useful pointing at
`docs/design/<slug>-survey.md` as it would be pointing at
`docs/library/surveys/<slug>-survey.md`. A future survey is free to be
filed directly under `docs/library/surveys/` if a later contributor decides
the split is worth it; nothing here forecloses that.

## 3. Landing-spot vocabulary

A fixed enum of where an adopted practice can land, so entries are
comparable across very different resource types:

| Landing spot | What it means | Example |
| --- | --- | --- |
| **doc** | A `docs/**` file (design note, CLI reference, guidance doc). | `docs/design/matching-and-floorplanning.md` |
| **`klt` check** | A new/extended field, flag, or verb in the `klt` CLI itself. | `klt extract --matched-group`, `klt size`'s `target.vds_v` |
| **review rubric** | A layout-economy or design-review rubric line (e.g. the #1013 layout-economy skill). | The Pelgrom-law matching-cost threshold in `.claude/skills/economy-review/SKILL.md` (added by [#1024](https://github.com/2AMLogic/klayout-tools/pull/1024)) |
| **canary convention** | A corpus fixture, canary test, or regeneration recipe under `tests/corpus/`. | The `gcd` macro fixture LVS canary from the scgallery/zerosoc survey |
| **spec-review reference file** | A `.claude/skills/spec-review/references/*.md` block-class checklist. | `references/ota-amplifier.md`, `references/comparator.md` (added by [#1027](https://github.com/2AMLogic/klayout-tools/pull/1027) — the first resource to land in exactly this category) |

## 4. Index

### 4.1 Analog-VLSI design methodology — from the r/chipdesign resource thread

Survey: [`docs/design/analog-resource-survey.md`](../design/analog-resource-survey.md)
([#1014](https://github.com/2AMLogic/klayout-tools/issues/1014), survey PR
[#1019](https://github.com/2AMLogic/klayout-tools/pull/1019)).

| Resource | Provenance & licensing | Informs | Landing spot(s) | 
| --- | --- | --- | --- |
| r/chipdesign analog-VLSI course-note thread (7 resources: Sahoo & Mandal (IIT-KGP), Zele (IIT-B), Maity (IIT-KGP), two unattributed Drive folders, Krishnapura's NPTEL course) | **Informal provenance** — six of the seven are professors' course notes shared via personal Google Drive links, not a licensed release. Two were directly fetched and inspected: Sahoo's notes have a text layer that OCRs to unusable fragments (photographed handwriting); Mandal's are image-only with no text layer at all — **the honest access note this entry exists to record.** The seventh (Krishnapura, NPTEL course 108106080) was independently confirmed public via search and is cited by course name/number only, never mirrored. | Analog sizing methodology, matching, OTA/comparator design review | No landing spot directly — the thread is not citable as canonical (see licensing rules); its methodology is extracted instead via the primary literature rows below, which *do* have landing spots. |
| Silveira, Flandre, Jespers — "A gm/ID based methodology for the design of CMOS analog circuits and its application to low-voltage and low-power design," *IEEE JSSC*, 1996 | Primary literature, cited by title/author, not vendored. | `klt size` sizing methodology | **klt check** — `klt size`'s fixed-`Vds` gm/Id lookup-table mode (`target.vds_v`), closing the diode-connected-only limitation the survey found: [#1015](https://github.com/2AMLogic/klayout-tools/issues/1015), shipped in [#1025](https://github.com/2AMLogic/klayout-tools/pull/1025) ([`docs/cli/size.md`](../cli/size.md) → "Method"). |
| Pelgrom, Duinmaijer, Welbers — "Matching properties of MOS transistors," *IEEE JSSC*, 1989 | Primary literature, cited by title/author, not vendored. | Device matching, `klt gen` generator sizing, layout-economy review | **doc** — [`docs/design/matching-and-floorplanning.md`](../design/matching-and-floorplanning.md), the Pelgrom's-law-to-generator-parameter bridge doc: [#1016](https://github.com/2AMLogic/klayout-tools/issues/1016), shipped in [#1026](https://github.com/2AMLogic/klayout-tools/pull/1026). **review rubric** — handed to [#1013](https://github.com/2AMLogic/klayout-tools/issues/1013) (layout-economy review) as a matching-cost-legitimacy threshold, shipped in [#1024](https://github.com/2AMLogic/klayout-tools/pull/1024): `.claude/skills/economy-review/SKILL.md`'s density-expectation table gives matched pairs / current mirrors a 0.35–0.55 typical band and flags below ~0.25 with no matching/isolation rationale, citing Pelgrom-law matching requirements as legitimate area cost. |
| Razavi, *Design of Analog CMOS Integrated Circuits*; Hastings, *The Art of Analog Layout* | Primary literature (textbooks), cited by title/author, not vendored. | Amplifier/OTA/comparator design review; post-layout matched-pair verification | **spec-review reference file** — `references/ota-amplifier.md` and `references/comparator.md`, grounded in this repo's own `kb/entries/*-ota.json`/`*-comparator.json` KB entries plus this literature: [#1017](https://github.com/2AMLogic/klayout-tools/issues/1017), shipped in [#1027](https://github.com/2AMLogic/klayout-tools/pull/1027). **klt check** — `klt extract --matched-group`, flagging a declared matched pair (differential-pair leg, current-mirror leg) whose extracted `W`/`L`/multiplier diverge post-layout, per Hastings' matched-pair-verification practice: [#1018](https://github.com/2AMLogic/klayout-tools/issues/1018), shipped in [#1046](https://github.com/2AMLogic/klayout-tools/pull/1046) (`docs/cli/extract.md` → "Matched-device geometry check"). |

### 4.2 lambdalib — evaluated, not adopted

Survey: [`docs/design/lambdalib-survey.md`](../design/lambdalib-survey.md)
([#39](https://github.com/2AMLogic/klayout-tools/issues/39), PR
[#50](https://github.com/2AMLogic/klayout-tools/pull/50)).

| Resource | Provenance & licensing | Informs | Landing spot(s) |
| --- | --- | --- | --- |
| [siliconcompiler/lambdalib](https://github.com/siliconcompiler/lambdalib) (Zero ASIC Corp) | Public GitHub repo, MIT license (the survey corrected #39's own "Apache-2.0" claim after reading `LICENSE` directly). | RTL cell library for the digital flow; candidate corpus-fixture source | **doc** — declined-with-rationale, not adopted: no dependency, no `klt` subcommand, no KB entry (pure RTL with no layout data of its own, and its path to GDS runs through a synthesis+APR toolchain this repo does not schedule). Its one actionable thread ("compiled via scgallery's rtl2gds flow, could produce full-chip corpora") was left as a cross-reference comment on [#40](https://github.com/2AMLogic/klayout-tools/issues/40) rather than a competing issue — see the scgallery/zerosoc entry below. |

### 4.3 Mixed-signal co-simulation approach — RNM vs. XSPICE `d_process` vs. Verilog-AMS

Survey: [`docs/design/co-simulation-approach-survey.md`](../design/co-simulation-approach-survey.md)
([#395](https://github.com/2AMLogic/klayout-tools/issues/395), part of Epic
[#393](https://github.com/2AMLogic/klayout-tools/issues/393), PR
[#407](https://github.com/2AMLogic/klayout-tools/pull/407)).

| Resource | Provenance & licensing | Informs | Landing spot(s) |
| --- | --- | --- | --- |
| Real-number modeling (RNM), ngspice XSPICE `d_process`, Verilog-AMS/VHDL-AMS — surveyed as three candidate mixed-signal co-simulation approaches (general EDA technique families, not single papers) | Public/open-source tooling survey (Icarus Verilog, ngspice/XSPICE) plus general field knowledge of the proprietary Verilog-AMS/VHDL-AMS landscape. | Mixed-signal (analog+digital) co-simulation | **doc** — the survey's own recommendation ("RNM for v1") is recorded and was the basis Epic #393 closed against for its Phase 1 success criterion. **No `klt` check yet**: Epic #393's actual Phases 2–3 ([#438](https://github.com/2AMLogic/klayout-tools/issues/438), [#456](https://github.com/2AMLogic/klayout-tools/issues/456)) built LEF-abstract emission and cross-domain signoff instead of the RNM co-simulation contract itself — the proposed JSON contract in this survey's §3 has not been implemented. Recorded here honestly as a decision made but not yet built, per this library's own "entries earn their place by impact" standard. |

### 4.4 OpenROAD invocation surface

Survey: [`docs/design/openroad-invocation-survey.md`](../design/openroad-invocation-survey.md)
([#397](https://github.com/2AMLogic/klayout-tools/issues/397), part of Epic
[#391](https://github.com/2AMLogic/klayout-tools/issues/391), PR
[#410](https://github.com/2AMLogic/klayout-tools/pull/410)).

| Resource | Provenance & licensing | Informs | Landing spot(s) |
| --- | --- | --- | --- |
| OpenROAD / OpenROAD-flow-scripts (ORFS) — CLI/Tcl invocation surface, floorplan input requirements, DEF/GDS output shape | Public, BSD-licensed open-source tool; a real `openroad/orfs:latest` container run (Docker, `linux/amd64` emulation) was executed directly for this survey — not read from docs alone. | `klt place-and-route`'s OpenROAD wrap | **klt check** — the survey is the direct engine-invocation-surface input to the shared digital contracts spike ([#399](https://github.com/2AMLogic/klayout-tools/issues/399), PR [#414](https://github.com/2AMLogic/klayout-tools/pull/414)) and to `klt place-and-route` itself, shipped in PR [#431](https://github.com/2AMLogic/klayout-tools/pull/431) (`docs/cli/place-and-route.md`). |

### 4.5 `klt place-and-route` improvements (Epic #700 Phase 1) — placement, legalization, CTS

Survey: [`docs/design/place-and-route-improvements-survey.md`](../design/place-and-route-improvements-survey.md)
([#735](https://github.com/2AMLogic/klayout-tools/issues/735), part of Epic
[#700](https://github.com/2AMLogic/klayout-tools/issues/700), PR
[#741](https://github.com/2AMLogic/klayout-tools/pull/741)).

| Resource | Provenance & licensing | Informs | Landing spot(s) |
| --- | --- | --- | --- |
| Cheng, Kahng, Kang, Wang — "RePlAce: Advancing Solution Quality and Routability Validation in Global Placement," *IEEE TCAD*, 2019 (the algorithm behind OpenROAD's `gpl`, already wrapped) | Primary literature, cited by title/author. | Global placement quality | **klt check** — routability-driven and timing-driven `global_placement` flags wired into `klt place-and-route`'s `place` stage: shipped in PR [#752](https://github.com/2AMLogic/klayout-tools/pull/752). |
| Spindler, Johannes — "Abacus: Fast Legalization of Standard Cell Circuits with Minimal Movement," *ISPD*, 2008 | Primary literature, cited by title/author. | Native-Rust standard-cell legalization, oracle-gated against OpenROAD's `opendp` | **doc** — spiked as a native-Rust legalizer, verdict **No-go** on QoR grounds (HPWL/displacement 21.8–403.4% worse than OpenROAD's own legalizer on the corpus slice): PR [#796](https://github.com/2AMLogic/klayout-tools/pull/796), documented in `native/legalize/README.md`. A declined-with-evidence outcome, not a silent drop. |
| repair_timing -hold / repair_antenna (general post-CTS/post-route EDA signoff practice, not a single citable paper) | General field practice, cited by technique name. | Post-CTS hold-timing repair; post-route antenna-violation repair | **klt check** — post-CTS hold repair shipped in PR [#753](https://github.com/2AMLogic/klayout-tools/pull/753); post-route antenna repair + `antenna_violation_count` field shipped in PR [#762](https://github.com/2AMLogic/klayout-tools/pull/762) (issue [#759](https://github.com/2AMLogic/klayout-tools/issues/759)). |
| TritonCTS sink-clustering / obstruction-aware clock-tree synthesis (general clock-tree literature, OpenROAD's own CTS engine) | General field practice + OpenROAD source, cited by technique name. | Clock-tree synthesis quality, clock-skew reporting | **klt check** — sink-cluster/obstruction-aware CTS + a new `clock_skew_ns` metric: shipped in PR [#794](https://github.com/2AMLogic/klayout-tools/pull/794) (issue #783). |
| Chu, Wong — "FLUTE: Fast Lookup Table Based Rectilinear Steiner Minimal Tree Algorithm for VLSI Design," *IEEE TCAD*, 2008 | Primary literature, cited by title/author. | Congestion pre-check ahead of full routing | **klt check** — a native-Rust FLUTE/RUDY congestion pre-check: shipped as a research spike in PR [#813](https://github.com/2AMLogic/klayout-tools/pull/813) (issue #785) — a validated library, not yet wired into the fleet gate. |

### 4.6 Native-Rust detailed routing (Epic #700 Phase 2)

Survey: [`docs/design/native-routing-survey.md`](../design/native-routing-survey.md)
([#934](https://github.com/2AMLogic/klayout-tools/issues/934), part of Epic
[#700](https://github.com/2AMLogic/klayout-tools/issues/700), PR
[#935](https://github.com/2AMLogic/klayout-tools/pull/935)).

| Resource | Provenance & licensing | Informs | Landing spot(s) |
| --- | --- | --- | --- |
| Lee — "An Algorithm for Path Connections and Its Applications," *IRE Trans. Electronic Computers*, 1961; Hightower — "A solution to line-routing problems on the continuous plane," *DAC*, 1969; McMurchie, Ebeling — "PathFinder: A Negotiation-Based Performance-Driven Router for FPGAs," *FPGA*, 1995 | Primary literature (foundational routing algorithms), cited by title/author; already embedded in this repo only indirectly, via the OpenROAD/TritonRoute wrap this survey audits rather than replaces. | Detailed-routing algorithm background for the native-vs-wrap decision | **doc** — background grounding for this survey's own recommendation ("proceed to routing, staged cautiously"); no direct `klt` code traces to these papers individually, since the survey's actual proposal reuses TritonRoute rather than reimplementing maze/line-probe/negotiated-congestion routing natively. |
| This survey's own measurement-and-reporting proposal (§4.5), grounded in this repo's own `detailed_route -output_drc` evidence | This repo's own data (`native/legalize/README.md`'s prior QoR result; a real `openroad/orfs:latest` run). | `klt place-and-route` DRC-violation reporting; routing-flag coverage | **klt check** — `route_drc_violation_count` field + a regenerated `mult8` corpus fixture: shipped in PR [#941](https://github.com/2AMLogic/klayout-tools/pull/941). **klt check** — an audit of timing-driven/repair-iteration routing flags: shipped in PR [#940](https://github.com/2AMLogic/klayout-tools/pull/940). |

### 4.7 Post-route static timing (Epic #700 Phase 3)

Survey: [`docs/design/post-route-sta-survey.md`](../design/post-route-sta-survey.md)
([#944](https://github.com/2AMLogic/klayout-tools/issues/944), part of Epic
[#700](https://github.com/2AMLogic/klayout-tools/issues/700), PR
[#945](https://github.com/2AMLogic/klayout-tools/pull/945)).

| Resource | Provenance & licensing | Informs | Landing spot(s) |
| --- | --- | --- | --- |
| SDF (Standard Delay Format) back-annotation practice; multi-corner setup/hold sign-off convention ("setup at slow PVT, hold at fast PVT") — general post-route STA/signoff field practice | General field practice + OpenROAD/Icarus Verilog source, cited by technique/format name. | Post-route static timing, gate-level re-simulation with back-annotated delays | **klt check** — a corner-swept `worst_setup_slack_ns`/`worst_hold_slack_ns` field sweeping every shipped PVT corner: shipped in PR [#955](https://github.com/2AMLogic/klayout-tools/pull/955) (issue #949). **klt check** — an Icarus `$sdf_annotate` feasibility spike recording a live **Go**: PR [#969](https://github.com/2AMLogic/klayout-tools/pull/969). **klt check** — the optional `post_route_sdf` request field, writing post-route SDF and back-annotating it in gate-level re-simulation: shipped in PR [#1007](https://github.com/2AMLogic/klayout-tools/pull/1007) (issue #1002). |

### 4.8 scgallery / zerosoc — test-corpus fixture sources

Survey: [`docs/design/scgallery-zerosoc-survey.md`](../design/scgallery-zerosoc-survey.md)
([#40](https://github.com/2AMLogic/klayout-tools/issues/40), PR
[#48](https://github.com/2AMLogic/klayout-tools/pull/48)).

| Resource | Provenance & licensing | Informs | Landing spot(s) |
| --- | --- | --- | --- |
| [siliconcompiler/scgallery](https://github.com/siliconcompiler/scgallery) and [siliconcompiler/zerosoc](https://github.com/siliconcompiler/zerosoc) | Public GitHub repos, Apache-2.0 license (checked directly against `LICENSE`). Neither ships a pre-built GDS/OASIS file — both are RTL-to-GDS build *recipes*, checked against all 19 releases (tags only, no build artifacts). | `tests/corpus/` macro/SoC-scale GDS fixtures | **canary convention** — `klt lvs` verified against a machine-generated GCD macro fixture, the trigger named in issue [#389](https://github.com/2AMLogic/klayout-tools/issues/389) resolved without needing scgallery/zerosoc directly: shipped in PR [#483](https://github.com/2AMLogic/klayout-tools/pull/483). The survey's own recommendation (generating a fixture via the full toolchain is a legitimate but separate EDA-install project) stands as the reason no fixture was vendored directly from either repo. |

### 4.9 siliconcompiler core — flow orchestration + KLayout driver

Survey: [`docs/design/siliconcompiler-core-survey.md`](../design/siliconcompiler-core-survey.md)
([#38](https://github.com/2AMLogic/klayout-tools/issues/38), PR
[#51](https://github.com/2AMLogic/klayout-tools/pull/51)).

| Resource | Provenance & licensing | Informs | Landing spot(s) |
| --- | --- | --- | --- |
| [siliconcompiler/siliconcompiler](https://github.com/siliconcompiler/siliconcompiler) core (Flowgraph DAG orchestration, `tools.klayout` driver, `PathSchema`/dataroot resolver) | Public GitHub repo, Apache-2.0 license; source read directly (`flowgraph.py`, `tools/klayout/{__init__,drc,export}.py`, `schema_support/pathschema.py`, `package/{__init__,github}.py`) on 2026-07-30. | Whether to adopt the framework for this project's own digital-flow orchestration | **doc** — "take ideas, not the framework" verdict, reconfirmed after Epic #391 independently adopted the Yosys+OpenROAD digital engine class. Fed the three-peer-paths reconciliation across `ROADMAP.md`/`README.md`/`ARCHITECTURE.md`: PR [#401](https://github.com/2AMLogic/klayout-tools/pull/401). |

### 4.10 `klt synthesize` QoR improvements (Epic #704)

Survey: [`docs/design/synthesize-qor-improvements-survey.md`](../design/synthesize-qor-improvements-survey.md)
([#736](https://github.com/2AMLogic/klayout-tools/issues/736), part of Epic
[#704](https://github.com/2AMLogic/klayout-tools/issues/704), PR
[#748](https://github.com/2AMLogic/klayout-tools/pull/748)).

| Resource | Provenance & licensing | Informs | Landing spot(s) |
| --- | --- | --- | --- |
| ABC (Berkeley logic-synthesis/verification system) constraint-driven technology mapping — the engine `klt synthesize` already wraps via Yosys | Public, open-source tool already wrapped; behavior read from its own invocation surface (`docs/design/yosys-synthesis-spike.md`, #396). | Gate-level QoR (area/delay) tuning | **klt check** — driving ABC with a constraint file, a delay target, and a cell-exclusion list: shipped in PR [#822](https://github.com/2AMLogic/klayout-tools/pull/822). |
| Liberty-driven native technology mapping (general technique family, motivated by measured Yosys/ABC QoR gaps this survey found) | This repo's own measurement, per the survey's evidence-tier discipline. | Native-Rust technology mapping as a Yosys/ABC alternative | **klt check** — `native/techmap`, a Liberty-driven native tech-mapper: shipped in PR [#886](https://github.com/2AMLogic/klayout-tools/pull/886). |
| Native-Rust gate-level static timing analysis, spiked against OpenSTA as the oracle | This repo's own spike, oracle-gated per the survey's §3.7 recommendation. | Pre-layout gate-level STA inside `klt synthesize`'s own restructuring loop | **klt check** — the spike recorded a live **Go**: PR [#830](https://github.com/2AMLogic/klayout-tools/pull/830); the resulting `klt-statime-native` engine (Epic #704 Phase 3, issues #809/#925/#926) is wired into `klt synthesize`'s `sta` field, documented in [`docs/cli/synthesize.md`](../cli/synthesize.md). |
