"""Bind the selected schematic DUT to its extracted name in testbench copies."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from ._spice_text import strip_spice_inline_comment
from ._spice_tokens import spice_nodes, split_spice_tokens
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


_SUBCKT_LINES = re.compile(r"^[ \t]*\.(?:subckt|ends)\b[^\n]*", re.I | re.M)


@dataclass
class _Scope:
    start: int
    end: int
    name: str
    parent: int | None


def _scopes(masked: str) -> list[_Scope]:
    """Every `.subckt` body in the testbench, with its enclosing body."""
    scopes: list[_Scope] = []
    open_: list[int] = []
    for line in _SUBCKT_LINES.finditer(masked):
        tokens = split_spice_tokens(line[0])
        if tokens[0].casefold() == ".subckt":
            name = tokens[1].casefold() if len(tokens) > 1 else ""
            scopes.append(
                _Scope(line.end(), len(masked), name, open_[-1] if open_ else None)
            )
            open_.append(len(scopes) - 1)
        elif open_:
            scopes[open_.pop()].end = line.start()
    return scopes


def _private_names(scopes: list[_Scope], at: int) -> set[str]:
    """Names of `.subckt`s defined inside a body enclosing offset ``at``."""
    enclosing = {i for i, scope in enumerate(scopes) if scope.start <= at < scope.end}
    return {scope.name for scope in scopes if scope.parent in enclosing}


def retarget_calls(
    text: str, schematic: str, physical: str, pin_count: int | None
) -> str | None:
    """Replace only the selected X-call target tokens, preserving all other bytes.

    A call resolves to the included schematic unless a `.subckt` of the same name
    is defined inside an enclosing testbench subcircuit; such private calls are
    left alone, and a private definition of the physical name is refused.
    """
    chunks: list[str] = []
    end = 0
    masked = _masked_body(text)
    scopes = _scopes(masked)
    public = {scope.name for scope in scopes if scope.parent is None}
    for match in _X_CARDS.finditer(masked):
        card = _CONTINUATION.sub(lambda prefix: " " * len(prefix[0]), match[0])
        tokens = split_spice_tokens(card)
        try:
            nodes, target, _params = _x_call(tokens)
        except SpiceFlattenError:
            continue
        if target.casefold() != schematic.casefold():
            continue
        private = _private_names(scopes, match.start())
        if schematic.casefold() in private or schematic.casefold() in public:
            continue
        if physical.casefold() in private:
            raise ValueError(
                f"instance '{tokens[0]}' would call a private .subckt "
                f"'{physical}' instead of the extracted top"
            )
        if physical.casefold() in public:
            raise ValueError(
                f"instance '{tokens[0]}' would call the testbench .subckt "
                f"'{physical}' instead of the extracted top"
            )
        actual_pins = len(spice_nodes(nodes))
        if pin_count is not None and actual_pins != pin_count:
            raise ValueError(
                f"instance '{tokens[0]}' has {actual_pins} nodes for {pin_count} pins"
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
    roots = _subckt_interfaces(read_text(schematic_path) or "", file_scope_only=True)
    if schematic.lower() not in roots:
        raise ValueError(f"no .subckt '{schematic}' in {schematic_path}")
    pin_count: int | None = len(roots[schematic.lower()][1])
    if extracted_path is not None:
        extracted = _subckt_interfaces(
            read_text(extracted_path) or "", file_scope_only=True
        )
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
