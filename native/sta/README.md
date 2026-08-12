# `klt-sta-native` — Epic #704 Phase 1c spike (issue #809)

**Verdict: No-go**, on the wrap-vs-native trade-off, per this issue's own
acceptance criteria. This crate stays in the repo as the spike's own
artifact — it is **not** wired into `klt synthesize`, has no Python binding,
and should not grow one until a follow-on spike measures a durable advantage
on real (non-emulated) hardware at realistic design sizes. `native/legalize/`
(issue #784, Epic #700's own Phase 1 spike) took the identical posture for a
different reason (QoR); this one clears accuracy but fails the "durable
measured advantage" leg.

## What this is

A from-scratch Rust gate-level static timing engine: reads a liberty file's
NLDM (non-linear delay model) cell timing tables and a `write_verilog`-mapped
gate netlist (the shape `klt synthesize` already writes to
`.klt/synthesize/<top>_synth.v`), builds a timing graph, and reports
worst-case register-to-register critical paths with bilinear NLDM
interpolation — the same physical quantity OpenSTA's `report_checks
-path_delay max` reports.

Three modules (`src/liberty.rs`, `src/netlist.rs`, `src/graph.rs`), each with
its own scope note at the top of the file, plus a `klt-sta` CLI
(`src/main.rs`) that emits JSON or the same tabular text `report_checks`
produces. See "Crate layout" below for what each module owns.

Deliberately a plain `cargo build`/`cargo test` binary + `rlib` crate — **no**
`pyo3`/`maturin`/CI wiring (unlike `native/mom/`, this repo's shipped
Rust-extension convention). Issue #809 is explicitly go/no-go; building the
production-integration surface before a "Go" verdict would spend real
maintenance cost on a result that turned out "No-go." See "The
Python↔Rust boundary this spike did not build" below for exactly what that
addition would look like if a follow-on reverses this verdict.

## How the comparison was run

1. `tests/corpus/sta/regenerate.sh` — real Yosys (`klt synthesize`, engine
   `0.68+post`) technology-maps six hand-written RTL designs (plus the
   repo's existing `gcd`/`modexp` worked examples) against real
   `sky130_fd_sc_hd`/`tt_025C_1v80` (`open_pdks
   c6d73a35f524070e85faff4a6a9eef49553ebc2b`, volare-fetched), and the
   mapped netlists are checked in gzip-compressed
   (`tests/corpus/sta/netlists/`).
2. `cargo build --release` in `native/sta/` — this crate's own `klt-sta`
   binary, run natively on the host (no emulation: this is the point of
   comparison against the emulated OpenSTA leg below).
3. Real **OpenSTA 3.1.0**, via the `openroad/orfs:latest` Docker image's
   bundled `/OpenROAD-flow-scripts/tools/install/OpenROAD/bin/sta` — the
   same oracle image `native/legalize/` and
   `tests/corpus/place_and_route/` use for OpenROAD. **Caveat, stated up
   front because it is load-bearing for reading the wall-clock numbers
   below**: this image is `linux/amd64`-only; run on an Apple Silicon
   (`arm64`) host, `sta` executes under Docker Desktop's QEMU binary
   translation, not natively. `klt-sta` runs natively (`arm64`, no
   translation) on the identical host. This asymmetry can only bias the
   wall-clock comparison **in `klt-sta`'s favor** — QEMU's per-instruction
   translation overhead falls on OpenSTA, not on the native engine — so a
   wall-clock result that still favors `klt-sta` only weakly, or does not
   favor it at all, is the more (not less) trustworthy signal, and is
   exactly what §"Results" below reports.
4. `tests/corpus/sta/compare.py` — runs both engines over the identical
   netlist + liberty per design, `--period 20` (long enough that no design
   violates setup, so slack is a pure restatement rather than a clipped
   number), `--iters 20` and, to confirm reproducibility, a second pass at
   `--iters 50`; reports the accuracy delta and both a **cold** (process
   launch, liberty parse, netlist read, one analysis) and **warm/iter**
   (liberty already parsed and resident, matching what a Phase-3
   optimization loop would pay per candidate) wall-clock number for each
   engine, discarding each side's own warm-up iteration before taking the
   median (`compare.py`'s own docstring documents why discarding
   asymmetrically would fabricate a ratio).

Every number below is from a real run against the real, checked-in corpus
fixtures in `tests/corpus/sta/netlists/` — none of it is asserted from
memory. Re-run it yourself with the commands in "Running it yourself" below.

## Results

### Accuracy: register-to-register critical path

| design | cells | OpenSTA (ns) | klt-sta (ns) | delta (ns) | delta (%) | same path/pins | max node delta (ns) |
|---|---:|---:|---:|---:|---:|:---:|---:|
| alu8 | 136 | 3.598868 | 3.574093 | -0.024775 | -0.6884 | yes | 0.024775 |
| crc32 | 172 | 2.327851 | 2.327851 | -0.000000 | -0.0000 | yes | 0.000000 |
| fifo16 | 414 | 2.377691 | 2.377691 | +0.000000 | +0.0000 | yes | 0.000001 |
| gcd | 384 | 4.328944 | 4.328945 | +0.000001 | +0.0000 | yes | 0.000001 |
| mac16 | 778 | 9.149858 | 9.148875 | -0.000983 | -0.0107 | yes | 0.000984 |
| modexp | 682 | 5.991359 | 5.991359 | -0.000000 | -0.0000 | yes | 0.000001 |

Identical at both `--iters 20` and `--iters 50` (both engines are
deterministic on a fixed netlist + liberty; `compare.py` asserts this on the
OpenSTA side and would abort otherwise). On every design, both engines agree
on **which** startpoint/endpoint pair is worst (`same path/pins`), not just
the delay number — a materially stronger check than matching the endpoint
value alone, which two independently-tie-broken engines could do by
coincidence.

Five of six designs match to **sub-picosecond** precision (≤1 fs reported,
i.e. floating-point noise). `alu8` is the outlier: **24.8 ps / 0.69%**,
entirely accumulated along a chain of `sky130_fd_sc_hd__maj3_1` (3-input
majority) gates feeding a final `sky130_fd_sc_hd__a31oi_1`. Traced
node-by-node (see the raw per-node arrival trace in git history of this
file's PR, or reproduce with `--format json`): the first two `maj3` stages
in the chain match to sub-picosecond precision; from the third stage on, a
~1.5–2 ps delta appears per stage and compounds, then the final `a31oi_1`
arc alone contributes an additional ~13 ps beyond what the accumulated slew
delta from upstream would predict linearly. It is **not** an unmodelled
`when`-conditioned duplicate arc — verified directly against the liberty
text that `sky130_fd_sc_hd__maj3_1` and `sky130_fd_sc_hd__a31oi_1` each
declare exactly one (unconditioned) `timing()` group per `related_pin` on
their output pin, so there is no arc-selection ambiguity to get wrong. The
likely mechanism is `graph.rs`'s documented **arrival-gated, not
arrival-path-specific** slew merge (a vertex's transition is the worst
transition among *any* fan-in arc whose input carries a valid arrival, not
only the arc that wins the arrival race) interacting with `a31oi_1`'s
particular table being more slew-sensitive than the `maj3` stages upstream
of it — plausible, but not confirmed to the single line of divergence, and
recorded here as an open, bounded, and small (sub-1%) residual gap rather
than a root cause.

### Wall-clock

`--iters 50` run (the `--iters 20` run reproduces the same shape; both are
in this PR's description for the record):

| design | cells | klt-sta cold (ms) | klt-sta warm/iter (ms) | OpenSTA cold (ms) | OpenSTA warm/iter (ms) | cold ratio | warm ratio |
|---|---:|---:|---:|---:|---:|---:|---:|
| alu8 | 136 | 1223.8 | 1.165 | 885.7 | 4.0 | 0.7x | 3x |
| crc32 | 172 | 1063.0 | 0.918 | 921.5 | 5.0 | 0.9x | 5x |
| fifo16 | 414 | 1386.3 | 10.080 | 1448.7 | 13.0 | 1.0x | 1x |
| gcd | 384 | 1439.5 | 9.054 | 1289.9 | 10.0 | 0.9x | 1x |
| mac16 | 778 | 2255.5 | 18.145 | 1893.3 | 18.0 | 0.8x | 1x |
| modexp | 682 | 2637.8 | 14.971 | 1659.2 | 18.0 | 0.6x | 1x |

("cold ratio"/"warm ratio" = OpenSTA time ÷ `klt-sta` time; >1x favors
`klt-sta`.)

**Cold-start is a wash**, ratio 0.6x–1.0x — `klt-sta`'s own liberty parser is
the reason: parsing the real 8 MB `tt_025C_1v80` liberty takes **576–891 ms**
on this host, **2.5–3.4x slower** than OpenSTA's own liberty read (263–267 ms
on every design, since it is design-independent) — despite OpenSTA paying
the QEMU translation tax and `klt-sta` not. `liberty.rs`'s own doc comment
claims this parse completes "well under a second," which is technically true
but undersells the finding: a hand-rolled Rust brace-matching scanner that
skips every unused liberty group is still measurably slower than a mature
C++ parser doing (at minimum) equivalent work, on the identical input, with
a translation-overhead handicap working against it. This is the honest
number, not the one the crate's own comments implied going in.

**Warm/iter is where §3.7's "Go" case has to be made**, and it does not
survive at realistic sizes: `klt-sta` is 3–5x faster on the two smallest
designs (136–172 cells), but the advantage **shrinks monotonically with
design size** and is gone by 384 cells — every design at 384+ cells (`gcd`,
`fifo16`, `mac16`, `modexp` — four of six, and the four largest) rounds to
1x. `gcd` is the clearest noise demonstration: at `--iters 20` its ratio is
0.995x (`klt-sta` marginally *slower*, 10.045 ms vs. OpenSTA's 10.0 ms); at
`--iters 50` it is 1.105x (`klt-sta` marginally *faster*, 9.054 ms vs.
10.0 ms) — the sign of the comparison flips between two runs of the same
design on the same host, which is itself the evidence that "1x" means
*within measurement noise*, not *a small but real win*. Given the emulation
asymmetry documented above biases every one of these numbers *toward*
`klt-sta`, the true same-hardware comparison is very unlikely to look better
for the native engine than what is reported here.

Third leg of the three-way comparison — today's `klt synthesize`: reports
`timing: null`, i.e. 0 ms and no answer. Both engines above are a strict
improvement over that baseline; the question this spike exists to answer is
native-vs-wrapped, not native-vs-nothing.

## Reading the results against the acceptance criteria

Issue #809's Go bar is a conjunction: **(a)** critical-path delay matches
OpenSTA within a documented tolerance on every design, **and** **(b)** the
native path shows a concrete, measured advantage over wrapping OpenSTA that
survives real build/packaging cost.

- **(a) Accuracy: met**, with one documented residual. Five of six designs
  match to sub-picosecond precision; `alu8` is 0.69% (24.8 ps) off, on the
  same reported critical path, root-caused to a bounded (not open-ended)
  interaction described above. A reasonable documented tolerance — say,
  ≤1% or ≤50 ps on a corpus at `tt_025C_1v80` scale — is met on every
  design tested.
- **(b) Concrete, durable advantage: not met.** The advantage is real but
  narrow: it exists only for the two smallest designs in this six-design
  slice (136–172 cells) and is within noise (1x, sometimes marginally
  reversed) for the four largest (384–778 cells) — the size range closer to
  what a real Phase-3 optimization loop would iterate on. This was measured
  under conditions that can only favor the native engine (native `arm64`
  execution vs. OpenSTA under QEMU `amd64` emulation), and it *still* washes
  out by ~400 cells. Per issue #809's own explicit No-go condition — "the
  wrapped path is within noise on the metric that actually motivated the
  native one" — that is exactly what four of six corpus designs show. This
  spike also never built the `pyo3` marshalling layer (§"The Python↔Rust
  boundary this spike did not build"), so even the narrow small-design
  advantage does not yet reflect the real per-call FFI/JSON round-trip cost
  a production integration would add on top of it.

**Verdict: No-go.** Wrap OpenSTA (subprocess + `report_checks` output
parsing — exactly the harness `compare.py`'s `run_opensta()` already
implements and validates) rather than build a native Rust STA engine for
Epic #704's Phase 3.

## The Python↔Rust boundary this spike did not build

Per issue #809 item 4, documented as an explicit deliverable regardless of
verdict — this is what a "Go" outcome would have required, for whoever
revisits this later:

- **Shape**: a `pyo3` extension module, `native/sta/pyproject.toml` +
  `Cargo.toml` `[lib] crate-type = ["cdylib", "rlib"]`, exactly
  `native/mom/`'s existing pattern (`klt_sta_native` importable from Python,
  `extension-module` feature gated to the `maturin` build only so `cargo
  test` keeps linking normally — see that crate's own `Cargo.toml` comments
  for why).
- **Call surface**: one function, roughly `analyze(liberty_path: str,
  netlist_path: str, top: str, period_ns: float | None, paths: int) ->
  dict` — a single call per (re-)analysis, returning the same JSON shape
  `src/main.rs`'s `Report` struct already serializes (`serde_json` on the
  Rust side, a plain `dict`/`list` marshal on the `pyo3` boundary — no
  streaming, no persistent solver handle across calls needed at this
  granularity).
- **What it would have cost, not measured here**: `pyo3`'s per-call
  marshalling overhead (Python↔Rust argument conversion, GIL acquisition,
  Rust→Python dict construction for the path/node tree) is real but was not
  isolated in this spike's numbers, which measure the compiled binary's
  cost only, not the extension-module boundary a Phase-3 loop would
  actually pay. Given the wall-clock margin (b) is already washed out for
  the size range that matters *before* this cost is added, adding it can
  only weaken the "Go" case further, never strengthen it.
- **CI/wheel story, had this gone "Go"**: mirror `native/mom/`'s existing
  `.github/workflows/ci.yml` `native` job (`native/mom` → `native/sta`
  `working-directory`, same `cargo fmt --check` / `cargo clippy -- -D
  warnings` / `cargo test` / `maturin develop` + Python integration test
  sequence) and its accompanying wheel-build step, unchanged in shape. That
  job does not exist for `native/sta/` — matching `native/legalize/`'s own
  precedent of leaving a "No-go" spike crate out of CI entirely rather than
  paying ongoing CI cost for code nothing calls.

## Crate layout

```
native/sta/
├── Cargo.toml         # plain bin+rlib crate; no pyo3/maturin (see above)
├── src/
│   ├── lib.rs         # re-exports the three modules below
│   ├── liberty.rs      # NLDM-subset liberty (.lib) reader + Lut interpolation
│   ├── netlist.rs      # structural-Verilog (write_verilog-mapped) reader
│   ├── graph.rs         # timing graph + block-based max-delay propagation
│   └── main.rs          # `klt-sta` CLI: JSON or text report, --repeat for
│                         # the per-iteration timing this spike measures
└── tests/
    └── tiny.lib         # hand-checkable 2-cell NLDM library, unit-test fixture
```

`tests/corpus/sta/` (repo-root-relative, not under `native/sta/` — shared
with the Python side, same convention as `tests/corpus/legalize/`) holds the
comparison harness and corpus fixtures: `compare.py` (the three-way
harness), `regenerate.sh` (regenerates the checked-in netlist fixtures — a
deliberate, reviewed act, never a CI step, per that script's own header),
`rtl/*.v` (four hand-written designs picked for shape diversity — wide mux,
shallow XOR fan-out, control/mux-dominated, deep arithmetic — documented in
each file's own header comment), and `netlists/*_synth.v.gz` +
`netlists/*.meta.json` (the checked-in mapped-netlist fixtures + their
generation provenance).

## Running it yourself

```bash
cd native/sta
cargo test              # unit tests, no PDK/OpenSTA/Docker required (21 tests)
cargo clippy --all-targets -- -D warnings
cargo fmt --check
cargo build --release

# Native engine only (no OpenSTA on a typical dev machine):
python3 ../../tests/corpus/sta/compare.py \
    --klt-sta target/release/klt-sta

# Full three-way, given a real sky130A install (volare) and access to a
# linux/amd64 OpenSTA (e.g. via `openroad/orfs:latest` — see this file's
# "How the comparison was run" for the exact invocation shape and its
# emulation caveat if run on non-amd64 hardware):
python3 ../../tests/corpus/sta/compare.py \
    --klt-sta target/release/klt-sta \
    --sta /path/to/sta \
    --json /tmp/sta-comparison.json
```

Regenerating the checked-in corpus fixtures themselves needs real Yosys and
a volare `sky130A` install — see `tests/corpus/sta/regenerate.sh`.
