"""Power-up state for named and unnamed sequential UDPs in ASAP7-style models."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from klayout_tools.functional_verification import (
    FunctionalVerificationError,
    run_functional_verification,
)
from klayout_tools.reset_coverage import sequential_elements
from test_functional_verification_initial_state import (
    MODELS,
    _SeedRecordingRunner,
    _stage,
    _state,
    _stub,
)

# ASAP7-shaped (issue #7939): the state-holding UDPs are declared separately
# from the cells and instantiated without an instance name; the flop also
# carries the `altos_dff_err` notifier companion, and the combinational UDP
# shares nothing but the `primitive` keyword with them.
ASAP7_UDPS = """\
primitive altos_dff (q, v, clk, d, xcr);
\toutput q; reg q; input v, clk, d, xcr;
\ttable
\t\t* ? ? ? : ? : x;
\t\t? (x1) 0 0 : ? : 0;
\t\t? (x1) 1 0 : ? : 1;
\t\t? 0 ? ? : ? : -;
\tendtable
endprimitive
primitive altos_dff_err (q, clk, d);
\toutput q; reg q; input clk, d;
\ttable
\t\t(0x) ? : ? : 0;
\t\t(1x) ? : ? : 1;
\tendtable
endprimitive
primitive altos_latch (q, v, clk, d);
\toutput q; reg q; input v, clk, d;
\ttable
\t\t* ? ? : ? : x;
\t\t? 1 0 : ? : 0;
\t\t? 1 1 : ? : 1;
\t\t? 0 ? : ? : -;
\tendtable
endprimitive
primitive altos_comb (y, a, b);
\tinput a, b; output y;
\ttable
\t\t0 0 : 0;
\t\t0 1 : 1;
\t\t1 0 : 1;
\t\t1 1 : 0;
\tendtable
endprimitive
"""

ASAP7_MODELS = """\
module DFFHQNx1_ASAP7_75t_R (QN, D, CLK);
\toutput QN; input D, CLK;
\treg notifier;
\twire int_fwire_d, int_fwire_IQN, xcr_0;
\tnot (int_fwire_d, D);
\taltos_dff_err (xcr_0, CLK, int_fwire_d);
\taltos_dff (int_fwire_IQN, notifier, CLK, int_fwire_d, xcr_0);
\tbuf (QN, int_fwire_IQN);
endmodule
module DHLx1_ASAP7_75t_R (Q, D, CLK);
\toutput Q; input D, CLK;
\treg notifier;
\twire int_fwire_IQ;
\taltos_latch (int_fwire_IQ, notifier, CLK, D);
\tbuf (Q, int_fwire_IQ);
endmodule
module COMBx1_ASAP7_75t_R (Y, A);
\toutput Y; input A;
\treg notifier;
\twire int_fwire;
\taltos_comb (int_fwire, A, A);
\tbuf (Y, int_fwire);
endmodule
module ICGx1_ASAP7_75t_R (GCLK, ENA, SE, CLK);
\toutput GCLK; input ENA, SE, CLK;
\treg notifier;
\twire int_fwire_IQ, int_fwire_test;
\tor (int_fwire_test, ENA, SE);
\taltos_latch (int_fwire_IQ, notifier, CLK, int_fwire_test);
\tand (GCLK, CLK, int_fwire_IQ);
endmodule
module SDFHx1_ASAP7_75t_R (QN, D, SE, SI, CLK);
\toutput QN; input D, SE, SI, CLK;
\treg notifier;
\twire int_fwire_d, int_fwire_IQN, si_bar, xcr_0;
\tnot (si_bar, SI);
\tand (int_fwire_d, D, si_bar);
\taltos_dff_err (xcr_0, CLK, int_fwire_d);
\taltos_dff (int_fwire_IQN, notifier, CLK, int_fwire_d, xcr_0);
\tbuf (QN, int_fwire_IQN);
endmodule
"""

ASAP7_NETLIST = """\
module top (clk, d, latch_d, scan_si, scan_se, gate_en, q_f, q_l, q_s, gclk, comb_y);
  input clk, d, latch_d, scan_si, scan_se, gate_en;
  output q_f, q_l, q_s, gclk, comb_y;
  DFFHQNx1_ASAP7_75t_R f0 (.CLK(clk), .D(d), .QN(q_f));
  DHLx1_ASAP7_75t_R l0 (.CLK(clk), .D(latch_d), .Q(q_l));
  ICGx1_ASAP7_75t_R g0 (.CLK(clk), .ENA(gate_en), .SE(scan_se), .GCLK(gclk));
  SDFHx1_ASAP7_75t_R s0 (.CLK(clk), .D(d), .SE(scan_se), .SI(scan_si), .QN(q_s));
  COMBx1_ASAP7_75t_R c0 (.A(d), .Y(comb_y));
endmodule
"""

ASAP7_DRIVABLE_NETLIST = ASAP7_NETLIST.replace(
    "  ICGx1_ASAP7_75t_R g0 (.CLK(clk), .ENA(gate_en), .SE(scan_se), .GCLK(gclk));\n"
    "  SDFHx1_ASAP7_75t_R s0 (.CLK(clk), .D(d), .SE(scan_se), .SI(scan_si), "
    ".QN(q_s));\n",
    "",
)


def _stage_asap7(tmp_path: Path) -> None:
    (tmp_path / "asap7_udps.v").write_text(ASAP7_UDPS, encoding="utf-8")
    (tmp_path / "asap7_models.v").write_text(ASAP7_MODELS, encoding="utf-8")
    (tmp_path / "asap7_net.v").write_text(ASAP7_NETLIST, encoding="utf-8")
    (tmp_path / "asap7_drivable_net.v").write_text(
        ASAP7_DRIVABLE_NETLIST, encoding="utf-8"
    )
    (tmp_path / "test_tb.py").write_text("# stub\n", encoding="utf-8")


def test_asap7_unnamed_udp_instances_drive_flop_and_latch(tmp_path):
    """Issue #7939's refusal: ASAP7 declares its state-holding UDPs in a
    separate file and instantiates them without an instance name. The flop
    and the data latch are drivable (one entry per *instance* -- the
    `altos_dff_err` companion inside the flop adds none), the clock gate
    (internal latch, no D, lone SE) and the scan flop (SI+SE) are
    unsupported, and the combinational UDP with a local notifier register
    is not a state element."""
    _stage_asap7(tmp_path)
    drivable, unsupported = sequential_elements(
        str(tmp_path / "asap7_net.v"),
        [str(tmp_path / "asap7_udps.v"), str(tmp_path / "asap7_models.v")],
        "top",
    )
    assert [
        (e["instance"], e["d_pin"], e["d_net"], e["capture_pin"], e["capture_net"])
        for e in drivable
    ] == [
        ("f0", "D", "d", "CLK", "clk"),
        ("l0", "D", "latch_d", "CLK", "clk"),
    ]
    assert unsupported == [
        {"instance": "g0", "cell": "ICGx1_ASAP7_75t_R", "reason": "no_d_or_clock_pin"},
        {"instance": "s0", "cell": "SDFHx1_ASAP7_75t_R", "reason": "scan_cell"},
    ]


@pytest.mark.parametrize("order", [[0, 1], [1, 0]])
def test_asap7_udp_rename_and_source_order_preserve_classification(tmp_path, order):
    """Recognition is by declaration shape, not a hard-coded family name:
    renaming the UDP and declaring it in a separate file -- in either
    source order -- still classifies the flop and the latch."""
    _stage_asap7(tmp_path)
    udps = tmp_path / "renamed_udps.v"
    udps.write_text(ASAP7_UDPS.replace("altos_dff", "klt_renamed_flop"))
    models = tmp_path / "renamed_models.v"
    models.write_text(ASAP7_MODELS.replace("altos_dff", "klt_renamed_flop"))
    sources = [str(models), str(udps)]
    drivable, unsupported = sequential_elements(
        str(tmp_path / "asap7_drivable_net.v"),
        [sources[i] for i in order],
        "top",
    )
    assert [e["instance"] for e in drivable] == ["f0", "l0"]
    assert unsupported == []


def test_sky130_name_fallback_without_primitive_declarations(tmp_path):
    """Callers that supply cell wrappers without the primitives they
    instantiate keep the sky130 ``udp_dff*``/``udp_dlatch*`` name
    fallback."""
    _stage(tmp_path)
    (tmp_path / "wrappers.v").write_text(
        MODELS.split("endprimitive\n", 1)[1], encoding="utf-8"
    )
    drivable, unsupported = sequential_elements(
        str(tmp_path / "net.v"), [str(tmp_path / "wrappers.v")], "top"
    )
    assert [e["instance"] for e in drivable] == ["f0", "f1", "u/f2[0]", "f3"]
    assert unsupported == []


def _asap7_request(tmp_path: Path, netlist: str, initial_state) -> str:
    request = {
        "engine": "icarus",
        "sources": ["asap7_udps.v", "asap7_models.v", netlist],
        "hdl_toplevel": "top",
        "testbench": {"module": "test_tb"},
        "options": {"initial_state": initial_state},
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    return str(path)


def test_asap7_request_shim_targets_both_d_pins(tmp_path, monkeypatch):
    _stage_asap7(tmp_path)
    runner = _SeedRecordingRunner()
    _stub(monkeypatch, runner)

    report = run_functional_verification(
        _asap7_request(
            tmp_path,
            "asap7_drivable_net.v",
            _state(netlist="asap7_drivable_net.v"),
        )
    )

    state = report["environment"]["initial_state"]
    assert state["state_elements"] == 2 and state["d_nets"] == 2
    assert state["clock"] == "clk"
    shim = Path(runner.build_kwargs["sources"][-1]).read_text(encoding="utf-8")
    assert "force top.f0.D = klt_bit_0;" in shim
    assert "force top.l0.D = klt_bit_1;" in shim


def test_asap7_clock_gate_and_scan_flop_are_named_request_errors(tmp_path, monkeypatch):
    _stage_asap7(tmp_path)
    _stub(monkeypatch, _SeedRecordingRunner())
    with pytest.raises(
        FunctionalVerificationError,
        match=(
            "g0 \\(ICGx1_ASAP7_75t_R: no_d_or_clock_pin\\), "
            "s0 \\(SDFHx1_ASAP7_75t_R: scan_cell\\)"
        ),
    ):
        run_functional_verification(
            _asap7_request(tmp_path, "asap7_net.v", _state(netlist="asap7_net.v"))
        )


@pytest.mark.parametrize("escaped", [False, True], ids=["one_line", "escaped_names"])
def test_udp_spelling_preserves_the_drive_list(tmp_path, escaped):
    _stage_asap7(tmp_path)
    models = tmp_path / "asap7_models.v"
    udps = tmp_path / "asap7_udps.v"
    if escaped:
        # Retitle every state-holding UDP, including its error companion.
        for source in (models, udps):
            source.write_text(
                re.sub(
                    r"\baltos_(?:dff(?:_err)?|latch)\b",
                    lambda m: "\\renamed." + m.group() + " ",
                    source.read_text(),
                )
            )
    else:
        # Named and anonymous instances can follow a semicolon on the same line.
        models.write_text(re.sub(r";\s+(?=altos_)", "; ", models.read_text()))
    drivable, unsupported = sequential_elements(
        str(tmp_path / "asap7_drivable_net.v"), [str(udps), str(models)], "top"
    )
    assert [e["instance"] for e in drivable] == ["f0", "l0"]
    assert not unsupported


@pytest.mark.parametrize("delay", ["#0", "#(0, 0)", "#((1-1), (2-2))"])
@pytest.mark.parametrize("named", [False, True], ids=["anonymous", "named"])
def test_delayed_udp_instances_preserve_the_drive_list(tmp_path, delay, named):
    _stage_asap7(tmp_path)
    models = tmp_path / "asap7_models.v"
    models.write_text(
        re.sub(
            r"\b(altos_\w+)\s*\(",
            lambda match: (
                f"{match[1]} {delay}\n" + (f"inst_{match[1]} " if named else "") + "("
            ),
            models.read_text(),
        )
    )
    drivable, unsupported = sequential_elements(
        str(tmp_path / "asap7_drivable_net.v"),
        [str(tmp_path / "asap7_udps.v"), str(models)],
        "top",
    )
    assert [entry["instance"] for entry in drivable] == ["f0", "l0"]
    assert not unsupported


def _header_form(udps: str) -> str:
    """Move each state-holding UDP's output register into its port list."""

    def header(m: re.Match[str]) -> str:
        inputs = ", ".join(f"input {p.strip()}" for p in m[2].split(","))
        return f"primitive {m[1]} (output reg q, {inputs});"

    return re.sub(
        r"primitive (altos_\w+) \(q, [^)]*\);\n\toutput q; reg q; input ([^;]*);",
        header,
        udps,
    )


#: Equivalent spellings of the output register, each valid Verilog for Icarus.
REGISTER_SPELLINGS = {
    "header": _header_form,
    "body_output_reg": lambda udps: udps.replace("output q; reg q;", "output reg q;"),
}


@pytest.mark.parametrize("spelling", sorted(REGISTER_SPELLINGS))
def test_udp_output_register_spelling_preserves_the_drive_list(tmp_path, spelling):
    """``primitive p (output reg q, input d, input clk);`` and ``output reg
    q;`` in the body are the same state-holding UDP as ``output q; reg q;``:
    the drive list is unchanged."""
    _stage_asap7(tmp_path)
    text = REGISTER_SPELLINGS[spelling](ASAP7_UDPS)
    assert "output q; reg q;" not in text
    udps = tmp_path / "asap7_udps.v"
    udps.write_text(text, encoding="utf-8")
    drivable, unsupported = sequential_elements(
        str(tmp_path / "asap7_drivable_net.v"),
        [str(udps), str(tmp_path / "asap7_models.v")],
        "top",
    )
    assert [e["instance"] for e in drivable] == ["f0", "l0"]
    assert not unsupported


@pytest.mark.parametrize("udp", ["generic_mux", "lib__udp_dff", "lib__udp_dlatch"])
def test_declared_combinational_udp_ignores_the_sky130_name_fallback(tmp_path, udp):
    """A UDP the supplied files declare keeps its declared classification: a
    two-input table with no ``current`` column is combinational even when its
    name matches the sky130 ``udp_dff``/``udp_dlatch`` fallback."""
    (tmp_path / "comb.v").write_text(
        f"primitive {udp} (q, d, clk);\n"
        "\toutput q; input d, clk;\n"
        "\ttable\n\t\t0 ? : 0;\n\t\t1 ? : 1;\n\tendtable\n"
        "endprimitive\n"
        "module wrapped (Q, D, CLK);\n"
        "\toutput Q; input D, CLK;\n"
        f"\t{udp} (Q, D, CLK);\n"
        "endmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "net.v").write_text(
        "module top (clk, d, q);\n"
        "  input clk, d; output q;\n"
        "  wrapped f0 (.CLK(clk), .D(d), .Q(q));\n"
        "endmodule\n",
        encoding="utf-8",
    )
    drivable, unsupported = sequential_elements(
        str(tmp_path / "net.v"), [str(tmp_path / "comb.v")], "top"
    )
    assert drivable == []
    assert unsupported == []
