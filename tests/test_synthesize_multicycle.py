"""Mapped-netlist multicycle analysis through synthesis and standalone STA."""

import json
import os
import shutil
from pathlib import Path

import pytest

from klayout_tools import post_route_sta, synthesize
from klayout_tools.cli import main
from klayout_tools.synthesize_constrained_timing import (
    constrained_timing_fields,
    mapped_sta_constraints,
)

CLOCK = {"port": "clk", "period_ns": 2.0}
FEEDBACK = {"from": "ff_fb", "to": "ff_fb", "setup": 4, "hold": 3}
PDK = {"cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80"}


def _write_request(tmp_path, constraints):
    request = {
        "schema": "klt.synthesize.request/1",
        "hdl_toplevel": "top",
        "sources": ["feedback.v"],
        "pdk": PDK,
        "constraints": constraints,
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return str(path)


@pytest.mark.parametrize(
    ("constraints", "message"),
    [
        ({"clock_period_ns": 2, "multicycle_paths": [FEEDBACK]}, "clock port"),
        (
            {
                "clocks": [CLOCK],
                "multicycle_paths": [FEEDBACK, {**FEEDBACK, "hold": 0}],
            },
            "multicycle_paths[1].hold",
        ),
    ],
)
def test_malformed_request_fails_before_library_or_yosys(
    tmp_path, monkeypatch, capsys, constraints, message
):
    (tmp_path / "feedback.v").write_text("module top(); endmodule\n")
    monkeypatch.setattr(
        synthesize, "_resolve_liberty", lambda *a, **kw: pytest.fail("late validation")
    )
    assert (
        main(["synthesize", _write_request(tmp_path, constraints), "--format", "json"])
        == 1
    )
    assert message in json.loads(capsys.readouterr().err)["error"]["message"]


@pytest.mark.parametrize("constraints", [None, {}, {"multicycle_paths": []}])
def test_no_entries_need_no_sta_engine(tmp_path, constraints):
    assert mapped_sta_constraints(constraints) is None
    assert (
        constrained_timing_fields(
            None,
            netlist_path=str(tmp_path / "top.v"),
            hdl_toplevel="top",
            cell_library=PDK["cell_library"],
            corner=PDK["corner"],
            macros=[],
            pdk_root=None,
            pdk_variant=None,
        )
        == {}
    )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("engine_fails", [False, True])
def test_sta_receives_named_clocks_resolved_macro_paths_and_all_entries(
    tmp_path, monkeypatch, engine_fails
):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    clock2 = {"port": "other", "period_ns": 3}
    entries = [FEEDBACK, {**FEEDBACK, "to": "ff_adj"}]
    constraints = mapped_sta_constraints(
        {"clock_period_ns": 1, "clocks": [CLOCK, clock2], "multicycle_paths": entries}
    )
    macro = {
        "cell": "mem",
        "lef": str(tmp_path / "mem.lef"),
        "lib": {PDK["corner"]: str(tmp_path / "mem.lib")},
        "gds": None,
        "verilog_blackbox": None,
    }

    records = [
        {**entry, "index": index, "matched_endpoints": 1}
        for index, entry in enumerate(entries)
    ]
    metrics = {
        "geometry_source": "netlist_estimate",
        "worst_slack_ns": -0.125,
        "worst_hold_slack_ns": 0.5,
        "timing_status": "constrained",
        "multicycle_paths": records,
    }

    def measure(path, *, pdk_root, pdk_variant):
        saved = json.loads(Path(path).read_text())
        assert saved == {
            "verilog": "mapped.v",
            "hdl_toplevel": "top",
            "pdk": PDK,
            "constraints": {"clocks": [CLOCK, clock2], "multicycle_paths": entries},
            "macros": [macro],
        }
        assert (pdk_root, pdk_variant) == (str(tmp_path), "sky130A")
        if engine_fails:
            raise post_route_sta.PostRouteStaError("openroad executable is missing")
        return {**metrics, "schema": "klt.sta.response/1"}

    monkeypatch.setattr(post_route_sta, "run_sta", measure)

    def analyze():
        return constrained_timing_fields(
            constraints,
            netlist_path=str(run_dir / "mapped.v"),
            hdl_toplevel="top",
            cell_library=PDK["cell_library"],
            corner=PDK["corner"],
            macros=[macro],
            pdk_root=str(tmp_path),
            pdk_variant="sky130A",
        )

    if engine_fails:
        with pytest.raises(
            synthesize.SynthesizeError,
            match=r"multicycle_paths\[0\].*multicycle_paths\[1\].*openroad",
        ):
            analyze()
    else:
        assert analyze() == {
            "constrained_timing": {
                "basis": "klt sta Verilog mode on the mapped netlist",
                **metrics,
            }
        }


@pytest.fixture
def feedback_design(tmp_path, monkeypatch):
    root = os.environ.get("PDK_ROOT")
    if (
        not root
        or not (
            Path(root)
            / "sky130A/libs.ref/sky130_fd_sc_hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib"
        ).is_file()
    ):
        pytest.skip("requires the SKY130 HD TT Liberty in PDK_ROOT")
    if os.environ.get("KLT_SKIP_OPENROAD_TESTS") == "1" or not all(
        shutil.which(tool) for tool in ("openroad", "yosys")
    ):
        pytest.skip("requires Yosys and OpenROAD")
    monkeypatch.setenv("PDK", "sky130A")
    # Keep named register endpoints and a hierarchical delay line. Yosys
    # must still synthesize the behavioral enable mux and emit the netlist.
    stages = 60
    cells = "\n".join(
        f"  sky130_fd_sc_hd__inv_1 u{i} (.A(t[{i}]), .Y(t[{i + 1}]));"
        for i in range(stages)
    )
    (tmp_path / "feedback.v").write_text(f"""module delay_line(input a, output y);
  wire [{stages}:0] t;
  assign t[0] = a;
{cells}
  assign y = t[{stages}];
endmodule
module top(input clk, input en, input d, output q, output next_q);
  wire delayed;
  delay_line chain (.a(q), .y(delayed));
  sky130_fd_sc_hd__dfxtp_1 ff_fb (.CLK(clk), .D(en ? delayed : d), .Q(q));
  sky130_fd_sc_hd__dfxtp_1 ff_adj (.CLK(clk), .D(q), .Q(next_q));
endmodule
""")
    return tmp_path


def test_live_synthesis_matches_sta_on_the_emitted_feedback_netlist(feedback_design):
    tmp_path = feedback_design
    constraints = {"clocks": [CLOCK], "multicycle_paths": [FEEDBACK]}
    path = _write_request(tmp_path, constraints)
    result = synthesize.run_synthesize(path)
    timing = result["constrained_timing"]
    assert timing["geometry_source"] == "netlist_estimate"
    assert "mapped netlist" in timing["basis"]
    netlist = tmp_path / ".klt/synthesize" / result["run_id"] / "top_synth.v"
    sta_request = {
        "verilog": str(netlist),
        "hdl_toplevel": "top",
        "pdk": PDK,
        "constraints": {**constraints, "report_instances": ["ff_fb", "ff_adj"]},
    }
    sta_path = tmp_path / "independent-sta.json"
    sta_path.write_text(json.dumps(sta_request))
    standalone = post_route_sta.run_sta(str(sta_path))
    for key in (
        "multicycle_paths",
        "worst_slack_ns",
        "worst_hold_slack_ns",
        "timing_status",
        "geometry_source",
    ):
        assert timing[key] == standalone[key]
    (record,) = timing["multicycle_paths"]
    assert record["matched_endpoints"] == 1
    assert record["setup_slack_ns"]["without"] < 0 < record["setup_slack_ns"]["with"]
    assert record["hold_slack_ns"]["with"] == pytest.approx(
        record["hold_slack_ns"]["without"], abs=1e-6
    )
    del sta_request["constraints"]["multicycle_paths"]
    sta_path.write_text(json.dumps(sta_request))
    single_cycle = post_route_sta.run_sta(str(sta_path))
    assert (
        standalone["instance_paths"]["ff_adj"]["into"]["setup"]
        == single_cycle["instance_paths"]["ff_adj"]["into"]["setup"]
    )


def test_live_unmatched_selector_is_an_indexed_request_error(feedback_design, capsys):
    tmp_path = feedback_design
    bad = {**FEEDBACK, "from": "absent_from_mapping"}
    assert (
        main(
            [
                "synthesize",
                _write_request(
                    tmp_path, {"clocks": [CLOCK], "multicycle_paths": [bad]}
                ),
                "--format",
                "json",
            ]
        )
        == 1
    )
    error = json.loads(capsys.readouterr().err)["error"]["message"]
    assert "multicycle_paths[0].from matches no register or pin" in error


def test_live_optional_engine_and_unchanged_synthesis_measurements(
    feedback_design, monkeypatch, capsys
):
    tmp_path = feedback_design
    clocks = {"clocks": [CLOCK]}
    path = _write_request(tmp_path, {**clocks, "multicycle_paths": [FEEDBACK]})
    measured = synthesize.run_synthesize(path)

    def unavailable(*args, **kwargs):
        raise post_route_sta.PostRouteStaError("openroad executable is missing")

    monkeypatch.setattr(post_route_sta, "run_sta", unavailable)
    assert main(["synthesize", path, "--format", "json"]) == 1
    message = json.loads(capsys.readouterr().err)["error"]["message"]
    assert (
        "multicycle_paths[0]" in message and "openroad executable is missing" in message
    )

    for constraints in (clocks, {**clocks, "multicycle_paths": []}):
        plain = synthesize.run_synthesize(_write_request(tmp_path, constraints))
        assert "constrained_timing" not in plain
        for key in ("sta", "timing", "clock_targets"):
            assert plain[key] == measured[key]
