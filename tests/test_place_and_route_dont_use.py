"""Caller-supplied OpenROAD exclusions across validation, scripts and reports."""

import pytest

from klayout_tools.place_and_route import PlaceAndRouteError, run_place_and_route
from klayout_tools.place_and_route_request import _validate_library
from test_place_and_route import (
    _setup_success_env,
    _stub_merge_def_to_gds,
    _stub_openroad_success,
)
from test_place_and_route_library import _stage_lines

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

SKY = "sky130_fd_sc_hd"
DEFAULTS = [SKY + "__lpflow_*", SKY + "__probe*"]


@pytest.mark.parametrize(
    "value", [None, "BUF*", [], [""], [3], ["a b"], ["a;b"], ["a\n"], ["a{b}"]]
)
def test_invalid_pattern_list(value):
    with pytest.raises(PlaceAndRouteError, match=r"request\.library\.dont_use"):
        _validate_library({"dont_use": value})


@pytest.mark.parametrize("cell_library", [SKY, "gf180mcu_fd_sc_mcu9t5v0"])
@pytest.mark.parametrize("stage", ["floorplan", "place", "cts", "route"])
def test_stage_script_order(cell_library, stage):
    defaults = DEFAULTS if cell_library == SKY else []
    patterns = ["AOI[12]?", "BUF_?!", *defaults, "AOI[12]?"]
    library = _validate_library({"dont_use": patterns})
    baseline = _stage_lines(cell_library, stage)
    lines = _stage_lines(cell_library, stage, library=library)
    expected = [f"set_dont_use {{{p}}}" for p in [*defaults, "AOI[12]?", "BUF_?!"]]
    if stage == "floorplan":
        assert lines == baseline
        return
    end = lines.index("read_liberty c.lib") + 1
    assert lines[end : end + len(expected)] == expected
    assert [line for line in lines if line.startswith("set_dont_use ")] == expected
    assert baseline == lines[: end + len(defaults)] + lines[end + len(expected) :]


def setup_run(tmp_path, monkeypatch, library, *, cells):
    path = _setup_success_env(tmp_path, monkeypatch, library=library)
    (liberty,) = (tmp_path / "install").rglob("*.lib")
    if cells is not None:
        text = liberty.read_text()
        body = "".join(f'cell ("{name}") {{ }}\n' for name in cells)
        liberty.write_text("library (test) {\n" + text + body + "}\n")
    return path


@pytest.mark.parametrize(
    ("library", "pattern", "cell"),
    [
        ({"dont_use": ["*buf_4"]}, "*buf_4", SKY + "__buf_4"),
        ({"dont_use": ["*diode_?"]}, "*diode_?", SKY + "__diode_2"),
        ({"dont_use": ["BUF?"], "cts_buffer": "BUF1"}, "BUF?", "BUF1"),
        ({"dont_use": ["D?"], "antenna_diode": {"cell": "D2", "pin": "A"}}, "D?", "D2"),
    ],
)
def test_conflicts_name_pattern_and_active_cell(
    tmp_path, monkeypatch, library, pattern, cell
):
    path = setup_run(tmp_path, monkeypatch, library, cells=[cell])
    with pytest.raises(PlaceAndRouteError, match=r"request\.library\.dont_use") as exc:
        run_place_and_route(path)
    assert pattern in str(exc.value) and cell in str(exc.value)


def test_zero_matches_in_resolved_liberty(tmp_path, monkeypatch):
    path = setup_run(tmp_path, monkeypatch, {"dont_use": ["buf*"]}, cells=["BUF1"])
    with pytest.raises(
        PlaceAndRouteError, match=r"request\.library\.dont_use.*buf\*.*no cell"
    ):
        run_place_and_route(path)


@pytest.mark.parametrize(
    "known_cells", [None, [SKY + "__diode_2", "AOI2", SKY + "__probe1"]]
)
def test_successful_response_reports_only_requested_exclusions(
    tmp_path, monkeypatch, known_cells
):
    patterns = [DEFAULTS[1], "AOI[12]", "*diode_?", "AOI[12]"]
    library = {"dont_use": patterns, "antenna_diode": None}
    path = setup_run(tmp_path, monkeypatch, library, cells=known_cells)
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)
    assert run_place_and_route(path)["cell_exclusions"] == {
        "requested": patterns[:-1],
        "library_defaults": DEFAULTS,
        "effective": [*DEFAULTS, "AOI[12]", "*diode_?"],
    }


def test_omitted_field_has_no_report(tmp_path, monkeypatch):
    path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)
    assert "cell_exclusions" not in run_place_and_route(path)
