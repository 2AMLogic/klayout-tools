"""``--attest 8 --check`` against a pre-#5982 committed envelope (#6595).

PR #5982 added the ``envelope`` recognition rows to item 8's check list, so
an envelope committed before that change must surface a one-time migration
drift when checked -- exactly the new passing rows plus their counts and
summary, never a crash or a silent ``match`` -- and re-attesting must settle
back to ``match``. Split from ``tests/test_signoff_attest_item8.py``, which
sits at the per-file size ratchet's 500-line limit (#2766); the inventory
fixtures are imported from it, exactly as
``tests/test_signoff_attest_item8_gzip.py`` does.
"""

from __future__ import annotations

import copy
import json

from klayout_tools.signoff_attest import check_attestation, render_envelope
from test_signoff_attest_item8 import _analog_inventory, _attest, _repo


def test_pre_change_committed_envelope_drifts_then_matches_on_re_attest(tmp_path):
    repo = _repo(tmp_path)
    inventory = _analog_inventory()
    inv_path = repo / "inventory.json"
    inv_path.write_text(json.dumps(inventory))
    committed = repo / "evidence" / "item8.json"

    fresh = _attest(repo, inventory)
    assert fresh["status"] == "pass"
    assert fresh["attestation"]["check_count"] == 9

    # The pre-#5982 shape: no `envelope` rows, 7 checks, the old summary.
    legacy = copy.deepcopy(fresh)
    attestation = legacy["attestation"]
    attestation["checks"] = [
        c for c in attestation["checks"] if c["check"] != "envelope"
    ]
    attestation["check_count"] = attestation["passed_count"] = 7
    legacy["summary"] = fresh["summary"].replace("9/9", "7/7")
    committed.write_text(render_envelope(legacy))

    drifted = check_attestation(8, str(inv_path), str(committed))

    assert drifted["status"] == "drifted"
    assert drifted["fresh_status"] == "pass"
    drift = {entry["field"]: entry for entry in drifted["drift"]}
    assert set(drift) == {
        "attestation.check_count",
        "attestation.checks",
        "attestation.passed_count",
        "summary",
    }
    assert drift["attestation.check_count"] == {
        "field": "attestation.check_count",
        "committed": 7,
        "fresh": 9,
    }
    assert drift["attestation.passed_count"] == {
        "field": "attestation.passed_count",
        "committed": 7,
        "fresh": 9,
    }
    added = [
        c for c in drift["attestation.checks"]["fresh"] if c["check"] == "envelope"
    ]
    assert {c["path"]: (c["role"], c["kind"], c["passed"]) for c in added} == {
        "evidence/sim_tt.json": ("evidence", "generic", True),
        "evidence/sim_ss.json": ("evidence", "generic", True),
    }
    assert [
        c for c in drift["attestation.checks"]["fresh"] if c["check"] != "envelope"
    ] == drift["attestation.checks"]["committed"]
    assert drift["summary"] == {
        "field": "summary",
        "committed": legacy["summary"],
        "fresh": fresh["summary"],
    }

    committed.write_text(render_envelope(fresh))
    rechecked = check_attestation(8, str(inv_path), str(committed))
    assert rechecked["status"] == "match"
    assert rechecked["fresh_status"] == "pass"
