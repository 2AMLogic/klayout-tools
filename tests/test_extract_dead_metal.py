"""Unit tests for :mod:`klayout_tools.extract_dead_metal` (issue #5545).

The module is pure data shaping over the plain dicts ``_detect_dead_metal``
returns -- no KLayout dependency at all -- so its edge cases (two roles on one
stream layer, ordering, float summation, the empty case, the exact-equality
warning swap) are pinned here directly, without building a layout.

The end-to-end contract that `klt extract --omit-dead-metal` composes out of
these helpers lives in ``tests/test_extract_dead_metal_omit.py``.
"""

from __future__ import annotations

from klayout_tools.extract_dead_metal import (
    dead_metal_warning,
    drop_dead_metal_rows,
    summarize_dead_metal,
)

# `extract_dead_metal.py`'s pure helpers, exercised without a layout: the
# roll-up is plain data shaping over `_detect_dead_metal`'s dict output, so the
# edge cases below are cheaper and sharper to pin here than via extraction.


def _cluster(role, layer, datatype, shapes, area):
    return {
        "role": role,
        "layer": layer,
        "datatype": datatype,
        "bbox_um": {"left": 0.0, "bottom": 0.0, "right": 1.0, "top": 1.0},
        "shapes": shapes,
        "area_um2": area,
    }


def test_summarize_dead_metal_groups_and_sorts_by_layer_datatype_role():
    """One row per distinct `(role, layer, datatype)`, sorted by `(layer,
    datatype, role)` -- two roles on one stream layer stay separate rows."""
    summary = summarize_dead_metal(
        [
            _cluster("via1", 68, 44, 3, 0.25),
            _cluster("metal2", 69, 20, 1, 0.5),
            _cluster("metal2", 69, 20, 2, 0.25),
            _cluster("metal2_alt", 69, 20, 1, 0.125),
        ]
    )

    assert [(r["layer"], r["datatype"], r["role"]) for r in summary["by_layer"]] == [
        (68, 44, "via1"),
        (69, 20, "metal2"),
        (69, 20, "metal2_alt"),
    ]
    assert summary["clusters"] == 4
    assert summary["shapes"] == 7
    assert summary["area_um2"] == 1.125
    met2 = summary["by_layer"][1]
    assert (met2["clusters"], met2["shapes"], met2["area_um2"]) == (2, 3, 0.75)


def test_summarize_dead_metal_rounds_float_summation_artifacts():
    """Areas are summed from already-rounded per-cluster values and re-rounded,
    so the field is a stable, diffable number rather than `0.30000000000000004`."""
    summary = summarize_dead_metal(
        [_cluster("metal1", 68, 20, 1, 0.1) for _ in range(3)]
    )
    assert summary["area_um2"] == 0.3
    assert summary["by_layer"][0]["area_um2"] == 0.3


def test_summarize_dead_metal_empty_is_zeroed_not_none():
    assert summarize_dead_metal([]) == {
        "clusters": 0,
        "shapes": 0,
        "area_um2": 0.0,
        "by_layer": [],
    }


def test_drop_dead_metal_rows_is_a_no_op_without_the_flag():
    """`omit=False` returns the same list object and leaves `warnings` alone."""
    rows = [_cluster("metal1", 68, 20, 1, 0.5)]
    warnings = [dead_metal_warning(rows, omitted=False), "unrelated"]
    before = list(warnings)

    assert drop_dead_metal_rows(rows, warnings, False) is rows
    assert warnings == before


def test_drop_dead_metal_rows_swaps_only_the_exact_warning_it_wrote():
    """The rewrite is an exact-equality swap of the string `dead_metal_warning`
    produced, so an unrelated warning that merely mentions dead metal is left
    untouched."""
    rows = [_cluster("metal1", 68, 20, 1, 0.5)]
    decoy = "dead_metal[] mentioned by some other diagnostic"
    warnings = [decoy, dead_metal_warning(rows, omitted=False)]

    assert drop_dead_metal_rows(rows, warnings, True) == []
    assert warnings == [decoy, dead_metal_warning(rows, omitted=True)]


def test_drop_dead_metal_rows_with_nothing_to_drop():
    """An empty list appended no warning in the first place, so the flag has
    nothing to rewrite and nothing to drop."""
    warnings = ["unrelated"]
    assert drop_dead_metal_rows([], warnings, True) == []
    assert warnings == ["unrelated"]


def test_dead_metal_warning_singular_and_plural_wording():
    """The counts are pluralised independently -- one cluster of two shapes
    reads "1 routing-stack cluster (2 shapes ...)"."""
    one = dead_metal_warning([_cluster("metal1", 68, 20, 2, 0.5)], omitted=False)
    assert one.startswith("1 routing-stack cluster (2 shapes on 68/20)")

    many = dead_metal_warning(
        [_cluster("metal1", 68, 20, 1, 0.5), _cluster("via1", 68, 44, 1, 0.25)],
        omitted=False,
    )
    assert many.startswith("2 routing-stack clusters (2 shapes on 68/20, 68/44)")


def test_dead_metal_warning_omitted_form_differs_only_in_its_tail():
    """Counts, layers and the whole explanatory body are byte-identical; only
    the closing pointer changes."""
    rows = [_cluster("metal1", 68, 20, 1, 0.5)]
    full = dead_metal_warning(rows, omitted=False)
    omitted = dead_metal_warning(rows, omitted=True)
    shared = "connection you intended is missing -- "

    assert full.split(shared)[0] == omitted.split(shared)[0]
    assert full.endswith("see dead_metal[] for the per-cluster layer/bbox/shape count.")
    assert "dead_metal[]" not in omitted.split(shared)[1]
    assert "dead_metal_summary" in omitted
