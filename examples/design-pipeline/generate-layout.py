#!/usr/bin/env python3
"""Drive the design-pipeline worked example's back end (S7 -> S10-post) end to
end: `klt gen` x3 -> `klt gen-compose` -> `klt drc` -> `klt extract` ->
`klt lvs` -> `klt sim`.

This is the companion to `generate.py` (which writes the *schematic-level*
S6/S10 inputs). Unlike `generate.py`, this script also *runs* the `klt` verbs,
because every S7-S10 artifact here is a tool output rather than a
hand-authored stage record -- there is nothing to check in until the chain has
actually run.

Devices come from `05-sizing.json`, not from `docs/cli/gen-compose.md`'s
illustrative worked example:

    M1/M2  input pair       W/L = 8/0.5 um  -> `diff_pair` (mirror=false)
    M3/M4  PMOS mirror load W/L = 6/1   um  -> `diff_pair` (mirror=true)
    M5     NMOS tail        W/L = 10/1  um  -> `mos_array` 1x2, device U1
    M5b    tail bias replica W/L = 10/1 um  -> `mos_array` 1x2, device U0

See the "S7-S10 results" section of README.md for what this chain does and
does not prove (in particular: every generated device draws NMOS-flavored
geometry, so the PMOS load extracts as `nfet` -- friction filed separately).

Run from the repo root (requires a resolvable sky130A PDK and ngspice on
PATH):

    uv run python3 examples/design-pipeline/generate-layout.py
"""

import json
import os
import subprocess
import sys

_DIR = os.path.dirname(os.path.abspath(__file__))

# --- S7 inputs: `klt gen` blocks, sized from 05-sizing.json ------------------

_BLOCKS = [
    {
        "slug": "tail",
        "generator": "mos_array",
        "cell_name": "ota_5t_tail",
        # M5 (tail) + M5b (bias replica): one matched 1x2 array, row-major so
        # U0 is the western (replica) device and U1 the eastern (tail) device
        # whose drain faces the differential pair placed to its east.
        "params": {
            "rows": 1,
            "cols": 2,
            "dummy": 0,
            "topology": "array",
            "w_um": 10,
            "l_um": 1,
        },
    },
    {
        "slug": "diffpair",
        "generator": "diff_pair",
        "cell_name": "ota_5t_inpair",
        # M1/M2 input pair. `splits: 1` keeps each device a single instance so
        # its Q1/Q2 port pair is unambiguous; `add_guard_ring: false` is
        # required for an external route to reach a port (#199).
        "params": {
            "mirror": False,
            "splits": 1,
            "add_guard_ring": False,
            "w_um": 8,
            "l_um": 0.5,
        },
    },
    {
        "slug": "mirror",
        "generator": "diff_pair",
        "cell_name": "ota_5t_mirror",
        # M3/M4 mirror load. `mirror: true` is a port-naming convention only
        # (M1_/M2_ instead of Q1_/Q2_) -- the geometry is identical.
        "params": {
            "mirror": True,
            "splits": 1,
            "add_guard_ring": False,
            "w_um": 6,
            "l_um": 1,
        },
    },
]

_TOP_CELL = "ota_5t_top"
_TOP_GDS = "ota_5t_top.gds"

# --- S7 input: the `klt gen-compose` request --------------------------------

# TAIL_A/TAIL_B decompose the three-way tail node (M5 drain + both input-pair
# sources) into two 2-pin nets sharing the tail.U1_D endpoint, since >2-pin
# bundle routing is out of scope for `klt gen-compose` today. N1/VOUT tie each
# input device's drain to the matching mirror device's *source*-side port --
# the closest topologically-meaningful connection the phase-2 router can make
# between opposite-facing ports (#199).
_COMPOSE_REQUEST = {
    "pdk": {"variant": "sky130A"},
    "blocks": [
        {"id": "tail", "generator_report": "gen-tail.json"},
        {"id": "diffpair", "generator_report": "gen-diffpair.json"},
        {"id": "mirror", "generator_report": "gen-mirror.json"},
    ],
    "placement": {
        "strategy": "row",
        "order": ["tail", "diffpair", "mirror"],
        "spacing_um": 1.0,
    },
    "connectivity": [
        {
            "net": "TAIL_A",
            "pins": [
                {"block": "tail", "port": "U1_D"},
                {"block": "diffpair", "port": "Q1_1_S"},
            ],
        },
        {
            "net": "TAIL_B",
            "pins": [
                {"block": "tail", "port": "U1_D"},
                {"block": "diffpair", "port": "Q2_1_S"},
            ],
        },
        {
            "net": "N1",
            "pins": [
                {"block": "diffpair", "port": "Q1_1_D"},
                {"block": "mirror", "port": "M1_1_S"},
            ],
        },
        {
            "net": "VOUT",
            "pins": [
                {"block": "diffpair", "port": "Q2_1_D"},
                {"block": "mirror", "port": "M2_1_S"},
            ],
        },
    ],
    "routing": {"layer_role": "metal", "width_um": 0.17},
    "options": {"cell_name": _TOP_CELL, "output": _TOP_GDS},
}

# --- S8 input: the LVS reference netlist ------------------------------------

# Hand-written schematic-equivalent of what the composed layout *should*
# contain: the six sized devices of 05-sizing.json, wired through the three
# routed nets plus the deck's global substrate tie. Gate nets and the
# unrouted drain/source terminals stay isolated (they are per-generator
# `ports[]` never passed through `connectivity[]`), exactly as extraction
# reports them. Every device is `nfet` because every `klt gen` MOS generator
# draws NMOS-flavored geometry -- see README.md "Friction".
_REFERENCE_NETLIST = """\
* LVS reference for the composed 5T OTA layout (S8).
* Device W/L are 05-sizing.json's sized values; SPICE M-card order is D G S B.
.subckt ota_5t_top N1 TAIL VOUT vsubs
M3  m3d  m3g  N1   vsubs nfet L=1U   W=6U
M1  N1   m1g  TAIL vsubs nfet L=0.5U W=8U
M5b m5bd m5bg m5bs vsubs nfet L=1U   W=10U
M5  TAIL m5g  m5s  vsubs nfet L=1U   W=10U
M4  m4d  m4g  VOUT vsubs nfet L=1U   W=6U
M2  VOUT m2g  TAIL vsubs nfet L=0.5U W=8U
.ends
"""

_LVS_REQUEST = {
    "schema": "klt.lvs.request/1",
    "layout": {"file": _TOP_GDS, "deck": "sky130", "top": _TOP_CELL},
    "reference": {"netlist": "ota_5t_reference.spice", "top": _TOP_CELL},
}

# --- S10 (post-extraction) inputs: testbench + corner request ---------------

# The testbench `.include`s the extracted netlist UNMODIFIED and instantiates
# its `.subckt` through the pins extraction promoted from `klt gen-compose`'s
# own net labels (N1 / TAIL_A|TAIL_B / VOUT / vsubs -- the .SUBCKT's pin
# *order*, not the pin text, positionally binds the Xota terminals).
#
# Each signal pin is driven from a supply-referenced resistive divider, so the
# measured pin ratios are supply-independent and any short between two pins
# (the failure mode a routing bug would produce) collapses the divider and
# fails the corresponding *_ratio limit. `.options rshunt=1e12` is the
# documented convergence aid for the five genuinely floating gate nets (#205).
_POST_TESTBENCH = """\
* Post-extraction testbench for the composed 5T OTA layout (S9 -> S10).
* Body only -- no .control/.end (klt sim wraps this).
*
* Includes `klt extract`'s output verbatim and biases the composed circuit
* through the pins it promoted from gen-compose's net labels. `nfet` is a
* generic level-1 model: `klt extract` emits the deck's device-class name, not
* a PDK model/subckt name, so the PDK model library cannot be bound here --
* see README.md "Friction".
.include "ota_5t_extracted.spice"
.model nfet nmos level=1
.options rshunt=1e12

Vdd    vdd   0 DC 1.8
Vvsubs vsubs 0 DC 0

* supply-referenced bias dividers: N1 = 2/3 vdd, VOUT = 1/2 vdd, TAIL = 1/4 vdd
Rn1a vdd n1_node   1MEG
Rn1b n1_node 0     2MEG
Rvoa vdd vout_node 1MEG
Rvob vout_node 0   1MEG
Rtla vdd tail_node 3MEG
Rtlb tail_node 0   1MEG

Xota n1_node tail_node vout_node vsubs ota_5t_top
"""

# No `corners.process` axis: the extracted netlist carries the deck's generic
# `nfet` device class, so there is no PDK `.lib` section to bind and a process
# sweep would be decorative. Supply and temperature are the pipeline's own
# declared axes and do sweep for real.
_POST_SIM_REQUEST = {
    "netlist": "ota_5t_post.spice",
    "engine": "ngspice",
    "corners": {
        "supply_v": {"vdd": [1.62, 1.98]},
        "temperature_c": [-40, 27, 125],
    },
    "analysis": {"kind": "tran", "args": "1n 1n"},
    "measurements": [
        {
            "name": "vdd_meas",
            "spice": ".meas tran vdd_meas find v(vdd) at=1n",
            "unit": "V",
        },
        {
            "name": "n1_v",
            "spice": ".meas tran n1_v find v(n1_node) at=1n",
            "unit": "V",
        },
        {
            "name": "vout_v",
            "spice": ".meas tran vout_v find v(vout_node) at=1n",
            "unit": "V",
        },
        {
            "name": "tail_v",
            "spice": ".meas tran tail_v find v(tail_node) at=1n",
            "unit": "V",
        },
        {
            "name": "n1_ratio",
            "spice": ".meas tran n1_ratio param='n1_v/vdd_meas'",
            "limits": {"min": 0.66, "max": 0.674},
        },
        {
            "name": "vout_ratio",
            "spice": ".meas tran vout_ratio param='vout_v/vdd_meas'",
            "limits": {"min": 0.495, "max": 0.505},
        },
        {
            "name": "tail_ratio",
            "spice": ".meas tran tail_ratio param='tail_v/vdd_meas'",
            "limits": {"min": 0.245, "max": 0.255},
        },
    ],
    "options": {"timeout_s": 60, "keep_artifacts": False},
}


def _write_text(name: str, text: str) -> None:
    path = os.path.join(_DIR, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    print(f"wrote {path}")


def _write_json(name: str, obj: dict) -> None:
    _write_text(name, json.dumps(obj, indent=2) + "\n")


def _run(argv: list[str], stdout_name: str, ok_exits: tuple[int, ...] = (0,)) -> dict:
    """Run a `klt` verb in the example directory, capturing its JSON response."""
    print("$ " + " ".join(argv))
    proc = subprocess.run(argv, cwd=_DIR, capture_output=True, text=True)
    if proc.returncode not in ok_exits:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"{argv[1]} exited {proc.returncode}")
    _write_text(stdout_name, proc.stdout)
    return json.loads(proc.stdout)


def main() -> None:
    klt = [sys.executable, "-m", "klayout_tools.cli"]

    # S7a: generate the three primitive blocks.
    for block in _BLOCKS:
        _run(
            klt
            + [
                "gen",
                block["generator"],
                "--params",
                json.dumps(block["params"]),
                "--pdk",
                "sky130A",
                "--cell-name",
                block["cell_name"],
                "-o",
                f"gen-{block['slug']}.gds",
                "--format",
                "json",
            ],
            f"gen-{block['slug']}.json",
        )

    # S7b: compose them into one placed-and-routed cell.
    _write_json("07-layout.request.json", _COMPOSE_REQUEST)
    compose = _run(
        klt + ["gen-compose", "07-layout.request.json", "--format", "json"],
        "07-layout.result.json",
    )
    print(
        f"  composed {compose['cell_name']}: "
        f"{len(compose['nets'])} nets, "
        f"{len(compose['unrouted_nets'])} unrouted"
    )

    # S8a: DRC half of Loop B.
    drc = _run(
        klt + ["drc", _TOP_GDS, "--deck", "sky130", "--format", "json"],
        "08-drc.result.json",
        ok_exits=(0, 3),
    )
    print(f"  drc: {drc['status']} ({drc['violation_count']} violations)")

    # S9: extraction.
    extract = _run(
        klt
        + [
            "extract",
            _TOP_GDS,
            "--deck",
            "sky130",
            "--top",
            _TOP_CELL,
            "-o",
            "ota_5t_extracted.spice",
            "--format",
            "json",
        ],
        "09-extract.result.json",
    )
    print(
        f"  extract: {extract['device_count']} devices, "
        f"{extract['pin_count']} pins, {extract['net_count']} nets"
    )

    # S8b: LVS half of Loop B.
    _write_text("ota_5t_reference.spice", _REFERENCE_NETLIST)
    _write_json("08-lvs.request.json", _LVS_REQUEST)
    lvs = _run(
        klt + ["lvs", "08-lvs.request.json", "--format", "json"],
        "08-lvs.result.json",
        ok_exits=(0, 3),
    )
    print(f"  lvs: {lvs['status']} (pins {lvs['counts']['pins']['matched']})")

    # S10 (post-extraction): corner sweep against the extracted netlist.
    _write_text("ota_5t_post.spice", _POST_TESTBENCH)
    _write_json("sim-post.request.json", _POST_SIM_REQUEST)
    sim = _run(
        klt + ["sim", "sim-post.request.json", "--format", "json"],
        "sim-post.result.json",
        ok_exits=(0, 2),
    )
    print(f"  sim: {sim['status']} ({sim['passed']}/{sim['corner_count']} corners)")


if __name__ == "__main__":
    main()
