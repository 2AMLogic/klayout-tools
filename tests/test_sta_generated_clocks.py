"""Generated clock validation, linkage, engine evidence and legacy control."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from klayout_tools import _sta_generated_clocks as generated
from klayout_tools import post_route_sta as sta
from test_post_route_sta import (
    _setup_success_env,
    _setup_verilog_success_env,
    _stub_openroad_success,
)

_ENTRY = {"source": "clk", "pin": "div_ff/Q", "divide_by": 2}
_FIXTURE = Path(__file__).parent / "fixtures" / "sta_generated_clocks"


def _extras(entries):
    return sta._validate_clock_extras(
        {"clock_port": "clk", "generated_clocks": entries}
    )


@pytest.mark.parametrize(
    "entry,diagnostic",
    [
        ([], "object"),
        ({"source": "clk", "pin": "p"}, "exactly one"),
        (_ENTRY | {"edges": [1, 2, 3]}, "exactly one"),
        (_ENTRY | {"divide_by": 1}, "divide_by"),
        (_ENTRY | {"divide_by": 2.5}, "divide_by"),
        (_ENTRY | {"divide_by": True}, "divide_by"),
        (_ENTRY | {"source": "miss"}, "source"),
        (_ENTRY | {"source": "klt_gen_clk_0"}, "source"),
        (_ENTRY | {"name": "derived"}, "unknown"),
        (_ENTRY | {"pin": ""}, "literal"),
        (_ENTRY | {"pin": "div*/Q"}, "literal"),
        (_ENTRY | {"pin": "div_ff[abc]/Q"}, "literal"),
        (_ENTRY | {"pin": "div_ff /Q"}, "literal"),
        (_ENTRY | {"pin": "-quiet"}, "literal"),
        (_ENTRY | {"pin": "div;$clk"}, "literal"),
    ],
)
def test_invalid_declaration_names_entry(entry, diagnostic):
    with pytest.raises(
        sta.PostRouteStaError, match=rf"generated_clocks\[0\].*{diagnostic}"
    ):
        _extras([entry])


@pytest.mark.parametrize(
    "edges",
    [[1, 3], [1, 2, 3, 4], [0, 1, 2], [1, 3, 3], [3, 2, 1], [True, 2, 3], "1 3 5"],
)
def test_invalid_edges(edges):
    with pytest.raises(sta.PostRouteStaError, match=r"generated_clocks\[0\].edges"):
        _extras([{"source": "clk", "pin": "div/Q", "edges": edges}])


def test_duplicate_pin_reserved_name_missing_clock_and_invalid_array():
    with pytest.raises(sta.PostRouteStaError, match=r"generated_clocks\[1\].pin"):
        _extras([_ENTRY, _ENTRY])
    with pytest.raises(sta.PostRouteStaError, match="generated name"):
        generated.validate({"generated_clocks": [_ENTRY]}, {"clk", "klt_gen_clk_0"})
    with pytest.raises(sta.PostRouteStaError, match=r"generated_clocks\[0\]"):
        sta._validate_constraints({"clock_period_ns": 10, "generated_clocks": [_ENTRY]})
    with pytest.raises(sta.PostRouteStaError, match="array"):
        _extras(None)


def test_secondary_source_and_multiple_clocks():
    extras = sta._validate_clock_extras(
        {
            "clocks": [
                {"port": "clk", "period_ns": 10},
                {"port": "aux", "period_ns": 12},
            ],
            "generated_clocks": [
                _ENTRY,
                {"source": "aux", "pin": "divider[2]/Q", "edges": [1, 3, 5]},
            ],
        }
    )
    script = "\n".join(sta._extra_clock_lines(extras))
    assert script.index("create_clock -name aux") < script.index("get_pins -quiet")
    assert script.index(r"divider\[2\]/Q") < script.index("create_generated_clock")
    assert "-divide_by 2 $klt_gen_pin_0" in script
    assert "-edges {1 3 5} $klt_gen_pin_1" in script


@pytest.mark.parametrize("matches", ["", "one two"])
def test_all_pins_are_resolved_before_creating_any_clock(matches):
    tcl = shutil.which("tclsh")
    if not tcl:
        pytest.skip("tclsh unavailable")
    rows = _extras([_ENTRY, _ENTRY | {"pin": "other/Q"}])["generated_clocks"]
    prelude = [
        "proc get_ports {args} {return {clk}}",
        "proc get_pins {flag query} {",
        f'    if {{[lindex $query 0] eq "other/Q"}} {{return {{{matches}}}}}',
        "    return {pin}",
        "}",
        "proc create_generated_clock {args} {puts CREATED}",
    ]
    lines = generated.constraint_lines(
        rows, sta._PORT_CHECK_BEGIN, sta._PORT_CHECK_END, sta._PORT_CHECK_EXIT_CODE
    )
    result = subprocess.run(
        [tcl], input="\n".join(prelude + lines), text=True, capture_output=True
    )
    assert (
        result.returncode == sta._PORT_CHECK_EXIT_CODE
        and "CREATED" not in result.stdout
    )
    errors = sta._parse_port_check_errors(result.stdout)
    assert (
        generated.link_error(errors)
        == f"constraints.generated_clocks[1].pin: other/Q matches "
        f"{len(matches.split())} linked pins"
    )


@pytest.mark.usefixtures("real_build_identity_git")
@pytest.mark.parametrize("setup", [_setup_success_env, _setup_verilog_success_env])
def test_omitted_and_empty_keep_script_and_response(tmp_path, monkeypatch, setup):
    path = Path(setup(tmp_path, monkeypatch))
    _stub_openroad_success(monkeypatch)
    before = sta.run_sta(str(path))
    script = next(tmp_path.rglob("sta_*.tcl"))
    original = script.read_bytes()
    request = json.loads(path.read_text())
    request["constraints"]["generated_clocks"] = []
    path.write_text(json.dumps(request))
    after = sta.run_sta(str(path))
    assert script.read_bytes() == original
    assert b"create_generated_clock" not in original

    # OpenROAD retention assigns a fresh log identifier and directory to each run.
    def stable_report(report):
        return json.dumps(report).replace(
            report["engine_log"]["invocation_id"], "<run>"
        )

    assert stable_report(before) == stable_report(after)


def test_unavailable_measurement_is_distinct_from_a_measured_zero():
    extras = _extras([_ENTRY])
    blank = generated.response(extras, "")["generated_clocks"][0]
    assert blank["period_ns"] is None and blank["clocked_registers"] is None
    assert "period_ns" in blank["reason"] and "clocked_registers" in blank["reason"]
    partial = "\n".join([generated._BEGIN, "klt_gen_clk_0\t0.0\t0", generated._END])
    row = generated.response(extras, partial)["generated_clocks"][0]
    assert row["period_ns"] is None and row["clocked_registers"] == 0
    assert row["reason"] == "OpenSTA did not report period_ns"


def test_captured_divider_sessions():
    capture = json.loads((_FIXTURE / "openroad_26Q3-1510.json").read_text())
    assert (_FIXTURE / capture["netlist"]).is_file()
    source = capture["source_clock_probes"]
    assert source["omitted"] == source["divide"] == ["10.000000", "2"]
    for case in capture["sessions"]:
        stdout = case["stdout"]
        completeness = sta._parse_constraint_completeness(stdout)
        assert completeness["unclocked_registers"]["sample"] == case["unclocked"]
        assert (
            completeness["unconstrained_endpoints"]["sample"] == case["unconstrained"]
        )
        extras = _extras(case["generated_clocks"])
        response = generated.response(extras, stdout)
        if case["generated_clocks"]:
            row = response["generated_clocks"][0]
            assert row["period_ns"] == 20 and row["clocked_registers"] == 1
        else:
            assert "generated_clocks" not in response
