"""Tests for the native-extension freshness guard (issue #1889).

CI's native-engine jobs install each pyo3 extension with `uv sync` and then
run that crate's Python tier against it. uv revalidates a cached *directory*
dependency against the source dir's `pyproject.toml`, never against its Rust
sources, so it can silently reinstall a previously built wheel and the Python
tier then validates a **stale** extension -- a false red when the branch fixes
something, and a silent false **green** when the branch breaks something (the
cache normally holds main's wheel). See
`docs/design/native-extension-freshness.md`.

The guard is a content fingerprint: `native/<crate>/build.rs` hashes the
crate's Rust sources at compile time and the `#[pymodule]` exposes the digest
as `__source_fingerprint__`; `scripts/native_source_fingerprint.py`
recomputes it from the checkout and fails loudly on a mismatch.

Three things are asserted here:

1. **Behaviour of the checker** -- it passes on a correctly rebuilt
   extension and fails loudly (nonzero + a `::error::` annotation) on a
   stale one, on one with no fingerprint at all, and on one that did not
   install.
2. **Cross-language parity** -- the Rust implementation in `build.rs` (which
   cannot import the Python one without breaking out-of-tree builds) is
   compiled standalone with `rustc` and must produce exactly the digest the
   Python implementation computes, for every crate.
3. **Workflow wiring** -- every pyo3 leg in `.github/workflows/ci.yml` both
   forces the rebuild (`--reinstall-package`) and runs the freshness gate, so
   a future leg that reintroduces the #1885/#1892 gap fails here rather than
   silently re-exposing CI to a stale extension.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "native_source_fingerprint.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
NATIVE_DIR = REPO_ROOT / "native"

# Every crate under native/ that ships a pyo3 extension module (the crates
# with a pyproject.toml are exactly those; native/legalize, native/techmap
# and native/wave are plain cargo binaries with no Python tier).
PYO3_CRATES = {
    "mom": "klt_mom_native",
    "congestion": "klt_congestion_native",
    "yield": "klt_yield_native",
    "statime": "klt_statime_native",
}


def _load_script_module():
    spec = importlib.util.spec_from_file_location("native_source_fingerprint", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fingerprint_module = _load_script_module()


def _write_crate(root: Path, lib_body: str = "pub fn answer() -> u32 { 42 }") -> Path:
    crate = root / "demo"
    (crate / "src" / "nested").mkdir(parents=True)
    (crate / "Cargo.toml").write_text('[package]\nname = "demo"\n')
    (crate / "src" / "lib.rs").write_text(lib_body + "\n")
    (crate / "src" / "nested" / "helper.rs").write_text("pub fn helper() {}\n")
    return crate


def _run_checker(crate_dir: Path, *args: str, pythonpath: Path | None = None):
    env = None
    if pythonpath is not None:
        import os

        env = dict(os.environ)
        env["PYTHONPATH"] = str(pythonpath)
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(crate_dir), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
    )


# --------------------------------------------------------------------------
# The fingerprint itself
# --------------------------------------------------------------------------


def test_fingerprint_is_deterministic(tmp_path: Path) -> None:
    crate = _write_crate(tmp_path)
    first = fingerprint_module.fingerprint(crate)
    assert first == fingerprint_module.fingerprint(crate)


def test_fingerprint_changes_when_a_rust_source_changes(tmp_path: Path) -> None:
    crate = _write_crate(tmp_path)
    before = fingerprint_module.fingerprint(crate)
    (crate / "src" / "lib.rs").write_text("pub fn answer() -> u32 { 43 }\n")
    assert fingerprint_module.fingerprint(crate) != before


def test_fingerprint_changes_when_a_nested_source_is_added(tmp_path: Path) -> None:
    crate = _write_crate(tmp_path)
    before = fingerprint_module.fingerprint(crate)
    (crate / "src" / "nested" / "extra.rs").write_text("pub fn extra() {}\n")
    assert fingerprint_module.fingerprint(crate) != before


def test_fingerprint_ignores_build_artifacts_and_non_rust_files(tmp_path: Path) -> None:
    """Cargo.lock/target/README churn must not look like a source change.

    Cargo may rewrite Cargo.lock during a build, and `target/` holds the
    build output itself -- hashing either would make the digest depend on
    something the two implementations cannot observe identically.
    """
    crate = _write_crate(tmp_path)
    before = fingerprint_module.fingerprint(crate)
    (crate / "Cargo.lock").write_text("# lockfile\n")
    (crate / "README.md").write_text("docs\n")
    (crate / "target").mkdir()
    (crate / "target" / "generated.rs").write_text("pub fn generated() {}\n")
    assert fingerprint_module.fingerprint(crate) == before


def test_fingerprint_is_path_sensitive(tmp_path: Path) -> None:
    """Renaming a file changes the digest even when contents do not."""
    crate = _write_crate(tmp_path)
    before = fingerprint_module.fingerprint(crate)
    (crate / "src" / "nested" / "helper.rs").rename(
        crate / "src" / "nested" / "renamed.rs"
    )
    assert fingerprint_module.fingerprint(crate) != before


# --------------------------------------------------------------------------
# The checker's behaviour (the CI gate)
# --------------------------------------------------------------------------


def _install_fake_extension(tmp_path: Path, name: str, fingerprint: str | None) -> Path:
    """Write an importable stand-in for a built extension module."""
    site = tmp_path / "site"
    site.mkdir(exist_ok=True)
    body = "" if fingerprint is None else f'__source_fingerprint__ = "{fingerprint}"\n'
    (site / f"{name}.py").write_text(body)
    return site


def test_checker_prints_the_fingerprint_without_assert_module(tmp_path: Path) -> None:
    crate = _write_crate(tmp_path)
    result = _run_checker(crate)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == fingerprint_module.fingerprint(crate)


def test_checker_passes_on_a_freshly_built_extension(tmp_path: Path) -> None:
    """The gate must not false-positive on a correctly rebuilt extension."""
    crate = _write_crate(tmp_path)
    site = _install_fake_extension(
        tmp_path, "demo_native", fingerprint_module.fingerprint(crate)
    )
    result = _run_checker(crate, "--assert-module", "demo_native", pythonpath=site)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::error::" not in result.stdout


def test_checker_fails_loudly_on_a_stale_extension(tmp_path: Path) -> None:
    crate = _write_crate(tmp_path)
    site = _install_fake_extension(tmp_path, "demo_native", "0000000000000000")
    result = _run_checker(crate, "--assert-module", "demo_native", pythonpath=site)
    assert result.returncode == 1
    assert "::error::" in result.stdout
    assert "STALE" in result.stdout
    # The message has to be actionable: both digests and the fix.
    combined = result.stdout + result.stderr
    assert "0000000000000000" in combined
    assert fingerprint_module.fingerprint(crate) in combined
    assert "--reinstall-package demo-native" in combined


def test_checker_fails_when_the_extension_predates_the_guard(tmp_path: Path) -> None:
    crate = _write_crate(tmp_path)
    site = _install_fake_extension(tmp_path, "demo_native", None)
    result = _run_checker(crate, "--assert-module", "demo_native", pythonpath=site)
    assert result.returncode == 1
    assert "::error::" in result.stdout
    assert "__source_fingerprint__" in result.stdout


def test_checker_fails_when_the_extension_did_not_install(tmp_path: Path) -> None:
    """The folded-in "no silent skip" gate keeps its per-leg message."""
    crate = _write_crate(tmp_path)
    result = _run_checker(
        crate,
        "--assert-module",
        "definitely_not_installed_native",
        "--import-error-message",
        "demo did not install -- tests/test_demo.py would have skipped",
        pythonpath=tmp_path / "empty",
    )
    assert result.returncode == 1
    assert "::error::demo did not install" in result.stdout


def test_checker_rejects_a_directory_that_is_not_a_crate(tmp_path: Path) -> None:
    result = _run_checker(tmp_path)
    assert result.returncode == 1
    assert "Cargo.toml" in result.stdout


# --------------------------------------------------------------------------
# Rust/Python parity
# --------------------------------------------------------------------------


def test_every_pyo3_crate_has_a_build_script_and_exposes_the_fingerprint() -> None:
    for crate, module_name in PYO3_CRATES.items():
        build_rs = NATIVE_DIR / crate / "build.rs"
        assert build_rs.is_file(), f"{crate} has no build.rs"
        assert "KLT_NATIVE_SOURCE_FINGERPRINT" in build_rs.read_text()
        lib_rs = (NATIVE_DIR / crate / "src" / "lib.rs").read_text()
        assert "__source_fingerprint__" in lib_rs, module_name
        assert 'env!("KLT_NATIVE_SOURCE_FINGERPRINT")' in lib_rs, module_name


def test_build_scripts_are_byte_identical_across_crates() -> None:
    """The duplication is deliberate; silent drift between copies is not."""
    contents = {
        crate: (NATIVE_DIR / crate / "build.rs").read_bytes() for crate in PYO3_CRATES
    }
    reference = contents["mom"]
    for crate, blob in contents.items():
        assert blob == reference, f"native/{crate}/build.rs drifted from native/mom's"


def _build_script_digest(rustc: str, crate_dir: Path, tmp_path: Path) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    binary = tmp_path / "build_script"
    subprocess.run(
        [rustc, "--edition", "2021", str(crate_dir / "build.rs"), "-o", str(binary)],
        check=True,
        capture_output=True,
    )
    # The build script emits `cargo:` directives on stdout; it reads the crate
    # from CARGO_MANIFEST_DIR, falling back to the working directory.
    result = subprocess.run(
        [str(binary)], cwd=crate_dir, check=True, capture_output=True, text=True
    )
    prefix = "cargo:rustc-env=KLT_NATIVE_SOURCE_FINGERPRINT="
    lines = [line for line in result.stdout.splitlines() if line.startswith(prefix)]
    assert len(lines) == 1, result.stdout
    return lines[0][len(prefix) :]


@pytest.mark.skipif(shutil.which("rustc") is None, reason="rustc not available")
def test_rust_build_script_agrees_with_the_python_implementation(
    tmp_path: Path,
) -> None:
    rustc = shutil.which("rustc")
    assert rustc is not None
    for crate in PYO3_CRATES:
        crate_dir = NATIVE_DIR / crate
        rust_digest = _build_script_digest(rustc, crate_dir, tmp_path / crate)
        assert rust_digest == fingerprint_module.fingerprint(crate_dir), (
            f"native/{crate}: build.rs and scripts/native_source_fingerprint.py "
            "disagree -- the freshness gate would fail on every run"
        )


@pytest.mark.skipif(shutil.which("rustc") is None, reason="rustc not available")
def test_rust_build_script_sees_a_source_edit(tmp_path: Path) -> None:
    """Parity is not an accident of both sides hashing nothing."""
    rustc = shutil.which("rustc")
    assert rustc is not None
    crate = tmp_path / "demo"
    crate.mkdir()
    shutil.copy(NATIVE_DIR / "mom" / "build.rs", crate / "build.rs")
    (crate / "Cargo.toml").write_text('[package]\nname = "demo"\n')
    (crate / "src").mkdir()
    (crate / "src" / "lib.rs").write_text("pub fn answer() -> u32 { 42 }\n")

    before = _build_script_digest(rustc, crate, tmp_path / "out-before")
    assert before == fingerprint_module.fingerprint(crate)

    (crate / "src" / "lib.rs").write_text("pub fn answer() -> u32 { 43 }\n")
    after = _build_script_digest(rustc, crate, tmp_path / "out-after")
    assert after != before
    assert after == fingerprint_module.fingerprint(crate)


# --------------------------------------------------------------------------
# Workflow wiring
# --------------------------------------------------------------------------


def test_every_pyo3_leg_forces_a_rebuild_and_gates_on_freshness() -> None:
    workflow = WORKFLOW.read_text()

    # The shared native matrix installs klt-<crate>-native for the mom /
    # congestion / yield legs via matrix interpolation (#1885); the statime
    # job names its package directly (#1892).
    assert "--reinstall-package klt-${{ matrix.crate }}-native" in workflow
    assert "--reinstall-package klt-statime-native" in workflow

    # Freshness gate (#1889): once for the matrix, once for statime.
    gate = "scripts/native_source_fingerprint.py"
    assert workflow.count(gate) == 2, textwrap.dedent(
        """\
        Every job that installs a pyo3 extension must run the freshness gate
        before its Python tier -- see docs/design/native-extension-freshness.md.
        """
    )
    assert "--assert-module ${{ matrix.extension_module }}" in workflow
    assert "--assert-module klt_statime_native" in workflow


def test_no_native_leg_imports_the_extension_as_its_only_gate() -> None:
    """The bare `python -c "import <ext>"` gate is subsumed by the checker.

    A bare import proves the extension is *present*, never that it is the one
    this checkout builds -- which is exactly how a cached wheel slipped past
    CI on PR #1869. Re-adding one for a pyo3 crate would quietly weaken the
    gate back to that.
    """
    workflow = WORKFLOW.read_text()
    for module_name in PYO3_CRATES.values():
        assert f'python -c "import {module_name}"' not in workflow, module_name
