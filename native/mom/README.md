# `klt-mom-native`

The Rust core behind `klt mom` (issue #718, Phase 0/1 of the
Method-of-Moments epic #701) — this repo's first Rust component, and the
convention every later `native/<engine>/` crate follows.

Packaged as a standalone `maturin`-built `pyo3` extension module (its own
`Cargo.toml` + `pyproject.toml`) rather than folding into the top-level
`pyproject.toml`'s `hatchling` build, so every other `klt` command's
packaging stays untouched. `solve_mom_json` (`lib.rs`) is the single entry
point exposed to Python: it accepts and returns JSON strings, so the
Rust/Python boundary stays a plain data contract, matching every `klt` verb
being JSON-contracted end to end ([`docs/json-contract.md`](../../docs/json-contract.md)).
`src/klayout_tools/mom.py` owns GDS/stackup parsing and wraps this crate's
narrower response into the shared `klt` envelope.

## What it does

Three solve methods, all reachable through `klt mom`'s request `method`
field (see [`docs/cli/mom.md`](../../docs/cli/mom.md) for the full CLI/JSON
reference):

- `geometry.rs` — turns each conductor's axis-aligned box(es) into flat
  constant-charge-density panels (quasi-static) or filaments (PEEC), shared
  by every method below.
- `solver.rs` — quasi-static (Laplace) point-collocation Method of Moments:
  fills the potential-coefficient matrix and solves for the Maxwell
  (short-circuit) capacitance matrix, following the classic direct-BEM
  capacitance-extraction procedure (Nabors & White, FastCap, IEEE TCAD
  1991).
- `peec.rs` — PEEC (Partial Element Equivalent Circuit) partial-inductance
  and DC resistance solve (Ruehli 1972's bundle-of-filaments formulation),
  issue #797, Phase 1a.
- `fullwave.rs` — frequency-domain, full-wave partial-impedance solve with a
  retarded free-space Green's function: extends the quasi-static/PEEC cores
  to a genuine frequency sweep, plus derived characteristic impedance and
  propagation constant for the two-conductor transmission-line case, issue
  #893, Phase 2a.
- `contract.rs` — the request/response JSON shapes this crate exposes to
  Python; deliberately narrower than `klt mom`'s own CLI output.

## Building / testing

```bash
uv sync --extra dev --group mom
uv run klt mom plates.gds plates.mom.json
```

or, without `uv`:

```bash
cd native/mom
maturin develop --release
```

```bash
cd native/mom
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
```

Full build/install reference, including the "rebuilding after a Rust
change" cache gotcha and the clean-failure behavior when the extension
isn't built: [`docs/cli/mom.md#building-the-native-extension`](../../docs/cli/mom.md#building-the-native-extension).
