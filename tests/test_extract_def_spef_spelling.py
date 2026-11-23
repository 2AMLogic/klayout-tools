"""`klt extract` DEF SPEF spelling for escaped-bracket nets (issue #4997)."""

from __future__ import annotations

from klayout_tools.extract import run_extract
from test_extract import _make_inverter_layout, _write_gds


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


def test_live_def_bus_bits_annotate_the_design(tmp_path):
    import json
    from pathlib import Path

    import pytest

    from klayout_tools.extract_spef import _write_spef, def_net_instance_pins
    from klayout_tools.post_route_sta import run_sta
    from test_post_route_sta import _live_sky130_env

    if _live_sky130_env() is None:
        pytest.skip("requires OpenROAD and the SKY130A PDK")

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
