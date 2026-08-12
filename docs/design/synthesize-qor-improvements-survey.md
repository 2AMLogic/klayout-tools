# Survey & proposal: QoR improvements to `klt synthesize`

**Status:** research / proposal, no implementation. Filed for issue #736, a
research-and-propose task under the re-scoped
[Epic #704](https://github.com/2AMLogic/klayout-tools/issues/704) ("RTL
synthesis for klt (Rust) — Verilog→technology-mapped gate netlist, Yosys as
oracle"). This document is the "fresh eyes" input later implementation
issues build from — it does **not** authorise implementation of anything
below.

**What this document does not settle.** Epic #704 carries
`loom:operator-only` and proposes eventually replacing today's
Yosys+ABC-orchestrating command with native-Rust elaboration, logic
optimization, technology mapping, and STA (its own Phases 1–3). This survey
does not presume that outcome. Every item below states its own
wrap-vs-native trade-off explicitly, as data for that decision rather than
an argument already made for Rust — the three highest-priority items here
are flag/flow changes to the existing Yosys invocation with no Rust content
at all, and the one item this survey does recommend building in Rust is
recommended for a reason (§3.7) that has nothing to do with beating ABC.

**Required prior art, read first, not re-derived here:**

- [`docs/design/yosys-synthesis-spike.md`](yosys-synthesis-spike.md) (#396)
  — the accepted Phase 1 spike this command implements: the invocation shape
  (a generated `.ys` script), the `stat -liberty … -json` output-parsing
  recipe, the licensing check, and the §3.5/§3.6 finding that Yosys has no
  working native timing report against a liberty-mapped netlist. §1 below
  characterises what shipped; it does not re-derive that spike.
- [`docs/design/digital-flow-contracts-spike.md`](digital-flow-contracts-spike.md)
  (#399) section 4 — the request/response JSON contract and exit-code table
  `klt synthesize` implements. This document proposes additive response
  fields in two places and flags each as additive, per that spike's posture.
- [`docs/cli/synthesize.md`](../cli/synthesize.md) — the command's own
  contract documentation, the ground truth for §1.
- [`docs/cli/equiv.md`](../cli/equiv.md) — `klt equiv` (#726), the
  correctness-loop mechanism this document's measurement plan (§4) uses.
  Its **combinational-only** scope is a load-bearing constraint on the
  ranking below, not a footnote — see §4.2.
- [`docs/design/place-and-route-improvements-survey.md`](place-and-route-improvements-survey.md)
  (#735) — the sibling survey for the back half of the digital flow, under
  Epic #700. This document deliberately mirrors its structure and
  evidence-tier discipline so the two read as one pair.

## Evidence-tier discipline

Following this repo's own convention (`docs/design-evidence-tiers.md`'s
ladder, and the #735 sibling survey's tiering). Unlike that sibling, **this
task did run the tool**: every number in §1.3 and §3 comes from a real
Yosys 0.68 invocation against a real volare `sky130A` liberty on
2026-08-11. Every claim below is one of:

- **[REPO]** — read directly from this repo's own source/docs, cited by
  file and line.
- **[RUN]** — measured live by this task, on the machine and toolchain
  named in §1.3, reproducible from the exact scripts quoted there. This is
  the strongest tier in this document.
- **[LIT]** — a technique or finding from the published EDA-CAD literature
  or a tool's documented surface, cited by name/venue/year to the best of
  this survey's ability without live library access. Treat exact author
  lists as best-effort — verify against the primary source before citing
  in a paper trail that requires precision.
- **[PROPOSAL]** — this document's own reasoning/recommendation, not a
  claim about the world.

No item below rests solely on an uncited assertion.

## 1. Baseline: what `klt synthesize` does today

### 1.1 The flow

**[REPO]**, from `src/klayout_tools/synthesize.py` and
`docs/cli/synthesize.md`. `klt synthesize` is a pure orchestration layer: it
never implements an elaboration, optimization, or mapping algorithm itself.
It resolves a liberty file via the shared `find_pdk()`/`libs_ref` discovery,
generates one `.ys` script, and runs `yosys -s <script>` as a single
subprocess (`_run_yosys`, `synthesize.py:385`). All actual algorithmic work
— elaboration, technology-independent optimization, FF mapping, and
technology mapping — is Yosys's and its bundled ABC's.

The generated script is fixed — seven lines, no request-derived variation
beyond paths and the top-module name (`_write_script`,
`synthesize.py:347-382`):

```
read_verilog <source>…
hierarchy -check -top <top>
synth -top <top>
dfflibmap -liberty <lib>
abc -liberty <lib>
clean
tee -q -o <stats> stat -liberty <lib> -json -top <top>
write_verilog -noattr <netlist>
```

**Every optimization knob Yosys and ABC expose is left at its default.**
`synth` is invoked with `-top` only; `abc` is invoked with `-liberty` only.
No `-flatten`, no `-D`, no `-constr`, no `-dont_use`, no `-script`, no
`-dff`/`-clk`.

### 1.2 What the defaults actually expand to

**[RUN]** — from `yosys -p 'help synth'` and `yosys -p 'help abc'` on Yosys
0.68+post, and confirmed against the live run logs in §1.3 (the ABC command
echo, `ABC: + …`).

`synth -top <top>` runs `hierarchy` → a `coarse` stage (`proc`, `opt_expr`,
`opt_clean`, `opt -nodffe -nosdff`, `fsm`, `opt`, `wreduce`, `peepopt`,
`alumacc`, `share`, `opt`, `memory -nomap`, `opt_clean`) → a `fine` stage
(`opt -fast -full`, `memory_map`, `opt -full`, `techmap`, `opt -fast`,
**a first bare `abc`** against Yosys's internal gate library, `opt -fast`)
→ `check`. Notably `flatten` runs **only if `-flatten` is passed** — it is
not in the default path.

`abc -liberty <lib>` **without `-constr`** expands to:

```
strash; &get -n; &fraig -x; &put; scorr; dc2; dretime; strash;
      &get -n; &dch -f; &nf {D}; &put
```

`abc -liberty <lib>` **with `-constr`** expands to the same prefix plus a
tail the no-`-constr` form never reaches:

```
… &nf {D}; &put; buffer; upsize {D}; dnsize {D}; stime -p
```

Three consequences follow directly, and each is a QoR lever the command
currently forfeits:

1. **No gate sizing or buffering.** `buffer`, `upsize`, and `dnsize` — ABC's
   load-driven buffer-insertion and drive-strength sizing steps — run
   **only** in the `-constr` variant. Today's netlist is mapped at whatever
   drive strengths `&nf` picked, with no model of the load each net drives
   and no notion of what cell drives the primary inputs.
2. **No delay target.** `{D}` is replaced by `-D <picoseconds>` when `abc
   -D` is given and by an empty string otherwise (`help abc`, **[RUN]**).
   Today it is always empty: `&nf`, `upsize`, and `dnsize` run untargeted.
3. **No timing report at all.** `stime -p` — ABC's own static-timing print,
   the only delay number anywhere in this flow — is likewise `-constr`-only.
   This is why `timing` is `null` in the response contract; §1.4 revisits
   that framing.

`-D` additionally rewrites `dretime` to `dretime; retime -o {D}` (**[RUN]**,
`help abc`, confirmed in the §1.3 `D2000` log where `ABC: + retime -o -D
2000` appears). That retiming step is inert in this flow regardless, because
`dfflibmap` maps every flip-flop to a liberty cell *before* `abc` runs and
`abc` is invoked without `-dff`/`-clk` — so ABC never sees a register and
has nothing to retime across (**[REPO]** for the script order,
`synthesize.py:369-371`; **[LIT]/[RUN]** for the `-dff` semantics).

### 1.3 Measured baseline and A/B, live

**[RUN]**, 2026-08-11. Toolchain: Yosys 0.68+post (git sha1 `c12172fb`,
Homebrew, macOS arm64); liberty
`~/.volare/sky130A/libs.ref/sky130_fd_sc_hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib`
(open_pdks `c6d73a35`, resolved by `klt pdk find`). Each variant is the
exact seven-line script of §1.1 with one thing changed; metrics are read
from the same `stat -liberty … -json` file the command itself parses.

**Design A — `gcd` (WIDTH=16), the repo's own worked-example RTL**
(`docs/design/yosys-synthesis-spike.md:396-438`), sequential, single module:

| Variant | Cells | Area (µm²) | ABC `stime` delay |
| --- | --- | --- | --- |
| **baseline** (today's exact script) | **335** | **2951.58** | *not reported* |
| `+ synth -flatten` | 335 | 2951.58 | *not reported* |
| `+ abc -constr` | 347 | 3138.01 | 2365.41 ps |
| `+ abc -D 2000 -constr` | 347 | 3138.01 | 2365.41 ps |
| `+ abc -D 500 -constr` | 347 | 3138.01 | 2365.41 ps |
| `+ abc -constr -dont_use 'sky130_fd_sc_hd__lpflow_*' -dont_use 'sky130_fd_sc_hd__probe*'` | 347 | **3086.71** | 2371.20 ps |

The baseline row reproduces `docs/cli/synthesize.md`'s documented worked
example **exactly** (335 instances, 2951.5808 µm², 1251.2 µm² sequential) —
so this A/B is anchored to the same number the shipped contract documents,
not to a differently-configured rerun.

**Design B — `mult8` (`assign p = a * b;`, 8×8), combinational**, chosen so
`klt equiv` can actually close the loop on it (§4.2) and so the delay knob
has a deep arithmetic cone to work on:

| Variant | Cells | Area (µm²) | ABC `stime` delay |
| --- | --- | --- | --- |
| **baseline** | **248** | **1939.36** | *not reported* |
| `+ abc -constr` | 250 | 2058.22 | 3281.92 ps |
| `+ abc -D 5000 -constr` | 250 | **1946.87** | **3909.46 ps** |
| `+ abc -D 2000 -constr` | 250 | 2058.22 | 3281.92 ps |
| `+ abc -D 1000 -constr` | 250 | 2058.22 | 3281.92 ps |
| `+ abc -D 500 -constr` | 250 | 2058.22 | 3281.92 ps |

**This is a real, measured delay/area trade-off curve on a knob the command
does not expose**: relaxing the target to `-D 5000` buys **5.7% area**
(2058.22 → 1946.87 µm²) for **16.0% delay** (3281.92 → 3909.46 ps). Today's
command is pinned to one unlabelled point on that curve, and it is not the
end a caller with a slack budget would necessarily choose.

The `-constr` file used throughout was the two-line form `help abc`
documents:

```
set_driving_cell sky130_fd_sc_hd__inv_2
set_load 0.03
```

**A third measured finding, from the baseline netlist itself.** The mapped
`gcd` netlist today contains **19 `sky130_fd_sc_hd__lpflow_*` instances**
(16 × `lpflow_isobufsrc_1`, 3 × `lpflow_inputiso1p_1`) — 5.7% of its 335
cells (**[RUN]**, `grep` over the emitted netlist). These are sky130's
low-power-flow isolation cells, intended for power-gated domain boundaries,
and they are the class of cell ORFS's own sky130hd platform excludes via
`DONT_USE_CELLS` (**[LIT]** — the exact ORFS glob list must be re-verified
against `platforms/sky130hd/config.mk` before being copied into code, not
taken from this document). ABC uses them here simply because they are in the
liberty and look like cheap buffers. Excluding them via `abc -dont_use`
**reduced area by 1.6%** (3138.01 → 3086.71 µm²) for **+0.2% delay**
(2365.41 → 2371.20 ps) — i.e. removing them is close to free on this design,
and it removes cells whose presence a signoff flow would reject on its own
terms.

### 1.4 Where it deliberately stops, and one framing to revisit

`docs/cli/synthesize.md`'s "Out of scope" section (timing, P&R, fleet
evaluation, a second engine) is unchanged and this survey does not propose
touching most of it. **One framing is worth revisiting, though**, with
evidence the #396 spike did not have:

`timing` is documented as `null` because "Yosys's own `sta`/`ltp` passes
could not produce a usable timing report against a liberty-mapped netlist"
(**[REPO]**, `docs/cli/synthesize.md:111-119`). That finding reproduces —
`ltp -noff` on today's mapped `gcd` netlist reports `Detected loop at …` for
every register-crossing path and yields no usable depth (**[RUN]**). But
the conclusion drawn from it, that no delay number is available from this
flow at all, is **too strong**: ABC's own `stime -p` produces one
(2365.41 ps / 3281.92 ps above), it is already computed inside the
subprocess this command runs, and it is discarded because `-constr` is
never passed. The honest caveat is that ABC reports `WireLoad = "none"`
(**[RUN]**, quoted verbatim in the §1.3 logs) — this is a **pre-layout,
wire-free** combinational estimate, not signoff STA, and it must be
labelled as such (§3.3) rather than presented as the timing number Phase 4's
OpenSTA step will eventually produce.

### 1.5 Net baseline characterization

Every metric this command reports today (`instance_count`, `area_um2`,
`sequential_area_um2`, `instance_counts_by_type`) is bounded by (a) Yosys's
and ABC's own algorithmic ceiling, which this command does not touch, and
(b) which of their *optional* flags this wrapper turns on — which it
controls entirely, and currently sets to none. Items §3.1–§3.5 live entirely
inside (b): zero new dependencies, zero Rust, and at most one additive
response field each. Items §3.6–§3.7 are the genuine wrap-vs-native question
Epic #704 asks, presented as open questions with explicit gates.

## 2. External SOTA survey

**2.1 AIG-based technology-independent optimization.** ABC (Brayton &
Mishchenko, "ABC: An Academic Industrial-Strength Verification Tool," CAV
2010 — **[LIT]**) represents combinational logic as an And-Inverter Graph
and optimizes it with DAG-aware rewriting, refactoring, and balancing
(Mishchenko, Chatterjee, Brayton, "DAG-Aware AIG Rewriting: A Fresh Look at
Combinational Logic Synthesis," DAC 2006 — **[LIT]**), packaged as the
familiar `resyn`/`resyn2`/`compress2rs` scripts. Yosys's default liberty
script (§1.2) uses `dc2` and `&dch -f`, i.e. a *fixed, short* slice of this
space — it does not iterate `resyn2`-style, and it is not the script an
engineer tuning for QoR would settle on without measuring alternatives.

**2.2 Structural bias and choices.** Technology mapping over a single
fixed AIG structure is provably limited by whatever decomposition the
front-end happened to produce (Chatterjee, Mishchenko, Brayton, Wang, Kam,
"Reducing Structural Bias in Technology Mapping," ICCAD 2005 / IEEE TCAD
2006 — **[LIT]**). The standard remedy is *choices*: keep several
functionally-equivalent structures and let the mapper pick per-cut. Yosys's
default script does invoke this (`&dch -f`), so this is one place the
default flow is **not** leaving obvious value on the table — worth stating
explicitly, because it is the first thing a reader of the DAC/ICCAD
literature would reach for.

**2.3 Cut-based structural mapping.** The mapper itself (`&nf`) is a
priority-cut mapper in the DAGON lineage (Keutzer, "DAGON: Technology
Binding and Local Optimization by DAG Matching," DAC 1987 — **[LIT]**;
Mishchenko, Cho, Chatterjee, Brayton, "Combinational and Sequential Mapping
with Priority Cuts," ICCAD 2007 — **[LIT]**), with delay-driven cut
selection and area recovery. Its objective is set by the delay target it is
given — which, per §1.2, is currently nothing.

**2.4 Mapping with decomposition / functional mapping.** Purely structural
covering is bounded by the subject graph; integrating decomposition into
mapping (Lehman, Watanabe, Grodstein, Harkness, "Logic Decomposition During
Technology Mapping," ICCAD 1995 — **[LIT]**) and Boolean-matching /
functional approaches recover some of that. This is the "structural vs.
functional mapping" axis the issue asks about; in practice ABC's
choices-plus-priority-cuts combination is the pragmatic middle of it, and
the realistic near-term lever for this repo is *which* structures and
*which* target `&nf` is handed, not replacing the mapper.

**2.5 Don't-care-based resynthesis.** SAT-based resubstitution with
observability/satisfiability don't-cares (`mfs`/`mfs2`; Mishchenko,
Brayton et al., "Scalable Don't-Care-Based Logic Optimization and
Resynthesis," FPGA 2009 / ACM TRETS 2011 — **[LIT]**) is a standard
post-mapping gate-count reducer. Yosys's LUT script uses `mfs2`; its
**liberty** script does not (§1.2, **[RUN]**) — a concrete asymmetry
between the FPGA and ASIC default paths, and a candidate for the
script-search item (§3.4) rather than an assumed win.

**2.6 Gain-based sizing and buffering.** `buffer; upsize; dnsize` implement
load-driven drive-strength selection, the practical form of the logical-effort
theory (Sutherland, Sproull, Harris, *Logical Effort: Designing Fast CMOS
Circuits*, 1999 — **[LIT]**). Its inputs are exactly what a `-constr` file
supplies: a driving cell for the primary inputs and an output load. Without
them the whole step is skipped (§1.2, **[RUN]**).

**2.7 Retiming.** Moving registers across combinational logic to balance
path delay (Leiserson & Saxe, "Retiming Synchronous Circuitry,"
*Algorithmica* 1991 — **[LIT]**) is the classic sequential lever on clock
period, and ABC implements it (`retime`). It is structurally unavailable in
today's flow (§1.2). It is also the single item in this document that
**breaks** the correctness loop the issue asks for — see §3.6.

**2.8 Alternative logic representations.** Majority-Inverter Graphs (Amarú,
Gaillardon, De Micheli, "Majority-Inverter Graph: A New Paradigm for Logic
Optimization," IEEE TCAD 2016 — **[LIT]**) and the EPFL logic-synthesis
libraries (Soeken et al., "The EPFL Logic Synthesis Libraries," 2018/2019 —
**[LIT]**, the `mockturtle` C++ header library) report depth improvements
over AIG-based flows on the EPFL combinational benchmarks (Amarú,
Gaillardon, De Micheli, IWLS 2015 — **[LIT]**). This is the most credible
"a different engine could genuinely beat the default" claim in the
literature. It is also a multi-year research programme, not a `klt` issue;
it is listed here for completeness and explicitly **not** proposed in §3.

**2.9 Automated flow/script tuning.** Because per-design QoR is strongly
sensitive to *which* optimization script is run, a productive line of work
treats script selection as a search problem — reinforcement-learning and
learned-heuristic approaches (Hosny, Hashemi, Shalan, Reda, "DRiLLS: Deep
Reinforcement Learning for Logic Synthesis," ASP-DAC 2020 — **[LIT]**; Yu,
Xiao, De Micheli, "Developing Synthesis Flows Without Human Knowledge," DAC
2018 — **[LIT]**), and OpenROAD's own AutoTuner for flow parameters
(**[LIT]**). The transferable, low-tech core of that result — *a
best-of-N sweep over scripts and delay targets beats any single fixed
script* — needs no ML and composes with machinery this repo already has
(§3.4).

**2.10 Yosys and the wrapped stack.** Yosys itself (Wolf, Glaser, Kepler,
"Yosys — A Free Verilog Synthesis Suite," Austrochip 2013 — **[LIT]**), ISC
licensed, with ABC under a permissive UC Berkeley licence — the licensing
posture the #396 spike already verified live and this survey does not
re-derive (**[REPO]**).

**2.11 One negative result, to save a later implementer the detour.**
Yosys's `abc9` pass (the newer `&`-space flow with better structural-choice
handling) exposes **no `-liberty` or `-genlib` option** — `yosys -p 'help
abc9'` matches neither string (**[RUN]**). `abc9` is a LUT/FPGA-oriented
path; it is not an available upgrade for ASIC liberty mapping today, and
`synth -abc9` should not be proposed as a cheap win.

## 3. Prioritized proposal

Seven items, ordered by (near-term, measurable QoR gain) ÷ (engineering and
correctness risk). Items 1–5 are flow/parameter changes to the existing
Yosys orchestration. Item 6 is a real lever this survey recommends
**deferring** on correctness grounds. Item 7 is the one native-Rust
recommendation, argued on measurement grounds rather than on beating ABC.

### 3.1 Consume `constraints.clock_period_ns` — pass `abc -D` and a `-constr` file (Priority 1)

- **Technique:** translate the request's existing
  `constraints.clock_period_ns` into `abc -D <picoseconds>`, and always
  emit a two-line ABC constraint file (`set_driving_cell` / `set_load`)
  passed as `abc -constr` (§1.2, §2.6).
- **QoR metric:** **critical-path delay**, with area as the measured
  counter-cost. Measured on `mult8`: 16.0% delay for 5.7% area across the
  `-D` range (§1.3, **[RUN]**). Secondary: enabling `-constr` at all turns
  on `buffer; upsize; dnsize`, the sizing/buffering step the command
  currently skips entirely.
- **Why first:** it is the only item that fixes a *documented* contract
  wart. `docs/cli/synthesize.md:145` states outright that
  `constraints.clock_period_ns` is "**accepted but not consumed** … has no
  effect on this command's output today" (**[REPO]**). The request field
  already exists, the engine flag already exists, and the measured effect
  is real. Nothing else in this list is that close to shipped.
- **Rust vs. flow:** **flow/parameter.** A few lines in `_write_script`
  (`synthesize.py:347-382`), plus writing one small constraint file next to
  the `.ys` script in the existing `.klt/synthesize/` artifacts directory.
- **Wrap-vs-native trade-off:** none to weigh — this is strictly inside the
  existing wrap, and argues for *using more of the tool already invoked*,
  not for replacing it.
- **Added scope, stated honestly:** `set_driving_cell` needs a
  per-`cell_library` cell name and `set_load` a per-library default
  capacitance. That is exactly the "not derivable from the resolved PDK
  install alone" problem `place_and_route.py`'s `_CTS_BUFFER_CELLS` /
  `_ROUTING_LAYER_RANGE` tables already solve (**[REPO]**), and this item is
  naturally a third table in that family, sourced the same way (ORFS's own
  `platforms/<variant>/config.mk`, cross-checked against the library's LEF
  and liberty — **never guessed**). This survey used
  `sky130_fd_sc_hd__inv_2` / `0.03` for its own A/B; those are a
  *measurement choice*, **[RUN]**-tier for reproducing this document's
  numbers and explicitly **not** a recommended default — the follow-on
  issue must derive real values, exactly as #629/#637 did for the existing
  two tables.
- **Measurement plan:** §4. The diagnostic pair is `area_um2` against the
  delay number §3.3 adds; `klt equiv` gates correctness (§4.2). Note the
  measured `gcd` result: `-D` changed **nothing** on that design (§1.3) —
  a design whose critical path is already short relative to any plausible
  target has no delay to buy back. The corpus run must therefore report the
  *distribution*, not one design's delta.
- **Risk:** area regression on designs that were already fast enough
  (measured: +6.3% on `gcd` from `-constr` alone, with no delay gain to
  show for it). This argues for a **caller-controlled** target — honour
  `clock_period_ns` when given, and decide deliberately what to do when it
  is `null` (candidates: keep today's untargeted behaviour, or pass
  `-constr` without `-D` for sizing only). That is a real design question
  the follow-on issue must settle with corpus data, not this document.

### 3.2 Exclude non-logic cells via `abc -dont_use` (Priority 2)

- **Technique:** pass `abc -dont_use <glob>` for the cell classes a real
  flow excludes — sky130's `lpflow_*` power-isolation cells, `probe*`
  test-probe cells, and the equivalents for any other supported library
  (§1.3, §2). `-dont_use` accepts glob patterns and is liberty-only
  (**[RUN]**, `help abc`).
- **QoR metric:** **area** (measured: −1.6% on `gcd`, at +0.2% delay —
  §1.3, **[RUN]**), plus a signoff-legality benefit that is not an area
  number: 19 of today's 335 `gcd` cells are power-domain isolation cells
  that have no business in a non-power-gated netlist.
- **Rust vs. flow:** **flow/parameter.** One flag, repeated.
- **Wrap-vs-native trade-off:** none.
- **Added scope:** a per-library exclusion table, same family and same
  sourcing discipline as §3.1's. The globs this survey measured with
  (`sky130_fd_sc_hd__lpflow_*`, `sky130_fd_sc_hd__probe*`) are
  **[RUN]**-tier for reproducing this document and **[LIT]**-tier as a claim
  about what ORFS excludes — re-verify against ORFS's own
  `platforms/sky130hd/config.mk` before writing them into code. Note that
  this repo has already had to read ORFS's `DONT_USE_CELLS` once, for a
  different purpose: `place_and_route.py:270` records that gf180's platform
  sets `DONT_USE_CELLS = *_1` and checks its CTS buffer choice against it
  (**[REPO]**) — so the sourcing path is established, and the gf180 case is
  a warning that these lists are not all "exclude the weird cells": a
  blanket `*_1` exclusion of every minimum-drive cell is a much larger area
  intervention than sky130's `lpflow_*`, and the two libraries should not be
  given the same treatment by analogy.
- **Measurement plan:** §4; the headline metric is `area_um2` and the
  regression gate is that `instance_counts_by_type` contains zero excluded
  cells afterwards — a direct, assertable check on the existing response
  field, needing no new contract surface.
- **Risk:** lowest in this list. Excluding cells can only make the mapper's
  choice set smaller, so a pathological area *increase* is possible in
  principle and must be measured rather than assumed away — but the failure
  mode is a worse number, never a wrong netlist.

### 3.3 Report a real (clearly-labelled, pre-layout) delay number (Priority 3)

- **Technique:** once §3.1 lands, ABC prints `stime -p`'s result into the
  Yosys log (`ABC: WireLoad = "none" … Delay = 2365.41 ps`, **[RUN]**).
  Capture it and fill the response's reserved `timing` object.
- **QoR metric:** this item **is** the measurement substrate. Today the
  command reports gate count and area and nothing else — so of the three
  QoR axes the issue names (gate count / logic depth / timing), **two are
  unmeasurable from this command's own output**. Every other item in this
  document is currently evaluated on area alone, which systematically
  favours the wrong end of the §1.3 trade-off curve.
- **Rust vs. flow:** **flow + a small parser.** The value is already in the
  subprocess output; the work is capturing it (ideally via the same
  `tee -q -o` discipline the #396 spike settled for `stat`, rather than
  regexing the interleaved log — the spike's own explicit warning,
  **[REPO]**) and shaping it.
- **Contract impact:** `timing` is already present, typed, and documented as
  reserved (**[REPO]**, `docs/cli/synthesize.md:186`), so populating it is
  **additive**, not a break — but it must be shaped so a later
  OpenSTA-backed number can coexist. **[PROPOSAL]:** an object that names
  its own provenance and limits, e.g.
  `{"source": "abc_stime", "wire_load": null, "critical_path_ps": 2365.41,
  "delay_target_ps": 2000}` — never a bare `delay_ps` float that a caller
  would reasonably mistake for signoff timing. The `WireLoad = "none"`
  caveat of §1.4 must appear in `docs/cli/synthesize.md`, not only in code.
- **Wrap-vs-native trade-off:** this is the wrap-side half of §3.7's
  native-side proposal; the two are complements, not alternatives. Ship
  this first regardless of what Epic #704 decides about Rust.
- **Measurement plan:** self-verifying — assert the parsed value against
  the value visible in the run log for the same design, and assert it is
  absent (`null`) when `-constr` is not passed.
- **Risk:** presenting a wire-free estimate as "timing" is the whole risk,
  and it is a documentation/naming risk rather than a technical one.

### 3.4 Best-of-N script and delay-target sweep (Priority 4)

- **Technique:** run the same design through several ABC scripts
  (`abc -script`, e.g. a `resyn2`-style iteration, a `mfs2`-augmented
  variant per §2.5) and/or several `-D` targets, and keep the best result
  by an explicit objective (§2.9).
- **QoR metric:** gate count **and** delay jointly — this is the only item
  that can improve both, because it selects rather than trades.
- **Rust vs. flow:** **flow/orchestration.** No new algorithm. The natural
  home is not `synthesize.py` itself but the existing fleet/DSE machinery:
  `klayout_tools.digital_fleet` already treats "one candidate evaluation"
  as its unit of parallelism (**[REPO]**,
  `docs/design/digital-fleet-unit-abstraction-decision.md`), and a
  synthesis-script sweep is exactly that shape, one stage earlier than the
  `(floorplan, seed)` sweep it runs today.
- **Wrap-vs-native trade-off:** none — and it is worth noting this item
  *raises the bar* any future native pass must clear, since "beat Yosys" is
  a much weaker claim than "beat the best of N tuned Yosys runs." A native
  effort that skips this comparison would be measuring against a
  deliberately weak baseline.
- **Measurement plan:** §4, reporting best-of-N against the single-run
  baseline **and** the wall-clock cost of N runs — a 5% area win that costs
  8× runtime is a different product decision than one that costs 2×.
- **Risk:** determinism. `klt synthesize`'s response is currently a
  deterministic function of its request; a sweep must keep that property
  (record the winning script in the response, per §3.3's provenance
  instinct) or the command stops being reproducible.
- **Priority note:** ranked below §3.1–§3.3 because it cannot be *evaluated*
  before §3.3 gives it an objective beyond area.

### 3.5 `synth -flatten` (Priority 5, gated on hierarchical evidence)

- **Technique:** pass `-flatten` to `synth` (§1.2), removing module
  boundaries so optimization crosses them; `-hieropt` is the
  hierarchy-preserving alternative.
- **QoR metric:** gate count / area, on **hierarchical** designs.
- **Rust vs. flow:** **flow/parameter.** One flag.
- **Honest measured result:** **no effect whatsoever** on either design
  tested — `gcd` is byte-identical at 335 cells / 2951.58 µm² with and
  without `-flatten` (§1.3, **[RUN]**), because it is a single module with
  no sub-hierarchy to flatten. This item is therefore **unmeasured**, not
  measured-and-good, and it is ranked here rather than higher for exactly
  that reason. It is included because it is the standard lever and because
  the corpus (§4.1) certainly contains hierarchical designs the two
  fixtures here do not represent.
- **Risk:** flattening destroys hierarchy the response's provenance and any
  downstream hierarchical LVS would otherwise use, and it inflates runtime
  and netlist size on large designs. Any adoption should be caller-visible
  (a request field), not a silent default.

### 3.6 Sequential ABC and retiming — recommended **deferred**, with the reason (Priority 6)

- **Technique:** invoke `abc -dff -clk <clk>` so ABC sees registers, which
  activates `retime -o -D` in the `-D` script (§1.2, §2.7).
- **QoR metric:** clock period / logic depth — the metric neither §3.1 nor
  §3.4 can move, because both are confined to fixed register boundaries.
  This is the largest *potential* win in this document.
- **Rust vs. flow:** **flow/parameter** — deceptively cheap, two flags.
- **Why this survey recommends deferring it anyway:** it is the one item
  that **breaks the correctness loop the issue asks for**. Retiming moves
  registers, so the post-synthesis netlist is no longer
  cycle-for-cycle-comparable to the RTL; proving it correct needs
  *sequential* equivalence (temporal induction / BMC), which `klt equiv`
  explicitly does not do — a design with flip-flops on either side is
  rejected outright with a scope error (**[RUN]**, verified: exit 1,
  "combinational-only MVP … sequential designs need a future phase"). Epic
  #704's own acceptance criteria require formal equivalence to the RTL. So
  this item is **blocked on #707's sequential phase**, and shipping it
  before then would mean shipping the one transformation in this list whose
  correctness this repo cannot check — against an epic whose stated
  discipline is "no claim without a runnable check."
- **Measurement plan:** unchanged in shape (§4), but gated: revisit when
  `klt equiv` gains a sequential mode, and re-rank then — on a corpus of
  clock-limited designs it would plausibly move to the top of this list.

### 3.7 Native-Rust gate-level static timing — the one Rust recommendation (Priority 7 to build, but see below)

> **Spiked (issue #809): Go**, on both halves of this section's own gate —
> the native-Rust engine (`native/statime/`) matched OpenSTA's worst-path
> delay within 0.36–1.34% on all three corpus designs tested (`gcd`,
> `mult8`, `modexp`; always a slight *under*-estimate, never over), and is
> **1.5–2.1× faster per call** than the realistic "wrap OpenSTA" comparison
> (a subprocess per call, matching how this repo already invokes every
> other engine, and requiring no new infrastructure). **Correction, same
> PR, caught during self-review**: an earlier draft of this spike reported
> "~30–40×" and "still ~1.5–2× faster even in the most sympathetic
> scenario," both wrong — the first mixed native's warm number against
> OpenSTA's cold number, the second used a heavier OpenROAD/LEF-requiring
> image when a lighter, LEF-free standalone OpenSTA build (`openroad/opensta`
> on Docker Hub) exists. Re-measured apples-to-apples: the realistic
> (cold, no-new-engineering) comparison is a real 1.5–2.1× on every design,
> and the hand-engineered-persistent-session comparison is **within noise**
> (native is faster on one design, slower on another, at parity on the
> third) — a materially more honest and more modest result than first
> reported, and the Go verdict now rests on the cold-regime comparison
> alone, not the (retracted) sympathetic-scenario claim. See
> `native/statime/README.md` for the full corrected measured comparison,
> the two real bugs the OpenSTA comparison caught and fixed along the way
> (rise/fall collapse, NLDM clamp-vs-extrapolate), and the documented
> Python↔Rust boundary / CI story this issue's acceptance criteria also
> asked for. Not yet wired into `klt synthesize`'s response contract — that
> is a separate follow-on issue now that the verdict is known. This
> section's own reasoning below is left unchanged as the historical record
> of the proposal that was tested.

- **Technique:** a Rust liberty parser plus a topological critical-path
  engine over the mapped netlist: NLDM lookup-table interpolation for
  cell delay/transition, longest-path propagation over the timing graph,
  register-to-register path enumeration. Explicitly **not** a placer,
  mapper, or optimizer.
- **QoR metric:** none directly — like §3.3, this item is *measurement
  infrastructure*. It is what makes "logic depth" and "timing" reportable
  QoR axes for every other item, at a fidelity `stime`'s wire-free estimate
  cannot reach, and with a per-path breakdown a caller can act on.
- **Rust vs. flow:** **native Rust**, and this survey argues it is the
  *right* first native slice for Epic #704 — for reasons that are
  deliberately not "we can beat ABC":
  1. **It is the only contract field with no source at all.** §1.4 shows
     `timing: null` is a genuine hole; §3.3 fills it with a caveated
     estimate, not a real answer. Every other Phase 1/2 candidate (native
     elaboration, native AIG optimization, native mapping) would be
     *re-implementing something that already works well*, and would have to
     clear a §3.4-tuned Yosys baseline to justify itself.
  2. **It has an unambiguous external oracle.** OpenSTA (already an
     OpenROAD dependency this project uses at the P&R stage — **[REPO]**,
     `docs/cli/place-and-route.md`) computes the same numbers on the same
     inputs. "Match OpenSTA's slack within tolerance on the corpus" is a
     crisp, automatable go/no-go, satisfying Epic #704's "every stage cites
     its oracle" bar with no interpretation required.
  3. **It is the shape Rust is actually good at**, and a bounded one:
     table interpolation and graph traversal over a few thousand nodes,
     no heuristic search, no open-ended optimization objective, and a
     correctness criterion that is a number rather than a judgement.
  4. **It is needed twice.** Epic #704 Phase 3 (timing-driven optimization)
     and Epic #700's STA loop both require it; building it as a shared
     component amortises it across both halves of the digital flow.
  5. **It retires the integration risk cheaply.** This repo has **zero Rust
     today** — no `Cargo.toml` anywhere in the tree (**[RUN]**). Whatever
     Epic #704 eventually builds natively, the *first* Rust component pays
     the whole cost of establishing the crate layout, the Python↔Rust
     boundary, the build/CI story, and the wheel-packaging question. Paying
     that on a component with an exact oracle and no algorithmic
     controversy is much cheaper than paying it on a mapper whose output is
     itself under debate.
- **Language trade-off, stated explicitly:** `CLAUDE.md` establishes this
  as a Python 3.10+/uv/pytest project; Epic #704 says "Language: Rust for
  the numerically/graph-hot passes." These are reconcilable only by keeping
  native work **scoped and optional** — a separately-built component behind
  the same JSON contract, with the flow degrading to today's behaviour when
  it is absent (precisely the posture `sequential_area_um2` already takes
  toward older Yosys builds, **[REPO]**, `synthesize.py:234-238`). A native
  pass that becomes a hard build dependency of `klt synthesize` would
  contradict "every command must be runnable in CI" for anyone without the
  toolchain. **[PROPOSAL]**, and a real decision for Epic #704, not one this
  document can make.
- **Wrap-vs-native trade-off, stated honestly:** OpenSTA already exists, is
  mature, and is BSD-licensed — "just shell out to OpenSTA" is a legitimate
  competing answer to the same gap, cheaper in the short run, and it should
  be evaluated head-to-head rather than dismissed. The case for Rust rests
  on (4) and (5) above (a reusable in-process component for a Phase-3
  optimization loop that would otherwise pay a subprocess round trip per
  iteration), not on OpenSTA being inadequate. If Epic #704 concludes it
  will never build native optimization passes, this item's justification
  largely collapses into "wrap OpenSTA," and that is the correct outcome.
- **Priority note:** ranked last **to build** because §3.1–§3.3 deliver
  measurable QoR sooner and at a fraction of the cost. It is ranked
  **first among native candidates**, and it is the item this survey would
  choose if Epic #704 asked "what is the first Rust thing to write."

## 4. Measurement harness — common to every item above

### 4.1 Corpus

Epic #520 (Tiny Tapeout — 4,572 project slots, 3,169 on `sky130A`, 72%
1×1-tile, per that epic's own measured 2026-08-04 shape — **[REPO]**) is the
intended benchmark, and it is a good fit here specifically because it is
**RTL**: unlike the P&R survey's corpus problem, synthesis needs only the
Verilog sources and a top module name, which every Tiny Tapeout project
ships. **No synthesis corpus harness exists today** — `tests/corpus/`
contains fixture GDS and P&R artifacts, not an RTL batch runner
(**[REPO]**). Building "run `klt synthesize` over N corpus designs and diff
the JSON responses" is itself scoped work and a real prerequisite for every
item above; it is small (the response is already JSON with the metrics in
it) but it is not free, and no item in §3 should claim corpus evidence
before it exists.

### 4.2 The correctness loop, and its measured limits

`klt equiv` (#726) is the gate: for each design and each variant, prove the
mapped netlist equivalent to the source RTL. Two measured facts constrain
how this can actually be used, and both were verified live rather than
assumed (**[RUN]**):

- **It is combinational-only, enforced.** `gcd`'s mapped netlist against its
  own RTL exits `1` with a scope error, not a verdict. Since most real
  corpus designs are sequential, **the correctness loop as specified does
  not close on most of the corpus today.** Practical consequences: (a)
  items §3.1–§3.5 all preserve register boundaries exactly, so a *structural*
  check (same flip-flop count and types in `instance_counts_by_type`, plus
  the existing `klt functional-verification` cocotb path where a testbench
  exists) is the available regression gate for sequential designs; (b) the
  formal gate applies to the combinational subset of the corpus, which
  should be identified explicitly rather than silently skipped; (c) §3.6 is
  the one item that *needs* the sequential mode and is deferred for it.
- **It is not cheap.** Proving the 8×8 multiplier (250 cells) equivalent
  took **99.38 s** — past `klt equiv`'s own 60 s default `timeout_s`, which
  would have returned `"inconclusive"` (**[RUN]**; the run above used
  `timeout_s: 120` and returned `"equivalent"`). Arithmetic-heavy
  combinational logic is exactly the SAT-hard case. A corpus-scale
  equivalence gate therefore needs a deliberate per-design budget and must
  treat `"inconclusive"` as a distinct, reported outcome — never folded
  into "passed."

### 4.3 A/B protocol

Same request document, same sources, one flag or step toggled per variant;
diff the JSON response fields directly (`instance_count`, `area_um2`,
`sequential_area_um2`, `instance_counts_by_type`, plus §3.3's proposed
`timing` object), never eyeballed from logs — the same discipline the #396
spike already imposed for `stat`. Yosys+ABC is deterministic for a fixed
script and input, so unlike P&R (§ the #735 sibling's seed discussion) there
is no seed to pin; a variant that produces different output across runs of
the same script is itself a defect report.

### 4.4 Yosys as oracle, and what that means here

Epic #704's "Yosys as ground truth" bar applies to §3.7 and to any future
native pass, where Yosys's output on the same input is the correctness bar.
It does **not** apply to §3.1–§3.6, which *are* Yosys — for those, the
oracle is `klt equiv` (§4.2) and the baseline is today's shipped default
flow, reproduced exactly in §1.3.

## 5. Follow-on implementation-issue sketches

### Sketch A — the top item: turn on the three ABC capabilities the flow forfeits (§3.1–§3.3)

**Title:** `synthesize: consume clock_period_ns as an ABC delay target, exclude non-logic cells, and report ABC's own critical-path delay`

**Why bundled:** all three are the same shape of change (a few lines in
`_write_script` plus one small artifact file), all three are unlocked by the
*same* `-constr` flag (§1.2 — `buffer`/`upsize`/`dnsize`/`stime -p` are all
`-constr`-gated, so §3.1 and §3.3 are physically the same change), and one
A/B corpus run measures all three deltas together. §3.2 is bundled because
it is the same table-sourcing exercise and the same measurement run.

**Scope:**
1. Emit a `<top>_abc.constr` file into the existing `.klt/synthesize/`
   artifacts directory (`set_driving_cell` / `set_load`), sourced from a new
   per-`cell_library` table following `place_and_route.py`'s
   `_CTS_BUFFER_CELLS`/`_ROUTING_LAYER_RANGE` precedent exactly —
   ORFS-`config.mk`-and-liberty-verified values, never guessed, never copied
   from this survey's own measurement placeholders.
2. Pass `abc -liberty <lib> -constr <file>`, plus `-D <ps>` derived from
   `constraints.clock_period_ns` when the request supplies it. Settle and
   document the `clock_period_ns: null` behaviour explicitly.
3. Add a per-library `-dont_use` glob table (same sourcing discipline);
   pass one `-dont_use` per entry.
4. Capture ABC's `stime -p` line and populate the reserved `timing` object
   with a self-labelling shape (§3.3), including the `WireLoad = "none"`
   pre-layout caveat in `docs/cli/synthesize.md`.
5. Update `docs/cli/synthesize.md`: the generated-script block, the
   `constraints.clock_period_ns` row (which currently says "not consumed"),
   the `timing` row, and the new artifact path. Also update
   `docs/design/yosys-synthesis-spike.md`'s §3.5/§3.6 framing, or note in
   the PR why it stands.

**Acceptance criteria:**
- `tests/test_synthesize.py` asserts the generated `.ys` contains `-constr`,
  the expected `-D` when `clock_period_ns` is given and no `-D` when it is
  not, and one `-dont_use` per table entry.
- `instance_counts_by_type` contains **zero** excluded cells for a real
  `sky130_fd_sc_hd` run (today's `gcd` baseline contains 19 — §1.3, a
  directly reproducible before/after).
- A real A/B on at least the two designs of §1.3 plus a corpus slice,
  reporting `instance_count`, `area_um2`, and the new delay number for
  baseline vs. each variant, captured in the PR body per this repo's
  "no claim without a runnable check" discipline. **The baseline row must
  reproduce 335 cells / 2951.5808 µm² for `gcd`** — if it does not, the
  harness is wrong before any conclusion is drawn from it.
- `klt equiv` returns `"equivalent"` for every combinational design in the
  slice, both before and after, with a `timeout_s` chosen against §4.2's
  measured 99 s data point (not the 60 s default).
- Every new per-library table entry cites its ORFS/liberty source, exactly
  as #629/#637 did for the existing two tables.

**Not in scope:** the corpus harness (§4.1's noted prerequisite gap) — this
issue may use a hand-picked slice; `synth -flatten` (§3.5, unmeasured);
`-dff`/retiming (§3.6, blocked on #707); anything native.

### Sketch B — the top native candidate: Rust gate-level STA, OpenSTA-gated (§3.7)

**Title:** `synthesize: spike a native-Rust gate-level static timing engine against OpenSTA`

**Framing:** explicitly a **spike**, producing a go/no-go decision backed by
real corpus numbers for Epic #704 — not a production STA engine merged on
faith. Its first competing alternative ("wrap OpenSTA instead") must be
evaluated in the same spike, not assumed away.

**Scope:**
1. A Rust crate that parses a liberty file's cell timing tables and a
   `write_verilog`-produced mapped netlist, builds the timing graph, and
   reports register-to-register critical paths with NLDM interpolation.
2. Run it and OpenSTA on the identical netlist + liberty across a small
   corpus slice; report per-design critical-path delay from both, the
   delta distribution, and wall-clock for both.
3. Report the same numbers for the "wrap OpenSTA" alternative (subprocess +
   parse), so the comparison is three-way: today's nothing, wrapped
   OpenSTA, native Rust.
4. Document the crate layout, the Python↔Rust boundary, and the CI/wheel
   story — the first-Rust-in-the-repo cost §3.7(5) names — as an explicit
   deliverable, since that cost is a large part of what this spike exists
   to measure.

**Acceptance criteria (decided by the numbers the spike produces, not
assumed here):**
- **Go** only if: critical-path delay matches OpenSTA within a documented
  tolerance on every design tested, **and** the native path shows a
  concrete advantage over wrapping OpenSTA that survives real
  build/packaging cost — most plausibly per-iteration latency inside a
  future Phase-3 optimization loop, measured rather than asserted.
- **No-go** (park it, wrap OpenSTA, document why) if accuracy cannot be
  matched, or if the wrapped path is within noise on the metric that
  actually motivated the native one.
- Either outcome is a valid, reportable result for Epic #704.

**Not in scope:** native elaboration, logic optimization, or technology
mapping (Epic #704's Phases 1–2 — larger, and each must clear a §3.4-tuned
Yosys baseline, not a default-flow one); wiring STA into the `klt
synthesize` response contract (a separate issue once/if this spike says
"Go"; §3.3 fills that field in the meantime with a caveated estimate).

## References

- Brayton, R., Mishchenko, A. "ABC: An Academic Industrial-Strength
  Verification Tool." CAV, 2010.
- Mishchenko, A., Chatterjee, S., Brayton, R. "DAG-Aware AIG Rewriting: A
  Fresh Look at Combinational Logic Synthesis." DAC, 2006.
- Chatterjee, S., Mishchenko, A., Brayton, R., Wang, X., Kam, T. "Reducing
  Structural Bias in Technology Mapping." ICCAD, 2005 / IEEE TCAD, 2006.
- Keutzer, K. "DAGON: Technology Binding and Local Optimization by DAG
  Matching." DAC, 1987.
- Mishchenko, A., Cho, S., Chatterjee, S., Brayton, R. "Combinational and
  Sequential Mapping with Priority Cuts." ICCAD, 2007.
- Lehman, E., Watanabe, Y., Grodstein, J., Harkness, H. "Logic Decomposition
  During Technology Mapping." ICCAD, 1995.
- Mishchenko, A., Brayton, R. et al. "Scalable Don't-Care-Based Logic
  Optimization and Resynthesis." FPGA, 2009 / ACM TRETS, 2011.
- Leiserson, C. E., Saxe, J. B. "Retiming Synchronous Circuitry."
  *Algorithmica*, 1991.
- Sutherland, I., Sproull, R., Harris, D. *Logical Effort: Designing Fast
  CMOS Circuits.* Morgan Kaufmann, 1999.
- Amarú, L., Gaillardon, P.-E., De Micheli, G. "Majority-Inverter Graph: A
  New Paradigm for Logic Optimization." IEEE TCAD, 2016.
- Amarú, L., Gaillardon, P.-E., De Micheli, G. "The EPFL Combinational
  Benchmark Suite." IWLS, 2015.
- Soeken, M. et al. "The EPFL Logic Synthesis Libraries." 2018/2019
  (`mockturtle`).
- Hosny, A., Hashemi, S., Shalan, M., Reda, S. "DRiLLS: Deep Reinforcement
  Learning for Logic Synthesis." ASP-DAC, 2020.
- Yu, C., Xiao, H., De Micheli, G. "Developing Synthesis Flows Without Human
  Knowledge." DAC, 2018.
- Wolf, C., Glaser, J., Kepler, J. "Yosys — A Free Verilog Synthesis Suite."
  Austrochip, 2013.
- `docs/design/yosys-synthesis-spike.md` (#396) — the accepted spike this
  command implements; §1 characterises the shipped result, §1.4 revisits one
  of its conclusions with new measured evidence.
- `docs/design/digital-flow-contracts-spike.md` (#399) — the contract this
  document proposes additive fields against.
- `docs/cli/equiv.md` (#726) — the correctness-loop mechanism of §4.2.
- `docs/design/place-and-route-improvements-survey.md` (#735) — the sibling
  survey for the back half of the digital flow.
- Epic #704 (this document's parent), Epic #700 (P&R), Epic #707 (formal
  equivalence — §3.6's blocker), Epic #520 (the Tiny Tapeout corpus §4.1
  targets).
