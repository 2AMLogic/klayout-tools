"""Native-crash stage diagnostics for ``klt lvs`` (issue #5630).

Issue #5630 reports a deterministic SIGSEGV (exit 139, empty stdout and
stderr) on a routed ~1070-instance ASAP7 block under the documented
"Routed multi-cell blocks" recipe (``finfet_bulk`` + ``flatten_layout`` +
``flatten_reference``), where each half of the recipe completes. A native
crash cannot be caught in-process, so
:mod:`klayout_tools.lvs_crash_diagnostics` makes faulthandler dump the
Python traceback (naming the ``klt_lvs_stage_*`` wrappers) to stderr, and
these tests pin:

* the stage wrapper really names its stage in a crash dump (a subprocess
  kills itself with SIGSEGV inside a stage, like the reported crash);
* ``klt lvs`` enables the diagnostics before running;
* the three-option recipe itself completes to a verdict on a routed
  multi-type synthetic block at the issue's scale -- the no-repro record:
  44 cell types x 1056 instances built from the pinned corpus. It never
  crashed here (``match``), which is why the fix is diagnostics, not a
  located call. See ``docs/cli/lvs.md``'s #5630 note.
"""

from __future__ import annotations

import bisect
import json
import re
import signal
import subprocess
import sys
import traceback
from argparse import Namespace
from pathlib import Path

import pytest

from klayout_tools.lvs_crash_diagnostics import current_stage, stage

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_RVT = _REPOSITORY_ROOT / "pdks/lambdapdk/lambdapdk/asap7/libs/asap7sc7p5t_rvt"
_GDS = _RVT / "gds" / "asap7sc7p5t_28_R.gds.gz"
_LEF = _RVT / "lef" / "asap7sc7p5t_28_R.lef"
_CDL = _RVT / "netlist" / "asap7sc7p5t_28_R.cdl"

_corpus = pytest.mark.skipif(
    not _GDS.is_file(),
    reason="pinned lambdapdk ASAP7 libraries not found under pdks/lambdapdk "
    "-- run scripts/fetch-pdks.sh to enable this test",
)


def test_stage_tracks_current_and_restores():
    @stage("outer")
    def outer():
        assert current_stage() == "outer"

        @stage("inner")
        def inner():
            assert current_stage() == "inner"

        inner()
        assert current_stage() == "outer"
        with pytest.raises(ValueError):
            inner_raise()
        assert current_stage() == "outer"

    @stage("inner")
    def inner_raise():
        raise ValueError("boom")

    assert current_stage() is None
    outer()
    assert current_stage() is None


def test_stage_names_its_frames_in_traceback():
    @stage("flatten_layout_combine")
    def explodes():
        raise RuntimeError("native-crash stand-in")

    with pytest.raises(RuntimeError) as caught:
        explodes()
    text = "\n".join(traceback.format_exception(caught.value))
    assert "klt_lvs_stage_flatten_layout_combine" in text


def test_native_crash_dumps_stage_named_traceback(tmp_path):
    """A real SIGSEGV (the reported failure mode) must leave a stderr
    traceback naming the stage -- not two empty streams."""
    script = tmp_path / "crash.py"
    script.write_text(
        "import os, signal, sys\n"
        f"sys.path.insert(0, {str(_REPOSITORY_ROOT / 'src')!r})\n"
        "from klayout_tools.lvs_crash_diagnostics import enable, stage\n"
        "assert enable()\n"
        "\n"
        "@stage('finfet_bulk_join')\n"
        "def native_call():\n"
        "    os.kill(os.getpid(), signal.SIGSEGV)\n"
        "\n"
        "native_call()\n"
    )
    proc = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == -signal.SIGSEGV
    assert proc.stdout == ""
    assert "Segmentation fault" in proc.stderr
    assert "klt_lvs_stage_finfet_bulk_join" in proc.stderr


def test_lvs_cmd_enables_crash_diagnostics(monkeypatch, capsys):
    from klayout_tools.cli import lvs_cmd

    calls: list[bool] = []
    monkeypatch.setattr(lvs_cmd, "enable_crash_diagnostics", lambda: calls.append(True))

    def refuse(_request, **_kwargs):
        raise lvs_cmd.LvsError("request refused (test)")

    monkeypatch.setattr(lvs_cmd, "run_lvs", refuse)
    exit_code = lvs_cmd.run(
        Namespace(
            request="irrelevant",
            check=None,
            rerun=False,
            layout=None,
            deck_file=None,
            deck_var=None,
            progress=None,
            format="text",
            output=None,
        )
    )
    assert exit_code == 1
    assert calls == [True]


# --------------------------------------------------------------------------- #
# Routed multi-type synthetic block (the #5630 no-repro record fixture)
# --------------------------------------------------------------------------- #

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
_GOOD_TYPES = [
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
    lef_text = _LEF.read_text()
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
    cdl_text = _CDL.read_text()
    for block in re.finditer(
        r"^\.SUBCKT\s+(\S+)\s*(.*?)^\.ENDS.*?$", cdl_text, re.S | re.M
    ):
        pins = block.group(0).splitlines()[0].split()[2:]
        subckts[block.group(1).upper()] = (block.group(0), pins)
    return sizes, directions, subckts


def _build_routed_chain(out_dir: Path, n: int) -> Path:
    """Abut ``n`` cells of 44 pinned types in one row and route a chained
    net list (M2 legs + M3 tracks over V1/V2, stitched rails) whose
    reference CDL wires exactly the same nets. Returns the request path."""
    import klayout.db as kdb

    sizes, directions, subckts = _library_tables()
    layout = kdb.Layout()
    layout.read(str(_GDS))
    pin_text_layer = layout.layer(19, 251)
    l_m2, l_v1 = layout.layer(20, 0), layout.layer(21, 0)
    l_m3, l_v2 = layout.layer(30, 0), layout.layer(25, 0)

    candidates = []
    for cell in sorted(layout.each_cell(), key=lambda c: c.name):
        name = cell.name
        if not name.endswith("_ASAP7_75t_R") or name not in sizes:
            continue
        if name.upper() not in subckts:
            continue
        pin_dirs = directions.get(name, {})
        points = {
            shape.text.string: (shape.text.trans.disp.x, shape.text.trans.disp.y)
            for shape in cell.shapes(pin_text_layer).each()
        }
        ins = sorted(
            p
            for p in points
            if p not in RAILS and pin_dirs.get(p, "").startswith("INPUT")
        )
        outs = sorted(
            p
            for p in points
            if p not in RAILS and pin_dirs.get(p, "").startswith("OUTPUT")
        )
        if ins and outs:
            candidates.append((name, points, ins, outs))
    chosen = [candidates[index - 1] for index in _GOOD_TYPES]

    top = layout.create_cell("TOP")
    placements = []
    x = 0
    for i in range(n):
        name, points, ins, outs = chosen[i % len(chosen)]
        top.insert(kdb.CellInstArray(layout.cell(name).cell_index(), kdb.Trans(x, 0)))
        placements.append((x, name, points, ins, outs))
        x += int(sizes[name][0] * 4000)
    row_width = x

    nets = [
        [(i, placements[i][4][0])] + [(i + 1, p) for p in placements[i + 1][3]]
        for i in range(n - 1)
    ]

    row_top = max(layout.cell(name).bbox().top for _, name, _, _, _ in placements)
    y0 = row_top + 400
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

    for i, endpoints in enumerate(endpoints_per_net):
        y_track = y0 + i * TRACK_PITCH
        for leg_x, pin_y in endpoints:
            top.shapes(l_m2).insert(
                kdb.Box(leg_x - LEG_W // 2, pin_y, leg_x + LEG_W // 2, y_track)
            )
            for via_layer, via_y in ((l_v1, pin_y), (l_v2, y_track)):
                top.shapes(via_layer).insert(
                    kdb.Box(
                        leg_x - VIA_W // 2,
                        via_y - VIA_W // 2,
                        leg_x + VIA_W // 2,
                        via_y + VIA_W // 2,
                    )
                )
        xs = [leg_x for leg_x, _ in endpoints]
        top.shapes(l_m3).insert(
            kdb.Box(
                min(xs) - LEG_W,
                y_track - TRACK_H // 2,
                max(xs) + LEG_W,
                y_track + TRACK_H // 2,
            )
        )

    def _clear_x(choices: list[int]) -> int:
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

    rail_y: dict[str, list[int]] = {"VSS": [], "VDD": []}
    for origin, name, rail_points, _, _ in placements:
        width = int(sizes[name][0] * 4000)
        stub_x = _clear_x([origin + 16, origin + width - 16])
        for rail in RAILS:
            pin_y = rail_points[rail][1]
            rail_y[rail].append(pin_y)
            top.shapes(l_v1).insert(
                kdb.Box(
                    stub_x - VIA_W // 2,
                    pin_y - VIA_W // 2,
                    stub_x + VIA_W // 2,
                    pin_y + VIA_W // 2,
                )
            )
    vss_bottom = min(rail_y["VSS"]) - 24
    vss_top = max(rail_y["VSS"]) + 24
    top.shapes(l_m2).insert(kdb.Box(0, vss_bottom, row_width, vss_top))
    y_vdd = y0 + max(len(nets) - 1, 0) * TRACK_PITCH + 300
    for (origin, name, _points, _, _), pin_y in zip(
        placements, rail_y["VDD"], strict=True
    ):
        width = int(sizes[name][0] * 4000)
        stub_x = _clear_x([origin + 16, origin + width - 16])
        top.shapes(l_m2).insert(
            kdb.Box(stub_x - LEG_W // 2, pin_y, stub_x + LEG_W // 2, y_vdd)
        )
        top.shapes(l_v2).insert(
            kdb.Box(
                stub_x - VIA_W // 2,
                y_vdd - VIA_W // 2,
                stub_x + VIA_W // 2,
                y_vdd + VIA_W // 2,
            )
        )
    top.shapes(l_m2).insert(
        kdb.Box(0, y_vdd - TRACK_H // 2, row_width, y_vdd + TRACK_H // 2)
    )

    gds_path = out_dir / "routed_chain.gds"
    layout.write(str(gds_path))

    definitions: list[str] = []
    pins_by_cell: dict[str, list[str]] = {}
    for _, name, _, _, _ in placements:
        if name in pins_by_cell:
            continue
        body, pins = subckts[name.upper()]
        pins_by_cell[name] = pins
        definitions.append(body)
    cell_net: dict[int, dict[str, str]] = {}
    for i, net in enumerate(nets):
        for cell_index, pin in net:
            cell_net.setdefault(cell_index, {})[pin] = f"net{i}"
    top_lines = [".SUBCKT TOP"]
    for i, (_, name, _, _, _) in enumerate(placements):
        args = []
        for pin in pins_by_cell[name]:
            if pin.upper() in RAILS:
                args.append(pin.upper())
            elif pin in cell_net.get(i, {}):
                args.append(cell_net[i][pin])
            else:
                args.append(f"x{i}_{pin}")
        top_lines.append(f"X{i} {' '.join(args)} {name}")
    top_lines.append(".ENDS")
    cdl_path = out_dir / "routed_chain.cdl"
    cdl_path.write_text("\n".join(definitions + top_lines) + "\n")

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


@_corpus
def test_routed_multitype_block_recipe_reaches_verdict(tmp_path):
    """The #4610 three-option recipe on a routed 44-type, 1056-instance
    block (the issue's scale) must reach a verdict, never die silently.
    This is the no-repro record for #5630: measured ``match``."""
    from klayout_tools.lvs import run_lvs

    request_path = _build_routed_chain(tmp_path, n=1056)
    report = run_lvs(request_path.read_text())
    assert report["status"] == "match", report.get("category_counts")
    counts = report["category_counts"]
    assert counts["device.finfet_bulk_tied"] > 0
    assert counts["topology.flattened"] == 2
