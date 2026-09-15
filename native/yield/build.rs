//! Embed a content fingerprint of this crate's Rust sources (issue #1889).
//!
//! `uv sync` revalidates a cached *directory* dependency against the source
//! dir's `pyproject.toml`, never against its Rust sources, so it can reinstall
//! a PREVIOUSLY BUILT `klt-<crate>-native` wheel while this checkout's Rust
//! code says something different -- silently testing a stale extension (a
//! false red when the branch fixes something, a false GREEN when it breaks
//! something). `--reinstall-package` in `.github/workflows/ci.yml` prevents
//! that; this fingerprint is the guard that makes a regression of it loud.
//!
//! The digest is exposed to Python as the extension module's
//! `__source_fingerprint__` (see `src/lib.rs`), and
//! `scripts/native_source_fingerprint.py` recomputes it from a checkout and
//! compares. Content, never mtime: the self-hosted runner reuses its
//! workspace and `git checkout` does not reliably advance a tracked file's
//! mtime, so a timestamp check would be defeated by the same mechanism it is
//! meant to catch.
//!
//! This file is byte-identical across every pyo3 crate in `native/` -- a
//! build script cannot import from outside its own package directory without
//! breaking out-of-tree builds, so the algorithm is duplicated on purpose.
//! `tests/test_native_source_fingerprint.py` compiles these build scripts
//! with `rustc` and asserts they agree with the Python implementation, so the
//! copies cannot drift silently.
//!
//! Algorithm (keep in sync with `scripts/native_source_fingerprint.py`):
//!
//! 1. Inputs are, relative to the crate directory: `Cargo.toml`, `build.rs`
//!    (each when present) and every `*.rs` under `src/` recursively.
//! 2. Sort the relative paths (POSIX separators) lexicographically.
//! 3. For each, feed `<relpath>\n<byte-length>\n<contents>` into FNV-1a-64.
//! 4. Render the digest as 16 lowercase hex digits.

use std::env;
use std::fs;
use std::path::{Path, PathBuf};

const FNV_OFFSET_BASIS: u64 = 0xcbf2_9ce4_8422_2325;
const FNV_PRIME: u64 = 0x0000_0100_0000_01b3;

/// Crate-root files that are part of the build and so part of the
/// fingerprint. `Cargo.lock` is deliberately excluded: cargo may rewrite it
/// during a build, and both implementations must hash a byte-identical input
/// set.
const ROOT_INPUTS: [&str; 2] = ["Cargo.toml", "build.rs"];

fn main() {
    // Cargo runs build scripts with `CARGO_MANIFEST_DIR` set to the package
    // root; the `.` fallback lets the script also be compiled and run
    // standalone with `rustc` from the crate directory (how the parity test
    // exercises it).
    let crate_dir =
        PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap_or_else(|_| ".".to_string()));

    let mut inputs: Vec<String> = ROOT_INPUTS
        .iter()
        .filter(|name| crate_dir.join(name).is_file())
        .map(|name| (*name).to_string())
        .collect();
    collect_rust_sources(&crate_dir.join("src"), "src", &mut inputs);
    inputs.sort();

    let mut digest = FNV_OFFSET_BASIS;
    for rel in &inputs {
        let contents = fs::read(crate_dir.join(rel))
            .unwrap_or_else(|err| panic!("failed to read fingerprint input {rel}: {err}"));
        let header = format!("{}\n{}\n", rel, contents.len());
        for byte in header.as_bytes().iter().chain(contents.iter()) {
            digest = (digest ^ u64::from(*byte)).wrapping_mul(FNV_PRIME);
        }
        println!("cargo:rerun-if-changed={rel}");
    }
    // Recurse on the directory too, so an added/removed source file (which
    // changes no existing file) still re-runs this script.
    println!("cargo:rerun-if-changed=src");
    println!("cargo:rustc-env=KLT_NATIVE_SOURCE_FINGERPRINT={digest:016x}");
}

/// Append every `*.rs` under `dir` to `out` as crate-relative POSIX paths.
fn collect_rust_sources(dir: &Path, rel_prefix: &str, out: &mut Vec<String>) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().into_owned();
        let rel = format!("{rel_prefix}/{name}");
        let path = entry.path();
        if path.is_dir() {
            collect_rust_sources(&path, &rel, out);
        } else if name.ends_with(".rs") {
            out.push(rel);
        }
    }
}
