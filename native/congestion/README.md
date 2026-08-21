# `klt-congestion-native`

The Rust core behind a FLUTE/RUDY-family post-placement congestion pre-check
for the digital fleet DSE (design-space exploration) loop (issue #785, Epic
#700 Phase 1 §3.6 of
[`docs/design/place-and-route-improvements-survey.md`](../../docs/design/place-and-route-improvements-survey.md)).

**Not a shipped `klt` CLI verb.** This is this repo's second Rust component
(after `native/mom/`), built the same way — a standalone `maturin`-built
`pyo3` extension module reached from the top-level `pyproject.toml` via a
PEP 735 dependency group (`uv sync --extra dev --group congestion`) — but it
exists to be called from an internal pre-check inside `digital_fleet.py`'s
candidate-ranking loop (issue #445), not from a public verb. Nothing here
changes `klt place-and-route`'s request/response contract, per issue #785's
own acceptance criterion.

## What it does

Given a placement (parsed from a DEF's `COMPONENTS`/`NETS`/`PINS` by the
Python layer, `src/klayout_tools/congestion.py`), estimates per-net routing
demand and distributes it across a tile grid, RUDY-style:

- `rsmt.rs` — per-net RSMT-family (rectilinear Steiner minimal tree) length
  estimation.
- `grid.rs` — RUDY-style demand-grid distribution (each net's estimated wire
  length spread across the tiles its bounding box overlaps) plus summary
  statistics.
- `contract.rs` — the request/response JSON shapes; mirrors
  `native/mom/src/contract.rs`'s split between "Rust does the numerically
  hot part" and "Python turns real inputs (a placement DEF) into the
  request shape."

See [`docs/design/flute-congestion-precheck-results.md`](../../docs/design/flute-congestion-precheck-results.md)
for the correlation study this pre-check's own acceptance criteria require
before any caller wires it into a real gating decision.

## Building / testing

```bash
uv sync --extra dev --group congestion
```

or, without `uv`:

```bash
cd native/congestion
maturin develop --release
```

```bash
cd native/congestion
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
```

See `../README.md` and [`docs/cli/yield.md#building-the-native-extension`](../../docs/cli/yield.md#building-the-native-extension)
for the shared rationale (optional Rust toolchain, checkout-only path
dependency, rebuild-after-edit gotcha) that applies to every crate under
`native/`.
