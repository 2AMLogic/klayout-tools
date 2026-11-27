"""Validate, constrain and measure generated clocks in standalone linked STA."""

from __future__ import annotations

import math
import re
from collections.abc import Collection
from typing import Any

from ._sta_request_diag import extract_block
from ._tcl import tcl_literal
from .post_route_sta_errors import PostRouteStaError

_BEGIN = "===KLT_STA_GENERATED_CLOCKS_BEGIN==="
_END = "===KLT_STA_GENERATED_CLOCKS_END==="
# Numeric indices are literal pin-name characters. Other bracket expressions
# would be selectors, and the request names exactly one linked pin.
_PIN = re.compile(r'(?:[^\s\x00-\x1f\x7f\\{};$"*?\[\]]|\[[0-9]+\])+')
_KEYS = {"source", "pin", "divide_by", "edges"}


def _ratio(entry: dict[str, Any], where: str) -> dict[str, Any]:
    if ("divide_by" in entry) == ("edges" in entry):
        raise PostRouteStaError(f"{where} requires exactly one of divide_by or edges")
    if "divide_by" in entry:
        n = entry["divide_by"]
        if type(n) is not int or n < 2:
            raise PostRouteStaError(f"{where}.divide_by must be an integer >= 2")
        return {"divide_by": n}
    edges = entry["edges"]
    if not (
        isinstance(edges, list)
        and len(edges) == 3
        and all(type(n) is int for n in edges)
        and 0 < edges[0] < edges[1] < edges[2]
    ):
        raise PostRouteStaError(
            f"{where}.edges requires three increasing positive integers"
        )
    return {"edges": list(edges)}


def _entry(value: Any, index: int, declared: Collection[object]) -> dict[str, Any]:
    where = f"request.constraints.generated_clocks[{index}]"
    if not isinstance(value, dict):
        raise PostRouteStaError(f"{where} must be an object")
    unknown = value.keys() - _KEYS
    if unknown:
        raise PostRouteStaError(
            f"{where} has unknown keys: {', '.join(sorted(unknown))}"
        )
    source = value.get("source")
    if not isinstance(source, str) or source not in declared:
        raise PostRouteStaError(f"{where}.source must name a declared clock port")
    pin = value.get("pin")
    if not isinstance(pin, str) or pin.startswith("-") or not _PIN.fullmatch(pin):
        raise PostRouteStaError(f"{where}.pin must name a single literal linked pin")
    name = f"klt_gen_clk_{index}"
    if name in declared:
        raise PostRouteStaError(f"{where}: generated name '{name}' is a declared clock")
    return {
        "index": index,
        "name": name,
        "source": source,
        "pin": pin,
        **_ratio(value, where),
    }


def validate(
    constraints: dict[str, Any], declared: Collection[object]
) -> list[dict[str, Any]]:
    raw = constraints.get("generated_clocks", [])
    if not isinstance(raw, list):
        raise PostRouteStaError("request.constraints.generated_clocks must be an array")
    rows = []
    seen = set()
    for index, value in enumerate(raw):
        row = _entry(value, index, declared)
        if row["pin"] in seen:
            raise PostRouteStaError(
                f"request.constraints.generated_clocks[{index}].pin "
                f"'{row['pin']}' is duplicated"
            )
        seen.add(row["pin"])
        rows.append(row)
    return rows


def constraint_lines(
    rows: list[dict[str, Any]], begin: str, end: str, exit_code: int
) -> list[str]:
    """Resolve every target and source before defining the first generated clock."""
    if not rows:
        return []
    lines = []
    for row in rows:
        index = row["index"]
        for field, kind in (("source", "ports"), ("pin", "pins")):
            word = tcl_literal(row[field])
            var = f"klt_gen_{field}_{index}"
            lines.extend(
                [
                    f"set {var} [get_{kind} -quiet [list {word}]]",
                    f"if {{[llength ${var}] != 1}} {{",
                    f'    puts "{begin}"',
                    f"    set matches [llength ${var}]",
                    f"    puts [join [list generated_clock {index}.{field} "
                    f'[list {word} matches $matches linked {kind}]] "\\t"]',
                    f'    puts "{end}"',
                    f"    exit {exit_code}",
                    "}",
                ]
            )
    for row in rows:
        index = row["index"]
        ratio = (
            f"-divide_by {row['divide_by']}"
            if "divide_by" in row
            else "-edges {" + " ".join(map(str, row["edges"])) + "}"
        )
        lines.append(
            f"create_generated_clock -name {row['name']} "
            f"-source $klt_gen_source_{index} {ratio} $klt_gen_pin_{index}"
        )
    return lines


def evidence_lines(extras: dict[str, Any] | None) -> list[str]:
    rows = (extras or {}).get("generated_clocks", [])
    if not rows:
        return []
    names = " ".join(row["name"] for row in rows)
    return [
        f'puts "{_BEGIN}"',
        f"foreach name {{{names}}} {{",
        "    set clock [get_clocks -quiet $name]",
        '    set period ""; set count ""',
        "    if {[llength $clock] == 1} {",
        "        if {[catch {get_property $clock period} period]} {set period {}}",
        "        if {[catch {llength [all_registers -clock $clock]} count]} {",
        "            set count {}",
        "        }",
        "    }",
        '    puts [join [list $name $period $count] "\\t"]',
        "}",
        f'puts "{_END}"',
    ]


def _measurement(columns: list[str]) -> dict[str, Any]:
    try:
        period = float(columns[0])
        measured = period if math.isfinite(period) and period > 0 else None
    except ValueError:
        measured = None
    count = int(columns[1]) if re.fullmatch(r"[0-9]+", columns[1]) else None
    result: dict[str, Any] = {"period_ns": measured, "clocked_registers": count}
    missing = [field for field, value in result.items() if value is None]
    if missing:
        result["reason"] = "OpenSTA did not report " + " and ".join(missing)
    return result


def response(extras: dict[str, Any] | None, stdout: str) -> dict[str, Any]:
    rows = (extras or {}).get("generated_clocks", [])
    if not rows:
        return {}
    evidence = {}
    for line in (extract_block(stdout, _BEGIN, _END) or "").splitlines():
        columns = line.split("\t")
        if len(columns) == 3:
            evidence[columns[0]] = columns[1:]
    return {
        "generated_clocks": [
            {**row, **_measurement(evidence.get(row["name"], ["", ""]))} for row in rows
        ]
    }


def link_error(rows: list[tuple[str, str, str]]) -> str | None:
    side, key, message = rows[0]
    if side != "generated_clock":
        return None
    index, field = key.split(".", 1)
    return f"constraints.generated_clocks[{index}].{field}: {message}"
