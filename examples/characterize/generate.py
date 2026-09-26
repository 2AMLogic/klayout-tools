#!/usr/bin/env python3
"""Generate the `klt characterize` worked example fixtures: a tiny synthetic
CMOS inverter/NAND2 cell netlist wrapping bare SPICE ``level=1`` MOSFET
models, a two-corner model library, and the request JSON that characterizes
the NAND2 into an NLDM Liberty model (see `docs/cli/characterize.md`).

These fixtures are deliberately **not** a real PDK deck -- CLAUDE.md's "open
PDKs only" rule is about design-rule/model data this repo *vendors*; a worked
example only needs to exercise the contract shape (a ``.subckt`` with named
power terminals, a Liberty ``function`` the arcs and their side-input states
are derived from, an input-slew x output-load grid, `.lib` process-corner
selection) which a level=1 square-law inverter gives without any external PDK
dependency. Same reasoning as `examples/size/generate.py`'s own synthetic
device library and `examples/sim/generate.py`'s RC divider.

The example characterizes ``nand2_demo`` rather than ``inv_demo`` on
purpose: a two-input cell is what makes the interesting half of this verb
visible -- two arcs, each measured with the *other* input held at its
non-controlling value, derived from ``function : "!(A*B)"`` rather than
declared by hand.

Like `examples/sim/`, this example has **no committed golden JSON output**:
the response carries `runtime_s` (wall-clock) and `engine_version` (whatever
ngspice is installed), so a byte-exact fixture would be flaky by
construction. `docs/cli/characterize.md` shows an illustrative (trimmed)
response instead, and `tests/test_characterize.py` runs this example live
(skipped when ngspice is not installed), asserting the documented *shape*.

Run from the repo root:

    uv run python3 examples/characterize/generate.py
"""

import json
import os

_DIR = os.path.dirname(os.path.abspath(__file__))


def write_models_lib() -> None:
    """A two-corner (``tt``/``ss``) synthetic model library.

    Only the MOSFET models live here -- the cell netlist below is corner
    independent, exactly as a real standard-cell ``spice/`` view is (the
    corner lives in the model library `klt sim` selects a section from).
    """
    path = os.path.join(_DIR, "models.lib")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            "* klt characterize worked example -- tiny synthetic model library\n"
            "* (not a real PDK -- see this directory's generate.py docstring)\n"
            ".lib tt\n"
            ".model nfet_demo nmos level=1 vto=0.4 kp=200u lambda=0.05 "
            "cgso=1.5n cgdo=1.5n cj=1m cjsw=200p\n"
            ".model pfet_demo pmos level=1 vto=-0.4 kp=80u lambda=0.05 "
            "cgso=1.5n cgdo=1.5n cj=1m cjsw=200p\n"
            ".endl tt\n"
            "\n"
            ".lib ss\n"
            ".model nfet_demo nmos level=1 vto=0.48 kp=160u lambda=0.05 "
            "cgso=1.5n cgdo=1.5n cj=1m cjsw=200p\n"
            ".model pfet_demo pmos level=1 vto=-0.48 kp=64u lambda=0.05 "
            "cgso=1.5n cgdo=1.5n cj=1m cjsw=200p\n"
            ".endl ss\n"
        )


def write_cells_spice() -> None:
    """The cell netlist: an inverter and a 2-input NAND, each a ``.subckt``
    with explicit ``VDD``/``VSS`` terminals.

    Terminal *order* is what the generated testbench binds against (a SPICE
    ``X`` line is positional), and it deliberately does not match the order
    the request lists pins in -- that is the point of reading the
    ``.subckt`` line rather than trusting the request's ordering.
    """
    path = os.path.join(_DIR, "cells.spice")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            "* klt characterize worked example -- synthetic standard cells\n"
            "* (not a real PDK -- see this directory's generate.py docstring)\n"
            "\n"
            ".subckt inv_demo Y A VDD VSS\n"
            "MN Y A VSS VSS nfet_demo w=1u l=0.15u\n"
            "MP Y A VDD VDD pfet_demo w=2u l=0.15u\n"
            ".ends inv_demo\n"
            "\n"
            ".subckt nand2_demo Y A B VDD VSS\n"
            "MN0 Y A net1 VSS nfet_demo w=2u l=0.15u\n"
            "MN1 net1 B VSS VSS nfet_demo w=2u l=0.15u\n"
            "MP0 Y A VDD VDD pfet_demo w=2u l=0.15u\n"
            "MP1 Y B VDD VDD pfet_demo w=2u l=0.15u\n"
            ".ends nand2_demo\n"
        )


def write_request() -> None:
    """The `klt characterize` request: one cell, one corner, a 4x4 grid.

    A 4x4 grid rather than a vendor-sized 7x7 keeps the example's single
    ngspice run fast (it is one transient over ``4 x 4 x 2 arcs = 32``
    instances); nothing about the shape changes with the grid's size.
    """
    request = {
        "cell": {
            "name": "nand2_demo",
            "netlist": "cells.spice",
            "pins": [
                {"name": "A", "direction": "input", "capacitance_pf": 0.002},
                {"name": "B", "direction": "input", "capacitance_pf": 0.002},
                {"name": "Y", "direction": "output", "function": "!(A*B)"},
            ],
            "power_pins": {"vdd": "VDD", "gnd": "VSS"},
            "area": 3.6,
        },
        "corner": {
            "name": "tt_1p80V_25C",
            "process": "tt",
            "supply_v": 1.8,
            "temperature_c": 25,
        },
        "grid": {
            "input_transition_ns": [0.02, 0.08, 0.24, 0.72],
            "output_load_pf": [0.005, 0.03, 0.1, 0.3],
        },
        "models": {"lib": "models.lib"},
        "library": {"name": "characterize_demo_tt_1p80V_25C"},
        "options": {"timeout_s": 600},
    }
    path = os.path.join(_DIR, "request.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(request, handle, indent=2)
        handle.write("\n")


def main() -> None:
    write_models_lib()
    write_cells_spice()
    write_request()
    print(f"wrote models.lib, cells.spice, request.json to {_DIR}")


if __name__ == "__main__":
    main()
