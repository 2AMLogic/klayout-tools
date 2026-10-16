"""Assertion helper for generator DRC tests on decks with DF.13/DF.14 (#3607).

The gf180mcu deck enforces a 20 um tap-distance rule by default. The layout
generators do not emit well/substrate taps yet (tracked separately), so their
output is expected to violate exactly the tap-distance rules and nothing else.
The helper keeps every other rule's coverage whole-deck while pinning the known
tap gap -- a regression in any non-tap rule still fails the test.
"""

TAP_DISTANCE_CHECK = "tap_distance"


def assert_clean_except_tap_distance(report):
    """Assert `report` has no violations other than `tap_distance` ones."""
    other = [v for v in report["violations"] if v.get("check") != TAP_DISTANCE_CHECK]
    assert not other, other
