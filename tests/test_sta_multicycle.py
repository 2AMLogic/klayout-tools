"""``klt sta`` ``constraints.multicycle_paths`` (issue #6921): the request
validator and its bounded selector grammar, the paired
``set_multicycle_path`` emitter, the without-session script derivation,
and the with/without measurement parsing and response assembly.

The stubbed ``openroad`` integration tests (``run_sta`` script shape,
response fields, request-error surfacing) and the pinned-engine feedback
fixture -- #6908's release criterion -- live in
``test_sta_multicycle_run.py``, split from this file for the per-file size
ratchet."""

from __future__ import annotations

import re

import pytest

from klayout_tools._sta_multicycle import (
    MCP_BEGIN,
    MCP_END,
    MCPERR_BEGIN,
    MCPERR_END,
    multicycle_lines,
    validate_multicycle_paths,
    without_script_lines,
)
from klayout_tools.post_route_sta import PostRouteStaError
from klayout_tools.post_route_sta_multicycle import (
    multicycle_request_error,
    multicycle_response,
    no_timed_path_error,
    parse_multicycle_measurements,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_ENTRY = {"from": "ff_fb", "to": "ff_fb", "setup": 4, "hold": 3}
_ENTRY_THROUGH = {**_ENTRY, "through": "u_mux/X"}


# -- validation ------------------------------------------------------------- #


def test_validate_omitted_null_and_empty_are_none():
    assert validate_multicycle_paths({}) is None
    assert validate_multicycle_paths({"multicycle_paths": None}) is None
    assert validate_multicycle_paths({"multicycle_paths": []}) is None


def test_validate_normalizes_entries_with_index():
    entries = validate_multicycle_paths({"multicycle_paths": [_ENTRY, _ENTRY_THROUGH]})
    assert entries == [
        {
            "index": 0,
            "from": "ff_fb",
            "through": None,
            "to": "ff_fb",
            "setup": 4,
            "hold": 3,
        },
        {
            "index": 1,
            "from": "ff_fb",
            "through": "u_mux/X",
            "to": "ff_fb",
            "setup": 4,
            "hold": 3,
        },
    ]


def test_validate_null_through_is_absent():
    assert (
        validate_multicycle_paths({"multicycle_paths": [{**_ENTRY, "through": None}]})[
            0
        ]["through"]
        is None
    )


def test_validate_bus_and_wildcard_selectors_are_literal():
    entries = validate_multicycle_paths(
        {
            "multicycle_paths": [
                {"from": "q[0]", "to": "ff_?/D", "setup": 2, "hold": 1},
            ]
        }
    )
    assert (entries[0]["from"], entries[0]["to"]) == ("q[0]", "ff_?/D")


_MALFORMED_SELECTORS = ["a b", "a;b", "{a}", "$a", "-a", "a\\b"]
_BAD_MULTIPLIERS = [True, False, 0, -2, 1.5, "4"]


@pytest.mark.parametrize("selector", _MALFORMED_SELECTORS)
@pytest.mark.parametrize("key", ["from", "to", "through"])
def test_validate_rejects_malformed_selectors(key, selector):
    with pytest.raises(PostRouteStaError, match="malformed selector"):
        validate_multicycle_paths({"multicycle_paths": [{**_ENTRY, key: selector}]})


@pytest.mark.parametrize("multiplier", _BAD_MULTIPLIERS)
@pytest.mark.parametrize("key", ["setup", "hold"])
def test_validate_rejects_bad_multipliers(key, multiplier):
    with pytest.raises(PostRouteStaError, match="must be a positive integer"):
        validate_multicycle_paths({"multicycle_paths": [{**_ENTRY, key: multiplier}]})


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("nope", "must be a list of objects"),
        ({"from": "a"}, "must be a list of objects"),
        ([7], "must be an object"),
        ([{**_ENTRY, "extra": 1}], "unknown key(s): extra"),
        ([{**_ENTRY, "from": ""}], "must be a non-empty selector string"),
        ([{**_ENTRY, "to": ["ff"]}], "must be a non-empty selector string"),
        (
            [{k: v for k, v in _ENTRY.items() if k != "hold"}],
            "must be a positive integer",
        ),
    ],
)
def test_validate_rejects(value, message):
    with pytest.raises(PostRouteStaError, match=re.escape(message)):
        validate_multicycle_paths({"multicycle_paths": value})


def test_validate_error_names_entry_index():
    with pytest.raises(PostRouteStaError) as excinfo:
        validate_multicycle_paths(
            {"multicycle_paths": [_ENTRY, {**_ENTRY, "setup": 0}]}
        )
    assert "constraints.multicycle_paths[1].setup" in str(excinfo.value)


# -- emitter ---------------------------------------------------------------- #


def test_lines_none_and_empty_emit_nothing():
    assert multicycle_lines(None) == []
    assert multicycle_lines([]) == []


def test_lines_pair_setup_and_hold_over_identical_selection():
    lines = multicycle_lines(
        validate_multicycle_paths({"multicycle_paths": [_ENTRY_THROUGH]})
    )
    applied = [ln for ln in lines if ln.startswith("set_multicycle_path")]
    assert len(applied) == 2
    assert applied[0] == (
        "set_multicycle_path 4 -setup "
        "-from $klt_mcp_from(0) -through $klt_mcp_thr(0) "
        "-to $klt_mcp_to(0) ;# klt mcp 0"
    )
    assert applied[1] == (
        "set_multicycle_path 3 -hold "
        "-from $klt_mcp_from(0) -through $klt_mcp_thr(0) "
        "-to $klt_mcp_to(0) ;# klt mcp 0"
    )


def test_lines_without_through_omit_the_flag():
    lines = multicycle_lines(validate_multicycle_paths({"multicycle_paths": [_ENTRY]}))
    applied = [ln for ln in lines if ln.startswith("set_multicycle_path")]
    assert all("-through" not in ln for ln in applied)
    assert applied[0].endswith("-to $klt_mcp_to(0) ;# klt mcp 0")


def test_lines_resolve_measure_and_fail_closed():
    lines = multicycle_lines(validate_multicycle_paths({"multicycle_paths": [_ENTRY]}))
    # Resolution is engine-side (liberty sequential-ness via is_clock, the
    # applied-clock test for unclocked registers), never a netlist regex.
    assert "proc klt_mcp_end_objs {klt_sel} {" in lines
    assert any("get_property $klt_p is_clock" in line for line in lines)
    assert any("get_property $klt_p clocks" in line for line in lines)
    assert f'puts "{MCPERR_BEGIN}"' in lines
    assert "    exit 3" in lines
    # Measurement: both check sides, bounded enumeration, after the pair.
    text = "\n".join(lines)
    assert f'puts "{MCP_BEGIN}"' in lines
    for side in ("max", "min"):
        assert (
            f"find_timing_paths -from $klt_mcp_from(0) "
            f"-to $klt_mcp_to(0) -path_delay {side} "
            "-group_path_count 100000 -endpoint_path_count 1" in text
        )
    assert text.index("set_multicycle_path 4 -setup") < text.index(
        f'puts "{MCP_BEGIN}"'
    )


def test_lines_quote_selectors_as_data():
    lines = multicycle_lines(
        validate_multicycle_paths(
            {"multicycle_paths": [{**_ENTRY, "from": "q[0]", "to": "ff_b/D"}]}
        )
    )
    resolved = next(ln for ln in lines if ln.startswith("foreach {klt_i"))
    # bus brackets backslash-escaped, one literal Tcl word, no substitution
    assert " q\\[0\\] {} ff_b/D] {" in resolved
    assert "$" not in resolved


# -- without-session derivation --------------------------------------------- #


def test_without_script_drops_only_that_entrys_pair():
    entries = validate_multicycle_paths({"multicycle_paths": [_ENTRY, _ENTRY_THROUGH]})
    assert [entry["index"] for entry in entries] == [0, 1]
    lines = multicycle_lines(entries)
    without = without_script_lines(lines, 0)
    assert not any(line.endswith(";# klt mcp 0") for line in without)
    assert any(line.endswith(";# klt mcp 1") for line in without)
    assert len(without) == len(lines) - 2  # exactly the setup+hold pair


def test_without_script_drops_sdf_write():
    lines = ["read_def x", "write_sdf -divider . out.sdf", "report_checks"]
    assert without_script_lines(lines, 0) == ["read_def x", "report_checks"]


# -- stdout parsing ---------------------------------------------------------- #


def _mcp_block(*rows: str) -> str:
    return "\n".join([MCP_BEGIN, *rows, MCP_END])


def _mcperr_block(*rows: str) -> str:
    return "\n".join([MCPERR_BEGIN, *rows, MCPERR_END])


def test_request_error_none_without_block_and_rows():
    assert multicycle_request_error("") is None
    assert multicycle_request_error(_mcperr_block()) is None
    assert multicycle_request_error(_mcp_block()) is None


def test_request_error_names_index_and_each_problem():
    stdout = _mcperr_block(
        "MCPERR\t0\tfrom matched_nothing",
        "MCPERR\t1\tthrough matched_nothing",
        "MCPERR\t2\tto unclocked_register ff_u",
    )
    message = multicycle_request_error(stdout)
    assert message.startswith("constraints.multicycle_paths rejected: ")
    assert (
        "constraints.multicycle_paths[0].from matches no register or pin in "
        "the linked design" in message
    )
    assert (
        "constraints.multicycle_paths[1].through matches no pin in the "
        "linked design" in message
    )
    assert (
        "constraints.multicycle_paths[2].to selects unclocked register "
        "'ff_u'" in message
    )


def test_request_error_passthrough_for_unexpected_message():
    message = multicycle_request_error(_mcperr_block("MCPERR\t0\tboom"))
    assert message == (
        "constraints.multicycle_paths rejected: constraints.multicycle_paths[0]: boom"
    )


def test_no_timed_path_error_names_selection():
    entry = validate_multicycle_paths({"multicycle_paths": [_ENTRY_THROUGH]})[0]
    message = no_timed_path_error(entry)
    assert "constraints.multicycle_paths[0]" in message
    assert (
        "'ff_fb'" in message and "'u_mux/X'" in message and "no timed path" in message
    )


def test_parse_rows_slack_and_endpoints():
    entries = validate_multicycle_paths({"multicycle_paths": [_ENTRY]})
    stdout = _mcp_block(
        "MCP\t0\tmax\t5.465151234\tff_fb/D",
        "MCP\t0\tmax\t5.500000001\tff_fb/D",
        "MCP\t0\tmin\t2.29215\tff_fb/D",
    )
    parsed = parse_multicycle_measurements(stdout, entries)
    assert parsed[0]["setup"] == [
        (5.465151234, "ff_fb/D"),
        (5.500000001, "ff_fb/D"),
    ]
    assert parsed[0]["hold"] == [(2.29215, "ff_fb/D")]
    assert parsed[0]["reason"] is None


def test_parse_measure_failure_is_null_never_zero():
    entries = validate_multicycle_paths({"multicycle_paths": [_ENTRY]})
    stdout = _mcp_block(
        "MCP\t0\tmax\t5.4\tff_fb/D",
        "MCPERR\t0\tmin\tsta engine exploded",
    )
    parsed = parse_multicycle_measurements(stdout, entries)
    assert parsed[0]["setup"] == [(5.4, "ff_fb/D")]
    assert parsed[0]["hold"] is None
    assert "hold: engine query failed: sta engine exploded" in parsed[0]["reason"]


def test_parse_missing_block_is_unmeasured():
    entries = validate_multicycle_paths({"multicycle_paths": [_ENTRY]})
    parsed = parse_multicycle_measurements("nothing here", entries)
    assert parsed[0]["setup"] is None and parsed[0]["hold"] is None
    assert parsed[0]["reason"]


def test_parse_malformed_rows_fail_that_side():
    entries = validate_multicycle_paths({"multicycle_paths": [_ENTRY]})
    stdout = _mcp_block(
        "MCP\t0\tmax\tnan\tff_fb/D",
        "MCP\t0\tmin\t1e999\tff_fb/D",
    )
    parsed = parse_multicycle_measurements(stdout, entries)
    assert parsed[0]["setup"] is None and parsed[0]["hold"] is None
    assert "malformed" in parsed[0]["reason"] or "non-finite" in parsed[0]["reason"]


def test_parse_empty_rows_is_no_paths_not_failure():
    entries = validate_multicycle_paths({"multicycle_paths": [_ENTRY]})
    parsed = parse_multicycle_measurements(_mcp_block(""), entries)
    assert parsed[0]["setup"] == [] and parsed[0]["hold"] == []
    assert parsed[0]["reason"] is None


def _measurements(setup_rows, hold_rows, without_setup, without_hold):
    entries = validate_multicycle_paths({"multicycle_paths": [_ENTRY]})
    with_rows = {"setup": setup_rows, "hold": hold_rows, "reason": None}
    without_rows = {"setup": without_setup, "hold": without_hold, "reason": None}
    return entries, {0: with_rows}, {0: without_rows}


def test_response_merges_with_without_and_counts_endpoints():
    entries, with_rows, without_rows = _measurements(
        [(5.465151234, "ff_fb/D"), (5.500000001, "ff_fb/D")],
        [(2.29215, "ff_fb/D")],
        [(-0.534851234, "ff_fb/D")],
        [(2.29215, "ff_fb/D")],
    )
    (record,) = multicycle_response(entries, with_rows, without_rows)
    assert record == {
        "index": 0,
        "from": "ff_fb",
        "through": None,
        "to": "ff_fb",
        "setup": 4,
        "hold": 3,
        "matched_endpoints": 1,
        "setup_slack_ns": {"with": 5.46515, "without": -0.53485},
        "hold_slack_ns": {"with": 2.29215, "without": 2.29215},
    }


def test_response_null_and_reason_when_a_side_is_unmeasurable():
    entries, with_rows, without_rows = _measurements(
        [(5.4, "ff_fb/D")], None, [], [(1.0, "ff_fb/D")]
    )
    (record,) = multicycle_response(entries, with_rows, without_rows)
    assert record["hold_slack_ns"] == {"with": None, "without": 1.0}
    assert record["setup_slack_ns"]["without"] is None
    assert "hold_slack_ns.with unmeasurable" in record["reason"]
    assert "setup_slack_ns.without: no timed path" in record["reason"]
