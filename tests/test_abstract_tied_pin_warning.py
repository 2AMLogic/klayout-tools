"""``_tied_abstract_pin_warning``'s example-list truncation, unit-tested
directly.

The builder itself lives in :mod:`klayout_tools.extract_warnings` (moved out
of ``extract_abstract.py``, issue #5632, to hold that module under its
file-size ceiling) and its full-flow behaviour is covered in
``test_extract.py``'s abstract-cells fixtures; this file pins the one arm
those fixtures cannot reach -- more tied instances than
``_ABSTRACT_TIED_PIN_WARNING_EXAMPLES`` -- because the move put the helper
under ``extract_warnings``' 100% coverage floor and the per-file size
ratchet (#2766) kept ``test_extract.py`` from growing to host the test.
"""

from klayout_tools.extract_warnings import _tied_abstract_pin_warning


def test_tied_abstract_pin_warning_aggregates_beyond_five_instances():
    """The plain issue #1366 tie entry, unit-tested directly for its own
    example-list truncation: at most
    ``_ABSTRACT_TIED_PIN_WARNING_EXAMPLES`` instances spelled out, then the
    remainder counted -- the same readable-``warnings[]`` discipline the
    escalated issue #2695 entry's own aggregation test pins down next door
    in ``test_extract.py``."""
    tied = [(f"MACRO_{index}", "MACRO", "TIED", ["A", "B"], 2) for index in range(8)]
    (entry,) = _tied_abstract_pin_warning(tied)
    assert "8 --abstract-cells instance(s)" in entry
    assert "MACRO_4 (cell 'MACRO')" in entry
    assert "MACRO_5 (cell 'MACRO')" not in entry
    assert "... and 3 more instance(s)" in entry
