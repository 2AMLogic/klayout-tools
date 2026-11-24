"""The runner-version preflight on the `klt signoff` generator path (issue
#5654; split from test_signoff_generator_batch.py for the file-size ratchet).

``sim_batch_contract_preflight`` raises ``SimError`` when the submitting
client's own klt version cannot be pinned against the fleet runner. Four of
the five batch backends translated that into their own error class;
``signoff_generator_batch`` did not, and nothing on the `klt signoff` path
caught it -- not ``run_approved_generator``'s packaging guard, not
``cli/signoff_cmd``, not ``cli.main`` -- so it escaped as a Python traceback
instead of the error envelope ``docs/json-contract.md`` makes the API.

The per-module translation itself is asserted for all five backends in
tests/test_batch_common.py; this is the end-to-end half.
"""

from __future__ import annotations

import test_signoff_generator_batch as sgb_tests
from klayout_tools import _provenance
from klayout_tools import signoff_generator_batch as sgb

# The generator harness (fake Spot transport, throwaway git repo, the
# never-launch-locally spy), reused rather than rebuilt.
fake = sgb_tests.fake
no_local_generator = sgb_tests.no_local_generator
repo = sgb_tests.repo

_UNPINNABLE = "not a version!!"


def test_an_unpinnable_client_version_is_a_package_failed_row(
    repo, fake, tmp_path, monkeypatch
):
    """The row is a ``failed`` outcome carrying the preflight's own message,
    and nothing is ever submitted to the fleet."""
    monkeypatch.setattr(_provenance, "_klt_version", lambda: _UNPINNABLE)

    row = sgb_tests._item1(repo, sgb_tests._execution(tmp_path))

    sgb_tests._assert_failed(row, sgb.FAIL_PACKAGE)
    assert "cannot be pinned" in row["detail"]["generator_execution"]["message"]
    assert fake.submissions() == []
