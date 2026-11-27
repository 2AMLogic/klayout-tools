"""gf180mcu CO.3 measures `Contact` minus `SramCore` (issue #7960).

The foundry CO.3 checks the same `contact.not(sramcore)` operand as CO.4, against
Poly2: a contact inside `SramCore` (108/5) is outside `poly2.enclosing.contact.1`.
The curated rule passed the whole `Contact` layer and flagged every such contact.

The SramCore placements and expected counts are CO.4's, with Poly2 as the
conductor. They are parametrized here rather than beside CO.4's cases in
`test_drc_gf180mcu_cut_containment.py` because that module sits just under the
500-line limit of CI's per-file size ratchet.
"""

from __future__ import annotations

import pytest

from klayout_tools.drc import run_drc
from test_drc_gf180mcu_cut_containment import (
    _CO4_SRAMCORE_CASES,
    CONTACT,
    POLY2,
    SRAMCORE,
    _hits,
    _layout,
)

_CO3 = "poly2.enclosing.contact.1"


def _co3_shapes(sramcore_x):
    shapes = [(POLY2, (0, 0, 1000, 1000)), (CONTACT, (10, 390, 230, 610))]
    if sramcore_x is not None:
        shapes.append((SRAMCORE, (sramcore_x[0], -100, sramcore_x[1], 1100)))
    return shapes


@pytest.mark.parametrize("sramcore_x,expected", _CO4_SRAMCORE_CASES)
def test_co3_operand_excludes_sramcore(tmp_path, sramcore_x, expected):
    report = run_drc(_layout(tmp_path, "co3.gds", _co3_shapes(sramcore_x)), "gf180mcu")

    assert len(_hits(report, _CO3)) == expected
    assert _CO3 in report["coverage"]["rules_checked"]


def test_co3_boundary_marker_sits_where_the_unmarked_one_does(tmp_path):
    """Masking the contact's right edge leaves its left 10 nm of enclosure
    short, so the retained marker is the unmarked control's own left-edge
    marker -- none at the SramCore cut edge."""

    def markers(sramcore_x):
        path = _layout(tmp_path, "co3.gds", _co3_shapes(sramcore_x))
        hits = _hits(run_drc(path, "gf180mcu"), _CO3)
        return [(hit["bbox"], hit["polygon"]) for hit in hits]

    control = markers(None)
    assert len(control) == 1
    assert markers((100, 1100)) == control
