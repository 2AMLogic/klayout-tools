"""Worker report identity checks for a selected regrade."""

import pytest

from klayout_tools import sim
from klayout_tools.sim_errors import SimError
from klayout_tools.sim_regrade import (
    REGRADE_UNSUPPORTED_WORKER_CODE,
    require_regrade_echo,
)


@pytest.fixture
def points():
    return sim.expand_corners({"temperature_c": [27, 80]}, [])


@pytest.mark.parametrize("extra", [False, True])
def test_worker_returned_different_number_of_corners(points, extra):
    ids = [p.corner_id for p in points] if extra else []
    with pytest.raises(SimError) as error:
        require_regrade_echo(
            {"_regrade": {}}, points[:1], [{"corner_id": i} for i in ids], "batch"
        )
    assert error.value.code == REGRADE_UNSUPPORTED_WORKER_CODE
    assert f"returned {len(ids)} corner report(s) for the 1 selected unit(s)" in str(
        error.value
    )


@pytest.mark.parametrize("reordered", [False, True])
def test_worker_returned_different_corner_id(points, reordered):
    ids = [p.corner_id for p in points]
    actual = ids[::-1] if reordered else [ids[0], "unselected"]
    i = 0 if reordered else 1
    with pytest.raises(SimError) as error:
        require_regrade_echo(
            {"_regrade": {}}, points, [{"corner_id": cid} for cid in actual], "remote"
        )
    assert error.value.code == REGRADE_UNSUPPORTED_WORKER_CODE
    message = str(error.value)
    assert "remote worker" in message
    assert f"expected {ids[i]!r}" in message
    assert f"received {actual[i]!r}" in message


def test_matching_selection_or_ordinary_run(points):
    require_regrade_echo(
        {"_regrade": {}}, points, [{"corner_id": p.corner_id} for p in points], "batch"
    )
    require_regrade_echo({}, points, [], "batch")
