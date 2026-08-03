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
"""

from __future__ import annotations

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
    assert pinned in output, (
        f"expected pinned Icarus Verilog {pinned} in `iverilog -V` output, "
        f"got: {output!r}"
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
    assert pinned in output, (
        f"expected pinned Verilator {pinned} in `verilator --version` "
        f"output, got: {output!r}"
    )
