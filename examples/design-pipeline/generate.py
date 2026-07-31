#!/usr/bin/env python3
"""Regenerate the runnable `klt sim` inputs for the design-pipeline worked
example (Epic #105 Phase 3): the sky130 5T-OTA netlist body and the two
corner-sweep request JSONs (AC small-signal, and settling-transient for
supply current / DC operating point).

This mirrors `examples/sim/generate.py`: it writes the *inputs* a stage
consumes, not the simulator output. The per-stage pipeline artifacts
(`01-proposal.json` .. `05-sizing.json`) are hand-authored stage records,
not generated here -- they are the S1..S5 agent outputs, checked in as-is.

The committed `sim-ac.result.json` / `sim-op.result.json` are captured
outputs of running `klt sim` against these inputs on sky130A. Their
`runtime_s` (wall-clock) and `environment.engine_version` (installed
ngspice) fields are inherently machine-dependent, so re-running will not
reproduce them byte-for-byte -- the pass/fail verdicts and measured values
are what the writeup relies on. Requires a resolvable sky130A PDK
(`klt pdk find`) and ngspice on PATH.

Run from the repo root:

    uv run python3 examples/design-pipeline/generate.py
"""

import json
import os

_DIR = os.path.dirname(os.path.abspath(__file__))

# sky130 core-device FET convention: W/L in microns.
_NETLIST = """\
* 5T OTA (NMOS input pair, PMOS current-mirror load, NMOS current-mirror tail bias)
* sky130 schematic-level netlist BODY -- no .control/.end (klt sim wraps this).
* Device W/L are in microns (sky130_fd_pr subckt convention).
*
* Open-loop gain is characterized with a DC feedback network (Lfb/Cfb) that
* holds the output at the common-mode operating point (out = vcm) at DC while
* opening the loop at AC -- the standard way to measure open-loop gain without
* the output floating to a fragile, corner-dependent balance point (which the
* first Loop-A pass showed collapses gain at skewed corners). At AC, Lfb is
* open and Cfb ties inn to the AC ground, so vid = v(inp) - vcm = the AC drive
* and v(out)/vac is the open-loop transfer.
.param vdd=1.8
.param vcm=0.9
.param ibias=20u
.param cl=1p

* --- supplies and stimulus ---
Vdd  vdd 0 DC {vdd}
Vcm  cm  0 DC {vcm}
* single-ended AC drive on inp (magnitude 1 so v(out) magnitude == open-loop gain)
Vin  inp cm DC 0 AC 1
* ideal bias reference current injected into the tail mirror diode
Iref vdd nbias DC {ibias}
* output load
CL   out 0 {cl}

* --- DC feedback: closes at DC to bias out=vcm, opens at AC ---
Lfb  out inn 1e9
Cfb  inn cm  1e3

* --- tail current mirror (NMOS) ---
XM5b nbias nbias 0 0 sky130_fd_pr__nfet_01v8 L=1   W=10 nf=1 mult=1
XM5  tail  nbias 0 0 sky130_fd_pr__nfet_01v8 L=1   W=10 nf=1 mult=1

* --- input differential pair (NMOS) ---
XM1  n1  inp tail 0 sky130_fd_pr__nfet_01v8 L=0.5 W=8  nf=1 mult=1
XM2  out inn tail 0 sky130_fd_pr__nfet_01v8 L=0.5 W=8  nf=1 mult=1

* --- PMOS current-mirror load ---
XM3  n1  n1  vdd vdd sky130_fd_pr__pfet_01v8 L=1 W=6 nf=1 mult=1
XM4  out n1  vdd vdd sky130_fd_pr__pfet_01v8 L=1 W=6 nf=1 mult=1
"""

# The full 5x2x2 PVT matrix both requests sweep (design doc's declared corners).
_CORNERS = {
    "process": ["tt", "ss", "ff", "sf", "fs"],
    "supply_v": {"vdd": [1.62, 1.98]},
    "temperature_c": [-40, 125],
}

_MODELS = {"pdk": "sky130A", "lib": "libs.tech/ngspice/sky130.lib.spice"}

_AC_REQUEST = {
    "netlist": "ota_5t.spice",
    "engine": "ngspice",
    "models": _MODELS,
    "corners": _CORNERS,
    "analysis": {"kind": "ac", "args": "dec 20 1 1g"},
    "measurements": [
        {
            "name": "gain_db",
            "spice": ".meas ac gain_db find vdb(out) when frequency=10",
            "unit": "dB",
            "limits": {"min": 30},
        },
        {
            "name": "ugf",
            "spice": ".meas ac ugf when vdb(out)=0 cross=1",
            "unit": "Hz",
            "limits": {"min": 5e6},
        },
        {
            "name": "ph_ugf_rad",
            "spice": ".meas ac ph_ugf_rad find vp(out) when vdb(out)=0 cross=1",
            "unit": "rad",
        },
        {
            "name": "pm_deg",
            "spice": ".meas ac pm_deg param='180 + ph_ugf_rad * 57.29578'",
            "unit": "deg",
            "limits": {"min": 60},
        },
    ],
    "options": {"timeout_s": 60, "keep_artifacts": False},
}

_OP_REQUEST = {
    "netlist": "ota_5t.spice",
    "engine": "ngspice",
    "models": _MODELS,
    "corners": _CORNERS,
    "analysis": {"kind": "tran", "args": "10n 5u"},
    "measurements": [
        {
            "name": "idd",
            "spice": ".meas tran idd find i(vdd) at=5u",
            "unit": "A",
        },
        {
            "name": "isup_ua",
            "spice": ".meas tran isup_ua param='-idd*1e6'",
            "unit": "uA",
            "limits": {"max": 60},
        },
        {
            "name": "vout_dc",
            "spice": ".meas tran vout_dc find v(out) at=5u",
            "unit": "V",
            "limits": {"min": 0.3, "max": 1.5},
        },
    ],
    "options": {"timeout_s": 60, "keep_artifacts": False},
}


def _write_json(name: str, obj: dict) -> None:
    path = os.path.join(_DIR, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, indent=2)
        handle.write("\n")
    print(f"wrote {path}")


def main() -> None:
    path = os.path.join(_DIR, "ota_5t.spice")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(_NETLIST)
    print(f"wrote {path}")
    _write_json("sim-ac.request.json", _AC_REQUEST)
    _write_json("sim-op.request.json", _OP_REQUEST)


if __name__ == "__main__":
    main()
