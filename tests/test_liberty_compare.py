"""Parser regression tests for the shared Liberty reader."""

from __future__ import annotations

import pytest

from klayout_tools import liberty_compare

_UNTERMINATED = """library (x) {
  cell (A) {
    area : 0.23
    pg_pin (VDD) { pg_type : primary_power ; }
  }
  cell (B) { }
  cell (C) { }
}
"""


def test_missing_semicolon_before_group_keeps_later_cells():
    library = liberty_compare.parse_liberty_text(_UNTERMINATED)
    cells = library.children("cell")
    assert [c.args for c in cells] == [["A"], ["B"], ["C"]]
    assert cells[0].attributes["area"] == "0.23"
    assert [g.kind for g in cells[0].groups] == ["pg_pin"]


def test_unterminated_attribute_last_before_close_and_quoted():
    text = 'library (x) {\n cell (A) {\n area : "0.23"\n }\n cell (B) { }\n}\n'
    library = liberty_compare.parse_liberty_text(text)
    assert [c.args for c in library.children("cell")] == [["A"], ["B"]]
    assert library.children("cell")[0].attributes["area"] == "0.23"


def test_unclosed_group_still_errors():
    with pytest.raises(liberty_compare.LibertyCompareError):
        liberty_compare.parse_liberty_text(
            "library (x) {\n cell (A) {\n area : 0.23\n pg_pin (V) { a : 1 ; }\n"
        )
