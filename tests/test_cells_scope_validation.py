"""The ``--cells`` scope shared by ``klt ring-check`` and ``klt keepout-check``.

Both verbs validate an explicit cell-name list in two steps: a malformed list is
refused *before* the layout is read, and names the stream does not define are
refused *after*. The two steps live in ``klayout_tools._layout`` and take the
calling verb's exception class (issue #8065), so these tests pin the contract at
both verb boundaries: each verb raises its own class, and the two verbs'
messages are identical down to the full text.
"""

from __future__ import annotations

import klayout.db as kdb
import pytest

from klayout_tools._layout import require_known_cells, validate_cells
from klayout_tools.keepout_check import KeepoutCheckError, run_keepout_check
from klayout_tools.ring_check import RingCheckError, run_ring_check

_LAYER = (8, 0)

_VERBS = {
    "ring-check": (
        lambda path, cells: run_ring_check(str(path), [_LAYER], cells=cells),
        RingCheckError,
    ),
    "keepout-check": (
        lambda path, cells: run_keepout_check(str(path), [_LAYER], 1.0, cells=cells),
        KeepoutCheckError,
    ),
}

_MALFORMED = [
    ("RING", "--cells must be a list of cell names"),
    ([], "--cells is empty: at least one cell name required"),
    ([""], "malformed cell name in --cells: '' (must be a non-empty string)"),
    (["  "], "malformed cell name in --cells: '  ' (must be a non-empty string)"),
    ([3], "malformed cell name in --cells: 3 (must be a non-empty string)"),
    ([None], "malformed cell name in --cells: None (must be a non-empty string)"),
    (["RING", ""], "malformed cell name in --cells: '' (must be a non-empty string)"),
]


def _stream(tmp_path):
    """A stream whose sole top cell ``TOP`` places ``A``, ``B`` and ``C``, each
    holding one square on ``_LAYER``."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    layer = layout.layer(*_LAYER)
    for index, name in enumerate(("A", "B", "C")):
        cell = layout.create_cell(name)
        cell.shapes(layer).insert(kdb.Box(0, 0, 2000, 2000))
        top.insert(
            kdb.CellInstArray(
                cell.cell_index(), kdb.Trans(kdb.Vector(3000 * index, 3000 * index))
            )
        )
    path = tmp_path / "cells.gds"
    layout.write(str(path))
    return path


@pytest.mark.parametrize("cells, message", _MALFORMED)
def test_malformed_cells_are_refused_before_the_layout_is_read(
    tmp_path, cells, message
):
    absent = tmp_path / "absent.gds"
    messages = set()
    for run, error_cls in _VERBS.values():
        with pytest.raises(error_cls) as excinfo:
            run(absent, cells)
        messages.add(str(excinfo.value))
    assert messages == {message}


@pytest.mark.parametrize(
    "cells, message",
    [
        (["NOPE"], "unknown cell(s) in --cells: 'NOPE'"),
        (["NOPE", "A", "ZZZ", "NOPE"], "unknown cell(s) in --cells: 'NOPE', 'ZZZ'"),
        ([" A "], "unknown cell(s) in --cells: ' A '"),
    ],
)
def test_unknown_cells_are_all_reported_in_input_order(tmp_path, cells, message):
    path = _stream(tmp_path)
    messages = set()
    for run, error_cls in _VERBS.values():
        with pytest.raises(error_cls) as excinfo:
            run(path, cells)
        messages.add(str(excinfo.value))
    assert messages == {message}


@pytest.mark.parametrize("cells", [["B", "A", "C", "A"], ("B", "A", "C", "A")])
def test_repeated_name_keeps_its_first_position(tmp_path, cells):
    path = _stream(tmp_path)
    for run, _error_cls in _VERBS.values():
        assert run(path, cells)["cells"] == ["B", "A", "C"]


class _RequestError(Exception):
    pass


def test_shared_helpers_raise_the_callers_error_class():
    assert validate_cells(None, _RequestError) is None
    assert validate_cells(["B", "A", "B"], _RequestError) == ["B", "A"]
    with pytest.raises(_RequestError, match="--cells must be a list"):
        validate_cells("A", _RequestError)

    layout = kdb.Layout()
    layout.create_cell("A")
    require_known_cells(layout, None, _RequestError)
    require_known_cells(layout, ["A"], _RequestError)
    with pytest.raises(_RequestError, match="unknown cell"):
        require_known_cells(layout, ["A", "NOPE"], _RequestError)
