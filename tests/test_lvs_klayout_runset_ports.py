"""Issue #6903 tests that need no PDK install: the ``klayout-runset``
engine's strict top-port findings (parsed from the ``.lvsdb`` text the
``klayout.db`` API does not expose) and the mirrored-``inductor2``
terminal-order known limitation.

Part A's measurements behind these tests were taken on the pinned IHP
``ihp-sg13g2`` runset (v0.3.0, commit ``5cccb161``, digest
``sha256:e01bcb1e...`` in ``signoff_runset.PINNED_RUNSETS``) with KLayout
0.30.12; ``tests/fixtures/runset_inductor/mirrored.lvsdb`` is that run's
real database, so the envelope rules here run on captured bytes. The
PDK-gated reproduction of the same behaviours lives in
``tests/test_lvs_runset_inductor.py``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import lvs_tmp_repo
from klayout_tools import lvs_runset_ports as ports

tmp_path = lvs_tmp_repo.tmp_path  # pytest collects the fixture by module-level name

FIXTURES = Path(__file__).parent / "fixtures" / "runset_inductor"

#: The captured xref section (verbatim) of a real run: a compare whose
#: cross-reference is fully Match, with the strict top-port errors beside it.
_MIRRORED_XREF = """
Z(
 X(IND2 IND2 1
  L(
   M(W B('Matching nets LA vs. LB from an ambiguous group of nets'))
   M(E B('Port mismatch \\'LA\\' vs. \\'LB\\''))
   M(E B('Port mismatch \\'LB\\' vs. \\'LA\\''))
  )
  Z(
   N(2 1 1)
   N(3 2 1)
  )
 )
)
"""

#: The sg13g2-vco shape: port nets whose names carry the inductor labels
#: beside the top-level pin labels (issue #6903 context, measured on the
#: pinned runset -- these two lines are from the real capture).
_PINNET_XREF = """
Z(
 X(IND2 IND2 1
  L(
   M(E B('Port mismatch \\'LA,VDD\\' vs. \\'VDD\\''))
   M(E B('Port mismatch \\'LB,OUT\\' vs. \\'OUT\\''))
  )
 )
)
"""


def test_xref_log_entries_parse_both_forms():
    entries = ports.xref_log_entries(_MIRRORED_XREF)
    assert [e["severity"] for e in entries] == ["warning", "error", "error"]
    assert entries[1]["message"] == "Port mismatch 'LA' vs. 'LB'"


def test_strict_port_findings_are_verbatim():
    findings = ports.strict_port_findings(_MIRRORED_XREF)
    assert [(f["layout_name"], f["reference_name"]) for f in findings] == [
        ("LA", "LB"),
        ("LB", "LA"),
    ]
    assert all(f["message"].startswith("Port mismatch") for f in findings)


def test_pinnet_port_name_findings_verbatim():
    findings = ports.strict_port_findings(_PINNET_XREF)
    assert [f["message"] for f in findings] == [
        "Port mismatch 'LA,VDD' vs. 'VDD'",
        "Port mismatch 'LB,OUT' vs. 'OUT'",
    ]


def test_no_xref_section_means_no_findings():
    assert ports.strict_port_findings("netlist(\n anything\n)\n") == []


def test_captured_mirrored_database_carries_the_findings():
    text = (FIXTURES / "mirrored.lvsdb").read_text(errors="replace")
    findings = ports.strict_port_findings(text)
    assert [(f["layout_name"], f["reference_name"]) for f in findings] == [
        ("LA", "LB"),
        ("LB", "LA"),
    ]
    # The API-visible cross-reference is a clean Match: the pairs agree.
    import klayout.db as kdb

    l2n = kdb.LayoutVsSchematic()
    l2n.read(str(FIXTURES / "mirrored.lvsdb"))
    xref = l2n.xref()
    for cp in xref.each_circuit_pair():
        assert str(cp.status()) == "Match"


def test_port_mismatch_mismatches_shape():
    entries = ports.port_mismatch_mismatches(ports.strict_port_findings(_PINNET_XREF))
    assert [e["category"] for e in entries] == [ports.CATEGORY_PORT_NAME_MISMATCH] * 2
    assert all(e["severity"] == "error" for e in entries)
    assert entries[0]["net"] == {"layout": "LA,VDD", "reference": "VDD"}
    assert "Port mismatch 'LA,VDD' vs. 'VDD'" in entries[0]["description"]


def test_augment_verdict_forces_mismatch_status(tmp_path):
    db = tmp_path / "report.lvsdb"
    db.write_text(_MIRRORED_XREF, encoding="utf-8")
    verdict = {"status": "match", "mismatches": []}
    ports.augment_verdict(verdict, str(db))
    # While any strict-port finding stands, status must not read match.
    assert verdict["status"] == "mismatch"
    assert [m["category"] for m in verdict["mismatches"]].count(
        ports.CATEGORY_PORT_NAME_MISMATCH
    ) == 2


@pytest.mark.parametrize(
    ("message", "layout", "reference"),
    [
        ("Port mismatch 'LA,VDD'X' vs. 'VDD'", "LA,VDD'X", "VDD"),
        ("Port mismatch 'LB,OUT'X' vs. 'OUT'X'", "LB,OUT'X", "OUT'X"),
        ("Port mismatch 'a' vs. 'b' vs. 'c'", None, None),
        ("Port mismatch reported by runset", None, None),
    ],
)
def test_error_port_messages_always_fail_verdict(tmp_path, message, layout, reference):
    db = tmp_path / "ports.lvsdb"
    encoded = message.replace("'", "\\'")
    db.write_text(f"Z(\n M(E B('{encoded}'))\n)\n", encoding="utf-8")
    verdict = {"status": "match", "mismatches": []}
    ports.augment_verdict(verdict, str(db))
    assert verdict["status"] == "mismatch"
    (item,) = verdict["mismatches"]
    assert (item["severity"], item["category"], item["description"]) == (
        "error",
        ports.CATEGORY_PORT_NAME_MISMATCH,
        message,
    )
    assert item["net"] == {"layout": layout, "reference": reference}


def test_augment_verdict_warning_only_keeps_status(tmp_path):
    db = tmp_path / "report.lvsdb"
    db.write_text(
        "Z(\n X(IND2 IND2 1\n  L(\n"
        "   M(W B('Port mismatch \\'LA\\' vs. \\'LB\\''))\n  )\n )\n)\n",
        encoding="utf-8",
    )
    verdict = {"status": "match", "mismatches": []}
    ports.augment_verdict(verdict, str(db))
    assert verdict["status"] == "match"
    assert len(verdict["mismatches"]) == 1  # disclosed, not graded


def test_augment_verdict_missing_database_is_noop(tmp_path):
    verdict = {"status": "match", "mismatches": []}
    ports.augment_verdict(verdict, str(tmp_path / "absent.lvsdb"))
    assert verdict == {"status": "match", "mismatches": []}


def _row(t1, t2, ref_t1, pair_ok=True):
    return {
        "circuit": {"layout": "IND2", "reference": "IND2"},
        "device": "1",
        "reference_device": "1",
        "pair_ok": pair_ok,
        "t1_net": t1,
        "t2_net": t2,
        "reference_t1_net": ref_t1,
    }


def test_known_limitation_fires_on_mirrored_labels():
    rows = [_row("LB", "LA", "LA")]
    notes = ports.known_limitation_mismatches(
        rows, ports.strict_port_findings(_MIRRORED_XREF), "mismatch"
    )
    assert len(notes) == 1
    assert notes[0]["severity"] == "warning"
    assert notes[0]["category"] == "topology.known_limitation"
    assert notes[0]["details"]["limitation"] == (
        ports.LIMIT_MIRRORED_INDUCTOR2_TERMINAL_ORDER
    )


def test_known_limitation_negative_controls():
    findings = ports.strict_port_findings(_MIRRORED_XREF)
    # Control 1: the reference is the reversed side -- no note.
    assert (
        ports.known_limitation_mismatches(
            [_row("LA", "LB", "LB")], findings, "mismatch"
        )
        == []
    )
    # As-drawn: labels natural on both sides -- no note.
    assert (
        ports.known_limitation_mismatches(
            [_row("LA", "LB", "LA")], findings, "mismatch"
        )
        == []
    )
    # The pin-net shape (labels only on the layout side, no LB/LA on the
    # reference net names) -- no note: the reference convention is unreadable.
    assert (
        ports.known_limitation_mismatches(
            [_row("LA,VDD", "LB,OUT", "VDD")],
            ports.strict_port_findings(_PINNET_XREF),
            "mismatch",
        )
        == []
    )
    # A matching report never carries the note.
    assert (
        ports.known_limitation_mismatches([_row("LB", "LA", "LA")], findings, "match")
        == []
    )


def test_known_limitation_requires_the_failure_to_involve_the_device():
    findings = ports.strict_port_findings(_MIRRORED_XREF)
    # pair_ok True (absorbed into the crossed pairing) but the strict-port
    # findings do not name this device's nets: not involved, no note.
    unrelated = [
        {
            "severity": "error",
            "message": "Port mismatch 'VDD' vs. 'VDD2'",
            "layout_name": "VDD",
            "reference_name": "VDD2",
        }
    ]
    assert (
        ports.known_limitation_mismatches(
            [_row("LB", "LA", "LA")], unrelated, "mismatch"
        )
        == []
    )
    # A genuinely mismatching device pair is involved by itself.
    assert (
        len(
            ports.known_limitation_mismatches(
                [_row("LB", "LA", "LA", pair_ok=False)], unrelated, "mismatch"
            )
        )
        == 1
    )
    # And the mirrored fixture's own findings do name its nets.
    assert (
        len(
            ports.known_limitation_mismatches(
                [_row("LB", "LA", "LA")], findings, "mismatch"
            )
        )
        == 1
    )


def install_fake_klayout_capturing(monkeypatch, tmp_path, db_text):
    """Replace the subprocess boundary: the fake runset writes ``db_text``
    (a captured real ``.lvsdb``) as the report, exactly as an external
    process would."""
    fake_bin = tmp_path / "klayout"
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("KLT_KLAYOUT_BINARY", str(fake_bin))
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if cmd[0] != str(fake_bin):
            return real_run(cmd, *a, **kw)
        if cmd[1:] == ["-v"]:
            return subprocess.CompletedProcess(cmd, 0, "KLayout 0.30.12\n", "")
        args = dict(c.split("=", 1) for c in cmd if "=" in c and c != "-rd")
        Path(args["report"]).write_text(db_text, encoding="utf-8")
        # The pinned runset's own summary line on failure, logger-formatted:
        # engine_error_lines must not match it (mid-line ERROR), and the
        # strict-mode marker is absent from this synthetic stdout.
        return subprocess.CompletedProcess(
            cmd,
            0,
            "2026-01-01 00:00:00 +0000: Memory Usage (1K) : ERROR : Netlists"
            " don't match\n",
            "",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)


def _request(tmp_path):
    gds = tmp_path / "ind2.gds"
    gds.write_bytes((FIXTURES / "ind2_mirrored.gds").read_bytes())
    spice = tmp_path / "ind2.cdl"
    spice.write_text((FIXTURES / "ind2.cdl").read_text())
    deck = tmp_path / "pdk.lvs"
    deck.write_text("# runset\n")
    return json.dumps(
        {
            "engine": "klayout-runset",
            "deck_file": str(deck),
            "layout": {"file": str(gds), "top": "IND2"},
            "reference": {"netlist": str(spice), "top": "IND2"},
        }
    )


def test_run_lvs_never_matches_beside_strict_port_failure(tmp_path, monkeypatch):
    """The bug this closes: a compare-clean cross-reference used to read
    `status: "match"` while the wrapped runset had failed."""
    from klayout_tools.lvs import run_lvs

    db_text = (FIXTURES / "mirrored.lvsdb").read_text(errors="replace")
    install_fake_klayout_capturing(monkeypatch, tmp_path, db_text)
    report = run_lvs(_request(tmp_path))
    assert report["status"] == "mismatch"
    port_entries = [
        m
        for m in report["mismatches"]
        if m["category"] == ports.CATEGORY_PORT_NAME_MISMATCH
    ]
    assert [m["description"] for m in port_entries] == [
        "Port mismatch 'LA' vs. 'LB'",
        "Port mismatch 'LB' vs. 'LA'",
    ]
    assert report["category_error_counts"][ports.CATEGORY_PORT_NAME_MISMATCH] == 2
    # The runset's own verdict is in the envelope, with its log.
    assert report["provenance"]["runset"]["engine_error_lines"] == []
    assert [e["message"] for e in report["provenance"]["runset"]["xref_log"]] == [
        "Port mismatch 'LA' vs. 'LB'",
        "Port mismatch 'LB' vs. 'LA'",
    ]
    # And the known limitation is named beside the failure.
    limits = [
        m for m in report["mismatches"] if m["category"] == "topology.known_limitation"
    ]
    assert len(limits) == 1
    assert limits[0]["details"]["limitation"] == (
        ports.LIMIT_MIRRORED_INDUCTOR2_TERMINAL_ORDER
    )


def test_run_lvs_captures_strict_mode_marker(tmp_path, monkeypatch):
    from klayout_tools.lvs import run_lvs

    db_text = (FIXTURES / "mirrored.lvsdb").read_text(errors="replace")
    fake = install_fake_klayout_capturing_strict(monkeypatch, tmp_path, db_text)
    report = run_lvs(_request(tmp_path))
    assert report["provenance"]["runset"]["strict_top_port_mode"] is True
    assert fake["saw_marker"]


def install_fake_klayout_capturing_strict(monkeypatch, tmp_path, db_text):
    fake_bin = tmp_path / "klayout"
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("KLT_KLAYOUT_BINARY", str(fake_bin))
    real_run = subprocess.run
    state = {"saw_marker": False}

    def fake_run(cmd, *a, **kw):
        if cmd[0] != str(fake_bin):
            return real_run(cmd, *a, **kw)
        if cmd[1:] == ["-v"]:
            return subprocess.CompletedProcess(cmd, 0, "KLayout 0.30.12\n", "")
        args = dict(c.split("=", 1) for c in cmd if "=" in c and c != "-rd")
        Path(args["report"]).write_text(db_text, encoding="utf-8")
        stdout = (
            "Starting SG13G2 LVS Comparison in strict port mode.\n"
            "flag_missing_ports enabled: missing/mislabeled top-level ports are"
            " treated as errors.\n"
        )
        state["saw_marker"] = True
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return state


@pytest.mark.parametrize(
    "message,expected",
    [
        ("Starting SG13G2 LVS Comparison in strict port mode.", True),
        ("Starting SG13G2 LVS Comparison.", None),
        ("", None),
    ],
)
def test_strict_top_port_mode_marker(message, expected):
    assert ports.strict_top_port_mode(message) is expected
