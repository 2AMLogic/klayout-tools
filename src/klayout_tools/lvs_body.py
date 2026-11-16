"""The ``klt lvs`` MOS body-verification determination (issues #281, #1983,
#5749).

Split out of ``lvs_mismatch.py``/``lvs_power.py`` (issue #5749) -- both
debt-baselined past growth -- so the whole determination lives in one place,
which is where its one-source-of-truth invariant actually lives:
:func:`_body_net_warnings` renders the determination as the
``device.body_unverified`` ``mismatches[]`` prose and
:func:`_body_verification_report` renders the same counts as the
machine-checkable ``body_verification`` block (issue #1983) -- so the warning
a reader sees and the field a grader reads can never disagree.

Issue #5749 widened the determination along two axes:

- **It now tallies what it inspected, not only what failed.** The pre-#5749
  determination returned only the unverified counts, so "every eligible body
  resolved" and "no device was ever eligible" both rendered as
  ``status: "verified"`` with ``device_count: 0`` -- a vacuous pass
  indistinguishable from a passed check. The determination now returns
  *both* tallies, and the report carries ``inspected_count`` in every branch;
  zero eligible devices is ``unchecked`` with a reason naming why, never
  ``verified``.
- **It sees flavoured classes.** With ``options.compare_mos_flavour`` on,
  :func:`~klayout_tools.lvs_mos_options.split_layout_mos_flavours` has
  already moved every flavoured device onto a ``{base}__{flavour}`` class
  (``nfet`` -> ``nfet__nvt``) *before* the body check runs -- so selecting
  devices by exact equality with the deck's generic ``nfet_class``/
  ``pfet_class`` skipped every flavoured device while the report still read
  ``verified``. :func:`_device_deck_arm` classifies a flavoured name onto its
  deck arm through the same separator
  :func:`~klayout_tools.pdk_models.flavoured_mos_class_name` builds the
  names with, so the layout split and this check cannot drift.

Dependency surface mirrors ``lvs_mismatch.py``'s split discipline: this
module imports ``_mismatch`` from ``lvs_mismatch`` at module scope (no cycle
-- ``lvs_mismatch`` never imports this module), and everything defined in
``lvs.py`` (``CATEGORY_DEVICE_BODY_UNVERIFIED``, the ``BODY_STATUS_*``
constants) is imported *inside* the functions that use it, so ``lvs.py``
remains free to import this module at its own module scope.
"""

from __future__ import annotations

from typing import Any

from .lvs_mismatch import _mismatch
from .pdk_models import flavoured_mos_class_name

# The ``{base}__{flavour}`` separator, derived from the same function that
# builds the flavoured class names (issue #5749) rather than hardcoded, so
# the layout-side split and this check cannot disagree about the spelling.
_FLAVOUR_SEPARATOR = flavoured_mos_class_name("", "")


def _device_body_net_name(device: Any) -> str | None:
    """The expanded name of ``device``'s body/bulk terminal net (KLayout's
    ``DeviceExtractorMOS4Transistor`` names it ``"B"`` -- the same
    ``terminal.name.lower()`` convention ``extract.py``'s ``nets[]`` uses),
    or ``None`` if the device class declares no such terminal, or the
    terminal reaches no net at all."""
    device_class = device.device_class()
    for terminal in device_class.terminal_definitions():
        if terminal.name.lower() == "b":
            net = device.net_for_terminal(terminal.id())
            return net.expanded_name() if net is not None else None
    return None


#: KLayout's own placeholder spelling for a net that carries no drawn label:
#: ``Net.expanded_name()`` returns ``"$<n>"`` (``"$0"``, ``"$5"``, ...).
#:
#: **Raw, not backslash-escaped -- the two spellings are not interchangeable
#: (issue #2048).** ``extract_report.py``'s ``_ANONYMOUS_NET_PREFIX`` is
#: ``"\\$"`` because every net name *that* module reports has already
#: passed through :func:`~klayout_tools.extract.spice_safe_net_name`, which
#: escapes the leading ``$`` to match the written netlist's own node
#: spelling (issue #1162). Nothing on *this* module's path does that
#: escaping: :func:`_device_body_net_name` reads ``Net.expanded_name()``
#: straight off the in-memory netlist, so the prefix to match here is the
#: bare ``"$"``. Reusing ``extract_report.py``'s escaped constant here would
#: silently never match any real body net.
#:
#: **A netlist read back from disk spells it escaped (issue #3177).** For the
#: ``layout.netlist`` + ``layout.deck`` shape the circuit comes from
#: ``NetlistSpiceReader`` over a ``klt extract -o`` file, whose anonymous nodes
#: are written ``\\$<n>`` -- so :func:`_is_unresolved_body_net` accepts that
#: spelling too (:data:`_ESCAPED_ANONYMOUS_NET_PREFIX`).
_RAW_ANONYMOUS_NET_PREFIX = "$"
_ESCAPED_ANONYMOUS_NET_PREFIX = "\\$"


def _is_unresolved_body_net(name: str | None) -> bool:
    """Whether a body terminal's net name (as returned by
    :func:`_device_body_net_name`) fails to identify a real, drawn- or
    derived-tap net: either KLayout synthesized an anonymous placeholder for
    it (``"$<n>"`` -- no label reached the net, so nothing biases the body),
    or the terminal reached no net at all (``None``).

    The in-memory counterpart of ``extract.py``'s
    :func:`~klayout_tools.extract._detect_unbiased_pmos_body_nets`, which
    makes the same per-device determination against the already-escaped
    ``"\\$<n>"`` spelling it reports in ``klt extract``/``klt pex`` JSON --
    see :data:`_RAW_ANONYMOUS_NET_PREFIX` for why the prefix differs between
    the two paths even though the condition is identical.
    """
    return name is None or name.startswith(
        (_RAW_ANONYMOUS_NET_PREFIX, _ESCAPED_ANONYMOUS_NET_PREFIX)
    )


def _device_deck_arm(device_class: str, deck: Any) -> str | None:
    """Which deck MOS arm ``device_class`` belongs to (issue #5749):
    ``"nfet"`` when the name casefold-equals ``deck.nfet_class`` or starts
    with that base plus the flavoured-class separator (``nfet__nvt`` --
    :func:`~klayout_tools.pdk_models.flavoured_mos_class_name`'s own
    spelling, which
    :func:`~klayout_tools.lvs_mos_options.split_layout_mos_flavours` has
    already renamed the layout's flavoured devices to by the time the body
    check runs), likewise ``"pfet"``, else ``None``. Case-insensitive
    throughout, for the same issue-#3177 read-back reason as
    :func:`_body_inspected_and_unverified_counts`.
    """
    lowered = device_class.casefold()
    for arm, base in (("nfet", deck.nfet_class), ("pfet", deck.pfet_class)):
        base = base.casefold()
        if lowered == base or lowered.startswith(base + _FLAVOUR_SEPARATOR):
            return arm
    return None


def _body_class_report_name(device_class: str, deck: Any) -> str:
    """The class name a report names for ``device_class``: the deck's own
    spelling for a generic-arm device (so an upper-cased netlist read back
    from disk still reports ``nfet``, not ``NFET`` -- issue #3177), the
    extracted name for a flavoured one (issue #5749: ``device_classes``
    names what was really found, ``nfet__nvt``, not ``nfet``)."""
    for base in (deck.nfet_class, deck.pfet_class):
        if device_class.casefold() == base.casefold():
            return base
    return device_class


def _body_inspected_and_unverified_counts(
    layout_circuit: Any, deck: Any
) -> tuple[dict[str, int], dict[str, int]]:
    """``({class: inspected}, {class: unverified})`` for every MOS device on
    a deck arm (``nfet``/``pfet``, generic or flavoured -- see
    :func:`_device_deck_arm`) in ``layout_circuit``, keyed for the report by
    :func:`_body_class_report_name` (issue #5749; before that issue the
    determination returned only the unverified tally and selected devices by
    exact equality with the deck's two generic class names, so a
    ``compare_mos_flavour`` run inspected nothing yet still read
    ``verified``).

    The single source of truth for the determination:
    :func:`_body_net_warnings` renders the unverified counts as its
    ``device.body_unverified`` ``mismatches[]`` prose, and
    :func:`_body_verification_report` renders *both* tallies as the
    machine-checkable ``body_verification`` block (issue #1983) -- so the
    warning a reader sees and the field a grader reads can never disagree.
    See :func:`_body_net_warnings` for the full rationale on *which* devices
    count as unverified on which decks.

    The unverified map is empty when every MOS body terminal resolved to a
    real net (a layout that drew its substrate/well ties on a deck that has
    a tap mechanism); the inspected map is empty when the circuit had no
    eligible device at all -- the vacuous case issue #5749 stops rendering
    as ``verified``.
    """
    inspected: dict[str, int] = {}
    unverified: dict[str, int] = {}

    # Issue #3177: a netlist read back from disk (``layout.netlist`` +
    # ``layout.deck``) presents class and net names upper-cased
    # (``NFET``/``VSUBS``), so every name compare is case-insensitive --
    # including the flavoured-prefix match :func:`_device_deck_arm` makes.
    substrate_net = deck.substrate_net.casefold()
    for device in layout_circuit.each_device():
        device_class = device.device_class().name
        arm = _device_deck_arm(device_class, deck)
        if arm is None:
            continue
        name = _body_class_report_name(device_class, deck)
        inspected[name] = inspected.get(name, 0) + 1
        body = _device_body_net_name(device)
        if arm == "nfet":
            # A body on the deck-synthesized substrate global (or on no net
            # at all) was never tied by drawn geometry.
            if body is None or body.casefold() == substrate_net:
                unverified[name] = unverified.get(name, 0) + 1
        # Issue #2048: per-device, not deck-structural. A deck that merely
        # *declares* a tap mechanism does not guarantee any individual PMOS
        # body reached a real net -- so ask each device, exactly as `klt
        # extract`'s `_detect_unbiased_pmos_body_nets` already does.
        elif _is_unresolved_body_net(body):
            unverified[name] = unverified.get(name, 0) + 1

    return inspected, unverified


def _body_net_warnings(layout_circuit: Any, deck: Any) -> list[dict[str, Any]]:
    """Issue #281 (narrowed to real-tap-drawn layouts by #490): flag, as
    non-blocking ``severity: "warning"`` entries, the MOS body terminals
    that ``extract.py``'s inline extraction ties to a deck-synthesized net
    rather than deriving from real drawn tap/well-label geometry -- so a
    caller recording a clean ``klt lvs`` verdict can also record that this
    dimension went structurally unverified (see this module's own docstring
    reference, ``extract.py``'s ``nfet_body``/``connect_global`` handling,
    and ``docs/cli/extract.md`` -> "Coverage").

    Per-device, not deck-structural (#490): a deck that declares a distinct
    ``tap`` layer (e.g. sky130's ``tap=(65, 44)``) resolves an NMOS body
    terminal to a real, named net when a layout draws a substrate-tie ring
    outside every ``nwell`` and contacts it up to that net -- only a device
    whose body terminal *still* reaches the deck's synthesized
    ``substrate_net`` global (no ring drawn, or no tap mechanism at all,
    e.g. gf180mcu before issue #1084) is structurally unverified. The NMOS
    warning therefore counts only devices whose body net name equals
    ``deck.substrate_net`` (or resolves to no net at all), not every NMOS
    device.

    **The PMOS arm is per-device too, since issue #2048.** It used to be
    deck-structural: it fired only when the deck had no tap mechanism at all
    (``deck.tap``/``tap_nplus``/``tap_pplus`` all ``None``, gf180mcu before
    issue #1084), treating a deck that merely *declares* one as sufficient
    for every PMOS in every layout it extracts. It is not: a gf180mcu layout
    whose declared ``tap_nplus``/``tap_pplus`` implant layers draw no real
    well-tie shape still resolves each such PMOS body to an anonymous,
    KLayout-synthesized ``"$<n>"`` net with no DC bias path -- and that went
    unflagged here while ``klt pex``'s ``body_bias`` block, built from
    ``extract.py``'s per-device
    :func:`~klayout_tools.extract._detect_unbiased_pmos_body_nets`, reported
    the very same layout as ``"unbiased"``. The PMOS warning now counts
    exactly the devices whose body terminal reached such a placeholder net
    (or no net at all); see :func:`_is_unresolved_body_net`, and
    :data:`_RAW_ANONYMOUS_NET_PREFIX` for why this in-memory path matches a
    raw ``$`` where ``extract.py`` matches an escaped ``\\$``. A deck with a
    real well-tap mechanism (sky130's drawn ``tap``/``well_label``) still
    emits no PMOS warning on a well-labelled layout -- but now because each
    PMOS body demonstrably landed on a named net (e.g. ``VPB``), not because
    the deck declared a mechanism.

    Since issue #5749 there is one warning **per unverified device class**
    (still one per class in practice on decks without flavours, where only
    the two generic classes can appear): a flavoured class
    (``nfet__nvt``-style) is its own entry, naming the class the
    ``body_verification`` block's ``findings[]`` names, so the prose and the
    block keep agreeing on class names and counts now that flavoured names
    can be keys (aggregating through :func:`_device_deck_arm` -- the
    pre-#5749 ``counts.get(deck.nfet_class, 0)`` lookup would silently read
    ``0`` for a flavoured-only class and fire no warning at all).

    Neither warning fires for the pre-extracted ``layout.netlist`` request
    form *without* a ``layout.deck`` -- callers only reach this helper when a
    deck was resolved, mirroring how ``device_classes``/``provenance.deck``
    are conditioned on that same distinction in ``run_lvs``. With
    ``layout.netlist`` + ``layout.deck`` it does run, against the SPICE read
    back from disk: names there are upper-cased and anonymous nets are
    spelled ``\\$<n>``, so the compares are case-insensitive and accept both
    anonymous spellings (issue #3177).

    Counts only the layout's top circuit's own devices (``each_device()``,
    not recursive), matching ``run_lvs``'s own ``counts.devices.layout``
    convention -- curated-deck extraction never nests MOS devices inside a
    subcircuit.
    """
    from .lvs import CATEGORY_DEVICE_BODY_UNVERIFIED

    entries: list[dict[str, Any]] = []

    # Issue #1983: both this prose warning and the machine-checkable
    # `body_verification` block are rendered from one determination, so a
    # reader and a grader can never see different answers.
    _, unverified = _body_inspected_and_unverified_counts(layout_circuit, deck)

    for device_class in sorted(unverified):
        count = unverified[device_class]
        if _device_deck_arm(device_class, deck) == "nfet":
            description = (
                f"{count} NMOS device body terminal(s) were compared "
                f"against the '{deck.substrate_net}' deck-synthesized "
                "substrate net, not a real schematic net -- either no "
                "substrate-tap geometry is drawn, or the drawn tap's net "
                "carries no name label (a body resolves only to a *named* "
                "net; label the tap-connected conductor, e.g. VSS, to "
                'resolve it; see docs/cli/extract.md, "Coverage")'
            )
        else:
            description = (
                f"{count} PMOS device body terminal(s) were "
                "compared against an anonymous, KLayout-synthesized well "
                "net, not a real schematic net -- either no drawn or "
                "derived well-tap geometry exists, or the tap-connected net "
                "carries no name label (a body resolves only to a *named* "
                "net; label the tap-connected conductor, e.g. VDD, to "
                'resolve it; see docs/cli/extract.md, "Coverage")'
            )
        entries.append(
            _mismatch(
                CATEGORY_DEVICE_BODY_UNVERIFIED,
                "warning",
                description,
                "layout",
                device={"layout": None, "reference": None, "class": device_class},
            )
        )

    return entries


def _body_verification_unchecked(reason: str) -> dict[str, Any]:
    """A ``body_verification`` block for a run that could not determine how
    the layout's MOS body terminals were resolved (issue #1983), stating
    *why* in ``reason``.

    Always emitted -- including for the pre-extracted ``layout.netlist``
    request form this determination does not apply to -- so the question
    "did this run verify that the device bodies are tied?" is answerable
    from any ``klt lvs`` report on its own, rather than requiring a reader
    to know which ``layout`` request shape produced it and infer the answer
    from the *absence* of a ``device.body_unverified`` warning. That
    absence is exactly the ambiguity issue #1983 reported: on a
    pre-extracted netlist it means "not checked", and on an inline
    extraction it means "checked and clean", and nothing in the report
    distinguished them. Same discipline as
    :func:`_power_connectivity_unchecked`.

    Since issue #5749 the zero-eligible-devices case renders here too (see
    :func:`_body_verification_report`): inspecting nothing is also "this run
    verified nothing about the bodies", so it must not read ``verified``.
    ``inspected_count`` is ``0`` in both -- the tally this branch exists to
    make explicit.
    """

    from .lvs import BODY_STATUS_UNCHECKED

    return {
        "status": BODY_STATUS_UNCHECKED,
        "reason": reason,
        "device_classes": [],
        "device_count": 0,
        "inspected_count": 0,
        "findings": [],
        "finding_count": 0,
    }


def _body_verification_report(layout_circuit: Any, deck: Any) -> dict[str, Any]:
    """The ``body_verification`` block for an inline-extraction compare
    (issue #1983) -- the machine-checkable form of the
    ``device.body_unverified`` warning (issue #281).

    Rendered from :func:`_body_inspected_and_unverified_counts`, the *same*
    determination :func:`_body_net_warnings` renders its prose
    ``mismatches[]`` entries from, so the warning a human reads and the
    field a grader reads can never disagree about how many devices are
    affected or which classes they belong to.

    Why this needs to exist at all, given the warning already did: a
    ``mismatches[]`` entry is not gradeable. Answering "were this layout's
    device bodies verifiably tied?" from a committed ``klt lvs`` record meant
    string-matching a ``category`` inside an array whose other entries are
    ordinary compare findings -- so in practice nothing downstream asked, and
    a record carrying the warning was indistinguishable from a clean one at
    every consumer that only reads ``status`` (``klt signoff`` included). See
    ``docs/cli/lvs.md`` -> ``body_verification``.

    **Branch order (issue #5749).** Zero inspected devices ->
    ``BODY_STATUS_UNCHECKED`` with a reason naming the deck's MOS class
    names and that nothing matched them; else any unverified ->
    ``BODY_STATUS_UNVERIFIED``; else ``BODY_STATUS_VERIFIED``. Before
    #5749 the first and third branches were one: the determination counted
    only failures, so "inspected nothing" and "inspected everything, all
    resolved" both rendered ``verified`` with ``device_count: 0`` --
    indistinguishable at every status-only consumer. ``inspected_count``
    makes ``verified`` self-evidencing in every branch.

    **This does not change ``status``.** A layout with unverified bodies
    still reports ``status: "match"`` when the compare matched, and the
    warning entries are still ``severity: "warning"`` with
    ``mismatch_count`` counting them -- unchanged from before this block
    existed. The block makes the condition *visible to a grader*; whether it
    should also block a verdict is a separate policy question, deliberately
    left to the consumer (see ``docs/design-evidence-tiers.md`` item 7).
    """

    from .lvs import (
        BODY_STATUS_UNVERIFIED,
        BODY_STATUS_VERIFIED,
    )

    inspected, unverified = _body_inspected_and_unverified_counts(layout_circuit, deck)
    inspected_count = sum(inspected.values())
    if not inspected:
        return _body_verification_unchecked(
            f"no MOS device was eligible for the body check: the layout's "
            f"top circuit extracted no '{deck.nfet_class}' or "
            f"'{deck.pfet_class}' device, under either the deck's generic "
            "class names or their compare_mos_flavour-suffixed variants "
            f"(e.g. '{deck.nfet_class}__<flavour>'), so there was no body "
            "terminal to inspect and this run verified nothing about the "
            "device bodies"
        )
    if not unverified:
        return {
            "status": BODY_STATUS_VERIFIED,
            "reason": None,
            "device_classes": [],
            "device_count": 0,
            "inspected_count": inspected_count,
            "findings": [],
            "finding_count": 0,
        }
    findings = [
        {"class": device_class, "device_count": count}
        for device_class, count in sorted(unverified.items())
    ]
    return {
        "status": BODY_STATUS_UNVERIFIED,
        "reason": None,
        "device_classes": sorted(unverified),
        "device_count": sum(unverified.values()),
        "inspected_count": inspected_count,
        "findings": findings,
        "finding_count": len(findings),
    }
