"""Issue #6075: `options.sdf` zero-delay entries on a bit-selected INTERNAL net.

The one-modpath-per-bit-selected-net cap recorded for #1619 shape (b)
(#5705, PR #5840) is not confined to top-level port nets. Measured live on
raw iverilog/vvp 13.0 (2026-11-17): when the connecting net is a bit-select
of an *internal* wire, EVERY `INTERCONNECT` entry on that net fails --
`VPI error: Could not insert intermodpath!` per entry, then `SDF ERROR:
... Could not find intermodpath!` -- at fanout 1 as much as fanout 4, at
non-zero delay as much as zero (0-of-N; a scalar internal net annotates
cleanly at the same fanout, and a top-level port bit keeps #5705's
last-declared-survivor N-1-of-N rule). The escape does not matter.

The entries' SDF text carries no trace of this -- `u_src.Y u_l1.A` is
byte-identical whether the connecting net is a bit-select or a scalar -- so
unlike every sibling drop class the decision is made structurally, from the
same gate-level parse the run already compiles
(`_collect_sdf_internal_bus_net_pins`), and `_drop_sdf_zero_delay_internal_
bus_net_interconnects` removes exactly the all-zero entries under
`environment.sdf.dropped`'s `zero_delay_internal_bus_net_interconnect`
class. A non-zero entry on the same net stays in place and still fails the
run loudly (the #5769 boundary, untouched).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from klayout_tools import functional_verification as fv
from klayout_tools import functional_verification_sdf as fvs
from klayout_tools.functional_verification import run_functional_verification

HAVE_ICARUS = shutil.which("iverilog") is not None


def _have_cocotb() -> bool:
    try:
        import cocotb_tools.runner  # noqa: F401
    except Exception:
        return False
    return True


HAVE_COCOTB = _have_cocotb()

#: Cells + DUT in one source file, the sibling fixtures' convention. The
#: internal bus is the escaped spelling the field report carries
#: (`wire [3:0] \soc_bus ;` -- the plain spelling behaves identically,
#: measured); `u_src.Y` drives bit 3 and four `klt_load` pins fan it out.
#: `w` is the scalar-internal-net control: `u_w.Y` fans out to four loads
#: through entries whose SDF text is byte-identical to the failing ones,
#: yet annotates cleanly (measured -- the same netlist is the only
#: difference).
_DUT_V = """\
module klt_src (A, Y);
  input A;
  output Y;
  assign Y = A;
  specify
    (A => Y) = (0.0, 0.0);
  endspecify
endmodule

module klt_load (A, Z);
  input A;
  output Z;
  assign Z = A;
  specify
    (A => Z) = (0.0, 0.0);
  endspecify
endmodule

module dut(in, out1, out2, out3, out4, out5, out6, out7, out8);
  input in;
  output out1;
  output out2;
  output out3;
  output out4;
  output out5;
  output out6;
  output out7;
  output out8;
  wire a;
  wire w;
  wire [3:0] \\soc_bus ;
  klt_src u_src (.A(a), .Y(\\soc_bus [3]));
  klt_load u_l1 (.A(\\soc_bus [3]), .Z(out1));
  klt_load u_l2 (.A(\\soc_bus [3]), .Z(out2));
  klt_load u_l3 (.A(\\soc_bus [3]), .Z(out3));
  klt_load u_l4 (.A(\\soc_bus [3]), .Z(out4));
  klt_src u_w (.A(a), .Y(w));
  klt_load u_w1 (.A(w), .Z(out5));
  klt_load u_w2 (.A(w), .Z(out6));
  klt_load u_w3 (.A(w), .Z(out7));
  klt_load u_w4 (.A(w), .Z(out8));
  assign a = in;
endmodule
"""

#: ANSI-style header -- outside `parse_gate_level_verilog`'s grammar, so the
#: pin map must come back empty (a normalization pass is never worse than
#: not having tried).
_ANSI_DUT_V = """\
module klt_src (input wire A, output wire Y);
  assign Y = A;
  specify
    (A => Y) = (0.0, 0.0);
  endspecify
endmodule

module dut (input wire in, output wire out1);
  wire [3:0] \\soc_bus ;
  klt_src u_src (.A(in), .Y(\\soc_bus [3]));
endmodule
"""

#: A bit-select of a *bus port* is not an internal net: `a[0]` feeds
#: `u_x.A` (excluded -- #2984's source shape), while `nb[2]` on `u_src.Y` /
#: `u_l.A` is internal (mapped). `.D(soc_bus)` is a whole-bus connection
#: (a list value in the parse) -- excluded.
_PORT_BIT_DUT_V = """\
module klt_src (A, Y);
  input A;
  output Y;
  assign Y = A;
  specify
    (A => Y) = (0.0, 0.0);
  endspecify
endmodule

module klt_mix4 (A, D);
  input A;
  output [3:0] D;
  assign D = 4'b0;
  specify
    (A *> D) = (0.0, 0.0);
  endspecify
endmodule

module dut(a, out1);
  input [3:0] a;
  output out1;
  wire [3:0] nb;
  wire [3:0] soc_bus;
  klt_src u_src (.A(a[0]), .Y(nb[2]));
  klt_load u_l (.A(nb[2]), .Z(out1));
  klt_mix4 u_x (.A(a[1]), .D(soc_bus));
endmodule

module klt_load (A, Z);
  input A;
  output Z;
  assign Z = A;
  specify
    (A => Z) = (0.0, 0.0);
  endspecify
endmodule
"""


def _sdf_text(
    internal_bus_delay: str = "0.000:0.000:0.000",
    scalar_delay: str = "0.000:0.000:0.000",
    port_source_entry: str = "",
    deep_entry: str = "",
    bare_dest_entry: str = "",
) -> str:
    """An SDF whose top design `CELL` mixes internal-bit-select entries
    (`u_src.Y u_l*.A`) with a scalar-internal-net entry whose text is
    byte-identical in shape (`u_w.Y u_w1.A`) -- the discrimination this
    class exists for -- plus optional non-eligible shapes."""

    def _entry(src: str, dst: str, delay: str) -> str:
        return f"      (INTERCONNECT {src} {dst} ({delay}) ({delay}))"

    entries = [
        _entry("u_src.Y", f"u_l{i}.A", internal_bus_delay) for i in range(1, 5)
    ] + [_entry("u_w.Y", f"u_w{i}.A", scalar_delay) for i in range(1, 5)]
    if port_source_entry:
        entries.append(f"      {port_source_entry}")
    if deep_entry:
        entries.append(f"      {deep_entry}")
    if bare_dest_entry:
        entries.append(f"      {bare_dest_entry}")
    body = "\n".join(entries)
    return f"""\
(DELAYFILE
  (SDFVERSION "3.0")
  (DESIGN "dut")
  (DIVIDER .)
  (TIMESCALE 1ns)
  (CELL
    (CELLTYPE "dut")
    (INSTANCE)
    (DELAY (ABSOLUTE
{body}
    ))
  )
)
"""


def _write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


def _write_request(path: Path, request: dict) -> str:
    path.write_text(json.dumps(request), encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------- #
# The netlist-side pin map
# --------------------------------------------------------------------------- #


def test_collect_pins_maps_internal_bit_select_pins(tmp_path):
    dut = _write(tmp_path / "dut.v", _DUT_V)
    pins = fvs._collect_sdf_internal_bus_net_pins([dut], "dut")
    assert pins == {
        "u_src.Y": "soc_bus[3]",
        "u_l1.A": "soc_bus[3]",
        "u_l2.A": "soc_bus[3]",
        "u_l3.A": "soc_bus[3]",
        "u_l4.A": "soc_bus[3]",
    }


def test_collect_pins_exclude_scalar_nets_port_bits_and_whole_bus_connections(tmp_path):
    """A scalar connection (`u_src.A` -> `in`), a bit-select of a *bus
    port* (`a[0]`, `a[1]`), and a whole-bus (list) connection (`.D(soc_bus)`)
    are all excluded; only the internal wire's bit-select pins map."""
    dut = _write(tmp_path / "dut.v", _PORT_BIT_DUT_V)
    pins = fvs._collect_sdf_internal_bus_net_pins([dut], "dut")
    assert pins == {"u_src.Y": "nb[2]", "u_l.A": "nb[2]"}


def test_collect_pins_empty_when_the_module_is_outside_the_grammar(tmp_path):
    """An ANSI-style toplevel (which `parse_gate_level_verilog` does not
    model) yields an empty map -- the noop contract, never an error."""
    dut = _write(tmp_path / "dut.v", _ANSI_DUT_V)
    assert fvs._collect_sdf_internal_bus_net_pins([dut], "dut") == {}
    assert fvs._collect_sdf_internal_bus_net_pins(["/does-not-exist.v"], "dut") == {}


# --------------------------------------------------------------------------- #
# The drop
# --------------------------------------------------------------------------- #


def test_drop_sdf_zero_delay_internal_bus_net_interconnects_drops_only_those(tmp_path):
    """The load-bearing discrimination: the scalar-net entry's text is
    byte-identical to the dropped entries' shape and survives only because
    the netlist says its net is a scalar. A bit-selected bus-*port* source
    (`in[3] u_l1.A` -- #2984's source shape), a deeper-hierarchical
    destination, and a bare port destination are all left alone."""
    sdf_path = _write(
        Path(tempfile.mkdtemp()) / "route.sdf",
        _sdf_text(
            port_source_entry=(
                "(INTERCONNECT in[3] u_l1.A (0.000:0.000:0.000) (0.000:0.000:0.000))"
            ),
            deep_entry=(
                "(INTERCONNECT u_src.Y blk.u_l4.A (0.000:0.000:0.000)"
                " (0.000:0.000:0.000))"
            ),
            bare_dest_entry=(
                "(INTERCONNECT u_src.Y out3 (0.000:0.000:0.000) (0.000:0.000:0.000))"
            ),
        ),
    )
    pins = fvs._collect_sdf_internal_bus_net_pins(
        [_write(tmp_path / "dut.v", _DUT_V)], "dut"
    )

    result = fvs._drop_sdf_zero_delay_internal_bus_net_interconnects(sdf_path, pins)

    assert result is not None
    text, dropped = result
    assert dropped == 4
    assert text.count("(INTERCONNECT") == 7
    assert "(INTERCONNECT u_w.Y u_w1.A (0.000:0.000:0.000)" in text
    assert "(INTERCONNECT in[3] u_l1.A" in text
    assert "(INTERCONNECT u_src.Y blk.u_l4.A" in text
    assert "(INTERCONNECT u_src.Y out3" in text


def test_drop_keeps_a_nonzero_entry_on_the_same_net(tmp_path):
    """The #5769 boundary: a non-zero entry on the identical net is NOT
    dropped (its delay is real), so a mixed net keeps failing loudly."""
    sdf_path = _write(
        Path(tempfile.mkdtemp()) / "route.sdf",
        _sdf_text(
            internal_bus_delay="0.000:0.000:5.000",
            scalar_delay="5.000:5.000:5.000",
        ),
    )
    pins = fvs._collect_sdf_internal_bus_net_pins(
        [_write(tmp_path / "dut.v", _DUT_V)], "dut"
    )
    assert (
        fvs._drop_sdf_zero_delay_internal_bus_net_interconnects(sdf_path, pins) is None
    )


def test_drop_mixed_net_drops_only_the_zero_entries(tmp_path):
    """Run M of the spike §3.7 addendum's matrix, at classification level:
    on one net carrying a zero-delay and a non-zero entry, only the
    zero-delay one is dropped -- the non-zero one stays to fail the run."""
    sdf_path = _write(
        Path(tempfile.mkdtemp()) / "route.sdf",
        _sdf_text(
            internal_bus_delay="0.000:0.000:0.000",
            scalar_delay="0.000:0.000:0.000",
            port_source_entry="",
            deep_entry="",
            bare_dest_entry="",
        ).replace(
            "(INTERCONNECT u_src.Y u_l2.A (0.000:0.000:0.000) (0.000:0.000:0.000))",
            "(INTERCONNECT u_src.Y u_l2.A (5.000:5.000:5.000) (5.000:5.000:5.000))",
        ),
    )
    pins = fvs._collect_sdf_internal_bus_net_pins(
        [_write(tmp_path / "dut.v", _DUT_V)], "dut"
    )

    result = fvs._drop_sdf_zero_delay_internal_bus_net_interconnects(sdf_path, pins)

    assert result is not None
    text, dropped = result
    assert dropped == 3
    assert "(INTERCONNECT u_src.Y u_l2.A (5.000:5.000:5.000)" in text


def test_drop_noop_with_an_empty_map():
    """No internal bus nets in the netlist: returns None without even
    reading the file."""
    missing = str(Path(tempfile.mkdtemp()) / "does-not-exist.sdf")
    assert fvs._drop_sdf_zero_delay_internal_bus_net_interconnects(missing, {}) is None


def test_internal_bus_net_class_has_a_reason():
    assert fv.SDF_INTERNAL_BUS_NET_DROPPED_CLASS in fv.SDF_BENIGN_DIAGNOSTIC_REASONS


def test_bus_port_source_pass_cannot_claim_internal_net_entries(tmp_path):
    """Pipeline order pin: the #2984 pass keys its predicate on a
    bit-selected top-level *port* source token, so it can never claim an
    entry between two instance pins -- no entry is counted under both
    classes."""
    sdf_path = _write(
        Path(tempfile.mkdtemp()) / "route.sdf",
        _sdf_text(),
    )
    assert (
        fv._drop_sdf_zero_delay_bus_port_source_interconnects(sdf_path, {"soc_bus"})
        is None
    )
    pins = fvs._collect_sdf_internal_bus_net_pins(
        [_write(tmp_path / "dut.v", _DUT_V)], "dut"
    )
    result = fvs._drop_sdf_zero_delay_internal_bus_net_interconnects(sdf_path, pins)
    assert result is not None and result[1] == 4


# --------------------------------------------------------------------------- #
# Measured verdicts against the real simulator (spike §3.7's dated
# addendum: raw `iverilog`/`vvp` 13.0, the sibling fixtures' methodology)
# --------------------------------------------------------------------------- #


def _raw_tb_v(sdf_path: str) -> str:
    return f"""\
`timescale 1ns/1ns
module tb;
  reg in = 0;
  wire o1, o2, o3, o4, o5, o6, o7, o8;
  initial begin
    $sdf_annotate("{sdf_path}", dut);
  end
  dut dut(.in(in), .out1(o1), .out2(o2), .out3(o3), .out4(o4),
          .out5(o5), .out6(o6), .out7(o7), .out8(o8));
  initial begin
    #10 in = 1;
    #5 $display("DONE");
    $finish;
  end
endmodule
"""


def _compile_and_run(workdir: Path) -> str:
    vvp_out = workdir / "sim.vvp"
    build = subprocess.run(
        [
            "iverilog",
            "-g2012",
            "-gspecify",
            "-ginterconnect",
            "-o",
            str(vvp_out),
            str(workdir / "dut.v"),
            str(workdir / "tb.v"),
        ],
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        pytest.skip(f"iverilog lacks -ginterconnect: {build.stderr.strip()[:120]}")
    proc = subprocess.run(["vvp", str(vvp_out)], capture_output=True, text=True)
    return proc.stdout + proc.stderr


@pytest.mark.skipif(not HAVE_ICARUS, reason="iverilog is not installed on this machine")
def test_real_icarus_internal_bit_select_net_applies_zero_of_n(tmp_path):
    """The measured verdict the issue's field report carries, pinned here:
    ALL FOUR zero-delay entries on the bit-selected internal net fail --
    0-of-4, not #5705's last-declared-survivor N-1-of-N."""
    _write(tmp_path / "dut.v", _DUT_V)
    sdf_path = _write(tmp_path / "route.sdf", _sdf_text())
    _write(tmp_path / "tb.v", _raw_tb_v(sdf_path))

    transcript = _compile_and_run(tmp_path)

    assert transcript.count("VPI error: Could not insert intermodpath!") == 4
    assert transcript.count("Could not find intermodpath!") == 4
    assert "DONE" in transcript


@pytest.mark.skipif(not HAVE_ICARUS, reason="iverilog is not installed on this machine")
def test_real_icarus_scalar_internal_net_control_annotates_cleanly(tmp_path):
    """The control that isolates the mechanism: the byte-identical entries
    on a scalar internal net annotate cleanly -- same SDF text, same
    fanout, no diagnostics."""
    _write(tmp_path / "dut.v", _DUT_V)
    sdf_text = _sdf_text()
    for load in ("u_l1", "u_l2", "u_l3", "u_l4"):
        line = (
            f"      (INTERCONNECT u_src.Y {load}.A"
            " (0.000:0.000:0.000) (0.000:0.000:0.000))\n"
        )
        assert line in sdf_text
        sdf_text = sdf_text.replace(line, "")
    sdf_path = _write(tmp_path / "route.sdf", sdf_text)
    _write(tmp_path / "tb.v", _raw_tb_v(sdf_path))

    transcript = _compile_and_run(tmp_path)

    assert "Could not find intermodpath!" not in transcript
    assert "Could not insert intermodpath!" not in transcript
    assert "DONE" in transcript


# --------------------------------------------------------------------------- #
# End-to-end through the verb (real cocotb + Icarus; skipped, not failed,
# on machines without them -- the sibling integration fixtures' posture)
# --------------------------------------------------------------------------- #


_TESTBENCH_PY = """\
import cocotb
from cocotb.triggers import Timer


@cocotb.test()
async def test_outputs_settle(dut):
    dut_in = getattr(dut, "in")
    dut_in.value = 0
    await Timer(10, unit="ns")
    dut_in.value = 1
    await Timer(5, unit="ns")
"""


def _run_pipeline(tmp_path: Path) -> dict:
    _write(tmp_path / "dut.v", _DUT_V)
    _write(tmp_path / "test_dut.py", _TESTBENCH_PY)
    _write(tmp_path / "route.sdf", _sdf_text())
    request = {
        "engine": "icarus",
        "sources": ["dut.v"],
        "hdl_toplevel": "dut",
        "testbench": {"module": "test_dut"},
        "options": {"sdf": {"file": "route.sdf", "corner": "typ"}},
    }
    return run_functional_verification(
        _write_request(tmp_path / "request.json", request)
    )


@pytest.mark.skipif(not HAVE_COCOTB, reason="cocotb is not installed on this machine")
@pytest.mark.skipif(not HAVE_ICARUS, reason="iverilog is not installed on this machine")
def test_integration_real_icarus_internal_bus_net_zero_delay_is_dropped(tmp_path):
    """The 0-of-4 fixture completes once the class drops the entries; the
    response reports the new class, and the normalized copy carries no
    INTERCONNECT entry at all."""
    report = _run_pipeline(tmp_path)

    sdf = report["environment"]["sdf"]
    assert sdf["partial"] is True
    assert sdf["dropped"][fv.SDF_INTERNAL_BUS_NET_DROPPED_CLASS]["count"] == 4
    normalized = (
        tmp_path
        / ".klt"
        / "functional-verification"
        / ("klt_sdf_internal_bus_net_dropped.sdf")
    )
    assert normalized.exists()
    normalized_text = normalized.read_text(encoding="utf-8")
    assert "u_src.Y" not in normalized_text
    # The byte-identical scalar-net entries stay: only the bit-selected
    # internal net's entries were removed.
    assert normalized_text.count("(INTERCONNECT") == 4


@pytest.mark.skipif(not HAVE_COCOTB, reason="cocotb is not installed on this machine")
@pytest.mark.skipif(not HAVE_ICARUS, reason="iverilog is not installed on this machine")
def test_integration_real_icarus_internal_bus_net_mixed_keeps_failing(tmp_path):
    """The #5769 boundary, pinned live: the non-zero entry on the same net
    survives the drop pass and still fails the run loudly."""
    _write(tmp_path / "dut.v", _DUT_V)
    _write(tmp_path / "test_dut.py", _TESTBENCH_PY)
    _write(
        tmp_path / "route.sdf",
        _sdf_text().replace(
            "(INTERCONNECT u_src.Y u_l4.A (0.000:0.000:0.000) (0.000:0.000:0.000))",
            "(INTERCONNECT u_src.Y u_l4.A (5.000:5.000:5.000) (5.000:5.000:5.000))",
        ),
    )
    request = {
        "engine": "icarus",
        "sources": ["dut.v"],
        "hdl_toplevel": "dut",
        "testbench": {"module": "test_dut"},
        "options": {"sdf": {"file": "route.sdf", "corner": "typ"}},
    }
    with pytest.raises(fv.FunctionalVerificationError, match="did not fully apply"):
        run_functional_verification(_write_request(tmp_path / "request.json", request))
