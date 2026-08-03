#!/usr/bin/env python3
"""Generate the `klt sim` worked example fixtures: a tiny synthetic
testbench, a synthetic two-corner model library, and the request JSON that
sweeps them (see `docs/cli/sim.md`).

These fixtures are deliberately **not** a real PDK deck -- CLAUDE.md's "open
PDKs only" rule is about design-rule/model data this repo *vendors*; a
worked example only needs to exercise the contract shape (`.lib` process
selection, `alter`-based supply sweep, `.temp`, a `.meas` limit), which a
tiny fabricated RC divider does without any external PDK dependency (the
same reasoning `examples/drc/generate.py` uses for its synthetic GDS).

Unlike `examples/drc/example.drc.json`, this example has no committed golden
JSON output: `klt sim`'s response carries `runtime_s` (wall-clock, inherently
nondeterministic) and `engine_version` (whatever ngspice is installed), so a
byte-exact fixture would be flaky by construction. `docs/cli/sim.md` shows an
illustrative (trimmed) response instead; `tests/test_sim.py`'s
`test_examples_sim_*` tests run this example live (skipped when ngspice is
not installed) and assert the documented *shape*, not byte equality.

Run from the repo root:

    uv run python3 examples/sim/generate.py
"""

import json
import os

_DIR = os.path.dirname(os.path.abspath(__file__))


def write_testbench() -> None:
    """A resistor divider whose midpoint depends on the corner's ``vdd`` and
    a corner-selected scale factor -- enough to exercise every axis without
    needing any device models."""
    path = os.path.join(_DIR, "testbench.spice")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            "* klt sim worked example: resistor-divider testbench body\n"
            "* (no .control/.end -- klt sim generates those, see docs/cli/sim.md)\n"
            ".param vdd=1.8\n"
            "Vdd vdd 0 DC {vdd}\n"
            "R1 vdd out {1k*corner_scale}\n"
            "R2 out 0 1k\n"
            "C1 out 0 1n\n"
        )
    print(f"wrote {path}")


def write_corner_lib() -> None:
    """A synthetic two-corner (``tt``/``ss``) library defining ``corner_scale``,
    standing in for a PDK's process-corner `.lib` sections."""
    path = os.path.join(_DIR, "corner.lib")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            ".lib tt\n"
            ".param corner_scale=1.0\n"
            ".endl tt\n"
            "\n"
            ".lib ss\n"
            ".param corner_scale=1.15\n"
            ".endl ss\n"
        )
    print(f"wrote {path}")


def write_request() -> None:
    request = {
        "netlist": "testbench.spice",
        "engine": "ngspice",
        "models": {"lib": "corner.lib"},
        "corners": {
            "process": ["tt", "ss"],
            "supply_v": {"vdd": [1.62, 1.98]},
            "temperature_c": [-40, 125],
        },
        "analysis": {"kind": "tran", "args": "1n 5u"},
        "measurements": [
            {
                "name": "vout",
                "spice": ".meas tran vout FIND v(out) AT=5u",
                "unit": "V",
                "limits": {"min": 0.75, "max": 1.05},
            }
        ],
        "options": {"timeout_s": 30, "keep_artifacts": False},
    }
    path = os.path.join(_DIR, "request.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(request, handle, indent=2)
        handle.write("\n")
    print(f"wrote {path}")


def write_monte_carlo_testbench() -> None:
    """The Monte Carlo variant of the divider: `R1`'s value is drawn from a
    Gaussian rather than fixed, so the sampled runs actually *spread*.

    ngspice's ``agauss`` is seeded by the ``.options seed=`` card ``klt sim``
    writes ahead of the ``.include`` (see docs/cli/sim.md's "Monte Carlo
    sampling" section), which is the same mechanism a mismatch-aware PDK
    model library's own ``AGAUSS``/``GAUSS`` device parameters go through --
    so a five-line synthetic netlist exercises the full sample -> statistics
    path (spread, sigma, quantiles, the ``mean +/- k*sigma`` window) with no
    PDK dependency at all. Kept separate from ``testbench.spice`` so the
    deterministic PVT example stays deterministic.
    """
    path = os.path.join(_DIR, "testbench-mc.spice")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            "* klt sim worked example: Monte Carlo divider testbench body\n"
            "* R1 is drawn from N(1k, 5%) per sample, seeded by the\n"
            "* `.options seed=` card klt sim writes (see docs/cli/sim.md).\n"
            ".param vdd=1.8\n"
            ".param r1_scale=agauss(1, 0.05, 1)\n"
            "Vdd vdd 0 DC {vdd}\n"
            "R1 vdd out {1k*corner_scale*r1_scale}\n"
            "R2 out 0 1k\n"
            "C1 out 0 1n\n"
        )
    print(f"wrote {path}")


def write_monte_carlo_request() -> None:
    """A Monte Carlo variant of `request.json`: a single `tt` corner
    re-sampled `n=20` times instead of the PVT cross product, exercising both
    halves of `monte_carlo` (see docs/cli/sim.md's "Monte Carlo sampling"
    section) against synthetic fixtures -- the seeded sampling/fan-out
    contract, and the statistics rollup those samples reduce to.

    `quantiles` is declared explicitly here even though `[5, 50, 95]` is the
    default, so the example shows the field (and its `environment` echo);
    `k_sigma: 3` asks the classic signoff question -- does `mean +/- 3*sigma`
    still fit inside the measurement's limit window?
    """
    request = {
        "netlist": "testbench-mc.spice",
        "engine": "ngspice",
        "models": {"lib": "corner.lib"},
        "corners": {"process": ["tt"]},
        "monte_carlo": {
            "n": 20,
            "seed": 20260801,
            "vary": "mismatch",
            "quantiles": [5, 50, 95],
            "k_sigma": 3,
        },
        "analysis": {"kind": "tran", "args": "1n 5u"},
        "measurements": [
            {
                "name": "vout",
                "spice": ".meas tran vout FIND v(out) AT=5u",
                "unit": "V",
                "limits": {"min": 0.75, "max": 1.05},
            }
        ],
        "options": {"timeout_s": 30, "keep_artifacts": False},
    }
    path = os.path.join(_DIR, "request-monte-carlo.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(request, handle, indent=2)
        handle.write("\n")
    print(f"wrote {path}")


def main() -> None:
    write_testbench()
    write_corner_lib()
    write_request()
    write_monte_carlo_testbench()
    write_monte_carlo_request()


if __name__ == "__main__":
    main()
