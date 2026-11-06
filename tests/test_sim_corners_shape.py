"""``corners`` must be an object, not an array/scalar (issue #4978)."""

from __future__ import annotations

import pytest

from klayout_tools import sim


@pytest.mark.parametrize("bad", [[{"process": "tt"}], "tt", 3])
def test_expand_corners_non_object_raises_sim_error(bad):
    with pytest.raises(
        sim.SimError, match=r"corners must be an object.*corners\.units"
    ):
        sim._expand_corners(bad, [])


def test_expand_corners_empty_object_unchanged():
    assert len(sim._expand_corners({}, [])) == 1
