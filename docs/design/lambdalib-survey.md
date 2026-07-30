# Survey: lambdalib

**Status:** exploration finding, not a spike. This document answers the
three questions in #39 (part of the
[siliconcompiler](https://github.com/siliconcompiler/siliconcompiler)-org
survey started by #38) and records a "not adopted, here is why" outcome per
[docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "Mining the outside world." No
dependency, `klt` subcommand, or KB entry is proposed by this document.

**What was verified vs. inferred:** the README, cell-inventory tables, and a
sample of source files (`la_dffq.v`, `la_inv.v`, `la_spram.v`, `la_pll.v`,
`la_ring.v`) were fetched directly from
[siliconcompiler/lambdalib](https://github.com/siliconcompiler/lambdalib)
`main` (via `raw.githubusercontent.com` and the GitHub contents API) and read
in full. The lambdapdk cross-reference (directory listing, `sky130sc.py`)
was fetched the same way from
[siliconcompiler/lambdapdk](https://github.com/siliconcompiler/lambdapdk).
Nothing here is inferred from memory of the project; everything cited below
was fetched live during this survey.

## Correction to the issue body

#39 describes lambdalib as **Apache-2.0**. The repository's actual license,
per its `README.md` License section and `LICENSE` file, is **MIT**
(Copyright 2023 Zero ASIC Corporation). lambdapdk (the PDK-data sibling
project) *is* Apache-2.0 — the two projects' licenses were likely
conflated. This matters because MIT is a closer match to this repo's own
MIT license than Apache-2.0 would have been, though it turns out not to
change the adoption conclusion below.

## 1. What lambdalib contains, and at what abstraction level

lambdalib is **pure RTL** — Verilog source only, one module per file, no
netlist or layout data ships in the repository at all. Its own README states
the cell inventory as 8 sub-libraries, ~164 modules total:

| Sub-library | Count | Contents |
| --- | --- | --- |
| `stdlib` | 97 | Logic gates, AOI/OAI complex gates, muxes, flip-flops/latches, clock-tree cells, arithmetic, tie cells |
| `auxlib` | 22 | CDC synchronizers, glitchless clock muxes/gates, I/O buffers, DDR registers, power-switch cells, antenna/decap/keeper physical cells |
| `ramlib` | 6 | SPRAM, DPRAM, TDPRAM, register file, sync/async FIFO |
| `iolib` | 16 | Digital/analog/power I/O pad interfaces |
| `padring` | 3 | Padring generator |
| `veclib` | 15 | Bus-width-scalable buffers/inverters/muxes/registers |
| `fpgalib` | 3 | LUT4, BLE, CLB primitives |
| `analoglib` | 2 | PLL, ring oscillator |

Verified by reading source, not just the README table: every cell checked
(`la_dffq`, `la_inv`) is a **synthesizable behavioral model** with a `PROP`
parameter that is inert in the default implementation:

```verilog
module la_inv #(parameter PROP = "DEFAULT") (input a, output z);
    assign z = ~a;
endmodule
```

`la_spram.v` documents the mechanism explicitly in its header comment: the
generic module is "a synthesizable reference model," and "technology
specific implementations of `la_spram` would generally include one or more
hardcoded instantiations of RAM modules... relying on the `PROP`, AW, and DW
to select between the list of modules at build time." In other words,
lambdalib defines the **interface and a golden simulation model**; a real
tapeout swaps in a technology-specific implementation (a hardened SRAM
macro, a hand-picked standard-cell instantiation) through
[lambdapdk](https://github.com/siliconcompiler/lambdapdk) and
[SiliconCompiler](https://github.com/siliconcompiler/siliconcompiler)'s
build system — lambdalib cells are `siliconcompiler.Design` subclasses, not
standalone Verilog files consumed by an arbitrary flow.

**Abstraction level: RTL only, one level above netlist, zero levels into
layout.** No GDS, LEF, CDL, or liberty ships with lambdalib. That physical
data lives in lambdapdk instead — confirmed by fetching lambdapdk's
directory tree (`lambdapdk/sky130/libs/sky130io/{gds,lef,verilog,nldm}`,
`sky130sc.py` wiring `.gds`/`.lef`/`.cdl`/`.v`/`.lib.gz` files per standard-cell
corner) and its README ("Standard cells... I/O, SRAM" per PDK, "full
integration into the SiliconCompiler build system"). This repo already
depends on that data path: **lambdapdk is what open PR #42
(`scripts/fetch-pdks.sh`) fetches** — "stdcell LEF/GDS/liberty for sky130,
gf180, ..." per that PR's own description — independently of anything in
lambdalib.

`analoglib` is the one category whose name suggests relevance to this repo's
analog focus, and it does not hold up on inspection. `la_pll.v`'s header
comment reads: "In real ASIC design the la_pll is replaced by an actual PLL
implementation... A coarse simulation model is available." `la_ring.v`'s
header reads: "A structural ring oscillator can't be simulated in verilog so
we drive a simulation only FREF frequency in MHz to the output as a
behavioral model." Both are explicitly disclaimed as digital-testbench
stand-ins for real analog blocks, not analog implementations — `la_ring`'s
non-behavioral branch is literally built from `la_nand2`/`la_inv` digital
gate instances forming a combinational ring, which cannot represent real
transistor-level ring-oscillator behavior (device sizing, frequency vs.
process/voltage/temperature, phase noise). Neither module is a candidate
analog circuit generator by any reading.

## 2. Ready-made inputs for the closed loop?

**Not directly, and not soon.** lambdalib ships no GDS, so there is nothing
here to push through `klt drc`/`klt lvs` as-is. Turning a lambdalib RTL
design into GDS requires a full synthesis + place-and-route flow (yosys +
OpenROAD, orchestrated by SiliconCompiler, ending in a KLayout stream-out
step per #38's findings on `siliconcompiler.tools.klayout`) — a toolchain
this repo does not implement and is not phase-scheduled to implement
(`ROADMAP.md` phases 1–4 are read/check/write/extract on layouts that
already exist, not a P&R engine).

The GDS-level PDK data that lambdalib's ecosystem *does* usefully provide —
real standard-cell, SRAM-macro, and I/O-pad GDS/LEF/CDL/liberty — comes from
**lambdapdk**, not lambdalib, and this repo is already positioned to consume
it once PR #42 merges. That is a preexisting win independent of this
survey's outcome, not something #39 needs to add.

For **known-good full-chip designs** compiled end-to-end from lambdalib RTL
(the "push through spec → layout → DRC/LVS" scenario in #39's question), the
actual candidate source is
[scgallery](https://github.com/siliconcompiler/scgallery) — confirmed by
fetching its README, which states it "uses the rtl2gds flow in
SiliconCompiler to compile the designs from RTL to a GDS file" and ships a
`sc-gallery -design <name> -target <target>` runner rather than pre-built
GDS in the repository. That is squarely the subject of sibling issue **#40**
("Explore scgallery and zerosoc as sources of realistic test-corpus
designs"), not this one — scgallery's designs are very likely built *on*
lambdalib cells (it targets the same siliconcompiler `Design`/target
system), so this survey's finding is a direct input to #40's evaluation:
running scgallery requires standing up the same yosys+OpenROAD+SiliconCompiler
toolchain noted above, so the "is this worth it" call belongs with #40's
cost/benefit, not duplicated here.

## 3. Relevance to the generator layer in `docs/ARCHITECTURE.md`?

**No.** `docs/ARCHITECTURE.md`'s vision line — "spec → schematic/generator →
sized circuit → layout" — and its "Mining the outside world" section name
BAG-style generators as the relevant prior art: parameterized *analog*
circuit generators that produce sized, technology-specific transistor-level
implementations from a spec. lambdalib is a digital-ASIC RTL portability
library (standard cells, memory wrappers, I/O pads, a padring generator,
FPGA primitives) whose one nominally-analog category is, per §1 above,
explicitly non-representative digital placeholder logic. It solves a real
but different problem — write digital RTL once, retarget across PDKs via
SiliconCompiler — that is out of scope for this repo's analog/mixed-signal,
layout-generator-focused roadmap.

## Outcome

**Not adopted — no dependency, no `klt` subcommand, no KB entry.** Rationale
recap: lambdalib contributes no layout data of its own (§1), the path from
its RTL to GDS runs through a synthesis+APR toolchain this repo does not
have and is not scheduled to build (§2), and its only nominally-analog
category is documented by its own authors as a non-physical placeholder
(§3). This is a legitimate "not useful because…" outcome per #39's
acceptance criteria, not a gap in the survey.

**One live cross-reference, not a new issue:** #40 (scgallery/zerosoc) is
where this finding's one actionable thread — "lambdalib RTL compiled via
scgallery's rtl2gds flow could eventually produce full-chip GDS test
corpora" — actually needs to be weighed, because that evaluation is
inseparable from scgallery's own toolchain cost. Filing a second, competing
issue here would fragment that decision rather than inform it; a comment
was left on #40 instead pointing at this document.

No `ROADMAP.md` change: this finding does not open a new phase or capability
path (contrast the [SPICE corner-runner
spike](spice-corner-runner-spike.md), which proposed a schedulable
capability and earned a `ROADMAP.md` pointer). A "not adopted" survey result
has nothing to schedule.
