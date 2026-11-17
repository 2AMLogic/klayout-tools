"""``klt signoff --attest 8``: ``uncited_rows`` gap declarations (issue #5856).

``uncited_rows`` declares a known, owned spec-row gap: a row with no citable
evidence passes a visible ``uncited_row`` check instead of failing its
``citation`` check — item 8's counterpart of item 9's ``unbenched_rows``.

(Split from ``test_signoff_attest_item8.py`` for the file-size ratchet; the
inventory/attestation helpers are imported from it.)
"""

from __future__ import annotations

import json

import pytest

from klayout_tools.signoff_attest import AttestationError, check_attestation
from test_signoff_attest_item8 import (
    _analog_inventory,
    _attest,
    _committed_item8,
    _repo,
)


def _uncited_inventory() -> dict:
    inventory = _analog_inventory(spec_rows=["gain", "bandwidth", "psrr"])
    inventory["uncited_rows"] = [{"row": "psrr", "issue": 5688}]
    return inventory


def test_declared_uncited_row_passes_instead_of_failing_citation(tmp_path):
    repo = _repo(tmp_path)

    envelope = _attest(repo, _uncited_inventory())

    assert envelope["status"] == "pass"
    uncited = [
        c for c in envelope["attestation"]["checks"] if c["check"] == "uncited_row"
    ]
    assert uncited == [
        {
            "check": "uncited_row",
            "role": "row",
            "row": "psrr",
            "issue": 5688,
            "passed": True,
        }
    ]
    assert not [
        c
        for c in envelope["attestation"]["checks"]
        if c["check"] == "citation" and c["row"] == "psrr"
    ]


def test_omitted_or_empty_uncited_rows_leave_the_checks_unchanged(tmp_path):
    repo = _repo(tmp_path)

    envelope = _attest(repo, _analog_inventory())
    checks = envelope["attestation"]["checks"]

    # Exactly the pre-feature shape: report + two evidence files + two
    # citations + two freshness commands, nothing else.
    assert [c["check"] for c in checks] == [
        "present",
        "present",
        "present",
        "citation",
        "citation",
        "command",
        "command",
    ]
    # An empty declaration is the same as none: identical checks and status.
    # (The envelope's inventory pin still differs -- it hashes the bytes.)
    empty = _attest(repo, _analog_inventory(uncited_rows=[]))
    assert empty["attestation"]["checks"] == checks
    assert empty["status"] == envelope["status"]


def test_text_line_renders_an_uncited_row(tmp_path):
    from klayout_tools.signoff_attest_text import attestation_check_line

    repo = _repo(tmp_path)
    envelope = _attest(repo, _uncited_inventory())

    lines = [attestation_check_line(c) for c in envelope["attestation"]["checks"]]
    assert "[PASS] row psrr has no citation yet (issue #5688)" in lines


@pytest.mark.parametrize(
    "rows",
    [
        "R1",
        [{"row": "gain"}],
        [{"row": "", "issue": 1}],
        [{"row": "gain", "issue": 0}],
        [{"row": "gain", "issue": True}],
        [{"row": "gain", "issue": 1, "extra": 1}],
        [{"row": "gain", "issue": 1}, {"row": "gain", "issue": 2}],
    ],
)
def test_malformed_uncited_rows_are_refused(tmp_path, rows):
    repo = _repo(tmp_path)
    inventory = _analog_inventory()
    inventory["uncited_rows"] = rows

    with pytest.raises(AttestationError, match="uncited"):
        _attest(repo, inventory)


def test_uncited_row_that_is_also_cited_is_refused(tmp_path):
    repo = _repo(tmp_path)
    inventory = _analog_inventory()
    inventory["uncited_rows"] = [{"row": "gain", "issue": 1}]

    with pytest.raises(AttestationError, match="coexist"):
        _attest(repo, inventory)


def test_uncited_row_not_in_spec_rows_is_refused(tmp_path):
    repo = _repo(tmp_path)
    inventory = _analog_inventory()
    inventory["uncited_rows"] = [{"row": "psrr", "issue": 1}]

    with pytest.raises(AttestationError, match="spec_rows"):
        _attest(repo, inventory)


def test_check_reports_drift_when_uncited_rows_change(tmp_path):
    repo = _repo(tmp_path)
    committed = _committed_item8(repo, _uncited_inventory())

    inventory = _uncited_inventory()
    inventory["uncited_rows"] = []  # gap declaration withdrawn: the row now fails
    (repo / "inventory.json").write_text(json.dumps(inventory))
    withdrawn = check_attestation(8, str(repo / "inventory.json"), committed)
    assert withdrawn["status"] != "match"

    inventory["spec_rows"].remove("psrr")  # the row is hidden entirely
    (repo / "inventory.json").write_text(json.dumps(inventory))
    dropped = check_attestation(8, str(repo / "inventory.json"), committed)
    assert dropped["status"] != "match"
