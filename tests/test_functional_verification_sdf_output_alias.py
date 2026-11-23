"""Timing controls for separately assigned, indexed top-level outputs (#6937)."""

import pytest

from klayout_tools import functional_verification as fv
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
        top = top.replace(".out(y[0])", ".out(b0)").replace(".out(y[1])", ".out(b1)")
        top = top.replace(".in(y[0])", ".in(b0)")
        wires = "wire b0, b1;\n"
        if wiring == "whole":
            wires += "assign y = {b1,b0};\n"
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


@SKIP_NO_COCOTB
@pytest.mark.skipif(not HAVE_SIMULATOR["icarus"], reason="requires Icarus")
@pytest.mark.parametrize(
    ("wiring", "non_ansi", "delays", "status", "drops"),
    [
        ("direct", False, (DELAY, DELAY), "fail", 0),
        ("whole", False, (DELAY, DELAY), "fail", 0),
        ("separate", False, (ZERO, ZERO), "pass", 2),
        ("separate", True, (ZERO, ZERO), "pass", 2),
    ],
)
def test_output_arcs_on_icarus(tmp_path, wiring, non_ansi, delays, status, drops):
    request = _request(tmp_path, wiring, non_ansi=non_ansi, delays=delays)
    sdf = tmp_path / "route.sdf"
    original = sdf.read_bytes()
    report = fv.run_functional_verification(request)
    assert sdf.read_bytes() == original
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


@SKIP_NO_COCOTB
@pytest.mark.parametrize("non_ansi", [False, True])
@pytest.mark.parametrize("delay", ["(0:0:2) (0:0:0)", "(0:0:0) (1:0:0)"])
def test_nonzero_output_arc_refused_before_build(
    tmp_path, monkeypatch, non_ansi, delay
):
    req = _request(tmp_path, "separate", non_ansi=non_ansi, delays=(ZERO, delay))

    def unexpected_build(*args, **kwargs):
        pytest.fail("started simulator build for unsupported timing")

    monkeypatch.setattr(fv, "_run_build", unexpected_build)
    with pytest.raises(fv.FunctionalVerificationError, match=r"u_q1\.out.*y\[1\]"):
        fv.run_functional_verification(req)


def test_nested_cell_instance_cannot_name_a_top_output_alias(tmp_path):
    sdf = _bus_port_fanout_output_sdf_text().replace("(INSTANCE)", "(INSTANCE u_child)")
    path = tmp_path / "nested.sdf"
    path.write_text(sdf)
    assert fv._drop_sdf_zero_delay_alias_port_interconnects(str(path), {"y[0]"}) is None
