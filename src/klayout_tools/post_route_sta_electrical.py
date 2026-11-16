"""Electrical-check (``report_check_types``) markers and parsers for ``klt sta``.

Split out of :mod:`klayout_tools.post_route_sta` (issues #3005, #5241) to keep
that module under its file-size ceiling. ``post_route_sta`` re-imports every
name here, so existing ``post_route_sta._parse_electrical_check`` etc.
references keep working.
"""

from __future__ import annotations

import re
from typing import Any


def _extract_block(text: str, begin: str, end: str) -> str | None:
    """The text between the ``begin``/``end`` markers, or ``None`` when either
    marker is absent (same contract as ``post_route_sta._extract_block``)."""
    try:
        start_idx = text.index(begin) + len(begin)
        stop_idx = text.index(end, start_idx)
    except ValueError:
        return None
    return text[start_idx:stop_idx]


#: Marker pairs and sentinels for the per-corner ``electrical_checks`` block
#: (issue #3005): one ``report_check_types -violators`` region per limit class.
#: See :func:`_electrical_check_lines` / :func:`_parse_electrical_check`.
_MAX_SLEW_BEGIN = "===KLT_STA_MAX_SLEW_BEGIN==="
_MAX_SLEW_END = "===KLT_STA_MAX_SLEW_END==="
_MAX_CAP_BEGIN = "===KLT_STA_MAX_CAP_BEGIN==="
_MAX_CAP_END = "===KLT_STA_MAX_CAP_END==="
_MIN_PULSE_BEGIN = "===KLT_STA_MIN_PULSE_BEGIN==="
_MIN_PULSE_END = "===KLT_STA_MIN_PULSE_END==="
_MIN_PERIOD_BEGIN = "===KLT_STA_MIN_PERIOD_BEGIN==="
_MIN_PERIOD_END = "===KLT_STA_MIN_PERIOD_END==="
_ELECTRICAL_OK = "KLT_ELECTRICAL_OK"
_ELECTRICAL_UNSUPPORTED = "KLT_ELECTRICAL_UNSUPPORTED"
_ELECTRICAL_NET_PREFIX = "KLT_ELECTRICAL_NET"
#: One violator row of ``report_check_types``: ``<pin> <limit> <value> <slack>
#: (VIOLATED)``. Anchored from the right so a pin name is taken verbatim.
_ELECTRICAL_ROW_RE = re.compile(
    r"^(?P<pin>\S.*?)\s+(?P<limit>-?[\d.]+(?:e[-+]?\d+)?)\s+"
    r"(?P<value>-?[\d.]+(?:e[-+]?\d+)?)\s+(?P<slack>-?[\d.]+(?:e[-+]?\d+)?)"
    r"\s+\(VIOLATED\)\s*$",
    re.IGNORECASE,
)
#: One violator row of ``report_check_types -min_pulse_width`` / ``-min_period``
#: (issue #5241): ``<pin> [(low|high)] <required> <actual> <slack> (VIOLATED)``
#: (captured on OpenROAD 26Q3). The optional edge qualifier is dropped from the
#: pin name; the slack is the last numeric column.
_SLACK_ROW_RE = re.compile(
    r"^(?P<pin>\S+)(?:\s+\((?:low|high|rise|fall)\))?\s+"
    r"(?P<required>-?[\d.]+(?:e[-+]?\d+)?)\s+"
    r"(?P<actual>-?[\d.]+(?:e[-+]?\d+)?)\s+"
    r"(?P<slack>-?[\d.]+(?:e[-+]?\d+)?)\s+\(VIOLATED\)\s*$",
    re.IGNORECASE,
)


def _electrical_class(
    coverage: str, unit: str, rows: list[tuple[str, float, float]], nets: set[str]
) -> dict[str, Any]:
    """One limit class of the ``electrical_checks`` block. Counts, excess and
    pin are ``null`` unless ``coverage == "checked"``."""
    checked = coverage == "checked"
    worst = max(rows, key=lambda row: row[2], default=None) if checked else None
    return {
        "coverage": coverage,
        "limit_source": "liberty",
        "unit": unit,
        "violation_count": len(rows) if checked else None,
        "net_count": len(nets) if checked else None,
        "worst_excess": round(worst[2], 5) if worst is not None else None,
        "worst_pin": worst[0] if worst is not None else None,
    }


def _electrical_slack_class(
    coverage: str, rows: list[tuple[str, float]]
) -> dict[str, Any]:
    """One slack-reported limit class (``min_pulse_width`` / ``min_period``,
    issue #5241). Counts, slack and pin are ``null`` unless
    ``coverage == "checked"``."""
    checked = coverage == "checked"
    worst = min(rows, key=lambda row: row[1], default=None) if checked else None
    return {
        "coverage": coverage,
        "limit_source": "liberty",
        "unit": "ns",
        "violation_count": len(rows) if checked else None,
        "worst_slack_ns": round(worst[1], 5) if worst is not None else None,
        "worst_pin": worst[0] if worst is not None else None,
    }


def _parse_electrical_slack_check(stdout: str, begin: str, end: str) -> dict[str, Any]:
    """Parse one ``-min_pulse_width`` / ``-min_period`` ``-violators`` block
    (issue #5241); coverage semantics match :func:`_parse_electrical_check`.
    If the sentinel closed the block but a violator line could not be parsed
    into a pin and slack, the class is ``"unsupported"`` rather than a
    guessed count."""
    block = _extract_block(stdout, begin, end)
    if block is None:
        return _electrical_slack_class("not_run", [])
    if _ELECTRICAL_UNSUPPORTED in block or _ELECTRICAL_OK not in block:
        return _electrical_slack_class("unsupported", [])
    rows: list[tuple[str, float]] = []
    violated_lines = 0
    for line in block.splitlines():
        if line.startswith(_ELECTRICAL_NET_PREFIX + " "):
            continue
        if "(VIOLATED)" not in line:
            continue
        violated_lines += 1
        match = _SLACK_ROW_RE.match(line.strip())
        if match:
            rows.append((match.group("pin"), float(match.group("slack"))))
    if violated_lines != len(rows):
        return _electrical_slack_class("unsupported", [])
    return _electrical_slack_class("checked", rows)


def _parse_electrical_check(
    stdout: str, begin: str, end: str, unit: str
) -> dict[str, Any]:
    """Parse one ``report_check_types -violators`` block (issue #3005).

    ``coverage`` is ``"not_run"`` when the markers are absent (the session
    never reached the block), ``"unsupported"`` when the Tcl ``catch`` fired,
    and ``"checked"`` only when the ``KLT_ELECTRICAL_OK`` sentinel closed the
    block -- so zero violations is only ever reported for a check that ran.
    ``worst_excess`` is ``value - limit`` of the worst violator (positive)."""
    block = _extract_block(stdout, begin, end)
    if block is None:
        return _electrical_class("not_run", unit, [], set())
    if _ELECTRICAL_UNSUPPORTED in block or _ELECTRICAL_OK not in block:
        return _electrical_class("unsupported", unit, [], set())
    rows: list[tuple[str, float, float]] = []
    nets: set[str] = set()
    for line in block.splitlines():
        if line.startswith(_ELECTRICAL_NET_PREFIX + " "):
            parts = line.split(None, 2)
            if len(parts) == 3:
                nets.add(parts[2].strip())
            continue
        match = _ELECTRICAL_ROW_RE.match(line)
        if match:
            excess = float(match.group("value")) - float(match.group("limit"))
            rows.append((match.group("pin"), float(match.group("limit")), excess))
    return _electrical_class("checked", unit, rows, nets)
