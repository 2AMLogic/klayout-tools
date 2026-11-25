"""Native SG13G2 bipolar, MOS and passive reference cards for the
``klayout-runset`` engine (issues #7836, #7855).

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
* ``cap_cmim``: ``C<n> PLUS MINUS cap_cmim w= l= m=``
* ``rppd``/``rhigh``/``rsil``: ``R<n> A B SUB <model> w= l= m= [b=] [ps=]``

``El`` is in micrometres, so the symbol's ``El * 1e-6`` is the ``u`` suffix.

The passives are written with the generic conversion's ``A=``/``P=`` and
positional ``0`` value left out on purpose (issue #7855). ``CustomReader``
(``rule_decks/custom_reader.lvs``) maps a ``cap_cmim`` card's ``W``/``L`` onto
the device's first two parameters, and ``min_caps(1e-18)`` in ``sg13g2.lvs``
purges a capacitor whose first parameter is ``0``; a card without ``w``/``l``
reads as ``0`` and is purged before ``compare``. A resistor card with a
positional ``0`` after the model is read by ``rewrite_res_from_model_position``
with the model one field too far right, and the generic conversion also
dropped ``m``/``b``/``ps``, which the deck compares. The resistor form here is
the three-terminal ``R n1 n2 <sub> <model> <params>`` the deck documents and
the PDK's own ``res_devices`` CDL controls use; ``b`` and ``ps`` are carried
as written, never defaulted.

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

#: Passive callee (upper-cased) -> ``(card prefix, terminal count,
#: optional parameters carried when the call gives them)``. ``w``/``l`` are
#: required and ``m`` defaults to the symbol's ``1``; the optional names are
#: exactly the ones ``CustomReader#map_resistor_params`` reads and compares.
_PASSIVES: dict[str, tuple[str, int, tuple[str, ...]]] = {
    "CAP_CMIM": ("C", 2, ()),
    "RPPD": ("R", 3, ("b", "ps")),
    "RHIGH": ("R", 3, ("b", "ps")),
    "RSIL": ("R", 3, ("b", "ps")),
}

# Native model-last card shapes, including the RF devices.
PASSIVE_CARD_SHAPES = {
    **{name: (prefix, pins) for name, (prefix, pins, _) in _PASSIVES.items()},
    "INDUCTOR": ("L", 3),
    "INDUCTOR3": ("L", 4),
    "SG13_HV_SVARICAP": ("C", 4),
}

#: A bare micrometre value: a unit suffix would scale it a second time.
_PLAIN_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")

_TERMINALS = 4
_CDL_MODEL = re.compile(r"\$\[(.+)\]")


def sg13g2_model_name(name: object) -> str:
    """The upper-cased model name as the IHP reader sees it in native CDL."""
    text = str(name or "")
    wrapped = _CDL_MODEL.fullmatch(text)
    return (wrapped.group(1) if wrapped else text).upper()


def native_primitive_names() -> frozenset[str]:
    """Upper-cased callee names this module writes native cards for."""
    return frozenset(_BIPOLAR) | _MOS | frozenset(_PASSIVES)


def _passive_card(
    tokens: list[str], nodes: list[str], callee: str, params: dict[str, str]
) -> str | None:
    prefix, terminals, optional = _PASSIVES[callee.upper()]
    if len(nodes) != terminals or not params.get("w") or not params.get("l"):
        return None
    written = {
        "w": params["w"],
        "l": params["l"],
        "m": params.get("m", "1"),
        **{key: params[key] for key in optional if key in params},
    }
    return " ".join(
        [
            f"{prefix}{tokens[0]}",
            *nodes,
            callee,
            *(f"{key}={value}" for key, value in written.items()),
        ]
    )


def native_card(
    tokens: list[str], positional: list[str], params: dict[str, str]
) -> str | None:
    """The native card for the ``X`` call ``tokens``, or ``None`` when it
    cannot be written faithfully.

    ``positional`` is the call's nodes plus the callee, and ``params`` its
    ``key=value`` map with lower-cased keys (``_split_params``)."""
    nodes, callee = positional[:-1], positional[-1]
    key = callee.upper()
    if key in _PASSIVES:
        return _passive_card(tokens, nodes, callee, params)
    if len(nodes) != _TERMINALS:
        return None
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
