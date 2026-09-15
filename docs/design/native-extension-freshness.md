# Native-extension freshness in CI

**Status**: implemented (issue #1889; follows #1885/#1879 and #1892)

How this repo guarantees that a native-engine job's Python tier tests the
extension built from *that job's* checkout, and the recorded decision about
the self-hosted runners' persistent uv cache.

## The failure mode

Every pyo3 crate under `native/` (`mom`, `congestion`, `yield`, `statime`) is
a **non-editable directory dependency** resolved through the root
`pyproject.toml`'s `[tool.uv.sources]`, pinned at `0.1.0` and never
version-bumped:

```toml
[tool.uv.sources]
klt-mom-native = { path = "native/mom" }
```

uv revalidates a cached build of a directory dependency against the source
directory's **`pyproject.toml`**, not against its Rust sources. Those
`native/<crate>/pyproject.toml` files are usually byte-identical across
branches, and neither `setup-uv`'s default `cache-dependency-glob` nor the
self-hosted pool's host-persistent `UV_CACHE_DIR=/var/cache/uv` keys on
`native/**/*.rs` or `native/**/Cargo.*`. So a Rust-only change can produce:

```
Prepared 1 package in 527ms
Installed 15 packages in 1.70s
 + klt-mom-native==0.1.0 (from file:///…/native/mom)
```

— an *install*, not a maturin compile. The Rust tier (`cargo test`) is always
correct; the Python tier that follows it silently exercised the **previous**
extension.

Observed live on PR #1869 (run 34970422213, job 104385082636): `cargo test`
passed 53 tests including the four the PR added, then the Python tier failed
raising an error string the PR had already deleted.

This fails **both** ways, and the second is the dangerous one:

| Direction | Effect |
|---|---|
| Fail closed | A branch that *fixes* a native crate shows red against the old wheel. Annoying, visible. |
| Fail **open** | The cache normally holds `main`'s wheel, so a branch that *breaks* a native crate's runtime behaviour goes **green** against the old, still-working extension. Invisible. |

## Layer 1 — force the rebuild (`--reinstall-package`)

Every `uv sync` that installs a native extension names that one package:

```yaml
uv sync --locked --extra dev --group ${{ matrix.group }} \
  --reinstall-package klt-${{ matrix.crate }}-native
```

`--reinstall-package` implies `--refresh-package`, so the cached build is
invalidated and maturin recompiles from the checkout. It is scoped to the
single local path package, so every other wheel still comes from the cache
and the step stays fast.

Landed for the `mom`/`congestion`/`yield` matrix in #1879 (issue #1885) and
for `native-statime` in #1889 (issue #1892).

## Layer 2 — prove the rebuild happened (freshness gate)

A flag is only as good as the next edit that forgets it — `native-statime`
had the identical job structure and went four months without the flag. So the
freshness of the installed extension is now **asserted**, on every leg, before
its tests run:

```yaml
uv run --frozen … python scripts/native_source_fingerprint.py native/mom \
  --assert-module klt_mom_native \
  --import-error-message "…"
```

- Each crate's `build.rs` computes an **FNV-1a-64 content fingerprint** of its
  own sources (`Cargo.toml`, `build.rs`, and every `src/**/*.rs`) at compile
  time and emits it as `KLT_NATIVE_SOURCE_FINGERPRINT`; the `#[pymodule]`
  exposes it as `__source_fingerprint__`. The extension therefore *carries the
  identity of the sources it was actually built from*.
- `scripts/native_source_fingerprint.py` recomputes the same digest from the
  checkout and compares. A mismatch — or a module with no
  `__source_fingerprint__` at all, i.e. one built before this guard existed —
  fails the job with a `::error::` annotation naming both digests.
- The same step subsumes the old "no silent skip" import assert: an extension
  that did not install at all fails here with the leg's own message, so there
  is exactly one importability gate per leg and it now runs *before* pytest
  rather than after.

### Why content, not mtime

The obvious cheap guard — "the installed `.so` is newer than the newest
`native/<crate>/src/*.rs`" — is defeated by the very mechanism it is meant to
catch. These jobs run on a self-hosted pool that **reuses its workspace**, and
`git checkout` does not reliably advance a tracked file's mtime there: that is
part of *why* a stale wheel survives across branches in the first place. A
content hash is immune to it.

FNV-1a-64 is chosen because it is ~10 lines in both Rust and Python with no
dependencies. This is a build-provenance check against an accidental cache
hit, not a security boundary, so collision resistance is not a requirement.

### Why the algorithm is duplicated

A cargo build script cannot import code from outside its own package
directory without breaking out-of-tree builds, so `native/*/build.rs` are
byte-identical copies rather than a shared module.
`tests/test_native_source_fingerprint.py` defends that duplication: it asserts
the four copies are byte-identical, and (when `rustc` is available) compiles
`build.rs` standalone and asserts its digest equals the Python
implementation's for every crate. The two implementations cannot drift
silently.

## Decision — the host-persistent `UV_CACHE_DIR` stays (issue #1889, item 3)

The self-hosted `heavy` pool sets `UV_CACHE_DIR=/var/cache/uv` at host level,
shared across every job and branch on that runner.

**Decision: keep it, unscoped, and mitigate at the consumer instead** —
`--reinstall-package` on each native leg (layer 1) plus the freshness gate
(layer 2).

Rationale:

- The cache's value is concentrated in *ordinary* PyPI wheels (numpy, scipy,
  klayout, the dev toolchain): large, immutable, correctly keyed by version,
  and re-downloaded on every job without it. Per-branch or per-run scoping
  would give that up for every job on the pool to fix a problem that only
  affects four path dependencies.
- The defect is specific to `[tool.uv.sources]` **directory** dependencies,
  whose revalidation key (`pyproject.toml`) does not describe their real
  build inputs. Excluding exactly those from cache reuse is what
  `--reinstall-package` already does, precisely and per-invocation.
- Host-level cache configuration lives in runner provisioning, outside this
  repo; a fix that lives in `ci.yml` is reviewable in the same PR as the code
  it protects and travels to GitHub-hosted runners (fork PRs) unchanged.
- The residual risk of the flag being forgotten on a future native leg — the
  exact way #1892 happened — is what layer 2 covers: a new leg without the
  flag fails loudly on its first Rust-only change instead of going quietly
  green.

Revisit if a *fifth* mechanism for staleness appears that the fingerprint
cannot see (e.g. a cached wheel whose Rust sources are identical but whose
linked system libraries differ), or if the pool's cache is ever shared across
machines with differing ABIs.
