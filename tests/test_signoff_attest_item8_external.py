"""Item-8 citation of records produced outside klt (issue #6939).

`external_records` opts a cited path out of envelope recognition (issue
#5688's guard stays for every unlisted citation): the committed ORFS
collect / vendor report / lab sheet is bound by content hash plus the
caller's `envelope_checks` freshness command, and a metric binding may
declare `{"transform": "reciprocal", "scale": n}` so a record carrying a
minimum period binds the frequency metric. Split from
`tests/test_signoff_attest_item8.py`, which sits at the per-file size
ratchet's 500-line limit; the inventory fixtures are imported from it,
exactly as `tests/test_signoff_attest_item8_envelope.py` does.
"""

from __future__ import annotations

import gzip
import json
import re
from pathlib import Path

import pytest

from klayout_tools.signoff_attest import (
    AttestationError,
    check_attestation,
    render_envelope,
)
from klayout_tools.signoff_attest_text import attestation_check_line
from test_signoff_attest_item8 import (
    _PASS,
    _analog_inventory,
    _attest,
    _repo,
)

_EXTERNAL = "orfs/fmax.json"
_RECORD = {"min_period_ns": 2.5, "cell_area": 5.0, "power_w": 0.002}


def _record_repo(tmp_path) -> Path:
    repo = _repo(tmp_path)
    (repo / "orfs").mkdir()
    (repo / _EXTERNAL).write_text(json.dumps(_RECORD))
    return repo


def _external_inventory(**overrides) -> dict:
    metrics = {
        corner: {
            "fmax": {
                "envelope": _EXTERNAL,
                "field": "min_period_ns",
                "transform": "reciprocal",
                "scale": 1000,
            },
            "area": {"envelope": _EXTERNAL, "field": "cell_area"},
            "power": {"envelope": _EXTERNAL, "field": "power_w"},
        }
        for corner in ("tt",)
    }
    inventory = _analog_inventory(
        spec_rows=["fmax"],
        citations={"fmax": [_EXTERNAL]},
        envelope_checks={_EXTERNAL: _PASS},
        corners=["tt"],
        metrics=metrics,
        external_records=[_EXTERNAL],
    )
    inventory.update(overrides)
    return inventory


def _check(envelope: dict, **match) -> dict:
    """The one attestation check whose every ``match`` key/value pair holds."""
    wanted = match.items()
    found = [
        c
        for c in envelope["attestation"]["checks"]
        if all(c.get(key) == value for key, value in wanted)
    ]
    assert len(found) == 1, f"expected exactly one check matching {match}"
    return found[0]


def test_external_record_attests_pass_with_external_kind_and_hash(tmp_path):
    repo = _record_repo(tmp_path)

    envelope = _attest(repo, _external_inventory())

    assert envelope["status"] == "pass"
    row = _check(envelope, check="envelope", path=_EXTERNAL)
    assert row == {
        "check": "envelope",
        "role": "evidence",
        "path": _EXTERNAL,
        "kind": "external",
        "passed": True,
    }
    present = _check(envelope, check="present", path=_EXTERNAL)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", present["content_hash"])
    assert _check(envelope, check="citation", row="fmax")["passed"] is True


def test_same_file_cited_without_listing_still_fails_recognition(tmp_path):
    repo = _record_repo(tmp_path)
    inventory = _external_inventory()
    del inventory["external_records"]

    envelope = _attest(repo, inventory)

    assert envelope["status"] == "fail"
    row = _check(envelope, check="envelope", path=_EXTERNAL)
    assert row["kind"] is None
    assert row["passed"] is False
    assert row["reason"] == "unrecognized_envelope"
    assert _check(envelope, check="citation", row="fmax")["passed"] is False


def test_external_path_no_row_cites_is_refused(tmp_path):
    repo = _record_repo(tmp_path)
    inventory = _external_inventory(external_records=[_EXTERNAL, "orfs/other.json"])
    inventory["citations"]["fmax"] = [_EXTERNAL]

    with pytest.raises(AttestationError, match=r"no spec row cites.*orfs/other"):
        _attest(repo, inventory)


@pytest.mark.parametrize("bad", ["../outside.json", "/abs/fmax.json", 8])
def test_external_path_outside_repo_is_refused(tmp_path, bad):
    repo = _record_repo(tmp_path)
    inventory = _external_inventory(external_records=[bad])

    with pytest.raises(AttestationError):
        _attest(repo, inventory)


@pytest.mark.parametrize("bad", ["not-a-list", [], [""], [None]])
def test_external_records_wrong_shape_is_refused(tmp_path, bad):
    repo = _record_repo(tmp_path)
    inventory = _external_inventory(external_records=bad)

    with pytest.raises(AttestationError, match="'external_records'"):
        _attest(repo, inventory)


def test_external_record_still_needs_a_freshness_command(tmp_path):
    repo = _record_repo(tmp_path)
    inventory = _external_inventory()
    del inventory["envelope_checks"][_EXTERNAL]

    with pytest.raises(AttestationError, match="needs an 'envelope_checks' command"):
        _attest(repo, inventory)


def test_missing_external_record_fails_on_presence(tmp_path):
    repo = _record_repo(tmp_path)
    (repo / _EXTERNAL).unlink()

    envelope = _attest(repo, _external_inventory())

    assert envelope["status"] == "fail"
    assert _check(envelope, check="envelope", path=_EXTERNAL)["passed"] is False


def test_reciprocal_transform_binds_period_to_frequency(tmp_path):
    repo = _record_repo(tmp_path)

    envelope = _attest(repo, _external_inventory())

    assert envelope["status"] == "pass"
    metric = _check(envelope, check="metric", corner="tt", metric="fmax")
    assert metric["value"] == pytest.approx(400)
    assert metric["raw_value"] == pytest.approx(2.5)
    assert metric["transform"] == "reciprocal"
    assert metric["scale"] == 1000
    assert metric["passed"] is True
    plain = _check(envelope, check="metric", corner="tt", metric="area")
    assert plain["value"] == pytest.approx(5.0)
    assert "raw_value" not in plain


def test_transform_allowed_on_a_recognised_envelope_too(tmp_path):
    repo = _repo(tmp_path)
    inventory = _analog_inventory(
        spec_rows=["gain"],
        citations={"gain": ["evidence/sim_tt.json"]},
        envelope_checks={"evidence/sim_tt.json": _PASS},
        corners=["tt"],
        metrics={
            "tt": {
                name: {
                    "envelope": "evidence/sim_tt.json",
                    "field": f"result.{name}",
                    "transform": "reciprocal",
                    "scale": 1000,
                }
                for name in ("fmax", "area", "power")
            }
        },
    )

    envelope = _attest(repo, inventory)

    assert envelope["status"] == "pass"
    assert _check(envelope, check="metric", corner="tt", metric="fmax")["value"] == 10


@pytest.mark.parametrize(
    "binding",
    [
        {"transform": "reciprocal"},
        {"scale": 1000},
        {"transform": "sqrt", "scale": 1000},
        {"transform": "reciprocal", "scale": 0},
        {"transform": "reciprocal", "scale": -1},
        {"transform": "reciprocal", "scale": "1000"},
        {"transform": "reciprocal", "scale": True},
        {"transform": "reciprocal", "scale": "Infinity"},
    ],
)
def test_malformed_transform_is_refused(tmp_path, binding):
    repo = _record_repo(tmp_path)
    inventory = _external_inventory()
    inventory["metrics"]["tt"]["fmax"] = {
        "envelope": _EXTERNAL,
        "field": "min_period_ns",
        **binding,
    }

    with pytest.raises(AttestationError, match=r"metrics\.tt\.fmax"):
        _attest(repo, inventory)


@pytest.mark.parametrize(
    ("record", "field"),
    [
        ({"min_period_ns": 0, "cell_area": 1, "power_w": 1}, "min_period_ns"),
        ({"cell_area": 1, "power_w": 1}, "min_period_ns"),
        ({"min_period_ns": "2.5", "cell_area": 1, "power_w": 1}, "min_period_ns"),
    ],
)
def test_unconsumable_raw_fails_the_metric_check_only(tmp_path, record, field):
    repo = _record_repo(tmp_path)
    (repo / _EXTERNAL).write_text(json.dumps(record))

    envelope = _attest(repo, _external_inventory())

    assert envelope["status"] == "fail"
    metric = _check(envelope, check="metric", corner="tt", metric="fmax")
    assert metric["value"] is None
    assert metric["passed"] is False
    assert metric["raw_value"] == record.get(field)
    assert metric["transform"] == "reciprocal"
    assert _check(envelope, check="envelope", path=_EXTERNAL)["passed"] is True


def test_negative_raw_reciprocal_records_the_negative_value(tmp_path):
    repo = _record_repo(tmp_path)
    record = dict(_RECORD, min_period_ns=-2.5)
    (repo / _EXTERNAL).write_text(json.dumps(record))

    envelope = _attest(repo, _external_inventory())

    metric = _check(envelope, check="metric", corner="tt", metric="fmax")
    assert metric["value"] == pytest.approx(-400)
    assert metric["passed"] is True


def test_check_drift_when_the_external_record_changes(tmp_path):
    repo = _record_repo(tmp_path)
    inv_path = str(repo / "inventory.json")
    committed = repo / "item8.json"
    committed.write_text(render_envelope(_attest(repo, _external_inventory())))
    assert check_attestation(8, inv_path, str(committed))["status"] == "match"

    (repo / _EXTERNAL).write_text(json.dumps(dict(_RECORD, min_period_ns=2.0)))

    result = check_attestation(8, inv_path, str(committed))
    assert result["status"] == "drifted"
    fields = {entry["field"] for entry in result["drift"]}
    assert any(
        re.fullmatch(r"attestation\.checks\.\d+\.content_hash", f) for f in fields
    )
    assert any(re.fullmatch(r"attestation\.checks\.\d+\.value", f) for f in fields)
    assert result["fresh_status"] == "pass"


def test_inventory_using_neither_new_key_still_matches(tmp_path):
    repo = _repo(tmp_path)
    inv_path = str(repo / "inventory.json")
    committed = repo / "item8.json"
    committed.write_text(render_envelope(_attest(repo, _analog_inventory())))

    assert check_attestation(8, inv_path, str(committed))["status"] == "match"


def test_text_lines_render_for_every_check(tmp_path):
    repo = _record_repo(tmp_path)

    envelope = _attest(repo, _external_inventory())

    lines = [
        attestation_check_line(check) for check in envelope["attestation"]["checks"]
    ]
    assert f"[PASS] evidence {_EXTERNAL} envelope (external)" in lines


def test_recognised_envelope_listed_external_skips_recognition(tmp_path):
    repo = _repo(tmp_path)
    inventory = _analog_inventory(
        spec_rows=["gain"],
        citations={"gain": ["evidence/sim_tt.json"]},
        envelope_checks={"evidence/sim_tt.json": _PASS},
        external_records=["evidence/sim_tt.json"],
    )

    envelope = _attest(repo, inventory)

    assert envelope["status"] == "pass"
    assert _check(envelope, check="envelope", path="evidence/sim_tt.json") == {
        "check": "envelope",
        "role": "evidence",
        "path": "evidence/sim_tt.json",
        "kind": "external",
        "passed": True,
    }


def test_gzipped_external_record_is_hashed_and_metric_bound(tmp_path):
    repo = _record_repo(tmp_path)
    (repo / _EXTERNAL).unlink()
    gz = _EXTERNAL + ".gz"
    with open(repo / gz, "wb") as handle:
        handle.write(gzip.compress(json.dumps(_RECORD).encode()))
    inventory = _external_inventory()
    inventory["citations"] = {"fmax": [gz]}
    inventory["envelope_checks"] = {gz: _PASS}
    inventory["external_records"] = [gz]
    inventory["metrics"]["tt"] = {
        "fmax": {
            "envelope": gz,
            "field": "min_period_ns",
            "transform": "reciprocal",
            "scale": 1000,
        },
        "area": {"envelope": gz, "field": "cell_area"},
        "power": {"envelope": gz, "field": "power_w"},
    }

    envelope = _attest(repo, inventory)

    assert envelope["status"] == "pass"
    fmax = _check(envelope, check="metric", corner="tt", metric="fmax")
    assert fmax["value"] == pytest.approx(400)


def test_same_external_path_listed_twice_deduplicates(tmp_path):
    repo = _record_repo(tmp_path)
    inventory = _external_inventory(external_records=[_EXTERNAL, _EXTERNAL])

    envelope = _attest(repo, inventory)

    assert envelope["status"] == "pass"
    rows = [
        c
        for c in envelope["attestation"]["checks"]
        if c["check"] == "envelope" and c["path"] == _EXTERNAL
    ]
    assert rows == [
        {
            "check": "envelope",
            "role": "evidence",
            "path": _EXTERNAL,
            "kind": "external",
            "passed": True,
        }
    ]
