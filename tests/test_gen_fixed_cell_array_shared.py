"""The fixed-cell array implementation ``cap_vpp_array`` and ``pdk_cell``
share (issue #8081).

``pdk_cell`` generalizes ``cap_vpp_array`` (``gen_pdk_cell``'s docstring), so
the PCell factory, the array-parameter validation and the cell lookup with its
pitch-overlap guard live once -- in :mod:`klayout_tools.gen_pcells.cap_vpp_array`
and :mod:`klayout_tools.gen_cap_vpp` -- and both generators delegate to them.
These tests pin the sharing itself, so a copied-back ``produce_impl`` or a
second validation loop cannot reappear unnoticed, and pin the public contract
the sharing must leave byte-identical: the ``klt gen --list`` parameter array
and every error message the shared helpers emit.
"""

from __future__ import annotations

import pytest

from klayout_tools.gen import GenError, list_generators
from klayout_tools.gen_cap_vpp import _lookup_cell, _validate_array_params
from klayout_tools.gen_pcells import (
    _build_cap_vpp_array_pcell,
    _build_pdk_cell_pcell,
)

_PITCH_DESC = "Instance pitch (um); 0 abuts at the cell's bbox width"

#: ``klt gen --list``'s ``params`` array per generator: (name, type, default,
#: description) in declaration order.
_LISTED_PARAMS = {
    "cap_vpp_array": [
        (
            "pdk_cell",
            "string",
            "sky130_fd_pr__cap_vpp_02p4x04p6_m1m2_noshield",
            "PDK fixed capacitor cell name (sky130_fd_pr__cap_vpp_*)",
        ),
        ("num", "int", 4, "Number of cell instances in the row"),
        ("pitch_um", "double", 0.0, _PITCH_DESC),
    ],
    "pdk_cell": [
        (
            "library",
            "string",
            "",
            "PDK library directory under libs.ref (e.g. sg13g2_io)",
        ),
        ("pdk_cell", "string", "", "PDK fixed cell name"),
        ("num", "int", 1, "Number of cell instances in the row"),
        ("pitch_um", "double", 0.0, _PITCH_DESC),
    ],
}


def test_pcell_geometry_and_display_text_are_one_shared_implementation():
    cap = _build_cap_vpp_array_pcell()["cap_vpp_array"]
    pdk = _build_pdk_cell_pcell()["pdk_cell"]
    for method in ("produce_impl", "display_text_impl"):
        assert getattr(cap, method).__code__ is getattr(pdk, method).__code__


@pytest.mark.parametrize("name", sorted(_LISTED_PARAMS))
def test_listed_params_are_unchanged(name):
    gen = next(g for g in list_generators()["generators"] if g["name"] == name)
    listed = [
        (p["name"], p["type"], p["default"], p["description"]) for p in gen["params"]
    ]
    assert listed == _LISTED_PARAMS[name]


def test_validate_array_params_names_the_generator():
    with pytest.raises(GenError) as exc:
        _validate_array_params("cap_vpp_array", {"num": 0, "pitch_um": 0.0})
    assert str(exc.value) == "generator 'cap_vpp_array': params.num must be >= 1"
    with pytest.raises(GenError) as exc:
        _validate_array_params("pdk_cell", {"num": 1, "pitch_um": -0.5})
    assert str(exc.value) == "generator 'pdk_cell': params.pitch_um must be >= 0"


def _params(**overrides):
    return {"pdk_cell": "a", "num": 2, "pitch_um": 0.0} | overrides


def _table():
    return {"a": {"bbox_um": [-1.0, 0.0, 9.0, 10.0]}, "b": {"bbox_um": [0, 0, 5, 5]}}


def test_lookup_cell_unknown_name_lists_available_with_scope():
    with pytest.raises(GenError) as exc:
        _lookup_cell("cap_vpp_array", _table(), _params(pdk_cell="zz"))
    assert str(exc.value) == (
        "generator 'cap_vpp_array': unknown params.pdk_cell 'zz' -- available: a, b"
    )
    with pytest.raises(GenError) as exc:
        _lookup_cell(
            "pdk_cell", _table(), _params(pdk_cell="zz"), scope=" in library 'io'"
        )
    assert str(exc.value) == (
        "generator 'pdk_cell': unknown params.pdk_cell 'zz' in library 'io'"
        " -- available: a, b"
    )


def test_lookup_cell_rejects_pitch_narrower_than_the_cell_width():
    with pytest.raises(GenError) as exc:
        _lookup_cell("cap_vpp_array", _table(), _params(pitch_um=9.999))
    assert str(exc.value) == (
        "generator 'cap_vpp_array': params.pitch_um (9.999um) is smaller than the "
        "cell width (10um) -- instances would overlap; use 0 to abut or a pitch "
        ">= the cell width"
    )
    # 0 (abut), the cell width itself and any single instance are all allowed.
    for allowed in (
        _params(pitch_um=0.0),
        _params(pitch_um=10.0),
        _params(pitch_um=0.5, num=1),
    ):
        assert _lookup_cell("cap_vpp_array", _table(), allowed) == _table()["a"]
