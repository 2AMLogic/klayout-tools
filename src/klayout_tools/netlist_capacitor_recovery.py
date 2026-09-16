"""Recover a capacitor device's real device-class name across a
``klt extract``-written, bare-``C``-card SPICE round trip (issue #1876).

**Background.** Issue #1558 stopped ``klt extract -o netlist.spice`` from
writing an unbound capacitor's device-class name as a trailing (4th) token
on its ``C`` card (``C$1 a b 1.19e-13 <class>``), because ngspice's native
``C``-element parser reads that trailing token as a required capacitor
``.model`` reference -- and no deck's internal capacitor-class label is a
real ``.model``/``.subckt`` name, so the old card could never actually be
simulated. The card is now bare and value-only (``C$1 a b 1.19e-13``), which
is correct for simulatability, but leaves nothing in the card's own text for
``kdb.NetlistSpiceReader`` to recover the capacitor's real class from on
read-back -- it registers the device under KLayout's own generic/anonymous
capacitor class (name ``"CAP"``) instead. That breaks ``klt lvs``'s
name-based device-class correspondence whenever the *other* side of the
compare (a hand-authored/schematic-derived reference, or a layout netlist
extracted by a pre-#1558 ``klt``) names the capacitor's class explicitly.

**The fix.** ``kdb.NetlistSpiceWriter`` itself (not ``klt`` code) already
emits one ``* device instance <name> <transform...> <x,y> [<class>]``
comment line immediately before *every* device card it writes, trailing
class name included -- see
:func:`klayout_tools.pdk_models.create_model_binding_delegate`'s own
docstring and ``tests/test_extract.py``'s
``test_pdk_unbound_mim_flavour_writes_bare_value_only_capacitor_card``,
which locks the exact comment text. This module scans a SPICE netlist's own
text for that comment (:func:`parse_capacitor_class_comments`) and builds a
``kdb.NetlistSpiceReaderDelegate``-backed reader
(:func:`make_capacitor_class_recovery_reader`) that reattaches the recovered
class to an otherwise-bare ``C`` card's device -- instead of falling back to
KLayout's generic ``CAP`` class -- while deferring every other device/card to
KLayout's own default reading behaviour, byte-for-byte unchanged.

A missing or malformed comment (e.g. a hand-edited SPICE file, or a bare
``C`` card KLayout's own default writer -- not ``klt`` -- produced without a
preceding device-instance comment at all) degrades gracefully: the device
simply reads as the generic ``CAP`` class, exactly as it did before this
module existed. This mechanism never changes what is written to disk -- only
how ``klt`` itself re-reads a netlist for ``klt lvs``'s pre-extracted
``layout.netlist`` shape and reference-netlist reads; the bare, value-only
``C`` card format issue #1558 fixed is completely unaffected.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import klayout.db as kdb

#: KLayout's own reader assigns this name to the implicit top-level circuit
#: when a SPICE file has device cards outside any ``.subckt`` block (verified
#: empirically against ``kdb.NetlistSpiceReader``) -- the scanner below starts
#: in this same scope so a (rare) top-level bare capacitor card still
#: recovers correctly, rather than silently never matching because of a
#: circuit-name mismatch between the text scan and the real reader.
_TOP_LEVEL_CIRCUIT_NAME = ".TOP"

_SUBCKT_RE = re.compile(r"^\.subckt\s+(\S+)", re.IGNORECASE)
_ENDS_RE = re.compile(r"^\.ends\b", re.IGNORECASE)
_DEVICE_INSTANCE_COMMENT_RE = re.compile(
    r"^\*\s*device\s+instance\s+(\S+)\s+(.*)$", re.IGNORECASE
)


def _class_name_from_comment_rest(rest: str) -> str | None:
    """The trailing device-class-name token from a `` * device instance
    <name> <rest>`` comment's own ``<rest>`` text, or ``None`` when the
    comment carries no class name at all (an anonymous device class, e.g.
    every ``--parasitics`` ground/coupling capacitor).

    Found by locating the *last* whitespace-separated token containing a
    ``,`` -- KLayout's own ``x,y`` location marker, always comma-joined with
    no internal space, always present -- rather than assuming a fixed token
    count before it. The transform/array-index token(s) between the device
    name and the location are KLayout's own internal writer detail, not part
    of any contract ``klt`` relies on; anchoring on the location token's own
    distinguishing feature (a literal comma no other token in the comment
    carries) keeps this resilient to that detail changing.
    """
    tokens = rest.split()
    location_index = None
    for index, token in enumerate(tokens):
        if "," in token:
            location_index = index
    if location_index is None or location_index == len(tokens) - 1:
        return None
    class_name = " ".join(tokens[location_index + 1 :])
    return class_name or None


def parse_capacitor_class_comments(text: str) -> dict[tuple[str, str], str]:
    """Scan ``text`` (a ``klt extract``-written SPICE netlist, or any SPICE
    text following the same ``kdb.NetlistSpiceWriter`` convention) for
    `` * device instance <name> ... <class>`` comment lines immediately
    followed by that same device's own bare ``C`` card, and return a
    ``{(circuit_name, device_name): class_name}`` map recovering the class
    name for each one found.

    Association is by strict physical adjacency (the comment line
    immediately precedes the device card it describes, exactly as
    ``kdb.NetlistSpiceWriter`` emits it) plus a same-name, same-type sanity
    check against the following card's own leading letter and name --
    deliberately conservative: a mismatch, or no comment at all, simply
    leaves that device out of the returned map (callers degrade gracefully
    to the generic capacitor class for anything not found here, see
    :func:`make_capacitor_class_recovery_reader`).
    """
    recovered: dict[tuple[str, str], str] = {}
    current_circuit = _TOP_LEVEL_CIRCUIT_NAME
    pending: tuple[str, str] | None = None  # (device_name, class_name)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("*"):
            match = _DEVICE_INSTANCE_COMMENT_RE.match(line)
            if match:
                device_name = match.group(1)
                class_name = _class_name_from_comment_rest(match.group(2))
                pending = (device_name, class_name) if class_name else None
            else:
                pending = None
            continue

        subckt_match = _SUBCKT_RE.match(line)
        if subckt_match:
            current_circuit = subckt_match.group(1)
            pending = None
            continue
        if _ENDS_RE.match(line):
            current_circuit = _TOP_LEVEL_CIRCUIT_NAME
            pending = None
            continue

        if pending is not None:
            device_name, class_name = pending
            first_token = line.split(None, 1)[0]
            if first_token[:1].upper() == "C" and first_token[1:] == device_name:
                recovered[(current_circuit, device_name)] = class_name
        pending = None

    return recovered


def make_capacitor_class_recovery_reader(
    recovered: Mapping[tuple[str, str], str],
) -> kdb.NetlistSpiceReader:
    """Build a ``kdb.NetlistSpiceReader`` whose delegate reattaches a
    recovered capacitor device-class name (see
    :func:`parse_capacitor_class_comments`) to a bare ``C`` card's device
    instead of KLayout's own generic/anonymous ``CAP`` class -- for exactly
    the ``(circuit_name, device_name)`` pairs ``recovered`` names. Every
    other device/card (including a bare ``C`` card with no entry in
    ``recovered`` -- a missing or malformed comment) defers to KLayout's
    default reading behaviour, unchanged.

    The recovered device class is looked up by name in the netlist's own
    device-class registry first (``Circuit.netlist().device_class_by_name``)
    and only created when absent, so multiple devices/circuits sharing the
    same recovered class name -- and, moreover, a class name genuinely bound
    elsewhere in the same file via a real ``X``-card model binding --
    correctly share one class object, exactly the way KLayout's own default
    reading already shares one class object per class name.
    """
    import klayout.db as kdb

    class _CapacitorClassRecoveringDelegate(kdb.NetlistSpiceReaderDelegate):
        def element(
            self,
            circuit: kdb.Circuit,
            element_type: str,
            name: str,
            model: str,
            value: float,
            nets: list,
            params: dict,
        ) -> bool:
            if element_type == "C" and not model:
                class_name = recovered.get((circuit.name, name))
                if class_name:
                    netlist = circuit.netlist()
                    device_class = netlist.device_class_by_name(class_name)
                    if device_class is None or not isinstance(
                        device_class, kdb.DeviceClassCapacitor
                    ):
                        device_class = kdb.DeviceClassCapacitor()
                        device_class.name = class_name
                        netlist.add(device_class)
                    terminals = device_class.terminal_definitions()
                    if len(nets) == len(terminals):
                        device = circuit.create_device(device_class, name)
                        for net, terminal in zip(nets, terminals, strict=True):
                            device.connect_terminal(terminal.name, net)
                        device.set_parameter("C", value)
                        self.apply_parameter_scaling(device)
                        return True
                    # Defensive only -- never seen in practice (a capacitor
                    # card always carries exactly 2 nets/terminals). Falling
                    # through to the default handler below is safer than
                    # silently misconnecting a terminal.
            return super().element(
                circuit, element_type, name, model, value, nets, params
            )

    return kdb.NetlistSpiceReader(_CapacitorClassRecoveringDelegate())
