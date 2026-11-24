"""The declared top interface of a merged runset PEX netlist."""

import klayout.db as kdb
import pytest

import test_pex_net_correspondence as fixtures
from klayout_tools.pex_errors import PexError
from klayout_tools.pex_merged_pins import replace_port_header
from klayout_tools.pex_netlist_text import _subckt_interfaces
from test_pex_net_correspondence import _merged
from test_pex_net_correspondence import fake_klayout as fake_klayout


def test_curated_ports_reach_the_merged_spice_and_report(tmp_path, fake_klayout):
    report = _merged(tmp_path, fake_klayout, pins=frozenset({"VPWR", "A"}))
    source = (tmp_path / "leg.spice").read_text()
    assert _subckt_interfaces(source)["top"][1] == ["A", "VPWR"]
    assert report["pin_count"] == 2
    ports = report["net_correspondence"]["declared_ports"]
    assert [p["curated"] for p in ports] == ["A", "VPWR"]
    assert [(p["runset_cluster_id"], i) for i, p in enumerate(ports)] == sorted(
        [(n["net_id"], n["pin_index"]) for n in report["nets"] if n["pin"]],
        key=lambda pair: pair[1],
    )


@pytest.mark.parametrize(("pin", "cause"), [("MISSING", "missing"), ("Y", "ambiguous")])
def test_unresolved_declared_pin_refuses_atomic_output(
    tmp_path, fake_klayout, pin, cause
):
    target = tmp_path / "leg.spice"
    target.write_text("preserved\n")
    with pytest.raises(PexError, match=cause) as raised:
        _merged(tmp_path, fake_klayout, pins=frozenset({pin}))
    assert raised.value.code == "merged_pin_unresolved"
    assert target.read_text() == "preserved\n"


def test_split_or_unmatched_cluster_cannot_supply_a_port(tmp_path, fake_klayout):
    fake_klayout["bridge_terminals"] = True
    fake_klayout["isolate_metals"] = True
    with pytest.raises(PexError, match="correspondence") as raised:
        _merged(tmp_path, fake_klayout, pins=frozenset({"A", "VGND"}))
    assert raised.value.code == "merged_pin_unresolved"


@pytest.mark.parametrize(
    ("rename", "expected"), [(False, "SUPPLY|VPWR"), (True, "VPWR")]
)
def test_pin_alias_uses_curated_spelling(
    tmp_path, fake_klayout, monkeypatch, rename, expected
):
    def fixture_with_alias(path):
        layout = fixtures._make_sg13cmos5l_inverter_layout()
        layout.top_cell().shapes(layout.layer(8, 2)).insert(
            kdb.Text("SUPPLY", kdb.Trans(200, 2500))
        )
        layout.write(path)

    monkeypatch.setattr(fixtures, "_write_fixture", fixture_with_alias)
    report = _merged(
        tmp_path, fake_klayout, pins=frozenset({"A", "VPWR"}), rename=rename
    )
    assert report["rename_declared_pins"] is rename
    spice = (tmp_path / "leg.spice").read_text()
    assert _subckt_interfaces(spice)["top"][1] == ["A", expected]
    assert any(
        expected in line.split()[1:]
        for line in spice.splitlines()
        if line.startswith("X")
    )
    assert [p["curated"] for p in report["net_correspondence"]["declared_ports"]] == [
        "A",
        expected,
    ]


def test_pin_name_collision_on_an_unrelated_runset_net_is_refused(
    tmp_path, fake_klayout
):
    baseline = _merged(tmp_path, fake_klayout)
    matches = baseline["net_correspondence"]["matched"]
    other = next(m["runset"] for m in matches if m["curated"] == "VGND")
    fake_klayout["net_labels"] = {other.lstrip("\\"): "VPWR"}
    original = (tmp_path / "leg.spice").read_bytes()
    with pytest.raises(PexError, match="ambiguous SPICE node name"):
        _merged(tmp_path, fake_klayout, pins=frozenset({"VPWR"}))
    assert (tmp_path / "leg.spice").read_bytes() == original


def test_port_header_retains_parameter_defaults_and_wraps_long_lists():
    names = [f"PORT_{i:02}" for i in range(40)]
    lines = [
        ".SUBCKT TOP OLD1\n",
        "+ OLD2 params: gain=2\n",
        "R1 OLD1 OLD2 5\n",
        ".ENDS TOP\n",
    ]
    replace_port_header(lines, 0, "TOP", [{"curated": n} for n in names])
    assert _subckt_interfaces("".join(lines))["top"][1] == names
    assert "+ params: gain=2\n" in lines
    assert max(len(line.rstrip()) for line in lines) <= 80
    assert lines[-2:] == ["R1 OLD1 OLD2 5\n", ".ENDS TOP\n"]
