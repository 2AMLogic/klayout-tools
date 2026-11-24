#!/usr/bin/env python3
"""Epic #5199 phase 1 (issue #5201): grade ASAP7's first-order extraction
(`klt extract --deck asap7 --parasitics`) against `klt mom`'s field solver on
dense parallel lines.

Mirrors `examples/critical-net-mom-fidelity/` (sky130); phases 2-3 of #5199
are deliberately not here.

## Fixture (one per sweep point)

N parallel lines on one metal level `Mm` at `pitch_mult` x the LEF pitch,
plus three orthogonal lines each on `Mm-1` and `Mm+1` (nets `N*`, `D0..2`,
`U0..2`); the victim is the middle parallel line. Sweep: level (M2, M4) x
pitch (1x, 2x, 4x) x density (`dense`: 5 lines, `sparse`: 3) at `LENGTH_UM`.

## Three extraction paths

(a) `klt extract --deck asap7 --parasitics` (`run_extract`) -- the deck's
    lumped per-net R, ground C, and *vertical* overlap coupling.
(b) the same with `--critical-net <victim>`, **refused for every FinFET deck**
    (`extract_finfet.reject_unsupported_options`). The script calls it anyway
    and records the refusal verbatim: (b) reads `"unsupported"`, never
    dropped or zero-filled.
(c) `klt mom`'s native solver (`solve_capacitance_matrix`, in memory, as
    `klt extract --mom-net` does) for capacitance, and
    `run_mom(..., compute_inductance=True)` (PEEC) on one victim-shaped bar
    per level for DC resistance.

The relative error of (a)/(b) vs (c) is `100 * (a - c) / |c|`.

## Stack and idealisations

One explicit `stackup` (`asap7-stackup.json`) is written from `STACK`; the
cited vs ASSUMED inputs and the reference idealisations are in `README.md`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile

import klayout.db as kdb

from klayout_tools.decks import get_parasitics_deck
from klayout_tools.extract import run_extract
from klayout_tools.extract_spef import ExtractError
from klayout_tools.mom import MomError, run_mom, solve_capacitance_matrix

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
DBU_UM = 0.00025  # asap7.lyt
LAMBDAPDK_ROOT = os.path.join(REPO_ROOT, "pdks", "lambdapdk", "lambdapdk")

#: sha256 of the cited lambdapdk v0.2.17 files (same pins as decks/asap7.py).
SOURCES = {
    "asap7_tech.lef": (
        "asap7/base/apr/asap7_tech.lef",
        "830a032810b054d0dc24601c3f57c86073469e6c9cfb0314b2c82eb335b021d2",
    ),
    "typical.rules": (
        "asap7/base/pex/openroad/typical.rules",
        "32a117e5e866d3de1406e8f18d65b6f1857781f156638941a4769e25ef5252cc",
    ),
    "asap7sc7p5t_28_R.sp": (
        "asap7/libs/asap7sc7p5t_rvt/netlist/asap7sc7p5t_28_R.sp",
        "4f5c997ac109ccd9c8dcd02f204d553754381d0750af8a9c3df578bb6e6df88b",
    ),
}

#: Effective metal resistivity, ohm*m: median of R*w*t/l over the 17695
#: `$layer=M1` resistors of the pinned RVT RC netlist.
RHO_OHM_M = 1.0913e-07
BACKGROUND_PERMITTIVITY = 3.9
ILD_BELOW_M1_UM = 0.036  # ASSUMED: ground plate one M1 thickness below M1
LENGTH_UM = 0.36
PLATE_PAD_UM = 0.1

#: level -> (GDS layer, LEF width um, LEF pitch um, direction). M2 pitch is
#: 2 x its width (the LEF carries WIDTH/SPACING only); thickness derived below.
LEVELS = {
    1: (19, 0.018, 0.036, "V"),
    2: (20, 0.018, 0.036, "H"),
    3: (30, 0.018, 0.036, "V"),
    4: (40, 0.024, 0.048, "H"),
    5: (50, 0.024, 0.048, "V"),
}
M1_THICKNESS_UM = 0.036  # cited (pinned RC netlist)


def build_stack() -> dict[int, dict]:
    """level -> {layer, width_um, pitch_um, direction, thickness_um, z0_um,
    z1_um}. M1 thickness cited; the rest ASSUMED (see module docstring)."""
    stack: dict[int, dict] = {}
    z = 0.0
    for level, (layer, width, pitch, direction) in sorted(LEVELS.items()):
        thickness = M1_THICKNESS_UM if level == 1 else round(2 * width, 6)
        stack[level] = {
            "layer": layer,
            "width_um": width,
            "pitch_um": pitch,
            "direction": direction,
            "thickness_um": thickness,
            "z0_um": round(z, 6),
            "z1_um": round(z + thickness, 6),
        }
        z += thickness + thickness  # ILD height = this level's thickness
    return stack


STACK = build_stack()


def _sha256(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def source_provenance() -> dict:
    """Hash every cited lambdapdk file that is present; `verified` is False
    (not an error) when `scripts/fetch-pdks.sh` has not been run."""
    out = {}
    for name, (rel, pinned) in SOURCES.items():
        path = os.path.join(LAMBDAPDK_ROOT, rel)
        actual = _sha256(path) if os.path.exists(path) else None
        out[name] = {
            "lambdapdk_path": f"lambdapdk/{rel}",
            "release": "v0.2.17",
            "pinned_sha256": pinned,
            "present": actual is not None,
            "verified": actual == pinned,
        }
    return out


def stackup_spec() -> dict:
    """The explicit `klt mom` stackup (spec format), one conductor per level."""
    return {
        "_comment": (
            "generated by generate_and_measure.py from STACK; see its "
            "docstring for what is cited vs ASSUMED"
        ),
        "background_permittivity": BACKGROUND_PERMITTIVITY,
        "stackup": [
            {
                "layer": f"{v['layer']}/0",
                "conductor": f"M{level}",
                "z0_um": v["z0_um"],
                "z1_um": v["z1_um"],
                "conductivity_S_per_m": round(1.0 / RHO_OHM_M, 1),
            }
            for level, v in STACK.items()
        ],
    }


# --------------------------------------------------------------------------- #
# Fixture
# --------------------------------------------------------------------------- #


def point_geometry(level: int, pitch_mult: int, density: str, length_um: float) -> dict:
    """Pure geometry: `{net: {level, x0, y0, x1, y1}}` (um) plus the victim."""
    own = STACK[level]
    n = 5 if density == "dense" else 3
    pitch = pitch_mult * own["pitch_um"]
    w = own["width_um"]
    shapes: dict[str, dict] = {}
    for i in range(n):
        shapes[f"N{i}"] = {
            "level": level,
            "x0": 0.0,
            "y0": i * pitch,
            "x1": length_um,
            "y1": i * pitch + w,
        }
    span = (n - 1) * pitch + w
    for tag, olevel in (("D", level - 1), ("U", level + 1)):
        o = STACK[olevel]
        for j in range(3):
            xc = length_um / 2 + (j - 1) * o["pitch_um"]
            shapes[f"{tag}{j}"] = {
                "level": olevel,
                "x0": xc - o["width_um"] / 2,
                "y0": -0.05,
                "x1": xc + o["width_um"] / 2,
                "y1": span + 0.05,
            }
    return {"shapes": shapes, "victim": f"N{n // 2}", "pitch_um": pitch}


def _dbu(value_um: float) -> int:
    return round(value_um / DBU_UM)


def write_gds(shapes: dict, path: str) -> None:
    layout = kdb.Layout()
    layout.dbu = DBU_UM
    top = layout.create_cell("TOP")
    for net, s in shapes.items():
        layer = STACK[s["level"]]["layer"]
        box = kdb.Box(_dbu(s["x0"]), _dbu(s["y0"]), _dbu(s["x1"]), _dbu(s["y1"]))
        top.shapes(layout.layer(layer, 0)).insert(box)
        top.shapes(layout.layer(layer, 251)).insert(
            kdb.Text(net, kdb.Trans(box.center().x, box.center().y))
        )
    layout.write(path)


def _rel_err(value: float | None, ref: float | None) -> float | None:
    if value is None or ref is None or ref == 0:
        return None
    return round(100.0 * (value - ref) / abs(ref), 2)


def measure_deck(gds: str, victim: str, spice: str) -> dict:
    """(a) `--parasitics` and (b) `--critical-net`."""
    report = run_extract(gds, "asap7", output=spice, parasitics=True)
    entry = next(n for n in report["parasitics"]["nets"] if n["net"] == victim)
    coupled = {c["net"]: c["capacitance_ff"] for c in entry["coupled"]}
    parasitics = {
        "resistance_ohm": entry["resistance_ohm"],
        "c_ground_ff": entry["capacitance_ff"],
        "c_same_level_ff": round(
            sum(v for k, v in coupled.items() if k.startswith("N")), 6
        ),
        "c_adjacent_levels_ff": round(
            sum(v for k, v in coupled.items() if k[0] in "UD"), 6
        ),
        "coupled_ff": coupled,
    }
    try:
        run_extract(
            gds,
            "asap7",
            output=spice,
            parasitics=True,
            critical_nets=[victim],
        )
        critical: dict = {"status": "supported"}  # pragma: no cover - would be news
    except ExtractError as exc:
        critical = {"status": "unsupported", "reason": str(exc)}
    return {
        "parasitics": parasitics,
        "critical_net": critical,
        "tool_version": report["provenance"]["klt_version"],
        "klayout_version": report["provenance"]["klayout_version"],
        "deck_content_hash": report["provenance"]["deck"]["content_hash"],
    }


def _box(s: dict) -> dict:
    z = STACK[s["level"]]
    return {
        "x0_um": s["x0"],
        "y0_um": s["y0"],
        "x1_um": s["x1"],
        "y1_um": s["y1"],
        "z0_um": z["z0_um"],
        "z1_um": z["z1_um"],
    }


def measure_mom(geometry: dict, panel_sizes: list[float]) -> dict:
    """(c) capacitance of the victim, finest feasible panel size reported."""
    shapes = geometry["shapes"]
    xs = [v for s in shapes.values() for v in (s["x0"], s["x1"])]
    ys = [v for s in shapes.values() for v in (s["y0"], s["y1"])]
    plate_z = -ILD_BELOW_M1_UM
    plate = {
        "x0_um": min(xs) - PLATE_PAD_UM,
        "y0_um": min(ys) - PLATE_PAD_UM,
        "x1_um": max(xs) + PLATE_PAD_UM,
        "y1_um": max(ys) + PLATE_PAD_UM,
        "z0_um": plate_z,
        "z1_um": plate_z,
    }
    conductors = [{"name": net, "boxes": [_box(s)]} for net, s in shapes.items()]
    conductors.append({"name": "GND", "boxes": [plate]})
    victim = geometry["victim"]
    attempts = []
    for size in panel_sizes:
        try:
            response = solve_capacitance_matrix(
                conductors, BACKGROUND_PERMITTIVITY, panel_size_um=size
            )
        except MomError as exc:
            attempts.append({"panel_size_um": size, "error": str(exc)})
            continue
        idx = {name: i for i, name in enumerate(response["conductors"])}
        row = response["capacitance_matrix_ff"][idx[victim]]

        coupling = {name: -row[i] for name, i in idx.items()}
        same = {n: coupling[n] for n in shapes if n.startswith("N") and n != victim}
        adjacent = {n: coupling[n] for n in shapes if n[0] in "UD"}
        attempts.append(
            {
                "panel_size_um": size,
                "panel_count": response["panel_count"],
                "c_ground_ff": round(coupling["GND"], 6),
                "c_same_level_ff": round(sum(same.values()), 6),
                "c_adjacent_levels_ff": round(sum(adjacent.values()), 6),
                "c_total_ff": round(row[idx[victim]], 6),
                "coupling_each_ff": {
                    k: round(v, 6) for k, v in {**same, **adjacent}.items()
                },
                "warnings": response["warnings"],
            }
        )
    solved = [a for a in attempts if "c_total_ff" in a]
    if not solved:
        raise SystemExit(f"klt mom could not solve any panel size: {attempts}")
    return {"converged": solved[-1], "mesh_refinement": attempts}


def measure_mom_resistance(level: int, length_um: float) -> dict:
    """(c) DC resistance of one isolated bar of the level's width via PEEC."""
    own = STACK[level]
    with tempfile.TemporaryDirectory() as tmp:
        gds = os.path.join(tmp, "bar.gds")
        layout = kdb.Layout()
        layout.dbu = DBU_UM
        top = layout.create_cell("TOP")
        top.shapes(layout.layer(own["layer"], 0)).insert(
            kdb.Box(0, 0, _dbu(length_um), _dbu(own["width_um"]))
        )
        layout.write(gds)
        spec = {
            "background_permittivity": BACKGROUND_PERMITTIVITY,
            "compute_inductance": True,
            "filament_size_um": own["width_um"] / 2,
            "stackup": [
                {
                    "layer": f"{own['layer']}/0",
                    "conductor": "bar",
                    "z0_um": own["z0_um"],
                    "z1_um": own["z1_um"],
                    "conductivity_S_per_m": round(1.0 / RHO_OHM_M, 1),
                }
            ],
        }
        spec_path = os.path.join(tmp, "spec.json")
        with open(spec_path, "w") as handle:
            json.dump(spec, handle)
        response = run_mom(gds, spec_path)
    r = response["resistance_ohm"][0]
    analytic = (
        RHO_OHM_M * length_um * 1e-6 / (own["width_um"] * own["thickness_um"] * 1e-12)
    )
    return {"resistance_ohm": r, "analytic_rho_l_over_wt_ohm": round(analytic, 4)}


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #


def run_point(
    level: int,
    pitch_mult: int,
    density: str,
    length_um: float,
    out_dir: str,
    panel_divisors: tuple[float, ...] = (1.0, 2.0),
) -> dict:
    geometry = point_geometry(level, pitch_mult, density, length_um)
    point_id = f"M{level}-p{pitch_mult}x-{density}-L{length_um:g}"
    gds = os.path.join(out_dir, f"{point_id}.gds")
    write_gds(geometry["shapes"], gds)
    victim = geometry["victim"]
    with tempfile.TemporaryDirectory() as tmp:
        deck = measure_deck(gds, victim, os.path.join(tmp, "p.spice"))
    min_gap = geometry["pitch_um"] - STACK[level]["width_um"]
    mom = measure_mom(geometry, [round(min_gap / d, 6) for d in panel_divisors])
    res = measure_mom_resistance(level, length_um)
    c = mom["converged"]
    p = deck["parasitics"]
    critical = deck["critical_net"]
    r_per_um = lambda ohm: round(ohm / length_um, 4)  # noqa: E731
    return {
        "id": point_id,
        "level": f"M{level}",
        "pitch_um": geometry["pitch_um"],
        "pitch_mult": pitch_mult,
        "density": density,
        "length_um": length_um,
        "victim": victim,
        "gds": os.path.relpath(gds, HERE),
        "gds_sha256": _sha256(gds),
        "reference_klt_mom": {
            "r_ohm_per_um": r_per_um(res["resistance_ohm"]),
            "r_analytic_ohm_per_um": r_per_um(res["analytic_rho_l_over_wt_ohm"]),
            "c_ground_ff": c["c_ground_ff"],
            "c_same_level_ff": c["c_same_level_ff"],
            "c_adjacent_levels_ff": c["c_adjacent_levels_ff"],
            "coupling_each_ff": c["coupling_each_ff"],
            "panel_size_um": c["panel_size_um"],
            "panel_count": c["panel_count"],
            "mesh_refinement": mom["mesh_refinement"],
        },
        "a_parasitics": {
            "r_ohm_per_um": r_per_um(p["resistance_ohm"]),
            "c_ground_ff": p["c_ground_ff"],
            "c_same_level_ff": p["c_same_level_ff"],
            "c_adjacent_levels_ff": p["c_adjacent_levels_ff"],
            "coupled_ff": p["coupled_ff"],
            "rel_err_pct": {
                "r": _rel_err(
                    r_per_um(p["resistance_ohm"]), r_per_um(res["resistance_ohm"])
                ),
                "c_ground": _rel_err(p["c_ground_ff"], c["c_ground_ff"]),
                "c_same_level": _rel_err(p["c_same_level_ff"], c["c_same_level_ff"]),
                "c_adjacent_levels": _rel_err(
                    p["c_adjacent_levels_ff"], c["c_adjacent_levels_ff"]
                ),
            },
        },
        "b_critical_net": {
            "status": critical["status"],
            "reason": critical.get("reason"),
            "r_ohm_per_um": "unsupported",
            "c_ground_ff": "unsupported",
            "c_same_level_ff": "unsupported",
            "c_adjacent_levels_ff": "unsupported",
            "rel_err_pct": "unsupported",
        },
        "_tools": {
            k: deck[k] for k in ("tool_version", "klayout_version", "deck_content_hash")
        },
    }


IDEALISATIONS = [
    "reference is 3-D rectangular full-thickness boxes (no trapezoid, liner, "
    "roughness); one homogeneous dielectric (eps_r 3.9, ASSUMED)",
    "ground is a finite zero-thickness plate one M1 thickness below M1 "
    "(ASSUMED), not the substrate; vias not drawn (orthogonal lines are nets)",
    "M2+ thickness = 2x min width and ILD height = lower thickness are ASSUMED "
    "(not in the pinned archive); M1 thickness and rho are cited",
    "quasi-static C; R is DC of one isolated bar at the effective rho",
]


def sweep(
    levels=(2, 4),
    pitch_mults=(1, 2, 4),
    densities=("dense", "sparse"),
    length_um: float = LENGTH_UM,
    out_dir: str | None = None,
) -> dict:
    out_dir = out_dir or os.path.join(HERE, "points")
    os.makedirs(out_dir, exist_ok=True)
    points = [
        run_point(level, mult, density, length_um, out_dir)
        for level in levels
        for mult in pitch_mults
        for density in densities
    ]
    tools = points[0].pop("_tools")
    for point in points[1:]:
        point.pop("_tools")
    deck = get_parasitics_deck("asap7")
    return {
        "schema": "asap7-parallel-line-fidelity/1",
        "issue": "2AMLogic/klayout-tools#5201 (phase 1 of #5199)",
        "tools": {
            **tools,
            "script_sha256": _sha256(os.path.abspath(__file__)),
        },
        "inputs": {
            "sources": source_provenance(),
            "stackup": stackup_spec(),
            "effective_resistivity_ohm_m": RHO_OHM_M,
            "deck_has_metal_sidewalls": bool(deck.metal_sidewalls),
        },
        "idealisations": IDEALISATIONS,
        "points": points,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--out-dir", default=HERE)
    args = parser.parse_args(argv)
    result = sweep(out_dir=os.path.join(args.out_dir, "points"))
    with open(os.path.join(args.out_dir, "asap7-stackup.json"), "w") as handle:
        json.dump(stackup_spec(), handle, indent=2)
        handle.write("\n")
    with open(os.path.join(args.out_dir, "results.json"), "w") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print(f"wrote {len(result['points'])} points to {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
