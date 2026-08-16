#!/usr/bin/env python3
"""Generate the `klt size` worked example fixtures: a tiny synthetic
device library (an nmos/pmos subcircuit pair wrapping a bare SPICE
``level=1`` MOSFET model) and the request JSON that sizes an ``nmos_demo``
instance from a gm/Id target (see `docs/cli/size.md`).

These fixtures are deliberately **not** a real PDK deck -- CLAUDE.md's "open
PDKs only" rule is about design-rule/model data this repo *vendors*; a
worked example only needs to exercise the contract shape (subcircuit-call
convention, diode-connected bias sweep, the sky130-style ``m<subcircuit>``
internal op-point path) and a device whose gm/Id curve is analytically
checkable, which a level=1 square-law model gives for free (see
`tests/test_size.py`'s `test_reproduces_hand_derived_single_stage_reference`,
which independently re-derives the expected width from this model's own
textbook equations and asserts `klt size` reproduces it). This mirrors
`examples/sim/generate.py`'s reasoning for its own synthetic RC-divider
testbench.

A level=1 model does not expose ``Vth`` via ngspice's internal op-point
vectors (verified while building this module) -- `klt size` falls back to
``Vdsat`` (which equals `Vov` for a square-law model in saturation) for its
inversion-level classification in that case; see `docs/cli/size.md`'s
"Inversion-level classification" section.

Run from the repo root:

    uv run python3 examples/size/generate.py
"""

import json
import os

_DIR = os.path.dirname(os.path.abspath(__file__))


def write_models_lib() -> None:
    """A two-corner (``tt``/``ss``) synthetic device library: an
    ``nmos_demo``/``pmos_demo`` subcircuit pair, each wrapping a single
    bare SPICE ``level=1`` MOSFET -- the same "subcircuit wraps a plain
    device, instance named `m<subcircuit-name>`" shape every real
    sky130_fd_pr device uses (verified against the installed sky130A PDK
    while building `size.py`), just with textbook-checkable parameters
    instead of a vendored compact model.
    """
    path = os.path.join(_DIR, "models.lib")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            "* klt size worked example -- tiny synthetic device library\n"
            "* (not a real PDK -- see this file's generate.py docstring)\n"
            ".lib tt\n"
            ".subckt nmos_demo d g s b\n"
            ".param l=0.5 w=1 nf=1 mult=1\n"
            "mnmos_demo d g s b nmos_demo__model l={l} w={w}\n"
            ".model nmos_demo__model nmos level=1 vto=0.5 kp=120u "
            "lambda=0.02 gamma=0.4 phi=0.7\n"
            ".ends nmos_demo\n"
            "\n"
            ".subckt pmos_demo d g s b\n"
            ".param l=0.5 w=1 nf=1 mult=1\n"
            "mpmos_demo d g s b pmos_demo__model l={l} w={w}\n"
            ".model pmos_demo__model pmos level=1 vto=-0.5 kp=40u "
            "lambda=0.02 gamma=0.4 phi=0.7\n"
            ".ends pmos_demo\n"
            ".endl tt\n"
            "\n"
            ".lib ss\n"
            ".subckt nmos_demo d g s b\n"
            ".param l=0.5 w=1 nf=1 mult=1\n"
            "mnmos_demo d g s b nmos_demo__model l={l} w={w}\n"
            ".model nmos_demo__model nmos level=1 vto=0.55 kp=102u "
            "lambda=0.02 gamma=0.4 phi=0.7\n"
            ".ends nmos_demo\n"
            "\n"
            ".subckt pmos_demo d g s b\n"
            ".param l=0.5 w=1 nf=1 mult=1\n"
            "mpmos_demo d g s b pmos_demo__model l={l} w={w}\n"
            ".model pmos_demo__model pmos level=1 vto=-0.55 kp=34u "
            "lambda=0.02 gamma=0.4 phi=0.7\n"
            ".ends pmos_demo\n"
            ".endl ss\n"
        )
    print(f"wrote {path}")


def write_request() -> None:
    request = {
        "device": {
            "kind": "nmos",
            "model": "nmos_demo",
            "l_um": 0.5,
            "w_min_um": 0.5,
            "w_max_um": 20,
        },
        "models": {"lib": "models.lib"},
        "corner": {"process": "tt", "vdd_v": 1.8, "temperature_c": 27},
        "target": {"id_a": 2e-05, "gm_id": 8.0},
        "tolerance": {"gm_id_rel": 0.02},
        "options": {"sweep_points": 20},
    }
    path = os.path.join(_DIR, "request.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(request, handle, indent=2)
        handle.write("\n")
    print(f"wrote {path}")


def write_cascode_request() -> None:
    """The fixed-``Vds`` worked example (issue #1015): sizes the *same*
    ``nmos_demo`` device and current budget `write_request` above uses in
    diode-connected mode, but as a cascode leg -- held at a fixed
    ``Vds=0.3V`` (a typical cascode headroom, far below what a
    diode-connected tie would settle at for this device/current) via
    ``target.vds_v`` rather than the diode-connected ``Vds=Vgs`` tie.

    ``target.gm_id=10.0`` (vs. the diode-connected example's ``8.0``) is
    deliberately a different value, so the two committed fixtures are not
    trivially interchangeable -- `tests/test_size.py`'s
    `test_examples_size_cascode_worked_example_passes` runs this live and
    asserts both that it passes and that its confirmed ``Vds`` matches the
    requested ``0.3V`` exactly (enforced by an ideal voltage source, not
    swept -- see `docs/cli/size.md`'s "Fixed-Vds bias mode" section).
    """
    request = {
        "device": {
            "kind": "nmos",
            "model": "nmos_demo",
            "l_um": 0.5,
            "w_min_um": 0.5,
            "w_max_um": 20,
        },
        "models": {"lib": "models.lib"},
        "corner": {"process": "tt", "vdd_v": 1.8, "temperature_c": 27},
        "target": {"id_a": 2e-05, "gm_id": 10.0, "vds_v": 0.3},
        "tolerance": {"gm_id_rel": 0.02},
        "options": {"sweep_points": 20},
    }
    path = os.path.join(_DIR, "cascode_request.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(request, handle, indent=2)
        handle.write("\n")
    print(f"wrote {path}")


def write_topology_request() -> None:
    """The coupled ``diff_pair_mirror_tail`` worked example (issue #768,
    Phase 1a of epic #705): sizes an NMOS input pair, a PMOS mirror load,
    and an NMOS tail current source as one joint problem, on the same
    synthetic ``nmos_demo``/``pmos_demo`` library `write_models_lib` writes
    above -- reusing it rather than a second synthetic library, since the
    coupled topology needs exactly the NMOS/PMOS pair that library already
    provides.

    ``bias.vcm_v`` (1.3V, well above the ``Vdd/2`` default) is set
    explicitly here: at this synthetic model's textbook square-law
    parameters and these gm/Id targets, the default mid-supply common-mode
    bias leaves too little drain-source headroom for the tail device to
    stay in saturation once the topology is actually assembled -- exactly
    the kind of coupling a per-device diode-connected solve cannot see, and
    the reason this command solves against the assembled circuit instead
    (see `docs/cli/size.md`'s "Coupled multi-device topology sizing").
    `tests/test_size.py`'s
    `test_examples_size_topology_worked_example_passes` runs this live and
    asserts the joint solve converges with every device reporting `status:
    "pass"`.
    """
    request = {
        "topology": "diff_pair_mirror_tail",
        "devices": {
            "input_pair": {
                "kind": "nmos",
                "model": "nmos_demo",
                "l_um": 0.5,
                "w_min_um": 0.5,
                "w_max_um": 60,
            },
            "mirror": {
                "kind": "pmos",
                "model": "pmos_demo",
                "l_um": 0.5,
                "w_min_um": 0.5,
                "w_max_um": 60,
            },
            "tail": {
                "kind": "nmos",
                "model": "nmos_demo",
                "l_um": 0.5,
                "w_min_um": 0.5,
                "w_max_um": 60,
            },
        },
        "models": {"lib": "models.lib"},
        "corner": {"process": "tt", "vdd_v": 1.8, "temperature_c": 27},
        "budget": {"tail_current_a": 2e-05, "tail_mirror_ratio": 1},
        "target": {
            "input_pair": {"gm_id": 20.0},
            "mirror": {"gm_id": 12.0},
            "tail": {"gm_id": 20.0},
        },
        "bias": {"vcm_v": 1.3},
        "tolerance": {"gm_id_rel": 0.05},
        "options": {"sweep_points": 25, "max_joint_iterations": 6},
    }
    path = os.path.join(_DIR, "topology_request.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(request, handle, indent=2)
        handle.write("\n")
    print(f"wrote {path}")


if __name__ == "__main__":
    write_models_lib()
    write_request()
    write_cascode_request()
    write_topology_request()
