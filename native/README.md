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
| [`wave/`](wave/) | `klt-wave-native` | `klt wave build` / `klt wave query` (VCD/FST waveform store build + query; standalone `klt-wave` binary invoked as a subprocess) | Standalone binary crate — no `pyo3`/dependency-group wiring (issues #1599/#1600) |
| [`legalize/`](legalize/) | `klt-legalize-native` | Nothing shipped — an Abacus-style standard-cell row legalizer spike | **No-go** verdict (issue #784); kept as the spike's own artifact, not wired into `klt place-and-route`/`klt par` |
| [`nldm-interp/`](nldm-interp/) | `klt-nldm-interp` | Not a `klt` CLI verb — shared liberty NLDM bilinear-interpolation core, consumed as a path dependency by `statime/` and `techmap/` | Internal library crate (issue #2272, extracted from a near-verbatim fork between `statime/` and `techmap/`); no `pyo3`, no dependency-group wiring |

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

`techmap/`, `wave/`, and `legalize/` have no `pyo3`/dependency-group wiring —
build and run them directly with `cargo` (see
[`docs/cli/techmap.md#building-the-klt-techmap-binary`](../docs/cli/techmap.md#building-the-klt-techmap-binary)
for `techmap/`'s full build pointer, since `klt techmap` shells out to the
compiled binary rather than building it itself):

```bash
cd native/techmap && cargo build --release   # produces the klt-techmap binary
cd native/wave && cargo build --release      # produces the klt-wave binary (see docs/cli/wave.md)
cd native/legalize && cargo test             # go/no-go spike, cargo-only
```

Every crate carries its own `cargo fmt --check` / `cargo clippy --all-targets
-- -D warnings` / `cargo test` gate — see each verb's CLI doc ("Running the
Rust tests") for the exact invocation, and the crate's own `src/lib.rs` (or
`src/main.rs`) doc comment for what it implements and why it's shaped the
way it is.

### Release profile: why every crate sets `debug = 1`

Each crate's `[profile.release]` is `lto = true` **plus `debug = 1`**, and the
`debug` line is a correctness fix, not a debugging convenience. Left at
cargo's default (`debug = false`), the release profile implicitly adds
`-C strip=debuginfo`; on macOS with Apple `ld` `PROJECT:ld-27037.1` that strip
leaves a `LINKEDIT` string pool at an offset `dyld` rejects, so
`maturin develop --release` builds and installs an extension that then cannot
be imported at all:

```
ImportError: dlopen(.../klt_mom_native.cpython-312-darwin.so, 0x0002):
  mis-aligned LINKEDIT string pool, fileOffset=0x0007D524
```

Any nonzero `debug` suppresses the implicit strip; `1` (line tables only) is
the cheapest value that does. `lto` is **not** the trigger — issue #2261
bisected it, and `native/mom/Cargo.toml` carries the full write-up. Keep the
override when editing any of these profiles, and add it to a new crate's
`[profile.release]` too.

The cost is local-build-artifact size and nothing else — every crate here is
`publish = false`, built only from a checkout, and never published to PyPI.
Measured on Linux (x86_64, cargo 1.97.1) for `mom/`:
`target/release/libklt_mom_native.so` grows 0.79 MiB → 7.67 MiB and the
maturin-installed extension module 0.64 MiB → 3.44 MiB, with an incremental
release rebuild going from ~4.3 s to ~4.8 s.

`nldm-interp/` has no `[profile.release]` of its own on purpose: it is a path
dependency compiled under whichever parent crate is being built.
