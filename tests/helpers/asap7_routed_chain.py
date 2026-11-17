"""Routed multi-type ASAP7 block fixture for the #5630 no-repro record.

Abuts ``n`` cells of 44 pinned types in one row and routes a chained net
list (M2 legs + M3 tracks over V1/V2, stitched rails) whose reference CDL
wires exactly the same nets, then writes an `klt lvs` request exercising
the #4610 three-option recipe (``finfet_bulk`` + ``flatten_layout`` +
``flatten_reference``). Requires the pinned lambdapdk corpus under
``pdks/lambdapdk`` (``scripts/fetch-pdks.sh``).

Test-scoped, not shipped (see ``tests/helpers/__init__.py``).
"""

from __future__ import annotations

import bisect
import json
import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent.parent
RVT = REPOSITORY_ROOT / "pdks/lambdapdk/lambdapdk/asap7/libs/asap7sc7p5t_rvt"
GDS = RVT / "gds" / "asap7sc7p5t_28_R.gds.gz"
LEF = RVT / "lef" / "asap7sc7p5t_28_R.lef"
CDL = RVT / "netlist" / "asap7sc7p5t_28_R.cdl"

TRACK_PITCH = 80
LEG_W = 32
VIA_W = 36
TRACK_H = 32
PIN_OFFSET = 24
RAILS = {"VDD", "VSS"}

#: Candidate indices (1-based, into the sorted ``*_ASAP7_75t_R`` cells with
#: LEF size + CDL subckt + labelled INPUT/OUTPUT pins) of 44 cell types
#: whose routed chain LVS-matches standalone -- measured against the pinned
#: corpus. The chain below must keep reaching ``match`` for the recipe to
#: be exercised in its convergent (non-crashing) regime.
GOOD_TYPES = [
    1,
    3,
    6,
    7,
    8,
    9,
    10,
    11,
    12,
    13,
    14,
    15,
    16,
    17,
    18,
    19,
    20,
    21,
    22,
    23,
    24,
    25,
    26,
    27,
    28,
    29,
    30,
    32,
    35,
    36,
    37,
    38,
    39,
    41,
    43,
    44,
    45,
    47,
    48,
    49,
    50,
    52,
    53,
    54,
]


def _library_tables():
    lef_text = LEF.read_text()
    sizes: dict[str, tuple[float, float]] = {}
    directions: dict[str, dict[str, str]] = {}
    for macro in re.finditer(r"MACRO (\S+)(.*?)END \1", lef_text, re.S):
        size = re.search(r"SIZE\s+([\d.]+)\s+BY\s+([\d.]+)", macro.group(2))
        if size:
            sizes[macro.group(1)] = (float(size.group(1)), float(size.group(2)))
        pins = {}
        for pin in re.finditer(r"PIN (\S+)(.*?)END \1", macro.group(2), re.S):
            direction = re.search(r"DIRECTION (\w+)", pin.group(2))
            if direction:
                pins[pin.group(1)] = direction.group(1)
        directions[macro.group(1)] = pins
    subckts: dict[str, tuple[str, list[str]]] = {}
    cdl_text = CDL.read_text()
    for block in re.finditer(
        r"^\.SUBCKT\s+(\S+)\s*(.*?)^\.ENDS.*?$", cdl_text, re.S | re.M
    ):
        pins = block.group(0).splitlines()[0].split()[2:]
        subckts[block.group(1).upper()] = (block.group(0), pins)
    return sizes, directions, subckts


def _candidate_cell(cell, pin_text_layer, sizes, directions, subckts):
    """A routable 44-type candidate as ``(name, points, ins, outs)``, or None."""
    name = cell.name
    if not name.endswith("_ASAP7_75t_R") or name not in sizes:
        return None
    if name.upper() not in subckts:
        return None
    pin_dirs = directions.get(name, {})
    points = {
        shape.text.string: (shape.text.trans.disp.x, shape.text.trans.disp.y)
        for shape in cell.shapes(pin_text_layer).each()
    }
    ins = sorted(
        p for p in points if p not in RAILS and pin_dirs.get(p, "").startswith("INPUT")
    )
    outs = sorted(
        p for p in points if p not in RAILS and pin_dirs.get(p, "").startswith("OUTPUT")
    )
    if ins and outs:
        return (name, points, ins, outs)
    return None


def _routing_candidates(layout, pin_text_layer, sizes, directions, subckts):
    candidates = []
    for cell in sorted(layout.each_cell(), key=lambda c: c.name):
        picked = _candidate_cell(cell, pin_text_layer, sizes, directions, subckts)
        if picked is not None:
            candidates.append(picked)
    return candidates


def _place_chain(layout, chosen, n, sizes):
    """Abut ``n`` cells of ``chosen`` in one row."""
    import klayout.db as kdb

    top = layout.create_cell("TOP")
    placements = []
    x = 0
    for i in range(n):
        name, points, ins, outs = chosen[i % len(chosen)]
        top.insert(kdb.CellInstArray(layout.cell(name).cell_index(), kdb.Trans(x, 0)))
        placements.append((x, name, points, ins, outs))
        x += int(sizes[name][0] * 4000)
    return top, placements, x


def _chain_nets(placements, n):
    """Chained net list: output pin of cell ``i`` to every input pin of ``i+1``."""
    return [
        [(i, placements[i][4][0])] + [(i + 1, p) for p in placements[i + 1][3]]
        for i in range(n - 1)
    ]


def _net_endpoints(nets, placements):
    """Per-net routed pin endpoints plus every leg x, sorted."""
    legs_sorted: list[int] = []
    endpoints_per_net: list[list[tuple[int, int]]] = []
    for i, net in enumerate(nets):
        offset = PIN_OFFSET if i % 2 == 0 else -PIN_OFFSET
        endpoints = []
        for cell_index, pin in net:
            pin_x, pin_y = placements[cell_index][2][pin]
            endpoints.append((placements[cell_index][0] + pin_x + offset, pin_y))
        endpoints_per_net.append(endpoints)
        legs_sorted.extend(x_leg for x_leg, _ in endpoints)
    return legs_sorted, endpoints_per_net


def _place_via(top, layer, x, y):
    import klayout.db as kdb

    top.shapes(layer).insert(
        kdb.Box(x - VIA_W // 2, y - VIA_W // 2, x + VIA_W // 2, y + VIA_W // 2)
    )


def _draw_leg(top, l_m2, l_v1, l_v2, leg_x, pin_y, y_track):
    import klayout.db as kdb

    top.shapes(l_m2).insert(
        kdb.Box(leg_x - LEG_W // 2, pin_y, leg_x + LEG_W // 2, y_track)
    )
    for via_layer, via_y in ((l_v1, pin_y), (l_v2, y_track)):
        _place_via(top, via_layer, leg_x, via_y)


def _draw_track(top, l_m3, endpoints, y_track):
    import klayout.db as kdb

    xs = [leg_x for leg_x, _ in endpoints]
    top.shapes(l_m3).insert(
        kdb.Box(
            min(xs) - LEG_W,
            y_track - TRACK_H // 2,
            max(xs) + LEG_W,
            y_track + TRACK_H // 2,
        )
    )


def _route_signal_nets(top, layers, nets, placements, y0):
    """Draw each net's M2 legs + V1/V2 vias and its M3 track; return the
    sorted leg-x list for the rail-stub clearance check."""
    l_m2, l_m3, l_v1, l_v2 = layers
    legs_sorted, endpoints_per_net = _net_endpoints(nets, placements)
    for i, endpoints in enumerate(endpoints_per_net):
        y_track = y0 + i * TRACK_PITCH
        for leg_x, pin_y in endpoints:
            _draw_leg(top, l_m2, l_v1, l_v2, leg_x, pin_y, y_track)
        _draw_track(top, l_m3, endpoints, y_track)
    return legs_sorted


def _clear_x(legs_sorted: list[int], choices: list[int]) -> int:
    best, best_gap = choices[0], -1.0
    for candidate_x in choices:
        position = bisect.bisect_left(legs_sorted, candidate_x)
        near = [
            legs_sorted[k]
            for k in (position - 1, position)
            if 0 <= k < len(legs_sorted)
        ]
        gap = min((abs(candidate_x - q) for q in near), default=10**9)
        if gap > best_gap:
            best, best_gap = candidate_x, gap
    assert best_gap >= 60, "rail stub would collide with a signal leg"
    return best


def _place_rail_taps(top, l_v1, placements, sizes, legs_sorted):
    """Drop a V1 via on each rail pin of every instance; return the rail
    pin-y lists."""
    rail_y: dict[str, list[int]] = {"VSS": [], "VDD": []}
    for origin, name, rail_points, _, _ in placements:
        width = int(sizes[name][0] * 4000)
        stub_x = _clear_x(legs_sorted, [origin + 16, origin + width - 16])
        for rail in RAILS:
            pin_y = rail_points[rail][1]
            rail_y[rail].append(pin_y)
            _place_via(top, l_v1, stub_x, pin_y)
    return rail_y


def _draw_ground_rail(top, l_m2, rail_y, row_width):
    import klayout.db as kdb

    vss_bottom = min(rail_y["VSS"]) - 24
    vss_top = max(rail_y["VSS"]) + 24
    top.shapes(l_m2).insert(kdb.Box(0, vss_bottom, row_width, vss_top))


def _draw_power_rail(
    top, l_m2, l_v2, rail_y, placements, sizes, legs_sorted, y0, n_nets, row_width
):
    """Stub every VDD pin up to one M2 rail above the top track and strap it."""
    import klayout.db as kdb

    y_vdd = y0 + max(n_nets - 1, 0) * TRACK_PITCH + 300
    for (origin, name, _points, _, _), pin_y in zip(
        placements, rail_y["VDD"], strict=True
    ):
        width = int(sizes[name][0] * 4000)
        stub_x = _clear_x(legs_sorted, [origin + 16, origin + width - 16])
        top.shapes(l_m2).insert(
            kdb.Box(stub_x - LEG_W // 2, pin_y, stub_x + LEG_W // 2, y_vdd)
        )
        _place_via(top, l_v2, stub_x, y_vdd)
    top.shapes(l_m2).insert(
        kdb.Box(0, y_vdd - TRACK_H // 2, row_width, y_vdd + TRACK_H // 2)
    )
    return y_vdd


def _instance_args(pins, nets_here, i):
    args = []
    for pin in pins:
        if pin.upper() in RAILS:
            args.append(pin.upper())
        elif pin in nets_here:
            args.append(nets_here[pin])
        else:
            args.append(f"x{i}_{pin}")
    return args


def _cell_net_assignment(nets):
    cell_net: dict[int, dict[str, str]] = {}
    for i, net in enumerate(nets):
        for cell_index, pin in net:
            cell_net.setdefault(cell_index, {})[pin] = f"net{i}"
    return cell_net


def _write_reference_cdl(out_dir, subckts, placements, nets):
    definitions: list[str] = []
    pins_by_cell: dict[str, list[str]] = {}
    for _, name, _, _, _ in placements:
        if name in pins_by_cell:
            continue
        body, pins = subckts[name.upper()]
        pins_by_cell[name] = pins
        definitions.append(body)
    cell_net = _cell_net_assignment(nets)
    top_lines = [".SUBCKT TOP"]
    for i, (_, name, _, _, _) in enumerate(placements):
        args = _instance_args(pins_by_cell[name], cell_net.get(i, {}), i)
        top_lines.append(f"X{i} {' '.join(args)} {name}")
    top_lines.append(".ENDS")
    cdl_path = out_dir / "routed_chain.cdl"
    cdl_path.write_text("\n".join(definitions + top_lines) + "\n")
    return cdl_path


def _write_request(out_dir, gds_path, cdl_path):
    request_path = out_dir / "routed_chain.json"
    request_path.write_text(
        json.dumps(
            {
                "layout": {"file": str(gds_path), "deck": "asap7", "top": "TOP"},
                "reference": {"netlist": str(cdl_path), "top": "TOP"},
                "options": {
                    "finfet_bulk": {"nmos": "VSS", "pmos": "VDD"},
                    "flatten_layout": True,
                    "flatten_reference": True,
                },
            }
        )
    )
    return request_path


def build_routed_chain(out_dir: Path, n: int) -> Path:
    """Build the routed chain (see module docstring); return the request path."""
    import klayout.db as kdb

    sizes, directions, subckts = _library_tables()
    layout = kdb.Layout()
    layout.read(str(GDS))
    pin_text_layer = layout.layer(19, 251)
    l_m2, l_v1 = layout.layer(20, 0), layout.layer(21, 0)
    l_m3, l_v2 = layout.layer(30, 0), layout.layer(25, 0)

    candidates = _routing_candidates(layout, pin_text_layer, sizes, directions, subckts)
    chosen = [candidates[index - 1] for index in GOOD_TYPES]

    top, placements, row_width = _place_chain(layout, chosen, n, sizes)
    nets = _chain_nets(placements, n)

    row_top = max(layout.cell(name).bbox().top for _, name, _, _, _ in placements)
    y0 = row_top + 400
    legs_sorted = _route_signal_nets(
        top, (l_m2, l_m3, l_v1, l_v2), nets, placements, y0
    )
    rail_y = _place_rail_taps(top, l_v1, placements, sizes, legs_sorted)
    _draw_ground_rail(top, l_m2, rail_y, row_width)
    _draw_power_rail(
        top,
        l_m2,
        l_v2,
        rail_y,
        placements,
        sizes,
        legs_sorted,
        y0,
        len(nets),
        row_width,
    )

    gds_path = out_dir / "routed_chain.gds"
    layout.write(str(gds_path))

    cdl_path = _write_reference_cdl(out_dir, subckts, placements, nets)
    return _write_request(out_dir, gds_path, cdl_path)
