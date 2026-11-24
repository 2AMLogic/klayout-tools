"""Native SG13G2 bipolar and MOS reference cards for the ``klayout-runset``
engine (issue #7836).

IHP's ``sg13g2.lvs`` reader delegate turns only ``M``/``C``/``R``/``Q``/``L``/
``D`` cards into devices (``CUSTOM_READER`` in ``rule_decks/globals.lvs``). An
``X`` call to ``npn13G2v`` is therefore read as a call to an undefined
subcircuit and the device is never compared, and the generic ``subckt-call``
conversion writes a MOS call as ``nfet``/``pfet``, which no runset class
matches. This module writes the plain-element card the reader pairs with, in
exactly the form the pinned PDK's own xschem symbols emit for LVS
(``ihp-sg13g2/libs.tech/xschem/sg13g2_pr/<model>.sym``, ``lvs_format``):

* ``npn13G2v``: ``Q<n> C B E S npn13G2v le=<El>u we=120n m=<Nx>``
* ``npn13G2l``: ``Q<n> C B E S npn13G2l le=<El>u we=70n m=<Nx>``
* ``npn13G2``: ``Q<n> C B E S npn13G2 le=900n we=70n m=<Nx>``
* ``sg13_{lv,hv}_{n,p}mos``: ``M<n> D G S B <model> w= l= ng= m=``

``El`` is in micrometres, so the symbol's ``El * 1e-6`` is the ``u`` suffix.
A call this module cannot convert faithfully is *not* guessed at: it returns
``None`` and the call stays an ``X`` card for the unresolved-call scan to
report."""

from __future__ import annotations

import re

#: Bipolar callee (upper-cased) -> ``(we, le)``; a ``None`` ``le`` is the
#: call's own ``El`` (micrometres), which the model has no safe default for.
_BIPOLAR: dict[str, tuple[str, str | None]] = {
    "NPN13G2V": ("120n", None),
    "NPN13G2L": ("70n", None),
    "NPN13G2": ("70n", "900n"),
}

#: The MOS classes ``rule_decks/mos_extraction.lvs`` extracts.
_MOS = frozenset({"SG13_LV_NMOS", "SG13_LV_PMOS", "SG13_HV_NMOS", "SG13_HV_PMOS"})

#: A bare micrometre value: a unit suffix would scale it a second time.
_PLAIN_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")

_TERMINALS = 4


def native_primitive_names() -> frozenset[str]:
    """Upper-cased callee names this module writes native cards for."""
    return frozenset(_BIPOLAR) | _MOS


def native_card(
    tokens: list[str], positional: list[str], params: dict[str, str]
) -> str | None:
    """The native card for the ``X`` call ``tokens``, or ``None`` when it
    cannot be written faithfully.

    ``positional`` is the call's nodes plus the callee, and ``params`` its
    ``key=value`` map with lower-cased keys (``_split_params``)."""
    nodes, callee = positional[:-1], positional[-1]
    if len(nodes) != _TERMINALS:
        return None
    key = callee.upper()
    if key in _BIPOLAR:
        we, le = _BIPOLAR[key]
        if le is None:
            micrometres = params.get("el", "")
            if not _PLAIN_NUMBER.fullmatch(micrometres):
                return None
            le = f"{micrometres}u"
        multiplicity = params.get("nx", "1")
        return " ".join(
            [
                f"Q{tokens[0]}",
                *nodes,
                callee,
                f"le={le}",
                f"we={we}",
                f"m={multiplicity}",
            ]
        )
    if key in _MOS and "w" in params and "l" in params:
        rest = [t for t in tokens[1:] if t.lower() != "params:"]
        return " ".join([f"M{tokens[0]}", *rest])
    return None
