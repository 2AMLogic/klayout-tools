"""``request.batch.job_timeout_s`` validation (issues #5544, #6560;
``resolve_job_timeout_s`` in ``src/klayout_tools/sim_batch_job_timeout.py``).

Split out of ``tests/test_sim_batch_job_timeout.py``, which sits at the 500-line
file-size ratchet ceiling. Everything here goes through
``sb._resolve_batch_config`` -- the path a request's ``batch`` object takes --
so a refusal is the verb's ``SimError`` (the JSON error envelope), never a
traceback. No transport is involved: nothing here touches the ``aws`` CLI.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools import sim_batch as sb
from klayout_tools.sim import SimError
from test_sim_batch import _ceiling_request

UNITS = 8
PER_UNIT_TIMEOUT_S = 1500.0


def _resolve(tmp_path, **batch):
    return sb._resolve_batch_config(
        _ceiling_request(tmp_path, **batch),
        corner_count=UNITS,
        timeout_s=PER_UNIT_TIMEOUT_S,
        check_runner=False,
    )


def _refusal(raw) -> str:
    """The contract wording of the refusal, verbatim (the ``error`` text of
    the verb's JSON envelope is part of the contract)."""
    return (
        "request.batch.job_timeout_s must be a positive number of "
        f"seconds (got {raw!r})"
    )


@pytest.mark.parametrize(
    "raw",
    [0, -1, -3400.0, float("nan"), float("inf"), float("-inf"), "3400", [3400]]
    + [10**400, -(10**400)]
    + [True, False],
)
def test_invalid_job_timeout_s_is_rejected(tmp_path, raw):
    """The same-shaped SimError as the other ``batch.*`` resolvers, before
    any S3 write: the full wording, and no ``code``."""
    with pytest.raises(SimError) as excinfo:
        _resolve(tmp_path, job_timeout_s=raw)
    assert str(excinfo.value) == _refusal(raw)
    assert getattr(excinfo.value, "code", None) is None


def test_json_decoded_huge_int_job_timeout_s_is_a_sim_error(tmp_path):
    """Issue #6560: a 400-digit integer literal in the request JSON parses to
    a Python ``int`` too large for a ``float``. ``math.isfinite`` raised
    ``OverflowError`` on it -- a traceback where the JSON error envelope is
    promised -- so the guard must refuse it as an ordinary ``SimError``."""
    batch = json.loads('{"job_timeout_s": 1' + "0" * 400 + "}")
    assert type(batch["job_timeout_s"]) is int
    with pytest.raises(SimError) as excinfo:
        _resolve(tmp_path, **batch)
    assert str(excinfo.value) == _refusal(batch["job_timeout_s"])
    assert getattr(excinfo.value, "code", None) is None


@pytest.mark.parametrize("raw", [3400, 3400.0, 3.4e3])
def test_valid_job_timeout_s_resolves_to_float(tmp_path, raw):
    config = _resolve(tmp_path, job_timeout_s=raw)
    assert config.job_timeout_s == 3400.0
    assert isinstance(config.job_timeout_s, float)


@pytest.mark.parametrize(
    "batch",
    [{}, {"job_timeout_s": None}],
    ids=["absent", "explicit-null"],
)
def test_unstated_job_timeout_s_resolves_to_none(tmp_path, batch):
    """Absent and ``null`` both keep the serial upper bound."""
    assert _resolve(tmp_path, **batch).job_timeout_s is None
