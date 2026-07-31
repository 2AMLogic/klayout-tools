"""Per-block `klt sim` signals -- real transistor-level PVT sweeps for the
gallery's 7 standard cells (issue #99, epic #90 phase 2).

This module assembles, for each of the 7 gallery blocks, a `klt sim`
(`src/klayout_tools/sim.py`) request built from **real, vendored** SPICE
netlists -- fetched and checksum-verified by
`scripts/fetch-cell-netlists.sh` into `pdks/cell-netlists/` (gitignored,
mirrors the `pdks/lambdapdk/` pattern) -- and returns a condensed `signals`
dict ready to attach to that block's `layout.json`
(`src/klayout_tools/layout_metrics.py`'s schema; see that module and
`docs/cli/layout-metrics.md` for the `signals` field's documented shape).

Real data, real transistors, one documented substitution
----------------------------------------------------------

sky130 (`inv_1`, `nand2_2`, `buf_4`, `dfxtp_2`): the vendored
`sky130_fd_sc_hd__*.spice` cell netlists instantiate exactly two sky130
primitive devices -- `sky130_fd_pr__nfet_01v8` and
`sky130_fd_pr__pfet_01v8_hvt` -- both used, unmodified, straight from
`google/skywater-pdk-libs-sky130_fd_pr`. No substitution needed.

gf180mcu (`and2_1`, `dffnq_1`, `clkinv_1`): the vendored
`gf180mcu_fd_sc_mcu9t5v0__*.cdl` cell netlists instantiate `nfet_05v0` /
`pfet_05v0` -- names that do **not** exist as raw device models in the
currently published `google/globalfoundries-pdk-libs-gf180mcu_fd_pr`
primitive repo (it defines binned `nmos_3p3`/`pmos_3p3` (3.3V) and
`nmos_6p0`/`pmos_6p0` (6V) raw models only; no distinct 5V bin). This is a
real gap in the currently published open primitive-device repo, not
something this module can source around -- verified by exhaustively
grepping the vendored `sm141064.ngspice` model deck for every "05v0"/"5p0"
spelling. Independent published research hit the same gap and adopted the
same fix: "the adapter maps released CDL `nfet_05v0` and `pfet_05v0`
instances to the open ngspice `nmos_6p0` and `pmos_6p0` subcircuits while
preserving every transistor connection, width, and length"
(jqchen0715/gf180mcu-spice-surrogate-mej,
`RELEASED_LIBRARY_REPRODUCIBILITY.md`). This module applies the identical,
narrowly-scoped substitution -- `nfet_05v0` -> `nmos_6p0`, `pfet_05v0` ->
`pmos_6p0`, topology/W/L untouched -- to the 3 gf180mcu cells only, and
tags every gf180mcu result's `signals.device_substitution` with the exact
mapping applied so it is never silently mistaken for an as-published
characterization. The vendored `.cdl` files themselves are fetched and
checksummed byte-for-byte as published; the substitution happens only in
the generated (not vendored) per-block testbench text this module writes.

Test methodology
-----------------

One sensitized digital arc per cell family, transistor-level (not a
behavioral/logic-only model):

- **comb1** (`inv_1`, `buf_4`, `clkinv_1`): single input pulses, output
  loaded with a small fixed capacitor; `tphl`/`tplh` propagation delay.
- **comb2** (`nand2_2`, `and2_1`): one input pulses, the other input is
  held static at VDD (the classic single-sensitized-arc pattern -- same
  reasoning the gf180mcu-spice-surrogate-mej adapter above uses); same
  `tphl`/`tplh` pair.
- **dff** (`dfxtp_2`, `dffnq_1`): D is driven low then high well before a
  second clock edge, so the flop's first-ever Q transition is
  unambiguous; `tcq` clock-to-Q delay from that second active clock edge.
  `dffnq_1`'s `CLKN` pin is empirically falling-edge active (verified by
  simulation while building this module, not assumed from the name).

No `.meas` limits are declared (measurements are reported, not
pass/failed against a threshold) -- this module has no independent,
license-clean source of characterized sky130/gf180mcu timing to set
limits against; `docs/cli/sim.md`'s "No `limits` -> reported, never
fails" behavior is exactly the right fit.

PVT corner matrix
------------------

Process axis uses the corner names each PDK's own vendored model deck
defines (`tt`/`ss`/`ff` for sky130's per-device corner files assembled by
this module into `models.lib`; `typical`/`ss`/`ff`, the native `.LIB`
section names inside the vendored gf180mcu `sm141064.ngspice`). Supply
sweeps +-10% around each family's nominal rail plus the nominal rail
itself; temperature sweeps -40C/27C/125C. The full 3 x 3 x 3 cross product
would spend two thirds of its runtime on half-mixed points nobody reads,
so `corners.exclude` (see `docs/cli/sim.md`) prunes it to the 5
supply/temperature combinations that mean something -- the four PVT
extremes (+-10% supply x -40C/125C) plus the nominal point (nominal
supply, 27C) -- giving 3 process x 5 = **15 corners per cell**, of which
3 (one per process corner) are nominal.

Waveform artifacts (issue #100's viewer)
-----------------------------------------

`options.waveforms`/`options.keep_artifacts` are on, so every corner
produces a real ngspice rawfile parsed into `klt sim`'s documented
waveform JSON shape (`docs/cli/sim.md`). The site's already-shipped
waveform viewer (issue #100, `site/src/components/waveform/`) consumes
`signals.default_corner_id` plus a per-corner `waveform` path, so this
module emits both: the default corner is the nominal-process nominal
point, and the 3 nominal corners' waveforms are staged into
`blocks/<slug>/output/signals/<corner_id>.json` (path recorded relative to
`output/`, the same convention `renders` uses, which is what
`site/scripts/copy-renders.mjs` stages into the built site).

Only those 3 corners are staged, and each staged file is a **trimmed
projection** of the corner's full waveform artifact: the sweep variable
plus the testbench's own driven/observed pins, with sample values rounded
to `_WAVEFORM_SIG_DIGITS` significant digits. ngspice's rawfile carries
every internal node of the flattened cell (`dfxtp_2`: 97 variables, ~4 MB
of ASCII per corner, almost all of it MOSFET body-node voltages) -- real
data, but not data any viewer plots and not data worth committing 15
copies of per block. The trimmed file keeps the exact same
`{plotname, variables[], points[]}` shape, so it is still a valid `klt
sim` waveform artifact, just a projection of one; nothing is smoothed,
resampled, or synthesized.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from dataclasses import dataclass
from typing import Any

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from klayout_tools.sim import SimError, run_sim  # noqa: E402

NETLISTS_ROOT = os.path.join(REPO_ROOT, "pdks", "cell-netlists")

#: gf180mcu device-family substitution applied to the 3 gf180mcu cells'
#: generated testbenches (never to the vendored .cdl files themselves) --
#: see this module's docstring for the full rationale.
GF180_DEVICE_SUBSTITUTION = {"nfet_05v0": "nmos_6p0", "pfet_05v0": "pmos_6p0"}

#: The nominal-PVT temperature every cell's nominal corner is measured at
#: (also `klt sim`'s own default when a request declares no temperature
#: axis -- see `_expand_corners` in `src/klayout_tools/sim.py`).
NOMINAL_TEMPERATURE_C = 27

#: Fractional supply spread around each family's nominal rail.
SUPPLY_TOLERANCE = 0.10

#: Directory, relative to a block's ``output/``, that staged per-corner
#: waveform artifacts are written to. Matches the path convention issue
#: #100's viewer + `site/scripts/copy-renders.mjs` already ship with.
WAVEFORM_SUBDIR = "signals"

#: Significant digits kept in staged waveform sample values. ngspice emits
#: full double precision; ~6 digits is well past what a 640px-wide plot (or
#: any cursor readout) can resolve, and roughly halves the committed size.
_WAVEFORM_SIG_DIGITS = 6


@dataclass(frozen=True)
class CellConfig:
    """Everything needed to build a `klt sim` request for one gallery block."""

    pdk: str  # "sky130" | "gf180mcu"
    family: str  # "comb1" | "comb2" | "dff"
    cell_file: str  # filename under pdks/cell-netlists/<pdk>/cells/
    subckt: str
    ports: list[str]  # exact formal port order of the .subckt
    vdd_nominal: float
    signal: dict[str, str]  # role -> pin name, e.g. {"in": "A", "out": "Y"}
    corners_process: list[str]
    edge: str = "rise"  # dff only: "rise" | "fall" active clock edge
    inverting: bool = True  # comb1/comb2 only: does output polarity flip?


# Rail wiring is fixed per PDK (every standard cell in that library shares
# the same power/ground pin names). Every "at VDD" pin is tied to the same
# `vdd_net`, driven by exactly one source instance named `Vdd` -- `klt sim`'s
# `corners.supply_v` sweep does `alter <key>=<value>` against a device
# literally named `<key>` (see docs/cli/sim.md's "Corner axes"; confirmed
# empirically while building this module -- `alter` targets a *device*
# instance by name, not a `.param`, despite the doc's `.param vdd=1.8`-styled
# example). Tying every VDD-level pin to one shared net/source (rather than
# one source per rail) means a single `alter vdd=...` re-biases the whole
# cell correctly every corner.
_SKY130_RAILS = {"VGND": "0", "VNB": "0", "VPB": "vdd_net", "VPWR": "vdd_net"}
_GF180_RAILS = {"VSS": "0", "VPW": "0", "VNW": "vdd_net", "VDD": "vdd_net"}

_SKY130_PROCESS_CORNERS = ["tt", "ss", "ff"]
_GF180_PROCESS_CORNERS = ["typical", "ss", "ff"]

CELL_CONFIGS: dict[str, CellConfig] = {
    "sky130_fd_sc_hd__inv_1": CellConfig(
        pdk="sky130",
        family="comb1",
        cell_file="sky130_fd_sc_hd__inv_1.spice",
        subckt="sky130_fd_sc_hd__inv_1",
        ports=["A", "VGND", "VNB", "VPB", "VPWR", "Y"],
        vdd_nominal=1.8,
        signal={"in": "A", "out": "Y"},
        corners_process=_SKY130_PROCESS_CORNERS,
    ),
    "sky130_fd_sc_hd__nand2_2": CellConfig(
        pdk="sky130",
        family="comb2",
        cell_file="sky130_fd_sc_hd__nand2_2.spice",
        subckt="sky130_fd_sc_hd__nand2_2",
        ports=["A", "B", "VGND", "VNB", "VPB", "VPWR", "Y"],
        vdd_nominal=1.8,
        signal={"in": "A", "static": "B", "out": "Y"},
        corners_process=_SKY130_PROCESS_CORNERS,
    ),
    "sky130_fd_sc_hd__buf_4": CellConfig(
        pdk="sky130",
        family="comb1",
        cell_file="sky130_fd_sc_hd__buf_4.spice",
        subckt="sky130_fd_sc_hd__buf_4",
        ports=["A", "VGND", "VNB", "VPB", "VPWR", "X"],
        vdd_nominal=1.8,
        signal={"in": "A", "out": "X"},
        corners_process=_SKY130_PROCESS_CORNERS,
        inverting=False,  # buf_4 is a non-inverting buffer, X = A
    ),
    "sky130_fd_sc_hd__dfxtp_2": CellConfig(
        pdk="sky130",
        family="dff",
        cell_file="sky130_fd_sc_hd__dfxtp_2.spice",
        subckt="sky130_fd_sc_hd__dfxtp_2",
        ports=["CLK", "D", "VGND", "VNB", "VPB", "VPWR", "Q"],
        vdd_nominal=1.8,
        signal={"clk": "CLK", "d": "D", "out": "Q"},
        corners_process=_SKY130_PROCESS_CORNERS,
        edge="rise",
    ),
    "gf180mcu_fd_sc_mcu9t5v0__and2_1": CellConfig(
        pdk="gf180mcu",
        family="comb2",
        cell_file="gf180mcu_fd_sc_mcu9t5v0__and2_1.cdl",
        subckt="gf180mcu_fd_sc_mcu9t5v0__and2_1",
        ports=["A1", "A2", "Z", "VDD", "VNW", "VPW", "VSS"],
        vdd_nominal=5.0,
        signal={"in": "A1", "static": "A2", "out": "Z"},
        corners_process=_GF180_PROCESS_CORNERS,
        inverting=False,  # and2_1 is a non-inverting AND, Z = A1 . A2
    ),
    "gf180mcu_fd_sc_mcu9t5v0__dffnq_1": CellConfig(
        pdk="gf180mcu",
        family="dff",
        cell_file="gf180mcu_fd_sc_mcu9t5v0__dffnq_1.cdl",
        subckt="gf180mcu_fd_sc_mcu9t5v0__dffnq_1",
        ports=["D", "CLKN", "Q", "VDD", "VNW", "VPW", "VSS"],
        vdd_nominal=5.0,
        signal={"clk": "CLKN", "d": "D", "out": "Q"},
        corners_process=_GF180_PROCESS_CORNERS,
        edge="fall",
    ),
    "gf180mcu_fd_sc_mcu9t5v0__clkinv_1": CellConfig(
        pdk="gf180mcu",
        family="comb1",
        cell_file="gf180mcu_fd_sc_mcu9t5v0__clkinv_1.cdl",
        subckt="gf180mcu_fd_sc_mcu9t5v0__clkinv_1",
        ports=["I", "ZN", "VDD", "VNW", "VPW", "VSS"],
        vdd_nominal=5.0,
        signal={"in": "I", "out": "ZN"},
        corners_process=_GF180_PROCESS_CORNERS,
    ),
}


def netlists_available() -> bool:
    """Whether `scripts/fetch-cell-netlists.sh` has populated its output."""
    return os.path.isdir(NETLISTS_ROOT) and any(
        f.endswith((".spice", ".cdl")) for f in _walk_files(NETLISTS_ROOT)
    )


def _walk_files(root: str):
    for dirpath, _dirs, filenames in os.walk(root):
        for name in filenames:
            yield os.path.join(dirpath, name)


def _sky130_models_lib(work_dir: str) -> str:
    """Assemble a small `tt`/`ss`/`ff` model library from the vendored
    nfet_01v8/pfet_01v8_hvt device-corner files only (the two primitives the
    4 sky130 gallery cells instantiate) -- not the upstream repo's full
    ~100 MB, all-devices `sky130.lib.spice`."""
    models_dir = os.path.join(NETLISTS_ROOT, "sky130", "models")
    lines: list[str] = []
    for corner in _SKY130_PROCESS_CORNERS:
        lines.append(f".lib {corner}")
        for dev in ("nfet_01v8", "pfet_01v8_hvt"):
            mismatch_file = os.path.join(
                models_dir, f"sky130_fd_pr__{dev}__mismatch.corner.spice"
            )
            corner_file = os.path.join(
                models_dir, f"sky130_fd_pr__{dev}__{corner}.corner.spice"
            )
            lines.append(f'.include "{mismatch_file}"')
            lines.append(f'.include "{corner_file}"')
        lines.append(f".endl {corner}")
    path = os.path.join(work_dir, "sky130_models.lib.spice")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


def _gf180_models_lib(work_dir: str) -> str:
    """Wrapper selecting the vendored gf180mcu primitive deck's own
    `typical`/`ff`/`ss` `.LIB` sections, with `design.ngspice`'s globals
    (`fnoicor`, `sw_stat_global`, etc -- every corner section's `.param`
    formulas reference these) made visible inside each one.

    `klt sim`'s generated corner deck does exactly one thing with a process
    axis: ``.lib {models_lib} {process}`` (see ``_write_corner_deck`` in
    ``src/klayout_tools/sim.py``) -- and ngspice's ``.lib <file> <section>``
    directive only evaluates the text *inside* the matching
    ``.LIB <section>``/``.ENDL <section>`` block of that file; anything
    else in the file (a plain top-level ``.include design.ngspice``, tried
    first and confirmed broken empirically while building this module) is
    never reached. `design.ngspice`'s own params are copied verbatim into
    each section here instead. Nesting a **named** `.lib '<file>'
    <section>` selection (not a raw `.include`) inside another selected
    section, on the other hand, works fine -- confirmed empirically, and
    exactly how the vendored `sm141064.ngspice` deck already delegates to
    itself internally -- so each of this wrapper's sections just delegates
    to the vendored file's same-named section for the actual devices.
    """
    models_dir = os.path.join(NETLISTS_ROOT, "gf180mcu", "models")
    design_path = os.path.join(models_dir, "design.ngspice")
    sm_path = os.path.join(models_dir, "sm141064.ngspice")
    with open(design_path, encoding="utf-8") as handle:
        design_text = handle.read()

    path = os.path.join(work_dir, "gf180_models.lib.spice")
    with open(path, "w", encoding="utf-8") as handle:
        for corner in _GF180_PROCESS_CORNERS:
            handle.write(f".lib {corner}\n")
            handle.write(design_text)
            handle.write(f"\n.lib '{sm_path}' {corner}\n")
            handle.write(f".endl {corner}\n\n")
    return path


def _cell_body_text(cfg: CellConfig) -> str:
    """The vendored cell netlist text, with the documented gf180mcu device
    substitution applied (gf180mcu only; sky130 is returned byte-for-byte)."""
    cell_path = os.path.join(NETLISTS_ROOT, cfg.pdk, "cells", cfg.cell_file)
    with open(cell_path, encoding="utf-8") as handle:
        text = handle.read()
    if cfg.pdk == "gf180mcu":
        for old, new in GF180_DEVICE_SUBSTITUTION.items():
            text = re.sub(re.escape(old), new, text)
    return text


def _build_netlist(cfg: CellConfig, work_dir: str) -> str:
    """Write the `klt sim` circuit-body netlist (device defs + sources, no
    `.control`/`.end`) for one cell."""
    vdd = cfg.vdd_nominal
    rails = _SKY130_RAILS if cfg.pdk == "sky130" else _GF180_RAILS

    lines: list[str] = [_cell_body_text(cfg).rstrip(), ""]
    if cfg.pdk == "sky130":
        # The vendored sky130_fd_pr device subckts express W/L in a
        # pre-scaled convention (e.g. `w=650000u` for an 0.65um-wide
        # transistor) that only resolves to physically sane geometry with
        # a global 1e-6 length scale applied -- confirmed empirically
        # against the real device-model bins while building this module
        # (an unscaled deck fails every corner with "could not find a
        # valid modelname", ngspice's message when W/L fall outside every
        # bin's LMIN/LMAX/WMIN/WMAX range). gf180mcu's vendored .cdl
        # already expresses W/L in plain microns (`W=0.660000U`); no scale
        # needed there.
        lines.append(".option scale=1e-6")
    lines.append(f".param vdd={vdd:.3g}")

    # Exactly one alterable VDD source, named `Vdd` (full device name
    # `vdd`) so the request's `corners.supply_v: {"vdd": [...]}` sweep's
    # generated `alter vdd=...` command hits it directly.
    lines.append("Vdd vdd_net 0 DC {vdd}")

    if cfg.family in ("comb1", "comb2"):
        in_pin = cfg.signal["in"]
        out_pin = cfg.signal["out"]
        # tr=tf: sky130 devices are small/fast (0.1n edges plausible at this
        # node size); gf180mcu's 5V devices are larger/slower, so a wider
        # edge is used -- both values validated against real ngspice runs
        # while building this module (see the module docstring).
        tr = "0.1n" if cfg.pdk == "sky130" else "0.2n"
        lines.append(f"Vin {in_pin} 0 PULSE(0 {{vdd}} 1n {tr} {tr} 5n 20n)")
        if cfg.family == "comb2":
            static_pin = cfg.signal["static"]
            lines.append(f"Vstatic {static_pin} 0 DC {{vdd}}")
        lines.append(f"Cload {out_pin} 0 5f")
    else:  # dff
        clk_pin = cfg.signal["clk"]
        d_pin = cfg.signal["d"]
        out_pin = cfg.signal["out"]
        tr = "0.1n" if cfg.pdk == "sky130" else "0.2n"
        # First active edge (D=0) settles Q to a known state; D rises well
        # before the second active edge, giving Q's first-ever, unambiguous
        # transition -- see the module docstring.
        lines.append(f"Vclk {clk_pin} 0 PULSE(0 {{vdd}} 2n {tr} {tr} 5n 20n)")
        lines.append(f"Vd {d_pin} 0 PULSE(0 {{vdd}} 12n {tr} {tr} 100n 200n)")
        lines.append(f"Cload {out_pin} 0 5f")

    x0_nets = [rails.get(pin, pin) for pin in cfg.ports]
    lines.append("X0 " + " ".join(x0_nets) + " " + cfg.subckt)

    path = os.path.join(work_dir, "circuit.spice")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


def _measurements(cfg: CellConfig) -> list[dict[str, Any]]:
    half_vdd = round(cfg.vdd_nominal * 0.5, 6)
    if cfg.family in ("comb1", "comb2"):
        in_pin = cfg.signal["in"]
        out_pin = cfg.signal["out"]
        # `tphl`/`tplh` always name the OUTPUT transition direction (the
        # standard STA convention) -- which INPUT edge produces it depends
        # on whether the cell inverts: an inverting gate's falling output
        # follows the input's *rising* edge, a non-inverting gate's
        # falling output follows the input's own *falling* edge.
        if cfg.inverting:
            tphl_trig_edge, tplh_trig_edge = "RISE=1", "FALL=1"
        else:
            tphl_trig_edge, tplh_trig_edge = "FALL=1", "RISE=1"
        tphl_spice = (
            f".meas tran tphl TRIG v({in_pin}) VAL={half_vdd} {tphl_trig_edge} "
            f"TARG v({out_pin}) VAL={half_vdd} FALL=1"
        )
        tplh_spice = (
            f".meas tran tplh TRIG v({in_pin}) VAL={half_vdd} {tplh_trig_edge} "
            f"TARG v({out_pin}) VAL={half_vdd} RISE=1"
        )
        return [
            {"name": "tphl", "spice": tphl_spice, "unit": "s"},
            {"name": "tplh", "spice": tplh_spice, "unit": "s"},
        ]
    clk_pin = cfg.signal["clk"]
    out_pin = cfg.signal["out"]
    trig_edge = "RISE=2" if cfg.edge == "rise" else "FALL=2"
    tcq_spice = (
        f".meas tran tcq TRIG v({clk_pin}) VAL={half_vdd} {trig_edge} "
        f"TARG v({out_pin}) VAL={half_vdd} RISE=1"
    )
    return [{"name": "tcq", "spice": tcq_spice, "unit": "s"}]


def _analysis(cfg: CellConfig) -> dict[str, str]:
    if cfg.family == "dff":
        return {"kind": "tran", "args": "20p 40n"}
    return {"kind": "tran", "args": "10p 15n"}


def _supply_points(cfg: CellConfig) -> list[float]:
    """The supply axis: -10%, nominal, +10% (in that order)."""
    vdd = cfg.vdd_nominal
    return [
        round(vdd * (1 - SUPPLY_TOLERANCE), 4),
        round(vdd, 4),
        round(vdd * (1 + SUPPLY_TOLERANCE), 4),
    ]


def corner_id(process: str, supply: float, temperature_c: float) -> str:
    """Reproduce `klt sim`'s ``corner_id`` for a single-rail corner point.

    Kept in lockstep with ``CornerPoint.corner_id`` in
    ``src/klayout_tools/sim.py`` (``<process>/<supply>V/<temp>C``) so this
    module can name a corner -- e.g. for ``default_corner_id`` -- before
    the sweep has run. ``tests/test_gallery_signals.py`` pins the two
    implementations together against real `klt sim` output.
    """
    temp_label = (
        int(temperature_c) if float(temperature_c).is_integer() else temperature_c
    )
    return f"{process}/{supply:.3f}V/{temp_label}C"


def nominal_corner_ids(cfg: CellConfig) -> list[str]:
    """The nominal-supply / nominal-temperature corner of each process
    corner -- the corners whose waveform artifacts get staged into the
    block (see this module's docstring)."""
    nominal_supply = round(cfg.vdd_nominal, 4)
    return [
        corner_id(process, nominal_supply, NOMINAL_TEMPERATURE_C)
        for process in cfg.corners_process
    ]


def build_request(cfg: CellConfig, work_dir: str) -> dict[str, Any]:
    """Build the full `klt sim` request dict for one cell (paths are
    absolute, so the request is independent of ``work_dir``'s location)."""
    netlist_path = _build_netlist(cfg, work_dir)
    models_lib = (
        _sky130_models_lib(work_dir)
        if cfg.pdk == "sky130"
        else _gf180_models_lib(work_dir)
    )
    low, nominal, high = _supply_points(cfg)
    temperatures = [-40, NOMINAL_TEMPERATURE_C, 125]
    # Keep the four PVT extremes and the nominal point; drop the six
    # half-mixed combinations (nominal supply at a temperature extreme,
    # extreme supply at nominal temperature) -- see the module docstring.
    exclude = [
        {"supply_v": {"vdd": nominal}, "temperature_c": -40},
        {"supply_v": {"vdd": nominal}, "temperature_c": 125},
        {"supply_v": {"vdd": low}, "temperature_c": NOMINAL_TEMPERATURE_C},
        {"supply_v": {"vdd": high}, "temperature_c": NOMINAL_TEMPERATURE_C},
    ]
    return {
        "netlist": netlist_path,
        "engine": "ngspice",
        "models": {"lib": models_lib},
        "corners": {
            "process": cfg.corners_process,
            "supply_v": {"vdd": [low, nominal, high]},
            "temperature_c": temperatures,
        },
        "exclude": exclude,
        "analysis": _analysis(cfg),
        "measurements": _measurements(cfg),
        # Waveforms are captured for every corner (the engine has no
        # per-corner switch) but only the nominal ones are staged into the
        # block -- see `_stage_waveforms`.
        "options": {"timeout_s": 30, "keep_artifacts": True, "waveforms": True},
    }


def _round_sig(value: float, digits: int = _WAVEFORM_SIG_DIGITS) -> float:
    """Round to ``digits`` significant digits (0 and non-finite pass through)."""
    if value == 0 or not math.isfinite(value):
        return value
    exponent = math.floor(math.log10(abs(value)))
    return round(value, digits - 1 - exponent)


def _trim_waveform(waveform_path: str, cfg: CellConfig) -> dict[str, Any] | None:
    """Project a full `klt sim` waveform artifact down to the testbench's own
    pins (plus the sweep variable), rounded for size.

    Returns ``None`` when the artifact is unreadable or none of the cell's
    pins appear in it -- staging a waveform is best-effort, exactly like
    every other optional artifact in the gallery pipeline.
    """
    try:
        with open(waveform_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None

    variables = data.get("variables") or []
    points = data.get("points") or []
    if not variables or not points:
        return None

    wanted = {f"v({pin.lower()})" for pin in cfg.signal.values()}
    keep = [0] + [
        i
        for i, var in enumerate(variables)
        if i != 0 and str(var.get("name", "")).lower() in wanted
    ]
    if len(keep) < 2:
        return None

    return {
        "plotname": data.get("plotname", ""),
        "variables": [
            {
                "index": new_index,
                "name": variables[source_index].get("name"),
                "type": variables[source_index].get("type"),
            }
            for new_index, source_index in enumerate(keep)
        ],
        "points": [
            [_round_sig(row[i]) for i in keep if i < len(row)]
            for row in points
            if len(row) >= len(variables)
        ],
    }


def _stage_waveforms(
    cfg: CellConfig, corners: list[dict[str, Any]], block_dir: str | None
) -> None:
    """Stage the nominal corners' waveform artifacts into the block and
    rewrite each corner's ``artifacts`` into the site-facing ``waveform``
    path issue #100's viewer consumes.

    Every corner loses its ``artifacts`` object either way: those are
    absolute paths into a throwaway work directory, meaningless (and
    machine-specific) once serialized into a committed ``layout.json``.
    """
    staged_ids = set(nominal_corner_ids(cfg))
    for corner in corners:
        artifacts = corner.pop("artifacts", None) or {}
        if block_dir is None or corner.get("corner_id") not in staged_ids:
            continue
        source = artifacts.get("waveform")
        if not source or not os.path.isfile(source):
            continue
        trimmed = _trim_waveform(source, cfg)
        if trimmed is None:
            continue
        rel_path = f"{WAVEFORM_SUBDIR}/{corner['corner_id'].replace('/', '_')}.json"
        dest = os.path.join(block_dir, "output", rel_path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as handle:
            json.dump(trimmed, handle)
            handle.write("\n")
        corner["waveform"] = rel_path


def run_signals_for_block(
    slug: str, work_dir: str, block_dir: str | None = None
) -> dict[str, Any] | None:
    """Run the full PVT sweep for one gallery block and return the
    condensed ``signals`` dict for its ``layout.json``, or ``None`` when the
    vendored netlist data isn't present (graceful skip, mirrors
    ``layout_metrics.py``'s opt-in DRC/renders handling).

    When ``block_dir`` is given, the nominal corners' waveform artifacts are
    staged under ``<block_dir>/output/signals/`` and referenced from the
    returned dict (``default_corner_id`` + per-corner ``waveform``) -- the
    shape `site/src/components/waveform/WaveformViewer.tsx` consumes.
    """
    cfg = CELL_CONFIGS.get(slug)
    if cfg is None or not netlists_available():
        return None

    request = build_request(cfg, work_dir)
    request_path = os.path.join(work_dir, "request.json")

    with open(request_path, "w", encoding="utf-8") as handle:
        json.dump(request, handle, indent=2)

    try:
        response = run_sim(request_path)
    except SimError as exc:
        return {
            "schema_version": 1,
            "status": "error",
            "error": str(exc),
        }

    corners = response["corners"]
    _stage_waveforms(cfg, corners, block_dir)

    signals: dict[str, Any] = {
        "schema_version": response["schema_version"],
        "engine": response["environment"]["engine"],
        "engine_version": response["environment"]["engine_version"],
        "status": response["status"],
        "corner_count": response["corner_count"],
        # The corner the site's waveform viewer opens on: nominal supply,
        # nominal temperature, nominal process (`tt` / `typical`).
        "default_corner_id": nominal_corner_ids(cfg)[0],
        "passed": response["passed"],
        "failed": response["failed"],
        "errored": response["errored"],
        "measurements": response["measurements"],
        "corners": corners,
    }
    if cfg.pdk == "gf180mcu":
        signals["device_substitution"] = dict(GF180_DEVICE_SUBSTITUTION)
    return signals


def write_signals_json(block_dir: str, signals: dict[str, Any]) -> str:
    """Persist ``signals`` to ``<block_dir>/output/sim/signals.json``.

    This is the same file :func:`klayout_tools.layout_metrics._attach_signals`
    reads (verbatim) into a `layout.json`'s ``signals`` field -- writing it
    here means the artifact exists independently of this script's own
    in-memory attachment, so a future ``klt layout-metrics`` regeneration of
    these blocks picks it up with no changes.
    """
    import json

    sim_dir = os.path.join(block_dir, "output", "sim")
    os.makedirs(sim_dir, exist_ok=True)
    path = os.path.join(sim_dir, "signals.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(signals, handle, indent=2)
        handle.write("\n")
    return path
