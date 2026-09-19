"""Well continuity survives abstraction, while real power defects stay visible."""

import json
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.extract import run_extract
from klayout_tools.lvs import run_lvs


def _row_layout(*, broken_well=False, broken_metal=False, transform=None):
    layout = kdb.Layout()
    layout.dbu = 0.001
    cell = layout.create_cell("mylib__cell")
    top = layout.create_cell("top")

    def draw(target, layer, datatype, box):
        target.shapes(layout.layer(layer, datatype)).insert(kdb.Box(*box))

    def label(target, layer, datatype, text, x, y):
        target.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    # A real PMOS establishes a flat-extraction body control. The well
    # overlaps the next cell's well; no parent-level well bridges them.
    draw(cell, 64, 20, (-500, -500, 4500, 3000))
    draw(cell, 65, 20, (0, 0, 2000, 1000))
    draw(cell, 66, 20, (800, -200, 1200, 1200))
    for x, pin in [(200, "Y"), (1800, "A")]:
        draw(cell, 66, 44, (x - 100, 300, x + 100, 700))
        draw(cell, 67, 20, (x - 200, 200, x + 200, 800))
        label(cell, 67, 5, pin, x, 500)
    label(cell, 64, 5, "VPB", 3000, 500)
    draw(cell, 67, 20, (0, 2200, 4000, 2600))
    label(cell, 67, 5, "VPWR", 2000, 2400)

    pitch = 6000 if broken_well else 4000
    for index, x in enumerate([0, pitch]):
        top.insert(kdb.CellInstArray(cell.cell_index(), kdb.Trans(x, 0)))
        for local_x, pin in [(200, "y"), (1800, "a")]:
            draw(top, 67, 44, (x + local_x - 100, 300, x + local_x + 100, 700))
            draw(top, 68, 20, (x + local_x - 200, 200, x + local_x + 200, 800))
            label(top, 68, 5, f"{pin}{index}", x + local_x, 500)
    if broken_metal:
        # A real break in the cell's own rail, not merely removal of a
        # redundant parent wire. The continuous well must not repair metal.
        cell.shapes(layout.layer(67, 20)).clear()
        for x in [200, 1800]:
            draw(cell, 67, 20, (x - 200, 200, x + 200, 800))
        draw(cell, 67, 20, (0, 2200, 3500, 2600))
    else:
        draw(top, 67, 20, (0, 2200, pitch + 4000, 2600))

    # One genuine tap supplies the continuous well from the first rail.
    # The broken-well control puts the second PMOS on a different real tie.
    draw(top, 65, 44, (3000, 1000, 3600, 1600))
    draw(top, 66, 44, (3200, 1200, 3400, 1400))
    draw(top, 67, 20, (3100, 1100, 3500, 2600))
    if broken_well:
        draw(top, 65, 44, (pitch + 3000, 1000, pitch + 3600, 1600))
        draw(top, 66, 44, (pitch + 3200, 1200, pitch + 3400, 1400))
        draw(top, 67, 20, (pitch + 3100, 1100, pitch + 3500, 1500))
        label(top, 67, 5, "WRONG_BODY_TIE", pitch + 3300, 1300)
    if transform is not None:
        top.transform(transform)
    return layout


def _extract_row(tmp_path, *, abstract, **defects):
    path = tmp_path / "row.gds"
    _row_layout(**defects).write(str(path))
    return run_extract(
        str(path),
        "sky130",
        output=str(tmp_path / ("abstract.spice" if abstract else "flat.spice")),
        abstract_cell_patterns=("mylib__*",) if abstract else (),
    )


def _power_report(tmp_path, extraction, cell_name="mylib__cell"):
    root = tmp_path / "pdk"
    variant = root / "testpdk"
    (variant / "libs.tech").mkdir(parents=True, exist_ok=True)
    library = variant / "libs.ref" / "mylib"
    spice = library / "spice"
    spice.mkdir(parents=True, exist_ok=True)
    (spice / "mylib.spice").write_text(
        f".subckt {cell_name} A VGND VPB VPWR Y\n.ends\n"
    )
    # Authoritative roles also make this fixture compatible with #2076's
    # independently developed power-pin derivation fix.
    lef = library / "lef"
    lef.mkdir(exist_ok=True)
    (lef / "mylib.lef").write_text(
        f"MACRO {cell_name}\n"
        + "".join(
            f" PIN {pin}\n  USE {role} ;\n END {pin}\n"
            for pin, role in [
                ("A", "SIGNAL"),
                ("Y", "SIGNAL"),
                ("VGND", "GROUND"),
                ("VPB", "POWER"),
                ("VPWR", "POWER"),
            ]
        )
        + f"END {cell_name}\n"
    )
    reference = tmp_path / "reference.v"
    reference.write_text(
        "module top(a0,a1,y0,y1); input a0,a1; output y0,y1; "
        f"{cell_name} u0 (.A(a0),.Y(y0)); "
        f"{cell_name} u1 (.A(a1),.Y(y1)); endmodule\n"
    )
    request = {
        "layout": {"netlist": extraction["netlist_path"], "top": "top"},
        "reference": {
            "netlist": str(reference),
            "top": "top",
            "form": "gate-level-verilog",
            "library": "mylib",
            "pdk": "testpdk",
            "pdk_root": str(root),
        },
    }
    return run_lvs(json.dumps(request))


@pytest.mark.parametrize(
    "transform",
    [None, kdb.Trans(1, True, 2000, -3000)],
    ids=["ordinary-row", "rotated-mirrored-row"],
)
def test_abstracted_continuous_well_matches_flat_body_connectivity(tmp_path, transform):
    flat = _extract_row(tmp_path, abstract=False, transform=transform)
    assert flat["device_count"] == 2
    assert flat["unbiased_pmos_body_nets"] == []
    bodies = {device["nets"]["b"] for device in flat["devices"]}
    assert len(bodies) == 1 and "VPWR" in next(iter(bodies))
    abstract = _extract_row(tmp_path, abstract=True, transform=transform)
    assert abstract["device_count"] == 0
    result = _power_report(tmp_path, abstract)
    assert result["status"] == "match"
    assert result["power_connectivity"]["status"] == "match"
    assert result["power_connectivity"]["findings"] == []


@pytest.mark.parametrize(
    "defect,pin", [("broken_well", "VPB"), ("broken_metal", "VPWR")]
)
def test_abstraction_keeps_real_power_disconnects_visible(tmp_path, defect, pin):
    flat = _extract_row(tmp_path, abstract=False, **{defect: True})
    if defect == "broken_well":
        assert len({device["nets"]["b"] for device in flat["devices"]}) == 2
    abstract = _extract_row(tmp_path, abstract=True, **{defect: True})
    result = _power_report(tmp_path, abstract)
    assert result["status"] == "match"
    assert result["power_connectivity"]["status"] == "mismatch"
    assert any(
        finding["pin"] == pin and finding["rule"] == "power.inconsistent_pin_net"
        for finding in result["power_connectivity"]["findings"]
    )


def test_real_sky130_inverter_row_preserves_well_continuity(tmp_path):
    layout = kdb.Layout()
    layout.read(str(Path(__file__).parent / "corpus/sky130/sky130_fd_sc_hd__inv_1.gds"))
    cell = layout.top_cell()
    top = layout.create_cell("top")
    # The actual cell boundary gives the placement pitch, not its overhanging well bbox.
    pitch = cell.bbox(layout.layer(236, 0)).width()
    for index in range(2):
        offset = index * pitch
        top.insert(kdb.CellInstArray(cell.cell_index(), kdb.Trans(offset, 0)))
        for pin, x, y in [("a", 445, 1190), ("y", 905, 1190)]:
            top.shapes(layout.layer(67, 5)).insert(
                kdb.Text(f"{pin}{index}", kdb.Trans(offset + x, y))
            )
    # An unabstracted filler leaves a small well island under the first
    # body's port after logic-cell erasure. The far-end tap cannot reach it
    # without the actual logic wells, reproducing the reported mixed row.
    filler = layout.create_cell("FILLER_FLAT")
    filler.shapes(layout.layer(64, 20)).insert(kdb.Box(0, 2300, 400, 2900))
    top.insert(kdb.CellInstArray(filler.cell_index(), kdb.Trans()))
    # A parent-level tap on the far well overhang, joined to VPWR through
    # real licon/li1/mcon/met1. No synthetic parent well joins the row.
    for layer, datatype, box in [
        (65, 44, (40, 1400, 160, 1520)),
        (66, 44, (70, 1430, 130, 1490)),
        (67, 20, (40, 1400, 160, 2800)),
        (67, 44, (70, 2700, 130, 2760)),
        (68, 20, (-300, 2635, 160, 2805)),
    ]:
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Box(*box).transformed(kdb.Trans(2 * pitch, 0))
        )
    path = tmp_path / "real-row.gds"
    layout.write(str(path))
    flat = run_extract(str(path), "sky130", output=str(tmp_path / "flat.spice"))
    assert flat["device_count"] == 4
    assert flat["unbiased_pmos_body_nets"] == []
    pfet_bodies = {
        device["nets"]["b"] for device in flat["devices"] if device["class"] == "pfet"
    }
    assert len(pfet_bodies) == 1 and "VPWR" in next(iter(pfet_bodies))
    abstract = run_extract(
        str(path),
        "sky130",
        output=str(tmp_path / "abstract.spice"),
        abstract_cell_patterns=(cell.name,),
    )
    assert abstract["device_count"] == 0
    result = _power_report(tmp_path, abstract, cell_name=cell.name)
    assert result["status"] == "match"
    assert result["power_connectivity"]["status"] == "match"
    assert result["power_connectivity"]["findings"] == []
