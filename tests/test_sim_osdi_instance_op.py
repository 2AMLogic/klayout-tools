"""Read PSP103 device currents through the public sim CLI with real ngspice."""

import json
import os
import shutil
from pathlib import Path

import pytest

from klayout_tools.cli import main


def _psp103_files(repo: Path, override: str | None) -> tuple[Path, Path] | None:
    if override is not None:
        roots = [Path(override).resolve()]
    else:
        variant = Path("pdks/ihp-open-pdk/ihp-sg13g2")
        roots = [repo / variant]
        if repo.parent.name == "worktrees" and repo.parent.parent.name == ".loom":
            roots.append(repo.parents[2] / variant)
    for root in roots:
        assets = root / "libs.tech/ngspice"
        card = assets / "models/cornerMOSlv.lib"
        model = assets / "osdi/psp103.osdi"
        if card.is_file() and model.is_file():
            return card, model
    return None


def test_psp103_discovery_in_a_loom_worktree(tmp_path, monkeypatch):
    repo = tmp_path / ".loom/worktrees/worker"
    variant = Path("pdks/ihp-open-pdk/ihp-sg13g2")
    card = Path("libs.tech/ngspice/models/cornerMOSlv.lib")
    model = Path("libs.tech/ngspice/osdi/psp103.osdi")
    local, shared = repo / variant, tmp_path / variant
    for file in (local / card, shared / card, shared / model):
        file.parent.mkdir(parents=True)
        file.touch()
    assert _psp103_files(repo, None) == (shared / card, shared / model)
    (local / model).parent.mkdir(parents=True)
    (local / model).touch()
    assert _psp103_files(repo, None) == (local / card, local / model)
    monkeypatch.chdir(tmp_path)
    assert _psp103_files(repo, str(variant)) == (shared / card, shared / model)


@pytest.fixture
def psp103_request(tmp_path):
    if shutil.which("ngspice") is None:
        pytest.skip("requires ngspice")
    repo = Path(__file__).resolve().parents[1]
    files = _psp103_files(repo, os.environ.get("KLT_IHP_SG13G2_ROOT"))
    if files is None:
        pytest.skip("requires IHP cornerMOSlv.lib and compiled psp103.osdi")
    card, model = files

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
