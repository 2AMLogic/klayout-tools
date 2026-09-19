"""Observe physical cells and PDN structure in OpenROAD's produced DEF.

This is a structural inventory, not connectivity, DRC, or power-integrity
signoff. Checks follow the detector criteria reported in issue #2086 without
copying its platform-name heuristics: cell roles come from LEF declarations
and this repository's verified platform mappings. No platform recipe is added.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from .lef_header import _BLOCK_RE

_PLACED = re.compile(r"\+\s+(?:PLACED|FIXED|COVER)\s+\(")
_POINT = r"\(\s*[-\d*]+\s+[-\d*]+(?:\s+\d+)?\s*\)"
_LEF_ROLES = {
    "WELLTAP": "tap",
    "ENDCAP": "endcap",
    "SPACER": "filler",
    "TIEHIGH": "tie",
    "TIELOW": "tie",
}


def _library_context(cell_library: str, cell_lef: str, tech_lef: str) -> dict:
    # Runtime imports avoid a cycle with the P&R orchestrator; the existing
    # verified tables stay the sole source for platform-specific master names.
    from .place_and_route import _FILLER_CELLS, _ROW_RAIL_STRAP, _TAPCELL_CELLS
    from .synthesize import _TIE_CELLS

    roles = _lef_roles(cell_lef)
    tap, endcap, _ = _TAPCELL_CELLS.get(cell_library, (None, None, None))
    known = {
        "filler": _FILLER_CELLS.get(cell_library, ()),
        "tap": (tap,),
        "endcap": (endcap,),
        "tie": tuple(master for master, _ in _TIE_CELLS.get(cell_library, ())),
    }
    for role, masters in known.items():
        roles.update({master: role for master in masters if master is not None})
    layers = _routing_layers(tech_lef)
    rail = _ROW_RAIL_STRAP.get(cell_library, (None,))[0]
    return {
        "roles": roles,
        "tie_known": cell_library in _TIE_CELLS or "tie" in roles.values(),
        "tap_required": tap is not None,
        "endcap_required": endcap is not None,
        "rail_layer": rail or next(iter(layers), None),
        "layers": layers,
    }


def _lef_roles(path: str) -> dict[str, str]:
    roles = {}
    try:
        text = Path(path).read_text()
    except (OSError, UnicodeDecodeError):
        return roles
    for kind, name, body in _BLOCK_RE.findall(text):
        if kind != "MACRO":
            continue
        match = re.search(r"\bCLASS\s+([^;]+);", body, re.IGNORECASE)
        classes = match[1].upper().split() if match else []
        for class_name in classes:
            if class_name in _LEF_ROLES:
                roles[name] = _LEF_ROLES[class_name]
    return roles


def _routing_layers(path: str) -> list[str]:
    try:
        text = Path(path).read_text()
    except (OSError, UnicodeDecodeError):
        return []
    return [
        name
        for kind, name, body in _BLOCK_RE.findall(text)
        if kind == "LAYER" and re.search(r"\bTYPE\s+ROUTING\s*;", body, re.IGNORECASE)
    ]


def _records(text: str, section: str, *, optional: bool = False) -> list[str]:
    begin = re.search(rf"(?m)^\s*{section}\s+(\d+)\s*;", text)
    if begin is None and optional and not re.search(rf"(?m)^\s*{section}\b", text):
        return []
    if begin is None:
        raise ValueError(f"missing {section} section")
    end = re.search(rf"\bEND\s+{section}\b", text[begin.end() :])
    if end is None:
        raise ValueError(f"unterminated {section} section")
    body = text[begin.end() : begin.end() + end.start()]
    records = re.findall(r"(?ms)^\s*-\s+(.*?)\s*;", body)
    if len(records) != int(begin[1]):
        raise ValueError(f"{section} record count disagrees with its header")
    return records


def _cell_counts(records: list[str], context: dict) -> dict[str, int | None]:
    counts = Counter(
        {
            "instances": len(records),
            "placed_instances": 0,
            "tap": 0,
            "endcap": 0,
            "filler": 0,
            "tie": 0,
        }
    )
    for record in records:
        words = record.split()
        if len(words) < 2:
            raise ValueError("incomplete component record")
        if not _PLACED.search(record):
            continue
        counts["placed_instances"] += 1
        role = context["roles"].get(words[1])
        if role:
            counts[role] += 1
    result = dict(counts)
    if not context["tie_known"]:
        result["tie"] = None
    return result


def _via_count(path: str) -> int:
    candidates = re.findall(_POINT + r"\s+([^\s()+;]+)", path)
    reserved = {"NEW", "ROUTED", "FIXED", "COVER", "MASK", "STYLE", "SHAPE", "RECT"}
    count = sum(token.upper() not in reserved for token in candidates)
    repeat = re.search(r"\bDO\s+(\d+)\s+BY\s+(\d+)\b", path)
    return count * int(repeat[1]) * int(repeat[2]) if repeat else count


def _net_counts(body: str, context: dict) -> dict:
    rail = context["rail_layer"]
    layers = context["layers"]
    if rail not in layers:
        raise ValueError("routing-layer order unavailable from technology LEF")
    upper_layers = layers[layers.index(rail) + 1 :]
    counts = {
        "present": True,
        "followpin_segments": 0,
        "upper_stripe_segments": 0,
        "via_count": 0,
        "segments_by_layer": {},
    }
    for path in re.split(r"\b(?:ROUTED|FIXED|COVER|NEW)\s+", body)[1:]:
        header = re.match(r"(\S+)\s+\d+\b", path)
        points = re.findall(_POINT, path)
        if header is None or not points:
            raise ValueError("unsupported or incomplete SPECIALNET routing geometry")
        layer = header[1]
        if layer not in layers:
            raise ValueError(f"routing layer {layer!r} is absent from technology LEF")
        segments = max(0, len(points) - 1)
        shape = re.search(r"\+\s*SHAPE\s+(\w+)", path)
        shape_name = shape[1] if shape else "WIRE"
        counts["segments_by_layer"].setdefault(layer, Counter())[shape_name] += segments
        counts["followpin_segments"] += segments * (
            shape_name == "FOLLOWPIN" and layer == rail
        )
        counts["upper_stripe_segments"] += segments * (
            shape_name == "STRIPE" and layer in upper_layers
        )
        counts["via_count"] += _via_count(path)
    return counts


def _special_nets(records: list[str], names: list[str], context: dict) -> dict:
    by_name = {}
    for record in records:
        parts = record.split(maxsplit=1)
        by_name[parts[0]] = parts[1] if len(parts) > 1 else ""
    result = {}
    for name in names:
        if name in by_name:
            result[name] = _net_counts(by_name[name], context)
        else:
            result[name] = {
                "present": False,
                "followpin_segments": 0,
                "upper_stripe_segments": 0,
                "via_count": 0,
                "segments_by_layer": {},
            }
    return result


def _expected_nets(power: dict | None, cell_library: str) -> list[str]:
    from .place_and_route import _POWER_PIN_PATTERNS, _ROW_RAIL_STRAP

    if power is not None:
        return [power["power_net"], power["ground_net"]]
    if cell_library in _ROW_RAIL_STRAP:
        return list(_ROW_RAIL_STRAP[cell_library][-2:])
    return [
        pattern.strip("^$")
        for _, pattern, primary in _POWER_PIN_PATTERNS[cell_library]
        if primary
    ]


def _structure_issues(counts: dict, nets: dict, context: dict, stage: str) -> list[str]:
    issues = []
    required = {
        "tap": context["tap_required"],
        "endcap": context["endcap_required"],
        "filler": stage == "route",
    }
    issues.extend(
        f"no placed {role} cells"
        for role, needed in required.items()
        if needed and counts[role] == 0
    )
    for name, net in nets.items():
        if not net["present"]:
            issues.append(f"no SPECIALNET {name}")
            continue
        for key, description in (
            ("followpin_segments", "FOLLOWPIN rails"),
            ("upper_stripe_segments", "STRIPE segments above the rail layer"),
            ("via_count", "PDN vias"),
        ):
            if net[key] == 0:
                issues.append(f"{name}: no {description}")
    return issues


def observe_power(
    *,
    def_path: str | None,
    unrouted_def_path: str | None,
    stage: str,
    power: dict | None,
    cell_library: str,
    cell_lef: str,
    tech_lef: str,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Return actual physical evidence plus warnings, preserving unknowns."""
    path = def_path or unrouted_def_path
    observation: dict[str, Any] = {
        "status": "unavailable",
        "source_def": path,
        "counts": None,
        "special_nets": None,
        "issues": [],
    }
    warnings = []
    if power is None:
        warnings.append(
            {
                "code": "power_not_requested",
                "message": (
                    "request.power was omitted: configured tap/endcap insertion "
                    "and the full PDN did not run. A row-rail fallback, when "
                    "available, is not a complete power grid."
                ),
            }
        )
    try:
        if path is None:
            raise ValueError("this stage produces no DEF for physical inspection")
        text = Path(path).read_text()
        if not re.search(r"\bEND\s+DESIGN\b", text):
            raise ValueError("missing END DESIGN")
        context = _library_context(cell_library, cell_lef, tech_lef)
        counts = _cell_counts(_records(text, "COMPONENTS"), context)
        observation["counts"] = counts
        nets = _special_nets(
            _records(text, "SPECIALNETS", optional=True),
            _expected_nets(power, cell_library),
            context,
        )
        observation["special_nets"] = nets
        issues = _structure_issues(counts, nets, context, stage)
        observation["issues"] = issues
        observation["status"] = (
            "incomplete"
            if issues
            else ("structure_present" if stage == "route" else "not_routed")
        )
        if issues:
            warnings.append(
                {"code": "power_delivery_incomplete", "message": "; ".join(issues)}
            )
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        observation["issues"] = [str(exc)]
        warnings.append({"code": "power_evidence_unavailable", "message": str(exc)})
    return observation, warnings
