#!/usr/bin/env python3
"""Regenerate the routed dense layouts behind
``docs/design/erc-runtime-profile.md`` (issue #2229).

Takes the vendored ``2AMLogic/sky130-modexp`` RTL
(``tests/corpus/sky130_modexp_canary/sources/modexp.v``), overrides only its
``WIDTH`` parameter default, and runs it through ``klt synthesize`` (host
Yosys) -> ``klt place-and-route`` (OpenROAD in the ``openroad/orfs:latest``
Docker image), with the modexp canary's own place-and-route request shape
(``tests/corpus/sky130_modexp_canary/run_validation.py``) plus, unless
``--no-power``, the shipped ``sky130hd`` PDN preset (tapcells, met1 rails,
met4/met5 straps, fillers).

The re-parameterised RTL, every request, every response, and the routed GDS
land in ``OUTDIR``; none of it is committed (a ``WIDTH=128`` routed GDS is
~9 MB). Deliberate, reviewed, manually run -- **never a CI step**, the same
posture as ``tests/corpus/sky130_modexp_canary/run_validation.py``.

Requires: this checkout's ``klt`` (``uv sync --extra dev``), ``yosys`` on
``$PATH``, Docker with ``openroad/orfs:latest``, and a volare-fetched
``sky130A`` (``~/.volare`` or ``$PDK_ROOT``).

Usage (from the repo root)::

    python scripts/research/regenerate_erc_profile_fixture.py WIDTH OUTDIR [--no-power]

Then profile with::

    python scripts/research/profile_erc.py \
        OUTDIR/.klt/place-and-route/modexp.gds SPEC
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RTL = REPO_ROOT / "tests/corpus/sky130_modexp_canary/sources/modexp.v"
PDK_ROOT = os.environ.get("PDK_ROOT") or str(Path.home() / ".volare")
DOCKER_IMAGE = os.environ.get("ORFS_IMAGE", "openroad/orfs:latest")
CONTAINER_PATH = (
    "/OpenROAD-flow-scripts/tools/install/OpenROAD/bin:/usr/local/sbin:"
    "/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)
PDK = {"cell_library": "sky130_fd_sc_hd", "corner": "tt_025C_1v80"}


def _klt() -> str:
    venv_klt = REPO_ROOT / ".venv" / "bin" / "klt"
    return str(venv_klt) if venv_klt.exists() else (shutil.which("klt") or "klt")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("width", type=int, help="modexp WIDTH (CW caps it at 255)")
    parser.add_argument("outdir")
    parser.add_argument("--no-power", action="store_true")
    args = parser.parse_args(argv)
    if not 2 <= args.width <= 255:
        parser.error("WIDTH must be in [2, 255]: modexp's counters are 8 bits wide")

    out = Path(args.outdir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    rtl, n = re.subn(
        r"parameter WIDTH = 16", f"parameter WIDTH = {args.width}", RTL.read_text()
    )
    if n != 1:
        raise SystemExit(f"expected exactly one 'parameter WIDTH = 16' in {RTL}")
    (out / "modexp.v").write_text(rtl)
    env = dict(os.environ, PDK_ROOT=PDK_ROOT, PDK="sky130A")

    synth_req = {
        "schema": "klt.synthesize.request/1",
        "engine": "yosys",
        "sources": ["modexp.v"],
        "hdl_toplevel": "modexp",
        "pdk": PDK,
        "constraints": {"clock_period_ns": None},
    }
    (out / "synth_request.json").write_text(json.dumps(synth_req, indent=2))
    t0 = time.time()
    proc = subprocess.run(
        [_klt(), "synthesize", "synth_request.json", "--format", "json"],
        cwd=out,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    (out / "synth_response.json").write_text(proc.stdout or proc.stderr)
    synth = json.loads(proc.stdout or proc.stderr)
    if synth.get("status") != "ok":
        raise SystemExit(f"klt synthesize failed: {synth}")
    print(
        f"synthesize: {synth.get('instance_count')} instances, {time.time() - t0:.0f}s",
        flush=True,
    )
    netlist = (
        out / ".klt/synthesize" / synth["run_id"] / "modexp_synth.v"
    ).relative_to(out)

    par_req: dict = {
        "schema": "klt.place-and-route.request/1",
        "engine": "openroad",
        "netlist": str(netlist),
        "hdl_toplevel": "modexp",
        "pdk": PDK,
        "floorplan": {
            "method": "utilization",
            "utilization_pct": 35,
            "aspect_ratio": 1,
            "core_margin_um": 4,
            "site": "unithd",
        },
        "io": {"layer_h": "met3", "layer_v": "met2"},
        "constraints": {"clock_port": "clk", "clock_period_ns": 10.0},
        "seed": 42,
        "target_stage": "route",
    }
    if not args.no_power:
        par_req["power"] = {"preset": "sky130hd"}
    (out / "par_request.json").write_text(json.dumps(par_req, indent=2))
    cmd = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "-v",
        f"{REPO_ROOT}:/workdir/repo:ro",
        "-v",
        f"{out}:/workdir/scratch",
        "-v",
        f"{PDK_ROOT}:/workdir/volare:ro",
        "-e",
        "PDK=sky130A",
        "-e",
        "PDK_ROOT=/workdir/volare",
        "-e",
        f"PATH={CONTAINER_PATH}",
        DOCKER_IMAGE,
        "bash",
        "-c",
        (
            "pip3 install -q /workdir/repo && "
            'python3 -c "from klayout_tools.cli import main; import sys; '
            'sys.exit(main([\\"place-and-route\\", '
            '\\"/workdir/scratch/par_request.json\\", \\"--format\\", \\"json\\"]))"'
        ),
    ]
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=out, capture_output=True, text=True, check=False)
    (out / "par_response.json").write_text(proc.stdout or proc.stderr)
    (out / "par_stderr.txt").write_text(proc.stderr)
    print(f"place-and-route: rc={proc.returncode}, {time.time() - t0:.0f}s", flush=True)
    par = json.loads(proc.stdout or proc.stderr)
    if par.get("status") != "ok" or par.get("stage_reached") != "route":
        raise SystemExit(f"klt place-and-route did not reach route: {par}")
    gds = par["gds_path"].replace("/workdir/scratch", str(out), 1)
    print(f"routed GDS: {gds}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
