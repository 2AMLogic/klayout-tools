# Releasing prebuilt native wheels

Issue #2531 (operator ruling 2026-10-02): the Rust pyo3 extensions behind
`klt` verbs are published to PyPI as prebuilt wheels, one package per crate,
so a plain `pip install 'klayout-tools[<extra>]'` needs no Rust toolchain.

| Crate | PyPI package | Extra | Status |
|---|---|---|---|
| `native/yield/` | `klt-yield-native` | `klayout-tools[yield]` | enabled |
| `native/mom/`, `native/congestion/`, `native/statime/` | `klt-<crate>-native` | — | not yet (add when asked, see below) |

Platforms: Linux x86_64 (manylinux_2_28) and macOS arm64 (GitHub-hosted
`macos-14` runner). Wheels are `abi3` (`cp310-abi3`), so one wheel per platform
covers CPython 3.10+. Other platforms fall back to the from-source build
(`docs/cli/yield.md#building-the-native-extension`).

## One-time operator setup (not automatable by an agent)

1. Create the PyPI project `klt-yield-native` (public), e.g. by registering a
   *pending* trusted publisher at <https://pypi.org/manage/account/publishing/>.
2. Trusted publisher binding: owner `2AMLogic`, repository `klayout-tools`,
   workflow `publish-native.yml`, environment `pypi` (the same GitHub
   environment `publish.yml` uses).
3. Confirm the org's GitHub plan can run `macos-14` runners.

## Release sequence

Order matters: the `yield` extra pins `klt-yield-native>=0.1.0,<0.2`, so a
klayout-tools release that carries the extra must come **after** the matching
`klt-yield-native` publication; otherwise `pip install 'klayout-tools[yield]'`
fails with "no matching distribution" (a bare install is unaffected).

1. Bump `version` in `native/<crate>/pyproject.toml` (and `Cargo.toml`) when
   the crate changed.
2. Optional dry run: Actions -> "Publish native wheels to PyPI" ->
   *Run workflow* with the crate name. Builds and tests the installed wheel
   on both platforms, does not publish.
3. Push tag `klt-<crate>-native-v<version>` (e.g. `klt-yield-native-v0.1.0`).
   `.github/workflows/publish-native.yml` builds wheels + sdist, installs each
   built wheel into a clean venv next to klayout-tools and runs
   `tests/test_<crate>*.py` against it (skips count as failure), then
   publishes with trusted publishing. The tag must not start with `v`, so it
   never fires `publish.yml`.
4. Then follow `RELEASING.md` for the klayout-tools `v*` tag.

Until the first `klt-yield-native` tag has been published, the `[yield]` extra
resolves to nothing on PyPI and the from-source path remains the only one.

## From-source development is unchanged

`[tool.uv.sources]` still maps `klt-yield-native` to `native/yield`, and the
`yield` dependency group (`uv sync --group yield`, used by CI's `native`
job) still builds the checkout's Rust source. `uv sync --extra yield` in a
checkout likewise builds from the path source, never from PyPI.

## Adding another crate

1. Make `native/<crate>/pyproject.toml` publishable (`readme`, `urls`,
   a description without "not published").
2. Add `<crate>` to `SUPPORTED` in `publish-native.yml`'s `plan` job. If its
   tests need more than `tests/test_<crate>*.py` selects (e.g. external
   binaries), adjust the `test-wheel` step.
3. Add `[project.optional-dependencies] <crate> = ["klt-<crate>-native>=X,<Y"]`
   and refresh `uv.lock`.
4. Operator creates the PyPI project and trusted publisher (above).
