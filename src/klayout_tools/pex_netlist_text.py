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

from ._paths import fold_spice_continuations
from ._spice_tokens import split_spice_tokens
from .op_sanity import INCLUDE_CARD_RE, IncludeState, expand_includes
from .pex_asap7 import DEVICE_MODEL_CARD_PREFIXES

#: A ``.subckt`` header line -- ``group("body")`` is everything after the
#: directive (the subcircuit name, then its pins, then optional
#: ``name=value`` parameters). See :func:`_subckt_interfaces`.
_SUBCKT_RE = re.compile(r"^\s*\.subckt\s+(?P<body>\S.*?)\s*$", re.IGNORECASE)


def _read_text(path: str) -> str | None:
    """``path``'s text, or ``None`` if it cannot be read -- every caller here
    is building a *diagnostic*, so an unreadable file degrades the diagnostic
    rather than raising over it."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _logical_lines(netlist_text: str) -> list[str]:
    """``netlist_text``'s lines with SPICE ``+`` continuation lines folded
    into the line they continue, and inline ``$``/``;`` comments stripped --
    so :func:`_subckt_interfaces` sees each `.subckt` header whole."""
    lines = [
        re.split(r"\s\$|;", raw, maxsplit=1)[0].rstrip()
        for raw in netlist_text.splitlines()
    ]
    return fold_spice_continuations(lines)


def _subckt_interfaces(netlist_text: str) -> dict[str, tuple[str, list[str]]]:
    """``{lowercased subckt name: (name as written, [pin names])}`` for every
    ``.subckt`` header in ``netlist_text``, in file order.

    Pins stop at the first ``name=value`` token (or a ``params:`` keyword) --
    a `.subckt` header's trailing default-parameter assignments are not part
    of its terminal list. Lowercased keys because ngspice itself is
    case-insensitive about subcircuit names, and `klt extract` writes an
    upper-case ``.SUBCKT`` header where a hand-written schematic netlist
    often does not.
    """
    interfaces: dict[str, tuple[str, list[str]]] = {}
    for line in _logical_lines(netlist_text):
        match = _SUBCKT_RE.match(line)
        if not match:
            continue
        tokens = match.group("body").split()
        name = tokens[0]
        pins: list[str] = []
        for token in tokens[1:]:
            if "=" in token or token.lower() in ("params:", "param:"):
                break
            pins.append(token)
        interfaces.setdefault(name.lower(), (name, pins))
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


def _reference_is_hierarchical(netlist_text: str) -> bool:
    """``True`` if ``netlist_text`` contains an `X` device-instance card
    whose target names a `.subckt` **defined elsewhere in the same file**
    -- i.e. the netlist is hierarchical (a leaf `.subckt` plus one or more
    instance calls of it) rather than flat.

    `klt extract`'s own output is always flat, by design
    (``docs/cli/extract.md``: "Extraction is flat, not hierarchical"), so a
    hierarchical *reference* netlist compared against it is exactly the
    shape issue #1085 already documents for `klt lvs` (a leaf `.subckt`
    called N times on the reference side has no counterpart circuit on the
    flat extracted side to pair against). The same mismatch shows up here:
    a device nested inside a locally-defined `.subckt` appears once per
    *definition* in `_device_model_counts`'s card scan, not once per
    physical *instance*, while the flat extracted side always counts once
    per instance -- so a purely hierarchy-representation difference can
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
