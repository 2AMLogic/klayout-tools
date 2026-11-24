"""``klt gen-compose`` must not draw a via cut that crowds the target block's
own cuts (issue #5398).

sg13g2's PDK ``npn13G2`` HBT cell draws its own via1 arrays at the emitter/base
pins. A via drop landing on such a pin used to compose ``routed: true`` while
the new cut sat a few nm from the cell's own cuts -- one ``via1.space.1``
finding per neighbour. CI never has the real PDK, so these tests fabricate a
stand-in HBT cell with the same shape of problem (a cut a hair's breadth from
the port the route drops onto).
"""

from __future__ import annotations

import pytest

from klayout_tools import pdk
from klayout_tools.gen import generate
from klayout_tools.gen_compose import compose

_VARIANT = "ihp-sg13g2"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


def _install_hbt_stand_in(root, *, own_cut_offset_um):
    """Fabricate `sg13g2_pr.gds` with an `npn13G2` whose Metal1 emitter pad
    carries one Via1 cut `own_cut_offset_um` east of the `E` port (or no cut
    at all when ``None``)."""
    import klayout.db as kdb

    gds_dir = root / _VARIANT / "libs.ref" / "sg13g2_pr" / "gds"
    gds_dir.mkdir(parents=True)
    (root / _VARIANT / "libs.tech").mkdir(parents=True)
    lay = kdb.Layout()
    lay.dbu = 0.001
    m1, via1, txt = lay.layer(8, 0), lay.layer(19, 0), lay.layer(63, 0)
    cell = lay.create_cell("npn13G2")
    cell.shapes(m1).insert(kdb.Box(-500, -500, 500, 500))
    if own_cut_offset_um is not None:
        cx = int(round(own_cut_offset_um * 1000))
        cell.shapes(via1).insert(kdb.Box(cx - 95, -95, cx + 95, 95))
    cell.shapes(txt).insert(kdb.Text("E", 0, 0))
    cell.shapes(txt).insert(kdb.Text("B", 0, -300))
    cell.shapes(txt).insert(kdb.Text("C", 0, 300))
    lay.write(str(gds_dir / "sg13g2_pr.gds"))


def _compose_emitter_net(tmp_path, root):
    block = generate(
        {
            "generator": "bjt_array",
            "pdk": {"variant": _VARIANT, "root": str(root)},
            "params": {"flavor": "npn13G2", "rows": 1, "cols": 2},
            "options": {"output": str(tmp_path / "hbt.gds")},
        }
    )
    return compose(
        {
            "pdk": {"variant": _VARIANT, "root": str(root)},
            "blocks": [{"id": "a", "generator_report": block}],
            "placement": {"strategy": "row", "order": ["a"], "spacing_um": 1.0},
            "connectivity": [
                {
                    "net": "N",
                    "pins": [
                        {"block": "a", "port": "Q1_E"},
                        {"block": "a", "port": "Q2_E"},
                    ],
                }
            ],
            "routing": {"layer_role": "metal2", "width_um": 0.3},
            "options": {"cell_name": "hbt_top", "output": str(tmp_path / "c.gds")},
        }
    )


def test_via_drop_next_to_block_own_cut_is_reported_unrouted(tmp_path):
    root = tmp_path / "pdk_install"
    _install_hbt_stand_in(root, own_cut_offset_um=0.2)  # 10nm gap to the new cut

    report = _compose_emitter_net(tmp_path, root)

    assert report["unrouted_nets"] == ["N"]
    assert report["nets"][0]["routed"] is False
    reason = report["nets"][0]["legs"][0]["reason"]
    assert "via cut" in reason
    assert "via1.space.1" in reason


def test_via_drop_with_clear_block_cuts_still_routes(tmp_path):
    root = tmp_path / "pdk_install"
    _install_hbt_stand_in(root, own_cut_offset_um=None)

    report = _compose_emitter_net(tmp_path, root)

    assert report["unrouted_nets"] == []
    assert report["nets"][0]["routed"] is True


def test_via_drop_far_from_block_own_cut_still_routes(tmp_path):
    root = tmp_path / "pdk_install"
    # Pitch-clear: 0.45um centre-to-centre leaves 0.26um > via1.space.1.
    _install_hbt_stand_in(root, own_cut_offset_um=0.45)

    report = _compose_emitter_net(tmp_path, root)

    assert report["unrouted_nets"] == []
    assert report["nets"][0]["routed"] is True
