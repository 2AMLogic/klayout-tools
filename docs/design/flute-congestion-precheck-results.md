# FLUTE/RUDY-family congestion pre-check: correlation study results

Issue #785, Epic #700 Phase 1 Section 3.6 of
[`place-and-route-improvements-survey.md`](place-and-route-improvements-survey.md).
This document is the acceptance criteria's own required output: a
correlation-strength report plus a go/no-go verdict for wiring the
pre-check into `digital_fleet.py`'s candidate-ranking DSE loop (#445).

**Verdict: research spike. Ship the estimator + measurement harness now;
do not wire it into `digital_fleet.py`'s gating yet.** The direction of the
signal is strong and encouraging (see below), but this study's own corpus
-- one design (`gcd`), three floorplan-utilization variants -- is far too
small a sample to defend a go decision on an automated gate that would
silently skip a real DSE candidate's route stage. Section "What would
change the verdict" names the concrete next step.

## What was built

- `native/congestion/` -- a pyo3-backed Rust crate (`klt_congestion_native`,
  this repo's second native component after `native/mom/`) implementing a
  provably-correct RSMT-family per-net wirelength estimate (`src/rsmt.rs`:
  exact HPWL for degree <= 3, a rectilinear MST upper bound for degree >=
  4) and RUDY-style ("Rectangular Uniform wire Density," Spindler &
  Johannes, DATE 2007) demand-grid distribution (`src/grid.rs`). 13 Rust
  unit tests, `cargo clippy -- -D warnings` clean, `cargo fmt --check`
  clean.
- `src/klayout_tools/congestion.py` -- the Python boundary: `nets_from_def`
  (a pure-Python, no-native-extension-needed parser for a **placement-stage**
  DEF's `COMPONENTS`/`PINS`/`NETS` sections) and `estimate_congestion` (the
  thin JSON-in/JSON-out wrapper around the Rust extension). 13 Python unit
  tests.
- `src/klayout_tools/place_and_route.py` -- the `place` stage now writes a
  side-artifact placement-only DEF (`<output_dir>/<toplevel>.place.def`,
  alongside the existing `write_db` checkpoint) so a pre-check can read real
  post-placement geometry without needing the far more expensive `route`
  stage to have run first. **Not** part of `run_place_and_route`'s return
  value or `klt place-and-route`'s JSON response schema -- AC4's own
  explicit requirement -- so this is a non-breaking, additive-artifact-only
  change to that module.
- `scripts/research/flute_congestion_correlation.py` -- the correlation
  study driver: reads a manifest of trials (each naming a placement-stage
  DEF and that same candidate's actual post-route outcome), runs the
  pre-check on each, and reports the Pearson correlation between
  `congestion_score` and post-route "badness" (the DRC-violation count, or
  a documented large sentinel for a route that never converged), plus the
  DSE-sweep wall-clock a given reject threshold would have saved. 14 unit
  tests against synthetic trial data (no native extension or real DEF
  needed).
- `scripts/research/regenerate_flute_congestion_corpus.sh` -- reproduces
  this document's own corpus (see below), mirroring
  `tests/corpus/place_and_route/regenerate.sh`'s exact `openroad/orfs`
  Docker recipe. Deliberate, reviewed, manually run -- never a CI step.

## Corpus and method

**One design** (`gcd`, the same worked example as
`tests/corpus/place_and_route/gcd.gds.gz`, issue #436), **three
floorplan-utilization targets**, run end to end through the real `klt
synthesize` -> `klt place-and-route` pipeline against a real
`openroad/orfs:latest` Docker image and a real volare-fetched `sky130A`
install -- the same toolchain `tests/corpus/place_and_route/regenerate.sh`
itself uses, not a simulated or hand-constructed DEF.

| Trial | Requested `utilization_pct` | Reported post-legalization utilization | Outcome |
| --- | --- | --- | --- |
| `util38` | 38 | 53.3% | Routed clean: 0 DRC violations, 71s route-stage wall-clock |
| `util60` | 60 | 83.6% | Routed clean: 0 DRC violations, 146s route-stage wall-clock |
| `util68` | 68 | (placement legalized; never reached route completion) | **Did not converge**: `detailed_route` was still running when killed after 2460s (41 min) -- roughly 20-35x either successful trial's own route wall-clock, for a 384-instance design |

(A fourth attempt at `utilization_pct: 75` was rejected outright by
`detailed_placement` itself -- `DPL-0038`, no room to legalize -- before
the `place` stage's own `write_def` line ever runs, so it produced no
placement DEF and is not a usable trial for this study.)

Each trial's placement-stage DEF (`<toplevel>.place.def`, see "What was
built" above) was fed to `estimate_congestion` at a 20x20 RUDY grid over
the design's core area (the module's own defaults: sky130hd's met2 track
pitch, 3 assumed usable signal-routing layers -- see
`native/congestion/src/contract.rs`'s `DEFAULT_TRACK_PITCH_UM`/
`DEFAULT_LAYER_COUNT` docs for why those specific defaults). Real numbers,
this run:

| Trial | `congestion_score` | `max_density_um_per_um2` | Pre-check wall-clock |
| --- | --- | --- | --- |
| `util38` | 0.626 | 4.08 | 0.07s |
| `util60` | 0.641 | 4.18 | 0.02s |
| `util68` | **0.958** | **6.25** | 0.02s |

`flute_congestion_correlation.py --manifest ... --reject-threshold 0.8`
against this exact corpus:

```json
{
  "trial_count": 3,
  "correlation": { "pearson_r": 0.999 },
  "rejected_candidates": ["util68"],
  "kept_candidates": ["util38", "util60"],
  "false_rejects": [],
  "false_keeps": [],
  "wallclock": {
    "route_wallclock_saved_s": 2460,
    "total_pre_check_wallclock_s": 0.115
  }
}
```

## Reading the numbers honestly

**The direction is exactly what the survey hoped for.** The one candidate
whose route stage genuinely misbehaved (`util68`, the one that never
converged) is also the one candidate the pre-check scores meaningfully
higher than the other two (0.958 vs. 0.626/0.641) -- and it does so from a
DEF the `place` stage produces in a fraction of a second, before the
design has paid for `cts` or any part of `route` at all. At a 0.8
`congestion_score` threshold, the pre-check would have rejected exactly
the one bad candidate and kept both good ones -- zero false rejects, zero
false keeps, in this corpus.

**The AC2 wall-clock case is the strongest part of this result, and it is
real, not modeled.** `util68`'s route stage was still running when this
study killed it at 2460s; the pre-check that would have flagged it costs
0.02s. Even bounding "did not converge" at the 2460s observed (a lower
bound on its true cost -- it had not finished), a DSE sweep that ran the
pre-check before `route` on all three of these candidates would have paid
~0.12s total pre-check cost against ~2460s of route wall-clock avoided on
the one candidate worth skipping.

**The `0.999` Pearson correlation figure is not the strong claim it looks
like, and should not be quoted as if it were.** With `n = 3` and two of
those three trials landing at DRC-violation-count `badness = 0` (both
routed clean) while the third is assigned a large fixed "did not converge"
sentinel (see `flute_congestion_correlation.py`'s own
`BADNESS_SENTINEL_DID_NOT_CONVERGE` docs for why that substitution is
principled, not arbitrary), *any* monotonically-higher congestion score on
the one outlier trial would have produced a correlation coefficient close
to 1.0 -- the statistic is doing very little work over what "the outlier
scored higher" already tells you by inspection. This study never observed
a trial with a **nonzero, finite** DRC-violation count (every routed trial
in this corpus was clean), so it says nothing yet about whether
`congestion_score` tracks *degree* of routing difficulty short of outright
non-convergence -- only that it distinguishes "routes fine" from "does not
converge at all" for this one design.

**One design is not a corpus.** `gcd`'s own net topology, aspect ratio, and
pin count are one point in a large design space; nothing here shows the
same threshold (or even the same qualitative separation) holds for a
design with different net-degree distribution, a macro-heavy design, or a
design at a different absolute utilization range.

## What would change the verdict

A future pass that (a) adds at least 2-3 more independent designs to the
corpus (`scripts/research/regenerate_flute_congestion_corpus.sh` already
generalizes to any `klt synthesize`-able Verilog source, not just `gcd`),
and (b) finds at least one trial with a nonzero, finite DRC-violation count
(not just clean-vs-non-convergent) to test whether `congestion_score`
tracks *graded* routing difficulty, would give AC1/AC3 a real go/no-go
basis. Until then, this ships as a validated, tested library + measurement
harness -- callers who want to experiment with it can call
`klayout_tools.congestion.estimate_congestion` directly -- but it is
deliberately **not** wired into `digital_fleet.py`'s candidate-ranking
gate.
