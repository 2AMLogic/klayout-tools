"""Clock propagation evidence from latency reports and clock-sink counts."""

from __future__ import annotations

import pytest

from klayout_tools import post_route_sta

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def test_parse_clock_latency_sinks_max_and_sample():
    stdout = "\n".join(
        [
            f'puts "{post_route_sta._CLOCK_LATENCY_BEGIN}"',
            "Clock clk",
            "rise -> rise",
            "    min     max",
            "0.000000 0.000000 source latency",
            "0.302855         network latency ff/CLK",
            "        0.302855 network latency ff/CLK",
            "---------------",
            "0.302855 0.302855 latency",
            "        0.000000 skew",
            "",
            "fall -> fall",
            "    min     max",
            "0.000000 0.000000 source latency",
            "0.335104         network latency ff/CLK",
            "        0.335104 network latency ff/CLK",
            "---------------",
            "0.335104 0.335104 latency",
            "        0.000000 skew",
            "",
            f'puts "{post_route_sta._CLOCK_LATENCY_END}"',
            f'puts "{post_route_sta._CLOCK_SINK_COUNT_BEGIN}"',
            "KLT_STA_CLOCK_SINK_COUNT 1",
            f'puts "{post_route_sta._CLOCK_SINK_COUNT_END}"',
        ]
    )

    evidence = post_route_sta._parse_clock_latency(stdout, "clk")

    assert evidence == {
        "clock_sinks": 1,
        "max_network_latency_ns": 0.335104,
        "clock_sinks_sample": ["ff/CLK"],
    }


def test_parse_clock_latency_count_exceeds_latency_report_pins():
    """Issue #7417's reported harm, fixed: the latency report names only
    the min-/max-latency pin per transition table, so on a real tree its
    distinct-pin count (2 here) under-reports the sink count. With the
    engine-queried count marker present, `clock_sinks` is that count --
    while `clock_sinks_sample` still echoes just the report-named pins."""
    stdout = "\n".join(
        [
            f'puts "{post_route_sta._CLOCK_LATENCY_BEGIN}"',
            "Clock clk",
            "rise -> rise",
            "    min     max",
            "0.206505         network latency ff2/CLK",
            "        0.312403 network latency ff1/CLK",
            "---------------",
            "0.206505 0.312403 latency",
            "        0.105898 skew",
            "",
            "fall -> fall",
            "    min     max",
            "0.223682         network latency ff2/CLK",
            "        0.342710 network latency ff1/CLK",
            "---------------",
            "0.223682 0.342710 latency",
            "        0.119028 skew",
            "",
            f'puts "{post_route_sta._CLOCK_LATENCY_END}"',
            f'puts "{post_route_sta._CLOCK_SINK_COUNT_BEGIN}"',
            "KLT_STA_CLOCK_SINK_COUNT 517",
            f'puts "{post_route_sta._CLOCK_SINK_COUNT_END}"',
        ]
    )

    evidence = post_route_sta._parse_clock_latency(stdout, "clk")

    assert evidence["clock_sinks"] == 517
    assert evidence["clock_sinks_sample"] == ["ff2/CLK", "ff1/CLK"]
    assert evidence["max_network_latency_ns"] == 0.34271


def test_parse_clock_latency_rows_without_count_marker_is_unknown():
    """The #7417 bug's exact shape: a latency dump with rows but no
    sink-count block (an older capture/stub) must not fall back to the
    distinct-pin count -- that number under-reports every real tree."""
    stdout = "\n".join(
        [
            f'puts "{post_route_sta._CLOCK_LATENCY_BEGIN}"',
            "Clock clk",
            "0.206505         network latency ff2/CLK",
            "        0.312403 network latency ff1/CLK",
            f'puts "{post_route_sta._CLOCK_LATENCY_END}"',
        ]
    )

    evidence = post_route_sta._parse_clock_latency(stdout, "clk")

    assert evidence["clock_sinks"] is None
    assert evidence["max_network_latency_ns"] == 0.312403
    assert evidence["clock_sinks_sample"] == ["ff2/CLK", "ff1/CLK"]


@pytest.mark.parametrize(
    "count_row",
    [
        "KLT_STA_CLOCK_SINK_COUNT_ERROR get_clocks did not name exactly one clock",
        "KLT_STA_CLOCK_SINK_COUNT not-a-number",
        "engine banner noise the markers can bracket",
    ],
)
def test_parse_clock_latency_count_query_failed_is_unknown(count_row):
    """A `catch`-swallowed failure (no `all_registers` on the build, the
    exactly-one-clock guard firing) or a malformed count row leaves no
    parseable count -- unknown, never a fabricated number and never the
    latency report's distinct-pin count (rows are present here)."""
    stdout = "\n".join(
        [
            f'puts "{post_route_sta._CLOCK_LATENCY_BEGIN}"',
            "Clock clk",
            "0.206505         network latency ff2/CLK",
            "        0.312403 network latency ff1/CLK",
            f'puts "{post_route_sta._CLOCK_LATENCY_END}"',
            f'puts "{post_route_sta._CLOCK_SINK_COUNT_BEGIN}"',
            count_row,
            f'puts "{post_route_sta._CLOCK_SINK_COUNT_END}"',
        ]
    )

    evidence = post_route_sta._parse_clock_latency(stdout, "clk")

    assert evidence["clock_sinks"] is None
    assert evidence["clock_sinks_sample"] == ["ff2/CLK", "ff1/CLK"]


def test_parse_clock_latency_dedupes_sinks_and_caps_sample():
    lines = [f'puts "{post_route_sta._CLOCK_LATENCY_BEGIN}"', "Clock clk"]
    for i in range(post_route_sta._CLOCK_SINK_SAMPLE_LIMIT + 5):
        lines.append(f"0.0{i}         network latency ff{i}/CLK")
    lines += [
        "        0.990000 network latency ff1/CLK",  # max col of an early sink
        f'puts "{post_route_sta._CLOCK_LATENCY_END}"',
        f'puts "{post_route_sta._CLOCK_SINK_COUNT_BEGIN}"',
        "KLT_STA_CLOCK_SINK_COUNT 1000",
        f'puts "{post_route_sta._CLOCK_SINK_COUNT_END}"',
    ]

    evidence = post_route_sta._parse_clock_latency("\n".join(lines), "clk")

    # The count is the engine-queried number, not the report's distinct
    # pins (which is 26 here) nor the capped sample length.
    assert evidence["clock_sinks"] == 1000
    assert len(evidence["clock_sinks_sample"]) == (
        post_route_sta._CLOCK_SINK_SAMPLE_LIMIT
    )
    assert evidence["clock_sinks_sample"][0] == "ff0/CLK"
    assert evidence["max_network_latency_ns"] == 0.99


def test_parse_clock_latency_ignores_other_clock_sections():
    stdout = "\n".join(
        [
            f'puts "{post_route_sta._CLOCK_LATENCY_BEGIN}"',
            "Clock other",
            "1.000000         network latency x/CLK",
            "Clock clk",
            "0.200000         network latency ff/CLK",
            f'puts "{post_route_sta._CLOCK_LATENCY_END}"',
            f'puts "{post_route_sta._CLOCK_SINK_COUNT_BEGIN}"',
            "KLT_STA_CLOCK_SINK_COUNT 1",
            f'puts "{post_route_sta._CLOCK_SINK_COUNT_END}"',
        ]
    )

    evidence = post_route_sta._parse_clock_latency(stdout, "clk")

    assert evidence == {
        "clock_sinks": 1,
        "max_network_latency_ns": 0.2,
        "clock_sinks_sample": ["ff/CLK"],
    }


@pytest.mark.parametrize(
    "count_lines",
    [
        [
            post_route_sta._CLOCK_SINK_COUNT_BEGIN,
            "KLT_STA_CLOCK_SINK_COUNT_ERROR unavailable",
            post_route_sta._CLOCK_SINK_COUNT_END,
        ],
        [
            post_route_sta._CLOCK_SINK_COUNT_BEGIN,
            "KLT_STA_CLOCK_SINK_COUNT unreadable",
            post_route_sta._CLOCK_SINK_COUNT_END,
        ],
        [post_route_sta._CLOCK_SINK_COUNT_BEGIN, "KLT_STA_CLOCK_SINK_COUNT 4"],
        [post_route_sta._CLOCK_SINK_COUNT_END],
    ],
    ids=["query-error", "bad-count", "missing-end", "missing-begin"],
)
def test_empty_latency_section_with_unusable_sink_count_is_unknown(count_lines):
    stdout = "\n".join(
        [
            post_route_sta._CLOCK_LATENCY_BEGIN,
            "Clock clk",
            post_route_sta._CLOCK_LATENCY_END,
            *count_lines,
        ]
    )
    assert post_route_sta._parse_clock_latency(stdout, "clk")["clock_sinks"] is None


def test_parse_clock_latency_count_zero_with_empty_section_is_zero():
    """The tree-less contract through the count query itself (a clock the
    engine says reaches zero register clock pins), live-verified shape."""
    stdout = "\n".join(
        [
            f'puts "{post_route_sta._CLOCK_LATENCY_BEGIN}"',
            "Clock clk",
            f'puts "{post_route_sta._CLOCK_LATENCY_END}"',
            f'puts "{post_route_sta._CLOCK_SINK_COUNT_BEGIN}"',
            "KLT_STA_CLOCK_SINK_COUNT 0",
            f'puts "{post_route_sta._CLOCK_SINK_COUNT_END}"',
        ]
    )

    evidence = post_route_sta._parse_clock_latency(stdout, "clk")

    assert evidence == {
        "clock_sinks": 0,
        "max_network_latency_ns": None,
        "clock_sinks_sample": [],
    }


def test_parse_clock_latency_missing_block_is_unknown():
    evidence = post_route_sta._parse_clock_latency("no markers here", "clk")

    assert evidence == {
        "clock_sinks": None,
        "max_network_latency_ns": None,
        "clock_sinks_sample": [],
    }


def test_parse_clock_latency_error_text_is_unknown():
    """A `catch`-swallowed command failure (e.g. an OpenSTA build without
    `report_clock_latency`) leaves the command's error text inside the
    markers and no `Clock <name>` section -- unknown, never a fabricated
    sink count."""
    stdout = "\n".join(
        [
            f'puts "{post_route_sta._CLOCK_LATENCY_BEGIN}"',
            "invalid command name report_clock_latency",
            f'puts "{post_route_sta._CLOCK_LATENCY_END}"',
        ]
    )

    assert post_route_sta._parse_clock_latency(stdout, "clk") == {
        "clock_sinks": None,
        "max_network_latency_ns": None,
        "clock_sinks_sample": [],
    }
