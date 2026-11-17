"""Device-multiplier lint (issue #5779): the vendor ``mult`` / SPICE ``m=``
pair on instances of subcircuits that declare ``mult`` but never forward it
to drive.

Unit tests run the pure lint on inline synthetic subckt text (no vendored
PDK data); the ``run_sim`` tests stub the ngspice binary out like
``tests/test_sim.py`` does, so the additive ``environment.device_multiplier``
warning and the unchanged exit status are asserted on a real sweep report.
"""

from __future__ import annotations

import json

import pytest

from helpers.subprocess_fakes import _stub_subprocess_run
from klayout_tools import device_multiplier_lint as lint
from klayout_tools import sim
from klayout_tools.cli import main

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)

#: The sky130 shape under test: `mult` declared in the (continuation-folded)
#: header, never forwarded -- the body reads it only in a mismatch sigma
#: expression and hard-codes the primitive's own ``m = 1``.
DECLARING_NOT_FORWARDING = """
.subckt sky130_fd_pr__nfet_01v8 d g s b
+ par1={1} mult=1 w=1 l=0.15
M0 d g s b model l = {l} w = {w} m = 1
+ swx_vth = {AGAUSS(0,1.0,1)/sqrt(l*w*mult)}
.ends
"""

#: A subckt that does forward the parameter to drive.
FORWARDING = """
.subckt dev_forward d g s b mult=1 w=1
M0 d g s b model w={w} m = {mult}
.ends
"""


def _subckts(*texts: str) -> dict[str, str]:
    return lint.collect_subckts(texts)


def _findings(netlist: str, *closure: str) -> list[dict]:
    return lint.lint_instances(lint.instance_cards(netlist), _subckts(*closure))


def _report(netlist: str, *closure: str) -> dict:
    return lint.instance_report(lint.instance_cards(netlist), _subckts(*closure))


# --------------------------------------------------------------------------- #
# The three rule-set cases
# --------------------------------------------------------------------------- #


def test_mult_above_one_without_m_is_a_drive_1x_finding():
    findings = _findings(
        "XU1 d g s b sky130_fd_pr__nfet_01v8 mult=6\n", DECLARING_NOT_FORWARDING
    )
    assert [f["kind"] for f in findings] == [lint.DRIVE_1X]
    assert findings[0]["instance"] == "XU1"
    assert findings[0]["subckt"] == "sky130_fd_pr__nfet_01v8"
    assert findings[0]["m"] is None
    assert findings[0]["mult"] == "6"
    assert "simulates 1x" in findings[0]["note"]


def test_m_without_mult_is_a_disagreement_finding():
    findings = _findings(
        "XU1 d g s b sky130_fd_pr__nfet_01v8 m=3\n", DECLARING_NOT_FORWARDING
    )
    assert [f["kind"] for f in findings] == [lint.DRIVE_MISMATCH_DISAGREEMENT]
    assert findings[0]["m"] == "3"
    assert findings[0]["mult"] is None
    assert "disagree" in findings[0]["note"]


def test_m_unequal_to_mult_is_a_disagreement_finding():
    findings = _findings(
        "XU1 d g s b sky130_fd_pr__nfet_01v8 m=2 mult=6\n", DECLARING_NOT_FORWARDING
    )
    assert [f["kind"] for f in findings] == [lint.DRIVE_MISMATCH_DISAGREEMENT]


def test_equal_pair_is_clean():
    assert (
        _findings(
            "XU1 d g s b sky130_fd_pr__nfet_01v8 mult=6 m=6\n",
            DECLARING_NOT_FORWARDING,
        )
        == []
    )


def test_neither_param_is_clean():
    assert (
        _findings("XU1 d g s b sky130_fd_pr__nfet_01v8\n", DECLARING_NOT_FORWARDING)
        == []
    )


def test_mult_of_one_without_m_is_clean():
    assert (
        _findings(
            "XU1 d g s b sky130_fd_pr__nfet_01v8 mult=1\n", DECLARING_NOT_FORWARDING
        )
        == []
    )


def test_explicit_m_of_one_without_mult_is_clean():
    assert (
        _findings("XU1 d g s b sky130_fd_pr__nfet_01v8 m=1\n", DECLARING_NOT_FORWARDING)
        == []
    )


# --------------------------------------------------------------------------- #
# What is (never) flagged
# --------------------------------------------------------------------------- #


def test_forwarding_subckt_is_never_flagged():
    assert (
        _findings(
            "XU1 d g s b dev_forward mult=6\nXU2 d g s b dev_forward mult=6 m=3\n",
            FORWARDING,
        )
        == []
    )


def test_mismatch_expression_is_not_forwarding():
    # sqrt(l*w*mult) in the body must not read as forwarding: the sky130
    # shape stays a declaring-not-forwarding subckt and mult=6 still finds.
    findings = _findings(
        "XU1 d g s b sky130_fd_pr__nfet_01v8 mult=6\n", DECLARING_NOT_FORWARDING
    )
    assert [f["kind"] for f in findings] == [lint.DRIVE_1X]


def test_subckt_without_a_mult_param_is_out_of_scope():
    plain = ".subckt plain_sw a b c d\nM0 a b c d model\n.ends\n"
    assert _findings("XU1 a b c d plain_sw m=2 mult=2\n", plain, FORWARDING) == []


def test_no_message_suggests_dropping_a_form():
    text = json.dumps(
        _report(
            "XU1 d g s b sky130_fd_pr__nfet_01v8 mult=6\n"
            "XU2 d g s b sky130_fd_pr__nfet_01v8 m=3\n",
            DECLARING_NOT_FORWARDING,
        )
    ).lower()
    for phrase in ("ignor", "remov", "unused", "meaningless", "dead parameter"):
        assert phrase not in text, phrase


# --------------------------------------------------------------------------- #
# Reading cards: folding, case, hierarchy, unknown targets
# --------------------------------------------------------------------------- #


def test_continuation_cards_fold_before_matching():
    netlist = "XU1 d g s b\n+ sky130_fd_pr__nfet_01v8 mult=6\n"
    models = ".subckt sky130_fd_pr__nfet_01v8 d g s b\n+ mult=1\nR0 d g 1k\n.ends\n"
    findings = _findings(netlist, models)
    assert [f["kind"] for f in findings] == [lint.DRIVE_1X]


def test_param_names_match_case_insensitively():
    findings = _findings(
        "XU1 d g s b sky130_fd_pr__nfet_01v8 MULT = 6\n", DECLARING_NOT_FORWARDING
    )
    assert [f["kind"] for f in findings] == [lint.DRIVE_1X]
    assert findings[0]["mult"] == "6"


def test_hierarchical_instance_name_is_echoed_verbatim():
    findings = _findings(
        "Xsub.XU1 d g s b sky130_fd_pr__nfet_01v8 mult=4\n",
        DECLARING_NOT_FORWARDING,
    )
    assert findings[0]["instance"] == "Xsub.XU1"


def test_instance_inside_a_defined_subckt_is_checked_too():
    netlist = (
        "Xtop a b sky130_fd_pr__nfet_01v8 mult=6\n"
        ".subckt wrap p n\n"
        "Xin p n sky130_fd_pr__nfet_01v8 m=3\n"
        ".ends\n"
    )
    findings = _findings(netlist, DECLARING_NOT_FORWARDING)
    assert sorted(f["kind"] for f in findings) == [
        lint.DRIVE_1X,
        lint.DRIVE_MISMATCH_DISAGREEMENT,
    ]


def test_nested_subckt_forwarding_is_scoped_to_its_own_parameter():
    outer_declares_inner_forwards = (
        ".subckt outer d g s b mult=1\n"
        ".subckt inner d g s b mult=1\n"
        "M0 d g s b model m = {mult}\n"
        ".ends\n"
        "M1 d g s b model m = 1\n"
        ".ends\n"
    )
    subckts = _subckts(outer_declares_inner_forwards)
    assert set(subckts) == {"outer", "inner"}
    # the inner subckt forwards its own mult: never flagged
    assert (
        lint.lint_instances(lint.instance_cards("Xi d g s b inner mult=6\n"), subckts)
        == []
    )
    # the outer declares mult but its own body never forwards it
    findings = lint.lint_instances(
        lint.instance_cards("Xo d g s b outer mult=6\n"), subckts
    )
    assert [f["kind"] for f in findings] == [lint.DRIVE_1X]


def test_unknown_target_subckt_is_noted_not_finding():
    report = _report("XU1 a b somewhere_else mult=6\n", DECLARING_NOT_FORWARDING)
    assert report["findings"] == []
    assert "not cross-checked" in report["note"]


# --------------------------------------------------------------------------- #
# Unevaluated values
# --------------------------------------------------------------------------- #


def test_expression_mult_is_unevaluated_not_a_finding():
    report = _report(
        "XU1 d g s b sky130_fd_pr__nfet_01v8 mult={w*2}\n"
        "XU2 d g s b sky130_fd_pr__nfet_01v8 m={mparam}\n",
        DECLARING_NOT_FORWARDING,
    )
    assert report["findings"] == []
    assert [e["param"] for e in report["unevaluated"]] == ["mult", "m"]
    assert report["unevaluated"][0]["value"] == "{w*2}"


def test_expression_mult_blocks_the_comparison_but_plain_m_still_finding():
    # XU1 cannot be compared (mult is an expression) -> no finding;
    # XU2's plain pair is still checked.
    report = _report(
        "XU1 d g s b sky130_fd_pr__nfet_01v8 mult={w*2}\n"
        "XU2 d g s b sky130_fd_pr__nfet_01v8 m=2 mult=2\n",
        DECLARING_NOT_FORWARDING,
    )
    assert report["findings"] == []
    assert [e["instance"] for e in report["unevaluated"]] == ["XU1"]


def test_numeric_spellings_compare():
    assert (
        _findings(
            "XU1 d g s b sky130_fd_pr__nfet_01v8 m={6} mult=6.0\n",
            DECLARING_NOT_FORWARDING,
        )
        == []
    )
    findings = _findings(
        "XU1 d g s b sky130_fd_pr__nfet_01v8 m=6 mult=2\n",
        DECLARING_NOT_FORWARDING,
    )
    assert [f["kind"] for f in findings] == [lint.DRIVE_MISMATCH_DISAGREEMENT]


# --------------------------------------------------------------------------- #
# The klt sim integration: additive warning, unchanged exit status
# --------------------------------------------------------------------------- #


def _write_request(tmp_path, request: dict) -> str:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return str(path)


def _write_fixtures(tmp_path, *, body: str, models: str | None):
    if models is not None:
        (tmp_path / "models.spice").write_text(models)
        body = f".include models.spice\n{body}"
    (tmp_path / "body.spice").write_text(body)
    request: dict = {
        "netlist": "body.spice",
        "analysis": {"kind": "tran", "args": "1n 1u"},
    }
    return _write_request(tmp_path, request)


def test_run_sim_reports_finding_and_keeps_exit_status(tmp_path, monkeypatch, capsys):
    # Control: the same request with the pair agreeing -- the finding must
    # not move the verdict, whatever the stubbed corner's status is.
    control = _write_fixtures(
        tmp_path,
        body="XU1 d g s b sky130_fd_pr__nfet_01v8 mult=6 m=6\nR1 d 0 1k\n",
        models=DECLARING_NOT_FORWARDING,
    )
    _stub_subprocess_run(monkeypatch)
    control_status = sim.run_sim(control)["status"]
    assert "device-multiplier" not in capsys.readouterr().err

    request = _write_fixtures(
        tmp_path,
        body="XU1 d g s b sky130_fd_pr__nfet_01v8 mult=6\nR1 d 0 1k\n",
        models=DECLARING_NOT_FORWARDING,
    )

    report = sim.run_sim(request)
    env = report["environment"]

    entry = env["device_multiplier"]
    assert [f["kind"] for f in entry["findings"]] == [lint.DRIVE_1X]
    assert entry["findings"][0]["instance"] == "XU1"
    assert "not_scanned" not in entry
    assert report["status"] == control_status
    assert "device-multiplier finding(s)" in capsys.readouterr().err
    assert main(["sim", request, "--format", "json"]) == main(
        ["sim", control, "--format", "json"]
    )
    assert "device-multiplier finding(s)" in capsys.readouterr().err


def test_run_sim_clean_netlist_keeps_environment_unchanged(tmp_path, monkeypatch):
    request = _write_fixtures(
        tmp_path,
        body=("XU1 d g s b sky130_fd_pr__nfet_01v8 mult=6 m=6\nR1 d 0 1k\n"),
        models=DECLARING_NOT_FORWARDING,
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(request)

    assert "device_multiplier" not in report["environment"]


def test_run_sim_unresolvable_closure_notes_without_failing(
    tmp_path, monkeypatch, capsys
):
    request = _write_fixtures(
        tmp_path,
        body=(
            ".include missing.sp\n"
            "XU1 d g s b sky130_fd_pr__nfet_01v8 mult=6\n"
            "R1 d 0 1k\n"
        ),
        models=None,
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(request)
    entry = report["environment"]["device_multiplier"]

    assert entry["findings"] == []
    assert "not cross-checked" in entry["note"]
    assert entry["not_scanned"]
    assert "finding(s)" not in capsys.readouterr().err


def test_run_sim_forwarding_subckt_yields_no_field(tmp_path, monkeypatch):
    request = _write_fixtures(
        tmp_path,
        body="XU1 d g s b dev_forward mult=6\nR1 d 0 1k\n",
        models=FORWARDING,
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(request)

    assert "device_multiplier" not in report["environment"]
