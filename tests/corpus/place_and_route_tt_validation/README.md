# `klt place-and-route` Tiny Tapeout corpus validation — `tests/corpus/place_and_route_tt_validation/`

Epic [#700](https://github.com/2AMLogic/klayout-tools/issues/700) Phase 4
(issue [#1330](https://github.com/2AMLogic/klayout-tools/issues/1330)):
the epic's own acceptance criterion 2 requires validation "on ≥3 Tiny
Tapeout corpus designs (#520), with results compared against OpenROAD as
ground truth." This directory is that validation's checked-in evidence —
the exact commands that reproduce every number below, and the
last-measured snapshot (`results.json`).

## Why this reuses issue #990's corpus instead of a fresh fetch

[Epic #520](https://github.com/2AMLogic/klayout-tools/issues/520) (the
Tiny Tapeout corpus ingestion harness this issue's acceptance criteria
name) is still **open** and labeled `loom:operator-only` as of this
writing — no ready-made harness exists yet. Per this issue's own body,
that means: use an interim direct source rather than block on #520.

`tests/corpus/synth_e2e_validation/` (issue #990, Epic #704 Phase 4) faced
the identical situation one epic earlier and already solved it: it
selected real, public `tt06` shuttle designs directly from
`https://index.tinytapeout.com/tt06.json` (the same machine-readable index
Epic #520 itself names), verified their license, and vendored their RTL
under `sources/`. Rather than re-doing that sourcing work, this validation
**reuses 3 of that same corpus's 4 already-vetted designs** — the
provenance table below is a copy of that directory's own, restricted to
the 3 designs used here.

## Corpus (reused from `tests/corpus/synth_e2e_validation/sources/`)

| Design | `top` | Source | Commit | License |
| --- | --- | --- | --- | --- |
| `tt_um_LFSR_shivam` | `tt_um_LFSR_shivam` | [`beaprog/tt06-LFSR`](https://github.com/beaprog/tt06-LFSR) — 10-bit linear-feedback shift register, `tt06` shuttle | `6bffe5f606707446700dc5dc43db8fa4d6a74394` | Apache-2.0 |
| `tt_um_8bitALU` | `tt_um_8bitALU` | [`EdricOngKhaiJieh/8bitALU_verilog`](https://github.com/EdricOngKhaiJieh/8bitALU_verilog) — registered 8-bit ALU, `tt06` shuttle | `f9bcfd7d3cb6da2972d1678d9ec5e9402ae9eecb` | Apache-2.0 |
| `tt_um_CKPope_top` | `tt_um_CKPope_top` | [`CKPope/tt06-verilog-template`](https://github.com/CKPope/tt06-verilog-template) — X/Y position Mealy-FSM controller, `tt06` shuttle | `c93539faf09c864f1e4ed1ac31a8b16fcb5d7b73` | Apache-2.0 |

3 real, distinct Tiny Tapeout `tt06`-shuttle designs, all sequential
(register file / ALU / FSM), all sharing the standard TT wrapper interface
(`ui_in`/`uo_out`/`uio_in`/`uio_out`/`uio_oe`/`ena`/`clk`/`rst_n`). The
4th design in #990's own corpus (`tt_um_ALU`, purely combinational) is not
reused here — `klt place-and-route` requires a real clock port once
`target_stage` reaches `"place"` or later (no virtual/unconnected-clock
support, same constraint `tests/corpus/place_and_route/regenerate.sh`'s own
`mult8` fixture documents), and 3 real sequential designs already give
`klt place-and-route`'s clock-tree synthesis (`"cts"` stage) something
real to build, which a combinational design would not. See that
directory's own `README.md` for full provenance/licensing detail and the
`Apache-2.0 §4` attribution note (same obligation, unchanged, applies
here).

## What "compared against OpenROAD as ground truth" means here

`klt place-and-route`'s only implemented engine **is** OpenROAD (Epic #391
Phase 4; Epic #700's own Phase 0 explicitly reused that shipped command
rather than building a competing `klt par` — see #700's own "Re-scoped"
comment). There is no separate native placer/router yet to diff against a
standalone OpenROAD run — that is Epic #700's own future Phase 1/2 — so a
self-vs-self comparison of "klt's result" against "OpenROAD's result"
would be circular at this stage of the epic.

Epic #700's text names the actual oracle for this phase: **"LVS closes the
loop... a routed result is only accepted when `klt lvs` matches it against
the source netlist."** A full SPICE-level `klt lvs` round-trip needs a
Verilog→SPICE reference-netlist converter that does not exist yet — a
real, already-documented gap (`docs/cli/extract.md`'s "Gate-level Verilog
output": "a gate-level Verilog netlist ... is a deliberately deferred
follow-up"), not a discovery of this issue. So this validation substitutes
the equivalent, already-proven-in-this-repo method: issue #990's own
directed dual-DUT co-simulation diff, applied here to the **post-route**
netlist (`verilog_path`, issue #996 — the as-built netlist including every
CTS buffer, `repair_design`/`repair_timing` resize, and `repair_antennas`
diode OpenROAD's flow actually inserted) instead of the pre-route
synthesis netlist #990 checked. This is a *strictly stronger* connectivity/
functional check than #990's own — it validates the design **after**
OpenROAD has rewritten it, not before — and is the closest thing to
"netlist matches" achievable without the missing Verilog→SPICE converter.

Combined with:

- `klt drc --deck sky130` — an **independent**, klt-native geometric DRC
  check on the merged, routed GDS (the same `run_drc` function `klt drc`
  itself calls, distinct from OpenROAD's own internal
  `detailed_route -output_drc`/`check_antennas` reports below), and
- the place-and-route report's own timing/DRC metrics
  (`setup_violation_count`/`hold_violation_count`/
  `route_drc_violation_count`/`antenna_violation_count`),

this is the full, honestly-reported ground-truth comparison this issue's
acceptance criteria ask for, given the current shape of the pipeline.

## Reproducing it

```bash
uv sync --extra dev && source .venv/bin/activate   # klt on $PATH
# yosys, iverilog/vvp on $PATH; docker on $PATH (pulls openroad/orfs:latest
# if not already cached); a resolvable, volare-fetched sky130A PDK install
# (~/.volare, or set PDK_ROOT/PDK)
python3 tests/corpus/place_and_route_tt_validation/run_validation.py
```

Deliberate, reviewed, **never a CI step** — same convention as
`tests/corpus/place_and_route/regenerate.sh` and `tests/corpus/
synth_e2e_validation/run_validation.py`. A Yosys/OpenROAD/PDK version bump
can shift QoR numbers; re-running this script and re-committing
`results.json` is how that drift gets captured on purpose, not silently.

Per-design flow, all real tools, no stubs:

1. `klt synthesize` (real Yosys, on the host) — same as #990.
2. `klt place-and-route` (real OpenROAD, via the `openroad/orfs:latest`
   Docker image, floorplan → place → cts → route) — same recipe
   `tests/corpus/place_and_route/regenerate.sh` and `docs/cli/
   place-and-route.md`'s own "Installing OpenROAD" section document: the
   repo is `pip install`'d fresh inside the container (there is no `klt`
   in the upstream image), the scratch workdir and the host's `~/.volare`
   PDK install are both bind-mounted in.
3. `klt drc --deck sky130` (in-process, host-side, this repo's own
   `klayout` pip dependency) against the merged, routed GDS.
4. The directed dual-DUT GLS diff (Icarus Verilog, on the host, the real
   sky130 functional cell models) against the post-route `verilog_path`
   netlist, described above.

## Results (last measured — see `results.json` for the full snapshot)

Real OpenROAD `26Q3-1080-gab6fd26351` (`openroad/orfs:latest`),
`sky130_fd_sc_hd`/`tt_025C_1v80`, `sky130A` via `volare`
(`open_pdks c6d73a35f524070e85faff4a6a9eef49553ebc2b`), `seed: 1`,
`target_stage: "route"`.

| Design | Clock period (ns) | Stage reached | Utilization % | Wirelength (µm) | Worst slack (ns) | Setup/hold violations | Route DRC violations | Antenna violations | `klt drc` | Post-route equivalence |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `tt_um_LFSR_shivam` | 1.0 | route | 47.619 | 624 | 0.328 | 0/0 | 0 | 0 | clean | pass (200/200 cycles) |
| `tt_um_8bitALU` | 3.0 | route | 44.610 | 2161 | 2.328 | 0/0 | 0 | 0 | clean | pass (200/200 cycles) |
| `tt_um_CKPope_top` | 2.0 | route | 45.145 | 3351 | -0.109 | 5/0 | 0 | 0 | clean | pass (200/200 cycles) |

Every design reaches `stage_reached: "route"` (a full floorplan → place →
cts → route run, no partial-completion fallback), is **DRC-clean** both by
OpenROAD's own post-route checks (`route_drc_violation_count`/
`antenna_violation_count`, both `0` for every design) and by `klt drc`'s
own independent geometric deck (`status: "clean"`, `violation_count: 0`
for every design), and is **functionally equivalent to its own gold RTL**
after routing (all 200/200 diff cycles matched for every design — no
mismatch anywhere in this corpus).

`tt_um_CKPope_top`'s 2.0 ns clock constraint is genuinely tight relative to
its post-route delay: it reports **5 real setup violations** (worst slack
-0.109 ns, TNS -0.319 ns) — reported here exactly as measured, not loosened
to force a clean table. This is expected: the constraint was picked as a
~2x margin over Yosys/ABC's own pre-route critical-path *estimate*
(967.86 ps), which is not the same number OpenROAD's own post-route STA
computes once real placement/routing parasitics and CTS insertion delay
are in the picture — the gap between those two numbers is itself a small,
honest piece of P&R-oracle evidence, not a defect in the validation. It
does not affect the DRC-cleanliness or functional-equivalence findings
above, which are independent of the timing constraint chosen.

## Known limitation carried forward

Full SPICE-level `klt lvs` against the routed GDS is not exercised here —
see "What 'compared against OpenROAD as ground truth' means here" above.
This is the same pre-existing, already-documented Verilog→SPICE-reference
gap `docs/cli/extract.md` names, not a new discovery; closing it is a
distinct, already-deferred capability, out of scope for this issue.
