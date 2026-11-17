"""Tests that the P&R GDS merge avoids klayout-version-specific APIs.

``Layout.read_bytes``/``Layout.write_bytes`` are klayout 0.30-only (issue
#5768): ``klayout`` 0.28.x lacks them, so ``_merge_gds_view``'s
re-serialization round trip must use the plain ``Layout.read``/``write``
forms, which exist on every supported klayout module. Split out of
``tests/test_place_and_route.py`` so the new coverage carries no entry in
that file's own per-file size ratchet (``file-size-baseline.json``).
"""

from __future__ import annotations

import pytest

from klayout_tools import place_and_route_gds_merge as merge

kdb = pytest.importorskip("klayout.db")


def test_merge_gds_view_avoids_read_bytes_write_bytes(tmp_path):
    """`_merge_gds_view` must not use the 0.30-only `Layout.read_bytes` /
    `write_bytes` (issue #5768): `klayout` 0.28.x lacks them."""
    src = kdb.Layout()
    src.dbu = 0.001
    top = src.create_cell("blk")
    top.shapes(src.layer(1, 0)).insert(kdb.Box(0, 0, 100, 100))
    gds = tmp_path / "view.gds"
    src.write(str(gds))

    class NoBytesLayout(kdb.Layout):
        def read_bytes(self, *a, **k):
            raise AttributeError("read_bytes")

        def write_bytes(self, *a, **k):
            raise AttributeError("write_bytes")

    class _Kdb:
        def __getattr__(self, name):
            return NoBytesLayout if name == "Layout" else getattr(kdb, name)

    main = NoBytesLayout()
    main.dbu = 0.0005
    merge._merge_gds_view(_Kdb(), main, str(gds))
    cell = main.cell("blk")
    assert cell is not None
    box = cell.bbox()
    assert main.dbu == 0.0005
    assert (box.width(), box.height()) == (200, 200)
