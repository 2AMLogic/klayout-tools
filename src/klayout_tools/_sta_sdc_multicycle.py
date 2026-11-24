"""Parse the bounded multicycle SDC syntax and compare its setup/hold pairs."""

from __future__ import annotations

import re
from collections import defaultdict
from itertools import zip_longest
from typing import Any

from ._sta_multicycle import SELECTOR_CHARS

_COMMAND = "set_multicycle_path"
# Keep the final getter bracket separate from literal brackets in a bus name.
_WORD = re.compile(r"\[\s*(?:get_cells|get_pins)\s+([^\s]+?)\s*\](?=\s|$)|(\S+)")
_Key = tuple[str, str, str | None, str]


def _selector(token: re.Match[str] | None) -> str | None:
    if token is None:
        return None
    getter_value, bare = token.groups()
    if bare and re.fullmatch(r"\[[A-Za-z_]\w*\]", bare):
        return None  # A standalone Tcl command such as [all_registers].
    value = getter_value or bare
    return value if SELECTOR_CHARS.issuperset(value) else None


def parse_multicycle(line: str) -> dict[str, Any] | None:
    """Recognize one explicit side and one bounded selector per named point."""
    tokens = iter(_WORD.finditer(line))
    next(tokens)  # command name
    side = None
    multiplier = None
    selection: dict[str, str] = {}
    for token in tokens:
        word = token.group(2)
        if word in ("-setup", "-hold") and side is None:
            side = word[1:]
        elif word in ("-from", "-through", "-to") and word[1:] not in selection:
            selector = _selector(next(tokens, None))
            if selector is None:
                return None
            selection[word[1:]] = selector
        elif word and multiplier is None and re.fullmatch(r"[0-9]+", word):
            try:
                multiplier = int(word)
            except ValueError:  # Exceeds the interpreter's integer-string limit.
                return None
        else:
            return None
    if not multiplier or side is None or not {"from", "to"} <= selection.keys():
        return None
    return {"cmd": _COMMAND, "bound": side, "value": multiplier, **selection}


def compare_multicycle(
    commands: list[dict[str, Any]], entries: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Cancel equal entries first so repeating or reordering a selection is stable."""
    requested: dict[_Key, list[int]] = defaultdict(list)
    for entry in entries or []:
        for side in ("setup", "hold"):
            key = (side, entry["from"], entry.get("through"), entry["to"])
            requested[key].append(entry[side])
    unmatched: dict[_Key, list[dict[str, Any]]] = defaultdict(list)
    for cmd in commands:
        if cmd["cmd"] != _COMMAND:
            continue
        key = (cmd["bound"], cmd["from"], cmd.get("through"), cmd["to"])
        if cmd["value"] in requested[key]:
            requested[key].remove(cmd["value"])
        else:
            unmatched[key].append(cmd)

    rows = []
    for key, values in requested.items():
        side, source, through, target = key
        subject = f"{source} -> {target}"
        if through is not None:
            subject += f" through {through}"
        for cmd, value in zip_longest(unmatched[key], values):
            rows.append(
                {
                    "command": _COMMAND,
                    "bound": side,
                    "subject": subject,
                    "from": source,
                    "through": through,
                    "to": target,
                    "request_value": value,
                    "sdc_value": cmd["value"] if cmd else None,
                    **({"line": cmd["line"]} if cmd else {}),
                }
            )
    return rows
