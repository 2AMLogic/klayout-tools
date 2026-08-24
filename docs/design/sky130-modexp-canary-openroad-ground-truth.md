# `sky130-modexp` vs. plain OpenROAD: ground-truth comparison (Epic #700 acceptance criterion 2)

Closes the third leg of [issue #1329](https://github.com/2AMLogic/klayout-tools/issues/1329)
(Epic [#700](https://github.com/2AMLogic/klayout-tools/issues/700) Phase 4):
run the real `2AMLogic/sky130-modexp` fleet canary through `klt
place-and-route`, confirm `klt lvs`/`klt drc`-clean, and compare against a
plain OpenROAD-only run on the same design as ground truth. The first two
legs (real-netlist run, `klt lvs`/`klt drc`-clean) are established by
[`tests/corpus/sky130_modexp_canary/`](../../tests/corpus/sky130_modexp_canary/README.md)
(that directory's own README has the full methodology and reproduction
command); this document is the third leg — the OpenROAD-only ground-truth
comparison — plus an update to the design's overall signoff status.

## Update to `docs/design/sky130-modexp-canary-signoff-status.md`

That document (landed by PR #1332, 2026-08-22) recorded that the real
`sky130-modexp` canary's own committed evidence trail (as of its `main` at
the time) was **DRC-clean, LVS-mismatched** (1324 mismatches, PR #65,
2026-08-16) and had **no OpenROAD ground-truth comparison**. Both gaps are
closed by fresh evidence as of 2026-08-24, using this repo's *current* `klt`
(not `sky130-modexp`'s own pinned, older revision):

- **LVS is now clean.** Issue [#1336](https://github.com/2AMLogic/klayout-tools/issues/1336)
  (merged 2026-08-23 — *after* PR #65's attempt) added `klt lvs`'s
  `reference.form: "gate-level-verilog"` converter, closing exactly the gap
  PR #65's hand-rolled `build_reference_netlist.py` was working around. A
  fresh `klt synthesize` → `klt place-and-route` → `klt extract
  --abstract-cells --def-net-names` → `klt lvs` (`reference.form:
  "gate-level-verilog"`) run against the real `rtl/modexp.v` (see corpus
  README for the exact commands) reaches **`status: "match"`,
  `error_count: 0`** — down from 1324 mismatches to 4 `severity: "warning"`
  disclosures (ambiguous net pairings the comparer resolved on its own,
  never affecting `status` — see `docs/cli/lvs.md`'s `severity` field
  documentation). `klt drc --deck sky130` on the same fresh routed GDS
  remains **clean**, 0 violations, matching PR #65's own result.
- **The OpenROAD ground-truth comparison now exists** — this document,
  below.

`docs/design/sky130-modexp-canary-signoff-status.md` itself is left
unmodified rather than rewritten in place: it is `sky130-modexp` PR #65's
own frozen evidence record (a specific `klt`/OpenROAD/PDK pin, a specific
GDS content hash), and remains the accurate historical record of *that*
attempt. This document supersedes its "Bottom line" table for the purposes
of Epic #700's acceptance criteria, using fresh evidence gathered against
the current `klt` tree rather than editing history.

## Why "plain OpenROAD" means the ORFS canonical flow, not a second `klt` run

`klt place-and-route`'s only implemented engine already **is** OpenROAD
(Epic #391 Phase 4) — there is no separate `klt`-native placer/router to
diff against a standalone OpenROAD run, so re-running `klt place-and-route`
a second time would not be an independent ground truth. What *is*
independent: OpenROAD-flow-scripts (ORFS)'s own canonical `sky130hd`
platform flow — the Makefile-driven recipe `docs/design/openroad-invocation-survey.md`
(§4) already treats as the reference "how would this design route without
`klt`'s own choices" baseline, run through the **identical OpenROAD binary**
`klt place-and-route` itself shells out to (`openroad/orfs:latest`'s bundled
build, `26Q3-1278-g4421880472` — confirmed byte-identical version string on
both sides of this comparison), against the same PDK, on the same RTL. The
two flows diverge in orchestration, not in physics-solving engine: ORFS's
own Yosys synthesis config (dedicated `DONT_USE_CELLS`/latch/adder/
clock-gate cell maps, `abc_speed.script`) and its full canonical flow
(tapcell insertion, power-grid generation, metal fill, filler cells) versus
`klt place-and-route`'s documented v1 scope, which deliberately omits all
four (`docs/cli/place-and-route.md`, "What this flow does *not* produce").
That is exactly the axis this comparison is useful for: does `klt`'s
leaner orchestration leave real QoR on the table relative to OpenROAD's own
recommended, fuller recipe?

## Methodology

Both runs use:

- The same vendored RTL: `tests/corpus/sky130_modexp_canary/sources/modexp.v`
  (`2AMLogic/sky130-modexp`, commit `1b38ab4f1c5dc5b3396b50fcdf983f8768775e7b`).
- The same OpenROAD build: `26Q3-1278-g4421880472`, from the same
  `openroad/orfs:latest` Docker image.
- The same `sky130A` PDK install (`open_pdks` commit
  `c6d73a35f524070e85faff4a6a9eef49553ebc2b`, via `volare`).
- The same floorplan target: `CORE_UTILIZATION=35`, `CORE_ASPECT_RATIO=1`,
  `CORE_MARGIN=4` (ORFS) / `utilization_pct: 35, aspect_ratio: 1,
  core_margin_um: 4` (`klt`) — identical semantics, ORFS's
  `scripts/floorplan.tcl` and `klt place-and-route`'s own floorplan stage
  both call `initialize_floorplan -utilization ... -aspect_ratio ...
  -core_space ...` with these exact values.
- The same IO layer assignment (`IO_PLACER_H/V` = `met3`/`met2`, ORFS
  platform default; `io.layer_h/layer_v` = `met3`/`met2`, `klt`'s request —
  matches `sky130-modexp`'s own committed `flow/par-modexp.json`).
- The same site (`unithd`) and the same clock: port `clk`, 10 ns period
  (100 MHz) — `sky130-modexp`'s own committed constraint.

They differ in exactly the two ways described above: synthesis
configuration (Yosys pass sequence/cell-exclusion lists) and flow
completeness (tapcell/PDN/fill/filler).

### Reproducing the `klt` run

```bash
uv sync --extra dev && source .venv/bin/activate
PDK=sky130A python3 tests/corpus/sky130_modexp_canary/run_validation.py
```

Full detail: `tests/corpus/sky130_modexp_canary/README.md`.

### Reproducing the ORFS run

Not scripted as a checked-in, one-command reproduction (unlike the `klt`
side above) — ORFS's own Makefile flow expects a *stateful* design
directory inside its own image tree (`flow/designs/<platform>/<design>/`),
which does not fit a stateless `docker run --rm` invocation as cleanly as
`klt place-and-route`'s single JSON-request-in/JSON-response-out contract
does. The commands below are the exact, real sequence this document's
numbers came from (same convention `docs/design/openroad-invocation-survey.md`
already uses for its own "real, run inside the container" ORFS
measurements) — reproducible by any operator with `docker`, not merely
described:

```bash
# 1. Start a long-lived container (persistent filesystem across `docker exec`
#    calls -- ORFS's Makefile writes/reads relative to its own image tree).
sudo docker run -d --name modexp-orfs --user root \
  -v "$HOME":"$HOME" openroad/orfs:latest sleep infinity

# 2. Vendor the design into ORFS's own designs/ tree.
sudo docker exec modexp-orfs bash -c \
  'mkdir -p /OpenROAD-flow-scripts/flow/designs/src/modexp \
            /OpenROAD-flow-scripts/flow/designs/sky130hd/modexp'
sudo docker cp tests/corpus/sky130_modexp_canary/sources/modexp.v \
  modexp-orfs:/OpenROAD-flow-scripts/flow/designs/src/modexp/modexp.v

# config.mk (matches `klt place-and-route`'s own floorplan/IO targets --
# see "Methodology" above):
cat > /tmp/config.mk <<'EOF'
export DESIGN_NAME = modexp
export PLATFORM    = sky130hd
export VERILOG_FILES = $(DESIGN_HOME)/src/$(DESIGN_NICKNAME)/modexp.v
export SDC_FILE      = $(DESIGN_HOME)/$(PLATFORM)/$(DESIGN_NICKNAME)/constraint.sdc
export CORE_UTILIZATION = 35
export CORE_ASPECT_RATIO = 1
export CORE_MARGIN = 4
EOF

# constraint.sdc (10 ns / 100 MHz on port `clk`, matching
# sky130-modexp's own flow/par-modexp.json):
cat > /tmp/constraint.sdc <<'EOF'
current_design modexp
set clk_name  core_clock
set clk_port_name clk
set clk_period 10.0
set clk_io_pct 0.2
set clk_port [get_ports $clk_port_name]
create_clock -name $clk_name -period $clk_period $clk_port
set clk_io_name vclk_$clk_name
create_clock -name $clk_io_name -period $clk_period
set_clock_latency 0.290 [get_clocks $clk_name]
set_clock_latency 0.290 [get_clocks $clk_io_name]
set non_clock_inputs [all_inputs -no_clocks]
set_input_delay [expr $clk_period * $clk_io_pct] -clock $clk_io_name $non_clock_inputs
set_output_delay [expr $clk_period * $clk_io_pct] -clock $clk_io_name [all_outputs]
EOF

sudo docker cp /tmp/config.mk modexp-orfs:/OpenROAD-flow-scripts/flow/designs/sky130hd/modexp/config.mk
sudo docker cp /tmp/constraint.sdc modexp-orfs:/OpenROAD-flow-scripts/flow/designs/sky130hd/modexp/constraint.sdc

# 3. Run the full canonical flow (synth -> floorplan -> place -> cts ->
#    route -> finish, ORFS's own `.DEFAULT_GOAL := all`).
sudo docker exec modexp-orfs bash -c \
  'cd /OpenROAD-flow-scripts/flow && \
   export PDK_ROOT=/root/.volare PDK=sky130A && \
   make DESIGN_CONFIG=./designs/sky130hd/modexp/config.mk'
# (bind-mount the host's real ~/.volare at the same path instead, or copy
# it in, if the container has no PDK of its own)

# 4. Pull the routed GDS out and cross-check with klt drc independently.
sudo docker cp modexp-orfs:/OpenROAD-flow-scripts/flow/results/sky130hd/modexp/base/6_final.gds ./modexp_orfs.gds
klt drc modexp_orfs.gds --deck sky130 --format json
```

## Results

| Metric | `klt place-and-route` | Plain OpenROAD (ORFS canonical flow) |
| --- | --- | --- |
| OpenROAD build | `26Q3-1278-g4421880472` | `26Q3-1278-g4421880472` (identical binary) |
| Synthesis | `klt synthesize` (Yosys 0.68, no `DONT_USE`/latch/adder/clock-gate maps) | ORFS's own Yosys pass (Yosys 0.67, `sky130hd` `DONT_USE_CELLS` + latch/adder/clock-gate cell maps + `abc_speed.script`) |
| Post-synthesis instances | 717 | 795 |
| Post-route std-cell instances (real logic, excl. fill/tap) | 722 | 866 |
| Filler / tap cells inserted | none (`klt`'s documented v1 scope) | 2125 fill + 280 tap |
| Die area | 22,761.8 µm² | 24,941.9 µm² (157.93 × 157.93 µm) |
| Core area | 19,781.5 µm² | 21,958.6 µm² |
| Utilization | 37.03% (std cells only, no tap/fill) | 43% (final, incl. fill); 40% pre-fill (std cells + tapcells) |
| Wirelength (`route__wirelength` metric, real routed length — not HPWL) | 19,412 µm | 19,093 µm |
| Fmax | 180.29 MHz (period 5.55 ns) | 184.35 MHz (period 5.42 ns) |
| Worst setup slack (nominal corner) | 4.45 ns | 4.58 ns |
| Setup / hold violations | 0 / 0 | 0 / 0 |
| Antenna violations | 0 | 0 |
| Route DRC violations (engine's own) | 0 | 0 (empty `5_route_drc.rpt`) |
| `klt drc --deck sky130` (independent, klt-native) | clean, 0 violations | clean, 0 violations (cross-checked directly on the ORFS-produced GDS) |

**Reading this table.** `klt place-and-route`'s leaner flow (lighter
synthesis, no tapcell/PDN/fill overhead) reaches **timing within ~2%** of
ORFS's fuller canonical flow (180.29 MHz vs. 184.35 MHz Fmax; 4.45 ns vs.
4.58 ns worst setup slack) and **routed wirelength within ~1.7%** (19,412
µm vs. 19,093 µm), using the identical OpenROAD engine. Both reach zero
setup/hold/antenna/route-DRC violations. `klt`'s die/core area figures are
correspondingly smaller (no fill/tapcell area to account for), and its
post-synthesis instance count is lower (717 vs. 795) because it does not
apply ORFS's own `DONT_USE_CELLS`/latch/adder/clock-gate mapping tables —
that is a synthesis-configuration difference, not a placement/routing QoR
gap, and is consistent with `klt place-and-route`'s already-documented v1
scope (`docs/cli/place-and-route.md`, "What this flow does *not*
produce"), not a new finding.

**Nothing here indicates a `klt` defect.** The gap between the two flows is
exactly the gap `klt place-and-route`'s own documentation already
discloses (no tapcell/PDN/fill/`DONT_USE_CELLS`) — this comparison
quantifies that gap for the first time against the real fleet canary,
rather than discovering a new one. Landing tapcell/PDN/fill support in
`klt place-and-route` is Epic #700's own future scope (already implied by
"What this flow does *not* produce"), not a defect this issue's acceptance
criteria ask to be fixed.

## Bottom line against issue #1329's acceptance criteria

| # | Criterion | Status |
| --- | --- | --- |
| 1 | `klt place-and-route` run against the real `2AMLogic/sky130-modexp` netlist | **Met** — `tests/corpus/sky130_modexp_canary/run_validation.py`, against the real `rtl/modexp.v` (also independently already true per `sky130-modexp#7`/PR#17, 2026-08-14, predating this issue) |
| 2 | Result confirmed `klt lvs`-clean and `klt drc`-clean | **Met** — `klt drc`: `status: "clean"`, 0 violations; `klt lvs` (gate-level, `reference.form: "gate-level-verilog"`): `status: "match"`, `error_count: 0` |
| 3 | Compared against a plain OpenROAD run as ground truth, documented | **Met** — this document, above |

Epic #700's acceptance criterion 2's canary half is closed by this issue.

## Related

- Epic #700 (`klayout-tools#700`) — the parent epic this phase serves.
- [`tests/corpus/sky130_modexp_canary/`](../../tests/corpus/sky130_modexp_canary/README.md) —
  the checked-in, re-runnable `klt`-side half of this validation.
- `docs/design/sky130-modexp-canary-signoff-status.md` — the prior
  (2026-08-22) status report this document updates/supersedes for Epic
  #700's acceptance-criteria purposes, left unmodified as `sky130-modexp`
  PR #65's own frozen historical record.
- `docs/design/openroad-invocation-survey.md` — the original survey of how
  `klt place-and-route` orchestrates OpenROAD, and the precedent for
  treating ORFS's own canonical Makefile flow as the "plain OpenROAD"
  reference point.
- `docs/cli/lvs.md`, "Digital gate-level LVS: `reference.form =
  \"gate-level-verilog\"`" — the issue #1336 mechanism this validation's
  LVS leg exercises.
