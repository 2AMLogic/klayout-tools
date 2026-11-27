"""A sparse drawn well still defines a substrate-tie boundary."""

import klayout.db as kdb
import pytest

from test_erc import _COMPLEMENT_ID, _complement_tie, _run_complement


@pytest.mark.parametrize("vss_tap", [True, False], ids=["connected", "untapped"])
def test_sparse_well_complement_checks_the_substrate_tie(tmp_path, vss_tap):
    def expand_extent(layout, top):
        top.shapes(layout.layer(99, 0)).insert(kdb.DBox(0, 0, 1000, 1000))

    report = _run_complement(
        tmp_path, "sparse", _complement_tie(), vss_tap=vss_tap, edit=expand_extent
    )
    coverage = report["erc_coverage"]
    assert _COMPLEMENT_ID in coverage["checked"]
    assert _COMPLEMENT_ID in coverage["checked_by_well_assertion"]
    assert coverage["skipped"] == []
    findings = [
        (finding["layer"], finding["net"])
        for finding in report["erc_findings"]
        if finding["rule"] == "erc.missing_tie"
    ]
    assert findings == ([] if vss_tap else [("substrate_tie", "VSS")])
    assert report["erc_status"] == ("clean" if vss_tap else "violations")
