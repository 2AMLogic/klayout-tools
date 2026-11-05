"""`klt extract` DEF SPEF spelling for escaped-bracket hierarchical nets (issue #4997)."""

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
