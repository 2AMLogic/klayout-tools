# `native/`

Rust components. `klayout-tools` is a Python package (`src/klayout_tools/`)
by default — everything under `native/<engine>/` is an optional,
from-source-only add-on for the numerically hot pieces where wrapping an
existing tool (per `docs/ARCHITECTURE.md`'s "Rewrite rule") wasn't the right
call. Building any of them needs a Rust toolchain (`cargo`/`rustc`); no
`klt` verb *requires* one — a plain `pip install klayout-tools` / `uv tool
install klayout-tools` never touches this directory.

Each crate is self-contained: its own `Cargo.toml` (`name = "klt-<engine>-native"`,
`publish = false`), source under `src/`, and — for the crates wired into a
shipped `klt` verb — its own `pyproject.toml` reached from the top-level
`pyproject.toml` via a [PEP 735 dependency group](../pyproject.toml)
(`[tool.uv.sources]` points each group at its `native/<engine>/` path).

| Crate | `name` | Backs | Status |
| --- | --- | --- | --- |
| [`mom/`](mom/) | `klt-mom-native` | `klt mom` (Method-of-Moments capacitance + PEEC inductance/resistance solver) | Shipped `pyo3` extension — this repo's first Rust component |
| [`congestion/`](congestion/) | `klt-congestion-native` | Not a `klt` CLI verb — a RUDY/RSMT-family post-placement congestion pre-check for the fleet DSE loop's own internal candidate-ranking (`digital_fleet.py`) | Research spike, `pyo3` extension, wired via dependency group |
| [`yield/`](yield/) | `klt-yield-native` | `klt yield` / `klt yield-campaign` / `klt yield-sensitivity` (distribution fit, yield estimate + confidence intervals, Cp/Cpk, sample-size verdict) | Shipped `pyo3` extension |
| [`statime/`](statime/) | `klt-statime-native` | `klt synthesize`'s `sta` response field (gate-level static timing, NLDM) | Shipped `pyo3` extension (promoted from a go/no-go spike, issue #925); standalone `klt-statime` CLI binary also builds from the same crate |
| [`techmap/`](techmap/) | `klt-techmap-native` | `klt techmap` (Liberty-driven technology mapping, standalone binary invoked as a subprocess) | Standalone binary + library — not (yet) folded into a `pyo3` extension |
| [`legalize/`](legalize/) | `klt-legalize-native` | Nothing shipped — an Abacus-style standard-cell row legalizer spike | **No-go** verdict (issue #784); kept as the spike's own artifact, not wired into `klt place-and-route`/`klt par` |

## Building

Each crate that backs a shipped verb documents its own build/install
instructions on that verb's CLI reference page — see, e.g.,
[`docs/cli/yield.md#building-the-native-extension`](../docs/cli/yield.md#building-the-native-extension)
and [`docs/cli/mom.md`](../docs/cli/mom.md). The short version, using `uv`'s
dependency groups from a full repo checkout:

```bash
uv sync --extra dev --group mom       # klt mom
uv sync --extra dev --group yield     # klt yield / yield-campaign / yield-sensitivity
uv sync --extra dev --group statime   # klt synthesize's sta field
uv sync --extra dev --group congestion  # the congestion research crate (no klt verb)
```

`techmap/` and `legalize/` have no `pyo3`/dependency-group wiring — build
and run them directly with `cargo` (see
[`docs/cli/techmap.md#building-the-klt-techmap-binary`](../docs/cli/techmap.md#building-the-klt-techmap-binary)
for `techmap/`'s full build pointer, since `klt techmap` shells out to the
compiled binary rather than building it itself):

```bash
cd native/techmap && cargo build --release   # produces the klt-techmap binary
cd native/legalize && cargo test             # go/no-go spike, cargo-only
```

Every crate carries its own `cargo fmt --check` / `cargo clippy --all-targets
-- -D warnings` / `cargo test` gate — see each verb's CLI doc ("Running the
Rust tests") for the exact invocation, and the crate's own `src/lib.rs` (or
`src/main.rs`) doc comment for what it implements and why it's shaped the
way it is.
