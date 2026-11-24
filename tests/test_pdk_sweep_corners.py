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


def test_retained_library_limits_match_the_sweep_and_clear_on_rerun(
    tmp_path, monkeypatch
):
    request, _ = _install(
        tmp_path, monkeypatch, {"TT": "inv.lib", "slow/low": "buf.lib"}
    )
    _stub_openroad_success(
        monkeypatch,
        max_transition_violations=6,
        max_transition_violations_vs_library=2,
        max_capacitance_violations_vs_library=1,
        per_corner_design_rule_violations_vs_library={
            "TT": (0, 0),
            "slow%2Flow": (1, 0),
        },
    )
    report = run_place_and_route(request)
    combined = Path(report["library_limit_report_path"])
    assert report["stages"][-1]["library_limit_report_path"] == str(combined)
    assert combined.read_text().count("(VIOLATED)") == 3
    fast, slow = report["corners"]
    assert fast["library_limit_report_path"] is None
    corner_file = Path(slow["library_limit_report_path"])
    assert corner_file.name == "corner-slow%2Flow.rpt"
    assert (
        corner_file.read_text()
        == "max_transition\npin_0/A 0.75 1.20 -0.45 (VIOLATED)\n\nmax_capacitance\n"
    )
    assert str(combined) != str(corner_file)

    corner_file.unlink()
    resumed = run_place_and_route(request, resume=True)
    assert "route" not in resumed["resume"]["resumed_stages"]
    assert corner_file.is_file()

    _stub_openroad_success(monkeypatch)
    clean = run_place_and_route(request)
    assert clean["library_limit_report_path"] is None
    assert all(c["library_limit_report_path"] is None for c in clean["corners"])
    assert not combined.exists()
    assert not corner_file.exists()


def test_library_limits_report_handles_truncated_and_unwritable_output(tmp_path):
    from klayout_tools import place_and_route_library_limits as limits

    start = limits.SLEW_BEGIN
    output = start + "\n  pin/Z (VIOLATED)\n"
    assert (
        limits.library_limit_fields(output, tmp_path, None)[
            "max_transition_violation_count_vs_library"
        ]
        == 0
    )
    unavailable = tmp_path / "not-a-directory"
    unavailable.write_text("existing file")
    result = limits.library_limit_fields(output + limits.SLEW_END, unavailable, None)
    assert result["max_transition_violation_count_vs_library"] == 1
    assert result["library_limit_report_path"] is None
    assert unavailable.read_text() == "existing file"
