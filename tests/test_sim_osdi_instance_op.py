"""Read PSP103 device currents through the public sim CLI with real ngspice."""

import json
import os
import shutil
from pathlib import Path

import pytest

from klayout_tools.cli import main


@pytest.fixture
def psp103_request(tmp_path):
    if shutil.which("ngspice") is None:
        pytest.skip("requires ngspice")
    repo = Path(__file__).resolve().parents[1]
    checkout = (
        repo.parents[2]
        if repo.parent.name == "worktrees" and repo.parent.parent.name == ".loom"
        else repo
    )
    root = Path(
        os.environ.get("KLT_IHP_SG13G2_ROOT", checkout / "pdks/ihp-open-pdk/ihp-sg13g2")
    )
    assets = root / "libs.tech/ngspice"
    card = assets / "models/cornerMOSlv.lib"
    model = assets / "osdi/psp103.osdi"
    if not card.is_file() or not model.is_file():
        pytest.skip("requires IHP cornerMOSlv.lib and compiled psp103.osdi")

    (tmp_path / "body.spice").write_text(
        "* Nested PSP103 device\nVdrain d 0 0.6\nVgate g 0 0.7\n"
        "Xdut d g testdut\n.subckt testdut d g\n"
        "XM1 d g 0 0 sg13_lv_nmos W=1u L=0.13u\n.ends testdut\n"
    )
    return {
        "netlist": "body.spice",
        "models": {"lib": str(card)},
        "corners": {"process": ["mos_tt"], "temperature_c": [27]},
        "options": {"osdi_preload": [str(model)]},
    }


@pytest.mark.parametrize("analysis", ["op", "dc"])
def test_nested_osdi_current_matches_an_independent_meter(
    tmp_path, capsys, psp103_request, analysis
):
    path = "@n.xdut.xm1.nsg13_lv_nmos"
    request = psp103_request
    request["options"]["save_vectors"] = [path + "[ids]", path + "[ide]"]
    request["analysis"] = {
        "kind": analysis,
        "args": "" if analysis == "op" else "Vdrain 0.2 0.8 0.1",
    }
    if analysis == "op":
        measures = [
            {"name": "ids", "expr": path + "[ids]"},
            {"name": "ide", "expr": path + "[ide]"},
            {"name": "meter", "expr": "-i(vdrain)"},
        ]
    else:
        measures = [
            {"name": "peak", "expr": f"vecmax({path}[ids])"},
            {"name": "meter", "expr": "vecmax(-i(vdrain))"},
            {"name": "atbias", "spice": f".meas dc atbias FIND {path}[ids] AT=0.6"},
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
        assert result["ids"] == pytest.approx(result["meter"], rel=1e-4)
        assert result["ide"] == pytest.approx(result["meter"], rel=1e-4)
    else:
        assert result["peak"] == pytest.approx(result["meter"], rel=1e-4)
        assert result["atbias"] == pytest.approx(-result["supply"], rel=1e-4)
        assert 0 < result["atbias"] < result["peak"]
