"""Shared tool-availability probes for the real-binary test tiers.

The single canonical home for every ``shutil.which``-based availability
gate the suite uses (issue #5699). Before this module existed, the ngspice
gate alone was copy-pasted into 40+ test modules in three divergent forms,
and 14 of those copies silently ignored ``KLT_SKIP_NGSPICE_TESTS`` -- the
exact recurrence #2100 and #2329 had each closed one module at a time.
Import the probe and its ready-made mark instead of re-probing:

    from helpers.tool_probes import SKIP_NO_NGSPICE as _SKIP_NO_NGSPICE

Bind the mark under the module's established local alias (``_SKIP_NO_NGSPICE``,
``real_ngspice``, ``requires_ngspice``) so decorator sites never move.

The probes evaluate at import time on purpose: they run before any fixture
can monkeypatch ``shutil.which`` (see ``bare_name_ngspice_resolution`` in
``tests/conftest.py``), so mocked-engine tests stay hermetic while the
real-binary gates keep probing the true ``$PATH``.
"""

import os
import re
import shutil
import subprocess
from functools import cache

import pytest

#: Real `ngspice` invocations (sky130 vendor-deck simulations) are slow and
#: host-load-sensitive -- the same wall-clock cost is fine in CI's dedicated,
#: uncontended runner (~3.5min for the full suite, see issue #1651) but can
#: blow well past the local buildGate's timeout on a shared/busy host running
#: several concurrent builders. `KLT_SKIP_NGSPICE_TESTS=1` (set by
#: `npm run check:ci`'s local-only test script, never by CI) lets a host with
#: ngspice installed still skip this tier locally, mirroring what already
#: happens for free on a host with no ngspice at all.
NGSPICE_BINARY = shutil.which("ngspice")
NGSPICE_ON_PATH = NGSPICE_BINARY is not None
NGSPICE_TESTS_SKIPPED = os.environ.get("KLT_SKIP_NGSPICE_TESTS") == "1"
HAVE_NGSPICE = NGSPICE_ON_PATH and not NGSPICE_TESTS_SKIPPED

#: Ready-made gate for a real-ngspice test: skips both on a host without the
#: binary and under the local-only `KLT_SKIP_NGSPICE_TESTS=1` opt-out.
SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE, reason="ngspice is not installed on this machine"
)


def read_ngspice_version(binary: str | None) -> str | None:
    """Read the full version string from an ngspice binary's own banner."""
    if binary is None:
        return None
    try:
        completed = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"ngspice-([\w.]+)", completed.stdout or "")
    return match.group(1) if match else None


@cache
def ngspice_major() -> int | None:
    """Installed ngspice major, probed once when a version-gated test asks."""
    version = read_ngspice_version(NGSPICE_BINARY)
    major = (version or "").partition(".")[0]
    return int(major) if major.isascii() and major.isdigit() else None


def requires_ngspice(*, min_major: int) -> pytest.MarkDecorator:
    """Skip unless the installed ngspice meets the test's minimum major."""
    if NGSPICE_TESTS_SKIPPED:
        return pytest.mark.skipif(True, reason="KLT_SKIP_NGSPICE_TESTS=1")
    if NGSPICE_BINARY is None:
        return pytest.mark.skipif(
            True, reason=f"ngspice is not installed (requires major {min_major})"
        )
    major = ngspice_major()
    installed = "unknown" if major is None else str(major)
    return pytest.mark.skipif(
        major is None or major < min_major,
        reason=f"installed ngspice major {installed}, requires major {min_major}",
    )


# Icarus simulation needs both the compiler and its runtime. The SDF bus,
# escaped-divider and ifnone-edge integration tests all fail launching vvp
# when only iverilog is installed (issue #5699); a compiler-only gate is not
# sufficient for these real-simulation tiers.
HAVE_ICARUS = shutil.which("iverilog") is not None and shutil.which("vvp") is not None


def _have_cocotb() -> bool:
    try:
        import cocotb_tools.runner  # noqa: F401
    except Exception:
        return False
    return True


HAVE_COCOTB = _have_cocotb()

SKIP_NO_ICARUS = pytest.mark.skipif(
    not HAVE_ICARUS, reason="iverilog/vvp not installed"
)
SKIP_NO_COCOTB = pytest.mark.skipif(
    not HAVE_COCOTB, reason="cocotb is not installed on this machine"
)
