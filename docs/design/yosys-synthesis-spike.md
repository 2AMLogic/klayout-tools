# Spike: Yosys invocation survey (synthesis engine for a future `klt synthesize`)

**Status:** spike / survey. Nothing here authorises implementation. Per
[docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "How capabilities arrive," a new
engine class enters through a spike first — candidate-engine survey, proposed
JSON contract, wrap/build decision — before any build phase starts. This is
Phase 1 of [Epic #391](https://github.com/2AMLogic/klayout-tools/issues/391)
("adopt the digital engine class — Yosys + OpenROAD — RTL→GDS as a first-class
`klt` flow"), specifically issue #396: the Yosys half of Phase 1's three
parallel engine surveys (sibling issues: #397 for OpenROAD, #398 for
cocotb/Verilator/Icarus). #399 is the downstream step that turns all three
surveys into the three proposed JSON contracts (synthesis,
place-and-route, functional verification); this document is that issue's
synthesis-side input, following the structure the two prior accepted engine
surveys set:
[docs/design/siliconcompiler-core-survey.md](siliconcompiler-core-survey.md)
and
[docs/design/lvs-extraction-spike.md](lvs-extraction-spike.md).

**No dependency was added, no `klt` subcommand was written, and no code in
`src/klayout_tools/` changed as part of this spike.** Every finding below was
produced by actually invoking Yosys — not recalled from memory — the same
verification discipline the two prior surveys establish.

**Source-read provenance** (2026-08-03): local Yosys 0.67+post (build details
below), `yosys -p license` output, the Homebrew `yosys` formula's `License:`
metadata, GitHub's live repo/license API for `YosysHQ/yosys`,
`YosysHQ/abc`'s `copyright.txt`, and
`google/skywater-pdk-libs-sky130_fd_sc_hd`'s `LICENSE`/repo metadata; a local
volare-fetched sky130 PDK install (`~/.volare/sky130A`, an
[open_pdks](https://github.com/RTimothyEdwards/open_pdks) build tagged
`bdc9412b3e468c102d01b7cf6337be06ec6e9c9a`) for the worked example's
`sky130_fd_sc_hd` liberty file; `packages.ubuntu.com`'s live package page for
the `noble` (24.04) `yosys` apt package, since this repo's CI runs on
`ubuntu-latest`; and this repo's own `.github/workflows/ci.yml`,
`src/klayout_tools/pdk.py`, and `scripts/fetch-pdks.sh` for prior art and
current CI/PDK posture.

## 0. Where the input liberty file actually came from (a load-bearing correction)

The issue's own technical-context note is correct and worth restating up
front, because it changes where a future `klt synthesize` looks for its
liberty input: **this repo's `scripts/fetch-pdks.sh` (lambdapdk v0.2.17) does
not vendor `sky130_fd_sc_hd` standard-cell liberty.** Checked directly:
lambdapdk's `sky130` package ships `sky130io`/`sky130sram` support, not the
digital standard-cell library. The worked example in §4 below instead used a
**volare-installed open_pdks build** already present on this machine at
`~/.volare/sky130A` — exactly the install shape
`src/klayout_tools/pdk.py::find_pdk()` already knows how to discover
(`libs.ref/<variant>/lib/*.lib`, the same layout `list_cell_libraries()` and
`_parse_lib_corner()` already parse for `klt pdk`/`klt cells`). Concretely,
the liberty file used below lives at:

```
~/.volare/sky130A/libs.ref/sky130_fd_sc_hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib
```

**This is good news for Phase 2, not a gap to fill here.** A future
`klt synthesize` does not need a new PDK-fetch mechanism for standard-cell
liberty: it needs to call `find_pdk()` (already shipped, already tested) and
read `assets["libs_ref"] + "/sky130_fd_sc_hd/lib/<corner>.lib"` from whatever
install `klt pdk` already resolves — volare, ciel, or a conventional
open_pdks prefix, per `docs/cli/pdk.md`. `scripts/fetch-pdks.sh`/lambdapdk
remain the source for `sky130io`/`sky130sram`/analog primitive data (as they
are today); they are simply **not** the digital standard-cell liberty path,
and Phase 2 should not extend them to try to become one. The open question
this leaves for Phase 2's own scoping (not resolved here): what `klt
synthesize` should do when `find_pdk()` resolves an install that has no
`sky130_fd_sc_hd` liberty at all (e.g. a minimal/analog-only volare
variant) — most likely a clear "liberty not found for deck" error, matching
`klt drc`'s existing "deck requires an asset the resolved install doesn't
ship" posture, but that is Phase 2 implementation scope, not this survey's.

## 1. CLI / script-mode invocation surface

Yosys has three distinct ways to drive it, verified via `yosys --help` and
direct invocation:

| Mode | Invocation | What it's for |
| ---- | ---------- | -------------- |
| **Script mode** | `yosys -s script.ys` | Execute a `.ys` file — one Yosys command per line, `#`-comments, blank lines ignored. This is Yosys's own idiomatic form; every published sky130 flow (OpenLane, the Yosys manual's own examples) uses it. |
| **Inline command-string mode** | `yosys -p "cmd1; cmd2; cmd3"` | Same command language as `.ys`, passed as one semicolon-separated string on the command line — no temp file needed. Verified directly (§4 below uses this form for the STA/`tee` experiments). |
| **One-shot synthesis shortcut** | `yosys -S -o out.blif input.v` (or `-S` with `-b`/other backends) | A convenience shortcut that just calls the default `synth` script end-to-end for a simple RTL→gate-level-generic-cells run. **Does not do library mapping** (no `dfflibmap`/`abc -liberty`) — it maps to Yosys's internal generic cell library (`$_AND_`, `$_DFF_P_`, etc.), not real standard cells. Confirmed via `yosys --help`'s own description: *"a default script for transforming the Verilog input to a gate-level netlist."* Not what a `sky130_fd_sc_hd`-targeted synthesis run needs on its own. |

**Recommendation: script mode (`-s`), not `-S` and not building up a long
`-p` string in Python.** A real synthesis run needs an ordered sequence the
`-S` shortcut doesn't cover — read RTL, elaborate hierarchy, run `synth`,
map flip-flops to the liberty (`dfflibmap`), map combinational logic to the
liberty via ABC (`abc -liberty`), clean up, then emit both a stats summary
and the mapped netlist. A `klt synthesize` verb should **generate a `.ys`
script file into a scratch/output directory and invoke `yosys -s
<script>`**, the same "generate an input deck, invoke the engine
in-process-or-subprocess, read a written output file back" shape
`klt sim`'s corner-sweep deck generation and `klt extract`'s SPICE-writing
already use for other wrapped engines — not string-interpolate an
increasingly large `-p` argument, which gets fragile once quoting,
multi-line `-p`, and conditional passes (parasitics-style optional flags,
per the LVS spike's `--parasitics` precedent) are all in play. The generated
`.ys` script itself is a debuggable, saveable artifact — worth keeping
alongside the netlist output the same way `klt sim`'s generated corner deck
is kept (`sim.md` → environment/reproducibility discipline), not deleted
after the run.

A minimal, verified-working synthesis script for a `sky130_fd_sc_hd` target
(exact commands run for §4's worked example):

```
read_verilog gcd.v
hierarchy -check -top gcd
synth -top gcd
dfflibmap -liberty sky130_fd_sc_hd__tt_025C_1v80.lib
abc -liberty sky130_fd_sc_hd__tt_025C_1v80.lib
clean
stat -liberty sky130_fd_sc_hd__tt_025C_1v80.lib -json -top gcd
write_json gcd_netlist.json
write_verilog -noattr gcd_synth.v
```

Each line is a single, independently documented Yosys pass — this is not a
`synth`-only or `-S`-only invocation, and no `klt`-authored logic pass
(retiming, resource sharing, FSM extraction) is implied: `synth` itself
already runs the standard `proc`/`opt`/`fsm`/`memory`/`techmap`/`abc`
sequence (visible verbatim in §4's captured log) that every Yosys-based flow
(OpenLane included) relies on unmodified.

### What Yosys does *not* consume directly: timing constraints

Worth flagging explicitly because it shapes the synthesis contract's input
side (§4): **this invocation surface has no `.sdc`-consuming step.** Yosys's
`synth`/`abc -liberty` passes optimize combinational logic against the
liberty's own timing arcs (ABC's internal `dc2`/`dch` optimization uses the
per-cell delay data from the `.lib`), but nothing here reads an SDC file for
a target clock period or I/O delay budget the way OpenSTA/OpenROAD do
downstream. A `klt synthesize` request that carries a `constraints` field
(per the epic's proposed contract shape) would, at Phase 2, either (a) not
be consumed by the Yosys invocation at all and instead be carried through
unread to the Phase 4 P&R/STA step, or (b) inform only a coarse choice like
which liberty corner file to select (`sky130_fd_sc_hd__tt_025C_1v80.lib` vs.
a fast/slow corner) — never a per-path timing budget Yosys itself
optimizes against. This is a Phase 2 scoping decision, not resolved here;
flagging it now so #399's contract proposal does not assume Yosys reads
constraints it structurally cannot.

## 2. Output parsing: JSON stats vs. text-netlist parsing

Yosys offers (at least) three distinct output surfaces after a synthesis
run, all exercised live in this spike:

### (a) `stat -liberty <lib> -json [-top <mod>]` — a purpose-built metrics summary

`stat` is Yosys's own "print a summary of the design" pass. With
`-liberty`, it also computes **area** (a real number, from the liberty's
`area` attribute per cell type) and **sequential area** — not just a cell
tally. With `-json`, the summary is emitted as a small, flat JSON blob to
the log stream rather than a human-readable table.

**Capturing it cleanly requires `tee`, not raw stdout redirection**, because
`stat`'s JSON is written to Yosys's *log*, which is normally interleaved
with every other pass's log output (verified directly — see §4's full
`run.log`, where the JSON blob for this exact command appears at line 740,
sandwiched between ABC's re-integration log and the `write_json`/
`write_verilog` passes that follow it). The clean-capture pattern, verified
working in this spike:

```
tee -q -o stats.json stat -liberty sky130_fd_sc_hd__tt_025C_1v80.lib -json -top gcd
```

`tee`'s `-q` flag suppresses the command's normal log/console echo so
**only** the redirected file receives the JSON — confirmed by grepping the
full run log for the JSON's marker string (`"num_cells"`) after using
`tee -q -o`: zero matches in the log, two in the written file (the
`stats.json` file understandably repeats the block once for the named
module and once for the `design` rollup). Without `-q`, `tee -o` still
writes a clean file (also verified) but the JSON additionally leaks into the
interleaved log, which is fine for a human reading the log but means a
`klt`-side implementation must not try to regex the JSON back out of the
combined log — it must read the file `tee -o` wrote.

### (b) `write_json` — the full design-as-JSON netlist

`write_json` emits the **entire post-synthesis netlist** as JSON: every
module, every cell instance (type, per-instance parameters, per-port bit
connections keyed by internal signal-bit IDs), every wire's bit range. For
the worked example's ~45-line RTL module, this is **6,475 lines** of JSON
for 335 synthesized cells — comprehensive, but not a metrics summary. It
also **carries no area or timing data per cell** — an instance entry is
`{"type": "sky130_fd_sc_hd__clkinv_1", "parameters": {}, "attributes": {},
"connections": {...}}`, verified directly against a sampled cell — so
recovering an area rollup from this file means re-joining every instance's
`type` against the liberty's own area table by hand, which is exactly the
computation `stat -liberty` already does natively and correctly.

### (c) `write_verilog` — the mapped gate-level netlist as text

`write_verilog -noattr` emits the synthesized netlist as structural Verilog
(2,221 lines for the same design) — the actual deliverable a downstream P&R
tool consumes, not a reporting artifact. Parsing it for cell counts would
mean writing and maintaining a Verilog-instantiation-line scraper (regex or
a mini-parser over `module_type instance_name (.port(net), ...);` forms) —
strictly worse than either JSON path above for the sole purpose of "how many
cells, how much area."

### Recommendation, with rationale mapped to the synthesis contract's fields

**Use `stat -liberty <lib> -json -top <top>` via `tee -q -o <path>`
for the metrics `klt synthesize` needs (`cell_count`, `area`), and treat the
mapped netlist (`write_verilog`, or the design JSON from `write_json` if a
richer connectivity view is ever needed downstream) as a separate, path-
referenced artifact — never re-derive metrics from it.**

This is not "JSON is nicer than text" in the abstract — it's an
almost-exact field-for-field match to what the epic's proposed synthesis
contract (§ "Contracts to spike" in #391) already asks for:

| Contract field | Where it comes from |
| -------------- | -------------------- |
| `cell_count` | `stat -json`'s `num_cells` (335 in the worked example) — a direct field, no derivation. |
| `area` | `stat -json`'s `area` (2951.58, in the liberty's own unit — sky130's liberty declares µm² via `capacitive_load_unit`/library-level attributes, same unit-in-field-name discipline `klt`'s other verbs already use for `_um`-suffixed fields). |
| A `by_type` / cell-mix breakdown (not in the epic's rough sketch, but a natural additive field) | `stat -json`'s `num_cells_by_type` — already a sorted-key object, matching `klt drc`'s `rule_counts` / `klt extract`'s `device_counts` convention of a per-category count map. |
| "sequential vs. combinational area" (useful for a later P&R floorplan hint, not asked for explicitly) | `stat -json`'s `sequential_area` alongside `area` — a free additive field already computed by the engine (worked example: 1251.2 sequential / 1700.38 combinational, summing to the reported 2951.58 total). |
| `timing summary` | **Not available from Yosys with confidence — see §3.5 below.** This is the one contract field this survey cannot source directly from Yosys; flagged explicitly rather than papered over. |
| The netlist itself | `write_verilog` (a file path, referenced not inlined — same "artifacts are paths" discipline `klt extract`'s `netlist_path` already established). |

The reasoning mirrors the LVS spike's `devices[]`/`nets[]` vs. "the netlist
file is authoritative" split almost exactly: a **small, purpose-computed
JSON summary** for the metrics a caller actually wants to gate on
(`cell_count`, `area`) is the right shape for the `klt synthesize` response
body, while the **full mapped netlist stays a referenced file artifact**
(`write_verilog`'s output, or `write_json`'s design-JSON if a future
consumer needs full connectivity rather than a metrics rollup) — never
regenerate a metric by parsing the artifact when the engine already computed
it natively and correctly via `stat -liberty`.

## 3. Version and licensing check

### 3.1 Yosys core

Verified live, not recalled:

- **License: ISC**, confirmed two ways — `yosys -p license`'s own banner
  output (full text captured; the exact ISC permissive-license wording,
  "Permission to use, copy, modify, and/or distribute this software for any
  purpose with or without fee is hereby granted...") and, independently, the
  Homebrew `yosys` formula's own metadata (`License: ISC`) and the upstream
  `YosysHQ/yosys` repo's `COPYING` file fetched directly from GitHub
  (`ISC License / Copyright (C) 2012 - 2026 Claire Xenia Wolf`). Fully
  permissive — no GPL/copyleft posture to reconcile with `klt`'s MIT
  license, unlike the LVS spike's netgen (GPL) contrast case.
- **Maintenance status**: `YosysHQ/yosys` on GitHub — `pushed_at:
  2026-08-03` (today, at spike time), not archived, 4,644 stargazers.
  Actively maintained.
- **Bundled tech-mapper: ABC** (`yosys-abc`, invoked internally by the
  `abc -liberty` pass). Its license, fetched directly from
  `YosysHQ/abc`'s `copyright.txt`: a UC Berkeley permissive academic
  license ("Permission is hereby granted, without written agreement and
  without license or royalty fees, to use, copy, modify, and distribute
  this software... provided that the above copyright notice... appear in
  all copies," with the standard UC "as-is"/no-warranty disclaimer). Also
  fully permissive — the whole Yosys+ABC synthesis stack a `klt synthesize`
  would wrap is ISC + UC-permissive, no copyleft anywhere in it.

### 3.2 `sky130_fd_sc_hd` liberty compatibility

No version-specific incompatibility surfaced in this spike. `dfflibmap` and
`abc -liberty` both consumed the sky130 liberty file directly with no
parse errors or unsupported-construct warnings (see §4's full log — the
only warnings ABC emitted were expected multi-output-cell notices for
full-adder–shaped cells like `sky130_fd_sc_hd__fa_1`, not a compatibility
problem). Confirmed separately: `sky130_fd_sc_hd`'s own license (fetched
live from `google/skywater-pdk-libs-sky130_fd_sc_hd`'s `LICENSE` and repo
metadata) is **Apache-2.0** — permissive, same posture as every other open
PDK asset this repo already wraps, though note that specific mirror repo
itself is `archived` (`pushed_at: 2023-02-22`; the actively-maintained
distribution path today is open_pdks/volare, which is what §0/§4 actually
used).

### 3.3 Version used for the worked example (stated per the Test Plan requirement)

```
Yosys 0.67+post (git sha1 b8e7da6f40ae8f552c116bf6c359b07c6533e159, Release, AppleClang clang++ 21.0.0.21000099)
```

Installed via Homebrew (`brew info yosys` confirms `yosys 0.67 (stable,
bottled)`) on this machine. This is a recent release build, not a nightly
or a hand-built HEAD checkout.

### 3.4 CI reproducibility — explicitly unconfirmed, and here is the concrete gap

The issue's technical-context note asks this survey to state plainly
whether CI has (or needs) a reproducible Yosys source, not just "works on
the author's machine." Checked directly:

- This repo's `.github/workflows/ci.yml` installs no Yosys today (its only
  `apt-get install` line is for `ngspice`, for `klt sim`).
- This repo's CI runs on `ubuntu-latest`, which at spike time resolves to
  Ubuntu 24.04 ("noble"). Checked live against `packages.ubuntu.com`'s own
  `noble` package page: **Ubuntu 24.04's `apt` `yosys` package is
  `0.33-5build2`** — roughly thirty upstream releases behind the `0.67`
  used for this spike's worked example. `apt-get install yosys` in CI would
  therefore **not** reproduce this survey's findings — an old-enough Yosys
  may lack passes/flags this document relies on (`stat -json`'s exact field
  set has grown over releases; `dfflibmap`'s legalization behavior has also
  changed across versions).
- **This is a real, named gap for Phase 2, not resolved here.** Two
  credible paths, neither implemented by this spike: (a) YosysHQ's own
  prebuilt release artifacts (published far more frequently and at far
  newer versions than any Linux distro's `apt` package), or (b) building
  from source in CI (slower, but exactly reproducible to a pinned commit).
  Phase 2's own implementation issue should pick one and pin an exact
  version/commit the way `scripts/fetch-pdks.sh` pins an exact lambdapdk
  tag+checksum — "works on the author's machine" is not sufficient
  evidence of CI-readiness, and this survey deliberately does not claim it
  is.

**Resolved (issue #417).** Phase 2 picked option (b): CI now builds Yosys
from source, pinned to the official `v0.67` release tag (checksum-verified,
`scripts/install-yosys.sh`) — an exact, citable upstream release rather than
an arbitrary post-tag commit. A companion script
(`scripts/fetch-sky130-liberty.sh`) fetches the one real, Yosys-parseable
`sky130_fd_sc_hd__tt_025C_1v80.lib` this section 0's worked example needs
from a pinned volare/open_pdks release (the same commit section 0's
`~/.volare/sky130A` install above was built from), so
`tests/test_synthesize.py::test_integration_real_yosys_gcd_worked_example`
now runs for real in CI instead of always skipping — see
`.github/workflows/ci.yml`'s `test` job and `scripts/README.md` for both
scripts. Verified locally against this exact fetched liberty file: a real
Yosys `0.67` build reproduces this section's own worked-example numbers
(335 instances, 2951.5808 µm², 1251.2 µm² sequential) exactly, no drift to
document.

### 3.5 Known version-to-version risk for Phase 2 stability

Flagging one concrete behavioral difference discovered live rather than
recalled: Yosys's own `sta` pass (§3.6 below) could not produce a usable
timing report against the mapped `sky130_fd_sc_hd` netlist in this spike's
0.67+post build — every mapped standard-cell type was reported
`not recognised`. Whether an older or newer Yosys release changes this
(e.g. a future release wires `sta` through `read_liberty`-loaded timing
arcs more completely) is exactly the kind of behavior a pinned CI version
(§3.4) needs to fix in place — a `klt synthesize` contract that assumed
`sta` "just works" today could silently start passing or failing
differently on the next Yosys point release.

### 3.6 Timing: the honest gap

Tried directly, two ways, both captured in full in this repo's scratch
history for this spike:

1. `sta` immediately after the mapped, liberty-legalized netlist
   (`dfflibmap` + `abc -liberty` already run): **every one of the 36 mapped
   `sky130_fd_sc_hd__*` cell types is reported `not recognised! Ignoring.`**,
   and the pass concludes `No timing paths found.` `sta` does not
   automatically pull per-cell delay arcs from the same liberty file
   `abc -liberty` just mapped against — it needs its own explicit
   `read_liberty` load.
2. Adding `read_liberty -lib -ignore_miss_dir <lib>` before `sta` (the
   documented way to load timing-only blackbox modules for cells already
   present in the design): this **failed outright** —
   `ERROR: Module 'sky130_fd_sc_hd__clkinv_1' is used with parameters but
   is not parametric!` — because the cells already instantiated by
   `abc -liberty` collide with the liberty-frontend's own freshly-parsed
   module definitions for the same cell names.
3. `ltp` (longest topological path, a purely structural proxy with no
   delay units) reported `length=0` against the mapped netlist — it does
   not recognize the liberty-mapped standard-cell types as sequential
   elements the way it does Yosys's own native `$_DFF_*` cells, so it
   cannot walk register-to-register paths through them either.

**Conclusion: Yosys's own timing capability, at least as invoked in this
spike, cannot reliably produce the "timing summary" field the epic's rough
synthesis contract sketch asks for**, at least not without deeper,
version-sensitive massaging this spike did not find a working recipe for.
This matches the broader open-source sky130 flow's own division of labor:
every real sky130 flow (OpenLane included) hands the Yosys-produced
gate-level netlist to **OpenSTA** (bundled inside OpenROAD — this epic's
Phase 4 engine) for the authoritative static timing report, using an SDC
file OpenSTA reads directly. **Recommendation for #399: scope the
synthesis contract's `timing summary` field as either omitted at Phase 2
(cell count + area only, a real and useful partial contract), or explicitly
deferred to Phase 4's OpenROAD/OpenSTA invocation** (which already needs to
run STA for P&R signoff) rather than committing to a Yosys-native timing
field this spike could not get working. This is a scope boundary worth
resolving explicitly in #399's contract design, the same way the LVS spike
explicitly deferred parasitics rather than half-committing to it.

## 4. Worked example: GCD synthesized against `sky130_fd_sc_hd`

### Inputs

**RTL** (`gcd.v`, 45 lines) — a register-transfer-level GCD (subtractive
Euclidean algorithm), parametrized 16-bit width, with a `start`/`done`
handshake:

```verilog
module gcd #(
    parameter WIDTH = 16
) (
    input  wire             clk,
    input  wire             rst_n,
    input  wire              start,
    input  wire [WIDTH-1:0] a_in,
    input  wire [WIDTH-1:0] b_in,
    output reg              done,
    output reg  [WIDTH-1:0] result
);

    reg [WIDTH-1:0] a, b;
    reg             busy;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            a      <= {WIDTH{1'b0}};
            b      <= {WIDTH{1'b0}};
            busy   <= 1'b0;
            done   <= 1'b0;
            result <= {WIDTH{1'b0}};
        end else if (start && !busy) begin
            a      <= a_in;
            b      <= b_in;
            busy   <= 1'b1;
            done   <= 1'b0;
        end else if (busy) begin
            if (b == {WIDTH{1'b0}}) begin
                busy   <= 1'b0;
                done   <= 1'b1;
                result <= a;
            end else if (a > b) begin
                a <= a - b;
            end else begin
                b <= b - a;
            end
        end else begin
            done <= 1'b0;
        end
    end

endmodule
```

**Liberty**: `sky130_fd_sc_hd__tt_025C_1v80.lib` (typical-typical corner,
25°C, 1.80V nominal) from the volare install described in §0.

**Top module**: `gcd` (no sub-hierarchy — a single-module design, the
simplest case; a real `klt synthesize` request would pass `--top` the same
way `klt extract`/`klt drc` already take a `--top` flag for multi-cell
streams).

**Constraints**: none supplied — per §1's finding, Yosys's own synthesis
passes have no SDC-consuming step for this flow, so there is nothing to
pass at this stage.

### Invocation

```
$ yosys -s synth_gcd.ys
```

where `synth_gcd.ys` is exactly the script listed in §1. Full raw log
captured (864 lines); key excerpts below, unedited.

**Frontend + elaboration** (opening banner and hierarchy pass):

```
 /----------------------------------------------------------------------------\
 |  yosys -- Yosys Open SYnthesis Suite                                       |
 |  Copyright (C) 2012 - 2026  Claire Xenia Wolf <claire@yosyshq.com>         |
 |  Distributed under an ISC-like license, type "license" to see terms        |
 \----------------------------------------------------------------------------/
 Yosys 0.67+post (git sha1 b8e7da6f40ae8f552c116bf6c359b07c6533e159, Release, AppleClang clang++ 21.0.0.21000099)

-- Executing script file `synth_gcd.ys' --

1. Executing Verilog-2005 frontend: gcd.v
Parsing Verilog input from `gcd.v' to AST representation.
Generating RTLIL representation for module `\gcd'.
Successfully finished Verilog frontend.

2. Executing HIERARCHY pass (managing design hierarchy).
...
Top module:  \gcd
```

**Post-`synth` generic-cell statistics** (before liberty mapping — Yosys's
own internal cell library):

```
=== gcd ===

        +----------Local Count, excluding submodules.
        |
      197 wires
      302 wire bits
       10 public wires
       85 public wire bits
        7 ports
       52 port bits
      267 cells
        5   $_ANDNOT_
       48   $_AND_
       49   $_DFFE_PN0P_
        1   $_DFF_PN0_
       33   $_MUX_
       54   $_NAND_
        9   $_NOR_
       34   $_ORNOT_
        2   $_OR_
       10   $_XNOR_
       22   $_XOR_
```

**`dfflibmap` mapping flip-flops to `sky130_fd_sc_hd`**:

```
4. Executing DFFLIBMAP pass (mapping DFF cells to sequential cells from liberty file).
  cell sky130_fd_sc_hd__dfxtp_1 (noninv, pins=3, area=20.02) is a direct match for cell type $_DFF_P_.
  cell sky130_fd_sc_hd__dfrtn_1 (noninv, pins=4, area=25.02) is a direct match for cell type $_DFF_NN0_.
  cell sky130_fd_sc_hd__dfrtp_1 (noninv, pins=4, area=25.02) is a direct match for cell type $_DFF_PN0_.
  cell sky130_fd_sc_hd__dfstp_2 (noninv, pins=4, area=26.28) is a direct match for cell type $_DFF_PN1_.
  cell sky130_fd_sc_hd__edfxtp_1 (noninv, pins=4, area=30.03) is a direct match for cell type $_DFFE_PP_.
  ...
4.1. Executing DFFLEGALIZE pass (convert FFs to types supported by the target).
Mapping DFF cells in module `\gcd':
  mapped 50 $_DFF_PN0_ cells to \sky130_fd_sc_hd__dfrtp_1 cells.
```

**`abc -liberty` mapping combinational logic** (final re-integration
summary — every cell type below is a real `sky130_fd_sc_hd` standard cell):

```
5.1.2. Re-integrating ABC results.
ABC RESULTS:   sky130_fd_sc_hd__a211o_1 cells:        1
ABC RESULTS:   sky130_fd_sc_hd__a211oi_1 cells:        1
ABC RESULTS:   sky130_fd_sc_hd__a21boi_0 cells:        2
ABC RESULTS:   sky130_fd_sc_hd__a21o_1 cells:        5
ABC RESULTS:   sky130_fd_sc_hd__a21oi_1 cells:       30
ABC RESULTS:   sky130_fd_sc_hd__a22oi_1 cells:        1
ABC RESULTS:   sky130_fd_sc_hd__a311oi_1 cells:        6
ABC RESULTS:   sky130_fd_sc_hd__a31oi_1 cells:        7
ABC RESULTS:   sky130_fd_sc_hd__and2_0 cells:        5
ABC RESULTS:   sky130_fd_sc_hd__and3_1 cells:        2
ABC RESULTS:   sky130_fd_sc_hd__clkinv_1 cells:        4
ABC RESULTS:   sky130_fd_sc_hd__lpflow_inputiso1p_1 cells:        3
ABC RESULTS:   sky130_fd_sc_hd__lpflow_isobufsrc_1 cells:       16
ABC RESULTS:   sky130_fd_sc_hd__maj3_1 cells:        3
ABC RESULTS:   sky130_fd_sc_hd__mux2_1 cells:       16
ABC RESULTS:   sky130_fd_sc_hd__mux2i_1 cells:        2
ABC RESULTS:   sky130_fd_sc_hd__nand2_1 cells:       35
ABC RESULTS:   sky130_fd_sc_hd__nand2b_1 cells:       18
ABC RESULTS:   sky130_fd_sc_hd__nand3_1 cells:        9
ABC RESULTS:   sky130_fd_sc_hd__nand4_1 cells:        3
ABC RESULTS:   sky130_fd_sc_hd__nor2_1 cells:       36
ABC RESULTS:   sky130_fd_sc_hd__nor2b_1 cells:        1
ABC RESULTS:   sky130_fd_sc_hd__nor3_1 cells:        6
ABC RESULTS:   sky130_fd_sc_hd__nor3b_1 cells:        1
ABC RESULTS:   sky130_fd_sc_hd__nor4_1 cells:        3
ABC RESULTS:   sky130_fd_sc_hd__o211ai_1 cells:        3
ABC RESULTS:   sky130_fd_sc_hd__o21a_1 cells:        3
ABC RESULTS:   sky130_fd_sc_hd__o21ai_0 cells:       32
ABC RESULTS:   sky130_fd_sc_hd__o22ai_1 cells:        1
ABC RESULTS:   sky130_fd_sc_hd__o311ai_0 cells:        5
ABC RESULTS:   sky130_fd_sc_hd__o31ai_1 cells:        3
ABC RESULTS:   sky130_fd_sc_hd__or3_1 cells:        2
ABC RESULTS:   sky130_fd_sc_hd__or4_1 cells:        1
ABC RESULTS:   sky130_fd_sc_hd__xnor2_1 cells:       13
ABC RESULTS:   sky130_fd_sc_hd__xor2_1 cells:        6
ABC RESULTS:        internal signals:      216
ABC RESULTS:           input signals:       83
ABC RESULTS:          output signals:       50
```

**`stat -liberty ... -json -top gcd`** — the full, unedited JSON emitted to
the log (this is the exact block `tee -q -o` isolates cleanly into its own
file per §2's recommendation):

```json
{
   "creator": "Yosys 0.67+post (git sha1 b8e7da6f40ae8f552c116bf6c359b07c6533e159, Release, AppleClang clang++ 21.0.0.21000099)",
   "invocation": "stat -liberty sky130_fd_sc_hd__tt_025C_1v80.lib -json -top gcd ",
   "modules": {
      "\\gcd": {
         "num_wires":         295,
         "num_wire_bits":     370,
         "num_pub_wires":     10,
         "num_pub_wire_bits": 85,
         "num_ports":         7,
         "num_port_bits":     52,
         "num_memories":      0,
         "num_memory_bits":   0,
         "num_processes":     0,
         "num_cells":         335,
         "num_submodules":       0,
         "area":              2951.580800,
         "sequential_area":    1251.200000,
         "num_cells_by_type": {
            "sky130_fd_sc_hd__a211o_1": 1,
            "sky130_fd_sc_hd__a211oi_1": 1,
            "sky130_fd_sc_hd__a21boi_0": 2,
            "sky130_fd_sc_hd__a21o_1": 5,
            "sky130_fd_sc_hd__a21oi_1": 30,
            "sky130_fd_sc_hd__a22oi_1": 1,
            "sky130_fd_sc_hd__a311oi_1": 6,
            "sky130_fd_sc_hd__a31oi_1": 7,
            "sky130_fd_sc_hd__and2_0": 5,
            "sky130_fd_sc_hd__and3_1": 2,
            "sky130_fd_sc_hd__clkinv_1": 4,
            "sky130_fd_sc_hd__dfrtp_1": 50,
            "sky130_fd_sc_hd__lpflow_inputiso1p_1": 3,
            "sky130_fd_sc_hd__lpflow_isobufsrc_1": 16,
            "sky130_fd_sc_hd__maj3_1": 3,
            "sky130_fd_sc_hd__mux2_1": 16,
            "sky130_fd_sc_hd__mux2i_1": 2,
            "sky130_fd_sc_hd__nand2_1": 35,
            "sky130_fd_sc_hd__nand2b_1": 18,
            "sky130_fd_sc_hd__nand3_1": 9,
            "sky130_fd_sc_hd__nand4_1": 3,
            "sky130_fd_sc_hd__nor2_1": 36,
            "sky130_fd_sc_hd__nor2b_1": 1,
            "sky130_fd_sc_hd__nor3_1": 6,
            "sky130_fd_sc_hd__nor3b_1": 1,
            "sky130_fd_sc_hd__nor4_1": 3,
            "sky130_fd_sc_hd__o211ai_1": 3,
            "sky130_fd_sc_hd__o21a_1": 3,
            "sky130_fd_sc_hd__o21ai_0": 32,
            "sky130_fd_sc_hd__o22ai_1": 1,
            "sky130_fd_sc_hd__o311ai_0": 5,
            "sky130_fd_sc_hd__o31ai_1": 3,
            "sky130_fd_sc_hd__or3_1": 2,
            "sky130_fd_sc_hd__or4_1": 1,
            "sky130_fd_sc_hd__xnor2_1": 13,
            "sky130_fd_sc_hd__xor2_1": 6
         }
      }
   }
}
```

(`design` rollup key omitted here for brevity — identical to the `\gcd`
entry above for this single-module design, exactly as captured live.)

**Run outcome**: `yosys -s synth_gcd.ys` exited **`0`**. `stat`'s
`num_cells: 335` splits as **285 combinational + 50 sequential**
(`sky130_fd_sc_hd__dfrtp_1` ×50, confirmed against the `ABC RESULTS`/
`DFFLEGALIZE` excerpts above), summing to the reported `area: 2951.5808`
(µm², sky130's liberty unit) — `sequential_area: 1251.2` (the 50 DFFs ×
25.02 µm² each, matching `dfflibmap`'s own reported per-cell area above)
plus **1700.3808 µm² of combinational area** (`area - sequential_area`,
computed directly from the two reported fields, not re-measured).

**Total wall time**: `1.74s` for this design (from the log's own closing
line, `End of script. Logfile hash: fbad905188, time: 1.74s, ...`) — trivial
for a toy module; not a claim about scaling to the epic's actual
marketing#56 GCD/RSA-modexp canary block, which is a substantially larger
design.

## 5. Recommendation summary

| Question | Verdict |
| -------- | ------- |
| Invocation surface | Generate a `.ys` script per run (`read_verilog` → `hierarchy` → `synth` → `dfflibmap` → `abc -liberty` → `clean` → `stat`/`write_*`) and invoke `yosys -s <script>`. Not the `-S` shortcut (no liberty mapping) and not an ever-growing `-p` string. |
| Output parsing | `stat -liberty <lib> -json -top <top>` captured via `tee -q -o <path>` for `cell_count`/`area`/`sequential_area`/`num_cells_by_type` — a purpose-built, already-correct summary. Treat `write_verilog`'s mapped netlist (or `write_json`'s full design JSON, if a future consumer needs full connectivity) as a referenced artifact, never re-derived-from for metrics. |
| Licensing | Yosys core: ISC (permissive). Bundled ABC: UC Berkeley permissive academic license. `sky130_fd_sc_hd` liberty: Apache-2.0. No copyleft anywhere in this stack — fully compatible with wrapping behind `klt`'s MIT license. |
| Version/CI | `0.67+post` used here (Homebrew, current). **CI reproducibility is an open gap**: Ubuntu 24.04's `apt` `yosys` is `0.33-5build2`, ~30 releases stale — Phase 2 must pin a real version (prebuilt release or a from-source build), not assume `apt-get install yosys` reproduces this. |
| Synthesis-contract mapping | `cell_count`/`area` map directly and reliably to `stat -json`'s `num_cells`/`area`. **`timing summary` does not** — Yosys's own `sta`/`ltp` passes could not produce a usable result against the liberty-mapped netlist in this spike; recommend #399 either omit timing at Phase 2 or defer it explicitly to Phase 4's OpenROAD/OpenSTA invocation. |
| PDK input | `sky130_fd_sc_hd` liberty is **not** in this repo's fetched lambdapdk payload; it comes from a volare/open_pdks install via the same `find_pdk()`/`libs_ref` discovery `klt pdk`/`klt cells` already use — no new PDK-resolution mechanism needed for Phase 2. |

## Out of scope for this spike

No `klt` subcommand, dependency, or code was added. Phase 2 (the synthesis
verb build) and #399 (the three-contract proposal, consuming this survey
alongside #397/#398) carry the next steps, gated on these findings — the
same "spike, then build" sequencing the LVS and SPICE-corner-runner spikes
established for their own epics.
