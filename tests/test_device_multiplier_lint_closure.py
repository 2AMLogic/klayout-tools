"""Device-multiplier findings and disclosures across SPICE include files."""

import pytest

from helpers.subprocess_fakes import _stub_subprocess_run
from klayout_tools import device_multiplier_lint as lint
from klayout_tools import sim
from test_device_multiplier_lint import (
    DECLARING_NOT_FORWARDING,
    _findings,
    _report,
    _subckts,
    _write_fixtures,
)

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)


def test_closure_file_card_finding_carries_file():
    cards = [("XU1 d g s b sky130_fd_pr__nfet_01v8 mult=6", "dut.sp")]
    findings = lint.lint_instances(cards, _subckts(DECLARING_NOT_FORWARDING))
    assert [f["kind"] for f in findings] == [lint.DRIVE_1X]
    assert findings[0]["file"] == "dut.sp"


def test_top_level_card_finding_has_no_file_key():
    findings = _findings(
        "XU1 d g s b sky130_fd_pr__nfet_01v8 mult=6\n", DECLARING_NOT_FORWARDING
    )
    assert findings == [
        {
            "instance": "XU1",
            "subckt": "sky130_fd_pr__nfet_01v8",
            "kind": lint.DRIVE_1X,
            "m": None,
            "mult": "6",
            "note": findings[0]["note"],
        }
    ]


def test_two_closure_files_each_defining_x1_are_distinguished_by_file():
    model = ".subckt dev a b mult=1\nR1 a b 1k\n.ends\n"
    cards = [
        ("X1 a b dev mult=6", "a.sp"),
        ("X1 a b dev m=3", "b.sp"),
    ]
    findings = lint.lint_instances(cards, _subckts(model))
    assert [(f["file"], f["kind"]) for f in findings] == [
        ("a.sp", lint.DRIVE_1X),
        ("b.sp", lint.DRIVE_MISMATCH_DISAGREEMENT),
    ]
    assert all(f["instance"] == "X1" for f in findings)


def test_closure_expression_card_is_not_unevaluated_noise():
    # The sky130 res_xhigh_po shape: a library subckt's own body passes
    # mult={mult} on to a declaring-not-forwarding subckt. That is a
    # parametric library definition, not a user pair to cross-check, so a
    # closure card's uncomparable value must not surface as `unevaluated`.
    cards = [
        ("Xwrap d g s b sky130_fd_pr__nfet_01v8 w={w} l={l} mult={mult}", "models.sp")
    ]
    report = lint.instance_report(cards, _subckts(DECLARING_NOT_FORWARDING))
    assert report == {"findings": []}


def test_closure_unverified_card_is_not_noted():
    cards = [("XU1 a b somewhere_else mult=6", "lib.sp")]
    report = lint.instance_report(cards, _subckts(DECLARING_NOT_FORWARDING))
    assert report == {"findings": []}


def test_top_level_expression_and_unverified_disclosures_are_kept():
    # The suppression above is scoped to closure cards: the netlist's own
    # uncomparable or unresolvable pairs stay disclosed exactly as before.
    report = _report(
        "XU1 d g s b sky130_fd_pr__nfet_01v8 mult={w*2}\nXU2 a b somewhere_else m=2\n",
        DECLARING_NOT_FORWARDING,
    )
    assert report["findings"] == []
    assert "not cross-checked" in report["note"]
    assert [e["instance"] for e in report["unevaluated"]] == ["XU1"]


def test_run_sim_dut_card_in_included_file_is_flagged_with_file(
    tmp_path, monkeypatch, capsys
):
    # The DUT sits in the included file, not the netlist: the finding must
    # still fire, now carrying the closure file's scanned-closure label.
    request = _write_fixtures(
        tmp_path,
        body="R1 d 0 1k\n",
        models=DECLARING_NOT_FORWARDING
        + "XU1 d g s b sky130_fd_pr__nfet_01v8 mult=6\n",
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(request)
    entry = report["environment"]["device_multiplier"]

    (finding,) = entry["findings"]
    assert finding["kind"] == lint.DRIVE_1X
    assert finding["instance"] == "XU1"
    assert finding["file"] == "<external>/models.spice"
    assert "device-multiplier finding(s)" in capsys.readouterr().err


def test_run_sim_netlist_in_own_closure_is_not_double_counted(tmp_path, monkeypatch):
    # stage_netlist always lists the netlist itself as the first closure
    # file, so every top-level card is reachable twice; each must yield at
    # most one finding, with today's top-level shape (no `file` key).
    request = _write_fixtures(
        tmp_path,
        body="XU1 d g s b sky130_fd_pr__nfet_01v8 mult=6\nR1 d 0 1k\n",
        models=DECLARING_NOT_FORWARDING,
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(request)
    entry = report["environment"]["device_multiplier"]

    assert [f["instance"] for f in entry["findings"]] == ["XU1"]
    assert all("file" not in f for f in entry["findings"])


def test_run_sim_definitions_only_closure_file_stays_quiet(tmp_path, monkeypatch):
    # An included file that only defines subcircuits (no instance cards of
    # its own) must not wake the lint on a clean netlist.
    request = _write_fixtures(
        tmp_path,
        body="XU1 d g s b sky130_fd_pr__nfet_01v8 mult=6 m=6\nR1 d 0 1k\n",
        models=DECLARING_NOT_FORWARDING,
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(request)

    assert "device_multiplier" not in report["environment"]


def test_vendored_model_internals_are_quiet_but_dut_cards_are_checked(
    tmp_path, monkeypatch, capsys
):
    # A reduced sky130 combined/continuous deck. The FET library instantiates
    # parasitic devices declared by the diode library. Both are ordinary
    # relative includes outside PDK_ROOT, as they would be in a vendored copy.
    monkeypatch.setenv("PDK_ROOT", str(tmp_path / "installed-pdks"))
    models = tmp_path / "models"
    models.mkdir()
    (models / "models_fet.spice").write_text(
        ".subckt fet d g s b mult=1\n"
        "Xdrain b d diode m = 0.5\n"
        "Xsrc b s diode mult=6\n"
        "M1 d g s b nch\n"
        ".MODEL nch NMOS\n"
        ".ends\n"
    )
    (models / "models_diodes.spice").write_text(
        ".subckt diode a b mult=1\nD1 a b junction\n.model junction D\n.ends\n"
    )
    includes = (
        '.include "models/models_fet.spice"\n.include "models/models_diodes.spice"\n'
    )
    _stub_subprocess_run(monkeypatch)
    request = _write_fixtures(tmp_path, body=includes + "X1 d g 0 0 fet\n", models=None)
    assert "device_multiplier" not in sim.run_sim(request)["environment"]
    assert "device-multiplier finding(s)" not in capsys.readouterr().err

    (tmp_path / "dut.spice").write_text(
        "* .model in a comment\n.control\n.model ctrl D\n.endc\n"
        ".subckt dut d g\nXD d g 0 0 fet mult=6\n.ends\n"
    )
    request = _write_fixtures(
        tmp_path,
        body=includes + '.include "dut.spice"\n.model local D\n'
        "Xtop d g 0 0 fet m=2\nXuut d g dut\n",
        models=None,
    )
    findings = sim.run_sim(request)["environment"]["device_multiplier"]["findings"]
    assert [(f["instance"], f["kind"], f.get("file")) for f in findings] == [
        ("Xtop", lint.DRIVE_MISMATCH_DISAGREEMENT, None),
        ("XD", lint.DRIVE_1X, "<external>/dut.spice"),
    ]
