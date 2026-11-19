"""`resume_hosts` -- the fleet size a resumed dispatch may ask for (#6685).

`--resume` reuses finished units, so the dispatch can be left with fewer
outstanding units than the requested `hosts`. That must not trip the "hosts
exceeds the number of units to dispatch" refusal, which exists for a *fresh*
request that asks for more hosts than it has units.

The end-to-end resume case lives in `test_sim_netlist_digest_capture.py`
(`test_run_sim_resume_reports_scope_from_batch_dispatch[True-2]`); this pins
the clamp rule itself, offline and without a fake batch transport.
"""

from __future__ import annotations

import pytest

from klayout_tools.sim_batch_resume import resume_hosts


@pytest.mark.parametrize(
    ("hosts", "requested_units", "dispatch_units", "expected"),
    [
        # A resume that reused units is capped at what is still outstanding.
        (2, 2, 1, 1),
        (4, 6, 3, 3),
        (8, 8, 1, 1),
        # Already within the outstanding count: untouched.
        (2, 4, 3, 2),
        (3, 6, 3, 3),
        (1, 4, 2, 1),
        # Nothing reused (a fresh run, or a resume that harvested nothing):
        # `hosts > units` is still the caller's mistake, so it is passed
        # through for `_run_batch_fleet` to refuse.
        (3, 2, 2, 3),
        (2, 1, 1, 2),
        # Everything reused: nothing is dispatched, so nothing to clamp.
        (2, 2, 0, 2),
    ],
)
def test_resume_hosts(hosts, requested_units, dispatch_units, expected):
    corner_points = [object()] * requested_units
    dispatch_points = corner_points[:dispatch_units]
    assert resume_hosts(hosts, corner_points, dispatch_points) == expected
