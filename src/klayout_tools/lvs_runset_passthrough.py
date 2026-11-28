"""Report DEVICE_LIST calls left for the runset's own SPICE reader."""

from __future__ import annotations

from typing import Any, NamedTuple

from .lvs_base import CATEGORY_REFERENCE_UNRESOLVED_SUBCKT_CALL
from .lvs_mismatch import _mismatch
from .lvs_replay import _reference_conversion_echo


class Passthrough(NamedTuple):
    """The ``X`` cards the conversion left for the runset's own reader
    (issue #5796): the callee names as written (sorted, unique) and the
    number of cards hidden."""

    callees: list[str]
    cards: int


def reference_conversion_echo(
    ref_spec: dict[str, Any], passthrough: Passthrough
) -> dict[str, Any]:
    """The report's ``reference_conversion`` echo. For ``subckt-call`` it adds
    ``passthrough_callees`` / ``passthrough_cards`` (issue #5796; empty/zero
    when nothing passed through), because the request echo alone cannot say
    whether the deck's ``DEVICE_LIST`` hid any card from the conversion."""
    form = ref_spec.get("form", "plain-element")
    echo = _reference_conversion_echo(form, ref_spec)
    if form == "subckt-call":
        echo["reference_conversion"].update(
            passthrough_callees=passthrough.callees,
            passthrough_cards=passthrough.cards,
        )
    return echo


def passthrough_rerun_excludes(committed: dict[str, Any]) -> set[tuple[str, ...]]:
    """``--rerun`` drift paths to ignore: a ``subckt-call`` report committed
    before issue #5796 has no ``passthrough_*`` keys, and the richer echo of
    the current build is not drift."""
    echoed = committed.get("reference_conversion")
    echoed = echoed if isinstance(echoed, dict) else {}
    return {
        ("reference_conversion", key)
        for key in ("passthrough_callees", "passthrough_cards")
        if key not in echoed
    }


def passthrough_finding(
    passthrough: Passthrough, verdict: dict[str, Any]
) -> list[dict[str, Any]]:
    """Issue #5796: one ``reference.unresolved_subckt_call`` finding when
    passthrough hid ``X`` cards and the runset's compare recognized **no**
    reference device -- the cards vanished from the reference without a trace
    (``_unresolved_reference_calls`` excludes ``DEVICE_LIST`` callees).

    Aggregate on purpose: the runset's reader renames devices (sky130
    ``special_nfet_01v8`` -> ``nfet_01v8``), so a callee cannot be matched to
    a produced class. A reference with any recognized device (curated cards
    converted, or the runset paired the passed-through ones, issue #5402)
    stays quiet."""
    recognized = verdict["coverage"]["recognized_device_classes"]["reference"]
    if not passthrough.cards or recognized:
        return []
    names = ", ".join(repr(c) for c in passthrough.callees)
    return [
        _mismatch(
            CATEGORY_REFERENCE_UNRESOLVED_SUBCKT_CALL,
            "error",
            f"{passthrough.cards} reference card(s) calling {names} were passed "
            "through to the runset's own reader (the deck's DEVICE_LIST names "
            "them and no reference.deck/reference.device_map binding claims "
            "them), but the runset recognized no reference device -- the "
            "cards were never compared. Pass reference.deck or "
            "reference.device_map to convert them, or write them as "
            'plain-element cards (see docs/cli/lvs.md, "klayout-runset")',
            "reference",
            details={
                "callees": passthrough.callees,
                "passthrough_cards": passthrough.cards,
            },
        )
    ]
