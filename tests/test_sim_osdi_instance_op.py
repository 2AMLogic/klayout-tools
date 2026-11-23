"""Read PSP103 device currents through the public sim CLI with real ngspice."""

import json
import shutil

import pytest

from helpers.ihp_sg13g2_root import CORNER_MOS_LIB, OSDI_MODEL, find_ihp_sg13g2
from klayout_tools.cli import main

_SKIP_NO_PDK = (
    "needs a fetched IHP-Open-PDK with compiled OSDI models "
    "(scripts/fetch-ihp-sg13g2.sh + scripts/fetch-sg13g2-sim-toolchain.sh, "
    "or KLT_IHP_SG13G2_ROOT=<.../ihp-sg13g2>)"
)


def _device(instance: str) -> str:
    """The ngspice vector path of one PSP103 device inside ``Xdut``."""
    return f"@n.xdut.{instance}.nsg13_lv_nmos"


@pytest.fixture
def psp103_request(tmp_path):
    if shutil.which("ngspice") is None:
        pytest.skip("requires ngspice")
    root = find_ihp_sg13g2(require_stdcell=False)
    if root is None:
        pytest.skip(_SKIP_NO_PDK)

    (tmp_path / "body.spice").write_text(
        "* Nested PSP103 devices\nVdrain d 0 0.6\nVgate g 0 0.7\n"
        "Xdut d g testdut\n.subckt testdut d g\n"
        "XM1 d g 0 0 sg13_lv_nmos W=1u L=0.13u\n"
        "XM2 d g 0 0 sg13_lv_nmos W=3u L=0.13u\n"
        ".ends testdut\n"
    )
    return {
        "netlist": "body.spice",
        "models": {"lib": str(root / CORNER_MOS_LIB)},
        "corners": {"process": ["mos_tt"], "temperature_c": [27]},
        "options": {"osdi_preload": [str(root / OSDI_MODEL)]},
    }


@pytest.mark.parametrize("analysis", ["op", "dc"])
def test_nested_osdi_current_matches_an_independent_meter(
    tmp_path, capsys, psp103_request, analysis
):
    """Two parallel legs in one subcircuit: each device's ``[ids]`` must read
    its own current (cross-checked against its own ``[ide]``), and the two
    must sum to the independent supply meter ``-i(vdrain)`` -- a read of the
    total supply current for either leg fails both checks."""
    xm1, xm2 = _device("xm1"), _device("xm2")
    request = psp103_request
    request["analysis"] = {
        "kind": analysis,
        "args": "" if analysis == "op" else "Vdrain 0.2 0.8 0.1",
    }
    request["options"]["save_vectors"] = [
        xm1 + "[ids]",
        xm1 + "[ide]",
        xm2 + "[ids]",
        xm2 + "[ide]",
    ]
    if analysis == "op":
        measures = [
            {"name": "ids1", "expr": xm1 + "[ids]"},
            {"name": "ide1", "expr": xm1 + "[ide]"},
            {"name": "ids2", "expr": xm2 + "[ids]"},
            {"name": "ide2", "expr": xm2 + "[ide]"},
            {"name": "meter", "expr": "-i(vdrain)"},
        ]
    else:
        measures = [
            {"name": "peak1", "expr": f"vecmax({xm1}[ids])"},
            {"name": "peak2", "expr": f"vecmax({xm2}[ids])"},
            {"name": "meter", "expr": "vecmax(-i(vdrain))"},
            {"name": "atbias1", "spice": f".meas dc atbias1 FIND {xm1}[ids] AT=0.6"},
            {"name": "atbias2", "spice": f".meas dc atbias2 FIND {xm2}[ids] AT=0.6"},
            {"name": "idebias1", "spice": f".meas dc idebias1 FIND {xm1}[ide] AT=0.6"},
            {"name": "idebias2", "spice": f".meas dc idebias2 FIND {xm2}[ide] AT=0.6"},
            {"name": "supply", "spice": ".meas dc supply FIND i(vdrain) AT=0.6"},
        ]
    request["measurements"] = [{**m, "unit": "A"} for m in measures]
    filename = tmp_path / "req.json"
    filename.write_text(json.dumps(request))
    assert main(["sim", str(filename), "--backend", "local", "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "pass"
    result = {m["name"]: m["value"] for m in report["corners"][0]["measurements"]}
    assert result["meter"] > 0
    if analysis == "op":
        assert 0 < result["ids1"] < result["ids2"] < result["meter"]
        assert result["ids1"] == pytest.approx(result["ide1"], rel=1e-4)
        assert result["ids2"] == pytest.approx(result["ide2"], rel=1e-4)
        assert result["ids1"] + result["ids2"] == pytest.approx(
            result["meter"], rel=1e-4
        )
    else:
        supply = -result["supply"]
        assert 0 < result["atbias1"] < result["atbias2"] < supply
        assert result["atbias1"] == pytest.approx(result["idebias1"], rel=1e-4)
        assert result["atbias2"] == pytest.approx(result["idebias2"], rel=1e-4)
        assert result["atbias1"] + result["atbias2"] == pytest.approx(supply, rel=1e-4)
        assert 0 < result["peak1"] < result["peak2"] < result["meter"]
        assert result["atbias2"] < result["peak2"]
