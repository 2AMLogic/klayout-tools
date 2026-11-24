"""SPICE-netlist *text* primitives shared by ``klt pex``'s diagnostics.

Factored out of ``pex.py`` (issue #5477) with no behaviour change: these are
the pure text readers/tallies every netlist diagnostic in that module builds
on -- logical-line folding, ``.subckt`` header parsing, the device-model
instance multiset, and the ``.include`` expansion the multiset reads through.
They depend on nothing in ``pex.py``, so they live here rather than keeping
that module's line count climbing; ``pex`` re-imports every name below, so
``klayout_tools.pex._logical_lines`` & co. still resolve for callers (and for
tests that monkeypatch them) exactly as before.
"""

from __future__ import annotations

import os
import re

from ._spice_text import fold_spice_continuations, strip_spice_inline_comment
from ._spice_tokens import spice_nodes, split_spice_tokens
from .op_sanity import INCLUDE_CARD_RE, IncludeState, expand_includes
from .pex_asap7 import DEVICE_MODEL_CARD_PREFIXES

#: A ``.subckt`` header line -- ``group("body")`` is everything after the
#: directive (the subcircuit name, then its pins, then optional
#: ``name=value`` parameters). See :func:`_subckt_interfaces`.
_SUBCKT_RE = re.compile(r"^\s*\.subckt\s+(?P<body>\S.*?)\s*$", re.IGNORECASE)

#: A ``.ends`` line, closing the innermost open ``.subckt``.
_ENDS_RE = re.compile(r"^\s*\.ends\b", re.IGNORECASE)


def _logical_lines(netlist_text: str) -> list[str]:
    """``netlist_text``'s lines with ``+`` continuations folded onto the line
    they continue and inline ``$``/``;`` comments stripped by the shared
    rule in ``_spice_text`` -- so :func:`_subckt_interfaces` sees each
    `.subckt` header whole, and a ``$PDK_ROOT``-style ``.include`` reaches
    :func:`_has_include_card`/:func:`_expand_reference_includes` unmangled."""
    lines = [strip_spice_inline_comment(raw) for raw in netlist_text.splitlines()]
    return fold_spice_continuations(lines)


def _logical_cards(text: str) -> list[str]:
    """``text``'s SPICE cards with ``+`` continuation lines joined and
    full-line ``*`` comments dropped (inline comments are *not* stripped
    here -- unlike :func:`_logical_lines`)."""
    return fold_spice_continuations(
        [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.startswith("*")
        ]
    )


def _subckt_interfaces(
    netlist_text: str, *, file_scope_only: bool = False
) -> dict[str, tuple[str, list[str]]]:
    """``{lowercased subckt name: (name as written, [pin names])}`` for every
    ``.subckt`` header in ``netlist_text``, in file order.

    With ``file_scope_only``, nested definitions are private to their parent.
    Pins stop at the first ``name=value`` token (or a ``params:`` keyword) --
    a `.subckt` header's trailing default-parameter assignments are not part
    of its terminal list. Lowercased keys because ngspice itself is
    case-insensitive about subcircuit names, and `klt extract` writes an
    upper-case ``.SUBCKT`` header where a hand-written schematic netlist
    often does not.
    """
    interfaces: dict[str, tuple[str, list[str]]] = {}
    depth = 0
    for line in _logical_lines(netlist_text):
        if _ENDS_RE.match(line):
            depth = max(0, depth - 1)
            continue
        match = _SUBCKT_RE.match(line)
        if not match:
            continue
        depth += 1
        if file_scope_only and depth > 1:
            continue
        tokens = split_spice_tokens(match.group("body"))
        name = tokens[0]
        pins: list[str] = []
        for token in tokens[1:]:
            if "=" in token or token.lower() in ("params:", "param:"):
                break
            pins.append(token)
        interfaces.setdefault(name.lower(), (name, spice_nodes(pins)))
    return interfaces


def _dut_declares_a_circuit(text: str) -> bool:
    """``True`` if ``text`` -- a ``.include``/``.inc`` target's own file
    contents -- declares at least one ``.subckt`` header or circuit element
    card of its own, i.e. plausibly *is* a DUT netlist rather than
    parameter/model/corner-file content with no circuit of its own (Gap 1,
    issue #1255: a testbench whose only `.include` happens to name a shared
    corner/parameter file used to be silently swapped in as if it were the
    DUT).

    Deliberately permissive about *how* the DUT is written: both a
    ``.SUBCKT``-wrapped file (any header at all, even an empty body --
    ``_subckt_interfaces`` only parses headers) and a flat file (bare device
    cards, no wrapper) pass this check; only a file with *no* circuit
    content at all -- pure ``.param``/``.model``/``.lib``/comment/`.` control
    cards -- fails it. A flat schematic DUT against a `.SUBCKT`-wrapped
    extraction is a *different*, separately-diagnosed condition -- see
    :func:`_flat_dut_mismatch`.

    Every non-blank line that survives comment-stripping and is not itself
    a `.`-prefixed control card is treated as a circuit element -- SPICE's
    own convention is that such a line names a device/subcircuit instance
    (``R``/``C``/``L``/``M``/``Q``/``D``/``X``/``V``/``I``/...), so no fixed
    prefix list is needed.
    """
    if _subckt_interfaces(text):
        return True
    for line in _logical_lines(text):
        stripped = line.strip()
        if not stripped or stripped.startswith(("*", ".")):
            continue
        return True
    return False


def _device_model_counts(netlist_text: str) -> dict[str, tuple[str, int]]:
    """``{lowercased model/subcircuit name: (name as written, instance
    count)}`` for every `M` (raw MOSFET)/`X` (subcircuit call, `klt
    extract`'s own convention for a bound MOS/bipolar device -- see
    ``pdk_models.py``'s ``_ModelBindingSpiceWriterDelegate.write_device``)/
    `Q` (raw BJT)/`D` (raw diode) device-instance card in ``netlist_text``,
    in file order.

    The model/subcircuit name is *the last whitespace token on the card that
    is not a ``name=value`` parameter assignment* -- SPICE's own convention
    is that a device card's trailing tokens, after its nodes, are either
    exactly one model/subcircuit name or a run of ``name=value`` parameters
    (e.g. ``L=``/``W=``/``AS=``/``AD=``), never both interleaved. This holds
    identically for a raw ``M<name> d g s b <model> [L=... W=...]`` card and
    for an ``X<name> <nodes...> <subckt> [param=value ...]`` subcircuit
    call, so one rule covers both without needing to know each card's exact
    node count up front.

    Lowercased keys, original-case values -- the same case-insensitive-key
    convention :func:`_subckt_interfaces` uses, since ngspice is
    case-insensitive about model/subcircuit names the same way it is about
    `.subckt` names.
    """
    counts: dict[str, tuple[str, int]] = {}
    for line in _logical_lines(netlist_text):
        stripped = line.strip()
        if not stripped or stripped[0].upper() not in DEVICE_MODEL_CARD_PREFIXES:
            continue
        parts = [token for token in split_spice_tokens(stripped) if "=" not in token]
        if len(parts) < 2:
            continue  # no nodes/model at all -- not a real device instance
        name = parts[-1]
        key = name.lower()
        existing_name, existing_count = counts.get(key, (name, 0))
        counts[key] = (existing_name, existing_count + 1)
    return counts


_Counts = dict[str, tuple[str, int]]


def _device_card(line: str) -> tuple[str, str] | None:
    """``(card prefix, model/subcircuit name)`` of a device-instance line, by
    :func:`_device_model_counts`'s own rule, or ``None`` for any other line."""
    stripped = line.strip()
    if not stripped or stripped[0].upper() not in DEVICE_MODEL_CARD_PREFIXES:
        return None
    tokens = [t for t in split_spice_tokens(stripped) if "=" not in t]
    return (stripped[0].upper(), tokens[-1]) if len(tokens) >= 2 else None


def _subckt_cards(netlist_text: str) -> dict[str, list[tuple[str, str]]]:
    """The device cards of each ``.subckt`` definition, keyed by lowercased
    name; ``""`` holds the cards written outside any definition."""
    scopes: dict[str, list[tuple[str, str]]] = {"": []}
    stack: list[str] = []
    for line in _logical_lines(netlist_text):
        header = _SUBCKT_RE.match(line)
        if header:
            stack.append(header.group("body").split()[0].lower())
            scopes.setdefault(stack[-1], [])
        elif _ENDS_RE.match(line):
            if stack:
                stack.pop()
        else:
            card = _device_card(line)
            if card:
                scopes[stack[-1] if stack else ""].append(card)
    return scopes


def _add_counts(into: _Counts, found: _Counts) -> None:
    for key, (shown, count) in found.items():
        name, have = into.get(key, (shown, 0))
        into[key] = (name, have + count)


class _Instantiation:
    """The devices a ``.subckt`` instantiates, following its local ``X`` calls
    (``follow``) or, when no root is known, ignoring them."""

    def __init__(self, cards: dict[str, list[tuple[str, str]]], follow: bool) -> None:
        self._cards = cards
        self._local = {name for name in cards if name}
        self._follow = follow
        self._memo: dict[str, _Counts] = {}

    def expand(self, scope: str, trail: tuple[str, ...]) -> _Counts:
        if scope not in self._memo:
            out: _Counts = {}
            for prefix, name in self._cards[scope]:
                key = name.lower()
                if prefix != "X" or key not in self._local:
                    _add_counts(out, {key: (name, 1)})
                elif self._follow and key not in trail:
                    _add_counts(out, self.expand(key, (*trail, key)))
            self._memo[scope] = out
        return self._memo[scope]


def _hierarchy_root(local: set[str], called: set[str], top: str | None) -> str | None:
    """``top`` when it names a local definition (case-insensitively), else the
    sole definition nothing calls, else ``None``."""
    wanted = (top or "").lower()
    if wanted in local:
        return wanted
    roots = local - called
    return next(iter(roots)) if len(roots) == 1 else None


def _instantiated_model_counts(netlist_text: str, top: str | None) -> _Counts:
    """:func:`_device_model_counts` for the devices *instantiated from* ``top``
    when ``netlist_text`` calls its own ``.subckt`` definitions (issue #6559),
    and exactly that card scan otherwise.

    ``klt extract --engine klayout-runset`` writes every circuit the runset
    produced, so a hierarchical runset leaves ``X`` calls into locally defined
    ``.subckt``\\ s. A plain card scan is wrong twice over there: each call is
    tallied as a device *named after the local cell*, and a device inside a
    definition counts once per definition rather than once per instance. Here
    an ``X`` card whose target is defined in the same text is a hierarchy edge,
    not a device: it contributes what its target instantiates, once per call.
    The cell name is never a key, and a leaf called twice counts twice.

    A netlist with no such call -- every flat netlist, and every ``klt extract``
    curated-deck output -- is returned by the plain card scan, unchanged.
    ``top`` names the root case-insensitively; failing that, the sole
    definition nothing else calls. When neither identifies one (not reachable
    from ``klt extract``, which refuses a netlist without a single top circuit)
    every definition is counted once with its local calls dropped, so a cell
    name is still never reported as a device family.
    """
    cards = _subckt_cards(netlist_text)
    local = {name for name in cards if name}
    called = {
        name.lower()
        for scope in cards.values()
        for prefix, name in scope
        if prefix == "X" and name.lower() in local
    }
    if not called:
        return _device_model_counts(netlist_text)

    root = _hierarchy_root(local, called, top)
    hierarchy = _Instantiation(cards, follow=root is not None)
    merged: _Counts = {}
    for scope in ["", *([root] if root is not None else sorted(local))]:
        _add_counts(merged, hierarchy.expand(scope, (scope,)))
    return merged


def _reference_is_hierarchical(netlist_text: str) -> bool:
    """``True`` if ``netlist_text`` contains an `X` device-instance card
    whose target names a `.subckt` **defined elsewhere in the same file**
    -- i.e. the netlist is hierarchical (a leaf `.subckt` plus one or more
    instance calls of it) rather than flat.

    `klt extract`'s curated-engine output is flat, by design
    (``docs/cli/extract.md``: "Extraction is flat, not hierarchical"; a
    ``--engine klayout-runset`` extraction may instead be hierarchical,
    whose devices are counted by instantiation), so a hierarchical
    *reference* netlist compared against a flat extraction is exactly the
    shape issue #1085 already documents for `klt lvs` (a leaf `.subckt`
    called N times on the reference side has no counterpart circuit on the
    flat extracted side to pair against). The same mismatch shows up here:
    a device nested inside a locally-defined `.subckt` appears once per
    *definition* in `_device_model_counts`'s card scan, not once per
    physical *instance*, while a flat extracted side counts once per
    instance -- so a purely hierarchy-representation difference can
    produce the same divergent multiset a genuine device-flavour swap
    would, without being one. See :func:`_model_mismatch`.
    """
    local_subckts = set(_subckt_interfaces(netlist_text))
    if not local_subckts:
        return False
    for line in _logical_lines(netlist_text):
        stripped = line.strip()
        if not stripped or stripped[0].upper() != "X":
            continue
        parts = [token for token in split_spice_tokens(stripped) if "=" not in token]
        if len(parts) < 2:
            continue
        if parts[-1].lower() in local_subckts:
            return True
    return False


def _has_include_card(netlist_text: str) -> bool:
    return any(
        INCLUDE_CARD_RE.match(line.strip()) for line in _logical_lines(netlist_text)
    )


def _expand_reference_includes(netlist_text: str, netlist_path: str) -> str:
    """``netlist_text`` with each resolvable ``.include``/``.inc`` card
    replaced by the included file's lines (issue #4630), so a thin
    ``.subckt`` wrapper around an included device file is tallied by its
    real devices. Unresolvable includes are dropped, never raised."""
    base_dir = os.path.dirname(os.path.abspath(netlist_path))
    return "\n".join(
        expand_includes(_logical_lines(netlist_text), base_dir, IncludeState(base_dir))
    )
