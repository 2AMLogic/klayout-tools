#!/usr/bin/env python3
"""Generate the `klt size` worked example fixtures: a tiny synthetic
device library (an nmos/pmos subcircuit pair wrapping a bare SPICE
``level=1`` MOSFET model), the request JSON that sizes an ``nmos_demo``
instance from a gm/Id target, and the request JSON that sizes a coupled
diff-pair+mirror+tail topology against the same library (see
`docs/cli/size.md`).

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


def write_topology_request() -> None:
    """The coupled diff-pair+mirror+tail worked example (issue #768, Phase 1
    of the analog-sizing epic #705) -- sizes the same ``nmos_demo``/
    ``pmos_demo`` synthetic library's devices as a 5T-OTA-shaped topology
    (NMOS input pair, PMOS current-mirror load, NMOS tail bias replica)
    rather than one device in isolation. Targets mirror the single-device
    worked example above (``gm/Id=8`` at the tail's full 20uA budget, so the
    pair/tail see 8.0 at their own ``Id=10uA``/``20uA``); the mirror's own
    target (``6.0``) is a different value on purpose, to exercise the
    request's three independent per-role targets rather than one value
    copy-pasted three times.
    """
    request = {
        "topology": {
            "kind": "diff_pair_mirror_tail",
            "vcm_v": 0.9,
            "pair": {
                "kind": "nmos",
                "model": "nmos_demo",
                "l_um": 0.5,
                "w_min_um": 0.5,
                "w_max_um": 20,
            },
            "mirror": {
                "model": "pmos_demo",
                "l_um": 0.5,
                "w_min_um": 0.5,
                "w_max_um": 40,
                "ratio": 1.0,
            },
            "tail": {
                "model": "nmos_demo",
                "l_um": 0.5,
                "w_min_um": 0.5,
                "w_max_um": 20,
            },
        },
        "models": {"lib": "models.lib"},
        "corner": {"process": "tt", "vdd_v": 1.8, "temperature_c": 27},
        "target": {
            "id_tail_a": 2e-05,
            "pair_gm_id": 8.0,
            "mirror_gm_id": 6.0,
            "tail_gm_id": 8.0,
        },
        "tolerance": {"gm_id_rel": 0.02},
        "options": {"sweep_points": 20},
    }
    path = os.path.join(_DIR, "topology-request.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(request, handle, indent=2)
        handle.write("\n")
    print(f"wrote {path}")


if __name__ == "__main__":
    write_models_lib()
    write_request()
    write_topology_request()
