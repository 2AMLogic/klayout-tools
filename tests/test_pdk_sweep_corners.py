"""Route-stage sweeps use the logical corners declared for a cell library."""

import json
from pathlib import Path

import pytest

from klayout_tools.pdk_cells import _descriptor_liberty, list_lib_corners
from klayout_tools.place_and_route import PlaceAndRouteError, run_place_and_route
from test_place_and_route import (
    _corner_sweep_script,
    _setup_success_env,
    _stub_merge_def_to_gds,
    _stub_openroad_success,
)

LIBRARY = "sky130_fd_sc_hd"
pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _install(tmp_path, monkeypatch, corners, **selection):
    request = _setup_success_env(
        tmp_path,
        monkeypatch,
        pdk={"cell_library": LIBRARY, "corner": "TT", **selection},
    )
    variant = tmp_path / "install/sky130A"
    monkeypatch.setenv("KLT_CACHE_DIR", str(tmp_path / "cache"))
    for name, cell in (("inv", "INV"), ("buf", "BUF")):
        (variant / f"{name}.lib").write_text(
            f'library ({name}) {{\n  time_unit : "1ns";\n'
            f"  cell ({cell}) {{ area : 1; }}\n}}\n"
        )
    (variant / "klt-pdk.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "assets": {"libs_ref": "libs.ref"},
                "liberty": {LIBRARY: corners},
            }
        )
    )
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)
    return request, variant


@pytest.mark.parametrize("selection", [{}, {"sweep_corners": ["TT"]}])
def test_route_sweeps_one_merged_descriptor_corner(tmp_path, monkeypatch, selection):
    request, variant = _install(
        tmp_path, monkeypatch, {"TT": ["inv.lib", "buf.lib"]}, **selection
    )
    selected, _ = _descriptor_liberty(
        {"root": str(variant)}, LIBRARY, "TT", PlaceAndRouteError
    )

    report = run_place_and_route(request)
    (corner,) = report["corners"]
    assert corner["name"] == "TT"
    assert {
        "setup_slack_ns",
        "hold_slack_ns",
        "max_transition_violation_count",
        "max_capacitance_violation_count_vs_library",
    } <= corner.keys()
    script = Path(_corner_sweep_script(request)).read_text().splitlines()
    assert [line for line in script if line.startswith("define_corners ")] == [
        "define_corners TT"
    ]
    assert [line for line in script if line.startswith("read_liberty -corner ")] == [
        f"read_liberty -corner TT {selected}"
    ]


def test_string_decks_use_descriptor_order_and_safe_artifact_names(
    tmp_path, monkeypatch
):
    request, _ = _install(
        tmp_path, monkeypatch, {"TT": "inv.lib", "slow/low": "buf.lib"}
    )
    report = run_place_and_route(request)
    assert [corner["name"] for corner in report["corners"]] == ["TT", "slow/low"]
    out = Path(_corner_sweep_script(request)).parent
    assert (out / "pnr_gcd_route_corner_slow%2Flow.tcl").is_file()


def test_unknown_sweep_names_descriptor_corners(tmp_path, monkeypatch):
    request, _ = _install(
        tmp_path, monkeypatch, {"TT": "inv.lib"}, sweep_corners=["buf"]
    )
    with pytest.raises(PlaceAndRouteError, match="available corners: TT"):
        run_place_and_route(request)


def test_unused_bad_corner_can_be_excluded_or_sweep_disabled(tmp_path, monkeypatch):
    corners = {"TT": "inv.lib", "SS": ["inv.lib", "buf.lib"]}
    request, variant = _install(tmp_path, monkeypatch, corners)
    member = variant / "buf.lib"
    member.write_text(member.read_text().replace("1ns", "1ps"))
    with pytest.raises(PlaceAndRouteError) as error:
        run_place_and_route(request)
    assert all(
        s in str(error.value) for s in ("inv.lib", "buf.lib", "pdk.sweep_corners: []")
    )
    for names in ([], ["TT"]):
        path = Path(request)
        payload = json.loads(path.read_text())
        payload["pdk"]["sweep_corners"] = names
        path.write_text(json.dumps(payload))
        assert [c["name"] for c in run_place_and_route(request)["corners"]] == names


def test_unmapped_library_uses_conventional_discovery(tmp_path, monkeypatch):
    from klayout_tools.pdk_sweep_corners import list_sweep_corners

    _, variant = _install(tmp_path, monkeypatch, {"TT": "inv.lib"})
    mapping = variant / "klt-pdk.json"
    info = {"root": str(variant), "assets": {"libs_ref": str(variant / "libs.ref")}}
    for descriptor in (True, False):
        if descriptor:
            data = json.loads(mapping.read_text())
            data["liberty"] = {"other": {"TT": "inv.lib"}}
            mapping.write_text(json.dumps(data))
        else:
            mapping.unlink()
        assert list_sweep_corners(
            LIBRARY, info, PlaceAndRouteError
        ) == list_lib_corners(LIBRARY, info)
