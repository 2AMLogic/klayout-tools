"""Tests for `klt power-intent` / `run_power_intent` (issue #4479).

Hermetic: cell models and the baseline netlist/intent are the tiny files under
`examples/power-intent/`; variants are written inline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools.cli import main
from klayout_tools.power_intent import PowerIntentError, run_power_intent

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "power-intent"
MODELS = str(EXAMPLE / "cell_models.v")
NETLIST = (EXAMPLE / "netlist.v").read_text()
INTENT = json.loads((EXAMPLE / "intent.json").read_text())

# The example's only defect is the unclamped gc -> o_c leak; this repairs it.
CLEAN_NETLIST = NETLIST.replace(
    "sky130_fd_sc_hd__buf_1 leak_c (.A(gc), .X(o_c));",
    "sky130_fd_sc_hd__and2_1 iso_c (.A(gc), .B(iso_n), .X(o_c));",
)


def _run(tmp_path, netlist=CLEAN_NETLIST, **intent_edits):
    intent = {**INTENT, **intent_edits}
    n, i = tmp_path / "n.v", tmp_path / "i.json"
    n.write_text(netlist)
    i.write_text(json.dumps(intent))
    return run_power_intent(str(n), [MODELS], str(i))


def _reasons(report):
    return sorted((v["check"], v["reason"]) for v in report["violations"])


def test_clean_design_passes(tmp_path):
    report = _run(tmp_path)
    assert report["status"] == "pass"
    assert report["violations"] == []
    assert report["summary"]["boundary_nets"] == 3  # buffered ga, gb, gc


def test_unclamped_output_is_caught(tmp_path):
    report = _run(tmp_path, netlist=NETLIST)
    assert report["status"] == "fail"
    assert report["violations"] == [
        {"check": "isolation", "net": "gc", "reason": "no_clamp", "sink": "port:o_c"}
    ]


def test_wrong_clamp_value(tmp_path):
    report = _run(tmp_path, clamp_values={**INTENT["clamp_values"], "gb": 0})
    assert _reasons(report) == [("isolation", "wrong_clamp_value")]


def test_undeclared_clamp_value(tmp_path):
    values = {k: v for k, v in INTENT["clamp_values"].items() if k != "gb"}
    assert _reasons(_run(tmp_path, clamp_values=values)) == [
        ("isolation", "undeclared_clamp_value")
    ]
    assert _run(tmp_path, clamp_values=values, default_clamp=1)["status"] == "pass"


def test_clamp_control_must_be_always_on(tmp_path):
    net = CLEAN_NETLIST.replace(".B(iso_n), .X(o_c)", ".B(gq), .X(o_c)")
    assert ("isolation", "no_clamp") in _reasons(_run(tmp_path, netlist=net))


def test_empty_gated_domain_is_rejected(tmp_path):
    with pytest.raises(PowerIntentError, match="gated_instances"):
        _run(tmp_path, gated_instances=[])


def test_unclassified_state_element(tmp_path):
    report = _run(tmp_path, retained=[])
    assert _reasons(report) == [("state", "unclassified_state")]


def test_retained_fed_by_gated(tmp_path):
    net = CLEAN_NETLIST.replace(".D(o_a)", ".D(gq)")
    report = _run(tmp_path, netlist=net)
    assert ("state", "retained_fed_by_gated") in _reasons(report)
    # Stopped clock exempts the element.
    stopped = _run(tmp_path, netlist=net, stopped_clock_nets=["clk"])
    assert not [v for v in stopped["violations"] if v["check"] == "state"]


def test_good_and_bad_sequence(tmp_path):
    bad = [
        "rail_good",
        "switch_off",
        "iso_assert",
        "iso_release",
        "clk_restart",
        "reset_release",
    ]
    report = _run(tmp_path, sequence=bad)
    pairs = {(v["before"], v["after"]) for v in report["violations"]}
    assert pairs == {("iso_assert", "switch_off"), ("reset_release", "clk_restart")}
    missing = _run(tmp_path, sequence=["rail_good", "iso_release"])
    assert {v["reason"] for v in missing["violations"]} == {"missing_event"}
    assert _run(tmp_path)["checks"]["sequencing"]["status"] == "pass"


def test_sequence_omitted_is_skipped(tmp_path):
    intent = {k: v for k, v in INTENT.items() if k != "sequence"}
    (tmp_path / "n.v").write_text(CLEAN_NETLIST)
    (tmp_path / "i.json").write_text(json.dumps(intent))
    report = run_power_intent(str(tmp_path / "n.v"), [MODELS], str(tmp_path / "i.json"))
    assert report["checks"]["sequencing"]["status"] == "skipped"


def test_unknown_instance_in_intent(tmp_path):
    with pytest.raises(PowerIntentError, match="nope"):
        _run(tmp_path, retained=["nope"])


def test_cli_json_envelope_and_exit_codes(tmp_path, capsys):
    n, i = tmp_path / "n.v", tmp_path / "i.json"
    n.write_text(NETLIST)
    i.write_text(json.dumps(INTENT))
    argv = [
        "power-intent",
        str(n),
        "--intent",
        str(i),
        "--cell-models",
        MODELS,
        "--format",
        "json",
    ]
    assert main(argv) == 3
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema_version"] == 1 and doc["status"] == "fail"
    n.write_text(CLEAN_NETLIST)
    assert main(argv) == 0
    capsys.readouterr()
    argv[1] = str(tmp_path / "missing.v")
    assert main(argv) == 1
