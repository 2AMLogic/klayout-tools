"""Version-pin verification for the functional-verification toolchain
(cocotb, Icarus Verilog, Verilator) -- issue #423, Phase 3 of Epic #391.

`docs/design/cocotb-verification-spike.md`'s worked example (section 6) was
captured against exact tool versions (cocotb 2.0.1, Icarus Verilog 13.0,
Verilator 5.050); this module asserts whatever toolchain is actually on the
host/CI runner still matches the pin recorded in
`scripts/install-icarus-verilog.sh` / `scripts/install-verilator.sh` /
`pyproject.toml`'s `functional-verification` extra, so a silent apt/pip
resolution drift (e.g. a CI image update that starts resolving a newer
default) fails loudly here rather than quietly invalidating the spike's
captured numbers the next time #422's verb runs the worked example for
real.

Every test below is `skipif`-guarded on the relevant tool actually being
present -- **never required for CI to pass on a machine that has not run
the corresponding `scripts/install-*.sh` script or installed the
`functional-verification` extra** (same "skip gracefully, never silently
pass on absence" discipline `tests/test_synthesize.py`'s own real-Yosys
integration test uses for the sibling Yosys pin, issue #417). CI itself
provisions all three (`.github/workflows/ci.yml`'s `test` job) so these
run for real there.

The two source-built tools (Icarus Verilog, Verilator) are held to their
exact pin only where the pin is the contract: on CI (``CI`` set, which
GitHub Actions always does) or when the binary on ``PATH`` is the one the
install script built (under its ``~/.cache/<tool>-<version>`` prefix). A
developer host whose package manager ships a different release -- e.g.
Homebrew's Verilator 5.052 against the 5.050 pin -- is not the provisioned
toolchain, so there a mismatch skips with both versions named instead of
failing every build gate on that host (issue #3072). cocotb stays strict
everywhere: it resolves from ``uv.lock`` into the project venv, so a
mismatch there is real drift.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _pinned_shell_var(script_name: str, var_name: str) -> str:
    """Extract `VAR_NAME="value"` from a scripts/*.sh file -- reads the pin
    directly out of the install script that is the actual source of truth,
    so this test can never silently drift out of sync with a version bump
    made only in the script (the failure mode a hardcoded duplicate literal
    here would risk)."""
    script_path = REPO_ROOT / "scripts" / script_name
    text = script_path.read_text(encoding="utf-8")
    match = re.search(rf'{re.escape(var_name)}="([^"]+)"', text)
    assert match, f"{var_name} not found in {script_path}"
    return match.group(1)


def _pin_is_enforced(binary: str | None, install_prefix: Path) -> bool:
    """Whether a pinned source-built tool must match its pin exactly here:
    on CI, or when the resolved ``binary`` lives under the install script's
    own prefix (so a local pinned install is still checked)."""
    if os.environ.get("CI", "").strip().lower() not in ("", "0", "false", "no"):
        return True
    if binary is None:
        return False
    try:
        Path(binary).resolve().relative_to(install_prefix.expanduser().resolve())
    except ValueError:
        return False
    return True


def _check_pinned_tool(
    *,
    tool: str,
    pinned: str,
    output: str,
    binary: str | None,
    install_prefix: Path,
    install_script: str,
) -> None:
    """Assert ``pinned`` appears in ``output`` where the pin is enforced
    (see :func:`_pin_is_enforced`); elsewhere skip, naming both versions."""
    if pinned in output:
        return
    message = f"expected pinned {tool} {pinned} in its version output, got: {output!r}"
    if _pin_is_enforced(binary, install_prefix):
        pytest.fail(message)
    pytest.skip(
        f"{message} -- this host's {tool} ({binary}) is not the pinned "
        f"install, so the exact pin is enforced only on CI (CI set) or for "
        f"a binary built by scripts/{install_script}; run that script to "
        "check the pin locally"
    )


def _install_prefix(env_var: str, default_name: str) -> Path:
    """The prefix ``scripts/install-*.sh`` installs into -- same default."""
    return Path(os.environ.get(env_var) or Path.home() / ".cache" / default_name)


def _pinned_cocotb_spec() -> str:
    """Extract the cocotb version bound from pyproject.toml's
    `functional-verification` extra (e.g. "2.0.1" from
    `cocotb>=2.0.1,<2.1; ...`) -- the source of truth for the pip/uv-side
    pin, distinct from the two source-build scripts above."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'"cocotb>=([0-9.]+),', pyproject)
    assert match, "cocotb version bound not found in pyproject.toml"
    return match.group(1)


# --------------------------------------------------------------------------- #
# cocotb
# --------------------------------------------------------------------------- #


def test_cocotb_version_matches_pin():
    try:
        import cocotb
    except ImportError:
        pytest.skip(
            "cocotb is not installed -- install the 'functional-verification' "
            "extra (uv sync --extra functional-verification) to run this check"
        )
    pinned = _pinned_cocotb_spec()
    assert cocotb.__version__ == pinned, (
        f"installed cocotb {cocotb.__version__} does not match the pin "
        f"{pinned} declared in pyproject.toml's functional-verification "
        "extra -- update the pin (and re-verify "
        "docs/design/cocotb-verification-spike.md's worked example) if this "
        "drift is intentional"
    )


# --------------------------------------------------------------------------- #
# Icarus Verilog
# --------------------------------------------------------------------------- #

HAVE_IVERILOG = shutil.which("iverilog") is not None


@pytest.mark.skipif(
    not HAVE_IVERILOG,
    reason="iverilog is not installed on this machine -- see "
    "scripts/install-icarus-verilog.sh",
)
def test_icarus_verilog_version_matches_pin():
    pinned = _pinned_shell_var("install-icarus-verilog.sh", "ICARUS_VERSION")
    result = subprocess.run(
        ["iverilog", "-V"], capture_output=True, text=True, check=False
    )
    output = result.stdout + result.stderr
    _check_pinned_tool(
        tool="Icarus Verilog",
        pinned=pinned,
        output=output,
        binary=shutil.which("iverilog"),
        install_prefix=_install_prefix("ICARUS_INSTALL_PREFIX", f"icarus-{pinned}"),
        install_script="install-icarus-verilog.sh",
    )


# --------------------------------------------------------------------------- #
# Verilator
# --------------------------------------------------------------------------- #

HAVE_VERILATOR = shutil.which("verilator") is not None


@pytest.mark.skipif(
    not HAVE_VERILATOR,
    reason="verilator is not installed on this machine -- see "
    "scripts/install-verilator.sh",
)
def test_verilator_version_matches_pin():
    pinned = _pinned_shell_var("install-verilator.sh", "VERILATOR_VERSION")
    result = subprocess.run(
        ["verilator", "--version"], capture_output=True, text=True, check=False
    )
    output = result.stdout + result.stderr
    _check_pinned_tool(
        tool="Verilator",
        pinned=pinned,
        output=output,
        binary=shutil.which("verilator"),
        install_prefix=_install_prefix(
            "VERILATOR_INSTALL_PREFIX", f"verilator-{pinned}"
        ),
        install_script="install-verilator.sh",
    )


# --------------------------------------------------------------------------- #
# Where the exact pin is enforced (issue #3072)
# --------------------------------------------------------------------------- #


def _check(tmp_path: Path, binary: Path, output: str) -> None:
    _check_pinned_tool(
        tool="Verilator",
        pinned="5.050",
        output=output,
        binary=str(binary),
        install_prefix=tmp_path / "verilator-5.050",
        install_script="install-verilator.sh",
    )


def test_pin_mismatch_fails_on_ci(tmp_path, monkeypatch):
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception, match="5.050"):
        _check(tmp_path, tmp_path / "brew" / "verilator", "Verilator 5.052\n")


def test_pin_mismatch_fails_for_the_pinned_install_off_ci(tmp_path, monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    binary = tmp_path / "verilator-5.050" / "bin" / "verilator"
    with pytest.raises(pytest.fail.Exception, match="5.050"):
        _check(tmp_path, binary, "Verilator 5.052\n")


def test_pin_mismatch_skips_for_an_unpinned_host_install_off_ci(tmp_path, monkeypatch):
    monkeypatch.setenv("CI", "false")
    with pytest.raises(pytest.skip.Exception) as excinfo:
        _check(tmp_path, tmp_path / "brew" / "verilator", "Verilator 5.052\n")
    assert "5.050" in str(excinfo.value) and "5.052" in str(excinfo.value)
    assert "install-verilator.sh" in str(excinfo.value)


def test_pin_match_passes_everywhere(tmp_path, monkeypatch):
    monkeypatch.setenv("CI", "true")
    _check(tmp_path, tmp_path / "brew" / "verilator", "Verilator 5.050 2026\n")
