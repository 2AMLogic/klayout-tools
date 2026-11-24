"""Replay issue #6991 against the pinned gf180-antifuse checkout (see spike §3.8)."""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

LIB = "gf180mcu_fd_sc_mcu7t5v0"
CASES = (
    "specify-min",
    "specify-typ",
    "full-min",
    "full-typ",
    "iopath",
    "interconnect",
    "slow",
    "margin",
)


def monitored_netlist(source, period):
    """Sample every Q before the next top edge and trace the first missed path."""
    cells = re.findall(
        r"^\s*gf180mcu_fd_sc_mcu7t5v0__dff\w+\s+\\(\S+)\s+\(", source, re.M
    )
    if len(cells) != 96 or source.count("endmodule") != 1:
        raise ValueError("Expected the pinned 96-flop routed top")
    stop = 44 * period
    monitor = [
        'integer fd_6991; initial fd_6991=$fopen("capture.tsv","w");',
        f"always @(posedge clk) if ($realtime < {stop}) begin",
        f"  #({period} - 0.001);",
    ]
    for cell in cells:
        monitor.append(
            f'  $fdisplay(fd_6991,"S %0.3f {cell} %b", $realtime, \\{cell} .Q);'
        )
    monitor.append("end")
    for name in ("_720_", "_721_"):
        cell = f"\\u_regs/{name} ."
        for event in ("posedge CLK", "D", "Q"):
            edge, pin = ("posedge ", "CLK") if event == "posedge CLK" else ("", event)
            monitor.append(
                f"always @({edge}{cell}{pin}) if ($realtime < {stop}) "
                f'$fdisplay(fd_6991,"{event} %0.3f {name} D=%b Q=%b", '
                f"$realtime, {cell}D, {cell}Q);"
            )
    driver = r"\u_regs/_500_ ."
    pins = [driver + pin for pin in ("A1", "A2", "A3", "Z")]
    monitor.append(
        f"always @({' or '.join(pins)}) if ($realtime < {stop}) "
        '$fdisplay(fd_6991,"AND3 %0.3f A1=%b A2=%b A3=%b Z=%b", '
        f"$realtime, {', '.join(pins)});"
    )
    return source.replace("endmodule", "\n".join([*monitor, "endmodule"]))


def run_case(design, run, case, pdk_root):
    bench_dir = design / "verification/cocotb"
    req = json.loads((bench_dir / "request_digital_top_sdf.json").read_text())
    bench = (bench_dir / "test_antifuse_digital_top.py").read_text()
    period = 100 if case in ("slow", "margin") else 10
    bench = bench.replace("Clock(d.clk, 10,", f"Clock(d.clk, {period},")
    if case == "margin":
        bench = bench.replace(
            "import ClockCycles, RisingEdge", "import ClockCycles, RisingEdge, Timer"
        )
        for call in ("ClockCycles(self.dut.clk, n)", "ClockCycles(d.clk, 8)"):
            bench = bench.replace(
                f"await {call}\n", f'await {call}\n        await Timer(10, unit="ns")\n'
            )
    (run / "test_antifuse_digital_top.py").write_text(bench)
    source = (bench_dir / req["sources"][0]).read_text()
    netlist = run / "monitored.v"
    netlist.write_text(monitored_netlist(source, period))
    req["sources"] = [str(netlist)]
    options = req["options"]
    sdf = options["sdf"]
    sdf_path = (bench_dir / sdf["file"]).resolve()
    sdf["file"] = str(sdf_path)
    sdf["corner"] = "min" if case.endswith("-min") else "typ"
    req["pdk"]["root"] = str(pdk_root)
    if case.startswith("specify-"):
        # request.pdk selects FUNCTIONAL without SDF. Name the timing sources.
        del req["pdk"]
        library = pdk_root / "gf180mcuD/libs.ref" / LIB / "verilog"
        req["sources"][:0] = [str(library / "primitives.v"), str(library / f"{LIB}.v")]
        options["build_args"] = ["-gspecify", "-ginterconnect", "-T", sdf["corner"]]
        del options["sdf"]
    elif case == "interconnect":
        sdf["entries"] = "interconnect"
    elif case == "iopath":
        # OpenSTA places all INTERCONNECT in the first (top-level) CELL. An empty
        # ABSOLUTE is invalid SDF; remove its entire CELL, retaining every leaf CELL.
        separator = "\n (CELL\n"
        header, top, leaves = sdf_path.read_text().split(separator, 2)
        if "(INSTANCE)" not in top or "(IOPATH" in top or "(INTERCONNECT" in leaves:
            raise ValueError("Unexpected OpenSTA SDF layout")
        subset = run / "iopath.sdf"
        subset.write_text(header + separator + leaves)
        sdf["file"] = str(subset)
    request = run / "request.json"
    request.write_text(json.dumps(req))
    report = run / "report.json"
    with report.open("w") as output:
        result = subprocess.run(
            [
                "klt",
                "functional-verification",
                str(request),
                "-o",
                str(run / "out"),
                "--format",
                "json",
            ],
            stdout=output,
        )
    if result.returncode not in (0, 3):
        raise SystemExit(result.returncode)
    data = json.loads(report.read_text())
    print(f"{case} {data['passed_count']}/{data['test_count']} passed ({report})")


def verdicts(xml):
    cases = ElementTree.parse(xml).findall(".//testcase")
    return [(case.get("name"), [item.tag for item in case]) for case in cases]


def raw_icarus(full, raw, pdk_root):
    """Replay the original suite on the exact full-typ annotation files."""
    import cocotb_tools.config as config
    from find_libpython import find_libpython

    out = full / "out"
    library = pdk_root / "gf180mcuD/libs.ref" / LIB / "verilog"
    with (raw / "build.log").open("w") as log:
        subprocess.run(
            [
                "iverilog",
                "-o",
                "sim.vvp",
                "-s",
                "klt_sdf_dut_wrapper",
                "-g2012",
                "-gspecify",
                "-ginterconnect",
                "-T",
                "typ",
                "-s",
                "klt_sdf_annotate",
                "-f",
                out / "sim_build_icarus/cmds.f",
                library / "primitives.v",
                library / f"{LIB}.v",
                full / "monitored.v",
                out / "klt_sdf_annotate.v",
                out / "klt_sdf_dut_wrapper.v",
            ],
            cwd=raw,
            stderr=log,
            check=True,
        )
    env = dict(
        os.environ,
        LIBPYTHON_LOC=find_libpython(),
        PYGPI_PYTHON_BIN=sys.executable,
        PYTHONPATH=str(full),
        COCOTB_TOPLEVEL="klt_sdf_dut_wrapper",
        COCOTB_TEST_MODULES="test_antifuse_digital_top",
        COCOTB_RANDOM_SEED="1",
        TOPLEVEL_LANG="verilog",
    )
    with (raw / "test.log").open("w") as log:
        subprocess.run(
            [
                "vvp",
                "-M",
                config.libs_dir,
                "-m",
                config.lib_name("vpi", "icarus"),
                "sim.vvp",
                "-none",
            ],
            cwd=raw,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
    if (raw / "capture.tsv").read_bytes() != (out / "capture.tsv").read_bytes():
        raise ValueError("Raw and KLT capture traces differ")
    raw_cases = verdicts(raw / "results.xml")
    if not raw_cases or raw_cases != verdicts(out / "results_icarus.xml"):
        raise ValueError("Raw and KLT per-test outcomes differ")
    print(f"Identical captures and {len(raw_cases)} test outcomes ({raw})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "design", type=Path, help="pinned gf180-antifuse checkout with generated SDF"
    )
    parser.add_argument(
        "output", type=Path, help="output directory shared by the comparisons"
    )
    parser.add_argument("case", choices=(*CASES, "raw"))
    args = parser.parse_args()
    root = args.output.resolve()
    run_dir = root / args.case
    run_dir.mkdir(parents=True, exist_ok=True)
    pdk = Path(os.environ["PDK_ROOT"]).resolve()
    if args.case == "raw":
        raw_icarus(root / "full-typ", run_dir, pdk)
    else:
        run_case(args.design.resolve(), run_dir, args.case, pdk)
