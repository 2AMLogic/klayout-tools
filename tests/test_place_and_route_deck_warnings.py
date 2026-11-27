"""Deck-known configuration warnings for ``klt place-and-route`` (issue #7311).

Two accepted ``request.power`` shapes are known to produce geometry that
``klt drc --deck gf180mcu`` rejects, yet a run using them reported
``status: "ok"`` with an empty ``warnings[]``: an explicit
``power.connects[].split_cuts`` (#4301), and the per-library 100 um default
``tapcell -distance`` pitch against the deck's 15 um 5 V/6 V tap-distance
rules. ``place_and_route_deck_warnings`` derives a ``warnings[]`` entry for
each from the validated request alone -- no DRC run, no refusal, no default
change; ``status``, exit code and ``route_drc_violation_count`` are unchanged.

Pure-function tests, same split as ``test_place_and_route_latch_warning.py``
(no OpenROAD needed), plus stubbed-OpenROAD checks that the strings reach
``response["warnings"]`` and stderr. Fixtures come from the parent module.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools.cli import main
from klayout_tools.decks import TapDistanceSpec, get_deck
from klayout_tools.place_and_route import run_place_and_route
from klayout_tools.place_and_route_deck_warnings import (
    GF180MCU_MV_LIBRARIES,
    MEASURED_DIRTY_SPLIT_CUTS,
    SPLIT_CUTS_DRC_RULES,
    TAP_DISTANCE_MV_RULES,
    deck_warnings,
    gf180mcu_mv_tap_distance_um,
    split_cuts_warning,
    tap_distance_warning,
)
from test_place_and_route import (
    _setup_gf180mcu_7t_success_env,
    _setup_gf180mcu_success_env,
    _stub_merge_def_to_gds,
    _stub_openroad_success,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_NINE_TRACK, _SEVEN_TRACK = GF180MCU_MV_LIBRARIES
_OTHER_LIBRARIES = ("sky130_fd_sc_hd", "asap7sc7p5t_28", "sg13g2_stdcell")
_M1_M4 = ("Metal1", "Metal4")
_ORFS_SPLIT_CUTS = {"layer": "Metal3", "width_um": 0.128}


def _connect(layers: tuple[str, str] = _M1_M4, split_cuts: dict | None = None) -> dict:
    """One ``power.connects[]`` echo entry in the response's own shape."""
    return {
        "layers": list(layers),
        "max_columns": 5,
        "ongrid": ["Metal2", "Metal3", "Metal4"],
        "split_cuts": split_cuts,
    }


def _power_info(
    connects: list[dict] | None = None, tapcell_distance_um: float | None = 100
) -> dict:
    """The response's ``power`` object, reduced to the two fields read."""
    return {
        "connects": [_connect()] if connects is None else connects,
        "tapcell_distance_um": tapcell_distance_um,
    }


def _power(tapcell_distance_um: float | None = None) -> dict:
    """The validated ``request.power`` block, reduced to the field read."""
    return {"tapcell_distance_um": tapcell_distance_um}


# --------------------------------------------------------------------------- #
# The two strings
# --------------------------------------------------------------------------- #


def test_split_cuts_warning_names_value_rules_and_issue():
    """Names the layer/width applied and the strap pair, all seven rule ids
    the measured run tripped, and #4301 -- and stays honest about the
    evidence: measured for the ORFS value only, unverified for others."""
    text = split_cuts_warning("Metal3", 0.128, "Metal1", "Metal4")

    assert text.startswith("gf180mcu deck warning (issue #7311): ")
    assert "-split_cuts {Metal3 0.128} between Metal1 and Metal4" in text
    for rule in SPLIT_CUTS_DRC_RULES:
        assert rule in text
    assert "issue #4301" in text
    assert "measured DRC-dirty" in text
    assert "unverified" in text
    assert "gf180mcu_9t_6M" in text and "gf180mcu_7t_6M" in text
    assert "route_drc_violation_count" in text
    assert "klt drc --deck gf180mcu" in text


def test_split_cuts_warning_still_cites_the_measured_value_for_any_request():
    """A value other than the measured one is named as applied, while the
    measured-dirty claim keeps naming the ORFS value it was measured on."""
    layer, width = MEASURED_DIRTY_SPLIT_CUTS

    text = split_cuts_warning("Metal2", 0.2, "Metal1", "Metal4")

    assert "-split_cuts {Metal2 0.2}" in text
    assert f"measured DRC-dirty for -split_cuts {{{layer} {width}}}" in text


def test_split_cuts_drc_rules_are_the_seven_reported_rule_ids():
    assert SPLIT_CUTS_DRC_RULES == (
        "metal2.ongrid.1",
        "metal3.ongrid.1",
        "metal4.ongrid.1",
        "via2.ongrid.1",
        "via3.ongrid.1",
        "via2.width.1",
        "via3.width.1",
    )
    assert MEASURED_DIRTY_SPLIT_CUTS == ("Metal3", 0.128)


def test_tap_distance_warning_names_rules_pitch_and_default_source():
    text = tap_distance_warning(100, 15, None)

    assert text.startswith("gf180mcu deck warning (issue #7311): ")
    assert "effective tapcell pitch 100 um" in text
    assert "per-library default" in text
    assert "exceeds the 15 um" in text
    for rule in TAP_DISTANCE_MV_RULES:
        assert rule in text
    assert "may fail" in text
    assert "placement input" in text
    assert "request.power.tapcell.distance_um" in text
    assert "klt drc --deck gf180mcu" in text


def test_tap_distance_warning_names_the_override_as_source():
    text = tap_distance_warning(20, 15, 20)

    assert "effective tapcell pitch 20 um" in text
    assert "request.power.tapcell.distance_um override" in text
    assert "per-library default" not in text


def test_tap_distance_rule_ids_are_the_decks_mv_pair():
    assert TAP_DISTANCE_MV_RULES == (
        "comp.tap_distance.nwell.mv.1",
        "comp.tap_distance.psub.mv.1",
    )


def test_tap_distance_limit_is_read_from_the_deck_rule_table():
    """The 15 um is the deck's own number, not a second literal: both MV
    rules carry it in ``region_spec``, and the warning reads it from there
    so the two cannot drift."""
    deck_limits = {
        rule.id: rule.region_spec.max_distance_um
        for rule in get_deck("gf180mcu")
        if rule.id in TAP_DISTANCE_MV_RULES
        and isinstance(rule.region_spec, TapDistanceSpec)
    }

    assert deck_limits == {rule: 15.0 for rule in TAP_DISTANCE_MV_RULES}
    assert gf180mcu_mv_tap_distance_um() == 15.0


# --------------------------------------------------------------------------- #
# Which requests warn
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("cell_library", GF180MCU_MV_LIBRARIES)
def test_gf180mcu_split_cuts_and_default_pitch_both_warn(cell_library):
    """The issue's own shape: the ORFS connect tuning transcribed explicitly
    plus the library's default 100 um pitch produces both entries."""
    warnings = deck_warnings(
        cell_library, _power(), _power_info([_connect(split_cuts=_ORFS_SPLIT_CUTS)])
    )

    assert warnings == [
        split_cuts_warning("Metal3", 0.128, "Metal1", "Metal4"),
        tap_distance_warning(100, 15, None),
    ]


def test_split_cuts_warning_fires_once_per_tuned_pair():
    connects = [
        _connect(_M1_M4, _ORFS_SPLIT_CUTS),
        _connect(("Metal4", "Metal5")),
        _connect(("Metal1", "Metal4"), _ORFS_SPLIT_CUTS),
    ]

    warnings = deck_warnings(_SEVEN_TRACK, _power(), _power_info(connects, None))

    assert warnings == [
        split_cuts_warning("Metal3", 0.128, "Metal1", "Metal4"),
        split_cuts_warning("Metal3", 0.128, "Metal1", "Metal4"),
    ]


def test_preset_without_split_cuts_has_no_split_cuts_warning():
    """The ``gf180mcu_*_6M`` presets omit ``-split_cuts`` deliberately
    (#4301), so a preset run only carries the tap-distance entry."""
    warnings = deck_warnings(_NINE_TRACK, _power(), _power_info())

    assert warnings == [tap_distance_warning(100, 15, None)]


@pytest.mark.parametrize("distance_um", [15, 12.5, 1])
def test_tapcell_override_at_or_under_the_limit_has_no_tap_warning(distance_um):
    """Test-plan edge case: exactly the deck's 15 um does not warn."""
    warnings = deck_warnings(
        _NINE_TRACK,
        _power(distance_um),
        _power_info(tapcell_distance_um=distance_um),
    )

    assert warnings == []


def test_tapcell_override_above_the_limit_warns_naming_the_override():
    warnings = deck_warnings(
        _SEVEN_TRACK, _power(20), _power_info(tapcell_distance_um=20)
    )

    assert warnings == [tap_distance_warning(20, 15, 20)]


def test_tapless_request_shape_has_no_tap_distance_warning():
    """``request.library.tapcell: null`` places no tapcells at all: the
    effective pitch is ``null``, and ``null > 15`` is not a comparison to
    make."""
    warnings = deck_warnings(
        _NINE_TRACK, _power(), _power_info(tapcell_distance_um=None)
    )

    assert warnings == []


@pytest.mark.parametrize("cell_library", _OTHER_LIBRARIES)
@pytest.mark.parametrize(
    "connects, distance_um, override",
    [
        ([_connect(split_cuts=_ORFS_SPLIT_CUTS)], 100, None),
        ([_connect(split_cuts=_ORFS_SPLIT_CUTS)], 20, 20),
        ([_connect()], 100, None),
    ],
)
def test_non_gf180mcu_libraries_never_warn(
    cell_library, connects, distance_um, override
):
    """Both triggers are gf180mcu deck facts; sky130/asap7/sg13g2 runs with
    the same shapes stay silent."""
    warnings = deck_warnings(
        cell_library,
        _power(override),
        _power_info(connects, distance_um),
    )

    assert warnings == []


@pytest.mark.parametrize("cell_library", (*GF180MCU_MV_LIBRARIES, *_OTHER_LIBRARIES))
def test_power_omitted_never_warns(cell_library):
    """Without ``request.power`` neither trigger can apply (the pitch is
    never emitted and there are no ``add_pdn_connect`` calls)."""
    power_info = _power_info([], None)

    assert deck_warnings(cell_library, None, power_info) == []


# --------------------------------------------------------------------------- #
# The strings reach the response and stderr (stubbed OpenROAD)
# --------------------------------------------------------------------------- #


def _run_gf180mcu(request_path, monkeypatch) -> dict:
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)
    return run_place_and_route(request_path)


def test_response_carries_both_warnings_and_status_stays_ok(tmp_path, monkeypatch):
    """Integration level: a preset whose ``connects`` override sets
    ``split_cuts`` (the test plan's preset-plus-override edge case) on the
    default pitch reaches the response's ``warnings[]`` with both strings,
    and nothing else about the run's verdict changes."""
    request_path = _setup_gf180mcu_success_env(
        tmp_path,
        monkeypatch,
        power={
            "preset": "gf180mcu_9t_6M",
            "connects": [{"layers": list(_M1_M4), "split_cuts": _ORFS_SPLIT_CUTS}],
        },
    )

    report = _run_gf180mcu(request_path, monkeypatch)

    assert report["status"] == "ok"
    assert report["stage_reached"] == "route"
    assert report["route_drc_violation_count"] == 0
    assert report["power"]["connects"][0]["split_cuts"] == _ORFS_SPLIT_CUTS
    assert report["power"]["tapcell_distance_um"] == 100
    assert split_cuts_warning("Metal3", 0.128, *_M1_M4) in report["warnings"]
    assert tap_distance_warning(100, 15, None) in report["warnings"]


def test_gf180mcu_7t_preset_run_carries_only_the_tap_distance_warning(
    tmp_path, monkeypatch
):
    request_path = _setup_gf180mcu_7t_success_env(
        tmp_path, monkeypatch, power={"preset": "gf180mcu_7t_6M"}
    )

    report = _run_gf180mcu(request_path, monkeypatch)

    assert tap_distance_warning(100, 15, None) in report["warnings"]
    assert not any("split_cuts" in w for w in report["warnings"])


def test_tapcell_override_within_the_limit_silences_the_response_warning(
    tmp_path, monkeypatch
):
    request_path = _setup_gf180mcu_success_env(
        tmp_path,
        monkeypatch,
        power={"preset": "gf180mcu_9t_6M", "tapcell": {"distance_um": 12}},
    )

    report = _run_gf180mcu(request_path, monkeypatch)

    assert report["power"]["tapcell_distance_um"] == 12
    assert not any("tap_distance" in w for w in report["warnings"])


def test_cli_mirrors_deck_warnings_to_stderr(tmp_path, monkeypatch, capsys):
    """Acceptance criterion: both strings reach stderr like every other
    ``warnings`` entry, one line each, leaving stdout one JSON document."""
    request_path = _setup_gf180mcu_success_env(
        tmp_path,
        monkeypatch,
        power={
            "preset": "gf180mcu_9t_6M",
            "connects": [{"layers": list(_M1_M4), "split_cuts": _ORFS_SPLIT_CUTS}],
        },
    )
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    exit_code = main(["place-and-route", request_path, "--format", "json"])

    assert exit_code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    stderr_warnings = [
        line.removeprefix("klt place-and-route: warning: ")
        for line in captured.err.splitlines()
    ]
    assert payload["warnings"] == stderr_warnings
    assert split_cuts_warning("Metal3", 0.128, *_M1_M4) in stderr_warnings
    assert tap_distance_warning(100, 15, None) in stderr_warnings
