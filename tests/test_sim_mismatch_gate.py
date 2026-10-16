"""Per-instance mismatch gating for ``monte_carlo.instances`` (issue #2995)."""

import pytest

from klayout_tools import sim
from klayout_tools import sim_mismatch_gate as gate

NETLIST = (
    ".param vdd=1.8\n"
    "Vdd vdd 0 DC {vdd}\n"
    "XM1 d g s b sky130_fd_pr__nfet_01v8 L=0.15 W=1\n"
    "+ m=4\n"
    "XM2 d2 g s b sky130_fd_pr__nfet_01v8 L=0.15 W=1 m=2\n"
    "R1 vdd d 1k\n"
    ".end\n"
)


def test_exclude_wraps_instance_and_keeps_m_inside_wrapper():
    text, rows = gate.gate_netlist(NETLIST, [], ["xm1"], "mc_mm_switch")

    assert {r["instance"]: r["randomised"] for r in rows} == {"XM1": False, "XM2": True}
    assert "XM1 d g s b klt_mmoff_XM1" in text
    assert ".param mc_mm_switch=0" in text
    # every parameter, including the continuation-line `m=4`, moved inside
    assert "L=0.15 W=1 m=4" in text
    assert ".subckt klt_mmoff_XM1 p0 p1 p2 p3" in text
    assert text.rstrip().endswith(".end")
    # the untouched instance is byte-identical
    assert "XM2 d2 g s b sky130_fd_pr__nfet_01v8 L=0.15 W=1 m=2" in text


def test_include_randomises_only_matches():
    _, rows = gate.gate_netlist(NETLIST, ["xm2"], [], "mc_mm_switch")

    assert {r["instance"]: r["randomised"] for r in rows} == {"XM1": False, "XM2": True}


def test_exclude_overrides_include_overlap():
    _, rows = gate.gate_netlist(NETLIST, ["xm*"], ["xm2"], "mc_mm_switch")

    assert {r["instance"]: r["randomised"] for r in rows} == {"XM1": True, "XM2": False}


def test_all_excluded_wraps_everything():
    text, rows = gate.gate_netlist(NETLIST, [], ["x*"], "sw_stat_mismatch")

    assert not any(r["randomised"] for r in rows)
    assert text.count(".param sw_stat_mismatch=0") == 2


def test_glob_matching_nothing_is_an_error():
    with pytest.raises(gate.GateError, match="matches no top-level X instance"):
        gate.gate_netlist(NETLIST, ["xq*"], [], "mc_mm_switch")


def test_glob_matching_non_subckt_element_is_an_error():
    with pytest.raises(gate.GateError, match="non-subcircuit"):
        gate.gate_netlist(NETLIST, [], ["r1"], "mc_mm_switch")


@pytest.mark.parametrize(
    "spec",
    [
        [],
        {},
        {"include": []},
        {"include": "xm1"},
        {"include": [""]},
        {"include": ["x1.xm1"]},
        {"bogus": ["xm1"]},
    ],
)
def test_invalid_specs_are_rejected(spec):
    with pytest.raises(gate.GateError):
        gate.validate_instances_spec(spec)


def _request(tmp_path, **mc_extra):
    import json

    (tmp_path / "body.spice").write_text(NETLIST)
    mc = {"n": 2, "seed": 1, "vary": "mismatch", **mc_extra}
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "models": {"pdk": "sky130A"},
                "monte_carlo": mc,
                "analysis": {"kind": "tran", "args": "1n 1u"},
            }
        )
    )
    return path


def test_run_sim_rejects_unknown_pdk(tmp_path):
    request = _request(tmp_path, instances={"exclude": ["xm1"]})
    import json

    doc = json.loads(request.read_text())
    doc["models"] = {"pdk": "not_a_real_pdk"}
    request.write_text(json.dumps(doc))

    with pytest.raises(sim.SimError, match="known mismatch switch"):
        sim.run_sim(str(request))


def test_run_sim_rejects_process_only_vary(tmp_path):
    request = _request(tmp_path, vary="process", instances={"exclude": ["xm1"]})

    with pytest.raises(sim.SimError, match="requires monte_carlo.vary"):
        sim.run_sim(str(request))


def test_run_sim_rejects_offhost_backend(tmp_path):
    request = _request(tmp_path, instances={"exclude": ["xm1"]})

    with pytest.raises(sim.SimError, match="only supported on local backends"):
        sim.run_sim(str(request), backend="batch")


def test_run_sim_reports_instances_and_runs_gated_netlist(tmp_path, monkeypatch):
    import re
    import subprocess

    included: list[str] = []

    real_run = subprocess.run

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        if "-o" not in cmd:
            return real_run(
                cmd, capture_output=capture_output, text=text, timeout=timeout, cwd=cwd
            )
        with open(cmd[cmd.index("-o") + 1], "w", encoding="utf-8") as handle:
            handle.write("")
        deck = next(c for c in cmd if str(c).endswith(".cir"))
        match = re.search(r"^\.include (\S+)", open(deck).read(), re.MULTILINE)
        included.append(open(match.group(1)).read())
        return subprocess.CompletedProcess(cmd, 0, "** ngspice-99\n", "")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)
    request = _request(tmp_path, instances={"exclude": ["xm1"]})

    report = sim.run_sim(str(request))

    gating = report["environment"]["monte_carlo"]["instance_mismatch"]
    assert gating["switch"] == "mc_mm_switch"
    assert gating["negative_control"] is False
    assert {r["instance"]: r["randomised"] for r in gating["instances"]} == {
        "XM1": False,
        "XM2": True,
    }
    assert included and all(".param mc_mm_switch=0" in t for t in included)
    # the reported netlist is still the user's own file
    assert report["environment"]["netlist_sha256"] != ""
