"""Timing controls for separately assigned, indexed top-level outputs (#6937)."""

import pytest

from klayout_tools import functional_verification as fv
from klayout_tools.functional_verification_sdf_output_concat import (
    _apply_sdf_output_assign_rewrite,
)
from test_functional_verification import (
    _BUS_PORT_FANOUT_OUTPUT_DUT_V,
    _BUS_PORT_FANOUT_OUTPUT_TESTBENCH_PY,
    HAVE_SIMULATOR,
    SKIP_NO_COCOTB,
    _bus_port_fanout_output_sdf_text,
    _write_request,
)

DELAY = "(2.000:2.000:2.000) (2.000:2.000:2.000)"
ZERO = "(0:0:0) (0:0:0)"


def _request(tmp_path, wiring, *, non_ansi=False, delays=(ZERO, ZERO)):
    top = _BUS_PORT_FANOUT_OUTPUT_DUT_V
    if wiring != "direct":
        top = top.replace(".out(y[0])", ".out(b0)")
        if wiring != "separate_cell":
            top = top.replace(".out(y[1])", ".out(b1)")
        top = top.replace(".in(y[0])", ".in(b0)")
        wires = "wire b0, b1;\n"
        if wiring == "whole":
            wires += "assign y = {b1,b0};\n"
        elif wiring == "separate_descending":
            wires += "assign y[1] = b1;\nassign y[0] = b0;\n"
        elif wiring == "separate_missing":
            wires += "assign y[0] = b0;\n"
        else:
            wires += "assign y[0] = b0;\nassign y[1] = b1;\n"
        top = top.replace("  klt_inv u_q", wires + "  klt_inv u_q")
    if non_ansi:
        top = top.replace(
            "(input wire d, output wire [1:0] y);", "(d, y);\ninput d;\noutput [1:0] y;"
        )
    (tmp_path / "design.v").write_text(top)
    (tmp_path / "test_timing.py").write_text(_BUS_PORT_FANOUT_OUTPUT_TESTBENCH_PY)
    sdf = _bus_port_fanout_output_sdf_text().replace(
        f"(INTERCONNECT u_q.out y[0] {DELAY})",
        f"(INTERCONNECT u_q.out y[0] {delays[0]})\n"
        f"      (INTERCONNECT u_q1.out y[1] {delays[1]})",
    )
    (tmp_path / "route.sdf").write_text(sdf)
    return _write_request(
        tmp_path / "request.json",
        {
            "sources": ["design.v"],
            "hdl_toplevel": "bus_fanout_out",
            "testbench": {"module": "test_timing"},
            "options": {"sdf": {"file": "route.sdf", "corner": "typ"}},
        },
    )


def _run_rewrite_step(tmp_path, top_text, sdf_text, hdl_toplevel="top"):
    design = tmp_path / "design.v"
    design.write_text(top_text)
    sdf = tmp_path / "route.sdf"
    sdf.write_text(sdf_text)
    return _apply_sdf_output_assign_rewrite(
        str(sdf), [str(design)], hdl_toplevel, str(tmp_path / "out")
    )


def _unit_sdf(destination="y[1]", delay="(2:2:2) (2:2:2)"):
    return f"""\
(DELAYFILE
  (SDFVERSION "3.0")
  (DESIGN "top")
  (DIVIDER .)
  (TIMESCALE 1ns)
  (CELL
    (CELLTYPE "top")
    (INSTANCE)
    (DELAY (ABSOLUTE
      (INTERCONNECT u1.o {destination} {delay})
    ))
  )
)
"""


_UNIT_TOP = """\
module top (input wire d, output wire [1:0] y);
  wire b0, b1;
  assign y[0] = b0;
  assign y[1] = b1;
  mod u1 (.i(d), .o(b0));
  mod u2 (.i(d), .o(b1));
endmodule
"""


@SKIP_NO_COCOTB
@pytest.mark.skipif(not HAVE_SIMULATOR["icarus"], reason="requires Icarus")
@pytest.mark.parametrize(
    ("wiring", "non_ansi", "delays", "status", "drops"),
    [
        ("direct", False, (DELAY, DELAY), "fail", 0),
        ("whole", False, (DELAY, DELAY), "fail", 0),
        ("separate", False, (ZERO, ZERO), "pass", 2),
        ("separate", True, (ZERO, ZERO), "pass", 2),
        # #6937's bounded follow-up (#7534): the per-bit spelling with real
        # (nonzero) output arcs is rewritten to the whole-vector concat in
        # a build-dir copy and reaches a graded verdict exactly like the
        # "whole" control -- including the mixed zero/nonzero bits, whose
        # zero arc now annotates as zero instead of being dropped.
        ("separate", False, (DELAY, DELAY), "fail", 0),
        ("separate", True, (DELAY, DELAY), "fail", 0),
        ("separate_descending", False, (DELAY, DELAY), "fail", 0),
        ("separate", False, (ZERO, DELAY), "pass", 0),
        ("separate", False, (DELAY, ZERO), "fail", 0),
    ],
)
def test_output_arcs_on_icarus(tmp_path, wiring, non_ansi, delays, status, drops):
    request = _request(tmp_path, wiring, non_ansi=non_ansi, delays=delays)
    sdf = tmp_path / "route.sdf"
    original = sdf.read_bytes()
    design = tmp_path / "design.v"
    design_original = design.read_bytes()
    report = fv.run_functional_verification(request)
    assert sdf.read_bytes() == original
    assert design.read_bytes() == design_original
    assert report["status"] == status
    if drops:
        normalized = tmp_path / ".klt/functional-verification/klt_sdf_alias_dropped.sdf"
        assert f"(INTERCONNECT u_q.out u_g2.in {DELAY})" in normalized.read_text()
    evidence = report["environment"]["sdf"]
    assert evidence["annotated"] and evidence["partial"] == bool(drops)
    assert (
        evidence["dropped"]
        .get("zero_delay_alias_port_interconnect", {})
        .get("count", 0)
        == drops
    )
    rewritten = evidence.get("output_assign_rewritten", [])
    if wiring in ("separate", "separate_descending") and any(d != ZERO for d in delays):
        assert len(rewritten) == 1
        assert rewritten[0]["port"] == "y" and rewritten[0]["bits"] == 2
        assert rewritten[0]["file"]["sha256"]
        copy = (
            tmp_path
            / ".klt/functional-verification/klt_sdf_output_concat_sources/0/design.v"
        )
        text = copy.read_text()
        assert text.count("assign y = {b1,b0};") == 1
        assert "assign y[" not in text
    else:
        assert rewritten == []


@SKIP_NO_COCOTB
@pytest.mark.parametrize(
    ("non_ansi", "wiring", "delays", "pattern"),
    [
        (False, "separate_cell", (ZERO, "(0:0:2) (0:0:0)"), r"u_q1\.out.*y\[1\]"),
        (True, "separate_cell", (ZERO, "(0:0:2) (0:0:0)"), r"u_q1\.out.*y\[1\]"),
        (False, "separate_cell", (ZERO, "(0:0:0) (1:0:0)"), r"u_q1\.out.*y\[1\]"),
        (True, "separate_cell", (ZERO, "(0:0:0) (1:0:0)"), r"u_q1\.out.*y\[1\]"),
        (False, "separate_missing", ("(0:0:2) (0:0:0)", ZERO), r"u_q\.out.*y\[0\]"),
        (True, "separate_missing", ("(0:0:2) (0:0:0)", ZERO), r"u_q\.out.*y\[0\]"),
    ],
)
def test_nonzero_output_arc_refused_before_build(
    tmp_path, monkeypatch, non_ansi, wiring, delays, pattern
):
    """A nonzero arc onto a separately assigned bit still refuses before the
    build for every shape #7534's rewrite cannot prove equivalent -- here a
    bit also driven by a cell pin (`.out(y[1])`) and a bus with a bit that
    has no per-bit assign at all."""
    req = _request(tmp_path, wiring, non_ansi=non_ansi, delays=delays)

    def unexpected_build(*args, **kwargs):
        pytest.fail("started simulator build for unsupported timing")

    monkeypatch.setattr(fv, "_run_build", unexpected_build)
    with pytest.raises(fv.FunctionalVerificationError, match=pattern):
        fv.run_functional_verification(req)


@pytest.mark.parametrize("order", [(0, 1, 2), (1, 0, 2), (2, 1, 0)])
def test_rewrite_folds_three_bit_bus_msb_first(tmp_path, order):
    top = (
        _UNIT_TOP.replace("[1:0]", "[2:0]")
        .replace(
            "  assign y[0] = b0;\n  assign y[1] = b1;\n",
            "".join(f"  assign y[{bit}] = b{bit};\n" for bit in order),
        )
        .replace("wire b0, b1;", "wire b0, b1, b2;")
        .replace(
            "  mod u2 (.i(d), .o(b1));\n",
            "  mod u2 (.i(d), .o(b1));\n  mod u3 (.i(d), .o(b2));\n",
        )
    )
    design = tmp_path / "design.v"
    design.write_text(top)
    sdf = tmp_path / "route.sdf"
    sdf.write_text(_unit_sdf())
    sources, rewritten = _apply_sdf_output_assign_rewrite(
        str(sdf), [str(design)], "top", str(tmp_path / "out")
    )
    assert design.read_text() == top
    assert len(rewritten) == 1
    assert rewritten[0]["port"] == "y" and rewritten[0]["bits"] == 3
    copy = tmp_path / "out/klt_sdf_output_concat_sources/0/design.v"
    text = copy.read_text()
    assert text.count("assign y = {b2,b1,b0};") == 1
    assert "assign y[" not in text
    assert sources == [str(copy)]


@pytest.mark.parametrize(
    ("label", "top_text", "sdf_text", "hdl_toplevel"),
    [
        (
            "cell_driven_bit",
            _UNIT_TOP.replace(".o(b1)", ".o(y[1])"),
            _unit_sdf(),
            "top",
        ),
        (
            "missing_bit_assign",
            _UNIT_TOP.replace("  assign y[1] = b1;\n", ""),
            _unit_sdf(),
            "top",
        ),
        (
            "expression_rhs",
            _UNIT_TOP.replace("assign y[1] = b1;", "assign y[1] = ~b1;"),
            _unit_sdf(),
            "top",
        ),
        (
            "duplicate_assign",
            _UNIT_TOP.replace(
                "  assign y[1] = b1;\n", "  assign y[1] = b1;\n  assign y[1] = b1;\n"
            ),
            _unit_sdf(),
            "top",
        ),
        (
            "whole_vector_assign_present",
            _UNIT_TOP.replace(
                "  assign y[0] = b0;\n  assign y[1] = b1;\n", "  assign y = {b1,b0};\n"
            ),
            _unit_sdf(),
            "top",
        ),
        (
            "ascending_range",
            _UNIT_TOP.replace("[1:0]", "[0:1]"),
            _unit_sdf(),
            "top",
        ),
        (
            "range_not_plain_n_to_0",
            _UNIT_TOP.replace("[1:0]", "[2:1]"),
            _unit_sdf(),
            "top",
        ),
        ("top_module_not_found", _UNIT_TOP, _unit_sdf(), "other"),
        ("zero_delay_only", _UNIT_TOP, _unit_sdf(delay="(0:0:0) (0:0:0)"), "top"),
        (
            "no_output_bus",
            _UNIT_TOP.replace("output wire [1:0] y", "output wire y").replace(
                "  assign y[0] = b0;\n  assign y[1] = b1;\n", "  assign y = b0;\n"
            ),
            _unit_sdf(),
            "top",
        ),
    ],
)
def test_unprovable_shapes_are_left_alone(
    tmp_path, label, top_text, sdf_text, hdl_toplevel
):
    design = tmp_path / "design.v"
    design.write_text(top_text)
    sdf = tmp_path / "route.sdf"
    sdf.write_text(sdf_text)
    sources, rewritten = _apply_sdf_output_assign_rewrite(
        str(sdf), [str(design)], hdl_toplevel, str(tmp_path / "out")
    )
    assert sources == [str(design)]
    assert rewritten == []
    assert not (tmp_path / "out/klt_sdf_output_concat_sources").exists()


def test_non_ansi_header_is_rewritten(tmp_path):
    top = _UNIT_TOP.replace(
        "(input wire d, output wire [1:0] y);", "(d, y);\ninput d;\noutput [1:0] y;"
    )
    design = tmp_path / "design.v"
    design.write_text(top)
    sdf = tmp_path / "route.sdf"
    sdf.write_text(_unit_sdf())
    sources, rewritten = _apply_sdf_output_assign_rewrite(
        str(sdf), [str(design)], "top", str(tmp_path / "out")
    )
    assert rewritten[0]["port"] == "y" and rewritten[0]["bits"] == 2
    copy = tmp_path / "out/klt_sdf_output_concat_sources/0/design.v"
    assert "assign y = {b1,b0};" in copy.read_text()
    assert "(d, y);" in copy.read_text() and "output [1:0] y;" in copy.read_text()


def test_comments_around_the_assigns_do_not_block_the_rewrite(tmp_path):
    top = _UNIT_TOP.replace(
        "  assign y[0] = b0;", "  // per-bit output assigns\n  assign y[0] = b0;"
    ).replace("  assign y[1] = b1;", "  assign y[1] = b1; // upper bit\n")
    design = tmp_path / "design.v"
    design.write_text(top)
    sdf = tmp_path / "route.sdf"
    sdf.write_text(_unit_sdf())
    sources, rewritten = _apply_sdf_output_assign_rewrite(
        str(sdf), [str(design)], "top", str(tmp_path / "out")
    )
    copy = tmp_path / "out/klt_sdf_output_concat_sources/0/design.v"
    assert rewritten and "assign y = {b1,b0};" in copy.read_text()


def test_nested_cell_instance_cannot_name_a_top_output_alias(tmp_path):
    sdf = _bus_port_fanout_output_sdf_text().replace("(INSTANCE)", "(INSTANCE u_child)")
    path = tmp_path / "nested.sdf"
    path.write_text(sdf)
    assert fv._drop_sdf_zero_delay_alias_port_interconnects(str(path), {"y[0]"}) is None
