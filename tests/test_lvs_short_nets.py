"""``hints.short_nets``' merge helper under an empty ``nets`` list
(``klt lvs``, reference side).

``_short_nets_merge_nets`` lives in :mod:`klayout_tools.lvs_short_nets`
(moved out of ``lvs.py``, issue #5632, to hold that module under its
file-size ceiling); the hint's end-to-end suite is ``test_lvs.py``'s
``short_nets`` block. This file pins the one arm that suite cannot reach --
an *explicitly empty* ``nets`` list beside a non-empty ``devices`` list --
because the move put the helper's early return under ``lvs_short_nets``'s
100% line-coverage floor and the per-file size ratchet (#2766) kept
``test_lvs.py`` from growing to host the test.
"""

from klayout_tools.lvs import CATEGORY_HINTS_SHORT_NETS_APPLIED, run_lvs
from test_lvs import _OPTION_LADDER_LAYOUT_CODE0_SPICE, _option_ladder_request


def test_short_nets_hint_empty_nets_list_merges_nothing(tmp_path):
    """`nets: []` alongside `devices` is a devices-only apply:
    `_short_nets_merge_nets`'s empty-group guard returns the no-op `[]` (no
    merge, no error -- distinct from the absent/empty-object request errors
    ``test_lvs.py`` pins), the disclosure names the dropped devices only,
    and nothing carries the merge's `OUT|P0` spelling: dropping S0 empties
    `P0` entirely, so `_purge_emptied_nets` removes it and the compare
    matches on the remaining OUT/P1/P2 pins -- resolved by removal here,
    where the real hint resolves it by merging."""
    path = _option_ladder_request(
        tmp_path,
        _OPTION_LADDER_LAYOUT_CODE0_SPICE,
        hints={"short_nets": {"nets": [], "devices": ["S0", "S1", "S2"]}},
    )

    report = run_lvs(path)

    assert report["status"] == "match"
    (entry,) = [
        m
        for m in report["mismatches"]
        if m["category"] == CATEGORY_HINTS_SHORT_NETS_APPLIED
    ]
    assert entry["details"]["merged_nets"] == []
    assert [d["name"] for d in entry["details"]["dropped_devices"]] == [
        "S0",
        "S1",
        "S2",
    ]
    assert not [c for c in report["net_correspondence"] if c["reference"] == "OUT|P0"]
