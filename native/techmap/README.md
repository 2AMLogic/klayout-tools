# `klt-techmap-native` — Epic #704 Phase 2b (issue #874)

Implements
[`docs/design/synth-techmap-stage-contract.md`](../../docs/design/synth-techmap-stage-contract.md)
(issue #873): Liberty-driven technology mapping — a `klt.synth.generic-netlist/1`
(engine-agnostic, 10-primitive gate netlist) + a resolved standard-cell
Liberty in, a mapped gate-level netlist + area/delay report out. Benchmarked
against Yosys/`abc`'s own technology mapping on the same 3-design proxy
corpus (`gcd`, `mult8`, `modexp`) prior Epic #704 phases already use — see
"Results" below.

## What this is

Not (yet) folded into `klt synthesize`/a future `klt synth` — this crate is
still a standalone binary (`klt-techmap <request.json>`) plus library,
following `native/statime/`'s own precedent (issue #809) of shipping the
engine and its own CLI first, production integration later. Issue #875 did
wire its own correctness gate in from the Python side though: `klt techmap`
(`src/klayout_tools/cli/techmap_cmd.py`,
`klayout_tools.techmap.run_techmap`) invokes this same binary as a
subprocess and, with `--verify-equivalence`, proves the mapped netlist
equivalent to its own pre-mapping generic netlist via `klt equiv` — see
`docs/design/synth-techmap-stage-contract.md` section 9 for the full
picture, and this crate's own `verilog::write_generic` (below) for the
generic-netlist-as-Verilog emitter that gate needs.

**Pipeline** (`src/`):

| Module | What it does |
| --- | --- |
| `liberty.rs` | Parses `.lib`: `area`, per-pin `direction`/`function`, `timing()` NLDM tables, and each cell's optional `ff (...)` group (`clocked_on`/`next_state`/`clear`/`preset`). A fork, not a shared dependency, of `native/statime/src/liberty.rs` — same grammar, different (non-overlapping) semantic extraction; see that module's own doc comment for why forking beat a premature shared crate. |
| `boolfn.rs` | A small Boolean-expression parser/evaluator over `function` attribute text (`!`/`&`/`|`/`^`/parens), used **by truth table**, not by string matching against a hand-picked set of literal function strings. |
| `celllib.rs` | Classifies every liberty cell against the 10-primitive vocabulary by truth table (combinational) / `ff`-group shape (sequential), builds per-type candidate lists (`area_um2` + a representative `delay_ns`, from `nldm.rs`), and picks the best one per a tunable area/delay score. |
| `nldm.rs` | Bilinear NLDM interpolation, forked from `native/statime/src/nldm.rs` (same table shape, same extrapolation convention) — used for a single representative-operating-point delay estimate, not a path-based STA graph. |
| `genlist.rs` | Parses/validates `klt.synth.generic-netlist/1` (this stage's own input, `docs/schemas/synth-generic-netlist.schema.json`). |
| `map.rs` | The mapping algorithm itself: picks a candidate per generic cell, realises `$dff`'s optional `RST`/`EN` pins (including via an inserted `$mux2` feedback chain when no single liberty cell combines async-reset and clock-enable — see "Cell chains" below), resolves `$const0`/`$const1` tie nets to a shared tie-cell instance. |
| `verilog.rs` | Two emitters: `write` emits the mapped netlist in **exactly** the dialect `native/statime/src/netlist.rs` already parses (contract section 4/5) — verified against the real parser, not just a shape check (see "Results" below). `write_generic` (issue #875) emits the *pre-mapping* generic netlist as self-contained behavioral Verilog (`assign`/`always @(posedge ...)`, no liberty needed) — `klt equiv`'s own `gold` side for the equivalence gate against `write`'s `gate` side. |
| `request.rs` / `main.rs` | `klt.synth.techmap.request/1` → `klt.synth.techmap.response/1`, per contract section 6. |

## Cell selection: the area/delay score

`celllib::best_comb`/`best_dff` score each candidate as
`area_um2 + delay_weight * delay_ns`. `delay_weight == 0.0` (no
`constraints.clock_period_ns` in the request) reduces this to pure area
minimisation; a positive weight (`main.rs`'s `TECHMAP_DELAY_WEIGHT = 50.0`,
used whenever a clock target is given) biases toward lower intrinsic delay
at an area cost. This is a **deliberately simple, stated v1 heuristic** —
not a critical-path/slack-aware sizing loop (that is Epic #704 Phase 3's
"timing-driven optimization" scope) — but it is the contract's own
"tunable cell-selection scoring function... this stage's own unlock"
(section 7). "Results" below reports both regimes measured, including a
case where it does *not* help (honestly, not hidden).

## Cell chains: `$dff` with `RST`/`EN`

`sky130_fd_sc_hd` has no single cell combining async-clear *and*
clock-enable (measured: `dfxtp`/`dfrtp` are plain/reset-only; `edfxtp` is
enable-only; no `edfrtp`). Rather than fail every design using both (the
*majority* case in this repo's own corpus — `gcd`'s 50 flops are 48
reset+enable, 2 reset-only; `modexp`'s 127 are 110+16 reset+enable, 1
reset-only), a generic `$dff`'s `EN` input is **always** realised the same
structural way, with or without `RST`: a `$mux2(select=EN, A=<Q
feedback>, B=D)` feeding a plain (or reset) flip-flop's `D` pin — the
classic "hold via feedback mux" construction, and exactly the "cell (or
cell chain)" language issue #874 itself uses. An active-low reset/enable
pin on the chosen liberty cell (`dfrtp`'s `RESET_B`) is bridged with an
inserted `$not`-realising inverter, so the generic vocabulary's active-high
convention never leaks a library-specific polarity into the caller.

## Constant nets (`docs/design/synth-techmap-stage-contract.md` section 8)

The contract leaves "how a tie-hi/tie-lo net is represented" as an open
question for this issue to propose. This crate's answer: the interim
on-ramp converter (`tests/corpus/techmap/yosys_to_generic.py`) emits the
reserved pseudo-net names `"$const0"`/`"$const1"`; `map.rs` resolves each
one it finds to a single shared instance of the cheapest liberty cell whose
output `function` text is the literal `"0"`/`"1"` (`sky130_fd_sc_hd__conb_1`'s
`LO`/`HI` pins) — not a directly-wired power rail, which is not a valid
logic net for a standard-cell input pin.

## A measured correctness pitfall: power-management cells matching by accident

`sky130_fd_sc_hd__lpflow_inputiso1p_1` (a power-domain input-isolation
cell) has `function : "(A) | (SLEEP)"` over its own two declared input
pins — a functionally *exact* 2-input OR realisation the truth-table
classifier alone cannot distinguish from a real `sky130_fd_sc_hd__or2`
cell, and it was in fact selected as this stage's cheapest `$or2` candidate
in an earlier pass over the real library (measured, not theoretical).
Every `lpflow_*` cell in `sky130_fd_sc_hd` is a power-management/isolation/
level-shifter primitive, never appropriate general logic. `celllib.rs`
excludes the whole family by name (`CELL_NAME_DENYLIST_SUBSTRINGS`) — this
stage's own minimal equivalent of `abc -dont_use`
(`docs/design/synthesize-qor-improvements-survey.md` documents that flag's
role in the existing flow) — since our minimal liberty parser doesn't
extract a `dont_use`/`is_isolation_cell`-style attribute to filter on
directly.

## Results — benchmarked against Yosys/`abc`

Measured against the same 3-design corpus prior Epic #704 phases use
(`gcd`, `mult8`, `modexp` — `tests/corpus/statime/`'s own designs, reused
here rather than building a fourth corpus harness; see
`tests/corpus/techmap/README.md`), `sky130_fd_sc_hd`/`tt_025C_1v80`, via
`tests/corpus/techmap/compare.py`. For each design: `klt synthesize`
(Yosys+`abc`, this repo's shipped default flow) is the oracle; this
crate's own `klt-techmap` maps the same design's interim-converter generic
netlist; **both** mapped netlists are then timed by the same tool
(`klt-statime critical-path`, issue #809's accepted native STA engine,
`--input-transition-ns 0.05 --output-load-pf 0.03`, no SDC) so the delay
comparison isn't confounded by two different STA engines/settings.

### Area-only cell selection (`constraints.clock_period_ns: null`)

| design | cells (oracle/techmap) | Δ% | area µm² (oracle/techmap) | Δ% | delay ns (oracle/techmap) | Δ% |
| --- | --- | --- | --- | --- | --- | --- |
| `gcd` | 366 / 551 | +50.5% | 3263.1 / 4678.2 | +43.4% | 3.0676 / 3.8529 | +25.6% |
| `mult8` | 249 / 347 | +39.4% | 2037.0 / 2448.6 | +20.2% | 3.5751 / 4.5377 | +26.9% |
| `modexp` | 710 / 1326 | +86.8% | 7126.8 / 11051.8 | +55.1% | 6.0345 / 6.3007 | +4.4% |

Reproduce with `uv run python3 tests/corpus/techmap/compare.py`.

### Delay-driven cell selection (`constraints.clock_period_ns: 5.0`)

| design | area µm² (oracle/techmap) | Δ% | delay ns (oracle/techmap) | Δ% |
| --- | --- | --- | --- | --- |
| `gcd` | 3263.1 / 5241.3 | +60.6% | 3.0676 / 3.7473 | +22.2% (vs. +25.6% area-only — improved) |
| `mult8` | 2037.0 / 3039.2 | +49.2% | 3.5751 / 4.6455 | +29.9% (vs. +26.9% area-only — **worse**) |
| `modexp` | 7126.8 / 12619.6 | +77.1% | 6.0345 / 5.8797 | **-2.6%** (vs. +4.4% area-only — improved, now *faster* than the oracle) |

Reproduce with `uv run python3 tests/corpus/techmap/compare.py --delay-driven`.

### Reading these numbers honestly

**This stage's v1 mapper uses meaningfully more cells/area than Yosys/`abc`
on every design, and the gap grows with design size (+50%/+39%/+87% gate
count).** The largest single reason: this stage's generic vocabulary is
deliberately the ten technology-independent primitives
(`$not`/`$buf`/`$and2`/.../`$mux2`/`$dff`) the contract defines — a
*covering* mapper choosing among 2-input primitives only, never `abc`'s
much larger compound-gate library (AOI/OAI/MAJ3/complex `a21o`-style gates
this repo's own oracle netlists use extensively — `grep`-confirmed against
the checked-in `tests/corpus/statime/*_netlist.v` fixtures). A compound
gate collapses several logic levels' worth of 2-input primitives into one
cell; this stage's mapper cannot do that yet — matching multi-level
patterns onto compound cells is exactly the kind of QoR work a later phase
(or a v2 vocabulary extension) would add, not something this issue's scope
covers.

**The delay-weighted score is a real, working knob, but not a
uniformly-winning one** — `mult8`'s delay got *worse* under delay-driven
selection (+29.9% vs +26.9%), while `modexp`'s improved enough to beat the
oracle outright (-2.6%). This is the expected signature of a
representative-operating-point heuristic (`celllib.rs`'s own docs) rather
than a true critical-path-aware sizing loop: locally "faster" per-cell
choices at a fixed nominal load/transition don't always shorten the real
global critical path, especially through the `$mux2`-feedback chains
`RST`+`EN` designs use heavily. Closing this gap with real slack-driven
sizing is explicitly Epic #704 Phase 3's job, not this issue's.

**The mapped-netlist dialect claim is verified, not assumed**: every
`*_mapped.v` this crate produced above was fed to
`native/statime`'s real `klt-statime critical-path` parser with **zero
code change**, successfully, for all 3 designs (contract section 5's own
acceptance check, run for real rather than argued from the spec text).

## Running it yourself

```bash
cd native/techmap
cargo test              # 27 unit tests, no PDK/Yosys required
cargo build --release
uv run python3 ../../tests/corpus/techmap/compare.py               # area-only
uv run python3 ../../tests/corpus/techmap/compare.py --delay-driven
```

Regenerating the checked-in `tests/corpus/techmap/*_generic.json` fixtures
needs real Yosys (`tests/corpus/techmap/regenerate.sh`); the benchmark
above additionally needs a real, volare-fetched `sky130A` PDK install and
the `native/statime` crate's own `klt-statime` binary (built automatically
by `compare.py` if missing).

## Known v1 gaps (not resolved here)

- **Reset-to-1 (`preset`) and negedge-clock flops are unsupported** —
  `celllib::classify_sequential` returns `None` for both; a design using
  them fails to map with a clear `Unrealizable` error, per contract
  section 6's exit-code-1 semantics, not silently mishandled.
- **No compound-gate covering** — see "Reading these numbers honestly"
  above; this is the largest measured QoR gap against Yosys/`abc`.
- **`timing` is always `null`** in this stage's own response — per
  contract section 8, wiring `native/statime` in as a real path-based
  delay estimator is a follow-on issue's scope, not this one's. This
  crate's own cell-selection score uses a per-cell representative delay
  (`nldm.rs`) for exactly that reason — it is a scoring input, not a
  reported timing result.
- **`inout` ports, multi-clock-domain designs, latches** are out of scope,
  matching the contract's own `v1` scope statement (section 2.2/2.3).

## Not in scope (per the issue)

Wiring this crate into `klt synthesize`, or a unified future `klt synth`
command spanning RTL elaboration through place-and-route — issue #875 wired
this crate's own `klt equiv` acceptance gate in (see "What this is" above)
but deliberately left `klt-techmap` a standalone binary, not folded into
`klt synthesize` itself. A native elaboration/logic-optimization stage that
would emit `klt.synth.generic-netlist/1` directly (rather than via the
interim Yosys-derived on-ramp this issue's own corpus harness uses) — Epic
#704 Phase 1/3, already-shipped/future work this issue doesn't touch. A
`pyo3`/`maturin` production-integration surface — deferred until a "Go"
verdict on wiring this stage in for real, following
`native/statime/README.md`'s own precedent.
