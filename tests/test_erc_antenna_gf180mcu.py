"""gf180mcu antenna-ratio limit table for `klt erc --pdk gf180mcu` (issue
#4003). Split out of `tests/test_erc.py` to respect the per-file size ratchet;
the fixtures mirror that module's ihp-sg13g2 helpers."""

from __future__ import annotations

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.erc import run_erc

DBU = 0.001


def _um(v: float) -> int:
    return int(round(v / DBU))


def _fixture(path, *, metal1_um2: float) -> None:
    """1 um^2 gate strapped through a contact to a metal bar whose area is
    ``metal1_um2`` (1 um tall, so width == area)."""
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(1, 0)).insert(kdb.Box.new(_um(0), _um(0), _um(1), _um(1)))
    top.shapes(layout.layer(2, 0)).insert(
        kdb.Box.new(_um(0.1), _um(0.1), _um(0.3), _um(0.3))
    )
    top.shapes(layout.layer(3, 0)).insert(
        kdb.Box.new(_um(0), _um(0), _um(metal1_um2), _um(1))
    )
    layout.write(str(path))


def _spec(path, metal_name: str = "Metal1") -> None:
    path.write_text(
        json.dumps(
            {
                "stackup": [
                    {"name": "GatPoly", "layer": "1/0", "role": "gate"},
                    {"name": metal_name, "layer": "3/0"},
                ],
                "vias": [
                    {
                        "name": "Cont",
                        "layer": "2/0",
                        "between": ["GatPoly", metal_name],
                    }
                ],
            }
        )
    )


@pytest.mark.parametrize(
    ("metal", "rule"),
    [
        ("Metal1", "ANT.16_i_ANT.2"),
        ("Metal5", "ANT.16_i_ANT.6"),
        ("MetalTop", "ANT.16_i_ANT.7"),
    ],
)
# The cumulative ratio counts the 1 um^2 gate itself, so 399 um^2 of metal is
# exactly 400 (at the limit -> pass) and 400 um^2 is 401 (over -> violate).
@pytest.mark.parametrize(
    ("area", "verdict"),
    [(450.0, "violate"), (400.0, "violate"), (399.0, "pass"), (350.0, "pass")],
)
def test_gf180mcu_antenna_ratio_verdict(tmp_path, metal, rule, area, verdict):
    gds = tmp_path / "gf180.gds"
    spec = tmp_path / "gf180.erc.json"
    _fixture(gds, metal1_um2=area)
    _spec(spec, metal_name=metal)

    report = run_erc(str(gds), str(spec), pdk="gf180mcu")

    assert report["pdk"] == "gf180mcu"
    level = next(lvl for lvl in report["gates"][0]["levels"] if lvl["layer"] == metal)
    assert level["antenna_ratio_max"] == pytest.approx(400.0)
    assert level["verdict"] == verdict
    assert "google/gf180mcu-pdk/blob/f6eeac7" in level["antenna_ratio_source"]
    assert "antenna.rb" in level["antenna_ratio_source"]
    assert f"'{rule}'" in level["antenna_ratio_source"]


def test_gf180mcu_unlisted_role_stays_unchecked_and_partial(tmp_path):
    gds = tmp_path / "gf180.gds"
    spec = tmp_path / "gf180.erc.json"
    _fixture(gds, metal1_um2=10.0)
    _spec(spec, metal_name="met1")

    report = run_erc(str(gds), str(spec), pdk="gf180mcu")

    level = next(lvl for lvl in report["gates"][0]["levels"] if lvl["layer"] == "met1")
    assert level["verdict"] == "unchecked"
    assert report["status"] == "not_checked"


def test_cli_accepts_pdk_gf180mcu(tmp_path, capsys):
    gds = tmp_path / "gf180.gds"
    spec = tmp_path / "gf180.erc.json"
    _fixture(gds, metal1_um2=10.0)
    _spec(spec)

    code = main(["erc", str(gds), str(spec), "--pdk", "gf180mcu", "--format", "json"])

    assert code in (0, 3)
    assert json.loads(capsys.readouterr().out)["pdk"] == "gf180mcu"
