"""Windowed transient expressions are expanded before simulator dispatch."""

import json

import pytest

import test_sim_batch as tb
from helpers.tool_probes import SKIP_NO_NGSPICE
from klayout_tools import sim
from klayout_tools.sim_measurement_template import expand_request


def _request(**body):
    return {
        "netlist": "body.spice",
        "analysis": {"kind": "tran", "args": "10n 10u"},
        "measurements": [
            {
                "name": "window",
                "unit": "V",
                "limits": {"min": 2.9, "max": 3.1},
                "plausible_range": {"min": 0, "max": 5},
                "windowed": {
                    "of": "v(in)+v(ref)",
                    "reduce": "avg",
                    "from": "7u",
                    "to": "9u",
                    **body,
                },
            }
        ],
    }


@pytest.mark.parametrize("reduce", ["min", "max", "avg", "rms", "pp"])
def test_expand_exact_card_and_keep_grading_fields(reduce):
    request = _request(reduce=reduce)
    spec = request["measurements"][0]
    (actual,) = expand_request(request)["measurements"]
    assert actual == {k: v for k, v in spec.items() if k != "windowed"} | {
        "spice": f".meas tran window {reduce.upper()} par('v(in)+v(ref)') FROM=7u TO=9u"
    }
    assert "windowed" in spec


@pytest.mark.parametrize(
    "body,match",
    [
        ({"reduce": []}, "reduce"),
        ({"reduce": "sum"}, "reduce"),
        ({"of": 'v("a")'}, "of"),
        ({"of": "v(a)\nv(b)"}, "of"),
        ({"of": "v({a})"}, "of"),
        ({"of": "v('a')"}, "of"),
        ({"from": None}, "from"),
        ({"to": True}, "to"),
        ({"from": "1meg"}, "from < to"),
        ({"from": 1, "to": 1}, "from < to"),
        ({"extra": 1}, "extra"),
    ],
)
def test_refuse_invalid_window(body, match):
    with pytest.raises(sim.SimError, match=f"measurement 'window'.*{match}"):
        expand_request(_request(**body))


@pytest.mark.parametrize("key", ["of", "from", "to", "reduce"])
def test_missing_body_key(key):
    request = _request()
    del request["measurements"][0]["windowed"][key]
    with pytest.raises(sim.SimError, match=f"measurement 'window'.*{key}"):
        expand_request(request)


def test_numeric_seconds():
    card = expand_request(_request(**{"from": 0, "to": 1e-5}))["measurements"][0][
        "spice"
    ]
    assert card.endswith("FROM=0 TO=1e-05")


@pytest.mark.parametrize("context", ["ac", "xyce", "steps", "inside_step", "spice"])
def test_refusal_through_public_request_loader(tmp_path, context):
    request = _request()
    if context == "ac":
        request["analysis"]["kind"] = "ac"
    elif context == "xyce":
        request["engine"] = "xyce"
    elif context in ("steps", "inside_step"):
        step = {"name": "ramp", "analysis": request.pop("analysis")}
        if context == "inside_step":
            step["measurements"] = request.pop("measurements")
        request["analysis_steps"] = [step]
    else:
        request["measurements"][0]["spice"] = ".meas tran foo MAX v(a)"
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    with pytest.raises(sim.SimError, match="measurement 'window'"):
        sim.load_request(str(path))


def test_forwarded_batch_request_only_carries_the_plain_card(tmp_path, monkeypatch):
    runner = tb._FakeRunner()
    runner.default = tb.fake_completed(tb._status("done"))
    runner.outputs = {"report.json": tb._sim_report()}
    tb._install_fake_batch_transport(monkeypatch, runner=runner)
    uploaded = tb._capture_uploads(monkeypatch, runner)
    (tmp_path / "body.spice").write_text("V1 in 0 1\nR1 in 0 1k\n")
    request = tb._batch_request(tmp_path)
    request["measurements"] = _request()["measurements"]
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    sim.run_sim(str(path))
    assert (
        json.loads(uploaded["request.json"])["measurements"]
        == expand_request(request)["measurements"]
    )


@SKIP_NO_NGSPICE
def test_real_ngspice_reduces_only_the_requested_interval(tmp_path):
    (tmp_path / "body.spice").write_text(
        "Vsig in 0 PWL(0 0 5u 0 5.1u 2 10u 2)\nVref ref 0 1\nR1 in 0 1k\nR2 ref 0 1k\n"
    )
    request = _request()
    request["measurements"].append(
        {
            "name": "whole",
            "spice": ".meas tran whole AVG par('v(in)+v(ref)') FROM=0 TO=10u",
        }
    )
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    report = sim.run_sim(str(path))
    (corner,) = report["corners"]
    window, whole = corner["measurements"]
    assert corner["status"] == window["status"] == "pass"
    assert window["unit"] == "V"
    assert window["value"] == pytest.approx(3.0)
    assert 1.9 < whole["value"] < 2.1


@pytest.mark.parametrize(
    "spec",
    [
        {"name": "bad name", "windowed": {}},
        {"name": "bad", "windowed": []},
    ],
)
def test_reject_bad_name_or_body(spec):
    request = _request()
    request["measurements"] = [spec]
    with pytest.raises(sim.SimError, match="measurement 'bad"):
        expand_request(request)


def test_unrelated_entries_remain_identical():
    request = {"analysis_steps": "invalid elsewhere", "measurements": [None]}
    assert expand_request(request) is request
