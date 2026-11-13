"""``klt netlist --block`` post-processing (issues #2680, #3899, #4490, #5439).

Split out of :mod:`klayout_tools.netlist` to keep that file under its
size-ratchet ceiling. All but one of these are pure bytes-in/bytes-out
transforms; :func:`apply_block_mode` additionally reads the cell's symbol
attributes off disk to restore its subcircuit parameters (issue #5439).
"""

from __future__ import annotations

import os
import re
from typing import Any

#: xschem comments out the top sheet's own ``.subckt``/``.ends`` pair when it
#: netlists it as a testbench (``**.subckt ...`` / ``**.ends``). Matches the
#: first such line so it can be uncommented; the capture group is everything
#: after the ``**`` (preserving the directive's own arguments verbatim).
_BLOCK_SUBCKT_COMMENT_RE = re.compile(
    r"^([ \t]*)\*\*([ \t]*\.subckt\b.*)$", re.IGNORECASE | re.MULTILINE
)
#: xschem wraps a long top-sheet header onto commented continuation lines
#: (``*+ ...``); once the header is uncommented they must become real SPICE
#: ``+`` continuations or the port list is silently truncated (issue #3899).
_BLOCK_CONTINUATION_COMMENT_RE = re.compile(r"^([ \t]*)\*{1,2}\+(.*)$")
_BLOCK_ENDS_COMMENT_RE = re.compile(
    r"^([ \t]*)\*\*([ \t]*\.ends\b.*)$", re.IGNORECASE | re.MULTILINE
)

#: xschem always appends a trailing ``.end`` -- correct for a complete deck,
#: wrong for an includable block (a second ``.end`` in the including
#: testbench's own deck would terminate it early). Matches only when ``.end``
#: is the last line of the file.
_TRAILING_END_RE = re.compile(r"\n[ \t]*\.end[ \t]*\n?\Z", re.IGNORECASE)


def _rejoin_header_continuations(text: str) -> str:
    """Rewrite the commented ``*+``/``**+`` lines contiguous with the
    just-uncommented ``.subckt`` header into real ``+`` continuations."""
    lines = text.split("\n")
    header = next(
        (
            i
            for i, line in enumerate(lines)
            if re.match(r"[ \t]*\.subckt\b", line, re.IGNORECASE)
        ),
        None,
    )
    if header is None:
        return text
    j = header + 1
    while j < len(lines):
        match = _BLOCK_CONTINUATION_COMMENT_RE.match(lines[j])
        if not match:
            break
        lines[j] = f"{match.group(1)}+{match.group(2)}"
        j += 1
    return "\n".join(lines)


_SUBCKT_HEADER_RE = re.compile(r"[ \t]*\.subckt\b", re.IGNORECASE)
_PARAMS_RE = re.compile(r"\bparams:", re.IGNORECASE)
#: A ``.subckt`` header's parameter part also begins at its first
#: ``key=value`` token, not only at an explicit ``params:`` keyword (issue
#: #5439): a restored template parameter whose value contains a comma
#: (``f=max(1,2)``) must not be rewritten as a bus-port group. Group 1 is the
#: token, so ``match.start(1)`` is where the parameter part begins.
_PARAM_ASSIGN_RE = re.compile(r"[ \t](\S*=)")


# --------------------------------------------------------------------------- #
# Subcircuit parameters on the restored header (issue #5439)
# --------------------------------------------------------------------------- #
#
# xschem declares a cell's subcircuit parameters in the ``template``
# attribute of its *symbol* attributes, which live in the top-level
# ``K {...}`` element of a ``.sym`` (or of the ``.sch`` itself). When a parent
# sheet instantiates the cell, xschem writes those parameters onto the child's
# ``.subckt`` card. The top sheet gets none: xschem comments its wrapper out
# (a deck has no parameters to declare), and its ``spice.awk`` post-processing
# skips every line whose first field starts with ``*`` -- so the commented
# ``**.subckt`` header never reaches the ``.subckt`` branch that would have
# added them. Under ``--block`` the premise is inverted (the sheet *is* a
# cell), so the parameters have to be restored here, or the emitted block
# references parameters it never declares and the including deck fails at
# parse time with ``unknown parameter``.

#: Tag of the element holding a cell's symbol attributes in an xschem
#: ``.sym``/``.sch``.
_SYMBOL_ATTR_TAG = "K"

#: xschem's ``spice.awk`` strips the first `` m=<digits>`` from a ``.subckt``
#: card ("m= is a multiplier not a param"). Replicated verbatim -- including
#: that it is a *prefix* match on the value, so a non-integer ``m=0.5``
#: leaves ``.5`` behind and a non-numeric ``m=M1`` is kept whole -- so a
#: block header is token-identical to the card xschem writes for the same
#: cell as a child.
_SUBCKT_MULTIPLIER_RE = re.compile(r" m=[0-9]+")

#: Template tokens xschem's ``get_sym_template()`` never puts on a ``.subckt``
#: card: the instance name and the SPICE device-letter prefix.
_TEMPLATE_DROP_KEYS = ("name", "spiceprefix")


def _is_emittable_value(value: str) -> bool:
    """Whether ``value`` can be written onto a ``.subckt`` card verbatim.

    A quoted or backslash-escaped ``template`` value can hold things SPICE
    cannot spell as a parameter default. ngspice reads the card as
    whitespace-separated tokens but re-joins a brace-delimited expression
    across whitespace, so (measured against ngspice 44):

    * whitespace **outside** braces is fatal -- the tail lands as a stray
      token and the including deck dies with ``Formula() error``
      (``note="a b"`` -> ``.subckt cell A B note=a b``), while whitespace
      **inside** them is fine (``f={a + b}`` evaluates);
    * a second ``=`` outside braces is fatal (``a=b=c``), inside them fine
      (``f={a==b}``);
    * an empty value (``note=``) and an unbalanced brace never load.

    A parameter failing this is left off the card and named in
    ``block.warning`` instead. xschem is no parity target here: for such a
    value its own card is unusable too -- it truncates the parameter list at
    the whitespace, losing that value's tail *and* every later parameter --
    so the choice is between an unloadable block and a loud, documented
    omission. Silence is what issue #5439 exists to remove, so the omission
    is reported rather than guessed at.
    """
    if not value:
        return False
    depth = 0
    for char in value:
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return False
        elif depth == 0 and (char.isspace() or char == "="):
            return False
    return depth == 0


def _braced_body(text: str, open_brace: int) -> str | None:
    """The text between ``text[open_brace]`` (a ``{``) and its matching
    ``}``, honoring xschem's backslash escapes. ``None`` if unbalanced."""
    depth = 0
    escape = False
    for i in range(open_brace, len(text)):
        char = text[i]
        if escape:
            escape = False
        elif char == "\\":
            escape = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace + 1 : i]
    return None


def _symbol_attr_element(text: str) -> tuple[str | None, bool]:
    """``(body, unterminated)`` for the ``K {...}`` element of an xschem file.

    An xschem file is a sequence of ``<tag> [args] {body}`` elements, so the
    elements are walked in order rather than pattern-matched: that way a
    ``{`` inside some *other* element's body -- a ``T {...}`` text label, say
    -- can never be mistaken for the start of the symbol attributes.
    ``body`` is ``None`` when the file has no ``K`` element; ``unterminated``
    is ``True`` when an element's braces never close.
    """
    pos = 0
    while True:
        brace = text.find("{", pos)
        if brace < 0:
            return None, False
        head = text[pos:brace].split()
        body = _braced_body(text, brace)
        if body is None:
            return None, True
        if head and head[0] == _SYMBOL_ATTR_TAG:
            return body, False
        pos = brace + len(body) + 2


def _attr_tokens(text: str) -> list[tuple[str, str]]:
    """The ``key=value`` pairs of an xschem attribute string, in order.

    Follows xschem's ``get_tok_value()``/``get_sym_template()`` lexer at
    ``with_quotes=0``: pairs are whitespace-separated, a quoted value may
    contain whitespace (and may span lines), an unescaped ``"`` or ``\\`` is
    dropped, and a backslash-escaped character is kept verbatim. A bare word
    carrying no ``=`` is not a pair and is skipped.

    This is the lexer only. What reaches a ``.subckt`` card is *not* always
    what xschem writes there: a value that cannot be a SPICE parameter
    default is filtered by :func:`subckt_params` rather than copied, since
    xschem's own card is unusable for those (see ``_VALUE_WHITESPACE_RE``).
    """
    pairs: list[tuple[str, str]] = []
    key: list[str] = []
    value: list[str] = []
    in_value = quote = escape = False

    def flush() -> None:
        if in_value and key:
            pairs.append(("".join(key), "".join(value)))

    for char in text:
        if escape:
            (value if in_value else key).append(char)
            escape = False
        elif char == "\\":
            escape = True
        elif char == '"':
            quote = not quote
        elif not quote and char in " \t\r\n":
            flush()
            key, value, in_value = [], [], False
        elif char == "=" and not in_value:
            in_value = True
        else:
            (value if in_value else key).append(char)
    flush()
    return pairs


def sibling_symbol(schematic: str) -> str | None:
    """The sibling ``<stem>.sym`` next to ``schematic``, or ``None``.

    This file is what a parent sheet instantiating the cell reads, so it
    governs both the cell's subcircuit parameters (issue #5439) and the port
    order of its ``.subckt`` card (issue #5473).
    """
    candidate = f"{os.path.splitext(schematic)[0]}.sym"
    return candidate if os.path.isfile(candidate) else None


def _symbol_attributes(schematic: str) -> tuple[dict[str, str], str | None]:
    """``(attributes, warning)`` for the cell ``schematic`` defines.

    The source is the one a parent instantiating the cell would use: the
    sibling ``<stem>.sym`` when it exists, else the schematic's own ``K {}``
    block. An unreadable source is reported as a warning rather than raised
    -- the netlist itself is still valid output.
    """
    source = sibling_symbol(schematic) or schematic
    try:
        with open(source, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        return {}, (
            f"--block could not read the symbol attributes of "
            f"{os.path.basename(source)} ({exc.strerror or exc}), so the "
            "restored '.subckt' header carries no subcircuit parameters; it "
            "was written as xschem produced it"
        )
    body, unterminated = _symbol_attr_element(text)
    if unterminated:
        return {}, (
            f"--block found an unterminated 'K {{...}}' element in "
            f"{os.path.basename(source)}, so the restored '.subckt' header "
            "carries no subcircuit parameters; it was written as xschem "
            "produced it"
        )
    if body is None:
        return {}, None
    attributes: dict[str, str] = {}
    for key, value in _attr_tokens(body):
        # xschem's get_tok_value() returns the first match for a token.
        attributes.setdefault(key, value)
    return attributes, None


def subckt_params(schematic: str) -> tuple[str, str | None]:
    """``(parameter_text, warning)`` for the cell ``schematic`` defines.

    ``parameter_text`` is the space-separated ``key=value`` list xschem puts
    on the cell's ``.subckt`` card when a parent instantiates it, derived from
    the same ``template`` attribute and filtered by the same rules: the
    ``name``/``spiceprefix`` pair is dropped, as is any token named by the
    symbol's own ``extra`` attribute (a substring test, as in xschem). Empty
    when the cell declares no ``template``, or one holding only those keys.

    One filter is klt's own rather than xschem's: a value SPICE cannot spell
    as a parameter default is omitted and named in ``warning``, because no
    card carrying it is loadable and xschem's own is not either
    (:func:`_is_emittable_value`).
    """
    attributes, warning = _symbol_attributes(schematic)
    template = attributes.get("template", "")
    if not template:
        return "", warning
    extra = attributes.get("extra", "")
    kept: list[str] = []
    skipped: list[str] = []
    for key, value in _attr_tokens(template):
        if key in _TEMPLATE_DROP_KEYS or (extra and key in extra):
            continue
        if not _is_emittable_value(value):
            skipped.append(key)
            continue
        kept.append(f"{key}={value}")
    if skipped:
        names = ", ".join(f"'{key}'" for key in skipped)
        plural = "s" if len(skipped) > 1 else ""
        detail = (
            f"--block left the subcircuit parameter{plural} {names} off the "
            f"restored '.subckt' header of {os.path.basename(schematic)}: "
            "the symbol template gives a value SPICE cannot spell as a "
            "parameter default (whitespace or a second '=' outside braces, "
            "an empty value, or an unbalanced brace), so emitting it would "
            "make the block fail at parse time in any including deck. Every "
            "other parameter was restored"
        )
        warning = detail if warning is None else f"{warning}. {detail}"
    return " ".join(kept), warning


def _append_subckt_params(text: str, schematic: str) -> tuple[str, str | None]:
    """``text`` with the cell's subcircuit parameters appended to the restored
    ``.subckt`` header (issue #5439), plus a warning when they could not be
    read.

    The parameters land after the *last* ``+`` continuation line's ports, not
    in the middle of a wrapped port list (issue #3899). A header that already
    carries a ``key=value`` token is left alone, so a future xschem that
    restores the parameters itself cannot produce a doubled list.
    """
    lines = text.split("\n")
    header = next(
        (i for i, line in enumerate(lines) if _SUBCKT_HEADER_RE.match(line)),
        None,
    )
    if header is None:
        return text, None
    last = header
    while last + 1 < len(lines) and lines[last + 1].lstrip().startswith("+"):
        last += 1
    # Neither a subcircuit name nor a port can contain '=', so any '=' in the
    # restored header means the parameters are already there.
    if any("=" in line for line in lines[header : last + 1]):
        return text, None
    params, warning = subckt_params(schematic)
    if not params:
        return text, warning
    appended = _SUBCKT_MULTIPLIER_RE.sub(" ", f" {params}", count=1).rstrip()
    if appended:
        lines[last] = lines[last].rstrip() + appended
    return "\n".join(lines), warning


def space_separate_subckt_ports(data: bytes) -> bytes:
    """Rewrite comma-joined bus ports in ``.subckt`` headers to spaces
    (issue #4490).

    xschem emits a bus port as ``a[1],a[0]``; ngspice mis-maps the ports of a
    subcircuit with two or more such groups (trailing nodes silently left
    unconnected). Only the port part of ``.subckt`` header lines and their
    ``+`` continuations changes -- text from the parameter list onward
    (whichever comes first: a ``params:`` keyword or the first ``key=value``
    token), comments and device cards are left alone. A bus-free netlist is
    returned unchanged.

    Stopping at a bare ``key=value`` token matters for the subcircuit
    parameters ``--block`` restores (issue #5439): a parameter value may
    legitimately contain a comma (``f=max(1,2)``), and spacing that comma
    would corrupt the expression. This is a deliberate divergence from
    xschem, whose ``spice.awk`` applies its ``gsub(",", " ")`` to the whole
    ``.subckt`` card, parameters included.
    """
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    in_header = False
    in_params = False
    for i, line in enumerate(lines):
        if _SUBCKT_HEADER_RE.match(line):
            in_header, in_params = True, False
        elif not (in_header and line.lstrip().startswith("+")):
            in_header = False
            continue
        if in_params:
            continue
        keyword = _PARAMS_RE.search(line)
        assign = _PARAM_ASSIGN_RE.search(line)
        starts = [m.start(g) for m, g in ((keyword, 0), (assign, 1)) if m is not None]
        split_at = min(starts) if starts else None
        head, tail = (
            (line[:split_at], line[split_at:]) if split_at is not None else (line, "")
        )
        if split_at is not None:
            in_params = True
        lines[i] = re.sub(r"[ \t]*,[ \t]*", " ", head) + tail
    return "\n".join(lines).encode("utf-8")


def apply_block_mode(
    data: bytes, *, schematic: str | None = None
) -> tuple[bytes, dict[str, Any]]:
    """Post-process a testbench-shaped xschem export into an includable block
    (issue #2680, edge 1).

    xschem comments out the top sheet's own ``.subckt``/``.ends`` pair and
    always appends a trailing ``.end`` -- correct when the top sheet is a
    testbench, wrong when it is a reusable block meant to be ``.include``d
    elsewhere. This uncomments the first commented ``.subckt``/``.ends`` pair
    found and drops the trailing ``.end``, so the result can be ``.include``d
    by a separate testbench deck without a duplicate ``.end`` terminating it
    early.

    ``schematic`` is the sheet that produced ``data``. When given, the cell's
    subcircuit parameters are restored onto the uncommented header
    (:func:`subckt_params`, issue #5439), so the emitted block declares the
    parameters its own device cards reference instead of failing the
    including deck at parse time.

    Never raises: a schematic that was not shaped like a testbench export
    (no commented header, no trailing ``.end``) is passed through unchanged,
    with the gap reported in the returned info dict's ``warning`` rather than
    failing the whole run -- the netlist itself is still valid output.
    """
    text = data.decode("utf-8", errors="replace")

    param_warning = None
    text, n_subckt = _BLOCK_SUBCKT_COMMENT_RE.subn(r"\1\2", text, count=1)
    if n_subckt:
        text = _rejoin_header_continuations(text)
        if schematic is not None:
            text, param_warning = _append_subckt_params(text, schematic)
    text, n_ends = _BLOCK_ENDS_COMMENT_RE.subn(r"\1\2", text, count=1)
    text, n_end = _TRAILING_END_RE.subn("\n", text, count=1)

    subckt_uncommented = n_subckt > 0
    ends_uncommented = n_ends > 0
    trailing_end_removed = n_end > 0

    warning = None
    missing = []
    if not subckt_uncommented:
        missing.append("no commented '**.subckt' header found to uncomment")
    if not ends_uncommented:
        missing.append("no commented '**.ends' found to uncomment")
    if not trailing_end_removed:
        missing.append("no trailing '.end' line found to remove")
    if missing:
        warning = (
            "--block requested but " + "; ".join(missing) + " -- the export "
            "may not have been shaped like a testbench netlist; it was "
            "written as xschem produced it"
        )
    if param_warning:
        warning = param_warning if warning is None else f"{warning}. {param_warning}"

    info = {
        "requested": True,
        "subckt_uncommented": subckt_uncommented,
        "ends_uncommented": ends_uncommented,
        "trailing_end_removed": trailing_end_removed,
        "warning": warning,
    }
    return text.encode("utf-8"), info
