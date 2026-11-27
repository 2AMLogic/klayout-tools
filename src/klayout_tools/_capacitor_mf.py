"""Capacitor ``mf``/``m`` alias handling for the netlist normalizer (#4951)."""

from __future__ import annotations


def capacitor_mf_is_m_alias(
    instance: str,
    subckt_name: str,
    params: dict[str, str],
    multiplier: int,
) -> bool:
    """Whether a capacitor's ``mf`` merely repeats its ``m`` (issue #4951).

    Schematic exporters emit ``MF=N m=N`` for one device (``m`` is the
    symbol's alias for ``mf``), so ``mf > 1`` equal to ``m`` is one
    multiplier, expanded once via ``m``. ``mf > 1`` with ``m`` absent or
    different is refused with a message naming the combination. Returns
    ``False`` when there is nothing to exclude (``mf`` absent or <= 1, or
    non-numeric -- left to :func:`.netlist_normalize._reject_multiplicity`).
    """
    from .netlist_normalize import NormalizeError

    if "mf" not in params:
        return False
    try:
        mf = float(params["mf"])
    except ValueError:
        return False
    if mf <= 1:
        return False
    if "m" in params and mf == multiplier:
        return True
    m_text = f"m={params['m']}" if "m" in params else "no m"
    raise NormalizeError(
        f"device '{instance}' (subcircuit '{subckt_name}'): capacitor "
        f"mf={params['mf']} with {m_text} is ambiguous; mf and m are the "
        "same multiplier, so drop one or make them equal (e.g. mf=2 m=2 "
        "expands to 2 parallel devices), or flatten the device in the "
        "schematic netlist"
    )
