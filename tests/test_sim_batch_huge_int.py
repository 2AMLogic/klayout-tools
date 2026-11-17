"""Huge-int ``batch`` seconds fields (issue #5994).

The stdlib ``json`` parser turns a 400-digit integer literal into a Python
``int`` too large for a ``float``; each ``request.batch`` seconds field must
refuse it through the documented :class:`~klayout_tools.sim.SimError`
envelope, not escape as an uncaught ``OverflowError`` traceback.
``test_sim_batch.py`` holds the end-to-end batch coverage but sits at its
file-size-baseline ceiling, so its number-contract regressions live here.
"""

from __future__ import annotations

import pytest

from klayout_tools import sim
from klayout_tools import sim_batch as sb


def test_huge_int_capacity_wait_s_is_a_documented_error_not_overflow_error():
    with pytest.raises(sim.SimError, match="capacity_wait_s"):
        sb.resolve_capacity_wait_s({"capacity_wait_s": 10**400})


def test_huge_int_interrupted_grace_s_is_a_documented_error_not_overflow_error():
    with pytest.raises(sim.SimError, match="interrupted_grace_s"):
        sb._resolve_interrupted_grace_s({"interrupted_grace_s": 10**400})


def test_huge_int_max_job_seconds_is_a_documented_error_not_overflow_error():
    with pytest.raises(sim.SimError, match=r"request\.batch\.max_job_seconds"):
        sb._resolve_max_job_seconds({"max_job_seconds": 10**400}, {})
