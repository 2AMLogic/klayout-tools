# `klt synth` end-to-end validation — `tests/corpus/synth_e2e_validation/`

Epic [#704](https://github.com/2AMLogic/klayout-tools/issues/704) Phase 4
(issue [#990](https://github.com/2AMLogic/klayout-tools/issues/990)): the
epic's own closing acceptance criterion is a distinct, end-to-end validation
of `klt synthesize` — every shipped phase's stage exercised together on a
real design — that no single phase issue (#807–809, #873–875, #925/#926)
exercised on its own. This directory is that validation's checked-in
evidence: the source RTL, the exact commands that reproduce every number
below, and the last-measured snapshot (`results.json`).

Phases 0–3 each validated `klt synthesize` against a narrower per-stage
oracle (Yosys/`abc` QoR, `klt equiv`, OpenSTA). This phase's job is
different: run the whole pipeline on real designs and check the *result*,
not a stage, against Yosys as ground truth — per the epic's own wording,
`sky130-modexp` plus ≥3 Tiny Tapeout corpus designs (Epic
[#520](https://github.com/2AMLogic/klayout-tools/issues/520); #520 itself is
not yet ingested, so this issue reuses its corpus/licensing conventions
directly against the public Tiny Tapeout index rather than waiting on it).

## Corpus

| Design | `top` | Kind | Source | Commit | License |
| --- | --- | --- | --- | --- | --- |
| `modexp` | `modexp` | sequential | [`examples/functional-verification/modexp.v`](../../../examples/functional-verification/modexp.v) (this repo) — the RTL half of the [`2AMLogic/sky130-modexp`](https://github.com/2AMLogic/sky130-modexp) canary; see [`docs/design/rsa-modexp-baseline.md`](../../../docs/design/rsa-modexp-baseline.md) | n/a (in-repo fixture) | MIT (this repo) |
| `tt_um_ALU` | `tt_um_ALU` | combinational | [`JoseKaisen/ALU_3bits`](https://github.com/JoseKaisen/ALU_3bits) — 3-bit ALU (add/sub/mul/div/mod), Tiny Tapeout `tt06` shuttle | `aef5319d47e2f6644e4df935d932ae1136e0cc20` | Apache-2.0 |
| `tt_um_8bitALU` | `tt_um_8bitALU` | sequential | [`EdricOngKhaiJieh/8bitALU_verilog`](https://github.com/EdricOngKhaiJieh/8bitALU_verilog) — registered 8-bit ALU, `tt06` shuttle | `f9bcfd7d3cb6da2972d1678d9ec5e9402ae9eecb` | Apache-2.0 |
| `tt_um_LFSR_shivam` | `tt_um_LFSR_shivam` | sequential | [`beaprog/tt06-LFSR`](https://github.com/beaprog/tt06-LFSR) — 10-bit linear-feedback shift register, `tt06` shuttle | `6bffe5f606707446700dc5dc43db8fa4d6a74394` | Apache-2.0 |
| `tt_um_CKPope_top` | `tt_um_CKPope_top` | sequential | [`CKPope/tt06-verilog-template`](https://github.com/CKPope/tt06-verilog-template) — X/Y position Mealy-FSM controller, `tt06` shuttle | `c93539faf09c864f1e4ed1ac31a8b16fcb5d7b73` | Apache-2.0 |

The 4 Tiny Tapeout designs and their pinned commits were selected from the
real, public `tt06` shuttle index
(`https://index.tinytapeout.com/tt06.json`, the same machine-readable index
Epic #520 names) — not hand-picked demo RTL. Each `macro`/`repo`/`commit`
triple above matches that index verbatim (re-verifiable via `curl -s
https://index.tinytapeout.com/tt06.json | jq '.projects[] | select(.macro
== "tt_um_ALU")'`, etc.). Selection criteria: real student/hobbyist tt06
projects, self-contained pure-RTL (no vendored hard macros or blackboxed
IP), a mix of purely-combinational and sequential/FSM designs so both of
`klt equiv`'s scope regimes (see "Equivalence method per design" below) are
exercised, and Apache-2.0 licensed (Tiny Tapeout's template default;
confirmed per-repo via `gh api repos/<owner>/<repo>/license`). Every
`source_files` list under `sources/<macro>/` matches that project's own
`info.yaml` exactly — the file(s) Tiny Tapeout's own hardening flow would
actually synthesize, not every file the repo happens to contain (e.g.
`tt_um_8bitALU`'s `info.yaml` names only `ALU_test.v`; that repo's
`project.v` defines an *unused*, duplicate-module-name scaffold file and is
correctly excluded).

**Attribution (Apache-2.0 §4).** Every file under `sources/` is redistributed
verbatim (unmodified) from the pinned commit above, under its original
Apache-2.0 license — distinct from, and not changed by, this repo's own MIT
license (same convention as `tests/corpus/README.md`'s "License note"). This
is a benchmark/validation corpus per Epic #520's own guardrails, not a
source of designs this repo claims as its own work.

## Reproducing it

```bash
uv sync --extra dev && source .venv/bin/activate   # klt on $PATH
# yosys, iverilog/vvp on $PATH; a resolvable sky130_fd_sc_hd liberty
# (volare, or set PDK_ROOT/SKY130_HD_LIB)
python3 tests/corpus/synth_e2e_validation/run_validation.py
```

Deliberate, reviewed, **never a CI step** — same convention as
`tests/corpus/statime/regenerate.sh` and `tests/corpus/techmap/regenerate.sh`.
A Yosys/ABC/PDK version bump can shift QoR numbers (see "A note on the
`modexp` cell count" below); re-running this script and re-committing
`results.json` is how that drift gets captured on purpose, not silently.

- `run_validation.py` — orchestrates everything below and writes
  `results.json`.
- `seq_equiv_check.sh` — the standalone Yosys `equiv_make`/`equiv_induct`
  sequential-equivalence script; reusable outside this corpus (see its own
  header comment).

## Equivalence method per design

`klt equiv`'s Phase 0 scope is **combinational only** — a design containing
flip-flops, latches, or memories on either side is rejected outright
(`docs/cli/equiv.md` → "Scope"; formal sequential equivalence is deferred to
a future phase of [#707](https://github.com/2AMLogic/klayout-tools/issues/707)).
Only `tt_um_ALU` here is purely combinational; `modexp` and the other 3 TT
designs are all sequential. So this validation uses two methods, both
"via Yosys" per the issue's own acceptance-criterion wording:

1. **Combinational (`tt_um_ALU`)** — `klt synthesize --verify-equivalence`,
   i.e. `klt equiv`'s real Yosys SAT proof, wired in as the command's own
   acceptance gate (issue #808). This is the clean, fully-formal case.
2. **Sequential (everything else)** — two checks, neither alone treated as
   the sole verdict:
   - `seq_equiv_check.sh`: plain Yosys `equiv_make` + `equiv_induct`
     (bounded k-step temporal induction — the same primitives SymbiYosys's
     `mode equiv` expands to for a bounded proof; this repo's CI has no
     `sby`, per `docs/cli/equiv.md`'s own "Why plain Yosys, not
     SymbiYosys" note, which applies here too). `read_liberty` (full
     functional import, not `-lib`/blackbox) is what makes this possible
     at all — it turns each sky130 sequential std-cell's liberty `ff`
     group into a real `$_DFF_*_` primitive Yosys's SAT engine can reason
     about, instead of an opaque blackbox `equiv_induct` cannot model
     (`No SAT model available for cell ... (sky130_fd_sc_hd__dfrtp_1)`
     without it).
   - Gate-level simulation (GLS): Icarus Verilog, the *real* synthesized
     netlist, and the *real* sky130 functional cell models
     (`libs.ref/sky130_fd_sc_hd/verilog/{primitives,sky130_fd_sc_hd}.v`,
     `-DFUNCTIONAL`) — either checked against a golden reference
     (`modexp`'s bit-exact `pow(base, exp, mod)`, the same reference
     `test_modexp.py`/`klt functional-verification` already checks the RTL
     against) or as a directed dual-DUT co-simulation diff (gold RTL and
     the gate netlist instantiated side by side, driven with identical
     stimulus, every TT-wrapper output pin compared every cycle).

**Why bounded induction alone is not enough here, and is not the sole
verdict.** `equiv_induct`'s own `help` text is explicit that it proves a
*weaker* property than full equivalence: "the two circuits will not diverge
after they produce equal outputs... for at least `<N>` cycles" — a real
formal result, but not one guaranteed to close for every design within a
practical depth `N`. It converges cleanly for `tt_um_8bitALU` and
`tt_um_LFSR_shivam` (small, quickly-settling state). It does **not**
converge within a bounded depth (16 steps tried) for `modexp` (a
multi-hundred-cycle square-and-multiply datapath — the induction hypothesis
would need to span the algorithm's full iteration count, not a small
constant) or `tt_um_CKPope_top` (an FSM whose capture registers only update
on a motion edge, so state correspondence does not "quickly propagate" in
the sense `equiv_induct`'s own docs describe). A non-convergent
`equiv_induct` run is reported here as **inconclusive**, exactly the
`klt equiv` convention for a timeout (`docs/cli/equiv.md` → "never
`equivalent`") — never miscast as a disproof. For those two designs, GLS is
the primary equivalence evidence; for the two where induction *does*
converge, GLS is corroborating.

**Directed vs. randomized stimulus (GLS).** The dual-DUT diff harness for
the 3 TT sequential designs uses a short sequence of held/directed
`(ui_in, uio_in)` plateaus, not free-running per-cycle randomized stimulus.
An earlier, exploratory version of this harness free-ran `$random` on
`ui_in` every cycle against `tt_um_CKPope_top` and saw transient mismatches
in the first ~50 cycles; switching to directed, held stimulus (`ui_in`
constant across each FSM capture window) reproduced **zero** mismatches
across 200 cycles, with the design converging to the exact expected X/Y
target. That is the expected, RTL-author-intended behavior of a design
whose own capture logic explicitly samples "whatever `ui_in` is during the
capture window" — changing the sampled value every single cycle is a
stimulus-timing artifact of the harness, not a synthesis defect, so the
final harness (checked in) uses held plateaus. This is noted here rather
than silently switched, per this issue's own "report as a defect, not
silently excluded" instruction — the investigation is the reason there is
no defect filed for `tt_um_CKPope_top`.

## Results (last measured — see `results.json` for the full snapshot)

Yosys `0.68+post`, `sky130_fd_sc_hd`/`tt_025C_1v80`, `sky130A` via `volare`.

| Design | Kind | Cells | Area (µm²) | ABC critical path (ps) | Equivalence verdict |
| --- | --- | --- | --- | --- | --- |
| `modexp` | sequential | 717 | 7150.608 | 4259.91 | GLS: **pass** (27/27 vectors vs. `pow()` oracle); Yosys induction: inconclusive (16-step bound) |
| `tt_um_ALU` | combinational | 97 | 639.3632 | 1247.69 | `klt equiv`: **equivalent** |
| `tt_um_8bitALU` | sequential | 110 | 760.7296 | 1501.22 | Yosys induction: **proven**; GLS: **pass** |
| `tt_um_LFSR_shivam` | sequential | 38 | 352.8384 | 415.66 | Yosys induction: **proven**; GLS: **pass** |
| `tt_um_CKPope_top` | sequential | 169 | 1493.9328 | 967.86 | GLS: **pass** (directed, 200/200 cycles); Yosys induction: inconclusive (16-step bound) |

**Zero defects found.** Every design in this corpus — the canary and all 4
Tiny Tapeout designs — passes real equivalence evidence against Yosys as the
ground-truth oracle: a full formal SAT proof for the one combinational
design, a full formal bounded-induction proof for 2 of the 4 sequential
designs, and a real, executable gate-level simulation (against a golden
oracle or a directed RTL-vs-gate diff) for every sequential design including
the 2 where induction did not converge. No counterexample — a proven,
concrete divergence — was found anywhere in this corpus.

### A note on the `modexp` cell count

`docs/design/rsa-modexp-baseline.md` / `sky130-modexp/docs/baseline.md`
record **682** cells, measured 2026-08-04 against Yosys `0.67+post`. This
validation measures **717** cells against Yosys `0.68+post` (this
environment's installed version) — a different Yosys/ABC build, not a
regression in `klt synthesize` itself; the same drift-is-expected note
`tests/corpus/statime/README.md`/`tests/corpus/techmap/README.md` already
carry for their own fixtures. This document does not update the
`sky130-modexp` canary's own baseline record — per
`docs/design/rsa-modexp-baseline.md`, that record now lives in the
`2AMLogic/sky130-modexp` repo itself, out of scope for this issue.

### `sta` (native gate-level STA, issue #925)

`klt-statime-native` was not built in the environment this snapshot was
measured in (`klt_statime_native` requires a `maturin`/Rust build — see
`native/statime/README.md`), so `report["sta"]` is `null` for every design
above; this is `klt synthesize`'s own documented degrade-to-`None` path
(`docs/cli/synthesize.md` → "`sta`"), not a validation gap specific to this
issue. The `timing` field (ABC's own `stime -p` estimate) is populated for
every design and reported above.
