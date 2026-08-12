#!/usr/bin/env python3
"""Three-way comparison harness for issue #809's gate-level STA spike.

Runs OpenSTA and ``native/sta/``'s ``klt-sta`` over the identical mapped
netlist + liberty for every design in ``tests/corpus/sta/netlists/`` and
reports:

* per-design critical-path (register-to-register data arrival) delay from
  both engines, and the delta;
* whether both engines picked the same startpoint/endpoint pin pair, and --
  when they did -- the largest per-node arrival delta anywhere along the
  reported path (a much stronger agreement check than the endpoint number
  alone, which two engines can match by coincidence);
* wall-clock for both, split into the two numbers that actually decide
  wrap-vs-native: a **cold** run (process start, liberty parse, netlist read,
  analysis) and a **warm per-iteration** run (liberty already parsed and
  resident, only netlist-read + analysis repeated) -- the latter being the
  cost a Phase-3 optimization loop pays per candidate.

The third leg of the three-way comparison -- "today's nothing", i.e. what
``klt synthesize`` reports now -- needs no measurement: ``timing`` is
``null`` in the response contract, at zero cost and zero information. It is
carried in the output table as a literal, not measured.

Usage (host, native engine only -- no OpenSTA on a typical dev machine)::

    python3 tests/corpus/sta/compare.py --klt-sta /path/to/klt-sta

Usage (full three-way, inside an amd64 container that has both -- see
``run_comparison.sh``, which is the supported entry point)::

    python3 compare.py --klt-sta /work/klt-sta --sta /path/to/sta \\
        --liberty /libs/.../sky130_fd_sc_hd__tt_025C_1v80.lib --json out.json
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
NETLIST_DIR = os.path.join(HERE, "netlists")

#: sky130's `tt_025C_1v80` liberty, relative to a volare/ciel PDK install's
#: `libs.ref`. Overridable with --liberty for a differently-rooted install.
DEFAULT_LIBERTY_REL = "sky130_fd_sc_hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib"

#: A period long enough that no corpus design violates setup, so `slack` is a
#: pure restatement of arrival + library setup rather than a clipped number.
DEFAULT_PERIOD_NS = 20.0

# `report_checks` detail line, with `-fields {slew capacitance input_pins}`:
#   "   0.010264    0.073654    0.376657    0.376657 v _668_/Q (cell)"
# The leading Cap/Slew columns are blank on input-pin rows, so both the
# 4-column and 2-column shapes have to match.
_PATH_LINE = re.compile(
    r"^\s*(?P<nums>(?:-?\d+\.\d+\s+){2,4})(?P<edge>[\^v])\s+(?P<pin>\S+)\s+\((?P<cell>[^)]*)\)\s*$"
)
_ARRIVAL = re.compile(r"^\s*(-?\d+\.\d+)\s+data arrival time\s*$")
_SETUP = re.compile(r"^\s*(-?\d+\.\d+)\s+library setup time\s*$")
_SLACK = re.compile(r"^\s*(-?\d+\.\d+)\s+slack\s*\((MET|VIOLATED)\)\s*$")
_MARK_ITER = re.compile(r"^MARK iter_ms (\d+)\s*$")
_MARK_LIB = re.compile(r"^MARK liberty_ms (\d+)\s*$")


def _designs() -> list[str]:
    names = sorted(
        f[: -len("_synth.v.gz")]
        for f in os.listdir(NETLIST_DIR)
        if f.endswith("_synth.v.gz")
    )
    if not names:
        raise SystemExit(f"no *_synth.v.gz fixtures in {NETLIST_DIR}")
    return names


def _meta(design: str) -> dict:
    path = os.path.join(NETLIST_DIR, f"{design}.meta.json")
    with open(path) as fh:
        return json.load(fh)


def _unpack(design: str, dest_dir: str) -> str:
    """Decompress a checked-in netlist fixture into ``dest_dir``."""
    src = os.path.join(NETLIST_DIR, f"{design}_synth.v.gz")
    dst = os.path.join(dest_dir, f"{design}_synth.v")
    with gzip.open(src, "rb") as fin, open(dst, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    return dst


# ---------------------------------------------------------------------------
# OpenSTA
# ---------------------------------------------------------------------------

_TCL = """\
set t0 [clock milliseconds]
read_liberty {liberty}
puts "MARK liberty_ms [expr {{[clock milliseconds]-$t0}}]"
for {{set i 0}} {{$i < {iters}}} {{incr i}} {{
  set t1 [clock milliseconds]
  read_verilog {netlist}
  link_design {top}
  create_clock -name clk -period {period} [get_ports clk]
  report_checks -path_delay max \\
      -from [all_registers -clock_pins] -to [all_registers -data_pins] \\
      -group_path_count 1 -digits 6 -fields {{slew capacitance input_pins}}
  puts "MARK iter_ms [expr {{[clock milliseconds]-$t1}}]"
}}
"""


def parse_opensta(text: str) -> dict:
    """Extract the worst reported path from ``report_checks`` output.

    Parses the *last* reported block, so an N-iteration warm run reports the
    same object a 1-iteration cold run does (every iteration is identical by
    construction -- OpenSTA is deterministic on a fixed netlist + liberty,
    which the harness asserts by comparing every block's arrival).
    """
    blocks = text.split("Startpoint:")
    arrivals: list[float] = []
    out: dict = {}
    for block in blocks[1:]:
        nodes = []
        arrival = setup = slack = None
        for line in block.splitlines():
            m = _PATH_LINE.match(line)
            if m:
                nums = [float(x) for x in m.group("nums").split()]
                # 4 columns: cap, slew, delay, time. 2 columns: delay, time.
                cap, slew = (nums[0], nums[1]) if len(nums) == 4 else (None, None)
                nodes.append(
                    {
                        "pin": m.group("pin"),
                        "cell": m.group("cell"),
                        "edge": "rise" if m.group("edge") == "^" else "fall",
                        "arrival_ns": nums[-1],
                        "delay_ns": nums[-2],
                        "slew_ns": slew,
                        "load_cap_pf": cap,
                    }
                )
                continue
            m = _ARRIVAL.match(line)
            if m and arrival is None:
                arrival = float(m.group(1))
                continue
            m = _SETUP.match(line)
            if m:
                setup = float(m.group(1))
                continue
            m = _SLACK.match(line)
            if m:
                slack = float(m.group(1))
        if arrival is None:
            continue
        arrivals.append(arrival)
        out = {
            "startpoint": nodes[0]["pin"] if nodes else None,
            "endpoint": nodes[-1]["pin"] if nodes else None,
            "arrival_ns": arrival,
            # OpenSTA prints setup as a negative adjustment to required time;
            # the native engine reports the library value. Normalise to the
            # library value so the two are directly comparable.
            "library_setup_ns": None if setup is None else -setup,
            "slack_ns": slack,
            "nodes": nodes,
        }
    if arrivals and (max(arrivals) - min(arrivals)) > 1e-9:
        raise SystemExit(
            f"OpenSTA was not deterministic across iterations: {sorted(set(arrivals))}"
        )
    return out


def run_opensta(
    sta: str, liberty: str, netlist: str, top: str, period: float, iters: int
) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "run.tcl")
        with open(script, "w") as fh:
            fh.write(
                _TCL.format(
                    liberty=liberty,
                    netlist=netlist,
                    top=top,
                    period=period,
                    iters=iters,
                )
            )
        t0 = time.perf_counter()
        proc = subprocess.run(
            [sta, "-exit", script], capture_output=True, text=True, check=False
        )
        wall_ms = (time.perf_counter() - t0) * 1e3
    if proc.returncode != 0:
        raise SystemExit(f"sta failed on {top}:\n{proc.stdout}\n{proc.stderr}")
    text = proc.stdout
    iter_ms = [float(m.group(1)) for m in map(_MARK_ITER.match, text.splitlines()) if m]
    lib_ms = [float(m.group(1)) for m in map(_MARK_LIB.match, text.splitlines()) if m]
    result = parse_opensta(text)
    result["timings"] = {
        "process_wall_ms": wall_ms,
        "liberty_parse_ms": lib_ms[0] if lib_ms else None,
        "iterations_ms": iter_ms,
    }
    return result


# ---------------------------------------------------------------------------
# native/sta
# ---------------------------------------------------------------------------


def run_native(
    binary: str, liberty: str, netlist: str, top: str, period: float, iters: int
) -> dict:
    cmd = [
        binary,
        "--liberty",
        liberty,
        "--netlist",
        netlist,
        "--top",
        top,
        "--period",
        str(period),
        "--paths",
        "1",
        "--repeat",
        str(iters),
        "--format",
        "json",
    ]
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    wall_ms = (time.perf_counter() - t0) * 1e3
    if proc.returncode != 0:
        raise SystemExit(f"klt-sta failed on {top}:\n{proc.stdout}\n{proc.stderr}")
    report = json.loads(proc.stdout)
    path = report["paths"][0] if report["paths"] else {}
    return {
        "startpoint": path.get("startpoint"),
        "endpoint": path.get("endpoint"),
        "arrival_ns": path.get("arrival_ns"),
        "library_setup_ns": path.get("library_setup_ns"),
        "slack_ns": path.get("slack_ns"),
        "nodes": path.get("nodes", []),
        "cells": report["cells"],
        "registers": report["registers"],
        "graph_vertices": report["graph_vertices"],
        "graph_edges": report["graph_edges"],
        "unknown_cells": report.get("unknown_cells", []),
        "combinational_loop_pins": report.get("combinational_loop_pins", []),
        "timings": {
            "process_wall_ms": wall_ms,
            "liberty_parse_ms": report["timings"]["liberty_parse_ms"],
            "iterations_ms": report["timings"]["analysis_ms_per_iteration"],
        },
    }


# ---------------------------------------------------------------------------
# comparison
# ---------------------------------------------------------------------------


def warm_ms(iterations: list[float]) -> float | None:
    """Steady-state per-iteration cost: the median of iterations 2..N.

    Discarding iteration 1 identically on both sides matters: OpenSTA's first
    `read_verilog`/`link_design` is several times slower than its steady state
    (allocator warm-up), and comparing its warmed-up mean against a cold
    native number -- or vice versa -- would fabricate a factor.
    """
    tail = iterations[1:] or iterations
    return statistics.median(tail) if tail else None


def compare_paths(native: dict, sta: dict) -> dict:
    same_endpoints = native.get("startpoint") == sta.get("startpoint") and native.get(
        "endpoint"
    ) == sta.get("endpoint")
    delta = None
    if native.get("arrival_ns") is not None and sta.get("arrival_ns") is not None:
        delta = native["arrival_ns"] - sta["arrival_ns"]
    max_node_delta = None
    node_count = None
    if same_endpoints:
        n_pins = [n["pin"] for n in native["nodes"]]
        s_pins = [n["pin"] for n in sta["nodes"]]
        if n_pins == s_pins:
            node_count = len(n_pins)
            max_node_delta = max(
                abs(a["arrival_ns"] - b["arrival_ns"])
                for a, b in zip(native["nodes"], sta["nodes"], strict=True)
            )
    return {
        "same_path_endpoints": same_endpoints,
        "same_path_pins": node_count is not None,
        "path_node_count": node_count,
        "arrival_delta_ns": delta,
        "arrival_delta_pct": (
            None
            if not delta or not sta.get("arrival_ns")
            else 100.0 * delta / sta["arrival_ns"]
        ),
        "max_node_arrival_delta_ns": max_node_delta,
        "setup_delta_ns": (
            None
            if native.get("library_setup_ns") is None
            or sta.get("library_setup_ns") is None
            else native["library_setup_ns"] - sta["library_setup_ns"]
        ),
        "slack_delta_ns": (
            None
            if native.get("slack_ns") is None or sta.get("slack_ns") is None
            else native["slack_ns"] - sta["slack_ns"]
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--klt-sta", required=True, help="path to the native/sta `klt-sta` binary"
    )
    ap.add_argument(
        "--sta", help="path to OpenSTA's `sta` (omit to skip the OpenSTA leg)"
    )
    ap.add_argument(
        "--liberty",
        help="path to the liberty file (default: $PDK_ROOT/$PDK/libs.ref/"
        + DEFAULT_LIBERTY_REL
        + ")",
    )
    ap.add_argument("--period", type=float, default=DEFAULT_PERIOD_NS)
    ap.add_argument(
        "--iters", type=int, default=20, help="analysis iterations per engine"
    )
    ap.add_argument("--designs", nargs="*", help="subset of designs (default: all)")
    ap.add_argument("--json", help="write the full result set here")
    ap.add_argument(
        "--label",
        default="",
        help="free-text label recorded in the JSON (e.g. the host)",
    )
    args = ap.parse_args()

    liberty = args.liberty
    if not liberty:
        root = os.environ.get("PDK_ROOT", os.path.expanduser("~/.volare"))
        pdk = os.environ.get("PDK", "sky130A")
        liberty = os.path.join(root, pdk, "libs.ref", DEFAULT_LIBERTY_REL)
    if not os.path.isfile(liberty):
        raise SystemExit(f"liberty not found: {liberty} (pass --liberty)")

    designs = args.designs or _designs()
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        for design in designs:
            netlist = _unpack(design, tmp)
            meta = _meta(design)
            native = run_native(
                args.klt_sta, liberty, netlist, design, args.period, args.iters
            )
            entry = {
                "design": design,
                "instance_count": meta["instance_count"],
                "area_um2": meta["area_um2"],
                "native": native,
            }
            if args.sta:
                sta = run_opensta(
                    args.sta, liberty, netlist, design, args.period, args.iters
                )
                entry["opensta"] = sta
                entry["comparison"] = compare_paths(native, sta)
            results.append(entry)

    out = {
        "schema": "klt.sta-spike.comparison/1",
        "label": args.label,
        "liberty": os.path.basename(liberty),
        "period_ns": args.period,
        "iterations": args.iters,
        "designs": results,
    }
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(out, fh, indent=2, sort_keys=True)
            fh.write("\n")

    _print_tables(out)
    return 0


def _fmt(v, spec="{:.6f}"):
    return "—" if v is None else spec.format(v)


def _same_path_label(c: dict) -> str:
    if c["same_path_pins"]:
        return "yes"
    return "endpoints only" if c["same_path_endpoints"] else "NO"


def _print_tables(out: dict) -> None:
    has_sta = any("opensta" in d for d in out["designs"])
    print()
    print(f"# STA spike comparison — {out['label'] or 'unlabelled'}")
    print(
        f"liberty: {out['liberty']}   period: {out['period_ns']} ns"
        f"   iterations: {out['iterations']}"
    )
    print()

    if has_sta:
        print("## Accuracy: register-to-register critical path")
        print()
        print(
            "| design | cells | OpenSTA (ns) | klt-sta (ns) | delta (ns) "
            "| delta (%) | same path | max node delta (ns) |"
        )
        print("|---|---:|---:|---:|---:|---:|:---:|---:|")
        for d in out["designs"]:
            c = d["comparison"]
            print(
                f"| {d['design']} | {d['instance_count']} "
                f"| {_fmt(d['opensta']['arrival_ns'])} "
                f"| {_fmt(d['native']['arrival_ns'])} "
                f"| {_fmt(c['arrival_delta_ns'], '{:+.6f}')} "
                f"| {_fmt(c['arrival_delta_pct'], '{:+.4f}')} "
                f"| {_same_path_label(c)} "
                f"| {_fmt(c['max_node_arrival_delta_ns'])} |"
            )
        print()

    print("## Wall-clock")
    print()
    header = "| design | cells | klt-sta cold (ms) | klt-sta warm/iter (ms) |"
    rule = "|---|---:|---:|---:|"
    if has_sta:
        header += (
            " OpenSTA cold (ms) | OpenSTA warm/iter (ms) | cold ratio | warm ratio |"
        )
        rule += "---:|---:|---:|---:|"
    print(header)
    print(rule)
    for d in out["designs"]:
        nw = warm_ms(d["native"]["timings"]["iterations_ms"])
        nc = d["native"]["timings"]["process_wall_ms"]
        row = (
            f"| {d['design']} | {d['instance_count']} "
            f"| {_fmt(nc, '{:.1f}')} | {_fmt(nw, '{:.3f}')} |"
        )
        if has_sta:
            sw = warm_ms(d["opensta"]["timings"]["iterations_ms"])
            sc = d["opensta"]["timings"]["process_wall_ms"]
            row += (
                f" {_fmt(sc, '{:.1f}')} | {_fmt(sw, '{:.1f}')} "
                f"| {_fmt(sc / nc if nc else None, '{:.1f}x')} "
                f"| {_fmt(sw / nw if nw else None, '{:.0f}x')} |"
            )
        print(row)
    print()
    print(
        "Third leg — today's `klt synthesize`: reports `timing: null`, "
        "i.e. 0 ms and no answer."
    )
    print()


if __name__ == "__main__":
    sys.exit(main())
