"""Tests for the public Monte Carlo seed derivation (issue #8068)."""

from __future__ import annotations

from klayout_tools import sim


def test_mc_seed_derivation_is_public_and_bit_identical():
    # Issue #8068: the derivation yield campaigns share is public through
    # ``sim``. Concrete integers computed from the private spelling *before*
    # the rename; base seed, part order, separators and digest conversion
    # decide whether a saved campaign replays, so they must never change.
    assert sim.derive_mc_seed(0, "yield-campaign", "{}") == 1725207548
    assert sim.derive_mc_seed(42, "process", "tt", 0) == 1900618099
    # Reproduces the first ``rndseed`` of the pre-2899 golden in test_sim.py.
    assert sim.derive_mc_seed(42, "rndseed", 1973556995, 1917083296) == 1010234817
