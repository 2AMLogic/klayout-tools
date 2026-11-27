"""Pin the Icarus time unit used for each kind of annotated delay."""

import json
from pathlib import Path

import pytest

from helpers.tool_probes import SKIP_NO_COCOTB, SKIP_NO_ICARUS
from klayout_tools.functional_verification import (
    FunctionalVerificationError,
    run_functional_verification,
)
from test_functional_verification_sdf_units import _refused, _sdf
from test_functional_verification_verilog_top import _stage_sdf_units

_COCOTB_BENCH = """\
import cocotb
from cocotb.triggers import RisingEdge, Timer, with_timeout
from cocotb.utils import get_sim_time

@cocotb.test()
async def check_edge(dut):
    dut.a.value = dut.b.value = 0
    await Timer(10, unit="ns")
    dut.a.value = dut.b.value = 1
    await with_timeout(RisingEdge(dut.y), 5, "ns")
    assert get_sim_time(unit="ps") == 12000
"""


@SKIP_NO_ICARUS
@pytest.mark.parametrize(
    "runner", ["verilog", pytest.param("cocotb", marks=SKIP_NO_COCOTB)]
)
@pytest.mark.parametrize(
    "celltype,instance,arc,last_unit",
    [
        ("and2", "", "INTERCONNECT u1.Y u2.A", "1ns"),
        ("and2", "", "INTERCONNECT u1.Y u2.A", "1ps"),
        ("chain", "child", "INTERCONNECT u1.Y u2.A", "1ns"),
        ("chain", "child", "INTERCONNECT u1.Y u2.A", "1ps"),
        ("and_cell", "u2", "IOPATH A Y", "1ps"),
    ],
)
def test_interconnect_uses_annotator_and_iopath_uses_cell(
    tmp_path, runner, celltype, instance, arc, last_unit
):
    cell = (
        f'(CELL (CELLTYPE "{celltype}") (INSTANCE {instance}) '
        f"(DELAY (ABSOLUTE ({arc} (2:2:2)))))"
    )
    request = Path(_stage_sdf_units(tmp_path, sdf_text=_sdf("1ns", cell)))
    dut = tmp_path / "and2.v"
    chain = dut.read_text().replace(
        "  and_cell u1(.A(a), .B(b), .Y(y));",
        "  wire mid;\n  and_cell u1(.A(a), .B(b), .Y(mid));\n"
        "  and_cell u2(.A(mid), .B(1'b1), .Y(y));",
    )
    if celltype == "chain":
        # The containing cell deliberately has a different unit from the SDF.
        # Only the annotator's unit controls this interconnect's delay.
        chain = chain.replace("1ns/1ps", "1ps/1ps").replace(
            "module and2(", "module chain("
        )
        chain += (
            "\n`timescale 1ns/1ps\nmodule and2(input a, input b, output y);\n"
            "  chain child(.a(a), .b(b), .y(y));\nendmodule\n"
        )
    dut.write_text(chain)
    spec = json.loads(request.read_text())
    if runner == "verilog":
        bench = tmp_path / "tb_and2.v"
        bench.write_text(
            bench.read_text().replace(
                "  task check",
                "  always @(posedge y) if ($realtime != 12.0)\n"
                '    $fatal(1, "edge at %0.3f ns", $realtime);\n  task check',
            )
        )
    else:
        (tmp_path / "test_edges.py").write_text(_COCOTB_BENCH)
        spec["testbench"] = {"module": "test_edges"}
        spec["sources"].remove("tb_and2.v")
    (tmp_path / "unused.v").write_text(
        f"`timescale {last_unit}/1ps\nmodule unused; endmodule\n"
    )
    spec["sources"].append("unused.v")
    request.write_text(json.dumps(spec))
    if arc.startswith("INTERCONNECT") and last_unit == "1ps":
        with pytest.raises(
            FunctionalVerificationError,
            match="INTERCONNECT.*klt_sdf_annotate.*1000x too small",
        ):
            run_functional_verification(str(request))
        artifacts = tmp_path / ".klt/functional-verification"
        assert not (artifacts / "build_icarus.log").exists()
        assert not (artifacts / "sim_build_icarus").exists()
    else:
        report = run_functional_verification(str(request))
        assert report["status"] == "pass"
        sdf = report["environment"]["sdf"]
        assert sdf["annotated"] and not sdf["partial"]
        assert sdf["timescale"] == sdf["time_unit"] == "1ns"


@pytest.mark.parametrize(
    "leaf_unit,last_unit,expected",
    [
        ("1ps", "1ns", 'CELLTYPE "and_cell"'),
        ("1ns", "1ps", "annotation module 'klt_sdf_annotate'"),
    ],
)
def test_mixed_delay_entries_in_one_cell_check_both_units(
    tmp_path, monkeypatch, leaf_unit, last_unit, expected
):
    cell = (
        '(CELL (CELLTYPE "and_cell") (INSTANCE u1) (DELAY (ABSOLUTE '
        "(IOPATH A Y (1:1:1)) ( InTeRcOnNeCt A Y (2:2:2)))))"
    )
    request = _stage_sdf_units(
        tmp_path, leaf_timescale=f"{leaf_unit}/1ps", sdf_text=_sdf("1ns", cell)
    )
    bench = tmp_path / "tb_and2.v"
    with bench.open("a") as source:
        source.write(f"\n`timescale {last_unit}/1ps\n")
    message = _refused(tmp_path, monkeypatch, request)
    assert expected in message and "1000x too small" in message
