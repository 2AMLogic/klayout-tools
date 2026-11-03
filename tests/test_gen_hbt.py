"""``klt gen bjt_array`` on sg13g2: PDK ``npn13G2*`` HBT cells (issue #4850)."""

from __future__ import annotations

import pytest

from klayout_tools import pdk
from klayout_tools.gen import GenError, generate

_SG13G2_VARIANT = "ihp-sg13g2"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


def _make_install(root, variant):
    (root / variant / "libs.tech").mkdir(parents=True)


@pytest.fixture()
def pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    _make_install(root, "sky130A")
    return root


@pytest.fixture()
def sg13g2_pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    _make_install(root, _SG13G2_VARIANT)
    return root


def _write_hbt_stand_in(install_root, cells=("npn13G2", "npn13G2L", "npn13G2V")):
    """Fabricate a tiny `sg13g2_pr.gds` holding stand-in HBT cells (CI never
    has the real PDK)."""
    import klayout.db as kdb

    gds_dir = install_root / _SG13G2_VARIANT / "libs.ref" / "sg13g2_pr" / "gds"
    gds_dir.mkdir(parents=True)
    lay = kdb.Layout()
    lay.dbu = 0.001
    m1 = lay.layer(8, 0)
    txt = lay.layer(63, 0)
    for name in cells:
        c = lay.create_cell(name)
        c.shapes(m1).insert(kdb.Box(-500, -500, 500, 500))
        c.shapes(txt).insert(kdb.Text("E", 0, 0))
        c.shapes(txt).insert(kdb.Text("B", 0, -300))
        c.shapes(txt).insert(kdb.Text("C", 0, 300))
    path = gds_dir / "sg13g2_pr.gds"
    lay.write(str(path))
    return path


@pytest.mark.parametrize(
    ("flavor", "cell"),
    [("npn13G2", "npn13G2"), ("npn13G2l", "npn13G2L"), ("npn13G2v", "npn13G2V")],
)
def test_sg13g2_bjt_array_instantiates_pdk_hbt_cell(
    flavor, cell, tmp_path, sg13g2_pdk_root
):
    """Issue #4850: `bjt_array` on sg13g2 places the PDK's own HBT cell on a
    rows x cols grid and reports flavor / count / source cell + GDS path."""
    import klayout.db as kdb

    src = _write_hbt_stand_in(sg13g2_pdk_root)
    output = tmp_path / "hbt.gds"
    report = generate(
        {
            "generator": "bjt_array",
            "pdk": {"variant": _SG13G2_VARIANT, "root": str(sg13g2_pdk_root)},
            "params": {"flavor": flavor, "rows": 2, "cols": 3},
            "options": {"output": str(output)},
        }
    )
    assert report["hbt"] == {
        "flavor": flavor,
        "cell": cell,
        "source_gds": str(src),
        "count": 6,
    }
    assert report["device_count"] == 6
    assert {p["name"] for p in report["ports"]} >= {"Q1_E", "Q6_B", "Q3_C"}
    lay = kdb.Layout()
    lay.read(str(output))
    top = lay.top_cell()
    assert sum(1 for _ in top.each_inst()) == 6
    m1 = lay.find_layer(8, 0)
    assert kdb.Region(top.begin_shapes_rec(m1)).count() == 6
    # Instances are disjoint (pitch = bbox + gap).
    assert kdb.Region(top.begin_shapes_rec(m1)).merged().count() == 6


def test_sg13g2_bjt_array_default_flavor_is_npn13g2(tmp_path, sg13g2_pdk_root):
    _write_hbt_stand_in(sg13g2_pdk_root)
    report = generate(
        {
            "generator": "bjt_array",
            "pdk": {"variant": _SG13G2_VARIANT, "root": str(sg13g2_pdk_root)},
            "options": {"output": str(tmp_path / "out.gds")},
        }
    )
    assert report["hbt"]["cell"] == "npn13G2"
    assert report["hbt"]["count"] == 9


def test_sg13g2_bjt_array_missing_gds_is_actionable(tmp_path, sg13g2_pdk_root):
    with pytest.raises(GenError, match="PDK HBT library not found"):
        generate(
            {
                "generator": "bjt_array",
                "pdk": {"variant": _SG13G2_VARIANT, "root": str(sg13g2_pdk_root)},
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )
    assert not (tmp_path / "out.gds").exists()


def test_sg13g2_bjt_array_missing_cell_is_actionable(tmp_path, sg13g2_pdk_root):
    _write_hbt_stand_in(sg13g2_pdk_root, cells=("npn13G2",))
    with pytest.raises(GenError, match=r"npn13G2L.*absent"):
        generate(
            {
                "generator": "bjt_array",
                "pdk": {"variant": _SG13G2_VARIANT, "root": str(sg13g2_pdk_root)},
                "params": {"flavor": "npn13G2l"},
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_sg13g2_bjt_array_unknown_flavor_rejected(tmp_path, sg13g2_pdk_root):
    _write_hbt_stand_in(sg13g2_pdk_root)
    with pytest.raises(GenError, match="params.flavor must be one of"):
        generate(
            {
                "generator": "bjt_array",
                "pdk": {"variant": _SG13G2_VARIANT, "root": str(sg13g2_pdk_root)},
                "params": {"flavor": "npn99"},
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )


def test_bjt_array_flavor_rejected_on_other_families(tmp_path, pdk_root):
    with pytest.raises(GenError, match="only supported on PDK family 'sg13g2'"):
        generate(
            {
                "generator": "bjt_array",
                "pdk": {"variant": "sky130A", "root": str(pdk_root)},
                "params": {"flavor": "npn13G2"},
                "options": {"output": str(tmp_path / "out.gds")},
            }
        )
