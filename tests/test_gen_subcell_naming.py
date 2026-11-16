"""Issue #5808: the reported ``subcell_naming_scheme`` id must agree with the
hierarchy actually written to the GDS -- the silent drift the field exists to
make mechanically detectable. This pins ``core-suffix-v1`` (the default PCell
path) and ``null`` (``flatten_output=True``) against the produced names;
``pdk-cell-v1`` is pinned in ``tests/test_gen_hbt.py``."""

from __future__ import annotations

import klayout.db as kdb
import pytest

from klayout_tools import gen, pdk


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def sky130_root(tmp_path):
    root = tmp_path / "pdk_install"
    (root / "sky130A" / "libs.tech").mkdir(parents=True)
    return root


@pytest.fixture()
def asap7_root(tmp_path):
    root = tmp_path / "asap7"
    for sub in (("base", "setup", "klayout"), ("base", "apr"), ("libs",)):
        root.joinpath(*sub).mkdir(parents=True)
    return root


def test_scheme_matches_written_core_suffix_names(tmp_path, sky130_root):
    """``core-suffix-v1``: the instanced sub-cell is ``<cell_name>__core``."""
    output = tmp_path / "r.gds"
    report = gen.generate(
        {
            "generator": "resistor_strip",
            "pdk": {"variant": "sky130A", "root": str(sky130_root)},
            "params": {"num": 2},
            "options": {"cell_name": "ring", "output": str(output)},
        }
    )
    assert report["subcell_naming_scheme"] == "core-suffix-v1"
    single = kdb.Layout()
    single.read(str(output))
    assert sorted(c.name for c in single.each_cell()) == ["ring", "ring__core"]


def test_flattened_stream_reports_null_scheme(tmp_path, asap7_root):
    """``null``: a flattened stream ships one flat cell, no named sub-cells."""
    output = tmp_path / "tap.gds"
    report = gen.generate(
        {
            "generator": "asap7_tap",
            "pdk": {"root": str(asap7_root)},
            "params": {"device": "ptap", "nfin": 2, "ncol": 1},
            "options": {"cell_name": "tap0", "output": str(output)},
        }
    )
    assert report["subcell_naming_scheme"] is None
    single = kdb.Layout()
    single.read(str(output))
    assert [c.name for c in single.each_cell()] == ["tap0"]
