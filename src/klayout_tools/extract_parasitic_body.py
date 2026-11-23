"""Body/bulk terminal classification for the star parasitics topology, and
the ``--parasitic-variant`` request grammar itself (issue #7317).

`klt extract --parasitics` fans every net out into a star: each device
terminal gets its own leg net behind a series resistor. For OSDI compact
models whose body terminal is *near-floating* -- driven only by the model's
internal capacitances, with no low-impedance path of its own -- that extra
leg node makes ngspice transient Newton iteration fail
(``timestep_too_small``) on circuits that converge when the body terminal
sits on the net hub. Body terminals are not signal paths, so a quasi-static
star gains nothing from separating them.

``--parasitic-variant drop-r-body:NET|*`` (opt-in, issue #7317) answers
that: on the matched nets, *body-class* terminals stay on the hub (no leg
net, no series ``R`` card) and the net's total series R is re-split across
the remaining signal terminals only, so the net's total is preserved.
Omitted, the flag leaves netlist and JSON byte-identical -- the same
guarantee ``drop-r`` documents.

Split out of ``extract_parasitics.py`` rather than grown there: that file
is size-ratchet pinned, and the ``ParasiticVariants`` grammar block moved
here wholesale (re-exported by ``extract_parasitics.py`` so every existing
``from klayout_tools.extract_parasitics import parse_parasitic_variants``
call site -- ``cli/_arg_types.py``, ``extract_validate.py``, tests -- keeps
working unchanged).

Like the rest of the parasitics subsystem this is a **private implementation
module, not part of the public API**.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

#: ``*`` wildcard of the ``drop-r:``/``drop-r-body:``/``drop-c-ground:``
#: variant verbs.
ALL_NETS = "*"

#: Closed grammar of ``--parasitic-variant`` (issue #3056; #7317 added the
#: ``drop-r-body`` verb).
PARASITIC_VARIANT_GRAMMAR = (
    "drop-r:NET|*, drop-r-body:NET|*, drop-c-ground:NET|*, drop-c-coupling"
)


@dataclass(frozen=True)
class ParasiticVariants:
    """Parsed ``--parasitic-variant`` request (issue #3056).

    ``drop_r``/``drop_r_body``/``drop_c_ground`` hold escaped net names (the
    spelling the report uses) or :data:`ALL_NETS` for the ``*`` wildcard.
    ``raw`` is the request as given, echoed back verbatim in the report.
    """

    raw: tuple[str, ...] = ()
    drop_r: frozenset[str] = frozenset()
    drop_r_body: frozenset[str] = frozenset()
    drop_c_ground: frozenset[str] = frozenset()
    drop_c_coupling: bool = False

    def drops_r(self, net: str) -> bool:
        return ALL_NETS in self.drop_r or net in self.drop_r

    def drops_r_body(self, net: str) -> bool:
        return ALL_NETS in self.drop_r_body or net in self.drop_r_body

    def drops_any_r(self, net: str) -> bool:
        """``drop-r`` or ``drop-r-body`` names this net -- the two verbs a
        ``--distributed-rc`` ladder must refuse together."""
        return self.drops_r(net) or self.drops_r_body(net)

    def drops_c_ground(self, net: str) -> bool:
        return ALL_NETS in self.drop_c_ground or net in self.drop_c_ground

    def named_nets(self) -> frozenset[str]:
        return (self.drop_r | self.drop_r_body | self.drop_c_ground) - {ALL_NETS}


def parse_parasitic_variants(
    raw: Sequence[str] | None, error: type[Exception]
) -> ParasiticVariants | None:
    """Parse repeated ``--parasitic-variant`` values (issue #3056); ``None``
    when none were given. Anything outside the closed grammar raises
    ``error`` (the calling command's own error class)."""
    if not raw:
        return None
    drop_r: set[str] = set()
    drop_r_body: set[str] = set()
    drop_c_ground: set[str] = set()
    drop_c_coupling = False
    for item in raw:
        verb, sep, net = item.partition(":")
        if verb == "drop-c-coupling" and not sep:
            drop_c_coupling = True
        elif verb == "drop-r" and sep and net:
            drop_r.add(net)
        elif verb == "drop-r-body" and sep and net:
            drop_r_body.add(net)
        elif verb == "drop-c-ground" and sep and net:
            drop_c_ground.add(net)
        else:
            raise error(
                f"unknown --parasitic-variant {item!r} -- expected one of: "
                f"{PARASITIC_VARIANT_GRAMMAR}"
            )
    return ParasiticVariants(
        raw=tuple(raw),
        drop_r=frozenset(drop_r),
        drop_r_body=frozenset(drop_r_body),
        drop_c_ground=frozenset(drop_c_ground),
        drop_c_coupling=drop_c_coupling,
    )


#: Which terminal of each exact KLayout device class is the body/bulk
#: terminal (issue #7317; names verified against ``klayout.db``'s
#: ``terminal_definitions()``). Keyed by ``type(device_class).__name__``,
#: **not** ``isinstance``: ``DeviceClassMOS4Transistor`` subclasses the
#: 3-terminal MOS class, ``DeviceClassBJT4Transistor`` subclasses
#: ``DeviceClassBJT3Transistor``, and the WithBulk classes subclass their
#: plain ones, so a subclass test cannot tell a MOS4 from a MOS3. Every
#: class absent here has no body terminal -- most notably
#: ``DeviceClassBJT3Transistor``, whose ``B`` is the *base*, a signal
#: terminal, never a body.
_BODY_TERMINAL_BY_CLASS_NAME = {
    "DeviceClassMOS4Transistor": "B",
    "DeviceClassResistorWithBulk": "W",
    "DeviceClassCapacitorWithBulk": "W",
    "DeviceClassBJT4Transistor": "S",
}


def body_terminal_flags(terminal_refs: list[Any]) -> list[bool]:
    """Parallel per-terminal flags: which of ``terminal_refs`` (KLayout
    ``DeviceTerminalRef`` objects, as ``Net.each_terminal()`` yields) are
    their device's body/bulk terminal, per
    :data:`_BODY_TERMINAL_BY_CLASS_NAME`. A device class with no body entry,
    or a terminal whose name is not that class's body terminal, is a signal
    terminal."""
    flags: list[bool] = []
    for term_ref in terminal_refs:
        class_name = type(term_ref.device().device_class()).__name__
        body = _BODY_TERMINAL_BY_CLASS_NAME.get(class_name)
        flags.append(body is not None and term_ref.terminal_def().name == body)
    return flags


def star_hub_only_indices(
    variants: ParasiticVariants | None,
    net_escaped: str,
    drop_r: bool,
    terminal_refs: list[Any],
) -> frozenset[int]:
    """Indices of ``terminal_refs`` that stay on the star's hub (no leg net,
    no series ``R`` card): **every** index under ``drop-r`` (issue #3056),
    only the body-class terminals under ``drop-r-body`` (issue #7317), and
    none otherwise.

    A ``drop-r-body`` net whose terminals are *all* body-class keeps the
    existing all-terminal star (empty set): there is no signal terminal to
    hand the net's resistance to, so merging would change nothing but the
    net's topology. ``drop-r`` wins when both name the same net -- it is the
    stronger drop."""
    if drop_r:
        return frozenset(range(len(terminal_refs)))
    if variants is None or not variants.drops_r_body(net_escaped):
        return frozenset()
    flags = body_terminal_flags(terminal_refs)
    if all(flags):
        return frozenset()
    return frozenset(i for i, body in enumerate(flags) if body)


def body_adjusted_star_weights(
    positions: list[tuple[float, float]], hub_only: frozenset[int]
) -> list[float]:
    """Per-terminal star resistance-split weights over ``positions``, with
    the ``hub_only`` terminals pinned to ``0.0`` and the remainder
    re-normalized over the signal terminals' positions only (issue #7317)
    -- so a ``drop-r-body`` net's leg resistances still sum back to its
    total, the same invariant :func:`_terminal_star_weights` guarantees for
    the unmodified star. Empty ``hub_only`` returns the plain weights,
    byte-identical to the pre-#7317 star split."""
    from .extract_parasitics import _terminal_star_weights

    if not hub_only:
        return _terminal_star_weights(positions)
    signal_weights = iter(
        _terminal_star_weights(
            [p for i, p in enumerate(positions) if i not in hub_only]
        )
    )
    return [
        0.0 if i in hub_only else next(signal_weights) for i in range(len(positions))
    ]
