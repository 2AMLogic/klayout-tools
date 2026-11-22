"""Issue #6953: ``disclosed_evidence`` distinguishes "evidence exists, but
this grader cannot read it" from "no evidence exists" on unmet T1 rows.

Lives beside ``tests/test_signoff.py`` (that file sits at its size-ratchet
ceiling), reusing its fixtures, exactly like ``test_signoff_unmet_reasons``.
"""

from __future__ import annotations

import json
import os

import pytest

from klayout_tools.cli import main
from klayout_tools.signoff import (
    SignoffError,
    apply_disclosed_evidence,
    build_fleet_report,
    build_tier_report,
)
from test_signoff import DRC_CLEAN_ENVELOPE, LVS_MATCH_ENVELOPE, _manifest, _write


def _t1_row(report: dict, item_id: int, partition: str | None = None) -> dict:
    for row in report["items"]:
        if (
            row["tier"] == "T1"
            and row["id"] == item_id
            and row["partition"] == partition
        ):
            return row
    raise LookupError(f"no T1 row for item {item_id} partition {partition!r}")


def _discloseable_tree(tmp_path) -> None:
    """The issue's reproduction tree: a Verilog netlist and a hand-written
    STA-results JSON, both committed and both opaque to this grader."""
    (tmp_path / "netlist").mkdir()
    (tmp_path / "netlist" / "demo.v").write_text("module demo(); endmodule\n")
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "sta.json").write_text(
        json.dumps({"tool": "OpenROAD", "corners": ["tt", "ss", "ff"]})
    )


def test_readable_disclosure_names_external_evidence(tmp_path, monkeypatch):
    """The issue's example: readable disclosed files yield
    ``unmet / evidence_not_klt_native``, ``citation: null``, the declared
    path in the row detail -- and T1 credit identical to the undisclosed
    case."""
    monkeypatch.chdir(tmp_path)
    _discloseable_tree(tmp_path)
    manifest = {
        "block": "demo",
        "kind": "digital",
        "disclosed_evidence": {"1": "netlist/demo.v", "5.digital": "reports/sta.json"},
    }

    report = build_tier_report(manifest)

    for item_id, path in ((1, "netlist/demo.v"), (5, "reports/sta.json")):
        row = _t1_row(report, item_id)
        assert row["status"] == "unmet"
        assert row["reason"] == "evidence_not_klt_native"
        assert row["citation"] is None
        assert row["detail"] == {"disclosed_evidence": path}
    plain = build_tier_report({"block": "demo", "kind": "digital"})
    assert report["tier"] == plain["tier"] is None
    assert report["t1_met_count"] == plain["t1_met_count"] == 0
    assert report["t1_item_count"] == plain["t1_item_count"]
    assert report["unmet_reason_counts"] == {
        "evidence_not_klt_native": 2,
        "no_evidence": plain["unmet_reason_counts"]["no_evidence"] - 2,
    }


def test_cli_exit_code_unchanged_by_a_disclosure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _discloseable_tree(tmp_path)
    disclosed = _write(
        tmp_path,
        "disclosed.json",
        {
            "block": "demo",
            "kind": "digital",
            "disclosed_evidence": {"5.digital": "reports/sta.json"},
        },
    )
    plain = _write(tmp_path, "plain.json", {"block": "demo", "kind": "digital"})

    assert (
        main(["signoff", "--manifest", disclosed, "--format", "json"])
        == main(["signoff", "--manifest", plain, "--format", "json"])
        == 3
    )


def test_text_output_names_the_new_reason(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _discloseable_tree(tmp_path)
    manifest = _write(
        tmp_path,
        "manifest.json",
        {
            "block": "demo",
            "kind": "digital",
            "disclosed_evidence": {"5.digital": "reports/sta.json"},
        },
    )

    main(["signoff", "--manifest", manifest, "--format", "text"])

    assert "reason: evidence_not_klt_native" in capsys.readouterr().out


def test_omitting_the_map_reproduces_the_undisclosed_report(tmp_path):
    report = build_tier_report(_manifest())

    for row in report["items"]:
        if row["tier"] != "T1":
            continue
        assert row["status"] == "unmet"
        assert row["reason"] == "no_evidence"
        assert "detail" not in row


def test_disclosure_is_partition_scoped_on_a_mixed_signal_block(tmp_path, monkeypatch):
    """A disclosure keyed only to ``5.digital`` affects only the digital
    row; the analog row keeps ``no_evidence`` (no bare ``5`` fallback was
    declared)."""
    monkeypatch.chdir(tmp_path)
    _discloseable_tree(tmp_path)
    manifest = {
        "block": "demo",
        "kind": "mixed-signal",
        "disclosed_evidence": {"5.digital": "reports/sta.json"},
    }

    report = build_tier_report(manifest)

    assert _t1_row(report, 5, "digital")["reason"] == "evidence_not_klt_native"
    assert _t1_row(report, 5, "analog")["reason"] == "no_evidence"
    assert "detail" not in _t1_row(report, 5, "analog")


def test_qualified_key_outranks_the_bare_key(tmp_path, monkeypatch):
    """``_lookup_evidence``'s partition-qualified precedence, reused: the
    qualifier for the partition being graded wins, the bare key is the
    fallback."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.json").write_text("{}\n")
    (tmp_path / "b.json").write_text("{}\n")

    both = build_tier_report(
        {
            "block": "demo",
            "kind": "digital",
            "disclosed_evidence": {"5": "a.json", "5.digital": "b.json"},
        }
    )
    assert _t1_row(both, 5)["detail"] == {"disclosed_evidence": "b.json"}

    bare = build_tier_report(
        {"block": "demo", "kind": "digital", "disclosed_evidence": {"5": "a.json"}}
    )
    assert _t1_row(bare, 5)["detail"] == {"disclosed_evidence": "a.json"}


def test_native_verdicts_keep_their_original_status_and_reason(tmp_path, monkeypatch):
    """A native pass stays ``met``; a native citation failure keeps its own
    reason; a spec-row verdict is untouched -- disclosure applies only to a
    row that would otherwise read ``unmet / no_evidence``."""
    monkeypatch.chdir(tmp_path)
    _discloseable_tree(tmp_path)
    (tmp_path / "a").write_text("row\n")
    drc = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    foreign = _write(tmp_path, "foreign.json", {"tool": "OpenROAD"})

    report = build_tier_report(
        {
            "block": "demo",
            "kind": "digital",
            "evidence": {"3": drc, "4": foreign},
            "spec_rows": {"5": [{"id": "tdec"}]},
            "disclosed_evidence": {"3": "drc.json", "4": "foreign.json", "5": "a"},
        }
    )

    row3 = _t1_row(report, 3)
    assert row3["status"] == "met"
    assert row3["citation"] is not None
    assert "detail" not in row3
    assert _t1_row(report, 4)["reason"] == "unrecognized_envelope"
    assert _t1_row(report, 5)["reason"] == "partial_coverage"


def test_a_native_envelope_at_a_disclosed_path_never_confers_credit(
    tmp_path, monkeypatch
):
    """Disclosure alone can never grade an item: a perfectly valid passing
    `klt` envelope sitting at the disclosed path still reads
    ``evidence_not_klt_native`` with ``citation: null``."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)

    report = build_tier_report(
        {
            "block": "demo",
            "kind": "digital",
            "disclosed_evidence": {"5.digital": "drc.json"},
        }
    )

    row = _t1_row(report, 5)
    assert row["status"] == "unmet"
    assert row["reason"] == "evidence_not_klt_native"
    assert row["citation"] is None


def test_missing_file_renders_unreadable_evidence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    report = build_tier_report(
        {
            "block": "demo",
            "kind": "digital",
            "disclosed_evidence": {"5.digital": "reports/missing.json"},
        }
    )

    row = _t1_row(report, 5)
    assert row["status"] == "unmet"
    assert row["reason"] == "unreadable_evidence"
    assert row["detail"] == {"disclosed_evidence": "reports/missing.json"}


def test_directory_renders_unreadable_evidence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reports").mkdir()

    report = build_tier_report(
        {
            "block": "demo",
            "kind": "digital",
            "disclosed_evidence": {"5.digital": "reports"},
        }
    )

    assert _t1_row(report, 5)["reason"] == "unreadable_evidence"


@pytest.mark.parametrize(
    "target,expected",
    [(None, "unreadable_evidence"), ("file", "evidence_not_klt_native")],
)
def test_regular_file_links_and_device_disclosures(tmp_path, target, expected):
    link = tmp_path / "artifact"
    if target is None:
        path = os.devnull
    else:
        regular = tmp_path / target
        regular.write_text("external measurement")
        link.symlink_to(regular)
        path = str(link)
    report = build_tier_report(
        {"kind": "digital", "disclosed_evidence": {"5.digital": path}}
    )
    row = _t1_row(report, 5)
    assert row["reason"] == expected
    assert row["detail"]["disclosed_evidence"] == path


@pytest.mark.parametrize(
    "bad",
    [
        "netlist/demo.v",  # not a JSON object
        {"1": 3},  # value is not a string
        {"1": ""},  # empty path
        {"1": {"file": "demo.v"}},  # an evidence-entry shape, not a path
        {"five": "demo.v"},  # key is not an item id
        {"5.quantum": "sta.json"},  # unknown partition qualifier
        {"5.digital\n": "sta.json"},
        {"5.": "sta.json"},  # dangling qualifier
        {".digital": "sta.json"},  # no item id
    ],
)
def test_malformed_declaration_is_a_validation_error(tmp_path, monkeypatch, bad):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SignoffError):
        build_tier_report(
            {"block": "demo", "kind": "digital", "disclosed_evidence": bad}
        )


def test_application_skips_rows_this_build_does_not_grade():
    """The disclosure lands only on a row this build graded and that graded
    ``unmet / no_evidence`` -- never on an ungraded row, a ``met`` row, or a
    recorded ``not_applicable`` one."""
    base = {"tier": "T1", "id": 12, "citation": None, "graded_by_build": True}
    ungraded = {
        **base,
        "status": "unmet",
        "reason": "no_evidence",
        "graded_by_build": False,
    }
    met = {**base, "status": "met", "reason": None}
    na = {**base, "status": "not_applicable", "reason": None}

    for row in (ungraded, met, na):
        out = apply_disclosed_evidence(
            dict(row), {"12": "x.json"}, item_id=12, partition_kind="digital"
        )
        assert out["reason"] == row["reason"]
        assert "detail" not in out


def test_fleet_blocker_reason_matches_the_tier_report(tmp_path, monkeypatch):
    """Item 5 is the first eligible blocker (3 and 4 cite passing native
    evidence); the roll-up's ``blocking_item.reason`` is the tier report's
    own reason for that row, not a re-derivation."""
    monkeypatch.chdir(tmp_path)
    _discloseable_tree(tmp_path)
    drc = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    lvs = _write(tmp_path, "lvs.json", LVS_MATCH_ENVELOPE)
    block = {
        "block": "demo",
        "kind": "digital",
        "evidence": {"3": drc, "4": lvs},
        "disclosed_evidence": {"5.digital": "reports/sta.json"},
    }

    fleet = build_fleet_report({"blocks": [block]})

    blocker = fleet["blocks"][0]["blocking_item"]
    assert blocker["id"] == 5
    assert blocker["reason"] == "evidence_not_klt_native"
    assert blocker["reason"] == _t1_row(build_tier_report(block), 5)["reason"]
