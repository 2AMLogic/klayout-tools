# `klt place-and-route` real fleet-canary validation — `tests/corpus/sky130_modexp_canary/`

Epic [#700](https://github.com/2AMLogic/klayout-tools/issues/700) Phase 4
(issue [#1329](https://github.com/2AMLogic/klayout-tools/issues/1329)):
closing acceptance criterion 2's canary half — validate `klt
place-and-route` on the *real* `2AMLogic/sky130-modexp` fleet canary (named
in the epic's own text as "the cleanest digital target"), not the local
test-proxy fixture at `tests/corpus/place_and_route/`. This directory is
that validation's checked-in evidence — the exact commands that reproduce
every number below, and the last-measured snapshot (`results.json`).

## Why this is a distinct corpus directory from `place_and_route_tt_validation`

[`tests/corpus/place_and_route_tt_validation/`](../place_and_route_tt_validation/)
(issue #1330, sibling Phase-4 issue) validates 3 *Tiny Tapeout* corpus
designs and, because at the time nothing in `klt` could turn a `verilog_path`
gate-level netlist into an LVS-comparable reference, substitutes a directed
GLS co-simulation diff for a real `klt lvs` round-trip. Issue
[#1336](https://github.com/2AMLogic/klayout-tools/issues/1336) (merged
2026-08-23, *after* that validation) closed that gap:
`reference.form: "gate-level-verilog"` now converts a `klt place-and-route`
as-built netlist directly, so this directory runs the actual oracle Epic
#700's own text names — *"a routed result is only accepted when `klt lvs`
matches it against the source netlist"* — against the real fleet canary,
rather than the GLS substitute.

## Provenance

`sources/modexp.v` is vendored (not fetched at run time — see "Why vendor
instead of fetch" below) from the real `2AMLogic/sky130-modexp` repo:

| Field | Value |
| --- | --- |
| Repo | [`2AMLogic/sky130-modexp`](https://github.com/2AMLogic/sky130-modexp) |
| Path | `rtl/modexp.v` |
| Commit | [`1b38ab4f1c5dc5b3396b50fcdf983f8768775e7b`](https://github.com/2AMLogic/sky130-modexp/commit/1b38ab4f1c5dc5b3396b50fcdf983f8768775e7b) (2026-08-24) |
| License | Apache-2.0 |

An RSA-style modular-exponentiation accelerator (square-and-multiply over a
shared interleaved "Blakley" modular multiplier), `WIDTH = 16` (the file's
own default parameter, unchanged here) — a real, order-of-magnitude-larger
digital block than `gcd`/`mult8` (this repo's own local proxy fixtures),
sequential (needs a real clock-tree-synthesis stage), and already
independently synthesized, placed, and routed at least once in its own
repo's evidence trail (`sky130-modexp#7`/PR#17, 2026-08-14) — this
directory reproduces that same design through *this* repo's current `klt`,
rather than reusing that frozen artifact, so the result reflects `klt`
capability landed since (issue #1336 chief among it).

**Why vendor instead of fetch.** The guidance for this issue prefers "a
pinned commit reference + fetch step" over copying a *generated* artifact
(a routed GDS/DEF/SPICE netlist, which can run to multiple MB) wholesale
into this repo. `sources/modexp.v` is the original ~6 KB RTL source, not a
generated artifact — vendoring it directly, with the commit/license
attribution above, matches this repo's own established convention for
small external RTL sources (see
[`tests/corpus/synth_e2e_validation/sources/`](../synth_e2e_validation/)'s
identical pattern for its Tiny Tapeout sources) rather than adding a
network-fetch dependency to a "deliberate, reviewed, never a CI step"
script. Every *generated* artifact this script produces (synthesized
netlist, routed GDS/DEF, extracted SPICE) is never committed — only this
directory's `results.json` snapshot is, mirroring
`place_and_route_tt_validation/results.json`.

**Attribution (Apache-2.0 §4).** `sources/modexp.v` is redistributed
unmodified with its own file header preserved plus a provenance comment
naming this exact commit — satisfying Apache-2.0's attribution requirement,
distinct from, and not changed by, this repo's own MIT license.

## What "compared against OpenROAD as ground truth" means here

Unlike `place_and_route_tt_validation`'s framing (at the time, no separate
native placer/router existed to diff against, so a self-vs-self compare
would be circular), issue #1329 asks specifically for this design to be
run through a **plain OpenROAD-only flow** (no `klt` orchestration) as a
ground-truth comparator. `docs/design/sky130-modexp-canary-openroad-ground-truth.md`
is that comparison — a real OpenROAD-flow-scripts (ORFS) canonical
`sky130hd` platform flow (Yosys synthesis with ORFS's own cell-mapping
config, floorplan, tapcell, PDN, place, CTS, global+detailed route, fill),
run against the same vendored RTL, same OpenROAD build, same PDK, same
utilization/aspect-ratio/core-margin/IO-layer/clock-period targets as
`klt place-and-route`'s own request — see that doc for the full
methodology and results table. This directory's own `run_validation.py`
covers the `klt`-side half (synthesize → place-and-route → drc → extract →
lvs); the ORFS-side half is a separate, manually-reproduced measurement
(reasons in that doc's own "Reproducing the ORFS run" section — it needs a
privileged `docker run` against a stateful container filesystem, which does
not fit this script's stateless, `docker run --rm` shape cleanly).

## Reproducing it

```bash
uv sync --extra dev && source .venv/bin/activate   # klt on $PATH
# yosys on $PATH; docker on $PATH (pulls openroad/orfs:latest if not
# already cached; DOCKER_RUNNER="sudo docker" on a host where the invoking
# user is not in the `docker` group); a resolvable, volare-fetched sky130A
# PDK install (~/.volare, or set PDK_ROOT/PDK)
python3 tests/corpus/sky130_modexp_canary/run_validation.py
```

Deliberate, reviewed, **never a CI step** — same convention as
`tests/corpus/place_and_route_tt_validation/run_validation.py` and
`tests/corpus/place_and_route/regenerate.sh`. A Yosys/OpenROAD/PDK version
bump can shift QoR numbers; re-running this script and re-committing
`results.json` is how that drift gets captured on purpose, not silently.

Per-design flow, all real tools, no stubs:

1. `klt synthesize` (real Yosys, on the host).
2. `klt place-and-route` (real OpenROAD, via the `openroad/orfs:latest`
   Docker image) — floorplan → place → cts → route, the same request shape
   (`utilization_pct: 35`, `aspect_ratio: 1`, `core_margin_um: 4`,
   `site: unithd`, IO on `met3`/`met2`, `clock_port: clk`,
   `clock_period_ns: 10.0`, `seed: 42`) as `sky130-modexp`'s own committed
   `flow/par-modexp.json`.
3. `klt drc --deck sky130` (in-process, host-side) against the merged,
   routed GDS.
4. `klt extract --abstract-cells 'sky130_fd_sc_hd__*' --def-net-names`
   (issues #620/#951) against the routed GDS — the layout side of a
   gate-level LVS compare.
5. `klt lvs`, `reference.form: "gate-level-verilog"` (issue #1336),
   against the same run's own `verilog_path` as-built netlist — the
   reference side.

## Results (last measured — see `results.json` for the full snapshot)

Real OpenROAD `26Q3-1278-g4421880472` (`openroad/orfs:latest`),
`sky130_fd_sc_hd`/`tt_025C_1v80`, `sky130A` via `volare`
(`open_pdks c6d73a35f524070e85faff4a6a9eef49553ebc2b`), `seed: 42`,
`target_stage: "route"`.

| Metric | Value |
| --- | --- |
| Stage reached | `route` |
| Die area | 22,761.8 µm² |
| Core area | 19,781.5 µm² |
| Utilization | 37.03% |
| Wirelength (detailed route) | 19,412 µm |
| Fmax | 180.29 MHz |
| Worst setup slack (nominal corner) | 4.45 ns |
| Setup / hold violations | 0 / 0 |
| Antenna violations | 0 |
| Route DRC violations (OpenROAD's own) | 0 |
| `klt drc --deck sky130` | **clean**, 0 violations |
| `klt lvs` (gate-level, vs. `verilog_path`) | **`status: "match"`**, `error_count: 0` (4 `severity: "warning"` ambiguous-net-pairing disclosures, `mismatch_count: 4` — never affects `status`) |

**Both AC2 halves are met on this fresh run.** `2026-08-16`'s prior
attempt at this same real design (`sky130-modexp#55`/PR#65, recorded in
`docs/design/sky130-modexp-canary-signoff-status.md`) reached
`status: "mismatch"` (1324 mismatches) building its LVS reference by hand
(`build_reference_netlist.py`, predating issue #1336) — this run instead
uses `klt lvs`'s now-native `reference.form: "gate-level-verilog"`
converter directly and reaches `status: "match"` with zero `error`-severity
findings. See `docs/design/sky130-modexp-canary-openroad-ground-truth.md`
for the full comparison against a plain-OpenROAD (ORFS) ground-truth run
and the historical LVS-methodology discussion.

## Known limitation carried forward

`klt place-and-route`'s documented v1 scope (unchanged by this issue) still
omits tapcell insertion, power-grid (PDN) generation, metal fill, and
filler-cell insertion — see `docs/design/sky130-modexp-canary-openroad-ground-truth.md`
for how that shows up against a canonical ORFS flow that does include all
four. Power-net (`VPWR`/`VGND`/well-tie) correspondence is out of scope for
the gate-level LVS compare here (`docs/cli/lvs.md`'s "Digital gate-level
LVS" — `verilog_path` is written without `-include_pwr_gnd`), same as
`tests/corpus/place_and_route_tt_validation`'s own carried-forward
limitation before issue #1336.
