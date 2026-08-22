"""Version-pin verification for the sequential-equivalence toolchain
(SymbiYosys/`sby`, Bitwuzla) -- issue #1312, Phase 2 of Epic #707.

`docs/design/sequential-equivalence-survey.md` section 4.1 identifies `sby`
plus one pinned SAT/SMT backend as the prerequisite for every later Phase 2
sequential-equivalence item; `scripts/install-symbiyosys.sh` installs both,
pinned and checksum-verified. This module asserts whatever `sby`/`bitwuzla`
is actually on the host/CI runner matches the pin recorded in that script,
so a silent drift (e.g. a re-run against a stale cache, or a manually
installed `sby`/`bitwuzla` shadowing the pinned one on `$PATH`) fails
loudly here instead of quietly invalidating this survey's own findings the
next time the register-correspondence engine (the survey's section 4.2)
runs against it for real.

Every test below is `skipif`-guarded on the relevant tool actually being
present -- **never required for CI to pass on a machine that has not run
`scripts/install-symbiyosys.sh`** (same "skip gracefully, never silently
pass on absence" discipline `tests/test_functional_verification_toolchain_
pins.py` already uses for the sibling Icarus Verilog/Verilator pins, issue
#423). CI itself provisions both (`.github/workflows/ci.yml`'s `test` job)
so these run for real there.
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


# --------------------------------------------------------------------------- #
# SymbiYosys (sby)
# --------------------------------------------------------------------------- #

HAVE_SBY = shutil.which("sby") is not None


@pytest.mark.skipif(
    not HAVE_SBY,
    reason="sby is not installed on this machine -- see scripts/install-symbiyosys.sh",
)
def test_sby_version_matches_pin():
    pinned = _pinned_shell_var("install-symbiyosys.sh", "SBY_VERSION")
    result = subprocess.run(
        ["sby", "--version"], capture_output=True, text=True, check=False
    )
    output = result.stdout + result.stderr
    assert pinned in output, (
        f"expected pinned SymbiYosys {pinned} in `sby --version` output, "
        f"got: {output!r}"
    )


# --------------------------------------------------------------------------- #
# Bitwuzla
# --------------------------------------------------------------------------- #

HAVE_BITWUZLA = shutil.which("bitwuzla") is not None


@pytest.mark.skipif(
    not HAVE_BITWUZLA,
    reason="bitwuzla is not installed on this machine -- see "
    "scripts/install-symbiyosys.sh",
)
def test_bitwuzla_version_matches_pin():
    pinned = _pinned_shell_var("install-symbiyosys.sh", "BITWUZLA_VERSION")
    result = subprocess.run(
        ["bitwuzla", "--version"], capture_output=True, text=True, check=False
    )
    output = result.stdout + result.stderr
    assert pinned in output, (
        f"expected pinned Bitwuzla {pinned} in `bitwuzla --version` output, "
        f"got: {output!r}"
    )
