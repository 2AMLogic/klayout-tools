"""Gate-level supply-net universe evidence for ``klt lvs`` (issue #2136).

Split out of :mod:`klayout_tools.lvs_mismatch` as a pure move -- the
``reference.form: "gate-level-verilog"`` supply-universe machinery
(:class:`_SupplyPinUniverse`, :func:`_net_name_aliases`,
:func:`_is_unverified_supply_correspondence`) is a cohesive,
low-coupling subsystem with no dependency on the rest of that module.
``lvs_mismatch`` re-imports every name defined here, so
``klayout_tools.lvs_mismatch.<name>`` stays a working import path for the
test suite and callers (the same discipline as the earlier
``lvs_mismatch_tolerance`` split).
"""

from __future__ import annotations

from typing import NamedTuple


class _SupplyPinUniverse(NamedTuple):
    """The two library-derived name sets :func:`_build_net_correspondence`
    needs to mark a supply-net correspondence as unverified (issue #2136).

    ``power_pin_names`` is the standard-cell library's own power/ground pin
    universe (``lvs.py``'s ``_gate_level_power_pin_names``);
    ``layout_supply_nets`` is the set of layout net names one of those pins
    demonstrably lands on (``lvs.py``'s ``_layout_supply_net_names``). Both
    are upper-cased, and both are derived -- never a hardcoded per-PDK name
    table or a ``V*`` glob. Built only for a ``reference.form:
    "gate-level-verilog"`` run with resolvable library pin orders; ``None``
    everywhere else.
    """

    power_pin_names: frozenset[str]
    layout_supply_nets: frozenset[str]


def _net_name_aliases(name: str | None) -> frozenset[str]:
    """``name`` upper-cased, plus each of its ``|``-separated aliases.

    A label-merged net is reported as ``VPWR|VDD`` (see ``_name_or_none``),
    and which alias a given lookup table happens to hold is not something
    either side controls -- so membership tests in this module consider the
    joined spelling *and* every alias it joins.
    """
    if not name:
        return frozenset()
    return frozenset(
        {name.upper(), *(alias.upper() for alias in name.split("|") if alias)}
    )


def _is_unverified_supply_correspondence(
    layout_name: str | None,
    reference_name: str | None,
    universe: _SupplyPinUniverse | None,
) -> bool:
    """True when this pairing puts a layout **supply** net opposite a
    reference net that is not itself a supply pin (issue #2136).

    A ``reference.form: "gate-level-verilog"`` reference carries no
    power/ground pins at all (see ``verilog_netlist.py``'s own "No
    power/ground pins" note), so the comparer never has a same-named
    candidate for the layout's ``VGND``/``VPWR`` and pairs it with whatever
    its graph heuristics reach first -- routinely an unrelated signal net.
    That pairing is reported as an ordinary match today, indistinguishable
    from one the compare actually verified.

    The reference-side half of the test is not redundant: it is what keeps
    a genuine supply-to-supply pairing (a reference that *does* declare the
    supply pin -- possible for a hand-written stub, and the shape every
    non-``gate-level-verilog`` form has) out of the flag.
    """
    if universe is None:
        return False
    if not (_net_name_aliases(layout_name) & universe.layout_supply_nets):
        return False
    return not (_net_name_aliases(reference_name) & universe.power_pin_names)
