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
import shutil

import pytest

#: Real `ngspice` invocations (sky130 vendor-deck simulations) are slow and
#: host-load-sensitive -- the same wall-clock cost is fine in CI's dedicated,
#: uncontended runner (~3.5min for the full suite, see issue #1651) but can
#: blow well past the local buildGate's timeout on a shared/busy host running
#: several concurrent builders. `KLT_SKIP_NGSPICE_TESTS=1` (set by
#: `npm run check:ci`'s local-only test script, never by CI) lets a host with
#: ngspice installed still skip this tier locally, mirroring what already
#: happens for free on a host with no ngspice at all.
NGSPICE_ON_PATH = shutil.which("ngspice") is not None
NGSPICE_TESTS_SKIPPED = os.environ.get("KLT_SKIP_NGSPICE_TESTS") == "1"
HAVE_NGSPICE = NGSPICE_ON_PATH and not NGSPICE_TESTS_SKIPPED

#: Ready-made gate for a real-ngspice test: skips both on a host without the
#: binary and under the local-only `KLT_SKIP_NGSPICE_TESTS=1` opt-out.
SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE, reason="ngspice is not installed on this machine"
)
