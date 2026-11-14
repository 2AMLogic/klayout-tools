"""Liberty unit declarations, as multipliers into the canonical units the
``klt characterize --compare-to`` comparison works in (issue #5063): time ->
ns, capacitance -> pF, energy -> pJ, leakage power -> pW.

Pure and dependency-free: it reads the ``attributes`` / ``complex_attributes``
of a parsed library group (see :mod:`klayout_tools.liberty_compare`) and
raises :class:`ValueError` on a missing or unparseable declaration; the caller
adds the file name. Units are never defaulted -- a silent default is how a
ps-vs-ns library gets compared a thousand times off.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Units:
    """Multipliers into the canonical units; ``declared`` echoes the raw
    declarations."""

    time: float
    capacitance: float
    energy: float
    leakage: float
    declared: dict[str, str]


_UNIT_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*([A-Za-z]+)\s*$")
_SI_PREFIX = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, "": 1.0}


def _scale(text: str | None, base: str, canonical: float) -> float | None:
    """``text`` (e.g. ``1ps``, ``1 nW``) in ``canonical`` base-units of
    ``base`` (``s``/``F``/``V``/``W``), else ``None`` (also if non-positive)."""
    match = _UNIT_RE.match(text) if text else None
    if not match or not match.group(2).lower().endswith(base.lower()):
        return None
    prefix = match.group(2)[: -len(base)].lower()
    if prefix not in _SI_PREFIX:
        return None
    value = float(match.group(1)) * _SI_PREFIX[prefix] / canonical
    return value if value > 0 else None


def _declared(library: Any, attribute: str, base: str, canonical: float):
    text = library.attributes.get(attribute)
    value = _scale(text, base, canonical)
    if value is None:
        raise ValueError(
            f"library has no {attribute} declaration"
            if text is None
            else f"unparseable {attribute} '{text}'"
        )
    return value, text.strip()


def library_units(library: Any) -> Units:
    """Read ``library``'s unit declarations; a missing or unparseable one
    raises :class:`ValueError`."""
    time, time_text = _declared(library, "time_unit", "s", 1e-9)
    voltage, voltage_text = _declared(library, "voltage_unit", "V", 1.0)
    leakage, leakage_text = _declared(library, "leakage_power_unit", "W", 1e-12)
    args = library.complex_attributes.get("capacitive_load_unit")
    if args is None:
        raise ValueError("library has no capacitive_load_unit declaration")
    # ``capacitive_load_unit(1, pf)`` or ``capacitive_load_unit("1pf")``.
    parts = " ".join(args).replace(",", " ").split()
    cap_text = ", ".join(args)
    cap = (
        _scale("".join(parts), "F", 1e-12)
        if len(parts) in (1, 2) and _UNIT_RE.match("".join(parts))
        else None
    )
    if cap is None:
        raise ValueError(f"unparseable capacitive_load_unit ({cap_text})")
    return Units(
        time=time,
        capacitance=cap,
        # voltage_unit^2 x capacitive_load_unit, in pJ (1 V^2 x 1 pF = 1 pJ).
        energy=voltage * voltage * cap,
        leakage=leakage,
        declared={
            "time_unit": time_text,
            "capacitive_load_unit": cap_text,
            "voltage_unit": voltage_text,
            "leakage_power_unit": leakage_text,
        },
    )
