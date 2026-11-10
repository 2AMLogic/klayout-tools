"""`--parasitics` sees a labelled net with no device terminal (issue #5201)."""

from __future__ import annotations

import klayout.db as kdb

from klayout_tools.extract import run_extract

DBU = 0.00025


def test_parasitics_see_a_labelled_net_with_no_device_terminal(tmp_path):
    """A bare routed wire carrying a pin-purpose label is promoted to a pin
    (issue #539 on the planar path) instead of being purged (#5201)."""
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(19, 0)).insert(kdb.Box(0, 0, 2000, 72))
    top.shapes(layout.layer(19, 251)).insert(kdb.Text("A", kdb.Trans(1000, 36)))
    path = str(tmp_path / "dev.gds")
    layout.write(path)
    report = run_extract(
        path, "asap7", output=str(tmp_path / "w.spice"), parasitics=True
    )
    assert [n["name"] for n in report["nets"]] == ["A"]
    assert [n["net"] for n in report["parasitics"]["nets"]] == ["A"]
