# `klt-legalize-native` — Epic #700 Phase 1 spike (issue #784)

**Verdict: No-go**, on quality-of-result (QoR) grounds, per this issue's own
acceptance criteria. This crate stays in the repo as the spike's own
artifact and the retired FFI/data-plumbing exercise Epic #700's later phases
can still learn from — it is **not** wired into `klt place-and-route`/`klt
par`, and should not be, until a follow-on materially closes the QoR gap
documented below.

## What this is

An Abacus-style ([Spindler & Johannes, ISPD 2008](https://doi.org/10.1145/1353629.1353655))
standard-cell row legalizer, implemented from scratch in Rust: reads an
OpenROAD-produced, pre-legalization "global placement" DEF checkpoint,
legalizes it (zero overlap, minimal displacement, on the site grid), and
writes a legalized DEF back out.

Deliberately a plain `cargo build`/`cargo test` binary crate — **no**
`pyo3`/`maturin`/CI wiring (unlike `native/mom/`, the packaging convention
this crate otherwise follows at `native/<engine>/`). Issue #784 is
explicitly go/no-go; adding the production-integration surface before a
"Go" verdict would spend real maintenance cost on a result that turned out
"No-go". If a follow-on later reverses this verdict, the `pyo3` extension-
module shape is a known, bounded addition (`native/mom/Cargo.toml`'s own
comments document exactly what that adds) — this crate does not need to
move to adopt it.

## How the comparison was run

1. `tests/corpus/legalize/regenerate.sh` — real Yosys (`klt synthesize`) +
   real OpenROAD (`openroad/orfs:latest`, sky130A via volare) — synthesizes
   `examples/functional-verification/gcd.v` and `modexp.v`, runs OpenROAD's
   own floorplan → `global_placement` → `repair_design`/`repair_timing`
   sequence (byte-identical to `place_and_route.py`'s own `place` stage up
   to this point), and captures **two** DEF checkpoints from the *same*
   frozen state: one immediately before `detailed_placement` (the common
   "GP" input both legalizers below start from), and OpenROAD's own
   `detailed_placement` result on it (the oracle).
2. `klt-legalize legalize <gp.def> <tech.lef> <cell.lef> unithd <out.def>`
   — this crate's own Rust legalizer, run on the identical GP input.
3. `tests/corpus/legalize/compare.py` — diffs both legalized DEFs against
   the same GP input: overlap count, total/max displacement, an HPWL proxy
   (`compare.py`'s own docstring / `src/metrics.rs`'s documents the exact
   bbox-center simplification and why it is still a fair relative
   comparison), and wall-clock.
4. `tests/corpus/legalize/merge_and_check.py` — merges each legalized DEF
   into a placement-only (pre-route) GDS via `place_and_route.py`'s own
   `_merge_def_to_gds`, runs real `klt drc --deck sky130` against each, and
   diffs the `NETS`/`PINS` sections of the GP input against this crate's
   legalized output byte-for-byte (this crate's `def.rs` never parses those
   sections at all — see that module's docstring — so this is a structural
   guarantee, not just an empirical spot-check, that legalization only
   moves geometry, never touches connectivity).

Every number below is from a real run against the real, checked-in corpus
fixtures in `tests/corpus/legalize/` — none of it is asserted from memory.

## Results

| design | engine | overlaps | DRC violations (placement-only GDS) | HPWL (µm) | total displacement (µm) | legalization wall-clock |
|---|---|---|---|---|---|---|
| gcd (454 cells) | OpenROAD `detailed_placement` | 0 | 1 (`diff.enclosing.licon.1`, pre-existing filler-gap — matches `tests/corpus/README.md`'s documented baseline) | 9,106.9 | 995.4 | 16 ms |
| gcd (454 cells) | `klt-legalize` (Rust) | 0 | 1 (same rule, same count) | 11,093.8 (**+21.8%**) | 3,123.1 (**+213.8%**) | 0.11 ms |
| modexp (844 cells) | OpenROAD `detailed_placement` | 0 | 6 (same rule) | 18,228.0 | 2,375.6 | 17 ms |
| modexp (844 cells) | `klt-legalize` (Rust) | 0 | 6 (same rule, same count) | 23,067.1 (**+26.6%**) | 11,958.6 (**+403.4%**) | 0.25 ms |

Connectivity invariance (`NETS`/`PINS` byte-diff, GP input vs. Rust output):
**unchanged on both designs**.

## Reading the results against the acceptance criteria

- **Legality (zero overlap on the site grid): met.** Both legalizers report
  0 bounding-box overlaps on both designs. This was not trivial to get
  right — an earlier version of this crate rounded each Abacus cluster's
  optimal position to the nearest *database unit* rather than the nearest
  *site-grid step*; that passed the bounding-box-overlap check but produced
  over a thousand real `li1.space.1`/`mcon.space.1`/`met1.space.1` design-
  rule violations per design once actually merged to GDS and DRC'd — cells
  a fraction of a site-width apart, neither abutting nor legally spaced.
  Real `klt drc`, not the overlap counter alone, is what caught that; after
  fixing the rounding to the nearest site step, the Rust-legalized variant's
  DRC violation count now matches OpenROAD's own oracle output exactly on
  both designs (the same single pre-existing, already-documented
  filler-cell-gap issue, not a new one). This is the strongest evidence in
  this spike's own favor: the core Abacus cluster-collapse algorithm is
  correctly implemented.
- **Connectivity preservation: met, and structurally guaranteed** (not just
  empirically true this run) — `def.rs` cannot parse, and therefore cannot
  mutate, `NETS`/`PINS`; only `COMPONENTS` placement is ever touched.
- **QoR within a documented tolerance of OpenROAD's own legalizer: not
  met.** HPWL is 21.8%/26.6% worse and total displacement is
  213.8%/403.4% worse than OpenROAD's own `detailed_placement` on the
  identical input, on both corpus designs. No reasonable tolerance
  reading of Epic #700's own "ship when it matches or beats" bar accepts a
  multi-hundred-percent displacement regression.
- **Wall-clock: the core algorithm is genuinely fast** (~150x faster than
  OpenROAD's own `detailed_placement` Tcl-command wall-clock, isolated via
  a `clock milliseconds` pair around just that command) — but this spike
  never built the `pyo3` binding, so it does **not** measure the real
  FFI/marshalling/DEF-round-trip overhead a production integration would
  add (per this issue's own acceptance criteria: "the wall-clock win
  doesn't materialize once real FFI/marshalling overhead is measured" is
  an explicit No-go condition). Moot here regardless — the QoR gap alone
  already fails the bar.

## Root cause (for whoever picks this up next)

This spike's row *legalization* (the Abacus cluster-collapse itself) is
correct and fast. The QoR gap traces to row *assignment*, which this spike
deliberately simplified to "nearest row by y" (see `abacus.rs`'s own
docstring) — a single committed choice per cell, no exploration. OpenROAD's
own `detailed_placement` (its stdout, captured during
`regenerate.sh`'s run) reports a "negotiation legalizer" with a "search
window ±20 sites horizontally, ±5 rows vertically, extendable up to ±500
sites / ±100 rows" — i.e. it searches many candidate rows per cell before
committing, which is exactly the lever this spike's row assignment does
not pull. A real Abacus implementation (per the original paper) does its
own dynamic-programming row-assignment search rather than this spike's
nearest-row shortcut; that is very likely most of the gap. A follow-on
spike that implements real multi-row search — not a change to the cluster
algorithm itself, which this spike already validated — is the natural next
experiment, should Epic #700 want to revisit a native-Rust legalizer later.

## Running it yourself

```bash
cd native/legalize
cargo test --release   # unit tests, no PDK/OpenROAD/Docker required
cargo build --release
python3 ../../tests/corpus/legalize/compare.py            # table above
uv run python3 ../../tests/corpus/legalize/merge_and_check.py  # DRC + connectivity
```

Regenerating the checked-in corpus fixtures themselves (a deliberate,
reviewed act, not something either script above does) needs real Yosys +
OpenROAD + a volare `sky130A` install — see
`tests/corpus/legalize/regenerate.sh`.
