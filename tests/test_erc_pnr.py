"""Exercise generation, validation and physical ERC using the same saved spec."""

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.place_and_route_power_pins import rail_label_evidence


def _report():
    return {
        "status": "ok",
        "stage_reached": "route",
        "macros": [],
        "provenance": {
            "pdk": {"name": "sky130A"},
            "deck": {"name": "sky130_fd_sc_hd__tt_025C_1v80"},
        },
        "layer_map": {"resolution": "exact", "path": "$PDK_ROOT/sky130A.map"},
        "power": {
            "pdn": True,
            "global_connect": True,
            "power_net": "VPWR",
            "ground_net": "VGND",
            "tapcell_master": "sky130_fd_sc_hd__tapvpwrvgnd_1",
            "straps": [{"layer": "met1"}, {"layer": "met4"}],
        },
    }


def _generate(tmp_path, capsys, report):
    run = tmp_path / "run.json"
    run.write_text(json.dumps(report))
    assert main(["erc", "--pnr-response", str(run), "--format", "json"]) == 0
    return capsys.readouterr().out


@pytest.mark.parametrize("renamed", [False, True])
def test_generate_validate_and_check_physical_tap(tmp_path, capsys, renamed):
    report = _report()
    power = report["power"]
    if renamed:
        power.update(power_net="VDD", ground_net="VSS")
        report["layer_map"] = {"resolution": "family", "path": "sky130.map"}
        power.update(rail_label_evidence(power, "sky130_fd_sc_hd", None))
    result = _generate(tmp_path, capsys, report)
    assert result == _generate(tmp_path, capsys, report)
    spec = json.loads(result)
    levels = spec["stackup"]
    assert [role["name"] for role in levels] == [
        "poly",
        "li1",
        "met1",
        "met2",
        "met3",
        "met4",
        "met5",
    ]
    assert [(role["layer"], role["label_layer"]) for role in levels] == [
        (f"{n}/20", f"{n}/5") for n in range(66, 73)
    ]
    assert [(v["layer"], v["between"]) for v in spec["vias"]] == [
        (f"{66 + i}/44", [lower["name"], upper["name"]])
        for i, (lower, upper) in enumerate(zip(levels[:-1], levels[1:], strict=True))
    ]
    nets = {entry["name"]: entry.get("same_net_as") for entry in spec["nets"]}
    expected = {"VPWR": None, "VGND": None}
    if renamed:
        expected = {"VPWR": "VDD", "VGND": "VSS", "VDD": None, "VSS": None}
    assert nets == expected
    assert len(nets) == len(spec["nets"])
    assert all("roles" not in entry for entry in spec["nets"])
    spec_path = tmp_path / "supply.json"
    spec_path.write_text(result)
    assert main(["erc", "--validate-spec", str(spec_path), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "valid"

    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")

    def box(layer, datatype, left, right):
        return top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Box(left, 0, right, 1000)
        )

    box(64, 20, 0, 1000)
    tap = box(65, 44, 100, 900)
    tap_contact = box(66, 44, 350, 650)
    for start, native, configured in (
        (0, "VPWR", power["power_net"]),
        (5000, "VGND", power["ground_net"]),
    ):
        box(67, 20, start, start + 1000)
        for i, name in enumerate(sorted({native, configured})):
            top.shapes(layout.layer(67, 5)).insert(
                kdb.Text(name, start + 400 + i * 100, 500)
            )
    box(65, 20, 2000, 3000)
    box(66, 20, 2100, 2400)
    box(66, 44, 2150, 2200)
    box(67, 20, 2100, 2400)
    # An unlabelled signal on a strap layer is legitimate.
    box(68, 20, 7000, 8000)
    gds = tmp_path / "test.gds"
    for remove in (None, tap_contact, tap):
        if remove is not None:
            remove.delete()
        layout.write(str(gds))
        assert main(
            ["erc", str(gds), str(spec_path), "--findings-only", "--format", "json"]
        ) == (4 if remove is None else 3)
        run = json.loads(capsys.readouterr().out)
        assert 'erc.missing_tie:["nwell_tap"]' in run["erc_coverage"]["checked"]
        rules = [f["rule"] for f in run["erc_findings"]]
        assert rules.count("erc.missing_tie") == int(remove is not None)
        assert "erc.unlabelled_conductor" not in rules
        assert "erc.unconnected_net" not in rules
        if remove is tap_contact:
            box(66, 44, 350, 650)


@pytest.mark.parametrize(
    ("section", "key", "value", "diagnostic"),
    [
        ("power", "straps", [{"layer": "met6"}], "straps[0].layer"),
        ("power", "straps", [], "power.straps"),
        ("power", "straps", [None], "straps[0]"),
        ("power", "pdn", False, "power.pdn"),
        ("power", "global_connect", False, "global_connect"),
        ("power", "tapcell_master", None, "tapcell_master"),
        ("power", "power_net", "VDD", "library_rail_labels"),
        ("power", "power_net", "VGND", "collide"),
        ("power", "ground_net", ["VSS"], "ground_net"),
        ("power", "library_rail_labels", None, "library_rail_labels"),
        ("layer_map", "resolution", "none", "layer_map"),
        ("layer_map", "path", "gf180mcu.map", "layer_map"),
        ("root", "macros", [{"cell": "RAM"}], "macros"),
        ("root", "stage_reached", "floorplan", "stage_reached"),
        ("provenance", "pdk", {"name": "gf180mcuD"}, "provenance.pdk"),
        ("provenance", "deck", {"name": "sky130_fd_sc_hs__tt"}, "provenance.deck"),
    ],
)
def test_unsupported_response(tmp_path, capsys, section, key, value, diagnostic):
    report = _report()
    (report if section == "root" else report[section])[key] = value
    path = tmp_path / "run.json"
    path.write_text(json.dumps(report))
    assert main(["erc", "--pnr-response", str(path), "--format", "json"]) == 1
    result = capsys.readouterr()
    assert not result.out
    assert diagnostic in result.err


@pytest.mark.parametrize(
    ("pattern", "role", "expected"),
    [
        ("^VNB$", "ground", True),
        ("^V.*$", "ground", False),
        ("V", "ground", False),
        ("^VPWR$", "ground", False),
    ],
)
def test_only_proven_rail_labels_are_echoed(pattern, role, expected):
    power = {"power_net": "VCCA", "ground_net": "VSSA"}
    pins = [("power", "^VPWR$", True), ("ground", "VGND", True), (role, pattern, False)]
    result = rail_label_evidence(power, "sky130_fd_sc_hd", {"power_pins": pins})
    assert result["library_rail_labels"] == (
        {"VPWR": "VCCA", "VGND": "VSSA"} if expected else None
    )
    assert rail_label_evidence(power, "custom", None) == {}


def test_generation_mode_errors_and_formats(tmp_path, capsys):
    path = tmp_path / "route.json"
    args = ["erc", "--pnr-response", str(path)]
    for content in (None, b"\xff", b"[", b"[]"):
        if content is not None:
            path.write_bytes(content)
        assert main([*args, "--format", "json"]) == 1
        assert json.loads(capsys.readouterr().err)["error"]["command"] == "erc"
    report = _report()
    report["provenance"]["pdk"]["name"] = "sky130B"
    report["layer_map"]["path"] = "sky130B.map"
    path.write_text(json.dumps(report))
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["ties"][0]["net"] == "VPWR"
    assert main([*args, "--top", "TOP"]) == 1
    assert "--pnr-response" in capsys.readouterr().err
    for conflict in (
        ["layout.gds"],
        ["--check", "report"],
        ["--validate-spec", "spec"],
    ):
        with pytest.raises(SystemExit) as error:
            main([*args, *conflict])
        assert error.value.code == 2
