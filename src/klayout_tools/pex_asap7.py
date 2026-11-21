"""asap7 netlist adaptation for the pex extracted legs (issue #5128).

Lives outside ``pex.py`` so that file stays under its size ceiling.
"""

from __future__ import annotations

import os
import re

#: ``extraction.netlist_adaptation`` value for the asap7 deck.
ASAP7_NETLIST_ADAPTATION = "asap7_bsimcmg.adapt_extracted_netlist"


#: Covered range of the phase-2 measurement (issue #5202, table in
#: ``docs/design/asap7-parallel-line-fidelity.md``): 12 points, none within
#: 10%. Run length was never varied, so no threshold exists and nothing
#: outside this range is claimed (issue #5203).
ASAP7_FIDELITY_DOC = "docs/design/asap7-parallel-line-fidelity.md"
ASAP7_COVERED_RANGE = (
    "ASAP7 M2 and M4, pitch 1x-4x the LEF pitch, same-level parallel "
    "neighbours, 0.36 um run, 10 kohm driver, 1 ps edge"
)
#: Measured error range, min to max over the 12 points (quiet and
#: opposite-switching), in percent.
ASAP7_DELAY_ERROR_PCT = (-97.8, -87.8)
ASAP7_ENERGY_ERROR_PCT = (-99.0, -92.6)


def fidelity_warnings_for_deck(deck_name: str | None) -> list[str]:
    """Run-level ``warnings[]`` strings for ``deck_name`` (issue #5203): one
    for asap7 stating the measured coupled-wiring fidelity gap, none for any
    other deck. A statement about the deck, not a per-net classification."""
    if deck_name != "asap7":
        return []
    d_lo, d_hi = ASAP7_DELAY_ERROR_PCT
    e_lo, e_hi = ASAP7_ENERGY_ERROR_PCT
    return [
        "asap7 extraction fidelity: on same-level coupled wiring within the "
        f"measured range ({ASAP7_COVERED_RANGE}) the deck's --parasitics "
        f"delay error is {d_lo}% to {d_hi}% and energy error is {e_lo}% to "
        f"{e_hi}% versus the klt mom reference. Treat the extracted delay "
        "and energy as a lower bound. Nothing outside that range was "
        f"measured. See {ASAP7_FIDELITY_DOC}."
    ]


def with_fidelity_warnings(result: dict, deck_name: str | None) -> dict:
    """Return ``result`` with the additive ``warnings: array<string>`` field
    (issue #5203) set from ``fidelity_warnings_for_deck`` -- present only when
    non-empty, so every non-asap7 report stays byte-identical."""
    warnings = fidelity_warnings_for_deck(deck_name)
    if warnings:
        result["warnings"] = warnings
    return result


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


#: A ``D`` device card's KLayout-spelled instance parameter (``A=``/``P=``).
_DIODE_PARAM = re.compile(r"(?<!\S)([AaPp])=(\S+)")


def ngspice_diode_cards(text: str) -> str:
    """``text`` with every ``D`` card's ``A=``/``P=`` rewritten to ngspice's
    ``area=``/``pj=`` (issue #5178; ngspice's D element rejects the KLayout
    spellings with ``unknown parameter (a)``). Values are kept, lower-cased
    (``178P`` -> ``178p``). Comments, directives and non-``D`` cards are
    untouched."""

    def spell(match: re.Match[str]) -> str:
        name = "area" if match.group(1).lower() == "a" else "pj"
        return f"{name}={match.group(2).lower()}"

    return "".join(
        _DIODE_PARAM.sub(spell, line) if line[:1] in "Dd" else line
        for line in text.splitlines(keepends=True)
    )


def diode_adapted_netlist(
    netlist_path: str, *, work_dir: str, label: str, side: str
) -> str:
    """``netlist_path`` itself, or -- when it holds a ``D`` card ngspice would
    reject -- a rewritten copy under ``work_dir``. Never edits the raw
    extraction: KLayout's SPICE reader (``klt lvs``) reads ``area=``/``pj=``
    back as 0, so only the pex-facing copy may carry ngspice's spelling."""
    with open(netlist_path, encoding="utf-8") as handle:
        text = handle.read()
    adapted = ngspice_diode_cards(text)
    if adapted == text:
        return netlist_path
    adapted_path = os.path.join(work_dir, f"{label}.{side}-dut.ngspice-diode.spice")
    with open(adapted_path, "w", encoding="utf-8") as handle:
        handle.write(adapted)
    return adapted_path


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

    def adapt(path: str, side: str) -> str:
        path = adapted_dut_netlist(
            deck_name, path, work_dir=work_dir, label="dut", side=side
        )
        return diode_adapted_netlist(path, work_dir=work_dir, label="dut", side=side)

    return adapt(extracted, "extracted"), device_only and adapt(
        device_only, "device-only"
    )
