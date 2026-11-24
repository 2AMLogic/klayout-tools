"""`klt extract` DEF SPEF spelling for escaped-bracket nets (issue #4997)."""

from __future__ import annotations

from klayout_tools.extract import run_extract
from test_extract import _make_inverter_layout, _write_gds
from test_post_route_sta import _LIVE_ONLY


def test_def_net_connections_hierarchical_escaped_net_keeps_def_spef_spelling(
    tmp_path,
):
    """Issue #4997: a generate-block net (escaped bracket + `.` + bare `/`)
    parsed from a real DEF `NETS` record and extracted end to end must keep
    the DEF's own SPEF spelling in `*D_NET`/`*CONN`/`*RES`, not the uniform
    escape fallback (`g_slice\\[0\\]_u_slice\\/_00_`) that read_spef cannot
    match (STA-1650)."""
    from klayout_tools.extract_spef import def_net_instance_pins

    def_path = tmp_path / "r.def"
    def_path.write_text(
        "NETS 1 ;\n"
        " - g_slice\\[0\\].u_slice/_00_ ( u1 VPB ) + ROUTED met1 ( 0 0 ) ( 1 1 ) ;\n"
        "END NETS\n"
    )
    path = _write_gds(
        _make_inverter_layout(substrate_tap_label="g_slice[0].u_slice/_00_"),
        tmp_path / "h.gds",
    )
    spef_path = tmp_path / "h.spef"
    run_extract(
        path,
        "sky130",
        output=str(tmp_path / "h.spice"),
        parasitics=True,
        spef_output=str(spef_path),
        declared_pins=frozenset({"A", "Y"}),
        def_net_connections=def_net_instance_pins(str(def_path)),
    )
    text = spef_path.read_text()
    spelled = "g_slice\\[0\\]\\.u_slice/_00_"
    assert f"*D_NET {spelled} " in text
    block = text.split(f"*D_NET {spelled} ", 1)[1].split("*D_NET", 1)[0]
    assert "*I u1:VPB B" in block
    assert f"{spelled}:1" in block
    assert "g_slice\\[0\\]_u_slice" not in text


def test_def_bus_bits_keep_literal_and_bus_brackets(tmp_path):
    from klayout_tools.extract_spef import def_net_instance_pins

    fixture = tmp_path / "bus.def"
    fixture.write_text(
        "NETS 1 ;\n"
        r" - u_cap/mem\[0\][3] ( clkbuf_0_u_out\/dly[10] X ) ;"
        "\n"
        "END NETS\n"
    )
    connections = def_net_instance_pins(str(fixture))
    net = next(iter(connections))
    assert net.spef == r"u_cap/mem\[0\][3]"
    assert connections[net][0][0].spef == r"clkbuf_0_u_out\/dly[10]"


@_LIVE_ONLY
def test_live_def_bus_bits_annotate_the_design(tmp_path):
    import json
    from pathlib import Path

    from klayout_tools.extract_spef import _write_spef, def_net_instance_pins
    from klayout_tools.post_route_sta import run_sta

    fixture = Path(__file__).parent / "fixtures" / "sta_electrical_checks"
    request = json.loads((fixture / "request.json").read_text())
    request["constraints"].update(propagated_clock=True, output_delay_ns=0.1)
    design = (fixture / "top.def").read_text()
    replacements = {
        "c3": r"clknet_0_u_out\/dly[10]",
        "b3": r"clkbuf_0_u_out\/dly[10]",
        "c2": r"u_cap/mem\[0\][3]",
    }
    for original, spelling in replacements.items():
        design = design.replace(f" {original} ", f" {spelling} ")
    def_path = tmp_path / "top.def"
    def_path.write_text(design)
    connections = def_net_instance_pins(str(def_path))
    nets = [
        {"net": net, "hub_net": net, "capacitance_ff": 200.0} for net in connections
    ]
    _write_spef(
        str(tmp_path / "top.spef"),
        design_name="top",
        klt_version=None,
        parasitics_report={"nets": nets},
        port_names=connections.port_pins,
        net_instance_pins=connections,
    )
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))
    result = run_sta(str(request_path), pdk_variant="sky130A")["spef_annotation"]
    assert result["reader_warning_count"] == 0, result["reader_warning_sample"]
    assert result["design_nets_annotated"] == result["design_nets_total"] == 10
    assert result["annotation_complete"], result["annotation_warning"]


def _extract_finfet_dotted_fixture(
    tmp_path, *, internal_label="sub.b[0]", collision=False
):
    """Routed names from the STA test design, on separate ASAP7 capacitor plates.

    Each net touches a device. Internal nets therefore survive pin demotion.
    The DEF supplies the actual digital pin names for the SPEF reader.
    """
    import json
    from pathlib import Path

    import klayout.db as kdb

    from klayout_tools.extract_spef import def_net_instance_pins
    from klayout_tools.place_and_route import def_pin_names

    fixture = Path(__file__).parent / "fixtures" / "sta_electrical_checks"
    design = (fixture / "top.def").read_text()
    design = design.replace(" q1 + NET q1 ", " out[1] + NET sub.sig ")
    design = design.replace(" q1 ( PIN q1 )", " sub.sig ( PIN out[1] )")
    design = design.replace(" c2 (", " sub.b[0] (")
    if collision:
        design = design.replace(" c1 (", " sub_sig (")
    def_path = tmp_path / "top.def"
    def_path.write_text(design)
    connections = def_net_instance_pins(str(def_path))

    layout = kdb.Layout()
    layout.dbu = 0.00025
    top = layout.create_cell("top")
    metal = top.shapes(layout.layer(19, 0))
    labels = top.shapes(layout.layer(19, 251))
    cap_marker = top.shapes(layout.layer(113, 0))
    for index, name in enumerate(connections):
        x = index % 2 * 260
        y = index // 2 * 2000
        plate = metal.insert(kdb.Box(x, y, x + 200, y + 200))
        label = internal_label if name == "sub.b[0]" else str(name)
        plate.set_property(1, label)
        labels.insert(kdb.Text(label, kdb.Trans(x + 100, y + 100)))
        if index % 2:
            cap_marker.insert(kdb.Box(0, y, 460, y + 200))
    gds = tmp_path / "routed.gds"
    layout.write(str(gds))
    spef = tmp_path / "top.spef"
    report = run_extract(
        str(gds),
        "asap7",
        output=str(tmp_path / "top.spice"),
        parasitics=True,
        def_net_names=True,
        def_net_connections=connections,
        declared_pins=def_pin_names(str(def_path)),
        spef_output=str(spef),
    )
    request = json.loads((fixture / "request.json").read_text())
    request["constraints"].update(propagated_clock=True, output_delay_ns=0.1)
    (tmp_path / "request.json").write_text(json.dumps(request))
    return report, spef.read_text()


def test_finfet_def_keeps_dotted_net_and_port_pin_spellings(tmp_path):
    report, spef = _extract_finfet_dotted_fixture(tmp_path)
    assert {row["net"] for row in report["parasitics"]["nets"]} >= {
        "sub_sig",
        "sub_b[0]",
    }
    assert "out[1] B" in spef.split("*PORTS\n")[1].split("*D_NET")[0]
    port_net = spef.split(r"*D_NET sub\.sig ", 1)[1].split("*END", 1)[0]
    assert "*P out[1] B" in port_net and "*I ff1:Q B" in port_net
    internal = spef.split(r"*D_NET sub\.b[0] ", 1)[1].split("*END", 1)[0]
    assert "*I b3:A B" in internal and "*I b2:X B" in internal
    assert all("the DEF does not know" not in warning for warning in report["warnings"])


@_LIVE_ONLY
def test_live_extracted_finfet_dotted_spef_annotates_design(tmp_path):
    from klayout_tools.post_route_sta import run_sta

    _extract_finfet_dotted_fixture(tmp_path)
    annotation = run_sta(str(tmp_path / "request.json"), pdk_variant="sky130A")[
        "spef_annotation"
    ]
    assert annotation["design_nets_annotated"] == annotation["design_nets_total"] == 10
    assert annotation["reader_warning_count"] == 0, annotation["reader_warning_sample"]
    assert annotation["annotation_complete"]


def test_dotted_def_name_without_a_matching_layout_net_is_diagnosed(tmp_path):
    report, spef = _extract_finfet_dotted_fixture(tmp_path, internal_label="sub_b[0]")
    assert r"*D_NET sub\.b[0] " not in spef
    warnings = "\n".join(report["warnings"])
    assert "DEF sub.b[0] -> extracted sub_b[0]" in warnings
    assert "the DEF does not know" not in warnings


def test_dot_free_renamed_port_retains_its_original_net_spelling(tmp_path):
    from klayout_tools.extract_spef import _write_spef, def_net_instance_pins

    design = tmp_path / "port.def"
    design.write_text("NETS 1 ;\n - alias[0] ( PIN OUT ) ( u1 Q ) ;\nEND NETS\n")
    connections = def_net_instance_pins(str(design))
    spef = tmp_path / "port.spef"
    _write_spef(
        str(spef),
        design_name="top",
        klt_version=None,
        parasitics_report={
            "nets": [{"net": "alias[0]", "hub_net": "alias[0]", "capacitance_ff": 1.0}]
        },
        port_names=connections.port_pins,
        net_instance_pins=connections,
    )
    text = spef.read_text()
    assert r"*D_NET alias\[0\] " in text and "*P OUT B" in text


def test_colliding_finfet_spellings_do_not_take_another_nets_instance_pins(tmp_path):
    report, spef = _extract_finfet_dotted_fixture(tmp_path, collision=True)
    assert sum(net["net"] == "sub_sig" for net in report["parasitics"]["nets"]) == 2
    assert "DEF sub.sig -> extracted sub_sig" in "\n".join(report["warnings"])
    assert "*I ff1:Q B" not in spef
    assert r"*D_NET sub\.b[0] " in spef
