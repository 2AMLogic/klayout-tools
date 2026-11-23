"""Deck-declared LVS defaults for ``klt lvs`` (issue #4312).

A PDK's own LVS deck can leave a device parameter out of its compare for good
(KLayout's ``ignore_parameter``): the gf180mcu eFuse's link resistance ``R``,
which the PDK's reference netlist states as a nominal value the drawn fuse is
not expected to reproduce. A deck carries that as
:attr:`~klayout_tools.decks.ResistorDevice.ignored_parameters`, and this module
applies it to the comparison *by default*, so a plain ``klt lvs`` of the PDK's
own cell against the PDK's own netlist neither depends on the nominal value nor
asks the caller to name parameters the reference never states.

The request stays in charge: a device class named in
``options.compare_parameters`` is left entirely to that option, which already
decides every parameter of the class (``options.compare_parameters``'
``["R"]`` compares the fuse's resistance after all).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Set
from typing import Any

from .lvs_mismatch import _mismatch

#: Same string as ``lvs.CATEGORY_DEVICE_PARAMETER_EXCLUDED`` (a test pins them
#: equal): the disclosure is the same finding, whichever hook excluded it.
CATEGORY_DEVICE_PARAMETER_EXCLUDED = "device.parameter_excluded"


def _disable(
    device_class: Any,
    ignored: set[str],
    disclosed: Set[tuple[str, str]],
    excluded: set[tuple[str, str]],
    narrowed: set[tuple[str, str]],
) -> None:
    """Turn ``ignored`` off on ``device_class`` and record each parameter as
    excluded -- and as *narrowed* when it was primary, i.e. really in the
    compare (see ``lvs._record_excluded_parameters``). A parameter another hook
    already took out and disclosed (``disclosed``) is left to that disclosure."""
    key = device_class.name.lower()
    for param in device_class.parameter_definitions():
        lowered = param.name.lower()
        if lowered not in ignored or (key, lowered) in disclosed:
            continue
        if param.is_primary:
            narrowed.add((key, lowered))
        excluded.add((key, lowered))
        device_class.enable_parameter(param.name, False)


def _compared(classes: Iterable[Any]) -> list[str]:
    """The parameters still primary on either side after the exclusion."""
    return sorted(
        {
            param.name
            for device_class in classes
            for param in device_class.parameter_definitions()
            if param.is_primary
        }
    )


def apply_deck_ignored_parameters(
    deck: Any,
    compare_parameters: Mapping[str, Any] | None,
    layout_netlist: Any,
    reference_netlist: Any,
    disclosed_parameters: Set[tuple[str, str]],
    excluded_parameters: set[tuple[str, str]],
    narrowed_parameters: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Leave every deck-ignored parameter out of the compare, on **both** sides.

    ``deck`` is the layout side's extraction deck (``None`` when the layout
    arrived as a netlist, which declares nothing); ``disclosed_parameters`` the
    ``(class, parameter)`` pairs an earlier hook (the placeholder ``R`` of a
    ``subckt-call`` reference) already excluded and disclosed. Run before
    ``_apply_compare_parameters``, for the same reason it does: both call
    ``DeviceClass.enable_parameter``, which has to precede the comparer. The two
    sides' classes are distinct objects, and ``NetlistComparer`` consults each
    device's own, so each is switched off. A class absent from both netlists is
    skipped, not an error: the deck declares the fuse whether or not the design
    draws one.

    Returns one ``severity: "warning"`` :data:`CATEGORY_DEVICE_PARAMETER_EXCLUDED`
    entry per parameter this actually took out of the compare (``side:
    "both"``), carrying ``details.source: "deck"`` so it is told apart from the
    ``options.compare_parameters`` one. A parameter the class already declared
    secondary was never compared and gets none -- the same "narrowed away, not
    unnamed" rule ``options.compare_parameters`` follows.
    """
    named = {name.lower() for name in compare_parameters or {}}
    entries: list[dict[str, Any]] = []
    for resistor in getattr(deck, "resistors", ()):
        if not resistor.ignored_parameters or resistor.name.lower() in named:
            continue
        ignored = {name.lower() for name in resistor.ignored_parameters}
        classes = [
            device_class
            for netlist in (layout_netlist, reference_netlist)
            for device_class in netlist.each_device_class()
            if device_class.name.lower() == resistor.name.lower()
        ]
        before: set[tuple[str, str]] = set()
        for device_class in classes:
            _disable(
                device_class, ignored, disclosed_parameters, excluded_parameters, before
            )
        narrowed_parameters |= before
        compared = _compared(classes)
        for param in sorted({name for _, name in before}):
            entries.append(
                _mismatch(
                    CATEGORY_DEVICE_PARAMETER_EXCLUDED,
                    "warning",
                    f"the layout deck leaves '{param.upper()}' of device class "
                    f"'{resistor.name}' out of the compare, as the PDK's own LVS "
                    "deck does (ignore_parameter) -- it was excluded from this "
                    "compare and is NOT verified; a 'match' does not confirm the "
                    "two sides agree on it. Name the class in "
                    "options.compare_parameters to compare it (see "
                    "docs/cli/lvs.md, 'device.parameter_excluded')",
                    "both",
                    device={
                        "layout": None,
                        "reference": None,
                        "class": resistor.name,
                    },
                    details={
                        "parameter": param.upper(),
                        "compared_parameters": compared,
                        "source": "deck",
                    },
                )
            )
    return entries
