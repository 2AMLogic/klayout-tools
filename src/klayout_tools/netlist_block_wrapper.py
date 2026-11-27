"""Shared xschem top-sheet ``**.subckt`` / ``*+`` / ``**.ends`` wrapper
handling for ``klt netlist --block`` and ``klt lvs`` (issues #2680, #3899,
#7918).

Split out of :mod:`klayout_tools.netlist_block` to stay under the per-file
line ceiling while sharing top-level wrapper promotion with ``klayout-runset``
reference preparation without stripping trailing ``.end`` cards.
"""

from __future__ import annotations

import re

from ._spice_text import strip_spice_inline_comment

#: xschem comments out the top sheet's own ``.subckt``/``.ends`` pair when it
#: netlists it as a testbench (``**.subckt ...`` / ``**.ends``).
_BLOCK_SUBCKT_COMMENT_RE = re.compile(
    r"^([ \t]*)\*\*([ \t]*\.subckt\b.*)$", re.IGNORECASE
)
#: xschem wraps a long top-sheet header onto commented continuation lines
#: (``*+ ...``); once the header is uncommented they must become real SPICE
#: ``+`` continuations or the port list is silently truncated (issue #3899).
_BLOCK_CONTINUATION_COMMENT_RE = re.compile(r"^([ \t]*)\*{1,2}\+(.*)$")
_BLOCK_ENDS_COMMENT_RE = re.compile(r"^([ \t]*)\*\*([ \t]*\.ends\b.*)$", re.IGNORECASE)
_LIB_ARG_RE = re.compile(r'"[^"]*"|\'[^\']*\'|\S+')


class DeckScope:
    """Lexical scope tracker for top-level SPICE cards vs. child definitions."""

    def __init__(self) -> None:
        self.subckt_depth = 0
        self.in_control = False
        self.in_lib_section = False
        self.after_end = False
        self.in_top_card = False

    @property
    def at_top(self) -> bool:
        return (
            self.subckt_depth == 0 and not self.in_control and not self.in_lib_section
        )

    def step(self, line: str) -> str:
        if self.after_end:
            return "after_end"
        stripped = line.lstrip()
        if not stripped or stripped.startswith("*"):
            self.in_top_card = False
            return "top_comment" if self.at_top else "scoped"
        active = strip_spice_inline_comment(stripped)
        if not active:
            self.in_top_card = False
            return "top_comment" if self.at_top else "scoped"
        if active.startswith("+"):
            return "top_continuation" if self.in_top_card else "scoped"
        self.in_top_card = False
        return self._advance_keyword(active)

    def _advance_keyword(self, active: str) -> str:
        head = active.split(None, 1)
        kw = head[0].lower()
        rest = head[1] if len(head) > 1 else ""
        if self.in_control:
            if kw == ".endc":
                self.in_control = False
            return "scoped"
        if self.at_top and kw == ".control":
            self.in_control = True
            return "scoped"
        if self.subckt_depth == 0 and self._step_lib(kw, rest):
            return "scoped"
        if self.in_lib_section:
            return "scoped"
        if kw in (".subckt", ".ends"):
            delta = 1 if kw == ".subckt" else -1
            self.subckt_depth = max(0, self.subckt_depth + delta)
            return "subckt"
        if self.subckt_depth > 0:
            return "scoped"
        if kw == ".end":
            self.after_end = True
            return "deck_end"
        if active.startswith("."):
            return "top_directive"
        self.in_top_card = True
        return "top_card"

    def _step_lib(self, kw: str, rest: str) -> bool:
        if kw == ".endl":
            self.in_lib_section = False
            return True
        if kw == ".lib" and len(_LIB_ARG_RE.findall(rest)) == 1:
            self.in_lib_section = True
            return True
        return False


def _rejoin_continuations_at(lines: list[str], header_idx: int, stop_idx: int) -> int:
    j = header_idx + 1
    while j < stop_idx:
        match = _BLOCK_CONTINUATION_COMMENT_RE.match(lines[j])
        if match is None:
            break
        lines[j] = f"{match.group(1)}+{match.group(2)}"
        j += 1
    return j


def _wrapper_subckt(line: str) -> tuple[re.Match[str], str | None] | None:
    match = _BLOCK_SUBCKT_COMMENT_RE.match(line)
    if match is None:
        return None
    tokens = strip_spice_inline_comment(match.group(2)).split()
    return match, (tokens[1] if len(tokens) >= 2 else None)


def _matching_wrapper_ends(line: str, name: str) -> re.Match[str] | None:
    match = _BLOCK_ENDS_COMMENT_RE.match(line)
    if match is None:
        return None
    tokens = strip_spice_inline_comment(match.group(2)).split()
    if len(tokens) == 1 or (len(tokens) == 2 and tokens[1].lower() == name.lower()):
        return match
    return None


def _find_complete_top_wrapper(
    lines: list[str],
) -> tuple[int, re.Match[str], str, int, re.Match[str]] | None:
    scope = DeckScope()
    subckt_hit: tuple[int, re.Match[str], str] | None = None
    for i, line in enumerate(lines):
        kind = scope.step(line)
        if kind == "subckt" and subckt_hit is not None:
            return None
        if kind != "top_comment":
            continue
        sub = _wrapper_subckt(line)
        if sub is not None:
            if subckt_hit is not None or sub[1] is None:
                return None
            subckt_hit = (i, sub[0], sub[1])
        elif _BLOCK_ENDS_COMMENT_RE.match(line):
            if subckt_hit is None:
                return None
            ends = _matching_wrapper_ends(line, subckt_hit[2])
            return (*subckt_hit, i, ends) if ends is not None else None
    return None


def _uncomment_first_wrapper(lines: list[str]) -> tuple[str | None, bool, bool]:
    wrapper_name: str | None = None
    subckt_uncommented = False
    for i, line in enumerate(lines):
        sub = _wrapper_subckt(line)
        if sub is not None:
            lines[i] = f"{sub[0].group(1)}{sub[0].group(2)}"
            _rejoin_continuations_at(lines, i, len(lines))
            wrapper_name = sub[1]
            subckt_uncommented = True
            break
    ends_uncommented = False
    for i, line in enumerate(lines):
        ends_match = _BLOCK_ENDS_COMMENT_RE.match(line)
        if ends_match is not None:
            lines[i] = f"{ends_match.group(1)}{ends_match.group(2)}"
            ends_uncommented = True
            break
    return wrapper_name, subckt_uncommented, ends_uncommented


def uncomment_xschem_wrapper(
    text: str, *, require_complete: bool = False
) -> tuple[str, str | None, bool, bool]:
    """Uncomment xschem's ``**.subckt`` / ``*+`` / ``**.ends`` top wrapper.

    Returns ``(text, wrapper_name, subckt_uncommented, ends_uncommented)``.
    With ``require_complete=True``, promotion runs only when a top-level
    ``**.subckt <name>`` and its matching top-level ``**.ends`` are both
    present without intervening active ``.subckt`` definitions.
    """
    lines = text.split("\n")
    if require_complete:
        found = _find_complete_top_wrapper(lines)
        if found is None:
            return text, None, False, False
        sub_i, sub_m, name, ends_i, ends_m = found
        lines[sub_i] = f"{sub_m.group(1)}{sub_m.group(2)}"
        _rejoin_continuations_at(lines, sub_i, ends_i)
        lines[ends_i] = f"{ends_m.group(1)}{ends_m.group(2)}"
        return "\n".join(lines), name, True, True

    wrapper_name, sub_ok, ends_ok = _uncomment_first_wrapper(lines)
    if not sub_ok and not ends_ok:
        return text, None, False, False
    return "\n".join(lines), wrapper_name, sub_ok, ends_ok
