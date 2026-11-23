"""gf180mcu antenna-ratio limit table for `klt erc --pdk gf180mcu` (issue
#4003). Split out of `tests/test_erc.py` to respect the per-file size ratchet;
the fixtures mirror that module's ihp-sg13g2 helpers."""

from __future__ import annotations

import json

import klayout.db as kdb
import pytest

from klayout_tools import erc_antenna_gf180mcu as gf180
from klayout_tools._layout import ActiveIndex
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


@pytest.mark.parametrize(("pdk", "limit"), [("gf180mcu", 400), ("ihp-sg13g2", 200)])
def test_pdk_limit_uses_stackup_spelling(tmp_path, capsys, pdk, limit):
    gds, spec = tmp_path / "case.gds", tmp_path / "case.json"
    _fixture(gds, metal1_um2=500)
    _spec(spec, metal_name="metal1")
    report = run_erc(str(gds), str(spec), pdk=pdk)
    layer = next(
        level for level in report["gates"][0]["levels"] if level["layer"] == "metal1"
    )
    assert layer["antenna_ratio_max"] == limit
    assert layer["verdict"] == "violate"
    assert "warning:" not in capsys.readouterr().err


def test_spec_limit_overrides_casefolded_pdk_limit(tmp_path, capsys):
    gds, spec = tmp_path / "case.gds", tmp_path / "case.json"
    _fixture(gds, metal1_um2=500)
    _spec(spec, metal_name="metal1")
    raw = json.loads(spec.read_text())
    raw["antenna_limits"] = [
        {"layer": "metal1", "max_ratio": 1000, "source": "fixture"}
    ]
    spec.write_text(json.dumps(raw))
    report = run_erc(str(gds), str(spec), pdk="gf180mcu")
    layer = next(
        level for level in report["gates"][0]["levels"] if level["layer"] == "metal1"
    )
    assert (layer["antenna_ratio_max"], layer["verdict"]) == (1000, "pass")
    assert layer["antenna_ratio_source"] == "spec antenna_limits[0]: fixture"
    assert "warning:" not in capsys.readouterr().err


def test_no_matching_pdk_layers_warns_once_on_stderr(tmp_path, capsys):
    gds, spec = tmp_path / "case.gds", tmp_path / "case.json"
    _fixture(gds, metal1_um2=500)
    _spec(spec, metal_name="LocalMetal")
    main(["erc", str(gds), str(spec), "--pdk", "gf180mcu", "--format", "json"])
    streams = capsys.readouterr()
    assert json.loads(streams.out)["status"] == "not_checked"
    warnings = [
        line for line in streams.err.splitlines() if "klt erc: warning:" in line
    ]
    assert len(warnings) == 1
    assert all(
        name in warnings[0] for name in ("LocalMetal", "Metal1", "MetalTop", "gf180mcu")
    )

    for options in ([], ["--findings-only"]):
        main(["erc", str(gds), str(spec), *options, "--format", "json"])
        assert "klt erc: warning:" not in capsys.readouterr().err


def test_pdk_rule_available_for_both_spellings_in_one_stackup(capsys):
    from klayout_tools.erc_antenna_names import match_antenna_layers

    rules = {"Metal1": (400.0, "canonical")}
    result = match_antenna_layers(rules, ["ungraded", "Metal1", "metal1"], "gf180mcu")
    assert result["Metal1"] == result["metal1"] == rules["Metal1"]
    assert "ungraded" not in result
    assert match_antenna_layers(rules, [], "gf180mcu") == rules
    assert not capsys.readouterr().err


def _diffusion_fixture(path, mode):
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("GATE")

    def box(layer, coords):
        top.shapes(layout.layer(layer, 0)).insert(kdb.Box(*map(_um, coords)))

    box(30, (0, 0, 1, 3))
    box(22, (-1, 0, 2, 1))
    box(32, (-1.2, -0.2, 2.2, 1.2))
    box(34, (0, 2, 450, 3))
    box(33, (0.3, 2.2, 0.55, 2.45))
    if mode != "under_gate":
        box(22, (10, 1, 12, 4))
        box(32, (9.8, 0.8, 12.2, 4.2))
    if mode == "contacted":
        box(33, (10.5, 2.2, 10.75, 2.45))
    if mode == "other_net":
        box(34, (10, 3.3, 12, 3.8))
        box(33, (10.5, 3.4, 10.75, 3.65))
    if mode == "under_gate":
        box(33, (0.3, 0.2, 0.55, 0.45))
        box(34, (0.1, 0.1, 0.8, 2.1))
    layout.write(str(path))


def _diffusion_spec(path, active="22/0", contact="33/0"):
    gate = {"name": "poly", "role": "gate", "layer": "30/0"}
    if active:
        gate["active_layer"] = active
    path.write_text(
        json.dumps(
            {
                "stackup": [gate, {"name": "Metal1", "layer": "34/0"}],
                "vias": [
                    {
                        "name": "whatever",
                        "layer": contact,
                        "between": ["Metal1", "poly"],
                    }
                ],
                # A custom limit can make the fallback raw-poly denominator violate too.
                **(
                    {
                        "antenna_limits": [
                            {"layer": "Metal1", "max_ratio": 1, "source": "control"}
                        ]
                    }
                    if active is None
                    else {}
                ),
            }
        )
    )


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("contacted", True),
        ("uncontacted", False),
        ("other_net", False),
        ("under_gate", False),
    ],
)
def test_contacted_diffusion_on_violating_final_net(tmp_path, capsys, mode, expected):
    gds, spec = tmp_path / "gate.gds", tmp_path / "erc.json"
    _diffusion_fixture(gds, mode)
    _diffusion_spec(spec)
    args = ["erc", str(gds), str(spec), "--pdk", "gf180mcu", "--format", "json"]
    assert main(args) == 3
    report = json.loads(capsys.readouterr().out)
    (gate,) = report["gates"]
    assert gate["has_contacted_diffusion"] is expected
    assert report["status"] == "violations"
    assert gate["antenna_verdict"] == gate["levels"][1]["verdict"] == "violate"
    if mode != "under_gate":
        assert gate["levels"][1]["antenna_ratio"] == pytest.approx(453)
    assert main(
        ["erc", str(gds), str(spec), "--findings-only", "--format", "json"]
    ) in (0, 4)
    (gate,) = json.loads(capsys.readouterr().out)["gates"]
    assert "has_contacted_diffusion" not in gate


def test_missing_active_declaration_is_unknown(tmp_path):
    gds, spec = tmp_path / "gate.gds", tmp_path / "erc.json"
    _diffusion_fixture(gds, "contacted")
    _diffusion_spec(spec, active=None)
    (gate,) = run_erc(str(gds), str(spec), pdk="gf180mcu")["gates"]
    assert gate["antenna_verdict"] == "violate"
    assert gate["has_contacted_diffusion"] is None


def test_incompatible_layer_identities_are_unknown(tmp_path):
    gds, spec = tmp_path / "gate.gds", tmp_path / "erc.json"
    _fixture(gds, metal1_um2=450)
    _spec(spec)
    (gate,) = run_erc(str(gds), str(spec), pdk="gf180mcu")["gates"]
    assert gate["antenna_verdict"] == "violate"
    assert gate["has_contacted_diffusion"] is None


def test_wrong_contact_layer_on_an_otherwise_measurable_gate(tmp_path):
    gds, spec = tmp_path / "gate.gds", tmp_path / "erc.json"
    _diffusion_fixture(gds, "contacted")
    layout = kdb.Layout()
    layout.read(str(gds))
    top = layout.top_cell()
    # Sustain the same routing connection through a caller's alternate layer.
    top.shapes(layout.layer(99, 0)).insert(kdb.Box(300, 2200, 550, 2450))
    layout.write(str(gds))
    _diffusion_spec(spec, contact="99/0")
    (gate,) = run_erc(str(gds), str(spec), pdk="gf180mcu")["gates"]
    assert gate["levels"][1]["antenna_ratio"] == 453
    assert gate["has_contacted_diffusion"] is None


def test_contacted_diffusion_goes_through_the_bbox_index(tmp_path, monkeypatch):
    # A bare `net_geometry & outside_channel` visits every active polygon in
    # the chip once per violating gate -- quadratic in design size (measured:
    # 7.5 s at 2000 gates vs 0.1 s indexed). Wall-clock is flaky in CI, so pin
    # the property structurally: every net goes through `ActiveIndex`.
    windows = []

    class Spy(ActiveIndex):
        def intersect(self, poly_region):
            windows.append(poly_region.bbox())
            return super().intersect(poly_region)

    monkeypatch.setattr(gf180, "ActiveIndex", Spy)
    gds, spec = tmp_path / "gate.gds", tmp_path / "erc.json"
    _diffusion_fixture(gds, "contacted")
    _diffusion_spec(spec)
    (gate,) = run_erc(str(gds), str(spec), pdk="gf180mcu")["gates"]
    assert gate["has_contacted_diffusion"] is True
    assert len(windows) == 1 and not windows[0].empty()
