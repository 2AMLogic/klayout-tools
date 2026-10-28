"""ASAP7 resistor/capacitor recognition and LVS (issue #4469).

Synthetic geometry only: an NFET plus a klt-marker M1 resistor (112/0) and a
klt-marker two-plate MOM capacitor (113/0). The coefficients are the deck's
labelled-assumed values, so expected R/C are derived from them.
"""

from __future__ import annotations

import json

import klayout.db as kdb
import pytest

from klayout_tools.decks.asap7 import (
    ASSUMED_M1_SHEET_RHO_OHM_SQ,
    ASSUMED_MOM_AREA_CAP_F_UM2,
)
from klayout_tools.extract import ExtractError, run_extract
from klayout_tools.lvs import run_lvs
from test_asap7_finfet import (
    M1,
    M1_PIN,
    _draw_device,
    _errors,
    _static_build_identity,  # noqa: F401  (fixture used via pytestmark)
    _write,
)

RES_MK, CAP_MK = (112, 0), (113, 0)
DBU = 0.00025
# Resistor segment: 400 dbu long x 36 dbu wide (0.1 um x 0.009 um).
R_OHM = ASSUMED_M1_SHEET_RHO_OHM_SQ * 400 / 36
# Capacitor marker footprint: 460 x 200 dbu.
C_F = ASSUMED_MOM_AREA_CAP_F_UM2 * (460 * DBU) * (200 * DBU)
_NCARD = "M0 D G S vsubs nmos_rvt w=81n l=20n nfin=3"


pytestmark = pytest.mark.usefixtures("_static_build_identity")


def _layout(*, res=True, cap=True, res_marker=None, cap_marker=None) -> kdb.Layout:
    lay = _draw_device()
    top = lay.top_cell()

    def box(ld, *a):
        top.shapes(lay.layer(*ld)).insert(kdb.Box(*a))

    def text(s, x, y):
        top.shapes(lay.layer(*M1_PIN)).insert(kdb.Text(s, kdb.Trans(x, y)))

    if res:
        box(M1, 2000, 2000, 3000, 2036)
        box(RES_MK, *(res_marker or (2300, 1990, 2700, 2046)))
        text("R1", 2010, 2018)
        text("R2", 2990, 2018)
    if cap:
        box(M1, 2000, 3000, 2200, 3200)
        box(M1, 2260, 3000, 2460, 3200)
        box(CAP_MK, *(cap_marker or (2000, 3000, 2460, 3200)))
        text("C1", 2100, 3100)
        text("C2", 2360, 3100)
    return lay


def _by_class(report):
    return {d["class"]: d for d in report["devices"]}


def test_extract_emits_resistor_and_capacitor_beside_the_fet(tmp_path):
    report = run_extract(
        _write(_layout(), tmp_path), "asap7", output=str(tmp_path / "o.sp")
    )
    devices = _by_class(report)
    assert set(devices) == {"nmos_rvt", "RES", "CAP"}
    assert devices["RES"]["params"]["r_ohm"] == pytest.approx(R_OHM, rel=1e-6)
    assert devices["RES"]["nets"] == {"a": "R1", "b": "R2"}
    assert devices["CAP"]["params"]["c_f"] == pytest.approx(C_F, rel=1e-6)
    spice = (tmp_path / "o.sp").read_text()
    assert "R$" in spice and "C$" in spice


def _lvs(tmp_path, ref_lines, layout=None):
    ref = tmp_path / "ref.cdl"
    ref.write_text(f".SUBCKT TOP S G D R1 R2 C1 C2\n{ref_lines}\n.ENDS\n")
    request = {
        "layout": {
            "file": _write(layout or _layout(), tmp_path),
            "deck": "asap7",
            "top": "TOP",
        },
        "reference": {"netlist": str(ref), "top": "TOP"},
    }
    return run_lvs(json.dumps(request))


def _ref(r=R_OHM, c=C_F, fet=True, passives=True):
    lines = [_NCARD] if fet else []
    if passives:
        lines += [f"R1 R1 R2 {r!r}", f"C1 C1 C2 {c!r}"]
    return "\n".join(lines)


def test_lvs_matches_with_passives_in_reference(tmp_path):
    report = _lvs(tmp_path, _ref())
    assert report["status"] == "match", report["mismatches"]


def test_lvs_device_only_case_still_matches(tmp_path):
    layout = _layout(res=False, cap=False)
    ref = tmp_path / "ref.cdl"
    ref.write_text(f".SUBCKT TOP S G D\n{_NCARD}\n.ENDS\n")
    report = run_lvs(
        json.dumps(
            {
                "layout": {
                    "file": _write(layout, tmp_path),
                    "deck": "asap7",
                    "top": "TOP",
                },
                "reference": {"netlist": str(ref), "top": "TOP"},
            }
        )
    )
    assert report["status"] == "match", report["mismatches"]


@pytest.mark.parametrize("kwargs", [{"r": R_OHM * 2}, {"c": C_F * 3}])
def test_lvs_wrong_passive_value_is_a_mismatch(tmp_path, kwargs):
    report = _lvs(tmp_path, _ref(**kwargs))
    assert report["status"] != "match"
    assert _errors(report)


def test_lvs_missing_passives_in_reference_is_a_mismatch(tmp_path):
    assert _lvs(tmp_path, _ref(passives=False))["status"] != "match"


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        # Resistor marker over a single-terminal stub (metal ends in the marker).
        ({"res_marker": (2000, 1990, 3000, 2046)}, "terminal"),
        # Marker over a capacitor drawn as one merged plate.
        ({"cap_marker": (2000, 3000, 2700, 3200)}, None),
    ],
)
def test_malformed_markers_are_named_findings(tmp_path, kwargs, fragment):
    if "cap_marker" in kwargs:
        lay = _layout(cap=False)
        top = lay.top_cell()
        top.shapes(lay.layer(*M1)).insert(kdb.Box(2000, 3000, 2700, 3200))
        top.shapes(lay.layer(*CAP_MK)).insert(kdb.Box(*kwargs["cap_marker"]))
        fragment = "port"
    else:
        lay = _layout(**kwargs)
    with pytest.raises(ExtractError, match=fragment):
        run_extract(_write(lay, tmp_path), "asap7", output=str(tmp_path / "o.sp"))


def test_marker_straddling_two_metal_levels_is_a_finding(tmp_path):
    lay = _layout()
    lay.top_cell().shapes(lay.layer(20, 0)).insert(kdb.Box(2400, 1990, 2500, 2046))
    with pytest.raises(ExtractError, match="straddles"):
        run_extract(_write(lay, tmp_path), "asap7", output=str(tmp_path / "o.sp"))
