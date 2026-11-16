"""`request.macros[].keepout` (issue #5313): a placement-only macro keep-out
that is independent of the PDN halo."""

from __future__ import annotations

from pathlib import Path

import pytest

from klayout_tools import place_and_route
from klayout_tools.place_and_route_request import PlaceAndRouteError
from klayout_tools.place_and_route_script import (
    _macro_blockage_lines,
    _macro_pdn_grid_lines,
)


def _lef(path: Path, size: str = "  SIZE 5.0 BY 3.0 ;\n") -> None:
    path.write_text(
        "VERSION 5.7 ;\nMACRO blk\n  CLASS BLOCK ;\n"
        + size
        + "  PIN VOUT\n    DIRECTION OUTPUT ;\n    USE ANALOG ;\n  END VOUT\n"
        "END blk\nEND LIBRARY\n",
        encoding="utf-8",
    )


def _macros(tmp_path: Path, **extra) -> list[dict]:
    _lef(tmp_path / "m.lef")
    entry = {"instance": "u1", "lef": "m.lef", "x_um": 10, "y_um": 20, **extra}
    return place_and_route._validate_macros([entry], str(tmp_path))


def test_keepout_absent_is_none_and_emits_nothing(tmp_path):
    macros = _macros(tmp_path)
    assert macros[0]["keepout"] is None
    assert _macro_blockage_lines(macros) == []


def test_keepout_only_emits_one_blockage_and_no_pdn(tmp_path):
    macros = _macros(tmp_path, keepout=4)
    assert _macro_blockage_lines(macros) == [
        "create_blockage -region {6.0 16.0 19.0 27.0}"
    ]
    assert macros[0]["halo"] is None
    assert _macro_pdn_grid_lines(macros, {"straps": []}) == []


def test_halo_only_is_unchanged(tmp_path):
    macros = _macros(tmp_path, halo=1)
    assert _macro_blockage_lines(macros) == [
        "create_blockage -region {9.0 19.0 16.0 24.0}"
    ]


def test_both_use_per_side_max_and_pdn_halo_stays_halo(tmp_path):
    macros = _macros(
        tmp_path, halo={"left": 1, "right": 6}, keepout={"left": 3, "top": 2}
    )
    assert _macro_blockage_lines(macros) == [
        "create_blockage -region {7.0 20.0 21.0 25.0}"
    ]
    assert macros[0]["halo"] == {"left": 1.0, "bottom": 0.0, "right": 6.0, "top": 0.0}


def test_rotated_macro_swaps_footprint(tmp_path):
    macros = _macros(tmp_path, keepout=1, orientation="R90")
    assert _macro_blockage_lines(macros) == [
        "create_blockage -region {9.0 19.0 14.0 26.0}"
    ]


def test_missing_lef_size_is_named_error(tmp_path):
    _lef(tmp_path / "m.lef", size="")
    macros = place_and_route._validate_macros(
        [{"instance": "u1", "lef": "m.lef", "x_um": 0, "y_um": 0, "keepout": 2}],
        str(tmp_path),
    )
    with pytest.raises(PlaceAndRouteError, match=r"keepout for instance 'u1'"):
        _macro_blockage_lines(macros)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (-1, r"keepout must not be negative"),
        ({"left": -1}, r"keepout\.left must not be negative"),
        ({}, r"keepout must name at least one of"),
        ({"sideways": 1}, r"keepout has unknown side"),
        (True, r"keepout must be a number or an object"),
        ("3", r"keepout must be a number"),
    ],
)
def test_keepout_validation_errors(tmp_path, value, message):
    with pytest.raises(PlaceAndRouteError, match=message):
        _macros(tmp_path, keepout=value)
