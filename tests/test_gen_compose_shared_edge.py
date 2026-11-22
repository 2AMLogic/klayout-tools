"""A fallback via landing must stay electrically separate from another net (#7024)."""

import pytest

from klayout_tools.extract import run_extract
from klayout_tools.gen_compose import compose
from test_gen_compose import _gen_block
from test_gen_compose import _isolate as _isolate
from test_gen_compose import pdk_root as pdk_root


@pytest.mark.parametrize("reverse", [False, True], ids=["SIG-first", "VDD-first"])
def test_fallback_landing_does_not_connect_distinct_nets(tmp_path, pdk_root, reverse):
    report = _gen_block(
        tmp_path,
        pdk_root,
        "mos_array",
        "arr",
        w_um=2,
        l_um=0.3,
        rows=2,
        cols=3,
        dummy=0,
        flavor="pfet",
        topology="common_centroid",
        gate_contact=True,
    )
    nets = [
        {"net": name, "pins": [{"block": "arr", "port": port} for port in terminals]}
        for name, terminals in [("SIG", ("U3_D", "U4_G")), ("VDD", ("U1_S", "U3_S"))]
    ]
    output = tmp_path / "top.gds"
    result = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [{"id": "arr", "generator_report": report}],
            "placement": {
                "strategy": "explicit",
                "order": ["arr"],
                "origins_um": {"arr": {"x": 0, "y": 0}},
            },
            "connectivity": list(reversed(nets)) if reverse else nets,
            "routing": {
                "layer_role": "metal2",
                "width_um": 0.3,
                "cross_block_layer_role": "metal3",
            },
            "options": {"cell_name": "top", "output": str(output)},
        },
        request_dir=str(tmp_path),
    )
    extracted = run_extract(str(output), "sky130", top="top")
    assert not extracted["merged_net_labels"]
    statuses = {net["net"]: net for net in result["nets"]}
    assert statuses["SIG"]["routed"]
    if reverse:
        assert statuses["VDD"]["routed"]
    if statuses["VDD"]["routed"]:
        names = {net["name"] for net in extracted["nets"]}
        assert {"SIG", "VDD"} <= names
        assert not result["unrouted_nets"]
    else:
        assert result["unrouted_nets"] == ["VDD"]
        reason = statuses["VDD"]["legs"][0]["reason"]
        assert "cross-block fallback shares a conductive edge" in reason
        assert "already-routed net 'SIG'" in reason
