"""Layout-free (`--no-layout`) mode of `klt lef-abstract` (issue #5714):
black-box abstract emitted from the socket descriptor alone. Split from
`test_lef_abstract.py` to keep that file under its size ratchet; the PDK
fixture helpers are shared, not duplicated."""

from __future__ import annotations

import json
from pathlib import Path

import klayout.db as kdb
import pytest

import test_lef_abstract as base
from klayout_tools.cli import main
from klayout_tools.lef_abstract import LefAbstractError, run_lef_abstract

# Re-bind the base module's fixtures/helpers so pytest collects them here.
_isolate_pdk = base._isolate_pdk
_make_pdk_install = base._make_pdk_install
_write_socket = base._write_socket
_basic_descriptor = base._basic_descriptor
_TECH_LEF_TEXT = base._TECH_LEF_TEXT


def _run_no_layout(tmp_path: Path, monkeypatch, *, descriptor=None, tech_lef=None):
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    if tech_lef is not None:
        (
            install_root
            / "sky130A/libs.ref/sky130_fd_sc_hd/techlef/sky130_fd_sc_hd__nom.tlef"
        ).write_text(tech_lef, encoding="utf-8")
    socket_path = _write_socket(
        descriptor or _basic_descriptor(), tmp_path / "socket.json"
    )
    output = tmp_path / "out.lef"
    report = run_lef_abstract(
        None,
        socket_path,
        macro_name="analog_block",
        cell_library="sky130_fd_sc_hd",
        output_path=str(output),
    )
    return report, output.read_text(encoding="utf-8"), socket_path


def test_no_layout_emits_synthesized_black_box(tmp_path, monkeypatch):
    descriptor = _basic_descriptor()
    descriptor["pins"][0]["width_um"] = 0.4
    descriptor["pins"][0]["height_um"] = 0.3
    report, lef_text, socket_path = _run_no_layout(
        tmp_path, monkeypatch, descriptor=descriptor
    )
    assert report["schema_version"] == 2
    assert report["self_check"]["status"] == "pass"
    assert report["file"] == {"path": None, "scope": "absent"}
    assert report["size_um"] == {"width": 5.0, "height": 5.0}
    assert "SIZE 5.0000 BY 5.0000 ;" in lef_text
    assert {p["geometry_source"] for p in report["pins"]} == {"synthesized"}
    pins = {p["name"]: p for p in report["pins"]}
    x0, y0, x1, y1 = pins["VOUT"]["rects_um"][0]
    assert (round(x1 - x0, 3), round(y1 - y0, 3)) == (0.4, 0.3)
    x0, y0, x1, y1 = pins["VDD"]["rects_um"][0]  # met1 WIDTH 0.14 fallback
    assert round(x1 - x0, 3) == 0.14
    assert report["provenance"]["input"]["role"] == "source"
    from klayout_tools._provenance import content_hash

    assert report["provenance"]["input"]["content_hash"] == content_hash(socket_path)


def test_no_layout_obs_covers_outline_minus_pins(tmp_path, monkeypatch):
    report, _lef, _ = _run_no_layout(tmp_path, monkeypatch)
    assert {o["layer"] for o in report["obs"]} == {"li1", "met1"}
    assert report["obs_shape_count"] > 0
    # Rebuild the OBS region from the report's LEF text and confirm the
    # declared pin ports are not covered while the outline otherwise is.
    obs_by_layer: dict[str, kdb.Region] = {}
    layer = None
    in_obs = False
    for line in _lef.splitlines():
        s = line.strip()
        if s == "OBS":
            in_obs = True
        elif in_obs and s.startswith("LAYER"):
            layer = s.split()[1]
            obs_by_layer.setdefault(layer, kdb.Region())
        elif in_obs and s.startswith("RECT"):
            x0, y0, x1, y1 = (round(float(v) * 1000) for v in s.split()[1:5])
            obs_by_layer[layer].insert(kdb.Box(x0, y0, x1, y1))
        elif in_obs and s.startswith("POLYGON"):
            nums = [round(float(v) * 1000) for v in s.split()[1:-1]]
            pts = [kdb.Point(nums[i], nums[i + 1]) for i in range(0, len(nums), 2)]
            obs_by_layer[layer].insert(kdb.Polygon(pts))
        elif s == "END":
            in_obs = False
    for pin in report["pins"]:
        x0, y0, x1, y1 = (round(v * 1000) for v in pin["rects_um"][0])
        pin_region = kdb.Region(kdb.Box(x0, y0, x1, y1))
        assert (obs_by_layer[pin["layer"]] & pin_region).is_empty()
    outline = kdb.Region(kdb.Box(0, 0, 5000, 5000))
    pin_total = kdb.Region()
    for pin in report["pins"]:
        if pin["layer"] == "met1":
            x0, y0, x1, y1 = (round(v * 1000) for v in pin["rects_um"][0])
            pin_total.insert(kdb.Box(x0, y0, x1, y1))
    assert ((obs_by_layer["met1"] + pin_total) ^ outline).is_empty()


def test_no_layout_unresolvable_pin_layer_is_unroutable(tmp_path, monkeypatch):
    descriptor = _basic_descriptor()
    descriptor["pins"].append({"name": "X", "layer": [99, 99], "x": 1.0, "y": 1.0})
    report, _lef, _ = _run_no_layout(tmp_path, monkeypatch, descriptor=descriptor)
    assert report["unroutable_pins"] == [{"name": "X", "layer": [99, 99]}]


def test_no_layout_footprint_only_black_box(tmp_path, monkeypatch):
    for pins in ([], None):
        report, lef_text, _ = _run_no_layout(
            tmp_path, monkeypatch, descriptor=_basic_descriptor(pins=pins)
        )
        assert report["pin_count"] == 0
        assert report["self_check"]["status"] == "pass"
        assert "OBS" in lef_text and "PIN" not in lef_text


def test_no_layout_without_database_microns_errors(tmp_path, monkeypatch):
    tech = _TECH_LEF_TEXT.replace("  DATABASE MICRONS 1000 ;\n", "")
    with pytest.raises(LefAbstractError, match="DATABASE MICRONS"):
        _run_no_layout(tmp_path, monkeypatch, tech_lef=tech)


def test_no_layout_with_top_raises(tmp_path, monkeypatch):
    _isolate_pdk(monkeypatch, tmp_path)
    socket_path = _write_socket(_basic_descriptor(), tmp_path / "s.json")
    with pytest.raises(LefAbstractError):
        run_lef_abstract(
            None,
            socket_path,
            macro_name="m",
            cell_library="sky130_fd_sc_hd",
            output_path=str(tmp_path / "o.lef"),
            top="X",
        )


def test_no_layout_pin_outside_outline_is_emitted_unclipped(tmp_path, monkeypatch):
    """A pin whose declared position falls outside the declared outline is
    still synthesized (the descriptor is the contract) and lands outside the
    macro footprint; its port is not clipped, and subtracting it leaves the
    outline-covering OBS unchanged (#5714 review: untested edge case)."""
    descriptor = _basic_descriptor()
    descriptor["pins"][1]["x"] = 6.0  # VDD (met1); outline is (0, 0)..(5, 5)
    descriptor["pins"][1]["y"] = 6.0
    report, lef_text, _ = _run_no_layout(tmp_path, monkeypatch, descriptor=descriptor)
    assert report["self_check"]["status"] == "pass"
    pin = {p["name"]: p for p in report["pins"]}["VDD"]
    x0, y0, x1, y1 = pin["rects_um"][0]
    assert x0 > 5.0 and y0 > 5.0 and x1 > 5.0 and y1 > 5.0  # unclipped
    # OBS on met1 still covers the whole outline: the only met1 pin's port
    # lies outside it, so subtracting the port is a no-op -- and subtracting
    # never extends OBS beyond the outline either.
    obs_met1 = kdb.Region()
    in_obs = False
    layer = None
    for line in lef_text.splitlines():
        s = line.strip()
        if s == "OBS":
            in_obs = True
            layer = None
        elif in_obs and s.startswith("LAYER"):
            layer = s.split()[1]
        elif in_obs and s.startswith("RECT") and layer == "met1":
            rx0, ry0, rx1, ry1 = (round(float(v) * 1000) for v in s.split()[1:5])
            obs_met1.insert(kdb.Box(rx0, ry0, rx1, ry1))
        elif s == "END":
            in_obs = False
    assert (obs_met1 ^ kdb.Region(kdb.Box(0, 0, 5000, 5000))).is_empty()


@pytest.mark.parametrize(
    "extra",
    [
        [],  # neither <file> nor --no-layout
        ["--no-layout", "--top", "X"],
        ["--no-layout", "--obs-mode", "detailed"],
        ["--no-layout", "--require-power-pins"],
        ["__FILE__", "--no-layout"],
    ],
)
def test_no_layout_usage_errors_exit_2(tmp_path, capsys, extra):
    argv = [
        "lef-abstract",
        "--socket",
        "s.json",
        "--macro-name",
        "m",
        "--cell-library",
        "sky130_fd_sc_hd",
        "-o",
        str(tmp_path / "o.lef"),
    ]
    argv += [str(tmp_path / "b.gds") if a == "__FILE__" else a for a in extra]
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert not (tmp_path / "o.lef").exists()


def test_cli_no_layout_json(tmp_path, monkeypatch, capsys):
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    socket_path = _write_socket(_basic_descriptor(), tmp_path / "socket.json")
    exit_code = main(
        [
            "lef-abstract",
            "--no-layout",
            "--socket",
            socket_path,
            "--macro-name",
            "analog_block",
            "--cell-library",
            "sky130_fd_sc_hd",
            "-o",
            str(tmp_path / "out.lef"),
            "--format",
            "json",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["file"] == {"path": None, "scope": "absent"}
    assert payload["obs_mode"] == "detailed"
