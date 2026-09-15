#!/usr/bin/env python3
"""Content fingerprint of a native crate's Rust sources (issue #1889).

CI's native-engine jobs install each pyo3 extension (`klt_mom_native`,
`klt_congestion_native`, `klt_yield_native`, `klt_statime_native`) with
`uv sync`, then run that crate's Python tier against it. uv revalidates a
cached *directory* dependency against the source dir's `pyproject.toml`, not
against its Rust sources -- and `native/<crate>/pyproject.toml` is usually
byte-identical across branches -- so `uv sync` can silently reinstall the
PREVIOUSLY BUILT wheel and the Python tier then validates a **stale**
extension. That fails closed (a correct branch shows red) but, far worse,
also fails **open**: a branch that genuinely breaks a native crate can go
green against the older, still-working extension.

`--reinstall-package klt-<crate>-native` in the workflow is the fix; this
script is the *guard* that makes a regression of that fix loud instead of
invisible. Each crate's `build.rs` computes the fingerprint below at compile
time and its `#[pymodule]` exposes it as `__source_fingerprint__`, so an
extension carries the identity of the sources it was actually built from.
Recomputing it here from the checkout and comparing is a **content**
comparison -- deliberately not an mtime comparison, because the self-hosted
runner reuses its workspace and `git checkout` does not reliably advance a
tracked file's mtime (the very mechanism that produces the stale wheel would
also defeat a timestamp check).

Usage::

    # print the fingerprint of a crate's current sources
    python scripts/native_source_fingerprint.py native/mom

    # assert the installed extension was built from those sources
    python scripts/native_source_fingerprint.py native/mom \
        --assert-module klt_mom_native

The algorithm is duplicated in each crate's `build.rs` (a build script cannot
import from outside its own package directory without breaking out-of-tree
builds). `tests/test_native_source_fingerprint.py` compiles those build
scripts with `rustc` and asserts both implementations agree, so the
duplication cannot drift silently.

Algorithm (keep both implementations in sync):

1. Inputs are, relative to the crate directory: ``Cargo.toml``, ``build.rs``
   (each when present) and every ``*.rs`` under ``src/`` recursively.
2. Sort the relative paths (POSIX separators) lexicographically.
3. For each, feed ``<relpath>\\n<byte-length>\\n<contents>`` into FNV-1a-64.
4. Render the digest as 16 lowercase hex digits.

FNV-1a-64 is chosen because it is ~10 lines in both Rust and Python with no
dependencies; this is a build-provenance check against an accidental cache
hit, not a security boundary.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

# The attribute each `#[pymodule]` exposes (see native/*/src/lib.rs).
FINGERPRINT_ATTR = "__source_fingerprint__"

_FNV_OFFSET_BASIS = 0xCBF29CE484222325
_FNV_PRIME = 0x100000001B3
_MASK64 = (1 << 64) - 1

# Crate-root files that are part of the build and therefore part of the
# fingerprint. Cargo.lock is deliberately excluded: cargo may rewrite it
# during a build, and the two implementations must agree on a byte-identical
# input set.
_ROOT_INPUTS = ("Cargo.toml", "build.rs")


def fingerprint_inputs(crate_dir: Path) -> list[Path]:
    """Return the crate-relative paths that feed the fingerprint, sorted."""
    rels: list[Path] = [
        Path(name) for name in _ROOT_INPUTS if (crate_dir / name).is_file()
    ]
    src_dir = crate_dir / "src"
    if src_dir.is_dir():
        rels.extend(
            path.relative_to(crate_dir)
            for path in src_dir.rglob("*.rs")
            if path.is_file()
        )
    return sorted(rels, key=lambda rel: rel.as_posix())


def fingerprint(crate_dir: Path) -> str:
    """Compute the crate's source fingerprint as 16 lowercase hex digits."""
    digest = _FNV_OFFSET_BASIS
    for rel in fingerprint_inputs(crate_dir):
        contents = (crate_dir / rel).read_bytes()
        header = f"{rel.as_posix()}\n{len(contents)}\n".encode()
        for byte in header + contents:
            digest = ((digest ^ byte) * _FNV_PRIME) & _MASK64
    return f"{digest:016x}"


def _fail(summary: str, *detail: str) -> int:
    """Emit a GitHub Actions error annotation plus human-readable detail."""
    print(f"::error::{summary}")
    print(summary, file=sys.stderr)
    for line in detail:
        print(line, file=sys.stderr)
    return 1


def _assert_module(module_name: str, crate_dir: Path, import_error_message: str) -> int:
    expected = fingerprint(crate_dir)
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        return _fail(
            import_error_message
            or f"{module_name} did not install -- its Python tier would have skipped",
            f"import {module_name} failed: {exc}",
        )

    actual = getattr(module, FINGERPRINT_ATTR, None)
    hint = (
        f"Rebuild it from this checkout with: uv sync --locked "
        f"--reinstall-package {_dist_name(module_name)}"
    )
    if actual is None:
        return _fail(
            f"{module_name} is STALE: it exposes no {FINGERPRINT_ATTR}, so it was "
            f"built before this guard existed and cannot be from this checkout",
            f"module file: {getattr(module, '__file__', '<unknown>')}",
            f"expected {FINGERPRINT_ATTR}: {expected} (from {crate_dir})",
            hint,
        )
    if actual != expected:
        return _fail(
            f"{module_name} is STALE: built from {actual}, but {crate_dir} hashes "
            f"to {expected} -- the Python tier would have tested a cached, "
            f"out-of-date extension",
            f"module file: {getattr(module, '__file__', '<unknown>')}",
            "fingerprint inputs: "
            + ", ".join(rel.as_posix() for rel in fingerprint_inputs(crate_dir)),
            hint,
        )

    print(
        f"OK: {module_name} was built from this checkout "
        f"({FINGERPRINT_ATTR}={actual}, sources={crate_dir})"
    )
    return 0


def _dist_name(module_name: str) -> str:
    """`klt_mom_native` -> `klt-mom-native` (the uv/PyPI distribution name)."""
    return module_name.replace("_", "-")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "crate_dir",
        type=Path,
        help="path to the native crate directory (e.g. native/mom)",
    )
    parser.add_argument(
        "--assert-module",
        metavar="MODULE",
        help=(
            "import MODULE and fail unless its "
            f"{FINGERPRINT_ATTR} matches the crate's current sources"
        ),
    )
    parser.add_argument(
        "--import-error-message",
        default="",
        metavar="TEXT",
        help="error text to emit when --assert-module cannot be imported",
    )
    args = parser.parse_args(argv)

    crate_dir = args.crate_dir
    if not (crate_dir / "Cargo.toml").is_file():
        return _fail(f"{crate_dir} is not a Rust crate directory (no Cargo.toml)")

    if args.assert_module:
        return _assert_module(args.assert_module, crate_dir, args.import_error_message)

    print(fingerprint(crate_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
