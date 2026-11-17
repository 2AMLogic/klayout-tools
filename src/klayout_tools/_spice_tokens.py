"""Splitting one SPICE card into tokens -- the rule every netlist-reading
``klt`` verb shares (issue #5461).

A card is whitespace-separated, *except* that an expression may contain
spaces: ``ad='int((nf+1)/2) * W/nf * 0.18u'``, ``w={2 * wn}``, ``w=(2 *
1u)``. And ngspice accepts four spellings of one parameter assignment --
``w=1``, ``w =1``, ``w= 1``, ``w = 1`` -- which every caller here wants
collapsed to the first, because they all go on to ask "is this token a
``name=value`` parameter?".

:func:`split_spice_tokens` answers both at once. It began as three
hand-rolled copies (``spice_flatten``, ``pex``, ``sim_mismatch_gate``); the
two lesser ones understood neither parentheses nor the spaced spellings, so
"the model name is the last token without ``=``" read ``0.15`` as the model
of ``XM1 d g s b sky130_fd_pr__nfet_01v8 w = 1 l = 0.15``. That is the same
failure family as issues #2866 (a quoted expression containing spaces) and
#4383 (parameter expressions), each previously fixed inside one copy rather
than by converging on one tokenizer -- which is why a third variant of the
bug stayed reachable until this module existed.

Companion modules: ``_spice_text`` (line-level pre-processing -- inline
comments, ``+`` continuation folding) runs *before* this one;
``_spice_number`` parses an individual value token *after* it.
"""

from __future__ import annotations


def _lex(line: str) -> list[str]:
    """Whitespace split keeping quoted/braced/parenthesised expressions whole."""
    tokens: list[str] = []
    current: list[str] = []
    quote = ""
    depth = 0
    for ch in line:
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch in "{(":
            depth += 1
        elif ch in "})":
            depth = max(depth - 1, 0)
        elif ch.isspace() and depth == 0:
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(ch)
    if current:
        tokens.append("".join(current))
    return tokens


def split_spice_tokens(line: str) -> list[str]:
    """:func:`_lex`, then re-join ``name = value`` / ``name= value`` /
    ``name =value`` spellings into one ``name=value`` token."""
    tokens = _lex(line)
    merged: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        has_next = index + 1 < len(tokens)
        if token == "=" and merged and has_next:
            merged[-1] = f"{merged[-1]}={tokens[index + 1]}"
            index += 2
        elif token.endswith("=") and token != "=" and has_next:
            merged.append(token + tokens[index + 1])
            index += 2
        elif token.startswith("=") and token != "=" and merged:
            merged[-1] = merged[-1] + token
            index += 1
        else:
            merged.append(token)
            index += 1
    return merged
