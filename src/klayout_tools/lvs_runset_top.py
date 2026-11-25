"""Top-circuit preparation and renaming for ``klayout-runset`` SPICE
references (issues #5402, #7918).

Promotes a complete top-level xschem ``**.subckt`` / ``*+`` / ``**.ends``
wrapper when the request selects it (omitted ``reference.top``, explicit
wrapper name, or ``.TOP``), or wraps top-level instance and element cards of a
headerless implicit ``.TOP`` into ``.subckt <layout_top> ... .ends`` while
keeping child definitions and deck-level directives outside.
"""

from __future__ import annotations

import os
import re

from .netlist_block import space_separate_subckt_ports, uncomment_xschem_wrapper
from .netlist_block_wrapper import DeckScope
from .netlist_normalize_scan import defined_subckt_names, quote_spice_includes

_SUBCKT_RE = re.compile(r"^(\s*\.(?:subckt|ends)\s+)(\S+)", re.I)
_QUOTED_INCLUDE_RE = re.compile(
    r"""^(\s*\.(?:include|inc)\s+)(['"])([^'"]+)\2(\s*)$""", re.IGNORECASE
)


def rename_top_subckt(text: str, old: str, new: str) -> str:
    """Rename the ``.subckt old`` header (and its ``.ends old``) to ``new``;
    only the exact, case-insensitive top name matches (issue #5402)."""
    out = []
    for line in text.split("\n"):
        m = _SUBCKT_RE.match(line)
        if m and m.group(2).lower() == old.lower():
            line = m.group(1) + new + line[m.end() :]
        out.append(line)
    return "\n".join(out)


def _space_promoted_header_ports(before: str, after: str) -> str:
    old_lines = before.split("\n")
    new_lines = after.split("\n")
    changed = [
        i for i, (a, b) in enumerate(zip(old_lines, new_lines, strict=True)) if a != b
    ]
    start = changed[0]
    end = start + 1
    while end < len(new_lines) and new_lines[end].lstrip().startswith("+"):
        end += 1
    spaced = (
        space_separate_subckt_ports("\n".join(new_lines[start:end]).encode("utf-8"))
        .decode("utf-8")
        .split("\n")
    )
    new_lines[start:end] = spaced
    return "\n".join(new_lines)


def _selector_matches_wrapper(
    ref_top: str | None,
    wrapper_name: str,
    layout_top: str | None,
    defined: set[str],
) -> bool:
    if ref_top is None:
        return not (layout_top and layout_top.upper() in defined)
    return ref_top.lower() == wrapper_name.lower() or ref_top.upper() == ".TOP"


def _wrap_implicit_top(text: str, layout_top: str) -> tuple[str, bool]:
    scope = DeckScope()
    deck_before_end: list[str] = []
    top_cards: list[str] = []
    deck_from_end: list[str] = []
    for line in text.split("\n"):
        kind = scope.step(line)
        if kind in ("top_card", "top_continuation"):
            top_cards.append(line)
        elif kind in ("deck_end", "after_end"):
            deck_from_end.append(line)
        else:
            deck_before_end.append(line)
    if not top_cards:
        return text, False
    while deck_before_end and deck_before_end[-1] == "":
        deck_before_end.pop()
    tail = deck_from_end or ([""] if text.endswith("\n") else [])
    assembled = [
        *deck_before_end,
        f".subckt {layout_top}",
        *top_cards,
        f".ends {layout_top}",
        *tail,
    ]
    return "\n".join(assembled), True


def prepare_reference_top(
    text: str, *, layout_top: str | None, ref_top: str | None
) -> tuple[str, str | None, str | None, bool]:
    """Prepare the selected top circuit before ``klayout-runset`` conversion.

    Returns ``(prepared_text, effective_ref_top, source_ref_top, changed)``.
    """
    defined = defined_subckt_names(text)
    promoted, wrapper_name, _, _ = uncomment_xschem_wrapper(text, require_complete=True)
    if (
        wrapper_name is not None
        and wrapper_name.upper() not in defined
        and _selector_matches_wrapper(ref_top, wrapper_name, layout_top, defined)
    ):
        promoted = _space_promoted_header_ports(text, promoted)
        source_top = wrapper_name.upper() if ref_top is None else ref_top
        return promoted, wrapper_name, source_top, True

    if (
        ref_top is not None
        and ref_top.upper() == ".TOP"
        and layout_top
        and layout_top.upper() != ".TOP"
        and layout_top.upper() not in defined
    ):
        wrapped, changed = _wrap_implicit_top(text, layout_top)
        if changed:
            return wrapped, layout_top, ref_top, True

    return text, ref_top, ref_top, False


def resolve_relative_includes(text: str, base_dir: str) -> str:
    """Resolve both unquoted and quoted relative ``.include``/``.inc`` paths
    against ``base_dir`` so a converted work-dir copy still finds them."""
    text = quote_spice_includes(text, base_dir)
    changed = False
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        ending = line[len(stripped) :]
        match = _QUOTED_INCLUDE_RE.match(stripped)
        if match is not None:
            target = match.group(3)
            if target and not os.path.isabs(target) and target[0] not in ("$", "~"):
                resolved = os.path.normpath(os.path.join(base_dir, target))
                quote = match.group(2)
                if quote not in resolved:
                    line = f"{match.group(1)}{quote}{resolved}{quote}{ending}"
                    changed = True
        out.append(line)
    return "".join(out) if changed else text
