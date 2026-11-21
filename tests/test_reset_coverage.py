"""Tests for `klt reset-coverage` / `run_reset_coverage` (issue #2998).

Hermetic: the cell models and netlists are the tiny files under
`examples/reset-coverage/` plus inline variants -- no PDK install needed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools.cli import main
from klayout_tools.reset_coverage import ResetCoverageError, run_reset_coverage

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "reset-coverage"
MODELS = str(EXAMPLE / "cell_models.v")
NETLIST = str(EXAMPLE / "netlist.v")


def _by_inst(report: dict) -> dict:
    return {e["instance"]: e for e in report["elements"]}


def _waivers(tmp_path: Path, entries: list[dict]) -> str:
    path = tmp_path / "w.json"
    path.write_text(json.dumps({"waivers": entries}))
    return str(path)


def test_classification_reason_codes():
    report = run_reset_coverage(NETLIST, [MODELS], "rst_n")
    got = {k: (v["status"], v["reason"]) for k, v in _by_inst(report).items()}
    assert got == {
        "f1": ("covered", None),  # two inverters: polarity preserved
        "f2": ("unreset", "gated_by_logic"),
        "f3": ("unreset", "no_async_pin"),
        "f4": ("unreset", "wrong_polarity"),  # one inverter
        "f5": ("unreset", "tied_off"),
        "m0": ("unreset", "hard_macro"),
    }
    assert report["status"] == "fail"
    assert report["summary"]["covered"] == 1


def test_active_high_reset_flips_polarity():
    report = run_reset_coverage(NETLIST, [MODELS], "rst_n", reset_polarity="high")
    got = _by_inst(report)
    assert got["f4"]["status"] == "covered"
    assert got["f1"]["reason"] == "wrong_polarity"


def test_unlisted_fails_and_full_waiver_passes(tmp_path):
    unreset = ["f2", "f3", "f4", "f5", "m0"]
    entries = [{"instance": i, "justification": "reviewed"} for i in unreset]
    report = run_reset_coverage(
        NETLIST, [MODELS], "rst_n", waiver_path=_waivers(tmp_path, entries)
    )
    assert report["status"] == "pass"
    assert report["summary"]["waived"] == 5

    partial = run_reset_coverage(
        NETLIST, [MODELS], "rst_n", waiver_path=_waivers(tmp_path, entries[:-1])
    )
    assert partial["status"] == "fail"
    assert partial["summary"]["unwaived"] == 1


def test_stale_waiver_fails(tmp_path):
    unreset = ["f2", "f3", "f4", "f5", "m0"]
    entries = [{"instance": i, "justification": "ok"} for i in unreset]
    for stale in (
        {"instance": "nope", "justification": "x"},
        {"instance": "f1", "justification": "x"},  # covered, nothing to waive
        {"instance": "f2", "reason": "tied_off", "justification": "x"},
    ):
        extra = entries[1:] if stale["instance"] == "f2" else entries
        report = run_reset_coverage(
            NETLIST, [MODELS], "rst_n", waiver_path=_waivers(tmp_path, [*extra, stale])
        )
        assert report["status"] == "fail"
        assert report["summary"]["stale_waivers"] == 1


def test_waiver_without_justification_is_request_error(tmp_path):
    with pytest.raises(ResetCoverageError, match="justification"):
        run_reset_coverage(
            NETLIST,
            [MODELS],
            "rst_n",
            waiver_path=_waivers(tmp_path, [{"instance": "f2", "justification": " "}]),
        )


def test_bad_inputs():
    with pytest.raises(ResetCoverageError, match="not a port"):
        run_reset_coverage(NETLIST, [MODELS], "nope")
    with pytest.raises(ResetCoverageError, match="cannot read"):
        run_reset_coverage("/nonexistent.v", [MODELS], "rst_n")


def test_empty_netlist_of_comb_cells_passes(tmp_path):
    net = tmp_path / "c.v"
    net.write_text(
        "module t (a, b, y); input a, b; output y;\n"
        "sky130_fd_sc_hd__and2_1 u (.A(a), .B(b), .X(y));\nendmodule\n"
    )
    report = run_reset_coverage(str(net), [MODELS], "a")
    assert report["status"] == "pass" and report["elements"] == []


def test_cli_json_and_exit_codes(capsys):
    argv = ["reset-coverage", NETLIST, "--cell-models", MODELS, "--reset-port", "rst_n"]
    assert main([*argv, "--format", "json"]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1 and payload["status"] == "fail"
    assert (
        main(
            [
                "reset-coverage",
                "/nonexistent.v",
                "--cell-models",
                MODELS,
                "--reset-port",
                "rst_n",
                "--format",
                "json",
            ]
        )
        == 1
    )


def test_net_type_keywords_in_port_declarations_are_ignored(tmp_path):
    """The real sky130 models declare every port as `input wire CLK` /
    `output wire Q`; the net-type keyword must not become part of the pin
    name (it hid every async pin, classifying all flops `no_async_pin`)."""
    models = tmp_path / "models.v"
    models.write_text(
        "module sky130_fd_sc_hd__dfrtp_1 (Q, CLK, D, RESET_B);\n"
        "  output wire Q; input wire CLK; input wire D; input wire RESET_B;\n"
        "  wire buf_Q, RESET;\n"
        "  not not0 (RESET, RESET_B);\n"
        "  sky130_fd_sc_hd__udp_dff$PR_pp$PG$N dff0 (buf_Q, D, CLK, RESET);\n"
        "  buf buf0 (Q, buf_Q);\n"
        "endmodule\n"
    )
    netlist = tmp_path / "net.v"
    netlist.write_text(
        "module top (clk, rst_n, d, q);\n  input clk, rst_n, d; output q;\n"
        "  sky130_fd_sc_hd__dfrtp_1 f (.CLK(clk), .D(d), .RESET_B(rst_n), .Q(q));\n"
        "endmodule\n"
    )
    report = run_reset_coverage(str(netlist), [str(models)], "rst_n")
    assert _by_inst(report)["f"]["status"] == "covered"
