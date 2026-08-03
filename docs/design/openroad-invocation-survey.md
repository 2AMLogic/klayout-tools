# Survey: OpenROAD invocation surface (place-and-route)

**Status:** survey / spike. Nothing here authorises implementation. Filed for
Epic #391 ("adopt the digital engine class — Yosys + OpenROAD") Phase 1,
issue #397 — the OpenROAD half of the three-engine survey (Yosys is #396,
cocotb/Verilator/Icarus is #398). Findings here are input to #399, the shared
Phase 1 issue that proposes the synthesis / place-and-route /
functional-verification JSON contracts — this document surveys the engine,
it does not itself propose the contract. Consistent with
[docs/design/digital-fleet-unit-abstraction-decision.md](digital-fleet-unit-abstraction-decision.md)
(#400, merged), which already names "place-and-route" as one of three
contracts composed serially per candidate — nothing here revisits that
decision, it grounds it.

**What was actually run vs. what is read from source.** Per this survey's
own acceptance criteria, every number below is labelled by its evidence
tier: **[RUN]** — produced live in this sandbox during this survey, from
raw OpenROAD/yosys output, not summarised from memory; **[SOURCE]** — read
directly from OpenROAD-flow-scripts' (ORFS) own Tcl/Makefile/Python source
in the container, not from its documentation; **[VERIFIED]** — a claim
checked directly against this repo's own installed `pip install klayout`
package in a live Python REPL. Nothing below is asserted from OpenROAD's
docs alone with no local check — where the sandbox blocked full execution
(see "Environment limitation" below), the fallback was reading ORFS's own
source, not paraphrasing its README.

## Environment limitation: no native OpenROAD in this sandbox, real x86_64 execution via Docker

`which openroad` and `brew search openroad` both come back empty in this
macOS/arm64 sandbox — OpenROAD has no Homebrew formula and no conda channel
was available (no `conda`/`mamba`/`micromamba` installed). But Docker
Desktop **is** available with a working daemon, and OpenROAD publishes an
official image with the full toolchain pre-built:

```
docker pull --platform linux/amd64 openroad/orfs:latest
```

This pulled and ran successfully — Docker Desktop's `linux/amd64` emulation
on Apple Silicon (QEMU-based) executed real x86_64 OpenROAD/Yosys/KLayout
binaries, not a paraphrase of their docs. Versions confirmed live **[RUN]**:

| Tool | Version | How confirmed |
| --- | --- | --- |
| OpenROAD | `26Q3-771-g7cfb2105c9` | `openroad -version` inside the container |
| Yosys (ORFS-bundled) | `0.67` (`sha1 2d1509d1b`) | `yosys -V` inside the container |
| KLayout (ORFS-bundled) | `0.30.7` | `klayout -v` inside the container |
| KLayout (this repo's pip dependency) | `0.30.10` | `.venv/bin/python3 -c "import klayout; print(klayout.__version__)"`, same repo checkout used for this survey |

The two KLayout versions being one patch apart (`0.30.7` in ORFS's image vs.
`0.30.10` this repo already depends on) matters for the GDS section below —
the API surface used there (`Technology`, `LoadLayoutOptions.lefdef_config`)
is stable across that range and was independently re-verified against the
newer version this repo actually ships.

**What did not complete: routing and CTS crashed under emulation, not under
OpenROAD.** The worked example below (§3) ran real synthesis, floorplanning,
global placement, resizing, and detailed placement — captured from raw
OpenROAD log/report output, not fabricated. Clock-tree synthesis started
(H-tree generation began for the `clk` net, 35 sinks) and then the container
process was killed mid-stage:

```
[INFO CTS-0027] Generating H-Tree topology for net clk.
[INFO CTS-0028]  Total number of sinks: 35.
 Level 1
    Direction: Vertical
    Sinks per sub-region: 18
...
Error: cts.tcl, 81 child killed: illegal instruction
```

`illegal instruction` from a *specific compiled x86_64 binary* running under
QEMU translation on an Apple Silicon host is the signature of an emulation
gap (an instruction QEMU's user-mode translator doesn't support, most likely
in TritonCTS's or OpenSTA's numerical code), not an OpenROAD logic defect —
but this was observed **once**, not independently reproduced by a retry: a
second attempt tried disabling the design's `OPENROAD_HIERARCHICAL=1` config
to rule out the (separately flagged, "in development") hierarchical-flow
path, but that attempt failed earlier, in synthesis, because `gcd`'s own
`config.mk` requires `OPENROAD_HIERARCHICAL=1` for its arithmetic-swap
optimization and errors out without it — a config-compatibility failure
unrelated to the CTS crash, not a second data point on it. **Routing, final
GDS generation, and the DRC/LVS-on-generated-GDS steps for this specific
worked example were not completed live in this sandbox.** The DEF→GDS
mechanism itself (§4) was still verified directly from ORFS's own source and
cross-checked against this repo's installed KLayout package, independent of
finishing this particular run. A native x86_64 host, a `linux/amd64` GitHub
Actions runner, or an emulation-clean container base would be the way to
confirm whether this specific crash is fundamental to the toolchain or
incidental to this one QEMU environment — worth a note for whoever scopes
CI for a future `klt place-and-route` verb, not a finding about OpenROAD
itself.

## 1. CLI/Tcl invocation surface

OpenROAD's own CLI **[SOURCE, confirmed via `openroad -help` [RUN]]**:

```
openroad [-help] [-version] [-no_init] [-no_splash] [-exit] [-gui] [-web]
         [-threads count|max] [-log file_name] [-metrics file_name]
         [-db file_name] [-no_settings] [-minimize] [-python] cmd_file
```

The batch invocation the issue names is exactly right:
`openroad -no_init -exit script.tcl` — `-no_init` skips the user's
`~/.openroad` init file (reproducibility: no machine-local Tcl state leaks
in), `-exit` terminates after the script runs instead of dropping to an
interactive Tcl prompt. `-gui`/`-web` are opt-in and never required —
headless is OpenROAD's own default mode, not something to suppress.

**There is no `-rd KEY=value` equivalent on the OpenROAD binary itself** —
that mechanism (seen in the sibling
[siliconcompiler-core-survey.md](siliconcompiler-core-survey.md) survey) is
specific to KLayout's `-r script.py -rd key=value` batch-script parameter
injection. OpenROAD-flow-scripts (ORFS) instead parameterizes its per-stage
Tcl scripts through **shell environment variables**, read inside the script
via `$::env(VAR_NAME)` **[SOURCE, `flow/scripts/floorplan.tcl`]** — e.g.
`$::env(CORE_UTILIZATION)`, `$::env(PLACE_SITE)`, `$::env(DONT_USE_CELLS)`.
A minimal `klt` wrapper has the same two real options as ORFS: env-var
injection (matches this ecosystem's convention, easy to `export` from
Python via `subprocess.run(..., env=...)`) or generating the Tcl script
text per invocation (more explicit, easier to unit-test the generated
script, no risk of an unset/misspelled env var silently falling through to
a Tcl default). Either is a small, self-contained decision for whoever
implements the P&R verb — this survey does not need to make it.

**OpenROAD's native Tcl API is rich and stage-shaped**, and this is the
layer a `klt` wrapper should drive directly rather than adopting ORFS's
Makefile as infrastructure — the same "wrap the engine, not the
orchestrator" conclusion the siliconcompiler survey reached for
`siliconcompiler`'s `Flowgraph`/`Project` monolith applies here for exactly
the same reason (ORFS's `Makefile` is ~800 lines of `make` orchestrating a
fixed 6-stage pipeline with its own env-var-driven config surface — useful
prior art, not something to depend on). Real commands used at each stage,
read from ORFS's own per-stage `.tcl` scripts and confirmed by name in the
real run's log:

| ORFS stage (real, from this run's log) | Key native OpenROAD Tcl commands used |
| --- | --- |
| `1_synth` (yosys, not OpenROAD) | n/a — see the sibling Yosys survey, #396 |
| `2_1_floorplan` | `initialize_floorplan`, `make_tracks`, `set_global_routing_layer_adjustment`, `set_routing_layers`, `set_dont_use` |
| `2_2_floorplan_macro`, `2_3_floorplan_tapcell`, `2_4_floorplan_pdn` | macro placement / tapcell insertion (`tapcell` package) / power-grid generation (`pdngen`) |
| `3_1_place_gp_skip_io`, `3_3_place_gp` | `global_placement` (Nesterov-based `gpl`) |
| `3_2_place_iop` | `place_pins` (I/O placer, `ppl`) |
| `3_4_place_resized` | `repair_design`, `repair_timing` (resizer, `rsz`) |
| `3_5_place_dp` | `detailed_placement` (`dpl`), `optimize_mirroring` |
| `4_1_cts` (crashed mid-stage, see above) | `clock_tree_synthesis` (`cts`/TritonCTS) |
| `5_*` (not reached) | `global_route` (`grt`/FastRoute), `detailed_route` (`drt`/TritonRoute) — read from `Makefile`/`fastroute.tcl`, not run |
| `6_*` (not reached) | `write_def`, then KLayout DEF→GDS merge — see §4 |

Every stage also calls a shared `report_metrics.tcl` helper
(`report_metrics <stage> <when>`) that writes both a human-readable `.rpt`
report *and* calls parallel `*_metric` Tcl procs
(`report_tns_metric`, `report_worst_slack_metric`, `report_fmax_metric`,
`report_erc_metrics`, `report_clock_skew_metric`) **[SOURCE,
`flow/scripts/report_metrics.tcl`]** — this is the most important finding
for #399's contract design, expanded in §5.

## 2. Floorplan input requirements

**[SOURCE, `flow/scripts/floorplan.tcl`]**, confirmed running live in this
survey's own worked example: ORFS's floorplan step supports exactly four
**mutually exclusive** floorplan-initialization methods (the script checks
`methods_defined > 1` and errors if more than one is set), each backed
directly by an OpenROAD Tcl command:

| Method | Env vars | Tcl call | Notes |
| --- | --- | --- | --- |
| 1. Existing DEF | `FLOORPLAN_DEF` | `read_def -floorplan_initialize` | Re-use a floorplan (die/core area, sometimes macro placement) from a prior run or a hand-authored DEF. |
| 2. Footprint (IO ring) | `FOOTPRINT`, `SIG_MAP_FILE` | ICeWall's `load_footprint` / `get_die_area` / `get_core_area` / `init_footprint` | ORFS's answer to "IO ring strategy" — `ICeWall` is a Tcl package for chips with a padframe/IO-ring (pad placement, ESD/corner cells). Padframe-specific, not needed for a core-only block like `gcd`. |
| 3. Explicit die/core coordinates | `DIE_AREA`, `CORE_AREA`, `PLACE_SITE` | `initialize_floorplan -die_area {llx lly urx ury} -core_area {...} -site <name>` | Most direct: caller states the rectangle in microns. |
| 4. Utilization-driven (**used by the worked example below**) | `CORE_UTILIZATION`, `CORE_ASPECT_RATIO`, `CORE_MARGIN`, `PLACE_SITE` | `initialize_floorplan -utilization <pct> -aspect_ratio <r> -core_space <um> -site <name>` | OpenROAD computes die/core area from the synthesized cell area, a target utilization percentage, an aspect ratio, and a margin. This is what `gcd`'s `config.mk` uses (`CORE_UTILIZATION = 38`). |

`PLACE_SITE` (e.g. `unithd` for sky130hd) names a site defined in the
technology LEF — the row-height/pitch unit `initialize_floorplan` snaps the
core boundary to. **IO pin placement** is a separate step from floorplan
initialization: `IO_PLACER_H`/`IO_PLACER_V` name the horizontal/vertical
metal layers (`met3`/`met2` for sky130hd) that `place_pins` (stage
`3_2_place_iop` above) assigns pins to — confirmed live in the worked
example's own log: 54 I/O pins placed across 280 available perimeter slots,
I/O-nets HPWL 1030.21 µm **[RUN]**.

**What a minimal `klt` floorplan spec needs**, distilled from the four
methods above: a **site name** (from the tech LEF), **one** of
{die+core rectangle} or {utilization, aspect ratio, margin}, an **IO pin
layer choice** (H/V routing layers) or an explicit per-pin placement list,
and optionally a macro placement / IO-ring strategy for designs with hard
macros or a padframe (out of scope for a `gcd`-sized core-only canary, but
real for anything with SRAM or a padframe). Method 4
(utilization-driven) is the natural default for a `klt` v1 — it needs the
fewest inputs and is exactly what produced the real numbers in §3.

## 3. Worked example: `gcd` synthesized and placed against sky130hd (real run)

**Design:** ORFS's own `flow/designs/sky130hd/gcd` example — the same `gcd`
design already referenced elsewhere in this repo's design docs (see
[docs/design/digital-fleet-unit-abstraction-decision.md](digital-fleet-unit-abstraction-decision.md)'s
own framing of a synth+P&R "candidate"). Verilog source is ORFS's stock
`gcd.v`; constraint file sets `clk_period = 1.1` ns (909 MHz) — a
deliberately aggressive constraint baked into ORFS's own stock example (not
chosen by this survey), which is why the timing numbers below show
persistent negative slack; that is expected behavior of the stock example,
not evidence of anything wrong with the flow.

**Command** (real, run inside the `openroad/orfs:latest` container):

```
cd /OpenROAD-flow-scripts/flow
make DESIGN_CONFIG=./designs/sky130hd/gcd/config.mk
```

This is ORFS's Makefile-driven orchestration, described in §1 as
infrastructure worth reading rather than adopting — used here only because
it is the fastest way to exercise the real per-stage Tcl scripts and
capture real OpenROAD output for this survey, not a recommendation to wrap
it as the P&R verb's implementation.

**Results, every number below read directly from raw `.rpt`/log output in
this run — [RUN], nothing paraphrased or estimated:**

| Stage | Metric | Value |
| --- | --- | --- |
| Synthesis (yosys) | Cell count / area (top `gcd` module) | 222 cells, ≈2,313 µm² (`synth_stat.txt`, `IFP-0103 Total instances area`) |
| Floorplan (`CORE_UTILIZATION=38`) | Die / core bounding box | Die (0,0)–(80.025, 80.025) µm; Core (1.38, 2.72)–(78.66, 78.88) µm |
| Floorplan | Core area / effective utilization | 5,885.645 µm² / 39.3% (`IFP-0102`/`IFP-0104`) |
| Floorplan (ideal-clock STA, ERC not yet placement-aware) | WNS / TNS | −1.15 ns / −48.24 ns (`2_floorplan_final.rpt`) |
| IO placement | Pins placed / available slots / IO-nets HPWL | 54 / 280 / 1,030.21 µm (`PPL-0001..0012`) |
| Global placement | Utilization after resizer/buffer insertion | 60.8% → 73.5% (routability-driven cell inflation, `GPL-0019`/`GPL-1126`-equivalent lines) |
| Global placement STA | WNS / TNS / fmax | −1.48 ns / −67.30 ns / 406.41 MHz (`3_global_place.rpt`) |
| Detailed placement (legalized) | HPWL before → after legalization+optimization | 6,361.6 µm → 5,625.9 µm (−11.6%, `DPL-*` log + "Detailed Improvement Results") |
| Detailed placement | Final design area / utilization | 4,070 µm² / 69% (`detailed place report_design_area`) |
| Detailed placement STA | WNS / TNS / fmax / critical-path delay | −1.55 ns / −68.37 ns / 404.60 MHz / 2.7235 ns (`3_detailed_place.rpt`) |
| Detailed placement | Setup / hold violation count | 69 / 0 (`detailed place report_check_types`) |
| Detailed placement | Estimated total power | 6.01 mW (69.6% combinational / 30.4% sequential) |
| Clock-tree synthesis | Reached: H-tree generation for `clk` (35 sinks) started, then crashed — see "Environment limitation" above | not completed |
| Routing, final DEF/GDS | Not reached in this sandbox run | n/a — see §4 for the mechanism, verified independently |

This satisfies "wirelength/slack/utilization captured from raw output" for
synthesis through detailed placement — a real netlist was **placed** against
sky130 with real OpenROAD output at every stage. It does **not** fully
satisfy "placed and routed": routing and CTS did not complete in this
sandbox, for the emulation reason documented above, not a flow or design
defect. Anyone re-running this to extend the evidence should use a native
`linux/amd64` host (bare metal or a GitHub Actions `ubuntu-latest` runner,
which is x86_64 natively) rather than emulation on Apple Silicon.

## 4. DEF/GDS output: OpenROAD emits DEF; KLayout's `pya` merges the GDS

**OpenROAD's native output is DEF, not GDS.** `write_def` is the terminal
output of the P&R stages; ORFS's `6_final.def` target is what the flow
produces from OpenROAD directly **[SOURCE, `flow/Makefile`]**. GDS
generation is a **separate, explicit downstream step** — exactly the shape
the issue anticipated:

```makefile
$(GDS_MERGED_FILE): $(RESULTS_DIR)/6_final.def $(OBJECTS_DIR)/klayout.lyt \
                     $(GDSOAS_FILES) $(WRAPPED_GDSOAS) $(SEAL_GDSOAS) | check-klayout
	$(SCRIPTS_DIR)/klayout.sh -zz -rd design_name=$(DESIGN_NAME) \
	        -rd in_def=$(RESULTS_DIR)/6_final.def \
	        -rd in_files="$(GDSOAS_FILES) $(WRAPPED_GDSOAS)" \
	        -rd seal_file="$(SEAL_GDSOAS)" \
	        -rd out_file=$(GDS_MERGED_FILE) \
	        -rd tech_file=$(OBJECTS_DIR)/klayout.lyt \
	        -rd layer_map=$(GDS_LAYER_MAP) \
	        -r $(UTILS_DIR)/def2stream.py
```

This is the same `klayout -r script.py -rd key=value` batch-parameter
pattern the siliconcompiler survey documented for a different KLayout
integration — **two independent upstream projects converged on the same
KLayout batch-invocation idiom**, which is a point of confidence in that
pattern generally, not a reason to adopt it here.

**`def2stream.py`'s actual merge logic is plain `pya`** **[SOURCE, read the
full 151-line script in the container]**:

```python
tech = pya_mod.Technology()
tech.load(tech_file)
layout_options = tech.load_layout_options
if len(layer_map) > 0:
    layout_options.lefdef_config.map_file = layer_map

main_layout = pya_mod.Layout()
main_layout.read(in_def, layout_options)          # DEF, streamed in via the tech's LEF/DEF reader config
# ... clear orphan cells, keep VIA_* cells from LEF vias ...
for fil in in_files.split():
    main_layout.read(fil)                          # merge in each standard-cell/macro GDS view
# ... copy just the top cell into a fresh Layout, validate no missing/orphan cells ...
top_only_layout.write(out_file)                     # write the final GDS
```

This is a DEF-plus-GDS-views merge (LEF/DEF gives geometry and connectivity
for the block-level routing/placement; the actual standard-cell/macro
*shapes* still come from their own GDS views, read in and grafted onto the
matching LEF-derived cells) — not a from-scratch DEF-to-polygon renderer.
The function signature (`merge_gds(pya_mod, tech_file, layer_map, in_def,
design_name, in_files, seal_file, out_file, allow_empty="")`) is written to
take `pya_mod` as a parameter and is only wired to KLayout's `-rd` global
mechanism in an `if pya is not None: ... except NameError` guard at the
bottom of the file — i.e. **the merge logic itself is a plain, callable
Python function, not inherently coupled to being invoked via `klayout -r`.**

**Explicit answer to the issue's question: this repo's existing `pya`
wrapping is the right tool, and it does not need the desktop KLayout
application or the `-rd`/`-r` subprocess pattern at all.** Verified live in
this repo's own checkout, not asserted **[VERIFIED]**:

```python
import klayout.db as db
db.Technology()                                   # exists
opts = db.LoadLayoutOptions()
opts.lefdef_config.map_file = "..."                # exact attribute def2stream.py sets
```

Both classes and the exact attribute path `def2stream.py` uses
(`LoadLayoutOptions().lefdef_config.map_file`) exist in this repo's already
-installed `klayout==0.30.10` pip package — the same in-process
`klayout.db` API `klt drc` already uses (per
[docs/design/siliconcompiler-core-survey.md](siliconcompiler-core-survey.md)'s
§2 finding that `klt drc`'s in-process pip-`klayout` engine is a deliberately
better CI-weight fit than a desktop-app subprocess driver — this DEF→GDS
step reaches the identical conclusion independently). A future `klt`
DEF→GDS step should port `def2stream.py`'s merge logic directly against
`klayout.db`, in-process, the same way `klt drc` already avoids shelling out
to a standalone `klayout` binary — not adopt ORFS's `klayout.sh -zz -rd ...
-r def2stream.py` subprocess invocation as-is.

## 5. sky130 PDK plumbing ORFS needs, and what this repo already has

**[SOURCE, `flow/platforms/sky130hd/config.mk` and file listing]** — the
concrete files ORFS's sky130hd platform wires in:

| Asset | File | Used for |
| --- | --- | --- |
| Technology LEF | `lef/sky130_fd_sc_hd.tlef` | Layer stack, routing grid, via rules |
| Merged standard-cell LEF | `lef/sky130_fd_sc_hd_merged.lef` | Cell footprints/pins for placement & routing |
| Liberty (single-corner, `tt_025C_1v80`) | `lib/sky130_fd_sc_hd__tt_025C_1v80.lib` | Timing (STA), used by both Yosys and OpenROAD's OpenSTA |
| Standard-cell GDS views | `gds/*.gds` | Merged into the final GDS by `def2stream.py` (§4) |
| KLayout tech/layer-map files | `sky130hd.lyt`, layer-map config | DEF→GDS merge (§4), DRC/LVS runsets |
| DRC/LVS decks | `drc/sky130hd.lydrc`, `lvs/sky130hd.lylvs`, `cdl/sky130hd.cdl` | Signoff-style checks (not `klt drc`'s own rule-table engine — a separate, `.lydrc`-DSL-driven deck) |
| Fill/PDN/tapcell/fastroute Tcl | `fill.json`, `pdn.tcl`, `tapcell.tcl`, `fastroute.tcl` | Metal fill rules, power-grid template, tap/endcap insertion, routing-layer config |

**This repo already resolves the liberty file** — `klt pdk`'s
`list_cell_libraries`/nominal-corner selection (`src/klayout_tools/pdk.py`)
already parses `.lib` files and picks the nominal (typical-process,
room-temperature) view exactly like ORFS's `sky130_fd_sc_hd__tt_025C_1v80.lib`
default. **It does not yet resolve LEF files at all** — `_ASSET_LAYOUT`
(`pdk.py`) tracks `ngspice`/`xschem`/`klayout`/`magic`/`netgen` tool
directories plus a generic `libs_ref` directory, but has no `lef` key and no
helper analogous to `netgen_setup_file` for locating a tech LEF or merged
LEF inside an `open_pdks` install. This is a real, concrete gap for whoever
implements the P&R verb (a `klt pdk`-level LEF resolver, or a P&R-specific
helper reading directly from the already-resolved `libs_ref` tree) — noted
here as a finding for that future issue, not filed as a new issue now (per
this repo's friction-log discipline: the need is anticipated but not yet
demonstrated by a caller).

**On adopting ORFS wholesale:** the platform `config.mk` above is real,
useful reference data (which LEF/lib files, which fill/PDN/tapcell scripts,
which `DONT_USE_CELLS` sky130hd needs excluded) — but ORFS's `Makefile` is a
fixed 6-stage pipeline with its own env-var config surface, the same
"monolith, not a contract" shape the siliconcompiler survey rejected as
infrastructure to depend on. **Verdict: take the platform config as data
(which files, which cells to exclude, which layers), do not adopt the
Makefile as the P&R verb's implementation** — mirrors that survey's own
verdict for the identical reason.

## 6. Signal for #399 (the shared contracts issue)

Not a contract proposal — #399 owns that. Three findings from this survey
that should shape it:

1. **OpenROAD has a native structured-metrics channel, parallel to its text
   reports, that ORFS itself only wires up for one narrow step.** `openroad
   -metrics <file>` writes JSON, and OpenSTA/OpenROAD's Tcl API pairs every
   human-readable `report_*` proc with a `*_metric` proc that presumably
   feeds it (`report_tns` / `report_tns_metric`, `report_worst_slack` /
   `report_worst_slack_metric`, `report_fmax_metric`, `report_erc_metrics`,
   `report_clock_skew_metric` — all confirmed to exist side-by-side in
   `flow/scripts/report_metrics.tcl` **[SOURCE]**). ORFS's own Makefile only
   passes `-metrics` for one step (`generate_abstract.tcl`) — its main
   per-stage flow relies on the text `.rpt` files this survey scraped for
   §3's table, and its own `genMetrics.py` tool is explicit in its own
   docstring that it works by "looking for specific information in specific
   files using regular expressions" **[SOURCE]** — the same
   scrape-the-text-log fragility the SPICE corner-runner spike
   (`docs/design/spice-corner-runner-spike.md`) flagged for ngspice.
   **Recommendation for whoever builds the P&R verb: drive every stage with
   `-metrics <file>.json` from day one and read OpenROAD's own native JSON,
   rather than parsing `.rpt` text — a stronger position than either ORFS's
   own regex scraping or Yosys's `stat -json` (see #396) had to settle for,
   if the `*_metric` procs are confirmed to populate that file the way
   `generate_abstract`'s usage implies.** This was not independently proven
   end-to-end in this survey (the CTS crash in §3 stopped the worked example
   before a full `-metrics`-instrumented run could be attempted) — flagging
   it as the strongest candidate worth confirming first, not a settled fact.
2. **Wirelength, slack, and utilization are all available per-stage, not
   just at the end** — §3's table has real numbers at floorplan, global
   placement, and detailed placement, each independently reportable. A P&R
   contract that only reports final numbers would discard information
   OpenROAD already surfaces cheaply; whether intermediate-stage metrics
   belong in the response is a real design question for #399, not decided
   here.
3. **P&R is genuinely stochastic in a way synthesis is not** — global
   placement (Nesterov-based `gpl`) and detailed placement both make
   iterative, solver-driven decisions (§3's HPWL and utilization numbers
   moved across multiple internal passes before settling); this is the
   same "P&R seed variants" axis
   [docs/design/digital-fleet-unit-abstraction-decision.md](digital-fleet-unit-abstraction-decision.md)
   already names as part of what varies across a "candidate." A P&R
   contract should have an explicit place for a seed (echoed in the
   response, the same "reproducibility provenance" role
   `environment.remote` fields already play for `klt sim`) — that decision
   doc already anticipated this; this survey's real run is the concrete
   evidence backing it.

## Follow-ups

No new issue filed from this survey. Two concrete, scoped items are named
above for whoever builds the P&R verb (Phase 4) to pick up directly, not
filed now because neither is blocking Phase 1: (a) `klt pdk` needs a LEF
resolver (§5) — currently only `.lib` is resolved; (b) confirm
`-metrics <file>.json` actually captures the `*_metric` Tcl procs' values
end-to-end for a full stage (§6) — this survey's own CTS crash prevented
finishing that check. Both are natural first tasks for the Phase 4 P&R-verb
issue, referencing this survey, rather than new Phase 1 scope.
