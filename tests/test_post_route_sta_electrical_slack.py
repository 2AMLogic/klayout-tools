"""`klt sta` electrical_checks: min_pulse_width / min_period (issue #5241).

Split out of ``test_post_route_sta.py`` to keep that file under its size ceiling.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools import post_route_sta
from klayout_tools import post_route_sta_electrical as electrical
from klayout_tools.post_route_sta import run_sta
from test_post_route_sta import _live_sky130_env, _write_request

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_ELECTRICAL_FIXTURE = Path(__file__).parent / "fixtures" / "sta_electrical_checks"


_PULSE_REPORT = """\
                                       Required     Actual
Pin                                       Width      Width      Slack
---------------------------------------------------------------------
ff1/CLK (low)                           0.08338    0.00782   -0.07557 (VIOLATED)
ff2/CLK (high)                          0.05872    0.03645   -0.02227 (VIOLATED)
KLT_ELECTRICAL_NET ff1/CLK clk
KLT_ELECTRICAL_OK
"""


def _slack_block(begin, end, body):
    return "\n".join([begin, body, end])


def test_parse_electrical_slack_check_violating_issue_5241():
    """Row format captured from OpenROAD 26Q3 `report_check_types
    -min_pulse_width -violators` (see the fixture README)."""
    result = electrical._parse_electrical_slack_check(
        _slack_block(
            electrical._MIN_PULSE_BEGIN,
            electrical._MIN_PULSE_END,
            _PULSE_REPORT,
        ),
        electrical._MIN_PULSE_BEGIN,
        electrical._MIN_PULSE_END,
    )

    assert result == {
        "coverage": "checked",
        "limit_source": "liberty",
        "unit": "ns",
        "violation_count": 2,
        "worst_slack_ns": -0.07557,
        "worst_pin": "ff1/CLK",
    }


def test_parse_electrical_slack_check_clean_unsupported_not_run_issue_5241():
    begin, end = electrical._MIN_PERIOD_BEGIN, electrical._MIN_PERIOD_END
    clean = electrical._parse_electrical_slack_check(
        _slack_block(begin, end, "\nKLT_ELECTRICAL_OK\n"), begin, end
    )
    assert clean["coverage"] == "checked"
    assert clean["violation_count"] == 0
    assert clean["worst_slack_ns"] is None

    unsupported = electrical._parse_electrical_slack_check(
        _slack_block(begin, end, "KLT_ELECTRICAL_UNSUPPORTED"), begin, end
    )
    assert unsupported["coverage"] == "unsupported"
    assert unsupported["violation_count"] is None
    assert unsupported["worst_slack_ns"] is None

    not_run = electrical._parse_electrical_slack_check("nothing", begin, end)
    assert not_run["coverage"] == "not_run"
    assert not_run["violation_count"] is None
    assert not_run["worst_slack_ns"] is None


def test_parse_electrical_slack_check_unparseable_row_is_unsupported():
    begin, end = electrical._MIN_PERIOD_BEGIN, electrical._MIN_PERIOD_END
    result = electrical._parse_electrical_slack_check(
        _slack_block(begin, end, "weird row format (VIOLATED)\nKLT_ELECTRICAL_OK"),
        begin,
        end,
    )
    assert result["coverage"] == "unsupported"
    assert result["violation_count"] is None


def test_electrical_checks_block_has_slack_classes_issue_5241():
    block = post_route_sta._electrical_checks_block("", False)
    for key in ("min_pulse_width", "min_period"):
        assert block[key]["coverage"] == "not_run"
        assert block[key]["worst_slack_ns"] is None


@pytest.mark.skipif(
    _live_sky130_env() is None,
    reason="needs openroad on PATH, PDK_ROOT with a sky130A install, and "
    "KLT_SKIP_OPENROAD_TESTS unset",
)
def test_live_engine_min_pulse_width_issue_5241(tmp_path):
    """Engine integration (issue #5241): a 2 ns clock meets every liberty
    min-pulse-width limit (checked, zero); a 0.05 ns clock violates them
    (negative worst slack). sky130 declares no min_period, so that class is
    a checked zero in both."""

    def run(period_ns):
        request = json.loads((_ELECTRICAL_FIXTURE / "request.json").read_text())
        request["def"] = str(_ELECTRICAL_FIXTURE / request["def"])
        request["spef"] = str(_ELECTRICAL_FIXTURE / request["spef"])
        request["constraints"]["clock_period_ns"] = period_ns
        return run_sta(
            _write_request(tmp_path / "request.json", request),
            pdk_variant="sky130A",
        )["electrical_checks"]

    clean = run(2.0)
    assert clean["min_pulse_width"]["coverage"] == "checked"
    assert clean["min_pulse_width"]["violation_count"] == 0
    assert clean["min_pulse_width"]["worst_slack_ns"] is None

    fast = run(0.05)
    assert fast["min_pulse_width"]["coverage"] == "checked"
    assert fast["min_pulse_width"]["violation_count"] >= 1
    assert fast["min_pulse_width"]["worst_slack_ns"] < 0
    assert fast["min_pulse_width"]["worst_pin"].endswith("/CLK")
    assert fast["min_period"]["coverage"] == "checked"
    assert fast["min_period"]["violation_count"] == 0
