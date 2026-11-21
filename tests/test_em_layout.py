"""GDS-region input through the same entry point as a supplied mesh."""

import hashlib
import json
import os
import shutil

import klayout.db as kdb
import pytest

from klayout_tools import pdk_stackup
from klayout_tools.cli import main
from klayout_tools.em import EmError, run_em


@pytest.fixture
def request_file(tmp_path, monkeypatch):
    install = tmp_path / "pdks" / "sky130A"
    (install / "libs.tech").mkdir(parents=True)
    (install / "libs.ref").mkdir()
    monkeypatch.setenv("PDK_ROOT", str(install.parent))
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    child = layout.create_cell("PAIR")
    top.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans(1000, 0)))
    metal = layout.layer(68, 20)
    labels = layout.layer(68, 5)
    for y, label in [(0, "BUS[0]"), (1000, "REF")]:
        child.shapes(metal).insert(kdb.Box(0, y, 2000, y + 400))
        child.shapes(labels).insert(kdb.Text(label, 1800, y + 200))
    # The via carries the BUS label onto an otherwise unnamed met2 shape.
    child.shapes(layout.layer(68, 44)).insert(kdb.Box(500, 50, 700, 250))
    child.shapes(layout.layer(69, 20)).insert(kdb.Box(0, 0, 1000, 400))
    # An unrelated island outside the requested region needs no terminal.
    child.shapes(metal).insert(kdb.Box(5000, 0, 5100, 100))
    layout.write(str(tmp_path / "pair.gds"))
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {
                "layout": "pair.gds",
                "region": {"cell": "TOP", "bbox_um": [1.2, -0.5, 2.5, 2]},
                "stackup": "sky130A",
                "options": {"output_dir": "artifacts"},
                "checks": [
                    {
                        "name": "coupling",
                        "output": "c",
                        "terminal_pair": ["BUS[0]", "REF"],
                        "limits": {"max": 0},
                    }
                ],
            }
        )
    )
    return path


def _edit(path, **changes):
    request = json.loads(path.read_text())
    request.update(changes)
    path.write_text(json.dumps(request))


def test_unlabelled_terminal_is_application_error(request_file, capsys):
    _edit(request_file, region={"cell": "PAIR"})
    assert main(["em", str(request_file), "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unlabeled" in json.loads(captured.err)["error"]["message"]


@pytest.mark.parametrize("label", ["REF", "SECOND"])
def test_ambiguous_labels(request_file, label):
    path = request_file.parent / "pair.gds"
    layout = kdb.Layout()
    layout.read(str(path))
    cell = layout.cell("PAIR")
    if label == "SECOND":
        cell.shapes(layout.layer(68, 5)).insert(kdb.Text(label, 200, 200))
    else:
        cell.shapes(layout.layer(68, 20)).insert(kdb.Box(200, 600, 500, 800))
        cell.shapes(layout.layer(68, 5)).insert(kdb.Text(label, 300, 700))
    layout.write(str(path))
    with pytest.raises(EmError, match="ambiguous"):
        run_em(str(request_file))


@pytest.mark.skipif(shutil.which("geode") is None, reason="needs geode")
def test_missing_gmsh(request_file, monkeypatch, capsys):
    monkeypatch.setenv("GEODE_GMSH", str(request_file.parent / "absent-gmsh"))
    assert main(["em", str(request_file), "--format", "json"]) == 1
    assert "gmsh" in json.loads(capsys.readouterr().err)["error"]["message"].lower()


@pytest.mark.skipif(
    not (shutil.which("geode") and shutil.which(os.environ.get("GEODE_GMSH", "gmsh"))),
    reason="needs geode and Gmsh",
)
def test_live_gds_mesh_and_capacitance(request_file):
    response = run_em(str(request_file))
    assert response["status"] == "pass"
    c = response["results"]["c"]
    assert c["terminals"] == ["BUS[0]", "REF"]
    assert c["matrix"][0][0] > 0 > c["matrix"][0][1]
    env = response["environment"]
    assert (
        env["layout_sha256"]
        == hashlib.sha256((request_file.parent / "pair.gds").read_bytes()).hexdigest()
    )
    stack = pdk_stackup.stackup("sky130A")
    assert (
        env["stackup_sha256"]
        == hashlib.sha256(
            json.dumps(stack, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert env["mesh_stats"]["nodes"] > 0
    assert env["mesh_stats"]["tets"] > 0
    (mesh,) = (request_file.parent / "artifacts").glob("*/layout.msh")
    assert env["mesh_sha256"] == hashlib.sha256(mesh.read_bytes()).hexdigest()
    assert response["artifacts"]["mesh"]["sha256"] == "sha256:" + env["mesh_sha256"]


def test_clipped_layers_and_labels_follow_stackup(request_file, monkeypatch):
    from klayout_tools import _em_layout

    stack = pdk_stackup.stackup("sky130A")
    met1 = next(c for c in stack["conductors"] if c["name"] == "met1")
    met1.update(z0_um=1.4, thickness_um=0.3, z1_um=1.7)
    stack["dielectrics"][0]["permittivity"] = 2.8
    monkeypatch.setattr(_em_layout, "stackup", lambda variant: stack)
    req = json.loads(request_file.read_text())
    req["ground"] = ["REF"]
    prepared = _em_layout.prepare_layout(req, str(request_file.parent))
    (signal,) = prepared.terminals
    assert signal["name"] == "BUS[0]"
    doc = prepared.document
    metals = {c["name"]: c for c in doc["conductors"]}
    assert metals["met1"]["z_bottom"] == 1.4
    assert metals["met1"]["thickness"] == 0.3
    assert doc["dielectrics"][0]["eps_r"] == [2.8, 0]
    assert [p["net"] for p in metals["via"]["polygons"]] == [signal["surface"]]
    assert [p["net"] for p in metals["met2"]["polygons"]] == [signal["surface"]]
    signal_poly = next(
        p for p in metals["met1"]["polygons"] if p["net"] == signal["surface"]
    )
    assert sorted(signal_poly["outer"]) == [[1.2, 0], [1.2, 0.4], [2.5, 0], [2.5, 0.4]]
    assert set(doc["ground"]) == {
        p["net"] for p in metals["met1"]["polygons"] if p is not signal_poly
    }
    assert "passivation" in prepared.diagnostics[0]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"mesh": {}}, "cannot be combined"),
        ({"region": {"bbox_um": [1, 2, 1, 3]}}, "bbox_um"),
        ({"region": {"bbox_um": [10, 10, 20, 20]}}, "no stackup conductors"),
        ({"region": {"cell": "MISSING"}}, "not found"),
        ({"layout": "absent.gds"}, "file not found"),
        ({"ground": ["absent"]}, "GDS labels"),
        ({"ground": ["REF", "BUS[0]"]}, "leave at least one terminal"),
    ],
)
def test_layout_request_errors(request_file, changes, message):
    _edit(request_file, **changes)
    with pytest.raises(EmError, match=message):
        run_em(str(request_file))


def test_unknown_passivation_is_an_error_for_top_metal(request_file):
    path = request_file.parent / "pair.gds"
    layout = kdb.Layout()
    layout.read(str(path))
    cell = layout.cell("PAIR")
    cell.shapes(layout.layer(72, 20)).insert(kdb.Box(400, 0, 800, 400))
    cell.shapes(layout.layer(72, 5)).insert(kdb.Text("TOP_PIN", 500, 200))
    layout.write(str(path))
    with pytest.raises(EmError, match="met5 reaches the grounded top"):
        run_em(str(request_file))


def test_rectilinear_hole_is_preserved_and_diagonals_refused():
    from klayout_tools._em_layout import _rings

    ring = kdb.Region(kdb.Box(0, 0, 10, 10)) - kdb.Region(kdb.Box(2, 2, 8, 8))
    rebuilt = kdb.Region()
    for outer in _rings(ring, 1.0):
        rebuilt.insert(kdb.Polygon([kdb.Point(int(x), int(y)) for x, y in outer]))
    assert (rebuilt ^ ring).is_empty()
    triangle = kdb.Polygon([kdb.Point(0, 0), kdb.Point(3, 0), kdb.Point(2, 5)])
    with pytest.raises(EmError, match="rectilinear"):
        list(_rings(kdb.Region(triangle), 1.0))


def test_discard_generated_artifacts(request_file, monkeypatch):
    """Replay only the external subprocess boundary; inspect both real argv."""
    from pathlib import Path

    from klayout_tools import _geode

    _edit(request_file, options={"geode_binary": "/bin/true", "keep_artifacts": False})
    stages = []

    def replay(cmd, report_path, log_path, timeout, keep):
        stages.append(cmd[1])
        if cmd[1] == "mesh":
            assert cmd[cmd.index("--analysis") + 1] == "capacitance"
            mesh = Path(cmd[cmd.index("--mesh-out") + 1])
            mesh.write_bytes(b"tagged mesh fixture")
            Path(cmd[cmd.index("--spec-out") + 1]).write_text("generated spec")
            report = {"mesh": {"n_nodes": 20, "n_tets": 30, "n_triangles": 10}}
        else:
            assert Path(cmd[2]).read_text() == "generated spec"
            report = {"terminals": ["net_1", "net_0"], "c_farad": [[3, -1], [-1, 2]]}
        Path(report_path).write_text(json.dumps(report))
        assert not keep

    monkeypatch.setattr(_geode, "_run_engine", replay)
    response = run_em(str(request_file))
    assert stages == ["mesh", "capacitance"]
    assert response["results"]["c"]["matrix"] == [[2, -1], [-1, 3]]
    assert (
        response["environment"]["mesh_sha256"]
        == hashlib.sha256(b"tagged mesh fixture").hexdigest()
    )
    assert response["artifacts"] == {}
