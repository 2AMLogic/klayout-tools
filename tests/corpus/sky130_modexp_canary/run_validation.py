#!/usr/bin/env python3
"""Regenerates `results.json` -- Epic #700 Phase 4 (issue #1329): run the
*real* `2AMLogic/sky130-modexp` fleet-canary design (not the local
`tests/corpus/place_and_route/` proxy fixture) through `klt synthesize` ->
`klt place-and-route` -> `klt extract` -> `klt lvs` -> `klt drc`, all real
tools (Yosys, OpenROAD via the `openroad/orfs:latest` Docker image, KLayout),
and record the result.

**Why this is a distinct corpus directory from `place_and_route_tt_validation`
(issue #1330).** That validation substituted a directed GLS co-simulation
diff for LVS, because at the time nothing in `klt` could turn a `klt
place-and-route` `verilog_path` gate-level netlist into an LVS-comparable
reference (`docs/cli/extract.md`'s then-open "Gate-level Verilog output"
gap). Issue #1336 (merged 2026-08-23, *after* that validation) closed that
gap: `klt lvs`'s `reference.form: "gate-level-verilog"` now converts
`verilog_path` directly, so this script runs the real thing Epic #700's
text names as the oracle -- "a routed result is only accepted when `klt
lvs` matches it against the source netlist" -- rather than the GLS
substitute.

**Provenance.** `sources/modexp.v` is vendored from the real
`2AMLogic/sky130-modexp` repo's `rtl/modexp.v`, commit
`1b38ab4f1c5dc5b3396b50fcdf983f8768775e7b` (2026-08-24), Apache-2.0 licensed
-- see `README.md`'s "Provenance" section for the full attribution and why a
small (~6 KB) RTL source is vendored directly (matching this repo's own
`tests/corpus/synth_e2e_validation/sources/` convention) rather than fetched
over the network at run time, while the *generated* artifacts this script
produces (synthesized netlist, routed GDS/DEF, extracted SPICE) are never
committed -- only this file's `results.json` snapshot is.

Requires: `klt` (this repo's own CLI, `uv sync --extra dev` then activate
the venv), `yosys` on `$PATH`; `docker` (pulls `openroad/orfs:latest` if not
already cached, needs `sudo` on hosts where the invoking user is not in the
`docker` group); a resolvable, volare-fetched `sky130A` PDK install
(`~/.volare` or `$PDK_ROOT`/`$PDK`).

Deliberate, reviewed, **never a CI step** -- same convention as
`tests/corpus/place_and_route_tt_validation/run_validation.py` and
`tests/corpus/place_and_route/regenerate.sh`. A Yosys/OpenROAD/PDK version
bump can shift QoR numbers; re-running this script and re-committing
`results.json` is how that drift gets captured on purpose, not silently.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CORPUS_DIR = Path(__file__).resolve().parent
SOURCES_DIR = CORPUS_DIR / "sources"

PDK_ROOT = os.environ.get("PDK_ROOT") or str(Path.home() / ".volare")
PDK_VARIANT = os.environ.get("PDK", "sky130A")
CELL_LIBRARY = "sky130_fd_sc_hd"
CORNER = "tt_025C_1v80"

# Same floorplan/IO/clock/seed shape as the real `sky130-modexp` repo's own
# committed `flow/par-modexp.json` (`sky130-modexp#7`/PR#17) -- this script
# reproduces that request against the vendored RTL rather than copying its
# frozen output artifacts.
FLOORPLAN = {
    "method": "utilization",
    "utilization_pct": 35,
    "aspect_ratio": 1,
    "core_margin_um": 4,
    "site": "unithd",
}
IO = {"layer_h": "met3", "layer_v": "met2"}
CLOCK_PORT = "clk"
CLOCK_PERIOD_NS = 10.0
SEED = 42

DOCKER_IMAGE = "openroad/orfs:latest"
CONTAINER_OPENROAD_BIN = (
    "/OpenROAD-flow-scripts/tools/install/OpenROAD/bin:/usr/local/sbin:"
    "/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)
# Space-separated (split, never shell-parsed) -- e.g. `DOCKER_RUNNER="sudo
# docker"` on a host where the invoking user is not in the `docker` group.
DOCKER_RUNNER = os.environ.get("DOCKER_RUNNER", "docker")


def _run(
    cmd: list[str], cwd: Path, env: dict | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True, check=False
    )


def _host_env() -> dict:
    env = dict(os.environ)
    env["PDK_ROOT"] = PDK_ROOT
    env["PDK"] = PDK_VARIANT
    return env


def _klt_json(cmd: list[str], workdir: Path, env: dict | None = None) -> dict:
    """Runs a host-side `klt` subcommand and parses its JSON envelope from
    stdout (success) or stderr (application error) -- `docs/json-contract.md`."""
    proc = _run(["klt", *cmd, "--format", "json"], cwd=workdir, env=env)
    for stream in (proc.stdout, proc.stderr):
        try:
            return json.loads(stream)
        except json.JSONDecodeError:
            continue
    raise RuntimeError(
        f"klt {' '.join(cmd)} did not emit JSON: rc={proc.returncode} "
        f"stdout={proc.stdout[-4000:]!r} stderr={proc.stderr[-4000:]!r}"
    )


def klt_synthesize(workdir: Path) -> dict:
    request = {
        "schema": "klt.synthesize.request/1",
        "engine": "yosys",
        "sources": ["modexp.v"],
        "hdl_toplevel": "modexp",
        "pdk": {"cell_library": CELL_LIBRARY, "corner": CORNER},
        "constraints": {"clock_period_ns": None},
    }
    (workdir / "synth_request.json").write_text(json.dumps(request), encoding="utf-8")
    return _klt_json(["synthesize", "synth_request.json"], workdir, env=_host_env())


def klt_place_and_route(workdir: Path, netlist_rel: str) -> dict:
    """Runs the real `openroad/orfs:latest` Docker image against `workdir`
    (mounted read-write at `/workdir/scratch`), this checkout (mounted
    read-only at `/workdir/repo`, `pip install`'d fresh inside the container
    -- there is no `klt` inside the upstream image), and the host's volare
    PDK install (mounted read-only at `/workdir/volare`) -- the same recipe
    `tests/corpus/place_and_route_tt_validation/run_validation.py` and
    `docs/cli/place-and-route.md`'s "Installing OpenROAD" section document."""
    request = {
        "schema": "klt.place-and-route.request/1",
        "engine": "openroad",
        "netlist": netlist_rel,
        "hdl_toplevel": "modexp",
        "pdk": {"cell_library": CELL_LIBRARY, "corner": CORNER},
        "floorplan": FLOORPLAN,
        "io": IO,
        "constraints": {"clock_port": CLOCK_PORT, "clock_period_ns": CLOCK_PERIOD_NS},
        "seed": SEED,
        "target_stage": "route",
    }
    (workdir / "par_request.json").write_text(json.dumps(request), encoding="utf-8")

    cmd = [
        *DOCKER_RUNNER.split(),
        "run",
        "--rm",
        "-v",
        f"{REPO_ROOT}:/workdir/repo:ro",
        "-v",
        f"{workdir}:/workdir/scratch",
        "-v",
        f"{PDK_ROOT}:/workdir/volare:ro",
        "-e",
        f"PDK={PDK_VARIANT}",
        "-e",
        "PDK_ROOT=/workdir/volare",
        "-e",
        f"PATH={CONTAINER_OPENROAD_BIN}",
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
    proc = _run(cmd, cwd=workdir)

    # The container runs as root (no `--user`, so `pip3 install -q
    # /workdir/repo` can write into the image's system site-packages), so
    # every path OpenROAD writes under the bind-mounted `workdir` comes back
    # root-owned on the host. Best-effort reclaim ownership so the caller's
    # `tempfile.TemporaryDirectory` cleanup (and any host-side read below)
    # doesn't hit a `PermissionError` -- silently a no-op wherever `sudo` is
    # unavailable or unneeded (rootless Docker, a `docker`-group host user).
    if shutil.which("sudo") is not None:
        subprocess.run(
            ["sudo", "chown", "-R", f"{os.getuid()}:{os.getgid()}", str(workdir)],
            capture_output=True,
            check=False,
        )

    for stream in (proc.stdout, proc.stderr):
        try:
            return json.loads(stream)
        except json.JSONDecodeError:
            continue
    raise RuntimeError(
        f"klt place-and-route did not emit JSON: rc={proc.returncode} "
        f"stdout={proc.stdout[-4000:]!r} stderr={proc.stderr[-4000:]!r}"
    )


def klt_extract_gate_level(workdir: Path, gds_path: str) -> dict:
    """Layout side of the gate-level LVS: every standard cell a pin-only
    black box (`--abstract-cells`, issue #620), with the design's own DEF
    net names recovered (`--def-net-names`, issue #951) -- see
    `docs/cli/lvs.md`'s "Digital gate-level LVS" for why both flags are
    required for this compare to ever reach `status: "match"`."""
    return _klt_json(
        [
            "extract",
            gds_path,
            "--deck",
            "sky130",
            "--abstract-cells",
            f"{CELL_LIBRARY}__*",
            "--def-net-names",
            "-o",
            "modexp.gate.spice",
        ],
        workdir,
        env=_host_env(),
    )


def klt_lvs_gate_level(workdir: Path, verilog_path: str) -> dict:
    """Compares the routed GDS (abstracted layout side) against the same
    run's own as-built `verilog_path`, via `reference.form:
    "gate-level-verilog"` (issue #1336) -- the real oracle Epic #700's text
    names, closed after `place_and_route_tt_validation` (issue #1330) had to
    substitute a GLS diff for it."""
    request = {
        "layout": {"netlist": "modexp.gate.spice", "top": "modexp"},
        "reference": {
            "netlist": verilog_path,
            "top": "modexp",
            "form": "gate-level-verilog",
            "library": CELL_LIBRARY,
        },
    }
    (workdir / "lvs_request.json").write_text(json.dumps(request), encoding="utf-8")
    return _klt_json(["lvs", "lvs_request.json"], workdir, env=_host_env())


def klt_drc(gds_path: str) -> dict:
    # In-process, host-side -- no docker/openroad needed, this repo's own
    # `klayout` pip dependency is enough (klayout_tools.drc.run_drc, the
    # same function `klt drc` itself calls).
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from klayout_tools.drc import run_drc

    return run_drc(gds_path, "sky130")


def main() -> int:
    # `dir=$TMPDIR` (falling back to the platform default, usually `/tmp`)
    # rather than a hardcoded path: on a `yowasp-yosys` install (this repo's
    # own CI/dev-container `yosys`, a WASI-sandboxed build), the runtime
    # unconditionally preopens its *own* throwaway scratch directory at the
    # in-sandbox mountpoint `/tmp` (`yowasp_runtime`'s `run_wasm`, "preopen
    # for temporary directory ... takes priority over implicit or explicit
    # OS mounts") -- so a real host path under `/tmp` is invisible to Yosys
    # inside that sandbox and every `-s <script>.ys` read fails with a
    # spurious "No such file or directory". Not a `klt`/Yosys design
    # netlist bug -- work around it by setting `TMPDIR` to a non-`/tmp`
    # directory before running this script on a `yowasp-yosys` host.
    tmp = tempfile.mkdtemp(prefix="modexp_canary_", dir=os.environ.get("TMPDIR"))
    try:
        return _run_validation(Path(tmp))
    finally:
        # `klt_place_and_route`'s container runs as root, so some paths
        # under `tmp` come back root-owned (best-effort `chown` there
        # notwithstanding -- it silently no-ops without a passwordless
        # `sudo chown` grant). A plain `shutil.rmtree` can therefore raise
        # `PermissionError` on cleanup even though every step above already
        # succeeded and `results.json` is already written -- never let a
        # cleanup failure look like a validation failure. Fall back to
        # `sudo rm -rf`, which this environment (like the rest of this
        # script's `docker`/`chown` calls) may have a passwordless grant
        # for even when `chown` does not; if that is unavailable too, leave
        # the directory for the OS's own tmp-reaper rather than raising.
        try:
            shutil.rmtree(tmp)
        except OSError:
            subprocess.run(["sudo", "rm", "-rf", tmp], capture_output=True, check=False)


def _run_validation(workdir: Path) -> int:
    shutil.copy(SOURCES_DIR / "modexp.v", workdir / "modexp.v")

    print("--> klt synthesize modexp (real Yosys)")
    synth = klt_synthesize(workdir)
    if synth.get("status") != "ok":
        raise RuntimeError(f"klt synthesize failed: {synth}")
    netlist_rel = str(Path(synth["netlist_path"]).relative_to(workdir))

    print("--> klt place-and-route modexp (real OpenROAD, openroad/orfs)")
    par = klt_place_and_route(workdir, netlist_rel)
    if par.get("status") != "ok" or par.get("stage_reached") != "route":
        raise RuntimeError(f"klt place-and-route did not reach route: {par}")

    # The report's own paths are the *container's* view
    # (`/workdir/scratch/...`, per the bind mount in
    # `klt_place_and_route`) -- rebase onto the host path so the
    # host-side steps below (klt_drc, klt_extract, klt_lvs) can read the
    # same files the container just wrote into this identical,
    # bind-mounted `workdir` (same fixup
    # `place_and_route_tt_validation/run_validation.py` applies).
    def _host_path(container_path: str) -> str:
        return container_path.replace("/workdir/scratch", str(workdir), 1)

    gds_path = _host_path(par["gds_path"])
    verilog_path = _host_path(par["verilog_path"])

    print("--> klt drc modexp (sky130 deck)")
    drc = klt_drc(gds_path)

    print("--> klt extract modexp (gate-level layout side)")
    extract = klt_extract_gate_level(workdir, gds_path)

    print("--> klt lvs modexp (gate-level: routed GDS vs. as-built verilog_path)")
    lvs = klt_lvs_gate_level(workdir, verilog_path)

    results = {
        "design": "modexp",
        "source": {
            "repo": "2AMLogic/sky130-modexp",
            "path": "rtl/modexp.v",
            "commit": "1b38ab4f1c5dc5b3396b50fcdf983f8768775e7b",
            "license": "Apache-2.0",
        },
        "synthesize": {
            "instance_count": synth.get("instance_count"),
            "area_um2": synth.get("area_um2"),
        },
        "place_and_route": {
            "engine_version": par.get("engine_version"),
            "stage_reached": par.get("stage_reached"),
            "die_area_um2": par.get("die_area_um2"),
            "core_area_um2": par.get("core_area_um2"),
            "utilization_pct": par.get("utilization_pct"),
            "wirelength_um": par.get("wirelength_um"),
            "fmax_mhz": par.get("fmax_mhz"),
            "worst_slack_ns": par.get("worst_slack_ns"),
            "setup_violation_count": par.get("setup_violation_count"),
            "hold_violation_count": par.get("hold_violation_count"),
            "antenna_violation_count": par.get("antenna_violation_count"),
            "route_drc_violation_count": par.get("route_drc_violation_count"),
        },
        "drc": {
            "status": drc.get("status"),
            "violation_count": drc.get("violation_count"),
        },
        "extract": {
            "net_count": extract.get("net_count"),
            "pin_count": extract.get("pin_count"),
            "warning_count": len(extract.get("warnings", [])),
        },
        "lvs": {
            "status": lvs.get("status"),
            "mismatch_count": lvs.get("mismatch_count"),
            "error_count": lvs.get("error_count"),
            "category_counts": lvs.get("category_counts"),
        },
    }

    out_path = CORPUS_DIR / "results.json"
    out_path.write_text(
        json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"\nWrote {out_path}")
    print(json.dumps(results, indent=2, sort_keys=True))

    ok = (
        par.get("stage_reached") == "route"
        and drc.get("status") == "clean"
        and lvs.get("status") == "match"
        and lvs.get("error_count", 1) == 0
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
