"""Issue #4915: ``erc_coverage.missing_tie_checked``."""

from klayout_tools.erc import _connectivity_coverage


def test_missing_tie_checked_false_without_ties():
    cov = _connectivity_coverage([], [], [])
    assert cov["missing_tie_checked"] is False
    disclosed = _connectivity_coverage(
        [], [], [], ties_disclosure={"kind": "unexpressible"}
    )
    assert disclosed["missing_tie_checked"] is False


def test_missing_tie_checked_false_when_every_tie_degenerate():
    cov = _connectivity_coverage(
        [], [], [{"name": "t1"}], degenerate_ties={"t1": "degenerate_tap_declaration"}
    )
    assert cov["missing_tie_checked"] is False


def test_missing_tie_checked_true_when_a_tie_is_checked():
    cov = _connectivity_coverage(
        [],
        [],
        [{"name": "t1"}, {"name": "t2"}],
        degenerate_ties={"t1": "degenerate_tap_declaration"},
    )
    assert cov["missing_tie_checked"] is True
