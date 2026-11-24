"""Parse violating endpoint rows captured from OpenROAD."""

import json
from pathlib import Path

import pytest

from klayout_tools.post_route_sta_violating_endpoints import (
    _VIOLATING_ENDPOINTS_BEGIN,
    _VIOLATING_ENDPOINTS_END,
    _violating_endpoints,
)

_VIOLATING_ENDPOINT_CAPTURES = json.loads(
    (
        Path(__file__).parent
        / "fixtures"
        / "sta_violating_endpoints"
        / "sg13g2_capture.json"
    ).read_text(encoding="utf-8")
)

_CAPTURE = _VIOLATING_ENDPOINT_CAPTURES["scenarios"]["two_group_violators"]

#: The three violating endpoints per side the capture's design
#: (`sg13g2_capture.json`'s netlist + SDC) reports, worst first after the
#: global resort. Expected rounded from the captured full-precision slacks.
_EXPECTED_SETUP = [
    {
        "check_type": "output_setup",
        "engine_check_role": "output setup",
        "startpoint": "b",
        "endpoint": "y2",
        "slack_ns": -0.17954,
        "path_group": "oclk1",
    },
    {
        "check_type": "output_setup",
        "engine_check_role": "output setup",
        "startpoint": "a",
        "endpoint": "y1",
        "slack_ns": -0.15888,
        "path_group": "oclk1",
    },
    {
        "check_type": "output_setup",
        "engine_check_role": "output setup",
        "startpoint": "b",
        "endpoint": "y3",
        "slack_ns": -0.15111,
        "path_group": "oclk2",
    },
]
_EXPECTED_HOLD = [
    {
        "check_type": "output_hold",
        "engine_check_role": "output hold",
        "startpoint": "a",
        "endpoint": "y3",
        "slack_ns": -0.26624,
        "path_group": "oclk2",
    },
    {
        "check_type": "output_hold",
        "engine_check_role": "output hold",
        "startpoint": "a",
        "endpoint": "y1",
        "slack_ns": -0.24449,
        "path_group": "oclk1",
    },
    {
        "check_type": "output_hold",
        "engine_check_role": "output hold",
        "startpoint": "b",
        "endpoint": "y2",
        "slack_ns": -0.22058,
        "path_group": "oclk1",
    },
]


def _capture_block_stdout() -> str:
    return _CAPTURE["stdout_block_ask_11"]


# -- parsing and the response block ----------------------------------------- #


def test_violating_endpoints_from_the_capture(tmp_path, monkeypatch):
    """Real engine output: both sides globally worst-first (the hold side's
    worst row lives in the second-queried group, so this exercises the
    resort), counts agreeing, nothing truncated."""
    block = _violating_endpoints(
        _capture_block_stdout(),
        10,
        _CAPTURE["violator_counts"]["setup"],
        _CAPTURE["violator_counts"]["hold"],
    )
    assert block == {
        "status": "ok",
        "reason": None,
        "setup": _EXPECTED_SETUP,
        "setup_truncated": False,
        "hold": _EXPECTED_HOLD,
        "hold_truncated": False,
    }


def test_violating_endpoints_resorts_across_groups():
    """The engine returns rows group-major with per-group order only
    (verified live: the capture's hold side is y1, y2, y3 in emission
    order); the response must be worst-first across groups."""
    hold_endpoints = [row["endpoint"] for row in _EXPECTED_HOLD]
    assert hold_endpoints == ["y3", "y1", "y2"]


def test_violating_endpoints_truncates_after_the_global_resort():
    block = _violating_endpoints(_capture_block_stdout(), 2, 3, 3)
    assert block["status"] == "ok"
    assert block["setup"] == _EXPECTED_SETUP[:2]
    assert block["hold"] == _EXPECTED_HOLD[:2]
    assert block["setup_truncated"] is True
    assert block["hold_truncated"] is True


def test_violating_endpoints_cap_one_keeps_only_the_worst_per_side():
    block = _violating_endpoints(_capture_block_stdout(), 1, 3, 3)
    assert block["setup"] == _EXPECTED_SETUP[:1]
    assert block["hold"] == _EXPECTED_HOLD[:1]
    assert block["setup_truncated"] is True
    assert block["hold_truncated"] is True


def test_violating_endpoints_per_group_ask_recovers_the_whole_list():
    """The ask-2 capture (per-group cap 2, the knob-1 request shape) still
    carries every violating endpoint of this design -- no group holds more
    than 2 -- so a knob-10 parse of it is complete and untruncated."""
    block = _violating_endpoints(_CAPTURE["stdout_block_ask_2"], 10, 3, 3)
    assert block["status"] == "ok"
    assert len(block["setup"]) == 3
    assert len(block["hold"]) == 3
    assert block["setup_truncated"] is False


def test_violating_endpoints_zero_violations_is_ok():
    block = _violating_endpoints(
        "===KLT_STA_VIOLATING_ENDPOINTS_BEGIN===\n"
        "===KLT_STA_VIOLATING_ENDPOINTS_END===\n",
        10,
        0,
        0,
    )
    assert block == {
        "status": "ok",
        "reason": None,
        "setup": [],
        "setup_truncated": False,
        "hold": [],
        "hold_truncated": False,
    }


def test_violating_endpoints_ignores_stray_engine_lines():
    block = _violating_endpoints(
        "===KLT_STA_VIOLATING_ENDPOINTS_BEGIN===\n"
        "Warning 527: something the engine said mid-block\n"
        + _capture_block_stdout().splitlines()[1]
        + "\n===KLT_STA_VIOLATING_ENDPOINTS_END===\n",
        10,
        1,
        0,
    )
    assert block["status"] == "ok"
    assert len(block["setup"]) == 1
    assert block["hold"] == []


def test_violating_endpoints_ties_keep_engine_order():
    stdout = (
        "===KLT_STA_VIOLATING_ENDPOINTS_BEGIN===\n"
        "VPATH\tmax\trecovery\t-1e-10\trst\tff_a/RESET_B\tasynchronous\n"
        "VPATH\tmax\tsetup\t-1e-10\ta\tff_b/D\tclk\n"
        "===KLT_STA_VIOLATING_ENDPOINTS_END===\n"
    )
    block = _violating_endpoints(stdout, 10, 2, 0)
    assert [row["engine_check_role"] for row in block["setup"]] == [
        "recovery",
        "setup",
    ]


def test_violating_endpoints_async_roles_keep_their_own_check_type():
    stdout = (
        "===KLT_STA_VIOLATING_ENDPOINTS_BEGIN===\n"
        "VPATH\tmax\trecovery\t-4.1e-10\trst_n\tff_a/RESET_B\tasynchronous\n"
        "VPATH\tmin\tremoval\t-2.9e-10\trst_n\tff_a/RESET_B\tasynchronous\n"
        "===KLT_STA_VIOLATING_ENDPOINTS_END===\n"
    )
    block = _violating_endpoints(stdout, 10, 1, 1)
    assert block["setup"][0]["check_type"] == "recovery"
    assert block["hold"][0]["check_type"] == "removal"
    assert block["setup"][0]["path_group"] == "asynchronous"


@pytest.mark.parametrize(
    ("block", "reason"),
    [
        (None, "absent from engine output"),
        (['ERROR\tinvalid command name "find_timing_paths"'], "introspection failed"),
        (["VPATH\tmax\tsetup\t1e-10\ta\tff/D"], "malformed violating-endpoint record"),
        (["VPATH\tsideways\tsetup\t-1e-10\ta\tff/D\tclk"], "malformed"),
        (
            ["VPATH\tmax\tsetup\tfast\ta\tff/D\tclk"],
            "malformed violating-endpoint slack",
        ),
        (["VPATH\tmax\tsetup\t-inf\ta\tff/D\tclk"], "non-finite"),
        (
            ["VPATH\tmax\tsetup\t-1e-10\t\tff/D\tclk"],
            "malformed violating-endpoint record",
        ),
    ],
)
def test_violating_endpoints_unavailable_never_fabricated(block, reason):
    """A failed parse yields empty lists and a reason -- never a partial
    list built from the good rows."""
    if block is None:
        stdout = "no markers here"
    else:
        stdout = "\n".join(
            [
                _VIOLATING_ENDPOINTS_BEGIN,
                *block,
                _VIOLATING_ENDPOINTS_END,
                "",
            ]
        )
    result = _violating_endpoints(stdout, 10, 2, 1)
    assert result["status"] == "unavailable"
    assert reason in result["reason"]
    assert result["setup"] == []
    assert result["hold"] == []
    assert result["setup_truncated"] is False
    assert result["hold_truncated"] is False


# -- count agreement -------------------------------------------------------- #


def test_violating_endpoints_short_list_is_inconsistent():
    """A side whose list is shorter than its violation count (and not
    truncated -- nothing proves more rows exist) is reported, not silently
    shortened."""
    block = _violating_endpoints(_capture_block_stdout(), 10, 2, 3)
    assert block["status"] == "inconsistent"
    assert "setup" in block["reason"]
    assert "setup_violation_count is 2" in block["reason"]
    assert len(block["setup"]) == 3
    assert len(block["hold"]) == 3


def test_violating_endpoints_truncated_count_below_cap_is_inconsistent():
    """Truncation proves the engine found more than the cap, so a count at
    or below the cap cannot describe the same population."""
    block = _violating_endpoints(_capture_block_stdout(), 2, 2, 3)
    assert block["status"] == "inconsistent"
    assert "setup" in block["reason"]
    assert "hold" not in block["reason"]
    assert len(block["setup"]) == 2


def test_violating_endpoints_both_sides_disagree_names_both():
    block = _violating_endpoints(_capture_block_stdout(), 2, 1, 1)
    assert block["status"] == "inconsistent"
    assert "setup" in block["reason"]
    assert "hold" in block["reason"]
