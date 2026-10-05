"""Exploratory reproduction for docs/design/erc-well-tap-connectivity-spike.md.

NOT a shipped fixture or test. This script generates three small, labelled
synthetic GDS layouts plus a set of `klt erc` spec variants, then runs the
current, unmodified `run_erc` against every (layout, variant) pair and prints
the island/short/gate outcome each one produces. No variant changes klt's
source: the "candidate mechanisms" are *emulated* with keys the spec already
accepts (an extra `stackup` role for the well plus a `vias` entry that names
the bridging layer), so the numbers show what each candidate connectivity
rule would do to the graph without any of them existing.

Usage (from the repository root):

    uv run python docs/design/erc_well_tap_spike_repro.py --out /tmp/wtspike

Every fixture/spec is written under `--out`, so each row can also be
re-run individually, e.g.:

    uv run klt erc /tmp/wtspike/same_well.gds \
        /tmp/wtspike/same_well.baseline.json --format json

Layer numbers follow sky130's public GDS map (open PDK) for readability:
nwell 64/20, diff 65/20, tap 65/44, poly 66/20, licon 66/44, li1 67/20
(label 67/5), mcon 67/44, met1 68/20 (label 68/5), nsdm 93/44, psdm 94/20.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import klayout.db as kdb

from klayout_tools.erc import run_erc

DBU = 0.001

L = {
    "nwell": (64, 20),
    "diff": (65, 20),
    "tap": (65, 44),
    "poly": (66, 20),
    "licon": (66, 44),
    "li1": (67, 20),
    "li1_label": (67, 5),
    "mcon": (67, 44),
    "met1": (68, 20),
    "met1_label": (68, 5),
    "nsdm": (93, 44),
    "psdm": (94, 20),
}


def _um(v: float) -> int:
    return int(round(v / DBU))


class _Cell:
    """Tiny drawing helper: boxes and labels in micrometres."""

    def __init__(self) -> None:
        self.layout = kdb.Layout()
        self.layout.dbu = DBU
        self.top = self.layout.create_cell("TOP")

    def box(self, layer: str, x0: float, y0: float, x1: float, y1: float) -> None:
        idx = self.layout.layer(*L[layer])
        self.top.shapes(idx).insert(kdb.Box(_um(x0), _um(y0), _um(x1), _um(y1)))

    def label(self, layer: str, text: str, x: float, y: float) -> None:
        idx = self.layout.layer(*L[layer])
        self.top.shapes(idx).insert(kdb.Text(text, kdb.Trans(_um(x), _um(y))))

    def write(self, path: Path) -> None:
        self.layout.write(str(path))


def _tap(
    c: _Cell, x0: float, rail_y: float, implant: str = "nsdm", net: str = "VPWR"
) -> None:
    """A tap (tap + `implant`: nsdm = n-well tap, psdm = substrate tap) at
    (x0, 1)-(x0+2, 3), contacted up to li1 and through mcon to a met1 rail
    whose bottom edge is at `rail_y`. The li1 pad carries its own `net`
    label (both pieces of one island carry the name, so the extra label
    cannot change any island count)."""
    c.box("tap", x0, 1, x0 + 2, 3)
    c.box(implant, x0 - 0.25, 0.75, x0 + 2.25, 3.25)
    c.box("licon", x0 + 0.5, 1.5, x0 + 1.5, 2.5)
    c.box("li1", x0, 1, x0 + 2, rail_y + 1)
    c.box("mcon", x0 + 0.5, rail_y + 0.2, x0 + 1.5, rail_y + 0.8)
    c.label("li1_label", net, x0 + 1, 2)


def _tie_gate(c: _Cell, implant: str = "psdm") -> None:
    """A poly-over-diff gate whose poly is strapped (licon -> li1 -> mcon)
    up to the *left* supply met1 fragment, i.e. a tie-hi (psdm, in the
    well) or tie-lo (nsdm, in the substrate) gate net that shares its
    electrical net with supply fragment A."""
    c.box("diff", 5, 5.5, 9, 7.5)
    c.box(implant, 4.75, 5.25, 9.25, 7.75)
    c.box("poly", 6.5, 5.0, 7.5, 8.0)
    c.box("licon", 6.6, 5.05, 7.4, 5.4)
    c.box("li1", 6.5, 4.5, 7.5, 5.45)
    c.box("mcon", 6.6, 4.55, 7.4, 4.9)


def same_well(path: Path) -> None:
    """E1: one merged n-well, two n-taps, each strapped to its *own* met1
    VPWR fragment; no metal joins the two fragments. Geometrically this is
    also exactly a severed VPWR rail whose two halves both tap one well."""
    c = _Cell()
    c.box("nwell", 0, 0, 20, 8)
    _tap(c, 1, 3)
    _tap(c, 17, 3)
    c.box("met1", 0, 3, 8, 5)
    c.box("met1", 12, 3, 20, 5)
    c.label("met1_label", "VPWR", 4, 4)
    c.label("met1_label", "VPWR", 16, 4)
    _tie_gate(c)
    c.write(path)


def separate_wells(path: Path) -> None:
    """E2: identical metal/tap geometry to E1, but the n-well is drawn as two
    disjoint polygons, one under each fragment -- the geometry #2180's
    reporter described for the two smaller islands."""
    c = _Cell()
    c.box("nwell", 0, 0, 9.5, 8)
    c.box("nwell", 10.5, 0, 20, 8)
    _tap(c, 1, 3)
    _tap(c, 17, 3)
    c.box("met1", 0, 3, 8, 5)
    c.box("met1", 12, 3, 20, 5)
    c.label("met1_label", "VPWR", 4, 4)
    c.label("met1_label", "VPWR", 16, 4)
    _tie_gate(c)
    c.write(path)


def substrate_taps(path: Path) -> None:
    """E4: no well at all. Two p-substrate taps, each strapped to its *own*
    met1 VGND fragment; no metal joins them. The substrate is one
    continuous p-type body, but no drawn layer marks it."""
    c = _Cell()
    _tap(c, 1, 3, implant="psdm", net="VGND")
    _tap(c, 17, 3, implant="psdm", net="VGND")
    c.box("met1", 0, 3, 8, 5)
    c.box("met1", 12, 3, 20, 5)
    c.label("met1_label", "VGND", 4, 4)
    c.label("met1_label", "VGND", 16, 4)
    _tie_gate(c, implant="nsdm")
    c.write(path)


def cmos_inverter(path: Path) -> None:
    """E3: an ordinary CMOS inverter. PMOS in the n-well, NMOS outside it;
    each device's source, channel and drain are one continuous drawn diff
    polygon; one n-tap ties the well to VPWR."""
    c = _Cell()
    c.box("nwell", 0, 7, 12, 15)
    # PMOS (in the well) and NMOS (outside it): one diff polygon each.
    c.box("diff", 2, 9, 8, 12)
    c.box("psdm", 1.75, 8.75, 8.25, 12.25)
    c.box("diff", 2, 1, 8, 4)
    c.box("nsdm", 1.75, 0.75, 8.25, 4.25)
    # Shared gate poly, contacted to an IN pad between the two devices.
    c.box("poly", 4.5, 0.5, 5.5, 12.5)
    c.box("licon", 4.6, 6.0, 5.4, 6.6)
    c.box("li1", 4.25, 5.75, 5.75, 6.85)
    c.label("li1_label", "IN", 5, 6.3)
    # PMOS source -> VPWR rail (top).
    c.box("licon", 2.5, 10, 3.2, 10.7)
    c.box("li1", 2.25, 9.75, 3.5, 13.5)
    c.box("mcon", 2.5, 12.8, 3.2, 13.3)
    c.box("met1", 0, 12.5, 12, 14.5)
    c.label("met1_label", "VPWR", 6, 13.5)
    # Both drains -> OUT li1 strip.
    c.box("licon", 6.8, 10, 7.5, 10.7)
    c.box("licon", 6.8, 2, 7.5, 2.7)
    c.box("li1", 6.5, 1.5, 7.75, 11.5)
    c.label("li1_label", "OUT", 7.1, 6)
    # NMOS source -> VGND rail (bottom).
    c.box("licon", 2.5, 2, 3.2, 2.7)
    c.box("li1", 2.25, -0.5, 3.5, 3)
    c.box("mcon", 2.5, -0.3, 3.2, 0.2)
    c.box("met1", 0, -1, 12, 0.5)
    c.label("met1_label", "VGND", 6, -0.25)
    # One n-tap in the well, strapped to the VPWR rail.
    c.box("tap", 9.5, 9, 11, 10.5)
    c.box("nsdm", 9.25, 8.75, 11.25, 10.75)
    c.box("licon", 9.9, 9.4, 10.6, 10.1)
    c.box("li1", 9.5, 9, 11, 13.5)
    c.box("mcon", 9.9, 12.8, 10.6, 13.3)
    c.write(path)


def _ld(name: str) -> str:
    layer, datatype = L[name]
    return f"{layer}/{datatype}"


def _base_spec(nets: list[dict]) -> dict:
    return {
        "stackup": [
            {
                "name": "poly",
                "layer": _ld("poly"),
                "role": "gate",
                "active_layer": _ld("diff"),
            },
            {"name": "li1", "layer": _ld("li1"), "label_layer": _ld("li1_label")},
            {"name": "met1", "layer": _ld("met1"), "label_layer": _ld("met1_label")},
        ],
        "vias": [
            {"name": "licon", "layer": _ld("licon"), "between": ["poly", "li1"]},
            {"name": "mcon", "layer": _ld("mcon"), "between": ["li1", "met1"]},
        ],
        "nets": nets,
    }


def _with_well_bridge(spec: dict, bridge_layer: str, bridge_name: str) -> dict:
    """Emulate a candidate well-continuity rule with existing keys: register
    the n-well as one more (self-connected) `stackup` role, and bridge it to
    li1 through `bridge_layer` -- the only place well and routing may join."""
    out = json.loads(json.dumps(spec))
    out["stackup"].append({"name": "nwell", "layer": _ld("nwell")})
    out["vias"].append(
        {"name": bridge_name, "layer": _ld(bridge_layer), "between": ["nwell", "li1"]}
    )
    return out


def _variants(nets: list[dict]) -> dict[str, dict]:
    base = _base_spec(nets)
    tie = dict(base)
    tie["ties"] = [
        {
            "name": "nwell_tie",
            "well_layer": _ld("nwell"),
            "tap_layer": _ld("tap"),
            # sky130's `tap` (65/44) is drawn only for taps: affirm it, so
            # the tie is graded as checked work rather than skipped as a
            # degenerate (#2199) declaration.
            "tap_is_dedicated": True,
            "connect_to": "li1",
            "net": "VPWR",
        }
    ]
    diff = json.loads(json.dumps(base))
    diff["stackup"].insert(1, {"name": "diff", "layer": _ld("diff")})
    diff["vias"].append(
        {"name": "licon_diff", "layer": _ld("licon"), "between": ["diff", "li1"]}
    )
    return {
        # Today's model, unchanged.
        "baseline": base,
        # Today's model plus a correctly narrowed ties[] entry (#2169 graph).
        "ties": tie,
        # Candidate: well conducts, joined to routing ONLY at tap sites.
        "well_via_tap": _with_well_bridge(base, "tap", "tap_bridge"),
        # Pre-#2169 / degenerate-tie shape: well joined at EVERY contact in it.
        "well_via_licon": _with_well_bridge(base, "licon", "licon_bridge"),
        # Naive: raw diffusion registered as an ordinary conductor.
        "diff_conductor": diff,
    }


EXPERIMENTS = {
    "same_well": (same_well, [{"name": "VPWR", "kind": "supply"}]),
    "separate_wells": (separate_wells, [{"name": "VPWR", "kind": "supply"}]),
    "substrate_taps": (substrate_taps, [{"name": "VGND", "kind": "supply"}]),
    "cmos_inverter": (
        cmos_inverter,
        [
            {"name": "VPWR", "kind": "supply"},
            {"name": "VGND", "kind": "supply"},
            {"name": "IN"},
            {"name": "OUT"},
        ],
    ),
}


def _summary(report: dict) -> dict:
    met1 = []
    for gate in report["gates"]:
        level = next(lv for lv in gate["levels"] if lv["layer"] == "met1")
        met1.append(
            {
                "net": gate["net"],
                "met1_cumulative_um2": level["cumulative_area_um2"],
                "met1_antenna_ratio": level["antenna_ratio"],
            }
        )
    return {
        "islands": {n["name"]: n["matched_islands"] for n in report["nets"]},
        "findings": sorted(
            f"{f['rule']}({f['net']}"
            + (f",{f['other_net']}" if f["other_net"] else "")
            + ")"
            for f in report["erc_findings"]
        ),
        "gate_count": report["gate_count"],
        "gates_met1": met1,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    results: dict[str, dict] = {}
    for name, (build, nets) in EXPERIMENTS.items():
        gds = args.out / f"{name}.gds"
        build(gds)
        for variant, spec in _variants(nets).items():
            if name == "substrate_taps" and variant == "ties":
                # E4 draws no n-well, so the n-well tie has nothing to grade
                # (it would only be skipped as `empty_well_region`, #2377).
                continue
            spec_path = args.out / f"{name}.{variant}.json"
            spec_path.write_text(json.dumps(spec, indent=2) + "\n")
            report = run_erc(str(gds), str(spec_path))
            results[f"{name}.{variant}"] = _summary(report)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
