"""Emit OpenSTA clock-propagation queries and parse their count and latency evidence."""

from __future__ import annotations

import re
from typing import Any

from ._sta_request_diag import extract_block as _extract_block
from ._tcl import tcl_literal as _tcl_literal

#: Issue #2739: brackets the ``report_clock_latency -digits 6`` dump a
#: ``propagated_clock`` request emits after ``set_propagated_clock`` (and,
#: when present, after ``read_spef``) -- evidence of what propagation
#: actually reached. Per clock: a ``Clock <name>`` header line, then one
#: (min-column) and one continuation (max-column) ``network latency <pin>``
#: row for the **min- and max-latency sink of each transition pair** -- the
#: extremes the report names, not one row per sink (issue #7417: on a
#: many-flop tree only those two pins per table ever print, so the rows can
#: never yield the sink count; that comes from
#: :func:`_clock_sink_count_lines`). A header can appear on its own even
#: when the clock reaches register pins.
_CLOCK_LATENCY_BEGIN = "===KLT_STA_CLOCK_LATENCY_BEGIN==="
_CLOCK_LATENCY_END = "===KLT_STA_CLOCK_LATENCY_END==="
#: ``report_clock_latency``'s per-clock section header (``Clock clk``).
_CLOCK_SECTION_RE = re.compile(r"^Clock (\S+)$")
#: One ``network latency <pin>`` row: the min-column value on the row
#: OpenSTA prints for the sink, and the max-column value on the continuation
#: line it indents under the same pin -- one regex captures both.
_NETWORK_LATENCY_RE = re.compile(r"^\s*([-+0-9.eE]+)\s+network latency (.+?)\s*$")
#: Upper bound on how many clock-sink pin names the parsed evidence echoes
#: into ``clock_propagation.clock_sinks_sample`` -- a diagnostic sample of
#: the pins the latency report names, not an exhaustive list (a real CTS
#: tree reaches thousands of clock pins; the exhaustive number is the
#: engine-queried ``clock_sinks``, issue #7417).
_CLOCK_SINK_SAMPLE_LIMIT = 20
#: Issue #7417: brackets :func:`_clock_sink_count_lines` -- the engine
#: query that counts the request clock's sink pins, so
#: ``clock_propagation.clock_sinks`` is the true sink count rather than the
#: number of pins the latency report happens to name.
_CLOCK_SINK_COUNT_BEGIN = "===KLT_STA_CLOCK_SINK_COUNT_BEGIN==="
_CLOCK_SINK_COUNT_END = "===KLT_STA_CLOCK_SINK_COUNT_END==="
#: The one parseable row inside the sink-count block: the count itself. An
#: ``KLT_STA_CLOCK_SINK_COUNT_ERROR <msg>`` row (the ``catch`` arm) matches
#: nothing and parses as unknown.
_CLOCK_SINK_COUNT_RE = re.compile(r"^KLT_STA_CLOCK_SINK_COUNT (\d+)$")


def _clock_latency_lines() -> list[str]:
    """Marker-delimited ``report_clock_latency -digits 6`` (issue #2739) --
    evidence of what propagation actually reached: one ``Clock <name>``
    section per clock, ``network latency <pin>`` rows for the min- and
    max-latency sink of each transition pair (the extremes, not every
    sink -- issue #7417; the sink *count* comes from
    :func:`_clock_sink_count_lines`, which runs right after this block).
    Emitted after any ``read_spef`` and before the timing metrics, so the
    latencies it reports reflect the same parasitics the timing numbers
    do. The separate sink-count query also covers clocks for which the
    latency report prints only a header.
    Wrapped in ``catch`` so an OpenSTA build without the command degrades
    the evidence to ``unknown`` rather than aborting the run (the same
    degradation :func:`_parasitic_annotation_lines` established)."""
    return [
        f'puts "{_CLOCK_LATENCY_BEGIN}"',
        "catch {report_clock_latency -digits 6} klt_clock_latency_msg",
        f'puts "{_CLOCK_LATENCY_END}"',
    ]


def _clock_sink_count_lines(clock_port: str) -> list[str]:
    """Marker-delimited count of *clock_port*'s sink pins (issue #7417) --
    OpenSTA's own ``all_registers -clock [get_clocks <clock_port>]
    -clock_pins`` enumeration, so ``clock_propagation.clock_sinks`` is the
    true number of register clock pins the clock network drives (through
    the CTS buffers), never the count of pins ``report_clock_latency``
    happens to name (its per-transition tables print only the min- and
    max-latency sink, so that count under-reports every real tree --
    live-verified on the pinned build: 4 flops behind buffers -> count 4
    while the latency report named no pin at all; a clock reaching no flop
    -> count 0).

    The ``get_clocks`` result is checked to be exactly one clock first: a
    miss warns (STA-0351) and returns an *empty* collection, and
    ``all_registers -clock {}`` silently drops the filter and would count
    every register in the design -- so the guard turns that into the
    ``catch`` arm's ``KLT_STA_CLOCK_SINK_COUNT_ERROR`` row
    (:func:`_parse_clock_sink_count` -> unknown) instead of a wrong
    number. Error text is flattened to one line (the
    :func:`_constraint_completeness_lines` convention) so a newline in an
    engine message can never inject a parseable count row."""
    return [
        f'puts "{_CLOCK_SINK_COUNT_BEGIN}"',
        "if {[catch {",
        f"        set klt_sink_clks [get_clocks {_tcl_literal(clock_port)}]",
        "        if {[llength $klt_sink_clks] != 1} {",
        '            error "get_clocks did not name exactly one clock"',
        "        }",
        "        llength [all_registers -clock $klt_sink_clks -clock_pins]",
        "    } klt_sink_msg]} {",
        '        puts "KLT_STA_CLOCK_SINK_COUNT_ERROR '
        '[string map [list \\n { } \\t { }] $klt_sink_msg]"',
        "    } else {",
        '        puts "KLT_STA_CLOCK_SINK_COUNT $klt_sink_msg"',
        "    }",
        f'puts "{_CLOCK_SINK_COUNT_END}"',
    ]


def _parse_clock_latency(stdout: str, clock_name: str) -> dict[str, Any]:
    """The ``clock_propagation`` evidence block (issue #2739), parsed from
    one session's :data:`_CLOCK_LATENCY_BEGIN`/:data:`_CLOCK_LATENCY_END`
    ``report_clock_latency`` dump and
    :data:`_CLOCK_SINK_COUNT_BEGIN`/:data:`_CLOCK_SINK_COUNT_END`
    sink-count query for the request's clock:

    - ``clock_sinks`` -- the number of clock sink pins the engine's own
      ``all_registers -clock ... -clock_pins`` enumeration returned (issue
      #7417: the latency report names only the min-/max-latency pin per
      transition table, so its distinct-pin count under-reports every
      real tree and is never used for this field). ``0`` is the explicit
      "propagation could not be established" signal -- the count query
      returned zero (a clock reaching no clock pin), or, on a
      count-marker-less stdout (an older capture), the section header
      printed with no ``network latency`` row under it. ``None`` means
      unknown. A failed or incomplete count query yields ``None`` even
      when the latency section consists only of its header.
    - ``max_network_latency_ns`` -- the worst ``network latency`` value the
      section reports (the max-column continuation values included);
      ``None`` when there are no sinks (or the section is absent).
    - ``clock_sinks_sample`` -- the pin names the latency report names
      (the per-transition min/max extremes), capped at
      :data:`_CLOCK_SINK_SAMPLE_LIMIT` (a diagnostic sample, mirroring the
      SPEF missing-nets sample, not an exhaustive list and not every
      sink).

    A request clock whose section is absent is ``unknown``, never ``0``:
    ``create_clock`` already failed the session for a missing clock port,
    so absence here means the evidence command itself did not run -- not
    that the tree is empty.
    """
    block = _extract_block(stdout, _CLOCK_LATENCY_BEGIN, _CLOCK_LATENCY_END)
    section_seen, rows = (
        _clock_latency_rows(block, clock_name) if block is not None else (False, [])
    )
    seen: set[str] = set()
    sample: list[str] = []
    for _latency_s, pin in rows:
        if pin not in seen:
            seen.add(pin)
            if len(sample) < _CLOCK_SINK_SAMPLE_LIMIT:
                sample.append(pin)
    max_latency = _max_network_latency(rows)
    clock_sinks = _parse_clock_sink_count(stdout)
    if (
        clock_sinks is None
        and section_seen
        and not rows
        and _CLOCK_SINK_COUNT_BEGIN not in stdout
        and _CLOCK_SINK_COUNT_END not in stdout
    ):
        clock_sinks = 0
    return {
        "clock_sinks": clock_sinks,
        "max_network_latency_ns": (
            round(max_latency, 6) if max_latency is not None else None
        ),
        "clock_sinks_sample": sample,
    }


def _parse_clock_sink_count(stdout: str) -> int | None:
    """The request clock's sink count from
    :func:`_clock_sink_count_lines`' marker block: the integer on the
    ``KLT_STA_CLOCK_SINK_COUNT <n>`` row, or ``None`` when the block is
    absent (a marker-less stdout from an older stub/capture), carries only
    the ``catch`` arm's ``KLT_STA_CLOCK_SINK_COUNT_ERROR`` row, or holds no
    parseable count -- unknown, never a guess."""
    block = _extract_block(stdout, _CLOCK_SINK_COUNT_BEGIN, _CLOCK_SINK_COUNT_END)
    if block is None:
        return None
    for line in block.splitlines():
        count = _CLOCK_SINK_COUNT_RE.match(line.rstrip())
        if count is not None:
            return int(count.group(1))
    return None


def _clock_latency_rows(
    block: str, clock_name: str
) -> tuple[bool, list[tuple[str, str]]]:
    """``(section_seen, rows)`` for *clock_name* in a ``report_clock_latency``
    block: whether the clock's section header appeared at all, and the
    ``(latency, pin)`` string pairs of every ``network latency`` row under
    it, in report order (duplicates across rise/fall tables kept)."""
    rows: list[tuple[str, str]] = []
    section_seen = False
    current_section: str | None = None
    for line in block.splitlines():
        header = _CLOCK_SECTION_RE.match(line.rstrip())
        if header:
            current_section = header.group(1)
            section_seen = section_seen or current_section == clock_name
            continue
        if current_section != clock_name:
            continue
        row = _NETWORK_LATENCY_RE.match(line.rstrip())
        if row is not None:
            latency_s, pin = row.groups()
            rows.append((latency_s, pin))
    return section_seen, rows


def _max_network_latency(rows: list[tuple[str, str]]) -> float | None:
    """The largest parseable latency among *rows* (see
    :func:`_clock_latency_rows`), or ``None`` when none parses."""
    max_latency: float | None = None
    for latency_s, _pin in rows:
        try:
            latency = float(latency_s)
        except ValueError:
            continue
        if max_latency is None or latency > max_latency:
            max_latency = latency
    return max_latency
