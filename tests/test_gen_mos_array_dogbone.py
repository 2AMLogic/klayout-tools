"""``mos_array`` ``dogbone_terminals`` option (issue #4851): w_um below the
generator-side 0.42um floor on sg13g2 via locally widened S/D heads."""

from __future__ import annotations

import pytest

from klayout_tools import pdk
from klayout_tools.drc import run_drc
from klayout_tools.gen import GenError, generate

VARIANT = "ihp-sg13g2"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def root(tmp_path):
    r = tmp_path / "pdk_install"
    (r / VARIANT / "libs.tech").mkdir(parents=True)
    return r


def _req(tmp_path, root, name, **params):
    return {
        "generator": "mos_array",
        "pdk": {"variant": VARIANT, "root": str(root)},
        "params": {
            "w_um": 0.25,
            "l_um": 12,
            "rows": 1,
            "cols": 1,
            "dummy": 0,
            "flavor": "nfet",
            **params,
        },
        "options": {"output": str(tmp_path / f"{name}.gds")},
    }


def test_default_keeps_floor_error(tmp_path, root):
    with pytest.raises(GenError, match=r"params\.w_um must be >= 0\.42"):
        generate(_req(tmp_path, root, "a"))


def test_below_deck_active_min_rejected(tmp_path, root):
    with pytest.raises(GenError, match="activ.width.1"):
        generate(_req(tmp_path, root, "a", w_um=0.1, dogbone_terminals=True))


def test_at_or_above_floor_is_noop(tmp_path, root):
    import klayout.db as kdb

    generate(_req(tmp_path, root, "off", w_um=0.5, l_um=1))
    generate(_req(tmp_path, root, "on", w_um=0.5, l_um=1, dogbone_terminals=True))
    a, b = kdb.Layout(), kdb.Layout()
    a.read(str(tmp_path / "off.gds"))
    b.read(str(tmp_path / "on.gds"))
    assert kdb.LayoutDiff().compare(a.top_cell(), b.top_cell())


def test_parallel_fingers_rejected(tmp_path, root):
    with pytest.raises(GenError, match="dogbone_terminals"):
        generate(
            _req(
                tmp_path,
                root,
                "p",
                fingers=2,
                finger_topology="parallel",
                dogbone_terminals=True,
            )
        )


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"gate_contact": True},
        {"w_um": 0.15, "l_um": 0.5},
        {"w_um": 0.15, "l_um": 0.5, "flavor": "pfet"},
        {"l_um": 1, "fingers": 2, "finger_topology": "series"},
        {"l_um": 1, "rows": 2, "cols": 2, "dummy": 1, "gate_contact": True},
    ],
)
def test_dogbone_drc_clean(tmp_path, root, params):
    generate(_req(tmp_path, root, "d", dogbone_terminals=True, **params))
    report = run_drc(str(tmp_path / "d.gds"), "sg13g2")
    assert report["status"] == "clean", report["violations"]


def test_channel_keeps_requested_width(tmp_path, root):
    import klayout.db as kdb

    generate(_req(tmp_path, root, "w", dogbone_terminals=True))
    layout = kdb.Layout()
    layout.read(str(tmp_path / "w.gds"))
    active = kdb.Region(layout.top_cell().begin_shapes_rec(layout.layer(1, 0)))
    poly = kdb.Region(layout.top_cell().begin_shapes_rec(layout.layer(5, 0)))
    channel = active & poly
    assert channel.bbox().height() == 250  # 0.25um at 1nm dbu
    assert channel.bbox().width() == 12000
