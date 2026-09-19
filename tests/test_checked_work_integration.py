"""Real producer/consumer contracts; engine subprocesses alone may be faked."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import klayout.db as kdb
import pytest
from jsonschema import Draft202012Validator

import test_drc_klayout_engine as klayout_fixtures
import test_erc as erc_fixtures
import test_pex as pex_fixtures
import test_power as power_fixtures
import test_sim as sim_fixtures
from klayout_tools import drc, erc, pex, power, sim
from klayout_tools.cli import main
from klayout_tools.coverage import (
    build_check_coverage,
    coverage_state,
    coverage_validation_error,
)
from klayout_tools.decks import DrcRule
from klayout_tools.signoff import build_signoff, build_tier_report
from test_signoff import DRC_CLEAN_ENVELOPE

_SCHEMA = json.loads(
    (Path(__file__).parents[1] / "docs/schemas/coverage.schema.json").read_text()
)


def _assert_contract(report, state):
    assert coverage_validation_error(report["coverage"]) is None
    Draft202012Validator(_SCHEMA).validate(report["coverage"])
    assert coverage_state(report) == state


def _assert_signoff(tmp_path, report, *, passed, item=None):
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(report))
    result = build_signoff([str(path)])
    assert result["checks"][0]["passed"] is passed
    if item is not None:
        tier = build_tier_report(
            {"block": "coverage", "kind": "analog", "evidence": {str(item): str(path)}}
        )
        row = next(row for row in tier["items"] if row["id"] == item)
        assert (row["status"] == "met") is passed
    return result


@pytest.mark.parametrize(
    "present,violating", [(False, False), (True, False), (True, True)]
)
def test_curated_drc_executes_real_rule_and_refuses_zero(
    tmp_path, monkeypatch, capsys, present, violating
):
    rule = DrcRule(
        id="test.width",
        description="synthetic 0.2 um width",
        check="width",
        layer=(1, 0),
        threshold_dbu=200,
    )
    monkeypatch.setattr(drc, "get_deck", lambda _: [rule])
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    if present:
        top.shapes(layout.layer(1, 0)).insert(
            kdb.Box(0, 0, 100 if violating else 500, 1000)
        )
    gds = tmp_path / "drc.gds"
    layout.write(str(gds))
    report = drc.run_drc(str(gds), "sky130")
    _assert_contract(report, "full" if present else "zero")
    assert report["status"] == (
        "violations" if violating else "clean" if present else "not_checked"
    )
    assert report["coverage"]["checked"] == (["test.width"] if present else [])
    _assert_signoff(tmp_path, report, passed=present and not violating, item=3)
    assert main(["drc", str(gds), "--deck", "sky130", "--format", "json"]) == (
        3 if violating else 0 if present else 4
    )
    assert json.loads(capsys.readouterr().out)["status"] == report["status"]


@pytest.mark.parametrize(
    "rdb",
    [
        klayout_fixtures._EMPTY_RDB,
        klayout_fixtures._CLEAN_WITH_CATEGORIES_RDB,
        klayout_fixtures._EDGE_PAIR_RDB,
    ],
)
def test_external_drc_categories_are_unknown_not_execution(tmp_path, monkeypatch, rdb):
    klayout_fixtures._stub_klayout_drc_subprocess(monkeypatch, rdb_xml=rdb)
    gds = klayout_fixtures._write_gds(tmp_path / "drc.gds")
    deck = klayout_fixtures._write_deck_file(tmp_path / "deck.lydrc")
    report = drc.run_drc_klayout_engine(gds, deck)
    _assert_contract(report, "unknown")
    assert report["coverage"]["nothing_checked"] is False
    assert report["status"] == (
        "violations" if report["violation_count"] else "coverage_unknown"
    )
    _assert_signoff(tmp_path, report, passed=False, item=3)


@pytest.mark.parametrize(
    "pdk,partial,violate,state,status,exit_code",
    [
        (None, False, False, "zero", "not_checked", 4),
        ("sky130", False, False, "full", "clean", 0),
        ("sky130", True, False, "partial", "clean", 0),
        ("sky130", True, True, "partial", "violations", 3),
    ],
)
def test_erc_antenna_actual_levels(
    tmp_path, capsys, pdk, partial, violate, state, status, exit_code
):
    gds, spec = tmp_path / "erc.gds", tmp_path / "erc.json"
    erc_fixtures._antenna_fixture(
        gds, li1_um2=80.0 if violate else 0.2, met1_um2=1.0, met2_um2=1.0
    )
    (erc_fixtures._full_stack_spec if partial else erc_fixtures._basic_spec)(spec)
    report = erc.run_erc(str(gds), str(spec), pdk=pdk)
    _assert_contract(report, state)
    assert report["status"] == status
    assert len(report["coverage"]["inapplicable"]) == 1
    assert report["coverage"]["inapplicable"][0]["reason"] == "gate_reference_level"
    if not partial:
        assert len(report["coverage"]["checked"]) == (3 if pdk else 0)
    args = ["erc", str(gds), str(spec), "--format", "json"]
    if pdk:
        args.extend(["--pdk", pdk])
    assert main(args) == exit_code
    assert json.loads(capsys.readouterr().out)["status"] == status


@pytest.mark.parametrize(
    "limit,state,status,code",
    [
        (None, "zero", "not_checked", 4),
        (0.01, "full", "pass", 0),
        (0.0001, "full", "fail", 3),
    ],
)
def test_power_real_current_and_declared_limit(
    tmp_path, capsys, limit, state, status, code
):
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(1, 0)).insert(kdb.Box(0, 0, 10000, 1000))
    top.shapes(layout.layer(1, 5)).insert(kdb.Text("VPWR", kdb.Trans(1000, 500)))
    gds, spec = tmp_path / "power.gds", tmp_path / "power.json"
    layout.write(str(gds))
    power_fixtures._em_spec(
        spec,
        power_nets=("VPWR",),
        pads=[
            {"name": "vdd", "net": "VPWR", "x_um": 0.0, "y_um": 0.5, "voltage_v": 1.8}
        ],
        current_model={
            "supply_net": "VPWR",
            "instances": [{"x_um": 10.0, "y_um": 0.5, "current_a": 0.001}],
        },
        met1_current_limit_a_per_um=limit,
    )
    report = power.run_power(str(gds), str(spec))
    _assert_contract(report, state)
    assert report["status"] == status
    assert len(report["coverage"]["checked"]) == (1 if limit else 0)
    _assert_signoff(tmp_path, report, passed=status == "pass")
    assert main(["power", str(gds), str(spec), "--format", "json"]) == code
    assert json.loads(capsys.readouterr().out)["status"] == status


@pytest.mark.parametrize(
    "measurements,state,status,code",
    [
        ([], "zero", "not_checked", 4),
        ([{"name": "vout", "limits": {"maximum": 2.0}}], "zero", "not_checked", 4),
        ([{"name": "vout"}], "full", "pass", 0),
        ([{"name": "vout", "limits": {"max": 2.0}}], "full", "pass", 0),
        (
            [{"name": "vout", "limits": {"max": 2.0, "maximum": 2.0}}],
            "partial",
            "pass",
            0,
        ),
        (
            [{"name": "vout", "limits": {"max": 0.5, "maximum": 2.0}}],
            "partial",
            "fail",
            3,
        ),
    ],
)
def test_sim_real_producer_measurement_coverage(
    tmp_path, monkeypatch, capsys, measurements, state, status, code
):
    sim_fixtures._write_body(tmp_path)
    request = sim_fixtures._write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {**m, "spice": f".meas tran {m['name']} FIND v(out) AT=1u"}
                for m in measurements
            ],
        },
    )
    sim_fixtures._stub_subprocess_run(monkeypatch, log_text="vout = 1.00000e+00\n")
    report = sim.run_sim(str(request))
    _assert_contract(report, state)
    assert report["status"] == status
    _assert_signoff(tmp_path, report, passed=status == "pass", item=1)
    assert main(["sim", str(request), "--format", "json"]) == code
    assert json.loads(capsys.readouterr().out)["status"] == status


@pytest.mark.parametrize(
    "mode,state,status,passed",
    [
        ("empty", "zero", "not_checked", False),
        ("pass", "full", "pass", True),
        ("partial", "partial", "pass", True),
        ("error", "zero", "error", False),
        ("fail", "full", "fail", False),
    ],
)
def test_pex_real_extraction_and_comparison_rollup(
    tmp_path, monkeypatch, capsys, mode, state, status, passed
):
    gds = pex_fixtures._write_gds(
        pex_fixtures._make_sky130_poly_resistor_layout(), tmp_path / "res.gds"
    )
    dut = pex_fixtures._write_schematic_dut(tmp_path / "dut.spice")
    tb = pex_fixtures._write_testbench(tmp_path / "tb.spice", dut)
    request = pex_fixtures._write_request(tmp_path / "request.json", tb)
    calls = []

    def fake_sim(*args, **kwargs):
        calls.append(args)
        if mode == "empty" or (mode == "partial" and (len(calls) - 1) % 4 >= 2):
            return {"corner_count": 0, "corners": [], "measurements": []}
        value = (
            None
            if mode == "error" and len(calls) % 2 == 0
            else 2.0
            if mode == "fail" and len(calls) % 2 == 0
            else 1.0
        )
        return dict(
            corner_count=1,
            measurements=[{"name": "vout"}],
            corners=[
                {
                    "corner_id": "tt/1.800V/27C",
                    "measurements": [
                        pex_fixtures._measurement(
                            value,
                            "error"
                            if value is None
                            else "fail"
                            if mode == "fail" and len(calls) % 2 == 0
                            else "pass",
                        )
                    ],
                }
            ],
        )

    monkeypatch.setattr(pex, "run_sim", fake_sim)
    requests = [str(request)]
    if mode == "partial":
        second = tmp_path / "empty.json"
        second.write_bytes(request.read_bytes())
        requests.append(str(second))
    report = pex.run_pex(gds, requests, "sky130")
    _assert_contract(report, state)
    assert report["status"] == status
    _assert_signoff(tmp_path, report, passed=passed, item=7)
    assert main(["pex", gds, *requests, "--deck", "sky130", "--format", "json"]) == (
        0 if passed else 3 if status == "fail" else 4
    )
    assert json.loads(capsys.readouterr().out)["status"] == status


@pytest.mark.parametrize("state", ["zero", "unknown", "malformed"])
@pytest.mark.parametrize("failed", [False, True])
def test_signoff_common_contract_cannot_be_hidden_by_success_status(
    tmp_path, state, failed
):
    report = copy.deepcopy(DRC_CLEAN_ENVELOPE)
    report["coverage"] = build_check_coverage(
        checked=[],
        unknown=[{"id": "engine", "reason": "unmeasured_rule_execution"}]
        if state == "unknown"
        else None,
    )
    if state == "malformed":
        report["coverage"]["checked"] = "not an array"
    if failed:
        report["status"] = "violations"
        report["violation_count"] = 1
    result = _assert_signoff(tmp_path, report, passed=False, item=3)
    assert result["checks"][0]["detail"]["coverage_state"] == state
    path = tmp_path / "evidence.json"
    tier = build_tier_report(
        {"block": "coverage", "kind": "analog", "evidence": {"3": str(path)}}
    )
    row = next(row for row in tier["items"] if row["id"] == 3)
    assert row["reason"] == (
        "check_failed"
        if failed
        else {
            "zero": "nothing_checked",
            "unknown": "coverage_unknown",
            "malformed": "malformed_coverage",
        }[state]
    )


@pytest.mark.parametrize(
    "coverage", [None, {}, {"rules_checked": ["DECLARED"], "nothing_checked": False}]
)
def test_legacy_external_declarations_never_qualify(tmp_path, coverage):
    report = copy.deepcopy(DRC_CLEAN_ENVELOPE)
    report["engine"] = "klayout"
    report["coverage"] = coverage
    assert coverage_state(report) == "unknown"
    _assert_signoff(tmp_path, report, passed=False, item=3)


def test_empty_deck_and_empty_simulation_matrix_are_known_zero(tmp_path, monkeypatch):
    layout = kdb.Layout()
    layout.create_cell("TOP")
    gds = tmp_path / "empty.gds"
    layout.write(str(gds))
    monkeypatch.setattr(drc, "get_deck", lambda _: [])
    report = drc.run_drc(str(gds), "sky130")
    _assert_contract(report, "zero")
    assert report["coverage"]["nothing_checked_reasons"] == ["deck_has_no_rules"]
    _assert_signoff(tmp_path, report, passed=False, item=3)
    sim_fixtures._write_body(tmp_path)
    request = sim_fixtures._write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "corners": {"supply_v": {"vdd": []}},
        },
    )
    sim_fixtures._stub_subprocess_run(monkeypatch)
    report = sim.run_sim(str(request))
    _assert_contract(report, "zero")
    assert report["status"] == "not_checked"
    assert report["coverage"]["nothing_checked_reasons"] == ["empty_corner_matrix"]
    _assert_signoff(tmp_path, report, passed=False, item=1)
