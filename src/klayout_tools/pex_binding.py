"""Bind the selected schematic DUT to its extracted name in testbench copies."""

from __future__ import annotations

import re
from collections.abc import Sequence

from ._spice_text import strip_spice_inline_comment
from ._spice_tokens import split_spice_tokens
from ._text import read_text
from .pex_netlist_text import _subckt_interfaces
from .spice_flatten import SpiceFlattenError, _x_call

# Match one X card including any continuation lines. Masking comments and
# continuation prefixes preserves offsets into the original testbench.
_X_CARDS = re.compile(r"^[ \t]*x[^\n]*(?:\n(?:[ \t]*\n)*[ \t]*\+[^\n]*)*", re.I | re.M)
_CONTINUATION = re.compile(r"^[ \t]*\+", re.M)


def _masked_body(text: str) -> str:
    lines = []
    control = False
    for line in text.split("\n"):
        code = strip_spice_inline_comment(line)
        directive = code.strip().lower()
        if directive == ".control":
            control = True
        if control or directive.startswith("*"):
            code = ""
        if directive == ".endc":
            control = False
        lines.append(code.ljust(len(line)))
    return "\n".join(lines)


def retarget_calls(
    text: str, schematic: str, physical: str, pin_count: int | None
) -> str | None:
    """Replace only the selected X-call target tokens, preserving all other bytes."""
    chunks: list[str] = []
    end = 0
    for match in _X_CARDS.finditer(_masked_body(text)):
        card = _CONTINUATION.sub(lambda prefix: " " * len(prefix[0]), match[0])
        tokens = split_spice_tokens(card)
        try:
            nodes, target, _params = _x_call(tokens)
        except SpiceFlattenError:
            continue
        if target.casefold() != schematic.casefold():
            continue
        if pin_count is not None and len(nodes) != pin_count:
            raise ValueError(
                f"instance '{tokens[0]}' has {len(nodes)} nodes for {pin_count} pins"
            )
        # Walk past the positional tokens so a node named like the DUT or a
        # parameter value containing its name cannot be mistaken for the target.
        offset = 0
        for token in (tokens[0], *nodes):
            offset = card.index(token, offset) + len(token)
        start = match.start() + card.index(target, offset)
        chunks.extend((text[end:start], physical))
        end = start + len(target)
    if not chunks:
        return None
    chunks.append(text[end:])
    return "".join(chunks)


def bind(
    schematic: str,
    physical: str,
    schematic_path: str,
    body_paths: Sequence[str],
    extracted_path: str | None,
) -> list[str | None]:
    """Resolve both headers and all testbenches before dispatch."""
    roots = _subckt_interfaces(read_text(schematic_path) or "")
    if schematic.lower() not in roots:
        raise ValueError(f"no .subckt '{schematic}' in {schematic_path}")
    pin_count: int | None = len(roots[schematic.lower()][1])
    if extracted_path is not None:
        extracted = _subckt_interfaces(read_text(extracted_path) or "")
        if physical.lower() not in extracted:
            raise ValueError(f"no .subckt '{physical}' in {extracted_path}")
        # The established structured pin_count_mismatch takes precedence.
        if len(extracted[physical.lower()][1]) != pin_count:
            pin_count = None
    bodies: list[str | None] = []
    for path in body_paths:
        text = read_text(path) or ""
        rewritten = retarget_calls(text, schematic, physical, pin_count)
        if rewritten is None:
            raise ValueError(f"no call to '{schematic}' in {path}")
        bodies.append(rewritten)
    return bodies
