"""asap7 netlist adaptation for the pex extracted legs (issue #5128).

Lives outside ``pex.py`` so that file stays under its size ceiling.
"""

from __future__ import annotations

import os

#: ``extraction.netlist_adaptation`` value for the asap7 deck.
ASAP7_NETLIST_ADAPTATION = "asap7_bsimcmg.adapt_extracted_netlist"


def netlist_adaptation_for_deck(deck_name: str | None) -> str | None:
    """Name of the simulator adaptation applied to the extracted legs' DUT
    netlist for ``deck_name``, or ``None`` (no adaptation, sky130 etc.)."""
    return ASAP7_NETLIST_ADAPTATION if deck_name == "asap7" else None


def adapted_dut_netlist(
    deck_name: str | None, netlist_path: str, *, work_dir: str, label: str, side: str
) -> str:
    """The netlist a leg's DUT `.include` should name: ``netlist_path``
    itself, except for asap7 where the raw `M` cards are rewritten to OSDI
    BSIM-CMG `N` cards in a copy under ``work_dir``. The raw extraction file
    is never modified."""
    if netlist_adaptation_for_deck(deck_name) is None:
        return netlist_path
    from .asap7_bsimcmg import adapt_extracted_netlist
    from .pex import PexError

    with open(netlist_path, encoding="utf-8") as handle:
        text = handle.read()
    try:
        adapted = adapt_extracted_netlist(text)
    except ValueError as exc:
        raise PexError(
            f"cannot adapt the {side} netlist {netlist_path} for ngspice's "
            f"BSIM-CMG (OSDI) flow: {exc}"
        ) from exc
    adapted_path = os.path.join(work_dir, f"{label}.{side}-dut.asap7-osdi.spice")
    with open(adapted_path, "w", encoding="utf-8") as handle:
        handle.write(adapted)
    return adapted_path


#: Device-instance card prefixes whose trailing (non-node) token names a
#: model or `.subckt` -- see ``pex._device_model_counts``. Deliberately
#: excludes passive cards (`R`/`C`/`L`) and sources (`V`/`I`): those are out
#: of ``pex._model_mismatch``'s scope (issue #2402's own Acceptance
#: Criteria -- "device-instance-based ... on `M`/`X` cards or equivalent",
#: not every card that happens to carry a trailing token). `N` is the OSDI
#: BSIM-CMG card the asap7 adaptation emits (issue #5128).
DEVICE_MODEL_CARD_PREFIXES = "MXQDN"


def adapt_leg_netlists(
    deck_name: str | None,
    extracted: str,
    device_only: str | None,
    work_dir: str,
    *,
    skip: bool = False,
) -> tuple[str, str | None]:
    """The (extracted, device-only) netlists the simulated legs should
    include: the inputs themselves unless the deck needs adapting. Call after
    ``--tie-net`` rewrites so the copies carry the ties. ``skip`` (issue
    #5137: ``--extract-only`` / ``--measure-command`` never reach a sim leg)
    returns the inputs untouched without adapting."""
    if skip:
        return extracted, device_only
    return (
        adapted_dut_netlist(
            deck_name, extracted, work_dir=work_dir, label="dut", side="extracted"
        ),
        device_only
        and adapted_dut_netlist(
            deck_name, device_only, work_dir=work_dir, label="dut", side="device-only"
        ),
    )
