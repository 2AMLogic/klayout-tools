"""Tests for the ``klayout_tools.gen_snap`` cut-box snap helpers."""

from klayout_tools.gen_snap import _snap_span


def test_snap_span_non_grid_multiple_rounds_each_edge_independently():
    # A span whose length is not a grid multiple cannot be translated whole:
    # each edge rounds to the grid on its own, so the span's size may change
    # (13 dbu -> 20 dbu, and 7 dbu -> 0 dbu for a degenerate zero-size span)
    # -- unlike the grid-multiple path, which shifts the span intact.
    assert _snap_span(4, 17, 10) == (0, 20)
    assert _snap_span(6, 13, 10) == (10, 10)
