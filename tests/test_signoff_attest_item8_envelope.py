"""Cited item-8 paths must be recognised envelopes (#5688).

A cited path that is prose, malformed JSON, or JSON without `schema_version`
fails the attestation even when its `envelope_checks` command exits `0`.
Split from `tests/test_signoff_attest_item8.py`, which sits at the per-file
size ratchet's 500-line limit (#2766); the inventory fixtures are imported
from it, exactly as `tests/test_signoff_attest_item8_gzip.py` does.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools.signoff import build_tier_report
from klayout_tools.signoff_attest import check_attestation, render_envelope
from test_signoff import STA_PRE_TIMING_STATUS_ENVELOPE, _manifest, _write
from test_signoff_attest_item8 import _PASS, _analog_inventory, _attest, _repo


def test_cited_prose_file_fails_even_with_a_true_command(tmp_path):
    repo = _repo(tmp_path)
    (repo / "evidence" / "NOTES.txt").write_text("looks fine to me\n")
    inventory = _analog_inventory(
        spec_rows=["gain"],
        citations={"gain": ["evidence/NOTES.txt"]},
        envelope_checks={"evidence/NOTES.txt": _PASS},
    )
    envelope = _attest(repo, inventory)
    assert envelope["status"] == "fail"
    checks = envelope["attestation"]["checks"]
    env = [c for c in checks if c["check"] == "envelope"]
    assert env == [
        {
            "check": "envelope",
            "role": "evidence",
            "path": "evidence/NOTES.txt",
            "kind": None,
            "passed": False,
        }
    ]
    assert [c["passed"] for c in checks if c["check"] == "citation"] == [False]


def test_real_generic_envelope_passes_and_records_kind(tmp_path):
    envelope = _attest(_repo(tmp_path), _analog_inventory())
    assert envelope["status"] == "pass"
    kinds = {
        c["path"]: c["kind"]
        for c in envelope["attestation"]["checks"]
        if c["check"] == "envelope"
    }
    assert kinds == {
        "evidence/sim_tt.json": "generic",
        "evidence/sim_ss.json": "generic",
    }


@pytest.mark.parametrize(
    "text", ["{not json", "[1, 2]", json.dumps({"status": "pass"})]
)
def test_malformed_or_schemaless_citation_fails(tmp_path, text):
    repo = _repo(tmp_path)
    (repo / "evidence" / "sim_tt.json").write_text(text)
    envelope = _attest(repo, _analog_inventory())
    assert envelope["status"] == "fail"
    assert ("envelope", "evidence") in [
        (c["check"], c["role"])
        for c in envelope["attestation"]["checks"]
        if not c["passed"]
    ]


def test_envelope_check_text_line():
    from klayout_tools.signoff_attest_text import attestation_check_line

    base = {"check": "envelope", "role": "evidence", "path": "a.json"}
    assert (
        attestation_check_line({**base, "kind": "generic", "passed": True})
        == "[PASS] evidence a.json envelope (generic)"
    )
    assert attestation_check_line({**base, "kind": None, "passed": False}) == (
        "[FAIL] evidence a.json envelope: not a recognised envelope"
    )


def _envelope_row(repo, document) -> dict:
    """The item-8 ``envelope`` check row for one cited ``document``."""
    (repo / "evidence" / "cited.json").write_text(json.dumps(document))
    inventory = _analog_inventory(
        spec_rows=["gain"],
        citations={"gain": ["evidence/cited.json"]},
        envelope_checks={"evidence/cited.json": _PASS},
    )
    (row,) = (
        c
        for c in _attest(repo, inventory)["attestation"]["checks"]
        if c["check"] == "envelope"
    )
    return row


def _manifest_reason(tmp_path, document) -> str:
    """The ``reason`` ``--manifest`` grading gives the same document cited as
    item 5 (the digital item that accepts the version-skewed ``sta`` shape)."""
    path = _write(tmp_path, "manifest-cited.json", document)
    result = build_tier_report(_manifest(kind="digital", evidence={"5": path}))
    return next(item for item in result["items"] if item["id"] == 5)["reason"]


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        # Issue #5986: a `klt sta` response written before `timing_status`
        # existed. The exact fixture `--manifest` grading is pinned against
        # (`test_envelope_version_skew_is_distinct_from_unrecognized_envelope`)
        # -- an older klt's valid output, remedied by re-running the check.
        pytest.param(
            STA_PRE_TIMING_STATUS_ENVELOPE, "envelope_version_skew", id="version-skew"
        ),
        # A genuinely foreign document: remedied by citing a different file.
        pytest.param({"not": "an envelope"}, "unrecognized_envelope", id="foreign"),
        # No `schema_version`: the recogniser's first refusal, still foreign.
        pytest.param({"status": "pass"}, "unrecognized_envelope", id="schemaless"),
        # Parsed as JSON but not an object: docs/cli/signoff.md's reason-values
        # table gives `--manifest` the same `unrecognized_envelope` for it.
        pytest.param([1, 2], "unrecognized_envelope", id="not-an-object"),
    ],
)
def test_refused_citation_names_the_reason_manifest_grading_gives(
    tmp_path, document, reason
):
    row = _envelope_row(_repo(tmp_path), document)

    assert row == {
        "check": "envelope",
        "role": "evidence",
        "path": "evidence/cited.json",
        "kind": None,
        "passed": False,
        "reason": reason,
    }
    # The two surfaces agree on the diagnosis, not only the verdict.
    assert _manifest_reason(tmp_path, document) == reason


def test_recognised_citation_row_carries_no_reason(tmp_path):
    document = {"schema_version": 1, "kind": "generic", "status": "pass"}
    row = _envelope_row(_repo(tmp_path), document)

    assert row["passed"] is True
    assert row["kind"] == "generic"
    assert "reason" not in row


def test_skew_text_line_names_the_remedy_and_differs_from_foreign():
    from klayout_tools.signoff_attest_text import attestation_check_line

    base = {
        "check": "envelope",
        "role": "evidence",
        "path": "a.json",
        "kind": None,
        "passed": False,
    }
    skew = attestation_check_line({**base, "reason": "envelope_version_skew"})
    foreign = attestation_check_line({**base, "reason": "unrecognized_envelope"})

    assert skew == (
        "[FAIL] evidence a.json envelope: version skew -- written by an older klt "
        "than this one recognises; re-run the cited check under the current klt"
    )
    assert foreign == "[FAIL] evidence a.json envelope: not a recognised envelope"
    # A row committed before the field existed renders as it always did.
    assert attestation_check_line(base) == foreign


def test_check_drift_from_the_new_field_is_confined_to_a_refused_row(tmp_path):
    """Measured ``--check`` impact (issue #5986): a *passing* attestation's rows
    gain nothing, so a committed one still matches; only a committed
    attestation that already *failed* on a refused envelope drifts, at that
    row's ``reason`` alone."""
    repo = _repo(tmp_path)
    inv_path = str(repo / "inventory.json")
    passing = repo / "evidence" / "item8-pass.json"
    passing.write_text(render_envelope(_attest(repo, _analog_inventory())))
    assert check_attestation(8, inv_path, str(passing))["status"] == "match"

    (repo / "evidence" / "sim_ss.json").write_text(
        json.dumps(STA_PRE_TIMING_STATUS_ENVELOPE)
    )
    legacy = json.loads(render_envelope(_attest(repo, _analog_inventory())))
    for row in legacy["attestation"]["checks"]:
        row.pop("reason", None)  # what a pre-#5986 klt wrote
    committed = repo / "evidence" / "item8-fail.json"
    committed.write_text(json.dumps(legacy))

    drifted = check_attestation(8, inv_path, str(committed))

    assert drifted["status"] == "drifted"
    assert [entry["field"].rsplit(".", 1)[-1] for entry in drifted["drift"]] == [
        "reason"
    ]
    assert drifted["drift"][0]["check"] == {
        "role": "evidence",
        "path": "evidence/sim_ss.json",
    }
