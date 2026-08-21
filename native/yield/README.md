# `klt-yield-native`

The Rust statistics core behind `klt yield` / `klt yield-campaign` / `klt
yield-sensitivity` (issue #816, Phase 1a of the statistical/yield epic
#710).

This repo's third Rust component (after `native/mom/`, issue #718, and
`native/congestion/`, issue #785), following the same standalone
`maturin`-built `pyo3` extension convention — its own `Cargo.toml` +
`pyproject.toml`, reached from the top-level `pyproject.toml` via a PEP 735
dependency group. The split with Python mirrors `mom.py`/`congestion.py`:
reading a `klt sim` Monte Carlo report (or a plain sample-set document) and
turning it into this crate's request shape is the Python layer's job
(`src/klayout_tools/yield_analysis.py`); everything statistical lives here.

## What it does

- `contract.rs` — the Rust/Python JSON boundary; see
  [`docs/cli/yield.md`](../../docs/cli/yield.md) for the user-facing schema.
- `stats.rs` — dependency-free special functions and sample statistics
  (`erf`/`erfc`, `norm_ppf`, `ln_gamma`, the regularized incomplete beta
  function, …), each a published, citable algorithm rather than a
  hand-rolled approximation.
- `estimate.rs` — turns a Monte Carlo sample set + spec limits into a yield
  estimate: an empirical Clopper-Pearson estimate (exact binomial interval,
  no distributional assumption) and a parametric normal estimate
  (delta-method interval), reported side by side, plus the sample-size
  verdict. **Structurally enforces one rule**: a yield number never travels
  without its confidence interval and sample count — the response types
  make an interval-less estimate unrepresentable, and
  `guard_no_bare_point_estimate` rejects a degenerate interval before
  serialization (see `docs/cli/yield.md`, "Never a bare point estimate").
- `sensitivity.rs` — `klt yield-sensitivity`'s ranking (issue #923, Phase
  3): attributes an output metric's variance across a completed campaign to
  the device/process parameters drawn per sample, via a correlation/
  regression-based method (a full Sobol/variance-based decomposition is
  deliberately deferred, per that issue's scope note).

## Building / testing

```bash
uv sync --extra dev --group yield
uv run klt yield examples/yield/mc-samples.json --limits examples/yield/spec-limits.json
```

or, without `uv`:

```bash
cd native/yield
maturin develop --release
```

```bash
cd native/yield
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
```

Full build/install reference, including the "rebuilding after a Rust
change" cache gotcha and the clean-failure behavior when the extension
isn't built: [`docs/cli/yield.md#building-the-native-extension`](../../docs/cli/yield.md#building-the-native-extension).
