"""Direct unit tests for the shared `klayout_tools.verilog_cell_models` helpers
and cross-verb error-class / precedence preservation (issue #8046).
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from klayout_tools import functional_verification as fv
from klayout_tools.functional_verification import (
    FunctionalVerificationError,
    run_functional_verification,
)
from klayout_tools.power_intent import PowerIntentError, run_power_intent
from klayout_tools.reset_coverage import (
    ResetCoverageError,
    run_reset_coverage,
)
from klayout_tools.reset_coverage import (
    sequential_elements as compat_sequential_elements,
)
from klayout_tools.verilog_cell_models import (
    CAPTURE_PINS,
    CONST_NETS,
    POWER_PINS,
    SCAN_PIN_PAIRS,
    CellModel,
    load_cell_models,
    load_flat_modules,
    read_text,
    reject_hierarchical_instances,
    select_top_module,
    sequential_elements,
)


class _CustomError(Exception):
    pass


CELL_MODELS = """\
primitive custom_dff (output reg q, input d, input clk);
  table
    0 r : ? : 0;
    1 r : ? : 1;
    ? n : ? : -;
  endtable
endprimitive

primitive custom_comb (y, a, b);
  output y; input a, b;
  table
    0 0 : 0;
    0 1 : 1;
    1 ? : 1;
  endtable
endprimitive

module dff_r (
  output wire Q, input wire CLK, input wire D, input wire RESET_B,
  input wire VPWR, input wire VGND
);
  wire d_int;
  custom_dff u0 (Q, D, CLK);
endmodule

module scan_ff (Q, CLK, D, SCD, SCE);
  output Q; input CLK, D, SCD, SCE;
  sky130_fd_sc_hd__udp_dff$P dff0 (Q, D, CLK);
endmodule

module clk_gate (GCLK, CLK, SE);
  output GCLK; input CLK, SE;
  sky130_fd_sc_hd__udp_dlatch$P lat0 (GCLK, SE, CLK);
endmodule

module no_clk_ff (Q, D);
  output Q; input D;
  custom_dff u0 (Q, D, D);
endmodule

module comb_cell (Y, A, B);
  output Y; input A, B;
  custom_comb u0 (Y, A, B);
endmodule

module inv_cell (Y, A, VPWR, VGND);
  output Y; input A, VPWR, VGND;
  wire n;
  not u0 (n, A);
  sky130_fd_sc_hd__udp_pwrgood_pp$PG u1 (Y, n, VPWR, VGND);
endmodule

module blackbox_ports_only (A, B);
endmodule
"""


def test_public_vocabularies_and_read_text(tmp_path: Path) -> None:
    assert "VPWR" in POWER_PINS and "VSS" in POWER_PINS
    assert CONST_NETS == {"__CONST0__": 0, "__CONST1__": 1}
    assert CAPTURE_PINS == ("CLK", "GATE", "CLK_N", "GATE_N")
    assert SCAN_PIN_PAIRS == (("SCD", "SCE"), ("SI", "SE"))

    sample = tmp_path / "sample.txt"
    sample.write_text("hello", encoding="utf-8")
    assert read_text(str(sample), "sample", error_cls=_CustomError) == "hello"

    missing = tmp_path / "missing.txt"
    with pytest.raises(_CustomError, match="cannot read sample"):
        read_text(str(missing), "sample", error_cls=_CustomError)


def test_load_cell_models_classifications_and_errors(tmp_path: Path) -> None:
    first = tmp_path / "models.v"
    first.write_text(CELL_MODELS, encoding="utf-8")
    # First definition of a module wins across files.
    second = tmp_path / "override.v"
    second.write_text(
        "module comb_cell (Q, CLK, D); output Q; input CLK, D;\n"
        "  sky130_fd_sc_hd__udp_dff$P u (Q, D, CLK);\nendmodule\n",
        encoding="utf-8",
    )
    models = load_cell_models([str(first), str(second)], error_cls=_CustomError)
    assert isinstance(models["dff_r"], CellModel)
    assert models["dff_r"].sequential is True
    assert models["dff_r"].async_pins == {"RESET_B": ("reset", 0)}
    assert models["comb_cell"].sequential is False
    assert models["inv_cell"].passthrough_parity == 1
    assert models["blackbox_ports_only"].inputs == ["A", "B"]
    assert models["blackbox_ports_only"].outputs == []

    empty = tmp_path / "empty.v"
    empty.write_text("// only comments\n", encoding="utf-8")
    with pytest.raises(_CustomError, match="no module found in the cell-model file"):
        load_cell_models([str(empty)], error_cls=_CustomError)


def test_flat_module_loading_top_selection_and_hierarchy_refusal(
    tmp_path: Path,
) -> None:
    bad = tmp_path / "bad.v"
    bad.write_text("module broken (\n", encoding="utf-8")
    with pytest.raises(_CustomError, match="cannot parse netlist"):
        load_flat_modules(str(bad), error_cls=_CustomError)

    hier = tmp_path / "hier.v"
    hier.write_text(
        "module leaf (a, y);\n"
        "  input a; output y;\n"
        "  inv_cell u0 (.A(a), .Y(y));\n"
        "endmodule\n"
        "module top (a, y);\n"
        "  input a; output y;\n"
        "  leaf u_leaf (.a(a), .y(y));\n"
        "  sram_macro u_mem (.A(a));\n"
        "endmodule\n",
        encoding="utf-8",
    )
    models_file = tmp_path / "models.v"
    models_file.write_text(CELL_MODELS, encoding="utf-8")
    models = load_cell_models([str(models_file)], error_cls=_CustomError)
    modules = load_flat_modules(str(hier), error_cls=_CustomError)

    with pytest.raises(_CustomError, match="netlist has 2 modules; name the top"):
        select_top_module(modules, None, error_cls=_CustomError)

    with pytest.raises(_CustomError, match="top module 'missing' not in netlist"):
        select_top_module(modules, "missing", error_cls=_CustomError)

    leaf_mod, names = select_top_module(modules, "leaf", error_cls=_CustomError)
    assert leaf_mod["name"] == "leaf" and names == {"leaf", "top"}
    reject_hierarchical_instances(leaf_mod, names, models, error_cls=_CustomError)

    single_mod, _ = select_top_module([leaf_mod], None, error_cls=_CustomError)
    assert single_mod["name"] == "leaf"

    top_mod, names = select_top_module(modules, "top", error_cls=_CustomError)
    with pytest.raises(
        _CustomError,
        match="instance 'u_leaf' is of module 'leaf' defined in the netlist; "
        "hierarchical netlists are not supported -- flatten first",
    ):
        reject_hierarchical_instances(top_mod, names, models, error_cls=_CustomError)


def test_sequential_elements_reasons_and_error_factory(tmp_path: Path) -> None:
    models_file = tmp_path / "models.v"
    models_file.write_text(CELL_MODELS, encoding="utf-8")
    netlist = tmp_path / "net.v"
    netlist.write_text(
        "module top (clk, rst_n, d, se, q0, q1, q2, q3, y);\n"
        "  input clk, rst_n, d, se; output q0, q1, q2, q3, y;\n"
        "  dff_r f_ok (.CLK(clk), .D(d), .RESET_B(rst_n), .Q(q0));\n"
        "  scan_ff f_scan (.CLK(clk), .D(d), .SCD(d), .SCE(se), .Q(q1));\n"
        "  clk_gate u_cg (.CLK(clk), .SE(se), .GCLK(q2));\n"
        "  no_clk_ff f_noclk (.D(d), .Q(q3));\n"
        "  comb_cell u_comb (.A(d), .B(se), .Y(y));\n"
        "  sram_macro u_macro (.CLK(clk));\n"
        "endmodule\n",
        encoding="utf-8",
    )
    drivable, unsupported = sequential_elements(
        str(netlist),
        [str(models_file)],
        "top",
        error_cls=lambda msg: _CustomError(f"prefixed: {msg}"),
    )
    assert [e["instance"] for e in drivable] == ["f_ok"]
    assert unsupported == [
        {"instance": "f_scan", "cell": "scan_ff", "reason": "scan_cell"},
        {"instance": "u_cg", "cell": "clk_gate", "reason": "no_d_or_clock_pin"},
        {"instance": "f_noclk", "cell": "no_clk_ff", "reason": "no_d_or_clock_pin"},
        {"instance": "u_macro", "cell": "sram_macro", "reason": "hard_macro"},
    ]
    assert compat_sequential_elements(str(netlist), [str(models_file)], "top") == (
        drivable,
        unsupported,
    )
    with pytest.raises(_CustomError, match="^prefixed: top module 'nope'"):
        sequential_elements(
            str(netlist),
            [str(models_file)],
            "nope",
            error_cls=lambda msg: _CustomError(f"prefixed: {msg}"),
        )


def test_consumer_error_classes_and_precedence_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    models_file = tmp_path / "models.v"
    models_file.write_text(CELL_MODELS, encoding="utf-8")
    hier = tmp_path / "hier.v"
    hier.write_text(
        "module sub (a, y);\n  input a; output y;\n"
        "  inv_cell u0 (.A(a), .Y(y));\nendmodule\n"
        "module top (rst_n, a, y);\n  input rst_n, a; output y;\n"
        "  sub u_sub (.a(a), .y(y));\n"
        "endmodule\n",
        encoding="utf-8",
    )
    # In reset_coverage, missing reset_port precedes hierarchical-instance refusal.
    with pytest.raises(ResetCoverageError, match="reset port 'missing_rst'"):
        run_reset_coverage(str(hier), [str(models_file)], "missing_rst", top="top")
    with pytest.raises(ResetCoverageError, match="hierarchical netlists"):
        run_reset_coverage(str(hier), [str(models_file)], "rst_n", top="top")

    # In power_intent, errors raise PowerIntentError directly (no ResetCoverageError).
    intent = tmp_path / "intent.json"
    intent.write_text(json.dumps({"gated_instances": ["u_sub"]}), encoding="utf-8")
    with pytest.raises(PowerIntentError, match="cannot read intent file") as exc_info:
        run_power_intent(
            str(hier), [str(models_file)], str(tmp_path / "no_intent.json")
        )
    assert not isinstance(exc_info.value.__cause__, ResetCoverageError)
    with pytest.raises(PowerIntentError, match="hierarchical netlists") as exc_info:
        run_power_intent(str(hier), [str(models_file)], str(intent), top="top")
    assert not isinstance(exc_info.value.__cause__, ResetCoverageError)

    # In functional_verification options.initial_state, errors keep the prefix.
    monkeypatch.setattr(
        fv,
        "_import_runner",
        lambda: types.SimpleNamespace(get_runner=lambda _n: object()),
    )
    monkeypatch.setattr(fv, "_engine_version", lambda _engine: "13.0")
    monkeypatch.setattr(fv, "_cocotb_version", lambda: "2.0.1")
    bad_net = tmp_path / "bad_net.v"
    bad_net.write_text("module broken (\n", encoding="utf-8")
    tb = tmp_path / "tb_mod.py"
    tb.write_text("", encoding="utf-8")
    req = tmp_path / "req.json"
    req.write_text(
        json.dumps(
            {
                "sources": [str(models_file), str(hier)],
                "hdl_toplevel": "top",
                "testbench": {"module": "tb_mod"},
                "options": {"initial_state": {"mode": "x", "netlist": str(bad_net)}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        FunctionalVerificationError,
        match=r"^options\.initial_state: cannot parse netlist",
    ) as fv_exc:
        run_functional_verification(str(req))
    assert not isinstance(fv_exc.value.__cause__, ResetCoverageError)
