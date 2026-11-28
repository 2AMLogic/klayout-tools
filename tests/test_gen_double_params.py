"""``klt gen``'s ``double`` parameters refuse non-finite and overflowing values
(issue #8104).

``gen._coerce_param``'s ``TypeDouble`` row is the one coercion point klt's
own generators (``klt gen <generator>``) and the vendor PCell path
(``klt gen --pdk-pcell``) share. Guarding it with ``is_number`` only excluded
``bool``: an integer too large for a ``float`` escaped as a raw
``OverflowError`` instead of the ``GenError`` envelope, and ``NaN`` /
``Infinity`` were accepted and handed to the PCell as-is. The row now guards
with ``is_finite_number``, so every one of those raises the existing
field-specific ``GenError`` and nothing else changes.

The PDK is **fabricated** under ``tmp_path`` (a bare ``sky130A/libs.tech``
probe) with the host search space isolated, as in ``test_gen.py``, so the
CLI cases are hermetic; the coercion cases need no PDK at all. The CLI
literals are the raw JSON spellings Python's ``json`` decoder accepts as-is:
a 401-digit integer literal becomes an ``int`` too large for a ``float``,
and ``1e400``, ``NaN``, ``Infinity`` and ``-Infinity`` decode to non-finite
floats.
"""

import json

import klayout.db as kdb
import pytest

from helpers.pdk_install_fixtures import isolate_pdk_search_space, make_install
from klayout_tools.cli import main
from klayout_tools.gen import GenError, _coerce_param

_TYPE_DOUBLE = kdb.PCellParameterDeclaration.TypeDouble

_OVERSIZED_INT = str(10**400)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    isolate_pdk_search_space(monkeypatch, tmp_path)


@pytest.fixture()
def pdk_root(tmp_path):
    return make_install(tmp_path / "pdk_install", "sky130A", assets=())


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(2, id="int"),
        pytest.param(-3, id="negative-int"),
        pytest.param(0, id="zero"),
        pytest.param(1.5, id="float"),
    ],
)
def test_finite_numbers_coerce_to_float(value):
    result = _coerce_param("mos_array", "w_um", value, _TYPE_DOUBLE)

    assert isinstance(result, float)
    assert result == float(value)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(10**400, id="int-too-large-for-float"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="infinity"),
        pytest.param(float("-inf"), id="-infinity"),
    ],
)
def test_non_finite_double_is_gen_error(value):
    with pytest.raises(GenError) as excinfo:
        _coerce_param("mos_array", "w_um", value, _TYPE_DOUBLE)

    assert str(excinfo.value) == "generator 'mos_array': params.w_um must be a number"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(True, id="true"),
        pytest.param(False, id="false"),
        pytest.param("2", id="numeric-string"),
        pytest.param("wide", id="string"),
    ],
)
def test_non_numbers_are_still_refused(value):
    with pytest.raises(GenError) as excinfo:
        _coerce_param("mos_array", "l_um", value, _TYPE_DOUBLE)

    assert str(excinfo.value) == "generator 'mos_array': params.l_um must be a number"


@pytest.mark.parametrize(
    "literal",
    [
        pytest.param(_OVERSIZED_INT, id="oversized-int"),
        pytest.param("1e400", id="overflowing-exponent"),
        pytest.param("NaN", id="nan"),
        pytest.param("Infinity", id="infinity"),
        pytest.param("-Infinity", id="-infinity"),
    ],
)
def test_cli_non_finite_double_is_application_error(
    tmp_path, pdk_root, capsys, literal
):
    output = tmp_path / "mos.gds"
    exit_code = main(
        [
            "gen",
            "mos_array",
            "--params",
            f'{{"w_um": {literal}}}',
            "--pdk",
            "sky130A",
            "--pdk-root",
            str(pdk_root),
            "-o",
            str(output),
            "--format",
            "json",
        ]
    )
    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["command"] == "gen"
    assert err["error"]["message"] == (
        "generator 'mos_array': params.w_um must be a number"
    )
    assert not output.exists()
