# `klt-statime-native` — Epic #704 Phase 1c spike (issue #809)

**Verdict: Go**, on both halves of this issue's own acceptance criteria —
accuracy against OpenSTA, and a concrete latency advantage over wrapping it
that requires no new engineering — measured on a 3-design corpus slice,
documented below. This crate stays a spike artifact (not wired into `klt
synthesize`) until a follow-on issue does that integration; see "Not in
scope" at the end.

**Correction (during PR review, same session):** an earlier draft of this
document reported a "~30–40×" native advantage and "still ~1.5–2× faster"
even in the most sympathetic wrap scenario. Both numbers were wrong,
caught by re-running the full comparison independently rather than trusting
the checked-in table: they used `openroad/orfs:latest` (1.59 GB, requires
two LEF files just to `link_design`) under the mistaken belief that
OpenSTA has no standalone build, and they compared native Rust's **warm**
number against OpenSTA's **cold** number — not an apples-to-apples
regime. **A standalone, LEF-free OpenSTA build exists** — `openroad/opensta`
on Docker Hub (587 MB image, `OpenSTA/build/sta`) — and needs only
`read_liberty`/`read_verilog`/`link_design`/`report_checks`, exactly this
crate's own inputs. Re-measured against that build, in both regimes
apples-to-apples, the real result is smaller and more nuanced than first
reported: see "Results — performance" below for the corrected numbers and
"Why OpenROAD's embedded OpenSTA, not a standalone binary" for the
corrected story of what OpenSTA needs to run. The accuracy numbers
(§"Results — accuracy") were independently re-verified against the
standalone build too and are unchanged — they were never affected by the
image mixup.

## What this is

A native-Rust liberty (NLDM) parser plus a gate-level static-timing engine:
parses a `.lib` file's cell timing tables (`liberty.rs`), parses a
`write_verilog -noattr`-produced structural netlist (`netlist.rs`), builds
a rise/fall-aware timing graph over it, and reports the worst
register-to-register (or input-to-output, for a combinational design)
critical path with NLDM bilinear interpolation (`nldm.rs`, `sta.rs`).
Deliberately **not** a placer, mapper, or optimizer — see
`docs/design/synthesize-qor-improvements-survey.md` §3.7 ("Sketch B"), the
proposal this issue implements.

## How the comparison was run

1. `tests/corpus/statime/regenerate.sh` — real `klt synthesize` (Yosys +
   `abc`, today's shipped default flow, no `-constr`/`-D`) against three
   RTL designs against `sky130_fd_sc_hd`/`tt_025C_1v80`: `gcd.v` (the
   repo's own sequential worked example), `modexp.v` (a larger sequential
   RSA-accelerator design, also already in
   `examples/functional-verification/`), and `mult8.v` (a new, purely
   combinational 8×8 multiplier this spike adds — no registers, so its
   critical path is input-port-to-output-port rather than
   register-to-register, complementing the other two).
2. This crate's own `klt-statime critical-path <netlist> <liberty> --top
   <name>` on the identical mapped netlist + liberty, with a uniform
   boundary condition (`--input-transition-ns 0.05 --output-load-pf 0.03`
   on every primary input/output — no SDC, no `create_clock`, matching
   OpenSTA's own `-unconstrained` report mode used for the oracle below).
3. **OpenSTA** (`read_liberty` / `read_verilog` / `link_design` /
   `report_checks`), same netlist + liberty + boundary condition —
   captured via OpenROAD's embedded copy and independently cross-checked
   against the standalone `openroad/opensta` build (identical numbers; see
   "Why OpenROAD's embedded OpenSTA, not a standalone binary" below for the
   corrected story and why the standalone build is the right one to use
   going forward). `tests/corpus/statime/oracle_results.json` is a checked-in
   snapshot; `tests/corpus/statime/README.md` documents exactly how to
   reproduce it.
4. `tests/corpus/statime/compare.py` — runs step 2 fresh and diffs it
   against the step-3 snapshot; the table below is its output.

Every number below is from a real run against the real, checked-in corpus
fixtures in `tests/corpus/statime/` — none of it is asserted from memory.

## Results — accuracy

| design | cells | OpenSTA worst path (ns) | Rust worst path (ns) | delta | same start/end pins? |
| --- | --- | --- | --- | --- | --- |
| `gcd` (sequential) | 353 | 4.5250 | 4.4643 | **1.34%** under | yes — `_640_/Q → _640_/D` (a register feeding its own next-state logic) |
| `mult8` (combinational) | 234 | 4.0100 | 3.9954 | **0.36%** under | yes — `b[5] → p[14]` |
| `modexp` (sequential) | 673 | 7.3700 | 7.3371 | **0.45%** under | yes — `_1186_/Q → _1204_/D` |

Reproduce with `uv run python3 tests/corpus/statime/compare.py`.

**All three designs land within 1.34% of OpenSTA, on the identical
start-to-end path both engines independently selected as the design's
global worst** (not just a coincidentally-similar different path — verified
by cross-querying OpenSTA with an explicit `-from`/`-to` for the exact pair
this crate reports, per-arc breakdown included). The delta is consistently
an *under*-estimate, not over — a residual worth flagging (see "Known
simplifications" below), but a clear "matches within a documented
tolerance" result by any reasonable reading of this issue's own Go
condition.

### Getting here: two real bugs this comparison caught, not assumed away

An earlier version of this engine used a single `max(cell_rise,
cell_fall)` value per hop (no separate rise/fall tracking) and clamped
NLDM lookups to the table's edge value outside its characterised range.
Both were *measured*, not theorised, to matter — this is exactly the kind
of finding a real oracle comparison is supposed to surface, not something
this spike is glossing over:

- **Rise/fall collapse**: overstated `gcd`'s critical path by up to
  **38%** (3.9585 ns vs. OpenSTA's 2.87 ns on one specific path) — the
  `max(rise, fall)` choice picks the worse of two *unrelated* physical
  quantities at every hop instead of following the one polarity actually
  active, and the error compounds hop over hop. Fixed by tracking rise and
  fall arrivals/transitions **separately** per net and using each arc's
  `timing_sense` (positive/negative/non-unate) to pick the correct input
  edge for each output edge — the standard technique, not a novel one.
- **Clamp-vs-extrapolate at the NLDM table boundary**: understated `gcd`'s
  critical path by **~9%** on the affected path — a real, unbuffered net
  in the *default* (no `-constr`) synthesis flow drives 0.0668 pF into a
  `sky130_fd_sc_hd__a21boi_0` gate, whose own liberty entry caps out at
  `max_capacitance: 0.0485`. OpenSTA extrapolates past the table edge along
  the last segment's slope; the clamped version froze at the edge value.
  Fixed by extrapolating (`nldm.rs`) instead of clamping — this is also
  the *first* corpus design (of the two originally measured) that actually
  exercises this path, which is why it wasn't caught on `mult8` alone.

Both fixes are documented here with the before/after numbers exactly as
measured, rather than retroactively smoothed into a clean final version
with no trace of the wrong answer that preceded it.

## Results — performance (the "wrap OpenSTA" three-way comparison)

Both engines measured **the same way**: `clock milliseconds`-instrumented
Tcl (OpenSTA) vs. this crate's own `timings_ms` output (Rust), each run
against the identical netlist/liberty pair, `openroad/opensta:latest`
(the standalone, LEF-free build — see below) via `docker exec` against a
long-lived container (so container-startup cost is excluded from both
sides symmetrically), 3–10 repeats per design/regime, all three corpus
designs, not just one:

| design | regime | OpenSTA (ms) | Native Rust (ms) | ratio |
| --- | --- | --- | --- | --- |
| `gcd` | cold (fresh liberty load each call) | 329 (5 reps, 253–375) | 159 (5 reps, 155–161) | **2.07×** native |
| `mult8` | cold | 251 (3 reps, 241–257) | 161 (5 reps, 157–166) | **1.55×** native |
| `modexp` | cold | 255 (3 reps, 252–257) | 165 (5 reps, 161–172) | **1.55×** native |
| `gcd` | warm (liberty loaded once, iterate) | 12.6 (10 reps, 12–14) | 12.4 (5 reps, 12.2–12.7) | ~parity |
| `mult8` | warm | 8.3 (10 reps, 8–10) | 12.5 (5 reps, 11.9–13.4) | **OpenSTA 1.5× faster** |
| `modexp` | warm | 24.3 (10 reps, 21–27) | 16.3 (5 reps, 15.1–17.0) | **1.49×** native |

"cold" = a fresh process/invocation pays its own liberty parse every call
(this repo's actual convention today — `synthesize.py` and
`place_and_route.py` both spawn one subprocess per stage, no persistent
session anywhere). "warm" = liberty parsed/loaded once, then only
netlist + analysis repeat — the regime a future Phase-3 optimization loop
would want, achieved on the OpenSTA side only via a hand-built persistent
Tcl session (new session-management machinery this codebase has nowhere
else today) and on the Rust side "for free," because reusing an in-memory
`liberty::parse()` result across calls needs no new machinery — it is
what an ordinary long-lived Rust value already does.

**Reading this against the acceptance criteria's "concrete, measured
advantage that survives real build/packaging cost, most plausibly
per-iteration latency inside a future Phase-3 optimization loop," honestly
this time:**

- **Cold / no-new-engineering regime — a real, consistent 1.5–2.1× native
  advantage on every design tested**, requiring zero new infrastructure on
  either side. This is the comparison that matches what "wrap OpenSTA"
  concretely means in this repo *today*, since no persistent-session
  machinery exists for any wrapped engine. It clears the bar, but it is a
  **materially smaller number than an earlier draft of this document
  reported** (~30–40×) — that number was an apples-to-oranges comparison
  (native's *warm* number against OpenSTA's *cold*, heavier-image number)
  and is retracted, not merely revised.
- **Warm / hand-engineered-session regime — genuinely within noise, not a
  clean native win.** Native is ~1.5× faster on `modexp`, ~1.5× *slower*
  than OpenSTA's warm session on `mult8`, and at parity on `gcd`. There is
  no consistent direction here across three designs — the honest read is
  "roughly the same order of magnitude," which is close to the wrapped-path
  No-go trigger ("within noise on the metric that actually motivated the
  native one") for the *specific* engineered-session comparison. This is a
  materially different (and much more honest) finding than an earlier
  draft's claim of "still ~1.5–2× faster … in the most sympathetic
  scenario," which does not survive re-measurement against the correct,
  lighter, LEF-free OpenSTA build.
- **What this spike did *not* measure, and is not entitled to claim**: a
  true in-process comparison, where a Rust Phase-3 optimizer calls this
  engine as a plain function (`sta::analyze`, no subprocess spawn, no CLI
  JSON round-trip) versus OpenSTA, which — even in its best-case warm
  regime above — still pays cross-process Tcl IPC because it structurally
  cannot be called in-process from a Rust caller. That comparison would
  plausibly widen the gap further in native's favor (removing the CLI
  process-spawn/JSON-write overhead this spike's numbers still include),
  but this spike built no `pyo3` binding (see "Not in scope"), so this is
  a documented hypothesis for the follow-on integration issue to verify,
  **not evidence this document is entitled to use for its own verdict.**

**Verdict basis, stated plainly:** the decisive, cleanly-measured, every-
design-consistent result is the cold/no-new-engineering regime's 1.5–2.1×
— real, reproducible, and requiring no infrastructure this repo doesn't
already have (`Go`). The warm/engineered-session result is reported with
equal prominence rather than buried, because it is a genuine, important
caveat: it shows the *strongest* form of the original claim ("native wins
even in OpenSTA's best case") does not hold, and it means the case for
Phase-3 integration should rest on the in-process/no-subprocess argument
above — a hypothesis, not (yet) a number — rather than on the warm-session
comparison this spike actually ran.

### Why OpenROAD's embedded OpenSTA, not a standalone binary — corrected

**An earlier draft of this section was wrong** and is left here, corrected
in place, as the record of what happened rather than silently smoothed
over. It claimed `find / -iname '*opensta*'` inside `openroad/orfs:latest`
came up empty and concluded no standalone OpenSTA build exists — true only
of that one, ORFS-flow-oriented image. **A standalone, dedicated OpenSTA
build does exist**: `openroad/opensta` on Docker Hub (587 MB vs.
`openroad/orfs`'s 1.59 GB), entrypoint `OpenSTA/build/sta`, and it runs
`read_liberty` / `read_verilog` / `link_design` / `report_checks` with
**no LEF file at all** — verified live, `sta -no_init -exit` against this
spike's own checked-in netlists and the resolved `sky130A` liberty,
reproducing `oracle_results.json`'s exact numbers (4.5250 / 4.0100 / 7.3700
ns) with zero LEF mounted. `tests/corpus/statime/README.md`'s reproduction
recipe now uses this lighter, correct image.

This matters for the wrap-vs-native argument in the opposite direction
from the original claim: OpenSTA is **not** architecturally tied to
OpenROAD/LEF for a pure-timing use case, so "wrap OpenSTA" does not
automatically inherit a P&R-oriented dependency footprint the way the
first draft argued. The corrected argument for native Rust rests on the
performance numbers above and the in-process-call structural argument, not
on a dependency-footprint claim that does not hold up.

## Known simplifications (what would need hardening before "Go" becomes "ship it")

1. **No wire delay / no parasitics.** A net's arrival equals its driver
   pin's arrival exactly — the same pre-layout, wire-free estimate class
   `docs/design/synthesize-qor-improvements-survey.md` §1.4 already
   documents for ABC's own `stime` (`WireLoad = "none"`), not a new
   limitation this spike introduces.
2. **Uniform boundary condition.** Every primary input (including the
   clock net — no `create_clock` is modeled, matching the OpenSTA
   `-unconstrained` oracle) gets the same input transition; every primary
   output gets the same extra load. No SDC, no per-pin overrides.
3. **Register data pins identified by the literal name `"D"`.** Correct
   for every `sky130_fd_sc_hd` sequential cell this corpus uses
   (`dfrtp_1`), not a general liberty-driven classification.
4. **Async set/reset arcs are parsed but never used for propagation** —
   `recovery`/`removal`/`clear`/`preset`/setup/hold timing types are all
   recognised by `liberty.rs` (`ArcKind::Other`) and simply excluded from
   delay propagation, matching this issue's own stated scope.
5. **A small residual accuracy gap remains** (0.36–1.34%, always an
   *under*-estimate on this corpus). The likely remaining sources: (a) this
   engine's own `related_pin`/arc-selection logic may not be
   bit-for-bit identical to OpenSTA's in every corner case, and (b) only 3
   corpus designs were tested — a wider corpus could surface a larger gap
   on a design shape not yet seen. Before this engine backs any real
   signoff decision, closing this gap (or adopting an explicit, published
   safety margin) is real follow-on work, not assumed away here.
6. **The strongest form of the performance argument — a true in-process
   call from a Rust Phase-3 optimizer, no subprocess/CLI/JSON round-trip at
   all — was not measured.** This spike's "native" numbers are all through
   the `klt-statime` CLI binary (process spawn + JSON file write included),
   and the *documented* performance result (see "Results — performance") is
   the more modest, no-new-engineering cold-regime comparison (1.5–2.1×),
   not the larger gap the in-process argument would plausibly produce. A
   follow-on `pyo3` integration issue should measure this directly before
   leaning further on it.

## Crate layout, the Python↔Rust boundary, and the CI/wheel story

The issue asks this be documented explicitly, since paying this cost was
originally expected to be a large part of what this spike measures.
**That premise is now partly stale**: this is the **third** Rust crate in
this repo, not the first. `native/mom/` (issue #718, merged as "this
repo's first Rust component" per its own docstring and `ci.yml`'s own
comment) already answered the crate-layout and pyo3/maturin/CI questions;
`native/legalize/` (issue #784) already answered the "spike stays a plain
binary, no pyo3, until a Go verdict" question this crate's own `Cargo.toml`
comment reuses verbatim. This crate mostly **reused** those decisions
rather than re-deriving them — which is itself a data point: the marginal
cost of a *third* Rust component, once the pattern exists, is close to
zero decision-making overhead, just the numerics.

**Crate layout** (`native/statime/`, following the established
`native/<engine>/` convention exactly):

```
native/statime/
├── Cargo.toml         # klt-statime-native — plain [[bin]] + [lib], no pyo3 (see below)
├── Cargo.lock
├── README.md           # this file
└── src/
    ├── lib.rs           # public module tree
    ├── liberty.rs        # .lib tokenizer/parser (825 lines incl. tests)
    ├── netlist.rs         # Yosys write_verilog -noattr parser (367 lines incl. tests)
    ├── nldm.rs             # bilinear interpolation + extrapolation (130 lines incl. tests)
    ├── sta.rs               # timing graph + rise/fall-aware propagation (537 lines incl. tests)
    └── main.rs                # `klt-statime critical-path` CLI
```
2,029 lines total including unit tests (11 tests, all in-module — no
integration-test harness needed yet). Dependencies: `serde` +
`serde_json` only — no numerics crate (`nalgebra`, the one `native/mom/`
needs) and no pyo3, so this is a lighter build than either existing crate.
`cargo build --release` from clean: ~6–11 s (measured, this session).

**Deliberately a plain `cargo build`/`cargo test` binary crate for this
spike** — no `pyo3`/`maturin` wiring, following `native/legalize/`'s
precedent (issue #784) rather than `native/mom/`'s production shape, and
for the identical stated reason: issue #809 is explicitly go/no-go, and
adding the production-integration surface before the verdict was known
would have spent real maintenance cost on a result that might have been
"No-go." Since the verdict *is* "Go," the natural next step (a **separate**
follow-on issue, not this one — see "Not in scope") is exactly the
`native/mom/Cargo.toml` shape already proven in this repo:

- `[lib] crate-type = ["cdylib", "rlib"]` (the `rlib` half is why `cargo
  test` keeps working unmodified — a `cdylib`-only crate can't link as a
  normal Rust test binary).
- `pyo3 = { version = "0.29" }` as a dependency, **without**
  `extension-module` enabled in `Cargo.toml` itself (that would break
  `cargo test`'s own linking against libpython) — instead a sibling
  `pyproject.toml`'s `[tool.maturin] features = ["pyo3/extension-module"]`
  turns it on only for the `maturin build`/`develop` path.
- **The Python↔Rust boundary itself**, mirrored exactly from
  `native/mom/src/lib.rs`'s own `solve_mom_json`: a single `#[pyfunction]`
  taking a JSON-string request and returning a JSON-string response
  (`PyResult<String>`, errors mapped to `PyValueError`) — e.g. a
  `critical_path_json(netlist_v: &str, liberty_lib: &str, top: &str,
  input_transition_ns: f64, output_load_pf: f64) -> PyResult<String>`
  wrapping this crate's existing `sta::analyze` and serialising its
  existing `StaResult`. The boundary stays a plain data contract — no
  bespoke object graph crosses the FFI boundary, consistent with every
  other `klt` verb being JSON-contracted end to end
  (`docs/json-contract.md`). This crate's `StaResult`/`PathReport` types
  already derive `serde::Serialize`, so no new serialisation code would be
  needed for that binding — only the `#[pyfunction]`/`#[pymodule]`
  wrapper itself (`native/mom/src/lib.rs` is ~40 lines of that wrapper
  code for comparison).

**CI/wheel story**: `native/legalize/` (the precedent this crate follows)
has **zero CI wiring** today — confirmed by grep against `.github/
workflows/ci.yml`, which has no `legalize` job. This crate follows the
same posture deliberately, for the same reason (a spike crate's tests
don't need to gate every PR in the repo before its own verdict is known).
`native/mom/` is the CI template a "Go" follow-on would copy: `ci.yml`'s
`native:` job (lines ~48–100) is ~50 lines — no Rust toolchain install
step needed (`ubuntu-latest` GitHub runners ship a rustup-managed stable
Rust already), a `~/.cargo/registry` + `native/<crate>/target` cache keyed
on `Cargo.lock`, then `cargo fmt --check` / `cargo clippy --all-targets --
-D warnings` / `cargo test` as gating steps, followed by a `maturin`-driven
build + install of the extension and a real end-to-end `klt <verb>` run
against it. Adding this crate's own job is a ~15-line copy of that
existing block (swap `native/mom` for `native/statime`, `klt mom` for the
verb this engine would eventually back) — a bounded, already-proven cost,
not an open question.

## Running it yourself

```bash
cd native/statime
cargo test              # 11 unit tests, no PDK/OpenROAD/Docker required
cargo build --release
uv run python3 ../../tests/corpus/statime/compare.py   # the table above
```

Regenerating the checked-in netlist fixtures themselves needs real Yosys +
a volare `sky130A` install (`tests/corpus/statime/regenerate.sh`);
regenerating the OpenSTA oracle snapshot additionally needs Docker + the
`openroad/opensta:latest` image (`tests/corpus/statime/README.md`) — neither
is required to reproduce the comparison above, which reads the checked-in
snapshot.

## Not in scope (per the issue)

Native elaboration, logic optimization, or technology mapping (Epic #704
Phases 1–2 proper). Wiring this engine into `klt synthesize`'s response
contract — a separate follow-on issue, now that this spike says "Go";
issue #807 fills the `timing` field in the meantime with a caveated ABC
`stime` estimate (§3.3 of the survey). The `pyo3`/`maturin` production
integration sketched above is likewise a separate issue's scope, not this
one's.
