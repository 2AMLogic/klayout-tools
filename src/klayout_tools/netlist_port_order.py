"""``klt netlist --block`` port-order diagnosis (issue #5473).

Where does a ``--block`` top sheet's ``.subckt`` port order come from? Not
from the pin instances in the schematic, which is what a schematic comment
next to them usually asserts. Measured against xschem 3.4.7:

* with **no** sibling ``<stem>.sym``, the ports follow the pin instances in
  the order they are stored in the ``.sch`` file -- not canvas position, not
  ``name=pN`` numbering -- unless every pin carries ``sim_pinnumber``, which
  overrides it;
* with a sibling ``<stem>.sym``, the ports follow **that symbol's** pin
  order instead, and ``sim_pinnumber`` on the schematic pins is ignored;
* a schematic pin the sibling symbol does not declare is **dropped from the
  ``.subckt`` card** entirely.

All three are silent, and port order is the positional contract of every
``subckt-call`` instantiation and LVS reference of the block: a reorder
mis-wires every positional caller until something electrical catches it,
several steps downstream (issue #5473).

This module is the diagnosis only. It compares the ports the schematic
declares with the ports the restored header carries and reports the
difference in ``block.port_order`` and ``block.warning``; it never rewrites
the header. Rewriting would be worse than silence: klt's order has to stay
the order xschem gives the same cell when a *parent sheet* instantiates it,
or an includable block would disagree with its own callers.

Order of evidence over guesswork: every comparison this module cannot make
soundly (a pin label that is not a plain scalar, a header port the schematic
never declared, a schematic whose pins klt cannot read) is reported as
``port_order: null`` with no warning, rather than risking a false alarm on a
contract this important.
"""

from __future__ import annotations

import os
import re
from typing import Any

from .netlist_block import (
    _PARAM_ASSIGN_RE,
    _PARAMS_RE,
    _SUBCKT_HEADER_RE,
    _attr_tokens,
    _braced_body,
    sibling_symbol,
)

#: xschem's pin devices, by symbol file name. A top sheet's ports are its
#: ``ipin``/``opin``/``iopin`` instances; a sheet whose ports come from
#: differently-named custom pin symbols is reported as not comparable rather
#: than guessed at (see the module docstring).
_PIN_SYMBOLS = frozenset({"ipin.sym", "opin.sym", "iopin.sym"})

#: Characters that take a port name outside what this comparison models.
#: xschem expands a bus or comma-list label (``a[3:0]``, ``a,b``) onto the
#: header in a form that is not a token-for-token image of the pin's ``lab``,
#: so such a schematic is reported as not comparable.
_NON_SCALAR_RE = re.compile(r"[\[\](),:{}\s]")


def _elements(text: str) -> list[tuple[list[str], str]] | None:
    """``(head_tokens, body)`` for each ``<tag> [args] {body}`` element of an
    xschem file, in file order; ``None`` when an element's braces never close.

    The same walk :func:`klayout_tools.netlist_block._symbol_attr_element`
    uses, and for the same reason: a ``{`` inside some *other* element's body
    (a ``T {...}`` text label, say) must never be mistaken for the start of
    the next element. An unbalanced brace is the whole file's problem rather
    than one element's, because every element after it is unreadable too --
    returning the prefix would look exactly like a schematic whose later pins
    were dropped from the card.
    """
    elements: list[tuple[list[str], str]] = []
    pos = 0
    while True:
        brace = text.find("{", pos)
        if brace < 0:
            return elements
        head = text[pos:brace].split()
        body = _braced_body(text, brace)
        if body is None:
            return None
        elements.append((head, body))
        pos = brace + len(body) + 2


def _first_attr(body: str, key: str) -> str | None:
    """The first value ``key`` takes in an xschem attribute string, as
    xschem's own ``get_tok_value()`` resolves it."""
    for name, value in _attr_tokens(body):
        if name == key:
            return value
    return None


def _is_pin_symbol(reference: str) -> bool:
    """Whether a ``C {...}`` symbol reference names an xschem pin device."""
    base = reference.strip().replace("\\", "/").rsplit("/", 1)[-1]
    return base.lower() in _PIN_SYMBOLS


def schematic_pin_labels(schematic: str) -> list[str] | None:
    """The port names ``schematic`` declares, in pin-instance file order.

    ``None`` when the schematic cannot be read, when a pin instance carries
    no ``lab``, or when a label is not a plain scalar name -- every case
    where a comparison against the emitted header would be a guess.
    """
    try:
        with open(schematic, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None
    elements = _elements(text)
    if elements is None:
        return None
    labels: list[str] = []
    walk = iter(elements)
    for head, body in walk:
        if not (head and head[0] == "C"):
            continue
        # A component is two elements: `C {symbol} x y rot flip {attrs}`.
        attributes = next(walk, None)
        if not _is_pin_symbol(body):
            continue
        if attributes is None:
            return None
        label = _first_attr(attributes[1], "lab")
        if label is None or _NON_SCALAR_RE.search(label):
            return None
        labels.append(label)
    return labels


def header_ports(netlist: str) -> list[str] | None:
    """The port names on the restored top-level ``.subckt`` header.

    The header is the file's first ``.subckt`` line (xschem writes the top
    sheet's own card ahead of every nested sub-block definition, which is the
    same assumption :func:`klayout_tools.netlist_block._append_subckt_params`
    makes) plus its ``+`` continuation lines. Everything from the parameter
    part onward -- a ``params:`` keyword or the first ``key=value`` token,
    exactly as :func:`klayout_tools.netlist_block.space_separate_subckt_ports`
    delimits it -- is not a port. ``None`` when there is no header to read.
    """
    lines = netlist.split("\n")
    header = next(
        (i for i, line in enumerate(lines) if _SUBCKT_HEADER_RE.match(line)),
        None,
    )
    if header is None:
        return None
    last = header
    while last + 1 < len(lines) and lines[last + 1].lstrip().startswith("+"):
        last += 1
    parts = [lines[header]]
    parts += [lines[i].lstrip()[1:] for i in range(header + 1, last + 1)]
    joined = " ".join(parts)
    keyword = _PARAMS_RE.search(joined)
    assign = _PARAM_ASSIGN_RE.search(joined)
    starts = [m.start(g) for m, g in ((keyword, 0), (assign, 1)) if m is not None]
    if starts:
        joined = joined[: min(starts)]
    tokens = joined.split()
    # tokens[0] is `.subckt`, tokens[1] the subcircuit name; the rest ports.
    return tokens[2:] if len(tokens) >= 2 else None


def _warning(
    schematic: str, declared: list[str], emitted: list[str], symbol: str | None
) -> str:
    """``block.warning`` for a port list that is not the schematic's."""
    missing = [port for port in declared if port not in emitted]
    if missing:
        names = ", ".join(f"'{port}'" for port in missing)
        plural = "s" if len(missing) > 1 else ""
        lead = (
            f"--block emitted a '.subckt' header that omits the pin{plural} "
            f"{names} declared by {os.path.basename(schematic)}"
        )
    else:
        lead = (
            "--block emitted a '.subckt' port order that is not the pin "
            f"order declared by {os.path.basename(schematic)}"
        )
    if symbol is not None:
        cause = (
            "The top sheet's ports come from the pin order of the sibling "
            f"symbol {os.path.basename(symbol)}, which is what xschem gives "
            "them -- reorder (or add) that symbol's pins to change the card"
        )
    else:
        cause = (
            "There is no sibling symbol, so the ports follow the pin order "
            "in the schematic file, overridden by 'sim_pinnumber' when every "
            "pin carries it"
        )
    return (
        f"{lead}: the schematic declares '{' '.join(declared)}' and the "
        f"header carries '{' '.join(emitted)}'. {cause}. Port order is the "
        "positional contract of every subckt-call instantiation and LVS "
        "reference of this block, so recheck its callers"
    )


def with_port_order(
    info: dict[str, Any], netlist: str, schematic: str | None
) -> dict[str, Any]:
    """``info`` plus the ``block.port_order`` diagnosis (issue #5473).

    ``port_order`` is ``None`` when the emitted ports are the ones the
    schematic declares, in that order, and when the two cannot be compared
    soundly; otherwise it is ``{"schematic": [...], "emitted": [...],
    "symbol": <path or None>}`` and ``warning`` gains a message naming both
    lists (appended to any warning already there, as ``apply_block_mode``
    joins its own).

    An emitted port the schematic never declared means klt did not read the
    schematic's pins correctly -- a custom pin symbol it does not recognise,
    most likely -- far more often than it means the symbol grew a pin, and
    the two are indistinguishable from here. That case is reported as not
    comparable rather than as a drifted order.
    """
    merged = dict(info)
    merged["port_order"] = None
    if schematic is None or not info.get("subckt_uncommented"):
        return merged
    emitted = header_ports(netlist)
    declared = schematic_pin_labels(schematic)
    if emitted is None or declared is None or declared == emitted:
        return merged
    if not declared or any(_NON_SCALAR_RE.search(port) for port in emitted):
        return merged
    if any(port not in declared for port in emitted):
        return merged
    symbol = sibling_symbol(schematic)
    merged["port_order"] = {
        "schematic": declared,
        "emitted": emitted,
        "symbol": symbol,
    }
    warning = _warning(schematic, declared, emitted, symbol)
    existing = info.get("warning")
    merged["warning"] = f"{existing}. {warning}" if existing else warning
    return merged


def port_order_drift_reason(
    entries: list[dict[str, Any]], xschem_version: str | None, symbol: str | None
) -> str:
    """``--check``'s ``drift.reason`` for a ``.subckt`` port-order-only drift
    (issue #4524, reworded for #5473).

    The original wording attributed the drift to "different xschem builds
    order ports differently" as fact. That is *a* cause, not the only one:
    the same xschem on both sides reorders a block's ports when the sibling
    symbol's pins are reordered, so the old text sent a reader looking for a
    toolchain difference that need not exist. ``symbol`` is the sibling
    symbol governing a ``--block`` top sheet's ports, when there is one.
    """
    names = ", ".join(entry["subckt"] for entry in entries)
    if symbol is not None:
        cause = (
            "the top sheet's port order comes from the pin order of the "
            f"sibling symbol {os.path.basename(symbol)}, so a reordered "
            "symbol pin is the first thing to check (a different xschem "
            "build can also order ports differently)"
        )
    else:
        cause = (
            "a cell's port order comes from the pin order of its symbol when "
            "it has one and from the pin order in its schematic otherwise, "
            "and a different xschem build can order ports differently"
        )
    return (
        f"the .subckt port order of {names} differs from the committed "
        f"netlist (local xschem {xschem_version or 'version unknown'}); "
        f"{cause} -- recheck positional subckt-call callers and LVS "
        "references before re-running without --check to refresh it"
    )
