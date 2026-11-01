"""``models.param_file`` (issue #4759): resolved like ``models.lib`` and
``.include``d ahead of the ``.lib`` cards of every generated deck."""

from pathlib import Path

import pytest

from klayout_tools import sim


def _deck(tmp_path: Path, point) -> list[str]:
    deck_path = tmp_path / "deck.spice"
    sim._write_corner_deck(
        deck_path=str(deck_path),
        netlist_path=str(tmp_path / "body.spice"),
        models_lib="/m/models.lib",
        point=point,
        analysis={"kind": "tran", "args": "1n 1u"},
        measurements_spec=[],
        raw_path=None,
    )
    return deck_path.read_text().splitlines()


def test_unset_leaves_lib_lines_unchanged():
    point = sim.CornerPoint("tt", {}, 27)
    assert sim._corner_lib_lines(point, "/m/models.lib") == [".lib /m/models.lib tt"]


def test_include_precedes_lib_cards(tmp_path):
    point = sim.CornerPoint("tt", {}, 27)
    point.param_file = "/m/design.ngspice"
    lines = _deck(tmp_path, point)
    inc = lines.index(".include /m/design.ngspice")
    assert inc < lines.index(".lib /m/models.lib tt")


def test_include_precedes_multi_section_libs():
    point = sim.CornerPoint("typ", {}, 27, process_sections=["nmos_t", "pmos_t"])
    point.param_file = "/m/design.ngspice"
    assert sim._corner_lib_lines(point, "/m/models.lib") == [
        ".include /m/design.ngspice",
        ".lib /m/models.lib nmos_t",
        ".lib /m/models.lib pmos_t",
    ]


def test_xyce_deck_has_include_first(tmp_path):
    point = sim.CornerPoint("tt", {}, 27)
    point.param_file = "/m/design.ngspice"
    deck_path = tmp_path / "x.cir"
    sim._write_xyce_deck(
        deck_path=str(deck_path),
        netlist_path=str(tmp_path / "body.spice"),
        models_lib="/m/models.lib",
        point=point,
        analysis={"kind": "tran", "args": "1n 1u"},
        measurements_spec=[],
    )
    lines = deck_path.read_text().splitlines()
    assert lines.index(".include /m/design.ngspice") < lines.index(
        ".lib /m/models.lib tt"
    )


def test_resolve_sets_every_point_relative_to_request_dir(tmp_path):
    (tmp_path / "design.ngspice").write_text(".param fnoicor=1\n")
    points = [sim.CornerPoint("tt", {}, 27), sim.CornerPoint("ss", {}, 27)]
    path = sim._resolve_param_file(
        points, {"param_file": "design.ngspice"}, str(tmp_path)
    )
    assert path == str(tmp_path / "design.ngspice")
    assert all(p.param_file == path for p in points)


def test_resolve_unset_is_none(tmp_path):
    point = sim.CornerPoint("tt", {}, 27)
    assert sim._resolve_param_file([point], {}, str(tmp_path)) is None
    assert point.param_file is None


def test_resolve_missing_file_is_a_sim_error(tmp_path):
    with pytest.raises(sim.SimError, match="model parameter file not found"):
        sim._resolve_param_file(
            [sim.CornerPoint("tt", {}, 27)], {"param_file": "nope"}, str(tmp_path)
        )
