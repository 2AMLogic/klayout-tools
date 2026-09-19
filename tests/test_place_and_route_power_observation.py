"""Offline DEF fixtures for #2086; these are synthetic, not live route evidence."""

import json
from pathlib import Path

import pytest

from klayout_tools import place_and_route
from klayout_tools.cli import main
from test_place_and_route import (
    _BASE_STRAPS,
    _script_write_def_path,
    _stub_merge_def_to_gds,
    _stub_openroad_success,
)
from test_place_and_route import (
    _setup_success_env as _base_env,
)


def _setup_power_env(tmp_path, monkeypatch, **kwargs):
    request = _base_env(tmp_path, monkeypatch, **kwargs)
    for path in (tmp_path / "install").rglob("*.tlef"):
        path.write_text(
            "LAYER met1\n TYPE ROUTING ;\nEND met1\n"
            "LAYER met2\n TYPE ROUTING ;\nEND met2\n"
        )
    return request


def _def_text(components, nets):
    return (
        "VERSION 5.8 ;\nDESIGN gcd ;\nUNITS DISTANCE MICRONS 1000 ;\n"
        f"COMPONENTS {len(components)} ;\n"
        + "\n".join(
            f"- u{i} {master} + {placement} ;"
            for i, (master, placement) in enumerate(components)
        )
        + "\nEND COMPONENTS\n"
        + (
            f"SPECIALNETS {len(nets)} ;\n" + "\n".join(nets) + "\nEND SPECIALNETS\n"
            if nets is not None
            else ""
        )
        + "END DESIGN\n"
    )


def _net(name, rail="met1", upper="met2", *, followpin=True, stripe=True, via=True):
    shapes = []
    if followpin:
        shapes.append(f"+ ROUTED {rail} 480 + SHAPE FOLLOWPIN ( 0 0 ) ( 1000 * )")
    if stripe:
        shapes.append(f"+ ROUTED {upper} 1000 + SHAPE STRIPE ( 0 0 ) ( * 2000 )")
    if via:
        shapes.append(f"+ ROUTED {rail} 0 ( 0 0 ) via_1")
    return f"- {name} ( * VPWR ) " + " ".join(shapes) + " + USE POWER ;"


def _sky_cells():
    return [
        (f"sky130_fd_sc_hd__{name}", "FIXED ( 0 0 ) N")
        for name in ("tapvpwrvgnd_1", "fill_1", "fill_2", "conb_1")
    ]


def _stub_def(monkeypatch, text):
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)
    base_run = place_and_route.subprocess.run

    def run(cmd, **kwargs):
        result = base_run(cmd, **kwargs)
        if len(cmd) > 5 and cmd[5].endswith(("_route.tcl", "_place.tcl", "_cts.tcl")):
            path = _script_write_def_path(cmd[5])
            if path:
                Path(path).write_text(text)
        return result

    monkeypatch.setattr(place_and_route.subprocess, "run", run)


def test_power_omission_is_visible_even_when_row_rail_fallback_runs(
    tmp_path, monkeypatch
):
    request = _setup_power_env(tmp_path, monkeypatch)
    _stub_def(monkeypatch, _def_text([], None))
    result = place_and_route.run_place_and_route(request)
    observation = result["power_observation"]
    assert observation["status"] == "incomplete"
    assert observation["counts"] == {
        "instances": 0,
        "placed_instances": 0,
        "tap": 0,
        "endcap": 0,
        "filler": 0,
        "tie": 0,
    }
    assert "power_not_requested" in {warning["code"] for warning in result["warnings"]}
    assert observation["special_nets"]["VPWR"]["present"] is False
    assert result["power"]["row_rail"]["emitted"] is True


def test_supplied_power_is_checked_from_def_not_requested_recipe(tmp_path, monkeypatch):
    request = _setup_power_env(tmp_path, monkeypatch, power={"straps": _BASE_STRAPS})
    cells = _sky_cells() + [("sky130_fd_sc_hd__fill_4", "UNPLACED")]
    _stub_def(monkeypatch, _def_text(cells, [_net("VDD"), _net("VSS")]))
    result = place_and_route.run_place_and_route(request)
    observation = result["power_observation"]
    assert observation["status"] == "structure_present"
    assert observation["counts"] == {
        "instances": 5,
        "placed_instances": 4,
        "tap": 1,
        "endcap": 0,
        "filler": 2,
        "tie": 1,
    }
    assert observation["special_nets"]["VDD"]["followpin_segments"] == 1
    assert observation["special_nets"]["VDD"]["upper_stripe_segments"] == 1
    assert observation["special_nets"]["VDD"]["via_count"] == 1
    assert result["warnings"] == []


@pytest.mark.parametrize(
    "missing", ["followpin", "stripe", "via", "net", "tap", "filler"]
)
def test_partial_supplied_power_reports_missing_structure(
    tmp_path, monkeypatch, missing
):
    request = _setup_power_env(tmp_path, monkeypatch, power={"straps": _BASE_STRAPS})
    cells = [
        cell
        for cell in _sky_cells()
        if not (missing == "tap" and "tapvpwr" in cell[0])
        and not (missing == "filler" and "__fill_" in cell[0])
    ]
    nets = [
        _net(
            "VDD",
            followpin=missing != "followpin",
            stripe=missing != "stripe",
            via=missing != "via",
        )
    ]
    if missing != "net":
        nets.append(_net("VSS"))
    _stub_def(monkeypatch, _def_text(cells, nets))
    result = place_and_route.run_place_and_route(request)
    assert result["power"]["pdn"] is True  # Invocation alone isn't evidence.
    assert result["power_observation"]["status"] == "incomplete"
    assert "power_delivery_incomplete" in {
        warning["code"] for warning in result["warnings"]
    }
    assert result["power_observation"]["issues"]


@pytest.mark.parametrize("text", ["fake def\n", "COMPONENTS 1 ;\nEND DESIGN\n"])
def test_unavailable_def_evidence_is_not_zero(tmp_path, monkeypatch, text):
    request = _setup_power_env(tmp_path, monkeypatch, power={"straps": _BASE_STRAPS})
    _stub_def(monkeypatch, text)
    result = place_and_route.run_place_and_route(request)
    assert result["power_observation"]["status"] == "unavailable"
    assert result["power_observation"]["counts"] is None
    assert result["power_observation"]["special_nets"] is None


@pytest.mark.parametrize("stage", ["floorplan", "place", "cts"])
def test_pre_route_evidence_is_honest_and_fillers_not_required(
    tmp_path, monkeypatch, stage
):
    request = _setup_power_env(
        tmp_path, monkeypatch, target_stage=stage, power={"straps": _BASE_STRAPS}
    )
    _stub_def(monkeypatch, _def_text(_sky_cells()[:1], [_net("VDD"), _net("VSS")]))
    result = place_and_route.run_place_and_route(request)
    observation = result["power_observation"]
    assert observation["status"] == (
        "unavailable" if stage == "floorplan" else "not_routed"
    )
    if stage != "floorplan":
        assert observation["counts"]["tap"] == 1
        assert observation["counts"]["filler"] == 0
        assert observation["issues"] == []


def test_text_and_json_expose_power_warning_and_observed_counts(
    tmp_path, monkeypatch, capsys
):
    request = _setup_power_env(tmp_path, monkeypatch)
    _stub_def(monkeypatch, _def_text([], None))
    assert main(["place-and-route", request, "--format", "text"]) == 0
    text = capsys.readouterr().out
    assert "WARNING [power_not_requested]" in text
    assert "tap=0" in text
    assert main(["place-and-route", request, "--format", "json"]) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["power_observation"]["counts"]["tap"] == 0
    assert result["warnings"]
    assert output.err == ""


@pytest.mark.parametrize("endcaps", [240, 0])
@pytest.mark.parametrize("declare_tie", [True, False])
def test_gf180_control_counts_and_lef_declared_ties(tmp_path, endcaps, declare_tie):
    """Synthetic DEF with the filer's positive-control totals, not its artifact.

    The 5,432 logic/seq entries include two constant drivers here solely to
    exercise declared tie classification; the filer did not report a tie count.
    """
    from klayout_tools.place_and_route_power_observation import observe_power

    library = "gf180mcu_fd_sc_mcu7t5v0"
    components = []
    for master, count in [
        ("fill_1", 9789),
        ("filltie", 244),
        ("endcap", endcaps),
        ("logic_1", 5430),
        ("constant_driver", 2),
    ]:
        components.extend([(f"{library}__{master}", "FIXED ( 0 0 ) N")] * count)
    path = tmp_path / "positive.def"
    path.write_text(
        _def_text(
            components, [_net(name, "Metal1", "Metal2") for name in ("VDD", "VSS")]
        )
    )
    cell_lef = tmp_path / "cells.lef"
    tie_class = "CORE TIEHIGH" if declare_tie else "CORE"
    cell_lef.write_text(
        f"MACRO {library}__constant_driver\n CLASS {tie_class} ;\n"
        f"END {library}__constant_driver\n"
    )
    tech_lef = tmp_path / "tech.lef"
    tech_lef.write_text(
        "LAYER Metal1\n TYPE ROUTING ;\nEND Metal1\n"
        "LAYER Metal2\n TYPE ROUTING ;\nEND Metal2\n"
    )
    observation, warnings = observe_power(
        def_path=str(path),
        unrouted_def_path=None,
        stage="route",
        power={"power_net": "VDD", "ground_net": "VSS"},
        cell_library=library,
        cell_lef=str(cell_lef),
        tech_lef=str(tech_lef),
    )
    assert observation["counts"] == {
        "instances": 15465 + endcaps,
        "placed_instances": 15465 + endcaps,
        "tap": 244,
        "endcap": endcaps,
        "filler": 9789,
        "tie": 2 if declare_tie else None,
    }
    assert observation["status"] == ("structure_present" if endcaps else "incomplete")
    assert bool(warnings) is (endcaps == 0)


def test_present_empty_special_nets_are_measured_zero(tmp_path, monkeypatch):
    request = _setup_power_env(tmp_path, monkeypatch, power={"straps": _BASE_STRAPS})
    _stub_def(monkeypatch, _def_text(_sky_cells(), ["- VDD ;", "- VSS ;"]))
    result = place_and_route.run_place_and_route(request)
    observation = result["power_observation"]
    assert observation["status"] == "incomplete"
    assert observation["special_nets"]["VDD"]["present"] is True
    assert observation["special_nets"]["VDD"]["via_count"] == 0


def test_pdn_via_array_counts_placements_not_definitions(tmp_path, monkeypatch):
    request = _setup_power_env(tmp_path, monkeypatch, power={"straps": _BASE_STRAPS})
    nets = [
        _net(name).replace("via_1", "via_1 DO 2 BY 3 STEP 100 100")
        for name in ("VDD", "VSS")
    ]
    _stub_def(monkeypatch, _def_text(_sky_cells(), nets))
    result = place_and_route.run_place_and_route(request)
    assert result["power_observation"]["special_nets"]["VDD"]["via_count"] == 6


@pytest.mark.parametrize(
    "suffix",
    [
        "SPECIALNETS 2\nEND SPECIALNETS\n",
        "SPECIALNETS 1 ;\n- VDD + ROUTED bad_layer 0 ( 0 0 ) via1 ;\nEND SPECIALNETS\n",
    ],
)
def test_unparseable_pdn_keeps_measured_cell_counts(tmp_path, monkeypatch, suffix):
    request = _setup_power_env(tmp_path, monkeypatch, power={"straps": _BASE_STRAPS})
    text = _def_text(_sky_cells(), None).replace("END DESIGN", suffix + "END DESIGN")
    _stub_def(monkeypatch, text)
    result = place_and_route.run_place_and_route(request)
    assert result["power_observation"]["status"] == "unavailable"
    assert result["power_observation"]["counts"]["tap"] == 1
    assert result["power_observation"]["special_nets"] is None


def test_custom_power_net_names_are_inspected(tmp_path, monkeypatch):
    power = {"straps": _BASE_STRAPS, "power_net": "VCCA", "ground_net": "GNDA"}
    request = _setup_power_env(tmp_path, monkeypatch, power=power)
    _stub_def(monkeypatch, _def_text(_sky_cells(), [_net("VCCA"), _net("GNDA")]))
    result = place_and_route.run_place_and_route(request)
    assert set(result["power_observation"]["special_nets"]) == {"VCCA", "GNDA"}
    assert result["power_observation"]["status"] == "structure_present"
