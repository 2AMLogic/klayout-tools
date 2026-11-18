"""What-if ``moves.json`` number validation (issue #5891).

The stdlib ``json`` parser turns a 400-digit integer literal into a Python
``int`` too large for a ``float``; the refusal must stay the documented
``EconomyError`` envelope (``klt economy --what-if``), not escape as an
uncaught ``OverflowError`` traceback. ``test_economy.py`` holds the
end-to-end what-if coverage; this file pins the number-contract wording.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools import economy_whatif
from klayout_tools.economy import EconomyError


def _spec(tmp_path, data):
    path = tmp_path / "moves.json"
    path.write_text(json.dumps(data))
    return str(path)


@pytest.mark.parametrize(
    "spec",
    [
        {"moves": [{"instance": "A", "dx_um": 10**400}]},
        {"reshape": [{"cell": "A", "width_um": 1, "height_um": 10**400}]},
    ],
)
def test_huge_int_is_a_documented_error_not_a_traceback(tmp_path, spec):
    with pytest.raises(EconomyError, match="must be finite, got"):
        economy_whatif.load_what_if_spec(_spec(tmp_path, spec), EconomyError)


def test_non_number_wording_is_preserved(tmp_path):
    spec = {"moves": [{"instance": "A", "dx_um": "far"}]}
    with pytest.raises(EconomyError, match="dx_um must be a number, got 'far'"):
        economy_whatif.load_what_if_spec(_spec(tmp_path, spec), EconomyError)
