#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Reproduce the issue #2746 observability negative-control experiments.

Design-spike evidence only (docs/design/synthesize-observability-spike.md).
Nothing here is a ``klt`` feature: no request field, no CLI flag. The script

1. copies the fixture RTL into a scratch work directory (the checked-in
   sources are hashed before and after and must not change);
2. runs the real, unmodified ``klt synthesize`` on the baseline top
   (``obs_core``) and on the explicit wrapper top (``obs_core_func``);
3. derives one Yosys script per candidate mechanism from the exact script
   ``klt synthesize`` generated (same liberty, same ABC constraints, same
   ``synth -top -> dfflibmap -> abc -> clean -> stat`` order), inserting only
   the candidate pre-pass after ``hierarchy`` plus *read-only* pre-mapping
   instrumentation (``stat -json`` and ``write_json`` immediately after
   ``synth``);
4. checks every mapped netlist's functional outputs (``acc``, ``parity``,
   ``delayed``) against the original RTL with a Yosys miter + SAT
   (``sat`` bounded model check from an all-zero initial state, plus an
   unbounded ``equiv_make``/``equiv_simple``/``equiv_induct`` proof -- the
   same primitives ``../synth_e2e_validation/seq_equiv_check.sh`` uses);
5. accounts sequential storage bits from the liberty's own ``ff``/
   ``ff_bank``/``latch``/``latch_bank``/``statetable`` groups, rolled up
   through hierarchy -- never by assuming one cell type per flop;
6. writes ``results.json`` into the work directory and prints a summary.

Usage::

    python3 run_experiments.py --workdir /tmp/obs2746 [--klt "uv run klt"]
        [--cell-library gf180mcu_fd_sc_mcu7t5v0] [--corner tt_025C_1v80]

Requires ``yosys`` on PATH and a resolvable open-PDK liberty (the same
``find_pdk()`` resolution ``klt synthesize`` uses). Headless; no GUI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = ("obs_core.v", "obs_core_func.v")
FUNCTIONAL_PORTS = ("clk", "rst_n", "en", "din", "acc", "parity", "delayed")
DBG_PORTS = ("dbg_probe", "dbg_shared", "dbg_hist")
BMC_DEPTH = 24


def sha256(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def run(cmd: list[str], *, cwd: str | None = None, check: bool = True) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise SystemExit(f"command failed ({proc.returncode}): {shlex.join(cmd)}")
    return proc.stdout + proc.stderr


# --------------------------------------------------------------------------
# Liberty sequential-cell classification
# --------------------------------------------------------------------------

_CELL_RE = re.compile(r'^\s*cell\s*\(\s*"?([A-Za-z0-9_]+)"?\s*\)\s*\{')
_SEQ_RE = re.compile(r"^\s*(ff_bank|ff|latch_bank|latch|statetable)\s*\(([^)]*)\)")


def classify_liberty(lib_path: str) -> dict[str, dict[str, int]]:
    """Map cell name -> {"ff_bits", "latch_bits", "statetable"}.

    ``ff``/``latch`` contribute one bit each; ``ff_bank``/``latch_bank``
    contribute their declared width (third group argument). ``statetable``
    cells (e.g. integrated clock gates) are flagged, not counted as storage.
    Only cells with at least one sequential group are returned.

    Groups nested inside a ``test_cell { ... }`` block are skipped: scan
    flops repeat their ``ff`` group there (the non-scan test view), and
    counting it would report two bits per scan flop.
    """
    result: dict[str, dict[str, int]] = {}
    current: str | None = None
    depth = 0
    test_cell_depth: int | None = None
    with open(lib_path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            cell = _CELL_RE.match(line)
            if cell:
                current = cell.group(1)
            if re.match(r"^\s*test_cell\s*\(", line) and test_cell_depth is None:
                test_cell_depth = depth
            seq = _SEQ_RE.match(line)
            depth += line.count("{") - line.count("}")
            if test_cell_depth is not None:
                if depth <= test_cell_depth:
                    test_cell_depth = None
                continue
            if cell or not seq or current is None:
                continue
            kind, args = seq.group(1), seq.group(2)
            entry = result.setdefault(
                current, {"ff_bits": 0, "latch_bits": 0, "statetable": 0}
            )
            if kind == "statetable":
                entry["statetable"] += 1
                continue
            width = 1
            if kind.endswith("_bank"):
                parts = [p.strip().strip('"') for p in args.split(",")]
                width = int(parts[2]) if len(parts) > 2 else 1
            entry["ff_bits" if kind.startswith("ff") else "latch_bits"] += width
    return result


def leaf_counts(modules: dict[str, Any], key: str) -> dict[str, int]:
    """Hierarchical leaf-cell rollup of a ``stat -json`` modules dict (same
    rule as ``synthesize._aggregate_cell_counts``: submodule pseudo-types are
    expanded and scaled by their instance count)."""
    out: dict[str, int] = {}
    for ctype, n in (modules[key].get("num_cells_by_type") or {}).items():
        sub = f"\\{ctype}"
        if sub in modules:
            for leaf, m in leaf_counts(modules, sub).items():
                out[leaf] = out.get(leaf, 0) + n * m
        else:
            out[ctype] = out.get(ctype, 0) + n
    return out


_YOSYS_FF = re.compile(r"^\$_(S?DFF|DFFE|SDFFE|SDFFCE|DFFSR|DFFSRE|ALDFF|ALDFFE)_")
_YOSYS_LATCH = re.compile(r"^\$_(DLATCH|DLATCHSR|SR)_")


def storage_bits(
    counts: dict[str, int], seq_cells: dict[str, dict[str, int]]
) -> dict[str, Any]:
    ff = latch = statetable = 0
    by_type: dict[str, int] = {}
    unmapped: dict[str, int] = {}
    for ctype, n in sorted(counts.items()):
        if ctype in seq_cells:
            info = seq_cells[ctype]
            ff += n * info["ff_bits"]
            latch += n * info["latch_bits"]
            statetable += n * info["statetable"]
            by_type[ctype] = n
        elif _YOSYS_FF.match(ctype):
            # Pre-mapping fine-grained Yosys primitive: one bit per cell.
            ff += n
            by_type[ctype] = n
        elif _YOSYS_LATCH.match(ctype):
            latch += n
            by_type[ctype] = n
        elif ctype.startswith("$") and ("dff" in ctype or "latch" in ctype):
            unmapped[ctype] = n  # coarse multi-bit cell: width not in stat
    return {
        "ff_bits": ff,
        "latch_bits": latch,
        "statetable_cells": statetable,
        "sequential_cells_by_type": by_type,
        "unaccounted_coarse_cells": unmapped,
    }


def premap_inventory(premap_json: str) -> dict[str, list[str]]:
    """Per-module list of named register bits whose FF survived ``synth``."""
    with open(premap_json, encoding="utf-8") as handle:
        data = json.load(handle)
    inventory: dict[str, list[str]] = {}
    for mod_name, mod in data["modules"].items():
        aliases: dict[int, list[str]] = {}
        for net, info in mod.get("netnames", {}).items():
            if info.get("hide_name"):
                continue
            for idx, bit in enumerate(info["bits"]):
                if isinstance(bit, int):
                    label = net if len(info["bits"]) == 1 else f"{net}[{idx}]"
                    aliases.setdefault(bit, []).append(label)
        # A Q bit usually has several public aliases (the register, an
        # output port, a submodule input after flattening). The fixture's
        # convention names every register `*_q`, so prefer that alias.
        bit_names = {
            bit: sorted(labels, key=lambda s: (not s.split("[")[0].endswith("_q"), s))[
                0
            ]
            for bit, labels in aliases.items()
        }
        names = []
        for cell in mod.get("cells", {}).values():
            if not _YOSYS_FF.match(cell["type"]):
                continue
            for bit in cell["connections"].get("Q", []):
                names.append(bit_names.get(bit, f"<anon {bit}>"))
        inventory[mod_name] = sorted(names)
    return inventory


# --------------------------------------------------------------------------
# Variants
# --------------------------------------------------------------------------

ATTR_TOP = "attr_top"
ATTR_SUB = "attr_sub"


def attributed_copy(src: str, dst: str, *, where: str) -> None:
    text = open(src, encoding="utf-8").read()
    if where == ATTR_TOP:
        for port in DBG_PORTS:
            text = re.sub(
                rf"(output\s+wire\s+\[\d+:0\]\s+{port}\b)",
                r"(* klt_observe *) \1",
                text,
            )
    else:  # probe_unit's own output port: a module-scoped attribute
        text = text.replace(
            "output wire [3:0] q\n", "(* klt_observe *) output wire [3:0] q\n"
        )
    with open(dst, "w", encoding="utf-8") as handle:
        handle.write(text)


VARIANTS: list[dict[str, Any]] = [
    {
        "id": "A_baseline",
        "mechanism": "none (production flow)",
        "top": "obs_core",
        "prepass": [],
    },
    {
        "id": "A2_baseline_flatten",
        "mechanism": "none + synth -flatten (flatten-matched baseline)",
        "top": "obs_core",
        "prepass": [],
        "flatten": True,
    },
    {
        "id": "B_wrapper",
        "mechanism": "explicit wrapper/top variant, production flow (no flatten)",
        "top": "obs_core_func",
        "prepass": [],
    },
    {
        "id": "C_wrapper_flatten",
        "mechanism": "explicit wrapper/top variant + synth -flatten",
        "top": "obs_core_func",
        "prepass": [],
        "flatten": True,
    },
    {
        "id": "D_toport_names",
        "mechanism": "caller-selected top-level output port names (no flatten)",
        "top": "obs_core",
        "prepass": [
            *[f"select -assert-count 1 obs_core/{p}" for p in DBG_PORTS],
            "delete -port " + " ".join(f"obs_core/{p}" for p in DBG_PORTS),
        ],
    },
    {
        "id": "E_toport_names_flatten",
        "mechanism": "caller-selected top-level output port names + synth -flatten",
        "top": "obs_core",
        "prepass": [
            *[f"select -assert-count 1 obs_core/{p}" for p in DBG_PORTS],
            "delete -port " + " ".join(f"obs_core/{p}" for p in DBG_PORTS),
        ],
        "flatten": True,
    },
    {
        "id": "F_attr_top_ports",
        "mechanism": "(* klt_observe *) on top-level dbg ports (edited source copy)",
        "top": "obs_core",
        "attr": ATTR_TOP,
        "prepass": ["select -assert-any a:klt_observe", "delete -port a:klt_observe"],
        "flatten": True,
    },
    {
        "id": "G_attr_submodule_port",
        "mechanism": "(* klt_observe *) on probe_unit.q (module-scoped attribute)",
        "top": "obs_core",
        "attr": ATTR_SUB,
        "prepass": ["select -assert-any a:klt_observe", "delete -port a:klt_observe"],
        "flatten": True,
        "expect_functional_change": True,
    },
    {
        "id": "H_internal_name_shared_q",
        "mechanism": (
            "caller-selected internal signal name `shared_q` (deleted after proc)"
        ),
        "top": "obs_core",
        "prepass": [
            "proc",
            "select -assert-count 1 obs_core/shared_q",
            "delete obs_core/shared_q",
        ],
        "flatten": True,
        "expect_functional_change": True,
    },
]


def derive_script(
    klt_script: str,
    variant: dict[str, Any],
    vdir: str,
    sources: list[str],
    *,
    evidence_only: bool = False,
) -> str:
    """Derive a variant script from the klt-generated one.

    ``evidence_only=False``: the mapping run. Its only addition besides the
    pre-pass is ``stat -json`` after ``synth`` -- verified not to perturb
    the mapped result (variant A must reproduce klt's own response exactly;
    ``main`` asserts this).

    ``evidence_only=True``: the same prefix through ``synth``, then
    ``write_json`` and stop. ``write_json`` is kept out of the mapping run
    because it was measured to change ABC's result on this fixture
    (3406.95 -> 3402.56 um2 on variant A with Yosys 0.69+post) -- it is not
    a read-only observer of downstream passes.
    """
    lines = [
        ln
        for ln in open(klt_script, encoding="utf-8").read().splitlines()
        if ln and not ln.startswith("#")
    ]
    out: list[str] = []
    top = variant["top"]
    for ln in lines:
        if ln.startswith("read_verilog "):
            continue
        if ln.startswith("hierarchy "):
            out += [f"read_verilog {s}" for s in sources]
            out.append(ln)
            out += variant["prepass"]
            continue
        if ln.startswith("synth "):
            out.append(f"synth -flatten -top {top}" if variant.get("flatten") else ln)
            if evidence_only:
                out.append(f"write_json {vdir}/premap.json")
                break
            out.append(f"tee -q -o {vdir}/premap_stats.json stat -json -top {top}")
            continue
        ln = re.sub(r"-o \S+_abc\.log", f"-o {vdir}/abc.log", ln)
        ln = re.sub(r"-o \S+_stats\.json", f"-o {vdir}/stats.json", ln)
        ln = re.sub(
            r"^write_verilog -noattr \S+", f"write_verilog -noattr {vdir}/netlist.v", ln
        )
        out.append(ln)
    return "\n".join(out) + "\n"


def equivalence(vdir: str, gate_top: str, src_dir: str, lib: str) -> dict[str, Any]:
    """Functional outputs of the mapped netlist vs. the original RTL."""
    gold_srcs = " ".join(os.path.join(src_dir, f) for f in FIXTURES)
    netlist = os.path.join(vdir, "netlist.v")
    gate_cleanup = ""
    with open(netlist, encoding="utf-8") as handle:
        text = handle.read()
    present = [p for p in DBG_PORTS if re.search(rf"\boutput\b[^;]*\b{p}\b", text)]
    if present:
        # Post-map port-flag removal only: no optimisation runs afterwards,
        # so the mapped logic is compared exactly as synthesized.
        gate_cleanup = (
            "delete -port " + " ".join(f"{gate_top}/{p}" for p in present) + "; "
        )
    common = (
        f"read_verilog {gold_srcs}; hierarchy -top obs_core_func; proc; flatten; "
        "rename obs_core_func gold; design -stash gold; "
        f"read_liberty -ignore_miss_func {lib}; read_verilog {netlist}; "
        f"hierarchy -top {gate_top}; {gate_cleanup}flatten; rename {gate_top} gate; "
        "design -copy-from gold -as gold gold; "
        "miter -equiv -flatten -make_assert -ignore_gold_x gold gate miter; "
        "hierarchy -top miter; async2sync; "
    )
    bmc = subprocess.run(
        [
            "yosys",
            "-p",
            common
            + f"sat -verify -prove-asserts -set-init-zero -seq {BMC_DEPTH} miter",
        ],
        capture_output=True,
        text=True,
    )
    # Unbounded check: the same equiv_make/equiv_simple/equiv_induct
    # primitives tests/corpus/synth_e2e_validation/seq_equiv_check.sh uses,
    # matching the functional output ports by name.
    induct_cmd = (
        f"read_verilog {gold_srcs}; hierarchy -top obs_core_func; proc; flatten; "
        "opt_clean; rename obs_core_func gold; design -stash gold; "
        f"read_liberty -ignore_miss_func {lib}; read_verilog {netlist}; "
        f"hierarchy -top {gate_top}; {gate_cleanup}flatten; rename {gate_top} gate; "
        "design -copy-from gold -as gold gold; async2sync; "
        "equiv_make gold gate equiv; hierarchy -top equiv; "
        "equiv_simple -seq 5; equiv_induct -seq 5; equiv_status -assert"
    )
    ind = subprocess.run(["yosys", "-p", induct_cmd], capture_output=True, text=True)
    with open(os.path.join(vdir, "equiv_bmc.log"), "w", encoding="utf-8") as handle:
        handle.write(bmc.stdout + bmc.stderr)
    with open(os.path.join(vdir, "equiv_induct.log"), "w", encoding="utf-8") as handle:
        handle.write(ind.stdout + ind.stderr)
    ind_out = ind.stdout + ind.stderr
    unproven = re.findall(r"Unproven \$equiv \S+ (\S+) (\S+)", ind_out)
    if ind.returncode == 0 and "Equivalence successfully proven!" in ind_out:
        induct = "proven"
    elif unproven:
        induct = "unproven: " + ", ".join(sorted({g.lstrip("\\") for g, _ in unproven}))
    else:
        induct = "inconclusive"
    return {
        "compared_outputs": ["acc", "parity", "delayed"],
        "bmc_initial_state": "all registers zero on both sides (-set-init-zero)",
        "bmc_depth": BMC_DEPTH,
        "bmc": "pass" if bmc.returncode == 0 else "FAIL",
        "equiv_induct": induct,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--klt", default="klt", help='klt command, e.g. "uv run klt"')
    ap.add_argument("--cell-library", default="gf180mcu_fd_sc_mcu7t5v0")
    ap.add_argument("--corner", default=None)
    args = ap.parse_args()

    work = os.path.abspath(args.workdir)
    if os.path.exists(work) and os.listdir(work):
        raise SystemExit(f"--workdir '{work}' must be empty or absent")
    src_dir = os.path.join(work, "src")
    os.makedirs(src_dir)
    fixture_hashes = {f: sha256(os.path.join(HERE, f)) for f in FIXTURES}
    for f in FIXTURES:
        shutil.copy2(os.path.join(HERE, f), src_dir)
    attributed_copy(
        os.path.join(src_dir, "obs_core.v"),
        os.path.join(src_dir, "obs_core_attr_top.v"),
        where=ATTR_TOP,
    )
    attributed_copy(
        os.path.join(src_dir, "obs_core.v"),
        os.path.join(src_dir, "obs_core_attr_sub.v"),
        where=ATTR_SUB,
    )

    klt = shlex.split(args.klt)
    responses: dict[str, Any] = {}
    for top in ("obs_core", "obs_core_func"):
        req = {
            "schema": "klt.synthesize.request/1",
            "engine": "yosys",
            "sources": list(FIXTURES) if top == "obs_core_func" else ["obs_core.v"],
            "hdl_toplevel": top,
            "pdk": {"cell_library": args.cell_library},
            "run_id": f"klt_{top}",
        }
        if args.corner:
            req["pdk"]["corner"] = args.corner
        req_path = os.path.join(src_dir, f"request_{top}.json")
        with open(req_path, "w", encoding="utf-8") as handle:
            json.dump(req, handle, indent=2)
        out = run([*klt, "synthesize", req_path, "--format", "json"], cwd=HERE)
        responses[top] = json.loads(out[out.index("{") :])
        with open(
            os.path.join(work, f"klt_response_{top}.json"), "w", encoding="utf-8"
        ) as handle:
            json.dump(responses[top], handle, indent=2)

    base_prov = responses["obs_core"]["provenance"]
    lib = (
        base_prov["liberty"]["path"]
        if isinstance(base_prov.get("liberty"), dict)
        else None
    )
    run_dirs = {
        top: os.path.join(src_dir, ".klt", "synthesize", f"klt_{top}")
        for top in responses
    }

    def klt_script(top: str) -> str:
        rd = run_dirs[top]
        runs = [f for f in os.listdir(rd) if f.endswith(".run.ys")]
        return os.path.join(rd, runs[0] if runs else f"synth_{top}.ys")

    if lib is None:
        m = re.search(r"dfflibmap -liberty (\S+)", open(klt_script("obs_core")).read())
        lib = m.group(1)
    seq_cells = classify_liberty(lib)

    results: dict[str, Any] = {
        "issue": 2746,
        "yosys_version": run(["yosys", "-V"]).strip(),
        "liberty_path_basename": os.path.basename(lib),
        "liberty_sha256": sha256(lib),
        "fixture_sha256": fixture_hashes,
        "klt_provenance": {
            "engine_version": responses["obs_core"].get("engine_version"),
            "pdk": base_prov.get("pdk"),
            "deck": base_prov.get("deck"),
        },
        "klt_responses": {
            top: {
                "instance_count": r["instance_count"],
                "area_um2": r["area_um2"],
                "sequential_area_um2": r["sequential_area_um2"],
                "netlist_sha256": r.get("netlist_sha256"),
                "storage": storage_bits(r["instance_counts_by_type"], seq_cells),
            }
            for top, r in responses.items()
        },
        "variants": {},
    }

    for variant in VARIANTS:
        vdir = os.path.join(work, variant["id"])
        os.makedirs(vdir)
        base_top_script = klt_script(
            "obs_core_func" if variant["top"] == "obs_core_func" else "obs_core"
        )
        if variant.get("attr") == ATTR_TOP:
            sources = [os.path.join(src_dir, "obs_core_attr_top.v")]
        elif variant.get("attr") == ATTR_SUB:
            sources = [os.path.join(src_dir, "obs_core_attr_sub.v")]
        elif variant["top"] == "obs_core_func":
            sources = [os.path.join(src_dir, f) for f in FIXTURES]
        else:
            sources = [os.path.join(src_dir, "obs_core.v")]
        script = derive_script(base_top_script, variant, vdir, sources)
        script_path = os.path.join(vdir, "synth.ys")
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write(script)
        evidence_path = os.path.join(vdir, "premap_evidence.ys")
        with open(evidence_path, "w", encoding="utf-8") as handle:
            handle.write(
                derive_script(
                    base_top_script, variant, vdir, sources, evidence_only=True
                )
            )
        proc = subprocess.run(
            ["yosys", "-s", script_path], capture_output=True, text=True
        )
        with open(os.path.join(vdir, "yosys.log"), "w", encoding="utf-8") as handle:
            handle.write(proc.stdout + proc.stderr)
        if proc.returncode == 0:
            run(["yosys", "-q", "-s", evidence_path])
        entry: dict[str, Any] = {
            "mechanism": variant["mechanism"],
            "top": variant["top"],
            "prepass": variant["prepass"],
            "flatten": bool(variant.get("flatten")),
            "script_sha256": sha256(script_path),
            "yosys_exit": proc.returncode,
            "undriven_warnings": len(
                re.findall(r"is used but has no driver", proc.stdout)
            ),
        }
        if proc.returncode != 0:
            entry["error"] = [
                ln.strip()
                for ln in (proc.stdout + proc.stderr).splitlines()
                if "ERROR" in ln
            ][-1:]
            results["variants"][variant["id"]] = entry
            continue
        with open(os.path.join(vdir, "stats.json"), encoding="utf-8") as handle:
            post = json.load(handle)["modules"]
        with open(os.path.join(vdir, "premap_stats.json"), encoding="utf-8") as handle:
            pre = json.load(handle)["modules"]
        key = f"\\{variant['top']}"
        post_counts = leaf_counts(post, key)
        entry["premap"] = {
            "storage": storage_bits(leaf_counts(pre, key), seq_cells),
            "register_bits_by_module": premap_inventory(
                os.path.join(vdir, "premap.json")
            ),
        }
        entry["mapped"] = {
            # `synth -flatten` leaves non-physical `$scopeinfo` cells
            # (Yosys >= 0.4x) in `stat`; exclude them so flattened and
            # hierarchical variants count the same physical instances.
            "instance_count": sum(
                n for t, n in post_counts.items() if t != "$scopeinfo"
            ),
            "scopeinfo_cells": post_counts.get("$scopeinfo", 0),
            "area_um2": post[key].get("area"),
            "netlist_sha256": sha256(os.path.join(vdir, "netlist.v")),
            "storage": storage_bits(post_counts, seq_cells),
        }
        entry["equivalence"] = equivalence(vdir, variant["top"], src_dir, lib)
        entry["expect_functional_change"] = bool(
            variant.get("expect_functional_change")
        )
        results["variants"][variant["id"]] = entry

    # The derived mapping scripts must reproduce klt's own numbers exactly
    # for the two variants that need no pre-pass; otherwise the derivation
    # itself perturbed the flow and every comparison below is suspect.
    reproduces = {}
    for vid, top in (("A_baseline", "obs_core"), ("B_wrapper", "obs_core_func")):
        mapped = results["variants"][vid]["mapped"]
        reproduces[vid] = (
            mapped["instance_count"] == responses[top]["instance_count"]
            and abs(mapped["area_um2"] - responses[top]["area_um2"]) < 1e-6
        )
    results["derived_scripts_reproduce_klt"] = reproduces

    # Selector validation probes (no synthesis).
    probe = run(
        [
            "yosys",
            "-p",
            f"read_verilog {os.path.join(src_dir, 'obs_core.v')}; "
            "hierarchy -top obs_core; delete -port obs_core/dbg_nope; "
            "select -list w:probe_q",
        ],
    )
    # A wrapper naming a port obs_core does not have (typo'd observation
    # port) must be rejected by elaboration, not silently ignored.
    bad_wrapper = os.path.join(src_dir, "bad_wrapper.v")
    with open(bad_wrapper, "w", encoding="utf-8") as handle:
        handle.write(
            "module bad_wrapper (input clk, input rst_n, input en,\n"
            "                    input [7:0] din, output [7:0] acc);\n"
            "    obs_core u_core (.clk(clk), .rst_n(rst_n), .en(en), .din(din),\n"
            "                     .acc(acc), .dbg_nope());\n"
            "endmodule\n"
        )
    wrap = subprocess.run(
        [
            "yosys",
            "-p",
            f"read_verilog {os.path.join(src_dir, 'obs_core.v')} {bad_wrapper}; "
            "hierarchy -check -top bad_wrapper",
        ],
        capture_output=True,
        text=True,
    )
    results["selector_probes"] = {
        "delete_nonexistent_port_warning_only": "did not match any object" in probe,
        "name_probe_q_matches": [
            ln.strip() for ln in probe.splitlines() if ln.strip().endswith("/probe_q")
        ],
        "wrapper_unknown_port_rejected": wrap.returncode != 0
        and "does not have a port named 'dbg_nope'" in wrap.stdout + wrap.stderr,
    }
    after = {f: sha256(os.path.join(HERE, f)) for f in FIXTURES}
    results["fixture_sources_unchanged"] = after == fixture_hashes

    with open(os.path.join(work, "results.json"), "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2, sort_keys=True)

    header = ("variant", "pre ff", "map ff", "cells", "area", "bmc", "equiv_induct")
    print("{:28} {:>6} {:>6} {:>6} {:>9}  {:5} {}".format(*header))
    for vid, e in results["variants"].items():
        if "mapped" not in e:
            print(f"{vid:28} ERROR {e.get('error')}")
            continue
        mapped, equiv = e["mapped"], e["equivalence"]
        print(
            f"{vid:28} {e['premap']['storage']['ff_bits']:>6} "
            f"{mapped['storage']['ff_bits']:>6} {mapped['instance_count']:>6} "
            f"{mapped['area_um2']:>9.2f}  {equiv['bmc']:5} {equiv['equiv_induct']}"
        )
    print(f"results: {os.path.join(work, 'results.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
