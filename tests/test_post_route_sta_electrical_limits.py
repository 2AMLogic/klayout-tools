"""Malformed maximum-limit violators invalidate just their own STA class."""

import pytest

from klayout_tools import post_route_sta as sta

_LIMITS = [
    ("max_transition", sta._MAX_SLEW_BEGIN, sta._MAX_SLEW_END, "ns"),
    ("max_capacitance", sta._MAX_CAP_BEGIN, sta._MAX_CAP_END, "pF"),
]
_VALID = "u1/Y 0.00100 0.03000 -0.02900 (VIOLATED)"


@pytest.mark.parametrize(("key", "begin", "end", "unit"), _LIMITS)
@pytest.mark.parametrize(
    "rows",
    [
        "u2/A broken (VIOLATED)",
        _VALID + "\nu2/A broken (VIOLATED)",
        "u2/A 1..0 0.03 -0.029 (VIOLATED)",
        "u2/A 0.001 0..03 -0.029 (VIOLATED)",
        "u2/A 0.001 0.03 -0..029 (VIOLATED)",
        "u2/A 0.001 1e999 -0.029 (VIOLATED)",
    ],
)
def test_marked_unparseable_electrical_row_invalidates_class(
    key, begin, end, unit, rows
):
    stdout = "\n".join(
        [begin, rows, "KLT_ELECTRICAL_NET u1/Y net1", "KLT_ELECTRICAL_OK", end]
    )
    result = sta._electrical_checks_block(stdout, has_spef=True)
    assert result[key] == {
        "coverage": "unsupported",
        "limit_source": "liberty",
        "unit": unit,
        "violation_count": None,
        "net_count": None,
        "worst_excess": None,
        "worst_pin": None,
    }


def test_bad_transition_keeps_independent_capacitance_evidence():
    stdout = "\n".join(
        [
            sta._MAX_SLEW_BEGIN,
            "u2/A broken (VIOLATED)",
            "KLT_ELECTRICAL_OK",
            sta._MAX_SLEW_END,
            sta._MAX_CAP_BEGIN,
            _VALID,
            "KLT_ELECTRICAL_NET u1/Y net1",
            "KLT_ELECTRICAL_OK",
            sta._MAX_CAP_END,
        ]
    )
    result = sta._electrical_checks_block(stdout, has_spef=False)
    assert result["max_transition"]["coverage"] == "unsupported"
    assert result["max_capacitance"] == {
        "coverage": "checked",
        "limit_source": "liberty",
        "unit": "pF",
        "violation_count": 1,
        "net_count": 1,
        "worst_excess": 0.029,
        "worst_pin": "u1/Y",
    }
