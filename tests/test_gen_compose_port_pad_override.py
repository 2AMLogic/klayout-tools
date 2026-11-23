"""Verify the label access footprint against metal drawn by gen-compose."""

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from test_gen_compose import pdk_root as pdk_root
from test_gen_compose_port_derivation import (
    MET1,
    MET1_PIN,
    MET2,
    _make_macro,
    _routed_access_request,
)


def _request(root, pdk, override=None, *, blocked=False, intermediate_array=False):
    a = root / "a"
    b = root / "b"
    a.mkdir()
    b.mkdir()
    obstacles = [(MET1, (4.0, 0.8, 10.0, 2.0))]
    if intermediate_array:
        obstacles = [(MET2, (4.0, 1.0, 10.0, 2.0))]
    if blocked:
        obstacles = [(MET1, (0, 0.8, 10, 2)), (MET1, (0, -2, 10, -0.2))]
    strip = (MET1, (0, 0, 10, 0.6))
    source = _make_macro(a, [strip, *obstacles], [(MET1_PIN, "A", 8.0, 0.3)])
    sink = _make_macro(b, [strip], [])
    array = [2, 2] if intermediate_array else [1, 1]
    request = _routed_access_request(source, sink, pdk, array, root / "top.gds")
    if intermediate_array:
        request["blocks"][0]["cell"]["ports_from_labels"]["access"]["up_to"] = "metal4"
        request["routing"].update(layer_role="metal4", width_um=0.3)
    if override is not None:
        request["routing"]["landing_pad_size_um"] = override
    path = root / "request.json"
    path.write_text(json.dumps(request))
    return path


@pytest.mark.parametrize("override, drawn_side", [(None, 420), (0.8, 800)])
def test_public_compose_draws_a_clear_landing(
    tmp_path, pdk_root, capsys, override, drawn_side
):
    path = _request(tmp_path, pdk_root, override)
    assert main(["gen-compose", str(path), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["nets"][0]["routed"]
    landing = report["blocks"][0]["derived_ports"][0]["landing"]
    if override is None:
        assert (landing["x_um"], landing["y_um"]) == pytest.approx((7.98, 0.315))
    gds = tmp_path / "top.gds"
    layout = kdb.Layout()
    layout.read(str(gds))
    center = kdb.DPoint(landing["x_um"], landing["y_um"]).to_itype(layout.dbu)
    boxes = [s.bbox() for s in layout.top_cell().shapes(layout.layer(*MET1)).each()]
    assert any(
        box.center() == center and box.width() == box.height() == drawn_side
        for box in boxes
    )
    assert (
        main(
            [
                "drc",
                str(gds),
                "--deck",
                "sky130",
                "--rules",
                "met1.space.1",
                "--format",
                "json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["violation_count"] == 0


def test_oversized_pad_reports_clearance_if_every_point_is_blocked(
    tmp_path, pdk_root, capsys
):
    path = _request(tmp_path, pdk_root, 0.8, blocked=True)
    assert main(["gen-compose", str(path), "--format", "json"]) == 1
    message = json.loads(capsys.readouterr().err)["error"]["message"]
    assert "no legal landing" in message and "clearance:" in message


def test_shared_metal_clears_the_larger_array_pad(tmp_path, pdk_root, capsys):
    path = _request(tmp_path, pdk_root, 0.8, intermediate_array=True)
    assert main(["gen-compose", str(path), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["nets"][0]["routed"]
    landing = report["blocks"][0]["derived_ports"][0]["landing"]
    layout = kdb.Layout()
    output = tmp_path / "top.gds"
    layout.read(str(output))
    center = kdb.DPoint(landing["x_um"], landing["y_um"]).to_itype(layout.dbu)
    widths = {
        shape.bbox().width()
        for shape in layout.top_cell().shapes(layout.layer(*MET2)).each()
        if shape.bbox().center() == center
    }
    # Both hops land on met2. Their 2-by-2 arrays add the pitch of their own
    # cut layer (320 and 400 DBU) to the shared 800 DBU baseline.
    assert {1120, 1200} <= widths
    assert (
        main(
            [
                "drc",
                str(output),
                "--deck",
                "sky130",
                "--rules",
                "met2.space.1",
                "--format",
                "json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["violation_count"] == 0


def test_access_below_floor_names_the_binding_pair(tmp_path, pdk_root, capsys):
    path = _request(tmp_path, pdk_root, 0.1)
    assert main(["gen-compose", str(path), "--format", "json"]) == 1
    message = json.loads(capsys.readouterr().err)["error"]["message"]
    assert "routing.landing_pad_size_um" in message
    assert (
        "layer (68, 20) over via layer (68, 44)" in message and "requires >=" in message
    )


def test_no_connectivity_preserves_default_access_size(tmp_path, pdk_root, capsys):
    path = _request(tmp_path, pdk_root, "unused")
    request = json.loads(path.read_text())
    request["connectivity"] = []
    path.write_text(json.dumps(request))
    assert main(["gen-compose", str(path), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    landing = report["blocks"][0]["derived_ports"][0]["landing"]
    assert (landing["x_um"], landing["y_um"]) == pytest.approx((7.98, 0.315))


@pytest.mark.parametrize("override", [0.1, 0.8])
def test_conflicting_pad_policies_precede_label_access(
    tmp_path, pdk_root, capsys, override
):
    path = _request(tmp_path, pdk_root, override, blocked=True)
    req = json.loads(path.read_text())
    req["routing"]["minimal_ladder"] = True
    path.write_text(json.dumps(req))
    assert main(["gen-compose", str(path), "--format", "json"]) == 1
    message = json.loads(capsys.readouterr().err)["error"]["message"]
    assert "minimal_ladder and request.routing.landing_pad_size_um" in message
    assert "cannot be combined" in message
    assert not (tmp_path / "top.gds").exists()
