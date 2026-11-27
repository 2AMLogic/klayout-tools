"""``corners`` must be an object, not an array/scalar (issue #4978)."""

from __future__ import annotations

import pytest

from klayout_tools import sim, sim_corners


@pytest.mark.parametrize("bad", [[{"process": "tt"}], "tt", 3])
def test_expand_corners_non_object_raises_sim_error(bad):
    with pytest.raises(
        sim.SimError, match=r"corners must be an object.*corners\.units"
    ):
        sim._expand_corners(bad, [])


def test_expand_corners_empty_object_unchanged():
    assert len(sim._expand_corners({}, [])) == 1


@pytest.mark.parametrize(
    "volts", [10**400, -(10**400), float("nan"), float("inf"), True]
)
def test_nodeset_values_refuse_non_finite_without_overflow_error(volts):
    # A 400-digit int literal parses to an int too large for a float
    # (issue #5994): refused as the SimError envelope, not an OverflowError.
    with pytest.raises(sim.SimError, match="finite number of volts"):
        sim_corners._normalize_nodeset_values({"vout": volts}, "request.nodeset")
