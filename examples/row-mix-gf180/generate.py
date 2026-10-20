#!/usr/bin/env python3
"""Generate `examples/row-mix-gf180/row_mix_gf180.gds`: a small, generic
mixed-device, shared-diffusion, fixed-pitch layout on gf180mcu -- the
`row_mix_gf180` worked example from `docs/design/custom-transistor-layout-
study.md` section 4.1 (issue #3969).

**Provenance.** Authored here from the study's section 4.1 specification with
`klayout.db`; it is *not* derived from, and does not validate, any downstream
generator (none has been inspected -- see the README). This script is a
one-off example, deliberately *not* a reusable row engine: the study's Stage 2
(`mos_row`) stays gated.

Layout (y up), gf180mcuD layers, 0.8 um gate pitch::

    VDD rail + n+ well tie        ------------------------------
    P strip   (W 1.6, in Nwell)   shared-diffusion chains, common/single poly
    routing channel               M1 gate pads/bars, M2 node verticals/trunk
    N strip   (W 0.8)             shared-diffusion chains
    VSS rail + p+ substrate tie   ------------------------------

Each row is a chain of devices whose facing source/drain nodes are *one*
diffusion shape. Orientation is derived (a device is flipped when its right
terminal matches the previous node), and a mismatch between facing nets with no
explicit ``BREAK`` is a hard `RowError`, not a silent short.

Run from the repo root::

    uv run python3 examples/row-mix-gf180/generate.py

Deterministic: two runs write byte-identical GDS (no timestamps).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import klayout.db as kdb

DBU_UM = 0.001

# gf180mcu layer/datatype pairs (curated deck, `decks/gf180mcu.py`).
COMP, POLY, CONTACT = (22, 0), (30, 0), (33, 0)
NWELL, NPLUS, PPLUS = (21, 0), (32, 0), (31, 0)
M1, VIA1, M2 = (34, 0), (35, 0), (36, 0)
M1_LABEL, M2_LABEL = (34, 10), (36, 10)

# Geometry constants (um). Every value sits on the 5 nm manufacturing grid.
GATE_L = 0.28
NODE_PITCH = (
    0.80  # node centre to node centre: L + 2*(contact/2 + 0.15 poly-contact gap)
)
HALF_NODE = 0.18  # comp edge beyond the end node's contact centre
CONTACT_SZ = 0.22
CONTACT_PAD_M1 = 0.30  # M1 pad over a contact / via1
VIA_SZ = 0.26
M2_W = 0.28
POLY_PAD = 0.36  # poly landing under a gate contact (contact + 2*0.07)
POLY_ENDCAP = 0.22
IMPLANT_MARGIN = 0.23
W_N, W_P = 0.80, 1.60
TIE_H = 0.40

# Vertical plan.
Y_RAIL_VSS = (0.15, 0.55)
Y_N = (1.61, 1.61 + W_N)
Y_P = (7.30, 7.30 + W_P)
Y_TIE_VDD = (Y_P[1] + 0.80, Y_P[1] + 0.80 + TIE_H)
Y_DUMMY_N_PAD = 1.20
Y_DUMMY_P_PAD = 9.25

MIN_EDGE_MARGIN = 0.14  # half comp.space.1 so a mirrored copy abuts legally
PORT_MIN_PITCH = CONTACT_PAD_M1 + 0.23  # M1 pad + M1 space

#: Routing grid for caller-specified port x (half the gate pitch).
ROUTING_GRID_X = 0.40

PITCH_UM = 8.00

#: Requested port x (um). ``B`` is deliberately off the routing grid.
PORT_X = {"A": 0.80, "B": 3.17, "C": 4.80}

BREAK = "BREAK"


class RowError(ValueError):
    """A generator-detected failure; ``code`` is the stable error code."""

    def __init__(self, code: str, message: str, **detail: object) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Device:
    name: str
    polarity: str  # "n" | "p"
    drain: str
    gate: str
    source: str


def _dev(name: str, d: str, g: str, s: str) -> Device:
    return Device(name, "n" if name.startswith("MN") else "p", d, g, s)


DEVICES = [
    # inverter 1
    _dev("MN1", "n1", "A", "VSS"),
    _dev("MP1", "n1", "A", "VDD"),
    # transmission gate: A-side net n1 -> n2, complementary controls
    _dev("MN2", "n2", "S", "n1"),
    _dev("MP2", "n2", "SB", "n1"),
    # NOR3 (inputs n2, B, C -> ny)
    _dev("MN3a", "ny", "n2", "VSS"),
    _dev("MN3b", "ny", "B", "VSS"),
    _dev("MN3c", "ny", "C", "VSS"),
    _dev("MP3a", "p1", "n2", "VDD"),
    _dev("MP3b", "p2", "B", "p1"),
    _dev("MP3c", "ny", "C", "p2"),
    # inverter 2 (ny -> Y)
    _dev("MN4", "Y", "ny", "VSS"),
    _dev("MP4", "Y", "ny", "VDD"),
    # dummy gates (off: gate tied to the row's own supply)
    _dev("MN5", "Y", "VSS", "VSS"),
    _dev("MP5", "Y", "VDD", "VDD"),
]

#: Row assignment. ``BREAK`` starts a new diffusion island at the next node. The
#: breaks are placed so the N and P rows put matching gates in the same column.
CHAINS = {
    "n": ["MN1", "MN2", BREAK, "MN3a", "MN3b", "MN3c", BREAK, "MN4", "MN5"],
    "p": ["MP1", "MP2", BREAK, "MP3a", "MP3b", "MP3c", BREAK, "MP4", "MP5"],
}

SUPPLY = {"VSS": "n", "VDD": "p"}

#: Gate net -> (pad y, node-net to bar to or None). Pad y is the net's track.
GATE_TRACKS = {
    "S": 3.00,  # N-only gate
    "A": 3.60,  # common gate
    "ny": 4.20,  # common gate (INV2); bar to the ny trunk
    "n2": 4.80,  # common gate (NOR3 a); bar to n2's vertical
    "B": 5.40,
    "C": 6.00,
    "SB": 6.60,  # P-only gate
}

#: Diffusion nets crossing the channel on M2: "vertical" joins the N and P
#: contact at one x; a number is a horizontal trunk y the node verticals reach.
NET_ROUTES = {"n1": "vertical", "n2": "vertical", "Y": "vertical", "ny": 4.20}

#: Gate net -> diffusion net (and node x) its M1 bar must reach.
GATE_LINKS = {"n2": 2.0, "ny": 5.2}

Y_PIN_Y = 5.00  # Y's M2 pin height on its vertical


# --------------------------------------------------------------------------- #
# Chain solver
# --------------------------------------------------------------------------- #
@dataclass
class Chain:
    nodes: list[str]  # net per node
    links: list[tuple[str, str]]  # per gap: ("gate", device name) | ("break", "")
    orient: dict[str, tuple[str, str]]  # device -> (left net, right net)


def solve_chain(row: str, entries: list[str], devices: dict[str, Device]) -> Chain:
    nodes: list[str] = []
    links: list[tuple[str, str]] = []
    orient: dict[str, tuple[str, str]] = {}
    prev: str | None = None
    pending_break = True
    for entry in entries:
        if entry == BREAK:
            pending_break = True
            continue
        dev = devices[entry]
        if pending_break or not nodes:
            left, right = dev.source, dev.drain
            if nodes:
                links.append(("break", ""))
            nodes.append(left)
        else:
            tail = nodes[-1]
            if dev.source == tail:
                left, right = dev.source, dev.drain
            elif dev.drain == tail:
                left, right = dev.drain, dev.source
            else:
                raise RowError(
                    "facing_net_mismatch",
                    f"row '{row}': {prev} ends on net '{tail}' but {dev.name} "
                    f"offers '{dev.source}'/'{dev.drain}' (add BREAK or reorder)",
                    row=row,
                    devices=[prev, dev.name],
                    nets=[tail, dev.source, dev.drain],
                )
        links.append(("gate", dev.name))
        nodes.append(right)
        orient[dev.name] = (left, right)
        pending_break = False
        prev = dev.name
    return Chain(nodes, links, orient)


# --------------------------------------------------------------------------- #
# Layout builder
# --------------------------------------------------------------------------- #
def _dbu(v: float) -> int:
    return int(round(v / DBU_UM))


class _Writer:
    def __init__(self, layout: kdb.Layout, cell: kdb.Cell) -> None:
        self.layout, self.cell = layout, cell

    def box(
        self, layer: tuple[int, int], x0: float, y0: float, x1: float, y1: float
    ) -> None:
        self.cell.shapes(self.layout.layer(*layer)).insert(
            kdb.Box(_dbu(x0), _dbu(y0), _dbu(x1), _dbu(y1))
        )

    def centered(
        self, layer: tuple[int, int], x: float, y: float, w: float, h: float
    ) -> None:
        self.box(layer, x - w / 2, y - h / 2, x + w / 2, y + h / 2)

    def label(self, layer: tuple[int, int], text: str, x: float, y: float) -> None:
        self.cell.shapes(self.layout.layer(*layer)).insert(
            kdb.Text(text, _dbu(x), _dbu(y))
        )


def min_pitch_um(chains: dict[str, Chain]) -> float:
    widest = max(len(c.nodes) for c in chains.values())
    return (widest - 1) * NODE_PITCH + 2 * HALF_NODE + 2 * MIN_EDGE_MARGIN


def isolated_width_um(devices: list[Device]) -> float:
    """Row width if every device kept its own two-contact diffusion island."""
    per_row = {"n": 0, "p": 0}
    for d in devices:
        per_row[d.polarity] += 1
    n = max(per_row.values())
    island = NODE_PITCH + 2 * HALF_NODE
    return n * island + (n - 1) * 0.28 + 2 * MIN_EDGE_MARGIN


def validate(
    devices: list[Device],
    chains_spec: dict[str, list[str]],
    port_x: dict[str, float],
    pitch_um: float,
) -> dict[str, Chain]:
    by_name = {d.name: d for d in devices}
    assigned = {e for entries in chains_spec.values() for e in entries if e != BREAK}
    missing = sorted(set(by_name) - assigned)
    if missing:
        raise RowError(
            "unassigned_device",
            f"netlist device(s) with no row assignment: {', '.join(missing)}",
            devices=missing,
        )
    chains = {
        row: solve_chain(row, entries, by_name) for row, entries in chains_spec.items()
    }
    need = min_pitch_um(chains)
    if pitch_um + 1e-9 < need:
        deficit = _dbu(need) - _dbu(pitch_um)
        raise RowError(
            "pitch_below_minimum",
            f"pitch {pitch_um} um is {deficit} dbu below the minimum legal "
            f"width {need:.3f} um",
            deficit_dbu=deficit,
        )
    names = sorted(port_x, key=port_x.get)
    for a, b in zip(names, names[1:], strict=False):
        if port_x[b] - port_x[a] < PORT_MIN_PITCH - 1e-9:
            raise RowError(
                "ports_too_close",
                f"ports '{a}' (x={port_x[a]}) and '{b}' (x={port_x[b]}) are closer "
                f"than "
                f"the metal pitch {PORT_MIN_PITCH:.2f} um",
                ports=[a, b],
            )
    return chains


def build_cell(
    layout: kdb.Layout,
    name: str = "row_mix_gf180",
    *,
    pitch_um: float = PITCH_UM,
    port_x: dict[str, float] | None = None,
    devices: list[Device] | None = None,
    chains_spec: dict[str, list[str]] | None = None,
    label_nets: set[str] | None = None,
    strap_vss: bool = True,
) -> kdb.Cell:
    port_x = dict(PORT_X if port_x is None else port_x)
    devices = DEVICES if devices is None else devices
    chains_spec = CHAINS if chains_spec is None else chains_spec
    chains = validate(devices, chains_spec, port_x, pitch_um)
    by_name = {d.name: d for d in devices}

    cell = layout.create_cell(name)
    w = _Writer(layout, cell)

    content = (
        max(len(c.nodes) for c in chains.values()) - 1
    ) * NODE_PITCH + 2 * HALF_NODE
    x0 = round((pitch_um - content) / 2 / 0.005) * 0.005 + HALF_NODE

    def node_x(k: int) -> float:
        return round(x0 + k * NODE_PITCH, 3)

    def gate_x(k: int) -> float:
        return round(x0 + (k + 0.5) * NODE_PITCH, 3)

    rows = {"n": Y_N, "p": Y_P}
    yc = {r: (y[0] + y[1]) / 2 for r, y in rows.items()}

    # --- well, implants, tie strips, rails ---------------------------------
    w.box(NWELL, 0, Y_P[0] - 0.45, pitch_um, Y_TIE_VDD[1] + 0.20)
    w.box(NPLUS, 0, Y_N[0] - IMPLANT_MARGIN, pitch_um, Y_N[1] + IMPLANT_MARGIN)
    w.box(PPLUS, 0, Y_P[0] - IMPLANT_MARGIN, pitch_um, Y_P[1] + IMPLANT_MARGIN)
    w.box(
        PPLUS,
        0,
        Y_RAIL_VSS[0] - IMPLANT_MARGIN,
        pitch_um,
        Y_RAIL_VSS[1] + IMPLANT_MARGIN,
    )
    w.box(
        NPLUS, 0, Y_TIE_VDD[0] - IMPLANT_MARGIN, pitch_um, Y_TIE_VDD[1] + IMPLANT_MARGIN
    )
    tie_x0, tie_x1 = x0 - HALF_NODE, round(x0 + content - HALF_NODE, 3)
    for ys in (Y_RAIL_VSS, Y_TIE_VDD):
        w.box(COMP, tie_x0, ys[0], tie_x1, ys[1])
        w.box(M1, 0, ys[0], pitch_um, ys[1])
    n_nodes = max(len(c.nodes) for c in chains.values())
    for k in range(n_nodes):
        for ys in (Y_RAIL_VSS, Y_TIE_VDD):
            w.centered(CONTACT, node_x(k), (ys[0] + ys[1]) / 2, CONTACT_SZ, CONTACT_SZ)

    # --- diffusion islands, gates ------------------------------------------
    gate_pad_done: set[tuple[str, int]] = set()
    row_gates: dict[str, dict[int, str]] = {"n": {}, "p": {}}
    for row, chain in chains.items():
        y0, y1 = rows[row]
        k = 0
        island_start = 0
        for gap, (kind, dev_name) in enumerate(chain.links + [("end", "")]):
            if kind in ("break", "end"):
                w.box(
                    COMP,
                    node_x(island_start) - HALF_NODE,
                    y0,
                    node_x(gap) + HALF_NODE,
                    y1,
                )
                island_start = gap + 1
            else:
                row_gates[row][gap] = by_name[dev_name].gate
        del k

    # poly per gate gap
    for gap in sorted(set(row_gates["n"]) | set(row_gates["p"])):
        gn, gp = row_gates["n"].get(gap), row_gates["p"].get(gap)
        gx = gate_x(gap)
        if gn is not None and gn == gp:
            ylo, yhi = Y_N[0] - POLY_ENDCAP, Y_P[1] + POLY_ENDCAP
            for g in (gn,):
                if g in GATE_TRACKS:
                    ty = GATE_TRACKS[g]
                    ylo, yhi = min(ylo, ty - POLY_PAD / 2), max(yhi, ty + POLY_PAD / 2)
            w.box(POLY, gx - GATE_L / 2, ylo, gx + GATE_L / 2, yhi)
            pads = [(gn, gx, GATE_TRACKS.get(gn))]
        else:
            pads = []
            for row, g in (("n", gn), ("p", gp)):
                if g is None:
                    continue
                y0, y1 = rows[row]
                ylo, yhi = y0 - POLY_ENDCAP, y1 + POLY_ENDCAP
                ty = GATE_TRACKS.get(g)
                if g in SUPPLY:  # dummy gate tied to its own rail
                    ty = Y_DUMMY_N_PAD if row == "n" else Y_DUMMY_P_PAD
                if ty is not None:
                    ylo, yhi = min(ylo, ty - POLY_PAD / 2), max(yhi, ty + POLY_PAD / 2)
                w.box(POLY, gx - GATE_L / 2, ylo, gx + GATE_L / 2, yhi)
                pads.append((g, gx, ty))
        for g, px, ty in pads:
            if ty is None:
                continue
            w.centered(POLY, px, ty, POLY_PAD, POLY_PAD)
            w.centered(CONTACT, px, ty, CONTACT_SZ, CONTACT_SZ)
            w.centered(M1, px, ty, CONTACT_PAD_M1, CONTACT_PAD_M1)
            gate_pad_done.add((g, gap))
            _route_gate(w, g, px, ty, port_x, pitch_um, label_nets)

    # --- diffusion node contacts and routing -------------------------------
    occurrences: dict[str, int] = {}
    for chain in chains.values():
        for net in chain.nodes:
            occurrences[net] = occurrences.get(net, 0) + 1
    ports_all = {"A", "B", "C", "S", "SB", "Y", *SUPPLY}
    trunk_nodes: dict[str, list[float]] = {}
    for row, chain in chains.items():
        for k, net in enumerate(chain.nodes):
            if occurrences[net] == 1 and net not in ports_all:
                continue  # internal series node: shared diffusion only
            x, y = node_x(k), yc[row]
            w.centered(CONTACT, x, y, CONTACT_SZ, CONTACT_SZ)
            w.centered(M1, x, y, CONTACT_PAD_M1, CONTACT_PAD_M1)
            if net in SUPPLY:
                rail = Y_RAIL_VSS if net == "VSS" else Y_TIE_VDD
                if net == "VSS" and not strap_vss and k == 0:
                    continue
                w.box(
                    M1,
                    x - CONTACT_PAD_M1 / 2,
                    min(rail[0], y),
                    x + CONTACT_PAD_M1 / 2,
                    max(rail[1], y),
                )
                continue
            route = NET_ROUTES[net]
            w.centered(VIA1, x, y, VIA_SZ, VIA_SZ)
            if route == "vertical":
                other = "p" if row == "n" else "n"
                if row == "n":
                    w.box(M2, x - M2_W / 2, y - 0.14, x + M2_W / 2, yc[other] + 0.14)
            else:
                lo, hi = sorted((y, route))
                w.box(
                    M2,
                    x - M2_W / 2,
                    lo - 0.14 if lo == y else lo - M2_W / 2,
                    x + M2_W / 2,
                    hi + 0.14 if hi == y else hi + M2_W / 2,
                )
                trunk_nodes.setdefault(net, []).append(x)
    for net, xs in trunk_nodes.items():
        ty = NET_ROUTES[net]
        link_x = GATE_LINKS.get(net)
        if link_x is not None:
            xs = xs + [link_x]
        w.box(M2, min(xs) - M2_W / 2, ty - M2_W / 2, max(xs) + M2_W / 2, ty + M2_W / 2)
        if link_x is not None:
            w.centered(VIA1, link_x, ty, VIA_SZ, VIA_SZ)
            w.centered(M1, link_x, ty, CONTACT_PAD_M1, CONTACT_PAD_M1)
    for net, link_x in GATE_LINKS.items():
        if net not in NET_ROUTES or not isinstance(NET_ROUTES[net], float):
            # n2: bar lands on the vertical, via on the existing M2 vertical
            ty = GATE_TRACKS[net]
            w.centered(VIA1, link_x, ty, VIA_SZ, VIA_SZ)
            w.centered(M1, link_x, ty, CONTACT_PAD_M1, CONTACT_PAD_M1)

    # --- labels -------------------------------------------------------------
    def want(net: str) -> bool:
        return label_nets is None or net in label_nets

    if want("VSS"):
        w.label(M1_LABEL, "VSS", 0.30, sum(Y_RAIL_VSS) / 2)
    if want("VDD"):
        w.label(M1_LABEL, "VDD", 0.30, sum(Y_TIE_VDD) / 2)
    if want("Y"):
        w.label(M2_LABEL, "Y", node_x(8), Y_PIN_Y)
    return cell


def _route_gate(
    w: _Writer,
    net: str,
    px: float,
    ty: float,
    port_x: dict[str, float],
    pitch_um: float,
    label_nets: set[str] | None,
) -> None:
    """M1 bar/label for the gate pad at (px, ty) of ``net``."""
    if net in SUPPLY:
        # dummy gate: bar to the same-row rail stub at the island's end node
        stub_x = px + NODE_PITCH / 2
        w.box(
            M1,
            px - CONTACT_PAD_M1 / 2,
            ty - CONTACT_PAD_M1 / 2,
            stub_x + CONTACT_PAD_M1 / 2,
            ty + CONTACT_PAD_M1 / 2,
        )
        return
    target = port_x.get(net, px)
    if net in GATE_LINKS:
        target = GATE_LINKS[net]
    if net == "S":  # control net runs edge to edge so it continues across abutment
        w.box(M1, 0, ty - CONTACT_PAD_M1 / 2, pitch_um, ty + CONTACT_PAD_M1 / 2)
    lo, hi = sorted((px, target))
    w.box(
        M1,
        lo - CONTACT_PAD_M1 / 2,
        ty - CONTACT_PAD_M1 / 2,
        hi + CONTACT_PAD_M1 / 2,
        ty + CONTACT_PAD_M1 / 2,
    )
    if (label_nets is None or net in label_nets) and net in {"A", "B", "C", "S", "SB"}:
        w.label(M1_LABEL, net, target if net in port_x else px, ty)


def build_layout(**kwargs: object) -> kdb.Layout:
    layout = kdb.Layout()
    layout.dbu = DBU_UM
    top = build_cell(layout, **kwargs)  # type: ignore[arg-type]
    top.name = "row_mix_gf180"
    return layout


def build_pair(layout_pitch_um: float = PITCH_UM) -> kdb.Layout:
    """Two abutted copies (the second mirrored) -- the supply/control
    continuity check. Only nets that must merge across the seam are labelled."""
    layout = kdb.Layout()
    layout.dbu = DBU_UM
    unit = build_cell(layout, "row_mix_gf180", label_nets={"VSS", "VDD", "S"})
    top = layout.create_cell("PAIR")
    top.insert(kdb.CellInstArray(unit.cell_index(), kdb.Trans()))
    top.insert(
        kdb.CellInstArray(
            unit.cell_index(), kdb.Trans(kdb.Trans.M90, _dbu(2 * layout_pitch_um), 0)
        )
    )
    return layout


def write_gds(layout: kdb.Layout, path: str) -> None:
    opts = kdb.SaveLayoutOptions()
    opts.gds2_write_timestamps = False  # byte-identical reruns (#320)
    layout.write(path, opts)


def write_receipts(here: str) -> list[str]:
    """Run the existing headless `klt` checks on the committed GDS and write
    their reports (receipts). Environment-dependent blocks are stripped, as in
    `examples/drc/example.drc.json`; paths are relative to this directory."""
    import json
    import tempfile

    from klayout_tools.drc import run_drc
    from klayout_tools.extract import run_extract
    from klayout_tools.lvs import run_lvs

    cwd = os.getcwd()
    os.chdir(here)
    written = []
    try:
        gds = "row_mix_gf180.gds"
        with tempfile.TemporaryDirectory() as scratch:
            drc = run_drc(gds, "gf180mcu")
            ext = run_extract(gds, "gf180mcu", output=os.path.join(scratch, "x.spice"))
            request = os.path.join(scratch, "lvs.json")
            with open(request, "w") as f:
                json.dump(
                    {
                        "layout": {"file": os.path.abspath(gds), "deck": "gf180mcu"},
                        "reference": {"netlist": os.path.abspath("row_mix_gf180.sp")},
                    },
                    f,
                )
            lvs = run_lvs(request)
        receipts = {
            "row_mix_gf180.drc.json": drc,
            "row_mix_gf180.lvs.json": {
                "status": lvs["status"],
                "counts": lvs["counts"],
                "mismatches": lvs["mismatches"],
                "engine": lvs["engine"],
            },
            "row_mix_gf180.extract.json": {
                "status": ext["status"],
                "device_count": ext["device_count"],
                "device_counts": ext["device_counts"],
                "net_count": ext["net_count"],
                "split_net_labels": ext["split_net_labels"],
                "devices": ext["devices"],
                "nets": ext["nets"],
            },
        }
        for name, report in receipts.items():
            report.pop("provenance", None)
            report.pop("file", None)
            with open(name, "w") as f:
                json.dump(report, f, indent=2, sort_keys=True)
                f.write("\n")
            written.append(name)
    finally:
        os.chdir(cwd)
    if drc["status"] != "clean" or lvs["status"] != "match":
        raise SystemExit(f"receipts not clean: drc={drc['status']} lvs={lvs['status']}")
    return written


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, "row_mix_gf180.gds")
    write_gds(build_layout(), out)
    pair = os.path.join(here, "row_mix_gf180_pair.gds")
    write_gds(build_pair(), pair)
    print(f"wrote {out}")
    print(f"wrote {pair}")
    for name in write_receipts(here):
        print(f"wrote {os.path.join(here, name)}")


if __name__ == "__main__":
    main()
