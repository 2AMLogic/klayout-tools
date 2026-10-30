# Survey: a library of verified analog designs

**Status:** research spike for #4596. Output is a decision document, not code.
No format, schema, CLI verb or MCP tool is proposed for implementation here;
any follow-up is filed at `loom:triage`.

**What was verified vs. inferred.** All external facts below were fetched on
**2026-10-29** from the URLs given: arXiv HTML/abstract pages, GitHub
repository pages and the GitHub REST API (`api.github.com/repos/<owner>/<repo>`,
which reports the license detected from the repo's license file and
`pushed_at`). Items marked **not stated** were absent from what was fetched;
they are not claimed to be absent from the project. The arXiv PDF of AnalogScout
could not be text-extracted in this environment (compressed streams), so item 1
is read from the paper's arXiv HTML rendering. Per-design licenses *inside*
the aggregate repositories (shuttle submissions, per-project subdirectories)
were **not** audited; a repo-level license does not necessarily cover every
vendored file.

## 1. The AnalogScout paper

Source: Long and Zhu, *AnalogScout: Simulation-Efficient PVT-Aware Analog
Sizing with LLM Agents*, arXiv:2610.37319v1
([abstract](https://arxiv.org/abs/2610.37319v1),
[HTML](https://arxiv.org/html/2610.37319v1)); paper license is arXiv
non-exclusive distribution 1.0 (covers the paper only).

| Question from the issue | Finding |
| --- | --- |
| How is the "library" built, stored, indexed, retrieved? | **There is no persistent library.** The paper describes an *elite pool* holding the K = 3 best feasible designs of the current run, ranked by (infeasible, violation, TT power, margin, area), plus an exact-match cache keyed on identical source text (15.5 % of development evaluation requests were repeats). Retrieval is stochastic seeding: a new exploration run starts from elite *j* with probability proportional to exp(-(r_j - r*)/sigma_r) on log-power; fresh starts are rare (floor 5 %). No indexing by spec, topology or PDK is described. |
| What does "verified" mean? | A fixed evaluator solves DC operating point and checks saturation and specs at the typical corner; survivors are checked at **nine PVT corners** (tt; ss, sf, fs at -10 % VDD; ff at +10 % VDD; each at -40 and 125 degC). Feasible = every spec at every corner. Sign-off adds >= 50 mV saturation margin at all corners, typical-corner mismatch Monte Carlo yield >= 95 % (N = 500) and reproduction by independent reference scripts. Simulation only (ngspice); layout is not mentioned. |
| Benchmarks | Miller 2-stage, folded-cascode and 3-stage NMC OTAs on SKY130 (1.8 V); Miller OTA also on GF180MCU (3.3 V) as a test-only PDK. |
| Release and license | The paper says code and open-PDK data "will be released" (future commitment). No repository is named and the license is **not stated**. The arXiv page links no paper-specific code or dataset. |

**Consequence for the premise.** The abstract's "reusable verified designs"
is, in the body, a per-run seed pool of three designs, not a curated
cross-project library. The paper therefore gives evidence that *seeding from
good designs helps within one run* (and a concrete, useful definition of
"verified": 9 corners + saturation margin + MC yield + independent
reproduction), but no evidence about a shared library, and nothing we can
adopt today. The recommendation below does not depend on its release.

## 2. Survey of existing FOSS designs, libraries and datasets

"Agent-consumable today" means: could an agent obtain a design and its
verification evidence with no human curation step.

| Project | PDK(s) | Verified level | Format | License (primary source) | Activity (`pushed_at`) | Agent-consumable today? |
| --- | --- | --- | --- | --- | --- | --- |
| [mabrains/Analog_blocks](https://github.com/mabrains/Analog_blocks) (OTA, bandgap, LDO; DC-DC listed) | SKY130 | README claims blocks "meeting corners"; has `Reports`/`testing` dirs, contents **not inspected**; ngspice | schematics, testbenches, netlists, images | LGPL-2.1 ([API](https://api.github.com/repos/mabrains/Analog_blocks)) | 2022-07-30 (stale) | Partly: netlists + testbenches exist, no machine-readable verification record |
| [CODA-Team/AnalogGym](https://github.com/CODA-Team/AnalogGym) (arXiv:2409.08534) | SKY130 for AMP/LDO; other nodes mentioned | Benchmark, not verified designs: 30 topologies, testbenches + metric extraction scripts for AMP and LDO; example runs tt only; no stored result records seen; ngspice >= 42 advised | SPICE netlists, testbenches, design-variable files | BSD-3-Clause ([API](https://api.github.com/repos/CODA-Team/AnalogGym)) | 2025-10-29 | Yes as sizing benchmark; no verified-design records |
| [nunocclourenco/BAIcO](https://github.com/nunocclourenco/BAIcO) | open tools/models (details not fetched) | Benchmark | not fetched | MIT ([API](https://api.github.com/repos/nunocclourenco/BAIcO)) | 2023-12-18 | Not assessed beyond license/activity |
| AMS-SizingBench (in AutoSizer, arXiv:2602.02849) | SKY130 | 24 circuits; benchmark | **not stated** | paper CC BY 4.0; code/data release and license **not stated** (no repo linked on arXiv page) | n/a | No (no artifact found) |
| SGAnalog (arXiv:2610.03934) | Tiny Tapeout shuttle designs | 273 topologies from open shuttle submissions; schematic-to-netlist and sizing tasks | schematic image + SPICE netlist | paper CC BY 4.0; dataset/code repo **not stated** | n/a | No (no artifact found); but confirms shuttle archives are a viable seed source |
| [idea-fasoc/OpenFASOC](https://github.com/idea-fasoc/OpenFASOC) | SKY130 (hd/hvl), GF180 (Glayout) | Generators (temp sensor, LDO, Glayout opamp); README names DRC/LVS via open_pdks and CI badges for sims; PEX and spec verification **not stated**; silicon status **not stated** on the page | generators (Python) -> GDS/netlist | Apache-2.0 ([API](https://api.github.com/repos/idea-fasoc/OpenFASOC)) | 2025-10-22 | Yes as generators; no per-instance verification record |
| [IHP-GmbH/IHP-Open-DesignLib](https://github.com/IHP-GmbH/IHP-Open-DesignLib) | IHP SG13G2 | Tapeout-organized (2023-2025 folders); analog/digital split, measured results **not stated** | per-tapeout submodules | Apache-2.0 ([API](https://api.github.com/repos/IHP-GmbH/IHP-Open-DesignLib)) | 2026-10-15 | Partly: GDS/designs by tapeout; contents not audited |
| [efabless/caravel_user_project](https://github.com/efabless/caravel_user_project) | SKY130 / GF180 | Template for shuttle projects, not a library; individual projects vary | varies | Apache-2.0 ([API](https://api.github.com/repos/efabless/caravel_user_project)) | 2025-02-25 | No (template; per-project licenses unaudited) |
| [google/skywater-pdk-libs-sky130_bag3_pr](https://github.com/google/skywater-pdk-libs-sky130_bag3_pr) | SKY130 | BAG3 primitives (work in progress per search listing), not designs | BAG3 primitives | not fetched | not fetched | No |
| Masala-CHAI (arXiv:2411.14299) | not stated | ~7,500 textbook schematics converted to netlists; no verification | SPICE | authors state dataset/code open-sourced; license **not fetched** | n/a | Not assessed |

**Leads from the issue.**

- *Open-PDK example and IP collections:* **partly confirmed.** IHP-Open-DesignLib
  exists and is Apache-2.0. The SKY130 and GF180MCU PDK repositories were *not*
  fetched, so any claim about IP collections inside them is **unconfirmed**.
- *OpenFASOC:* **confirmed**, Apache-2.0, active, generators rather than a
  library of fixed designs.
- *Open shuttle user-project archives:* **confirmed to exist** (the
  Google-hosted `shuttle/sky130/mpw-*` mirrors surfaced by search include
  mabrains LDO work; SGAnalog draws on Tiny Tapeout). Per-project licenses and
  verification contents **not audited**.
- *Academic analog benchmark datasets:* **confirmed** (AnalogGym, BAIcO,
  AMS-SizingBench, SGAnalog, Masala-CHAI). Only AnalogGym and BAIcO have a
  verified OSS-licensed repository; the others have no located artifact.

## 3. Gap analysis

What the survey did **not** find (absence in the sources above, not proof of
absence in the world):

1. **A per-design verification record.** No surveyed project ships a
   machine-readable record of corners run, specs met, margins, tool and PDK
   versions and a pinned reproduction recipe. AnalogScout's definition (9 PVT
   corners, saturation margin, MC yield, independent reproduction) is the best
   written criterion found, but it is not published as an artifact.
2. **Silicon-proven vs sim-only marking.** The shuttle/tapeout repos record that
   a design was submitted; measured-silicon evidence was **not stated** on any
   fetched page.
3. **Agent-facing retrieval** by spec, topology or PDK. Everything surveyed is
   browse-by-directory.
4. **License hygiene.** Licenses are repo-level and mixed (LGPL-2.1, BSD-3,
   MIT, Apache-2.0); LGPL-2.1 on netlists is of unclear effect for commercial
   reuse. Per-design provenance inside aggregates is unaudited.
5. **Layout-level evidence.** Benchmarks stop at schematic simulation; only the
   generators (OpenFASOC) and tapeout repos reach GDS, with DRC/LVS/PEX
   evidence not packaged per design.

What already exists and need not be rebuilt: OSS sizing benchmarks (AnalogGym,
BAIcO), generators (OpenFASOC), and design archives (IHP-Open-DesignLib, shuttle
mirrors).

## 4. Recommendation

**Ranked, for the operator:**

1. **(b) A klt-hosted verification-record format plus retrieval CLI, seeded
   from permissively licensed designs.** Recommended.
2. (a) Adopt and contribute to an existing library. Second choice: no
   surveyed project is a verified-design library, so there is nothing to
   contribute *to*; the nearest (AnalogGym, OpenFASOC) are a benchmark and a
   generator set whose maintainers have different goals.
3. (c) Not worth it. Rejected: the gap in section 3 is real and sits on
   klt's existing strengths.

**Why (b).** The unfilled gap is not designs, it is *evidence about designs*
in a form an agent can query and re-check. klt already owns the pieces:
`klt sim` (corners, Monte Carlo), DRC/LVS, extraction and the JSON envelope.

**What klt would add.** A design entry = netlist (and GDS where one exists) +
a verification record that `klt sim` can **re-run to reproduce**: corner
set, specs and measured values, MC yield, tool/PDK versions, and an
evidence-tier marker (sim-only / post-layout / silicon-proven, the last only
with a cited measurement). Retrieval by PDK, topology and spec window, with
JSON output. Reproduction-on-ingest is the differentiator versus a
README-claimed "meets corners".

**Rough scope.** A staged effort, each stage its own issue at `loom:triage`:
(i) record schema + validator, (ii) reproduce-a-record command on
`klt sim`, (iii) 3-5 seed entries, (iv) retrieval verb. Stage (iii) is the
first test of whether records reproduce; stop there if they do not.

**Candidate first PDK:** SKY130 (klt's first PDK; AnalogScout, AnalogGym and
AMS-SizingBench all target it, so seed designs and comparison points exist;
ngspice-based, fitting the existing fleet sim path).

**Seeding.** Prefer entries we write ourselves from textbook topologies
(Miller, folded cascode) and Apache-2.0/BSD/MIT sources after per-file license
checks; avoid LGPL-2.1 material until its reuse terms for netlists are
reviewed. Whether any designs from 2AM work may be released is a separate,
operator-gated decision and is out of scope here.

**Caveats.** The paper's code release is a promise only; if it appears it
could inform the record format but nothing here depends on it. The evidence
for "seeding from verified designs reduces simulations" is from one preprint
on three OTAs with a 3-design in-run pool, so the benefit of a *cross-project*
library is plausible but untested.
