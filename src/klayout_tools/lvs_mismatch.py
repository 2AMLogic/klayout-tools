"""Mismatch-classification and tolerance subsystem for ``klt lvs``.

Split out of ``lvs.py`` (issue #1721) as a self-contained,
mismatch-classification-and-tolerance subsystem -- net/device mismatch
construction (:func:`_build_net_correspondence`, :func:`_make_compare_logger`,
:func:`_mismatch`, :func:`_build_mismatches`), same-nets hint reconciliation
(:class:`_SameNetsHintOutcomes`, :func:`_same_nets_hint_outcomes`), parameter
mismatch + tolerance handling (:func:`_classify_param_mismatch`,
:func:`_parse_parameter_tolerance`, :func:`_collect_tolerance_snaps`,
:func:`_apply_tolerance_snaps`), and net-mismatch pooling/classification
(:class:`_UnionFind`, :func:`_net_mismatch_pools`,
:func:`_classify_net_mismatches`). This mirrors the shape of the earlier
``gen_compose.py``/``gen_compose_routing.py`` (#1717), ``_paths.py`` (#1715),
and ``gen.py``/``gen_pcells`` (#1713) splits: a cohesive, low-coupling
subsystem relocated verbatim out of a file that had grown past the point one
module should hold request/response orchestration *and* mismatch
classification.

``run_lvs``/``check_lvs_report`` (top-level request orchestration) and the
``combine_devices`` subsystem are *not* part of this split -- they stay in
``lvs.py``, calling into this module's entry points
(:func:`_build_mismatches`, :func:`_build_net_correspondence`,
:func:`_make_compare_logger`, plus a handful of the classification helpers
below) exactly as they called the same functions when this was one file.

Dependency surface is intentionally narrow: this module calls out to only
three helpers defined in ``lvs.py`` (``_name_or_none``,
``_subcircuit_parent_name``, ``_subcircuit_ref_name``) plus ``lvs.py``'s own
``LvsError``/``CATEGORY_*``/parameter-tolerance constants -- imported *inside*
the handful of functions that use them (the same deferred-import discipline
``gen_compose_routing.py`` uses to depend on ``gen_compose.py``) rather than
at module scope, so this module never has a load-time dependency back on
``lvs.py`` -- only ``lvs.py`` depends on this module at import time.
``lvs.py`` in turn imports this module's entry points back (module scope, no
cycle -- this module never imports ``lvs`` at its own module scope) to
preserve ``klayout_tools.lvs.<name>`` as a working import path for every name
the test suite/callers used before this split.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, NamedTuple


def _build_net_correspondence(logger: Any) -> list[dict[str, Any]]:
    """Turn ``logger.net_matches`` (every successful net pairing the
    comparer produced -- unambiguous and ambiguous alike) into the
    documented ``net_correspondence[]`` response field (issue #311).

    Each entry names the layout net and its reference counterpart via
    :func:`_name_or_none` (``expanded_name()``, the same helper
    ``mismatches[].net`` already uses -- so a label-merged net's ``A|B``
    alias join is represented identically here), plus a ``pin`` boolean:
    whether the *layout* net is one of the circuit's declared pins
    (``Net.pin_count() > 0``). ``same_circuits`` pins the layout/reference
    top circuits together before the compare runs (see ``run_lvs``), so a
    matched pair's declared-pin status agrees on both sides by
    construction -- reading it off the layout side is not a side/bias
    choice.

    A net can only be matched once per circuit, but the comparer logs one
    event per circuit scope; deduplicated on ``(scope, layout name,
    reference name)`` -- the same ``scope``-qualified identity
    ``_net_key`` uses elsewhere in this class. Scoping the key by circuit
    is essential: two distinct subcircuits routinely share a local net
    name (an internal ``MID``/``OUT``/``A``), and a name-only key would
    silently merge those unrelated nets into one entry -- dropping the
    other's correspondence and reporting the wrong ``pin`` flag for
    whichever won the dedup race (issue #311). Sorted by ``(reference,
    layout)`` so repeated runs against the same inputs diff clean,
    matching this module's existing determinism guarantee for
    ``mismatches[]`` (see ``_sort_key``).
    """
    from .lvs import _name_or_none

    seen: dict[tuple[int, str | None, str | None], dict[str, Any]] = {}
    for scope, layout_net, reference_net in logger.net_matches:
        layout_name = _name_or_none(layout_net)
        reference_name = _name_or_none(reference_net)
        key = (scope, layout_name, reference_name)
        if key in seen:
            continue
        seen[key] = {
            "layout": layout_name,
            "reference": reference_name,
            "pin": bool(layout_net is not None and layout_net.pin_count() > 0),
        }
    return sorted(
        seen.values(),
        key=lambda entry: (entry["reference"] or "", entry["layout"] or ""),
    )


def _make_compare_logger(
    layout_circuit: Any | None = None, reference_circuit: Any | None = None
) -> Any:
    """Build a ``klayout.db.GenericNetlistCompareLogger`` subclass instance
    that captures every compare event into plain Python records for
    post-processing into the documented ``mismatches[]`` shape.

    Built lazily (inside a function, not a module-level class) since it must
    subclass ``klayout.db.GenericNetlistCompareLogger``, which requires the
    ``klayout`` module to already be imported -- this module keeps that
    import lazy, matching ``extract.py``'s discipline of not paying that
    cost for ``klt --version``/argument parsing.

    ``layout_circuit``/``reference_circuit`` (issue #499, optional -- the
    ``_FakeLogger`` classification unit tests construct their own stand-in
    instead of calling this factory, so no caller of *this* function omits
    them in practice) are the same top-circuit objects ``run_lvs`` passes to
    ``NetlistComparer.same_circuits``/``compare``. ``begin_circuit`` compares
    each circuit pair it is handed against these two by identity to record
    ``top_scope`` -- the ``scope`` counter value in effect while the *top*
    circuit pair is being compared (empirically NOT scope 1 in general:
    ``NetlistComparer`` visits subcircuits before their parent, so a
    hierarchical design's top circuit is typically one of the *last*
    ``begin_circuit`` calls). ``_build_mismatches`` needs this to turn a
    declared ``hints.same_nets`` pair's *names* back into the exact
    ``_NetKey`` the top circuit's own compare events used.
    """
    import klayout.db as kdb

    class _Logger(kdb.GenericNetlistCompareLogger):
        def __init__(self) -> None:
            super().__init__()
            self.net_mismatches: list[tuple[Any, Any]] = []
            self.device_mismatches: list[tuple[Any, Any]] = []
            self.param_mismatches: list[tuple[Any, Any]] = []
            self.class_mismatches: list[tuple[Any, Any]] = []
            self.pin_mismatches: list[tuple[Any, Any]] = []
            self.circuit_mismatches: list[tuple[Any, Any, str]] = []
            self.subcircuit_mismatches: list[tuple[Any, Any]] = []
            self.device_class_mismatches: list[tuple[Any, Any]] = []
            self.ambiguous_net_matches: list[tuple[Any, Any]] = []
            #: Every successful net pairing (unambiguous *and* ambiguous), as
            #: ``(scope, layout net, reference net)`` -- the raw net objects
            #: the comparer handed the logger, tagged with the ``scope`` they
            #: were seen in so ``_build_net_correspondence`` can dedupe by
            #: circuit rather than by bare name (issue #311). This is the
            #: accumulator issue #311's ``net_correspondence`` response field
            #: is built from. Kept separate from ``matched_net_keys`` (below),
            #: which only stores the derived ``_NetKey`` identity used for the
            #: merge/split and issue #282 heuristics, not the objects
            #: themselves.
            self.net_matches: list[tuple[int, Any, Any]] = []
            # Scope counter: `begin_circuit` opens one compare scope per
            # circuit pair, and every event until `end_circuit` belongs to
            # it. Net/device names are unique within a circuit, so
            # `(scope, expanded_name)` is a stable identity for an event's
            # subject -- and, unlike `Net.circuit()`, is readable from the
            # *const* references the logger receives. Used by
            # `_degraded_param_pair` (issue #282).
            self.scope = 0
            #: ``(layout key, reference key)`` for every successful net
            #: pairing (matched or ambiguously matched).
            self.matched_net_keys: list[tuple[_NetKey, _NetKey]] = []
            #: Parallel to ``net_mismatches``: the key of each side's net
            #: (``None`` where that side had none).
            self.net_mismatch_keys: list[tuple[_NetKey | None, _NetKey | None]] = []
            #: Parallel to ``device_mismatches``: the scope each was seen in.
            self.device_mismatch_scopes: list[int] = []
            self.matched_nets = 0
            self.matched_devices = 0
            self.matched_pins = 0
            #: The ``scope`` value in effect while the top circuit pair is
            #: being compared, or ``None`` if that pair was never handed to
            #: `begin_circuit` (e.g. the top circuits could not be compared
            #: at all). See this factory's docstring.
            self.top_scope: int | None = None

        def _net_key(self, net: Any) -> _NetKey | None:
            return None if net is None else (self.scope, net.expanded_name())

        def begin_circuit(self, a: Any, b: Any) -> None:
            self.scope += 1
            if (
                layout_circuit is not None
                and reference_circuit is not None
                and a is layout_circuit
                and b is reference_circuit
            ):
                self.top_scope = self.scope

        def match_nets(self, a: Any, b: Any) -> None:
            self.matched_nets += 1
            key_a = self._net_key(a)
            key_b = self._net_key(b)
            if key_a is not None and key_b is not None:
                self.matched_net_keys.append((key_a, key_b))
            self.net_matches.append((self.scope, a, b))

        def match_ambiguous_nets(self, a: Any, b: Any, msg: str) -> None:
            self.matched_nets += 1
            key_a = self._net_key(a)
            key_b = self._net_key(b)
            if key_a is not None and key_b is not None:
                self.matched_net_keys.append((key_a, key_b))
            self.ambiguous_net_matches.append((a, b))
            self.net_matches.append((self.scope, a, b))

        def net_mismatch(self, a: Any, b: Any, msg: str) -> None:
            self.net_mismatches.append((a, b))
            self.net_mismatch_keys.append((self._net_key(a), self._net_key(b)))

        def match_devices(self, a: Any, b: Any) -> None:
            self.matched_devices += 1

        def match_devices_with_different_parameters(self, a: Any, b: Any) -> None:
            # Not counted in `matched_devices` -- see counts.devices.matched's
            # "strictly successful matches only" semantics in run_lvs.
            self.param_mismatches.append((a, b))

        def match_devices_with_different_device_classes(self, a: Any, b: Any) -> None:
            self.class_mismatches.append((a, b))

        def device_mismatch(self, a: Any, b: Any, msg: str) -> None:
            self.device_mismatches.append((a, b))
            self.device_mismatch_scopes.append(self.scope)

        def match_pins(self, a: Any, b: Any) -> None:
            self.matched_pins += 1

        def pin_mismatch(self, a: Any, b: Any, msg: str) -> None:
            self.pin_mismatches.append((a, b))

        def circuit_mismatch(self, a: Any, b: Any, msg: str) -> None:
            self.circuit_mismatches.append((a, b, msg))

        def circuit_skipped(self, a: Any, b: Any, msg: str) -> None:
            self.circuit_mismatches.append((a, b, msg))

        def subcircuit_mismatch(self, a: Any, b: Any, msg: str) -> None:
            self.subcircuit_mismatches.append((a, b))

        def device_class_mismatch(self, a: Any, b: Any, msg: str) -> None:
            self.device_class_mismatches.append((a, b))

    return _Logger()


# --------------------------------------------------------------------------- #
# Event -> mismatches[] classification
# --------------------------------------------------------------------------- #


def _mismatch(
    category: str,
    severity: str,
    description: str,
    side: str,
    *,
    net: dict[str, Any] | None = None,
    device: dict[str, Any] | None = None,
    property_: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
    circuit: dict[str, Any] | None = None,
    instance: dict[str, Any] | None = None,
    subcircuit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one ``mismatches[]`` entry.

    ``details`` (issue #343) is an engine-specific escape hatch: raw data
    that does not map cleanly onto the shared ``category``/``net``/
    ``device``/``property`` shape (currently only produced by the ``netgen``
    engine's report parser, e.g. the raw side-by-side text netgen printed for
    a net/device mismatch block it did not fully structure) -- additive per
    ``docs/json-contract.md`` ("adding a field is not [a breaking change]"),
    so every entry carries the key (``null`` when unused, never omitted,
    matching this contract's existing null-not-omitted convention) rather
    than only the netgen-engine ones.

    ``circuit``/``instance``/``subcircuit`` (issue #1132) name the module and
    instance an unmatched-circuit/unmatched-subcircuit-instance ``topology``
    finding could not pair -- ``{"layout": <name|None>, "reference":
    <name|None>}``, same optional-object convention as ``net``/``device``.
    ``circuit`` is the circuit (module) name for a whole-circuit mismatch, or
    the *containing* circuit for a subcircuit-instance mismatch; ``instance``
    is the subcircuit instance's own name; ``subcircuit`` is the name of the
    circuit the instance refers to (its "cell type"). All three are ``null``
    for every mismatch category that is not itself naming a circuit/instance
    (mirroring ``net``/``device``/``property`` being ``null`` off their own
    categories).
    """
    return {
        "category": category,
        "severity": severity,
        "description": description,
        "side": side,
        "net": net,
        "device": device,
        "property": property_,
        "details": details,
        "circuit": circuit,
        "instance": instance,
        "subcircuit": subcircuit,
    }


def _terminal_names(device_class: Any) -> list[str]:
    """The ordered terminal names ``device_class`` declares (e.g. ``["A",
    "B", "W"]`` for KLayout's ``DeviceClassResistorWithBulk``) -- the same
    ``terminal_definitions()`` walk ``_device_body_net_name`` and
    ``_degraded_param_pair`` already use, factored out for
    :func:`_device_class_arity_mismatch`."""
    return [terminal.name for terminal in device_class.terminal_definitions()]


def _device_class_arity_mismatch(a: Any, b: Any) -> dict[str, Any] | None:
    """Issue #504: detect the ``device_mismatch(a, b, msg)`` shape
    ``NetlistComparer`` emits when a layout device instance and a reference
    device instance share a device-class *name* but the class registered on
    each side declares a *different terminal list* -- e.g. the deck's
    ``DeviceExtractorResistorWithBulk`` writes a three-terminal (``A``/``B``/
    ``W``) ``RES_X`` class while a plain-element reference SPICE's two-node
    ``R`` card reads back as a two-terminal (``A``/``B``) ``RES_X`` class.

    Unlike the ordinary one-sided "no counterpart at all" case this
    function's caller otherwise reports as ``device.unmatched`` (exactly one
    of ``a``/``b`` is ``None``), the comparer hands this event **both**
    instances -- it found a same-named class on each side, could not
    reconcile the terminal count, and gave up pairing them at all (there is
    no ``match_devices_with_different_device_classes`` event either, since
    the class *name* agrees; see this module's docstring). Left
    unclassified, this collapses into an unattributable
    ``device.unmatched``/``net.unmatched`` cascade that names neither class's
    terminal list -- the "silent 0/0" this issue reports. Returns ``None``
    when ``a``/``b`` is missing, the class names differ (a different,
    already-covered case -- see ``class_mismatches`` above), or the
    terminal lists already agree (the ordinary both-sided
    ``device.unmatched`` case, if it ever arises).
    """
    from .lvs import CATEGORY_DEVICE_CLASS_ARITY, _name_or_none

    if a is None or b is None:
        return None
    class_a = a.device_class()
    class_b = b.device_class()
    if class_a.name != class_b.name:
        return None
    terminals_a = _terminal_names(class_a)
    terminals_b = _terminal_names(class_b)
    if terminals_a == terminals_b:
        return None
    return _mismatch(
        CATEGORY_DEVICE_CLASS_ARITY,
        "error",
        f"device class '{class_a.name}' is declared with a different "
        f"terminal list on each side (layout: {terminals_a}, reference: "
        f"{terminals_b}) -- the comparer cannot pair devices of this class "
        "at all; see docs/cli/lvs.md, 'device.class_arity'",
        "both",
        device={
            "layout": _name_or_none(a),
            "reference": _name_or_none(b),
            "class": class_a.name,
        },
        details={"layout_terminals": terminals_a, "reference_terminals": terminals_b},
    )


def _count_devices_of_class(netlist: Any, device_class: Any) -> int:
    """Count device instances of ``device_class`` anywhere in ``netlist``
    (device classes are netlist-scoped, but instances live on individual
    circuits -- see ``_build_mismatches``'s ``device_class_mismatches``
    handling)."""
    count = 0
    for circuit in netlist.each_circuit():
        for device in circuit.each_device():
            if device.device_class() is device_class:
                count += 1
    return count


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
    device. The PMOS warning only fires when the deck also has no tap
    mechanism at all -- neither a distinct drawn ``tap`` layer nor a
    derived one (``deck.tap``/``tap_nplus``/``tap_pplus`` all ``None``,
    issue #1084) -- a deck that has *either* ties PMOS bodies to a genuine,
    named net unconditionally (no ring required, since every PMOS sits
    inside an ``nwell`` by construction), so no warning is warranted there.
    A gf180mcu layout whose declared ``tap_nplus``/``tap_pplus`` implant
    layers happen to draw no real tie shape in a *specific* layout still
    resolves each such PMOS body to an anonymous net -- exactly as it did
    before this deck declared those fields -- but is no longer flagged by
    this deck-structural warning, mirroring sky130's own long-standing
    (optimistic) treatment of a deck that merely *has* a tap mechanism as
    sufficient, not a guarantee that every individual instance used it.

    Neither warning fires at all for the pre-extracted ``layout.netlist``
    request form -- callers only reach this helper when ``layout.file`` +
    ``layout.deck`` (inline extraction) was given, mirroring how
    ``device_classes``/``provenance.deck`` are conditioned on that same
    distinction in ``run_lvs``.

    Counts only the layout's top circuit's own devices (``each_device()``,
    not recursive), matching ``run_lvs``'s own ``counts.devices.layout``
    convention -- curated-deck extraction never nests MOS devices inside a
    subcircuit.
    """
    from .lvs import CATEGORY_DEVICE_BODY_UNVERIFIED

    entries: list[dict[str, Any]] = []

    nfet_count = sum(
        1
        for device in layout_circuit.each_device()
        if device.device_class().name == deck.nfet_class
        and _device_body_net_name(device) in (deck.substrate_net, None)
    )
    if nfet_count:
        entries.append(
            _mismatch(
                CATEGORY_DEVICE_BODY_UNVERIFIED,
                "warning",
                f"{nfet_count} NMOS device body terminal(s) were compared "
                f"against the '{deck.substrate_net}' deck-synthesized "
                "substrate net, not a real schematic net -- no drawn "
                "substrate-tap geometry resolved these device(s)' body "
                "terminal to a real net (see docs/cli/extract.md, "
                '"Coverage")',
                "layout",
                device={"layout": None, "reference": None, "class": deck.nfet_class},
            )
        )

    if deck.tap is None and deck.tap_nplus is None and deck.tap_pplus is None:
        pfet_count = sum(
            1
            for device in layout_circuit.each_device()
            if device.device_class().name == deck.pfet_class
        )
        if pfet_count:
            entries.append(
                _mismatch(
                    CATEGORY_DEVICE_BODY_UNVERIFIED,
                    "warning",
                    f"{pfet_count} PMOS device body terminal(s) were "
                    "compared against an anonymous, deck-synthesized well "
                    "net, not a real schematic net -- this deck has no "
                    "distinct well-tap layer (see docs/cli/extract.md, "
                    '"Coverage")',
                    "layout",
                    device={
                        "layout": None,
                        "reference": None,
                        "class": deck.pfet_class,
                    },
                )
            )

    return entries


def _build_mismatches(
    logger: Any,
    layout_netlist: Any | None = None,
    reference_netlist: Any | None = None,
    *,
    same_nets_hints: list[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    from .lvs import (
        CATEGORY_DEVICE_CLASS,
        CATEGORY_DEVICE_UNMATCHED,
        CATEGORY_HINTS_REJECTED,
        CATEGORY_PIN_UNMATCHED,
        CATEGORY_TOPOLOGY,
        _name_or_none,
        _subcircuit_parent_name,
        _subcircuit_ref_name,
    )

    mismatches: list[dict[str, Any]] = []

    # Issue #282: on a minimal circuit the comparer may decline to pair two
    # otherwise-identical devices whose only difference is a parameter,
    # reporting `device_mismatch` on each side (plus collateral net
    # mismatches) instead of the `match_devices_with_different_parameters`
    # event that yields `device.property`. Recover that here.
    degraded = _degraded_param_pair(logger)

    # Issue #499/#1484: how the comparer answered each declared
    # `hints.same_nets` assertion -- needed twice below (to decide which
    # declared pairs are `hints.rejected` findings, and to suppress the
    # `topology` entry that would otherwise report the same comparer event a
    # second time).
    hint_outcomes = _same_nets_hint_outcomes(logger, same_nets_hints)

    # Issue #1622 deliberately does NOT filter the `circuit_mismatches`/
    # `subcircuit_mismatches` loops below for power-only (filler/tap)
    # circuits. Two reasons: (a) a report-level filter cannot help anyway --
    # `status` is always derived from `compare()`'s own boolean result (see
    # this module's docstring), so a suppressed finding would still leave
    # `status: "mismatch"`; the fix has to remove the circuit *before* the
    # compare, which `_prune_power_only_layout_circuits` does. And (b) the
    # "power-only" classification is only sound against a
    # `reference.form: "gate-level-verilog"` reference, whose conversion
    # never carries power pins -- `_build_mismatches` runs for every
    # reference form, and applying it here masked a genuine unmatched
    # signal circuit under `"plain-element"` (any layout-only circuit whose
    # pin names simply never appear in a flat reference netlist's own pin
    # universe classified as "power-only" and vanished from the report).
    mismatches.extend(
        _classify_net_mismatches(
            logger.net_mismatches,
            event_keys=getattr(logger, "net_mismatch_keys", None),
            explained_layout_nets=(
                degraded.explained_layout_nets if degraded else frozenset()
            ),
            explained_reference_nets=(
                degraded.explained_reference_nets if degraded else frozenset()
            ),
            hint_reported_pairs=hint_outcomes.refused_after_pairing,
            matched_net_keys=getattr(logger, "matched_net_keys", ()) or (),
        )
    )

    for a, b in logger.device_mismatches:
        arity_mismatch = _device_class_arity_mismatch(a, b)
        if arity_mismatch is not None:
            # Issue #504: both `a`/`b` are present here (the comparer found
            # a same-named class on each side, it just could not reconcile
            # the terminal count) -- report the dedicated, terminal-list-
            # naming category instead of falling into the generic
            # one-sided-counterpart wording below, which would misdescribe
            # a same-named-but-different-arity class pair as having "no
            # counterpart on the other side" at all.
            mismatches.append(arity_mismatch)
            continue
        side = "layout" if b is None else "reference"
        class_name = None
        for obj in (a, b):
            if obj is not None:
                class_name = obj.device_class().name
                break
        mismatches.append(
            _mismatch(
                CATEGORY_DEVICE_UNMATCHED,
                "warning" if degraded else "error",
                (
                    "device has no counterpart on the other side, but the "
                    "circuit is too small for the comparer to pair it "
                    "structurally -- the root cause is the parameter "
                    "difference reported as 'device.property' on this same "
                    'device pair (see docs/cli/lvs.md, "Negative controls")'
                )
                if degraded
                else "device has no counterpart on the other side",
                side,
                device={
                    "layout": _name_or_none(a),
                    "reference": _name_or_none(b),
                    "class": class_name,
                },
            )
        )

    if degraded is not None:
        mismatches.extend(
            _classify_param_mismatch(degraded.layout_device, degraded.reference_device)
        )

    for a, b in logger.param_mismatches:
        mismatches.extend(_classify_param_mismatch(a, b))

    for a, b in logger.class_mismatches:
        a_class = a.device_class().name
        b_class = b.device_class().name
        mismatches.append(
            _mismatch(
                CATEGORY_DEVICE_CLASS,
                "error",
                f"matched device has a different device class on each side "
                f"(layout: {a_class}, reference: {b_class})",
                "both",
                device={
                    "layout": _name_or_none(a),
                    "reference": _name_or_none(b),
                    "class": a_class,
                },
            )
        )

    for _a, b in logger.pin_mismatches:
        side = "layout" if b is None else "reference"
        mismatches.append(
            _mismatch(
                CATEGORY_PIN_UNMATCHED,
                "error",
                "pin has no counterpart on the other side",
                side,
            )
        )

    for a, b, _msg in logger.circuit_mismatches:
        side = "layout" if b is None else ("reference" if a is None else "both")
        mismatches.append(
            _mismatch(
                CATEGORY_TOPOLOGY,
                "error",
                "circuit could not be matched to a counterpart",
                side,
                # Issue #1132: `a`/`b` are the `kdb.Circuit` objects
                # themselves (one side `None`) -- name the circuit (module)
                # that failed to pair so an anonymous `topology` finding is
                # attributable without a side-channel netlist diff.
                circuit={"layout": _name_or_none(a), "reference": _name_or_none(b)},
            )
        )

    for a, b in logger.subcircuit_mismatches:
        side = "layout" if b is None else ("reference" if a is None else "both")
        mismatches.append(
            _mismatch(
                CATEGORY_TOPOLOGY,
                "error",
                "subcircuit instance could not be matched to a counterpart",
                side,
                # Issue #1132: `a`/`b` are `kdb.SubCircuit` instances (one
                # side `None`) -- name the containing circuit, the
                # instance's own name, and the circuit it instantiates
                # (its "cell type") so this finding is attributable at
                # macro scale, matching the shape the issue proposes.
                circuit={
                    "layout": _subcircuit_parent_name(a),
                    "reference": _subcircuit_parent_name(b),
                },
                instance={
                    "layout": _name_or_none(a),
                    "reference": _name_or_none(b),
                },
                subcircuit={
                    "layout": _subcircuit_ref_name(a),
                    "reference": _subcircuit_ref_name(b),
                },
            )
        )

    for a, b in logger.device_class_mismatches:
        side = "layout" if b is None else ("reference" if a is None else "both")
        # `a`/`b` is the device class present on the side that has it (the
        # other side never registered a counterpart category at all -- see
        # this module's docstring on `NetlistComparer`'s own event
        # semantics). If that side's netlist genuinely has zero instances of
        # the class (e.g. `extract.py` unconditionally registers both
        # `nfet`/`pfet` device classes even when a layout only instantiates
        # one polarity), this is not a real topology defect -- downgrade to
        # `warning`, mirroring the `ambiguous_net_matches` precedent below.
        # A class with actual instances that still has no counterpart is a
        # genuine gap and stays `error`.
        present_class = a if a is not None else b
        present_netlist = layout_netlist if a is not None else reference_netlist
        instance_count = (
            _count_devices_of_class(present_netlist, present_class)
            if present_netlist is not None
            else None
        )
        if instance_count == 0:
            mismatches.append(
                _mismatch(
                    CATEGORY_TOPOLOGY,
                    "warning",
                    "device class has no counterpart on the other side, but "
                    "no devices of this class were extracted either -- not "
                    "a real topology mismatch",
                    side,
                )
            )
        else:
            mismatches.append(
                _mismatch(
                    CATEGORY_TOPOLOGY,
                    "error",
                    "device class could not be mapped to a counterpart",
                    side,
                )
            )

    for a, b in logger.ambiguous_net_matches:
        # Issue #596: an ambiguous pairing where *both* sides are a
        # single-device-terminal, non-pin net is not a routine naming nit --
        # it is a real finding (a net with no other connectivity, most often
        # an undriven MOS gate on both sides at once, since a
        # generator-driven flow that draws both the layout and the reference
        # from the same source reproduces the same defect on each). Detected
        # structurally from the paired `kdb.Net` objects themselves
        # (`terminal_count() == 1` and `pin_count() == 0` on each side) --
        # `getattr` guards keep this a no-op against a stand-in object (e.g.
        # a test double) that only implements `expanded_name()`.
        single_terminal_both_sides = (
            a is not None
            and b is not None
            and getattr(a, "terminal_count", lambda: None)() == 1
            and getattr(b, "terminal_count", lambda: None)() == 1
            and getattr(a, "pin_count", lambda: None)() == 0
            and getattr(b, "pin_count", lambda: None)() == 0
        )
        if single_terminal_both_sides:
            description = (
                "both sides pair a net that touches exactly one device "
                "terminal and carries no declared pin -- there is no DC "
                "path through this node on either side, so this is a real "
                "connectivity finding (e.g. an undriven MOS gate), not a "
                "routine ambiguous-pairing/hints.same_nets nit; see klt "
                "extract's single_terminal_nets[] for the layout-side "
                "terminal detail"
            )
        else:
            description = (
                "nets were paired ambiguously; the comparer resolved it "
                "structurally (consider a hints.same_nets entry to pin this down)"
            )
        mismatches.append(
            _mismatch(
                CATEGORY_TOPOLOGY,
                "warning",
                description,
                "both",
                net={
                    "layout": _name_or_none(a),
                    "reference": _name_or_none(b),
                },
            )
        )

    # Issue #499: a `hints.same_nets` pairing is a hard assertion --
    # `_apply_hints` calls `comparer.same_nets(..., must_match=True)` for
    # every declared pair. If the comparer did not end up confirming that
    # pair as a match, the caller's assertion was refused, and that
    # disagreement is reported here rather than silently dropped. Detected
    # structurally (declared pair vs. the comparer's own pairing events, both
    # keyed by the top circuit's `top_scope` -- see
    # `_same_nets_hint_outcomes`), not by parsing the comparer's own
    # `log_entry` text -- this field's own contract (docs/cli/lvs.md,
    # "mismatches[].description") requires a curated description, never raw
    # `NetlistComparer` log text.
    top_scope = getattr(logger, "top_scope", None)
    for layout_name, reference_name in same_nets_hints or ():
        pair = ((top_scope, layout_name), (top_scope, reference_name))
        if pair in hint_outcomes.confirmed:
            continue
        # Issue #1484: distinguish the two ways an assertion is refused. A
        # pair the comparer associated and then flagged (a both-sided
        # `net_mismatch`) is refused *because the two nets are not
        # topologically identical* -- the real difference is reported
        # elsewhere in this same `mismatches[]`, and no hint can paper over
        # it. Saying so is the difference between an actionable report and a
        # caller re-declaring the hint expecting a different answer.
        if pair in hint_outcomes.refused_after_pairing:
            description = (
                "hints.same_nets declared this pairing, but the comparer "
                "associated the two nets and found them not identical "
                "topologically -- the underlying structural difference is "
                "reported separately in this same mismatches[]; a "
                "hints.same_nets entry cannot resolve it (see "
                'docs/cli/lvs.md, "hints.rejected")'
            )
        else:
            description = (
                "hints.same_nets declared this pairing, but the comparer "
                "did not confirm it as a topological match"
            )
        mismatches.append(
            _mismatch(
                CATEGORY_HINTS_REJECTED,
                "error",
                description,
                "both",
                net={"layout": layout_name, "reference": reference_name},
            )
        )

    mismatches.sort(key=_sort_key)
    return mismatches


#: ``(compare scope, expanded net name)`` -- see ``_make_compare_logger``'s
#: ``scope`` counter for why identity is keyed this way rather than by the
#: net's circuit (which is not readable from a const reference).
_NetKey = tuple[int, str]


class _SameNetsHintOutcomes(NamedTuple):
    """How the comparer answered each declared ``hints.same_nets`` assertion,
    in :data:`_NetKey` pair terms (issue #499 / issue #1484).

    ``confirmed`` -- the comparer paired the two nets cleanly
    (``match_nets``/``match_ambiguous_nets``). The assertion was honored;
    nothing is reported.

    ``refused_after_pairing`` -- the comparer associated the declared pair but
    reported it as a **both-sided** ``net_mismatch``. Empirically (KLayout
    0.30.x) that is exactly the shape a *refused* ``same_nets(...,
    must_match=True)`` assertion takes: ``NetlistComparer`` logs "Nets A vs. B
    are paired explicitly, but are not identical topologically" and emits the
    pair as a net mismatch. It is still a ``hints.rejected`` finding -- but the
    same event *also* drives :func:`_classify_net_mismatches`'s ``topology``
    "nets were paired despite a name/identity conflict" entry, so reporting
    both would describe one comparer event twice and make declaring the hint
    strictly worse than not declaring it (issue #1484). The narrower,
    caller-attributed ``hints.rejected`` entry wins; the ``topology``
    duplicate is suppressed.
    """

    confirmed: frozenset[tuple[_NetKey, _NetKey]]
    refused_after_pairing: frozenset[tuple[_NetKey, _NetKey]]


def _same_nets_hint_outcomes(
    logger: Any,
    same_nets_hints: list[tuple[str, str]] | None,
) -> _SameNetsHintOutcomes:
    """Classify each declared ``hints.same_nets`` pair -- see
    :class:`_SameNetsHintOutcomes`.

    Both buckets are empty when the top circuit pair was never compared
    (``top_scope is None``), so every declared pair is then reported
    ``hints.rejected`` with the generic description, exactly as before.
    """
    top_scope = getattr(logger, "top_scope", None)
    declared = {
        ((top_scope, layout_name), (top_scope, reference_name))
        for layout_name, reference_name in same_nets_hints or ()
    }
    if top_scope is None or not declared:
        return _SameNetsHintOutcomes(frozenset(), frozenset())
    matched = set(getattr(logger, "matched_net_keys", None) or ())
    flagged = {
        (key_a, key_b)
        for key_a, key_b in getattr(logger, "net_mismatch_keys", None) or ()
        if key_a is not None and key_b is not None
    }
    confirmed = declared & matched
    return _SameNetsHintOutcomes(
        frozenset(confirmed),
        frozenset((declared & flagged) - confirmed),
    )


class _DegradedParamPair(NamedTuple):
    """One unmatched-device pair that :func:`_degraded_param_pair` proved is
    really a parameter difference (issue #282).

    ``explained_layout_nets``/``explained_reference_nets`` are the
    :data:`_NetKey` s of the one-sided net mismatches whose unmatched-ness is
    *fully* accounted for by this device pair (the nets touch no other device
    and carry no subcircuit pin) -- collateral, not independent findings.
    """

    layout_device: Any
    reference_device: Any
    explained_layout_nets: frozenset[_NetKey]
    explained_reference_nets: frozenset[_NetKey]


def _net_correspondence(
    logger: Any,
) -> tuple[dict[_NetKey, _NetKey], set[_NetKey], set[_NetKey]]:
    """``(layout->reference net pairing, layout-only nets, reference-only nets)``
    as the comparer saw them, in :data:`_NetKey` terms.

    The pairing includes both cleanly matched nets and *both-sided* net
    mismatch events: the comparer did associate those two nets with each
    other, it just also flagged the pairing -- for the purpose of deciding
    whether two devices sit on the same nets, an associated pair is an
    association.
    """
    paired: dict[_NetKey, _NetKey] = dict(logger.matched_net_keys)
    layout_only: set[_NetKey] = set()
    reference_only: set[_NetKey] = set()
    for key_a, key_b in logger.net_mismatch_keys:
        if key_a is not None and key_b is not None:
            paired.setdefault(key_a, key_b)
        elif key_a is not None:
            layout_only.add(key_a)
        elif key_b is not None:
            reference_only.add(key_b)
    return paired, layout_only, reference_only


def _net_is_explained_by_device(net: Any, device: Any) -> bool:
    """True when ``net``'s only device connection is ``device`` and it carries
    no subcircuit pin -- i.e. nothing but this one device can explain why the
    comparer failed to pair the net."""
    if net.subcircuit_pin_count() != 0:
        return False
    device_name = device.expanded_name()
    return all(
        ref.device().expanded_name() == device_name for ref in net.each_terminal()
    )


def _degraded_param_pair(logger: Any) -> _DegradedParamPair | None:
    """Detect the minimal-circuit degradation issue #282 describes and return
    the device pair behind it, or ``None``.

    ``NetlistComparer`` pairs devices from the surrounding net structure and
    only *then* compares parameters. On a circuit small enough that the
    devices' own terminals are the structure (the canonical case: a
    two-device inverter whose bulk terminals sit on their own substrate/well
    nets), a single wrong ``W`` leaves it with nothing to anchor the pairing
    on: it emits ``device_mismatch`` on each side plus a collateral one-sided
    net mismatch for every net that only those two devices touched, and never
    the ``match_devices_with_different_parameters`` event that would produce
    ``device.property``. The report then points at connectivity when the
    defect is a number.

    Everything needed to say so is already in hand, so this recovers it --
    deliberately narrowly, since a wrong claim here would mask a real
    connectivity defect. All of the following must hold:

    * exactly one unmatched device on each side and no other device mismatch;
    * identical device class name, terminal definitions and parameter
      definitions;
    * every terminal of the layout device lands on the net the reference
      device's same terminal lands on -- either a net the comparer explicitly
      paired, or a net left unpaired on *both* sides with the same top-level
      pin count and either the same name or no other device/subcircuit
      touching it (the collateral the device pair itself caused);
    * at least one parameter actually differs by more than this module's
      floating-point epsilon.

    The verdict is untouched either way: ``compare()`` already said
    "mismatch" and still does. This only decides which entry the caller reads
    first.
    """
    device_mismatches = list(logger.device_mismatches)
    scopes = list(getattr(logger, "device_mismatch_scopes", ()))
    if len(device_mismatches) != 2 or len(scopes) != 2:
        # A logger without the parallel bookkeeping (the fake loggers the
        # classification unit tests use) never enters this path.
        return None
    if scopes[0] != scopes[1]:
        # One unmatched device in each of two *different* circuits is two
        # findings, not one degraded pair.
        return None
    layout_only = [a for a, b in device_mismatches if a is not None and b is None]
    reference_only = [b for a, b in device_mismatches if a is None and b is not None]
    if len(layout_only) != 1 or len(reference_only) != 1:
        return None
    a, b = layout_only[0], reference_only[0]

    class_a = a.device_class()
    class_b = b.device_class()
    if class_a.name != class_b.name:
        return None
    if not hasattr(class_a, "terminal_definitions") or not hasattr(
        class_b, "terminal_definitions"
    ):
        return None

    terminals_a = [(t.id(), t.name) for t in class_a.terminal_definitions()]
    if terminals_a != [(t.id(), t.name) for t in class_b.terminal_definitions()]:
        return None
    params_a = list(class_a.parameter_definitions())
    if [(p.id(), p.name) for p in params_a] != [
        (p.id(), p.name) for p in class_b.parameter_definitions()
    ]:
        return None

    paired, unpaired_layout, unpaired_reference = _net_correspondence(logger)
    scope = scopes[0]
    explained_layout: set[_NetKey] = set()
    explained_reference: set[_NetKey] = set()

    for terminal_id, _terminal_name in terminals_a:
        net_a = a.net_for_terminal(terminal_id)
        net_b = b.net_for_terminal(terminal_id)
        if net_a is None and net_b is None:
            continue
        if net_a is None or net_b is None:
            return None
        key_a = (scope, net_a.expanded_name())
        key_b = (scope, net_b.expanded_name())
        if key_a in paired:
            if paired[key_a] != key_b:
                return None
            continue
        if key_a not in unpaired_layout or key_b not in unpaired_reference:
            return None
        # Both sides left this net unpaired. It corresponds only if the two
        # are interchangeable: same number of top-level pins, and either the
        # same name or -- for a net nothing but this one device touches (a
        # dangling well/bulk net is the common case) -- structurally
        # identical, whatever it happens to be called on each side.
        if net_a.pin_count() != net_b.pin_count():
            return None
        collateral = _net_is_explained_by_device(
            net_a, a
        ) and _net_is_explained_by_device(net_b, b)
        if not collateral and net_a.expanded_name() != net_b.expanded_name():
            return None
        if collateral:
            explained_layout.add(key_a)
            explained_reference.add(key_b)

    if not any(
        _values_differ(a.parameter(param.id()), b.parameter(param.id()))
        for param in params_a
    ):
        return None

    return _DegradedParamPair(
        a, b, frozenset(explained_layout), frozenset(explained_reference)
    )


def _classify_param_mismatch(a: Any, b: Any) -> list[dict[str, Any]]:
    """Turn one ``match_devices_with_different_parameters`` event into one
    ``device.property`` mismatch entry per parameter that actually differs
    (see this module's docstring: the comparer flags the *device pair*, not
    which specific parameter -- this module identifies that itself)."""
    from .lvs import _PARAM_DISPLAY_NAMES, CATEGORY_DEVICE_PROPERTY, _name_or_none

    class_name = a.device_class().name
    entries: list[dict[str, Any]] = []
    for param in a.device_class().parameter_definitions():
        a_value = a.parameter(param.id())
        b_value = b.parameter(param.id())
        if _values_differ(a_value, b_value):
            display_name = _PARAM_DISPLAY_NAMES.get(param.name, param.name.lower())
            entries.append(
                _mismatch(
                    CATEGORY_DEVICE_PROPERTY,
                    "error",
                    f"matched device parameter '{display_name}' differs",
                    "both",
                    device={
                        "layout": _name_or_none(a),
                        "reference": _name_or_none(b),
                        "class": class_name,
                    },
                    property_={
                        "name": display_name,
                        "layout": a_value,
                        "reference": b_value,
                    },
                )
            )
    if not entries:
        # The comparer's own (stricter) tolerance flagged a difference this
        # module's parameter-by-parameter epsilon didn't reproduce -- report
        # the device pair generically rather than silently dropping a real
        # finding (same safety-net principle as `run_lvs`'s empty-mismatches
        # guard).
        entries.append(
            _mismatch(
                CATEGORY_DEVICE_PROPERTY,
                "error",
                "matched device parameters differ",
                "both",
                device={
                    "layout": _name_or_none(a),
                    "reference": _name_or_none(b),
                    "class": class_name,
                },
            )
        )
    return entries


def _values_differ(a_value: float, b_value: float) -> bool:
    from .lvs import _PARAM_ABS_EPSILON, _PARAM_REL_EPSILON

    return abs(a_value - b_value) > max(
        _PARAM_ABS_EPSILON, _PARAM_REL_EPSILON * max(abs(a_value), abs(b_value))
    )


# --------------------------------------------------------------------------- #
# options.parameter_tolerance (issue #589): snap-and-recompare
# --------------------------------------------------------------------------- #


def _parse_parameter_tolerance(options: dict[str, Any]) -> float | None:
    """Validate ``options.parameter_tolerance`` into a float, or ``None`` when
    omitted (the default: exact compare, unchanged behaviour).

    A *relative* tolerance expressed as a fraction (``0.001`` is 0.1%), not a
    percentage and not an absolute value -- the parameters this compares span
    micrometres, ohms and farads in one request, so no single absolute number
    would be meaningful across them.

    Everything malformed raises :class:`LvsError` rather than degrading to the
    default, matching every other request-side hook in this module
    (``hints.same_nets``, ``reference.device_bulk``): a tolerance the caller
    believes is in force but is not would be the worst possible failure mode
    for this particular option.
    """
    from .lvs import _MAX_PARAMETER_TOLERANCE, LvsError

    if "parameter_tolerance" not in options:
        return None
    value = options["parameter_tolerance"]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LvsError(
            "options.parameter_tolerance must be a number (a relative "
            "tolerance expressed as a fraction, e.g. 0.001 for 0.1%); got "
            f"{value!r}"
        )
    tolerance = float(value)
    if tolerance < 0:
        raise LvsError(
            f"options.parameter_tolerance must not be negative; got {tolerance!r}"
        )
    if tolerance >= _MAX_PARAMETER_TOLERANCE:
        raise LvsError(
            f"options.parameter_tolerance must be below "
            f"{_MAX_PARAMETER_TOLERANCE} (a relative tolerance expressed as a "
            f"fraction -- 0.001 is 0.1%); got {tolerance!r}, which would call "
            "almost any two values equal"
        )
    return tolerance


class _ToleratedParam(NamedTuple):
    """One reference-side device parameter :func:`_collect_tolerance_snaps`
    resolved to be within ``options.parameter_tolerance`` of its layout-side
    counterpart (issue #589).

    ``circuit_name``/``device_id``/``param_id`` are the *lookup key* rather
    than the reference ``Device`` object itself: the objects a
    ``GenericNetlistCompareLogger`` receives are const references, and
    ``Device.set_parameter`` on one raises ``RuntimeError: Cannot call
    non-const method on a const reference``. The mutation therefore has to
    re-resolve the device from the owning netlist
    (:func:`_apply_tolerance_snaps`).
    """

    circuit_name: str
    device_id: int
    param_id: int
    display_name: str
    layout_value: float
    reference_value: float
    layout_device: str | None
    reference_device: str | None
    device_class: str | None
    relative_delta: float


def _relative_delta(a_value: float, b_value: float) -> float:
    """``|a-b| / max(|a|, |b|)`` -- the same denominator
    :func:`_values_differ`'s relative term uses, so a tolerance and the
    float-noise epsilon are expressed on one scale.

    ``0.0`` when both values are exactly zero; ``1.0`` when exactly one of
    them is (no finite relative tolerance below
    :data:`_MAX_PARAMETER_TOLERANCE` can absorb a zero-vs-nonzero difference,
    which is the intended outcome -- that is a missing/short device, not a
    rounding difference).
    """
    scale = max(abs(a_value), abs(b_value))
    if scale == 0.0:
        return 0.0
    return abs(a_value - b_value) / scale


def _tolerated_device_pair(
    a: Any, b: Any, tolerance: float
) -> list[_ToleratedParam] | None:
    """The snap records for one layout/reference device pair whose *every*
    differing parameter sits within ``tolerance``, or ``None``.

    All-or-nothing per device pair on purpose: snapping only the in-tolerance
    parameters of a pair that also differs by more than the tolerance
    somewhere else would drop that in-tolerance parameter from
    ``mismatches[]`` while the run still reports ``mismatch`` -- strictly less
    information than today's output for a pair the tolerance cannot rescue
    anyway. A pair with an out-of-tolerance parameter is therefore left
    completely alone, and reports exactly what it reports today.

    Uses a bare ``!=`` rather than :func:`_values_differ` to decide which
    parameters are candidates: ``NetlistComparer``'s own equality tolerance is
    *tighter* than this module's float-noise epsilon, so a difference below
    :data:`_PARAM_REL_EPSILON` can still be what the engine refused to match
    on -- and an explicit tolerance should absorb that too, not just the
    differences this module happens to classify.
    """
    from .lvs import _PARAM_DISPLAY_NAMES, _name_or_none

    device_class = a.device_class()
    try:
        circuit_name = b.circuit().name
        device_id = b.id()
    except (AttributeError, RuntimeError):
        # A stand-in device object (the classification unit tests' fake
        # loggers) has no owning circuit to re-resolve the mutation against.
        return None
    if circuit_name is None:
        return None

    records: list[_ToleratedParam] = []
    for param in device_class.parameter_definitions():
        a_value = a.parameter(param.id())
        b_value = b.parameter(param.id())
        if a_value == b_value:
            continue
        delta = _relative_delta(a_value, b_value)
        if delta > tolerance:
            return None
        records.append(
            _ToleratedParam(
                circuit_name=circuit_name,
                device_id=device_id,
                param_id=param.id(),
                display_name=_PARAM_DISPLAY_NAMES.get(param.name, param.name.lower()),
                layout_value=a_value,
                reference_value=b_value,
                layout_device=_name_or_none(a),
                reference_device=_name_or_none(b),
                device_class=device_class.name,
                relative_delta=delta,
            )
        )
    return records or None


def _collect_tolerance_snaps(logger: Any, tolerance: float) -> list[_ToleratedParam]:
    """Every reference-side parameter one ``compare()`` pass showed to be
    within ``tolerance`` of its layout-side counterpart (issue #589).

    Covers both routes a parameter-only difference reaches this module by:

    * ``match_devices_with_different_parameters`` -- the clean case, where
      surrounding connectivity was enough for ``NetlistComparer`` to pair the
      two devices before comparing their parameters;
    * :func:`_degraded_param_pair` -- the minimal-cell case (issue #282),
      where the devices' own terminals *are* the surrounding structure, the
      comparer never pairs them at all, and this module reconstructs the
      pairing from the ``device_mismatch``/``net_mismatch`` cascade instead.
      Handling only the first would leave the very shape real extracted
      layouts hit (any body/well net not shorted to a rail) unable to reach
      ``"match"`` however wide the tolerance.
    """
    pairs: list[tuple[Any, Any]] = list(getattr(logger, "param_mismatches", ()) or ())
    degraded = _degraded_param_pair(logger)
    if degraded is not None:
        pairs.append((degraded.layout_device, degraded.reference_device))

    snaps: list[_ToleratedParam] = []
    seen: set[tuple[str, int, int]] = set()
    for a, b in pairs:
        if a is None or b is None:
            continue
        records = _tolerated_device_pair(a, b, tolerance)
        if records is None:
            continue
        for record in records:
            key = (record.circuit_name, record.device_id, record.param_id)
            if key in seen:
                continue
            seen.add(key)
            snaps.append(record)
    return snaps


def _apply_tolerance_snaps(
    reference_netlist: Any, snaps: list[_ToleratedParam]
) -> None:
    """Set each snapped reference-side parameter to its layout-side value, so
    the next ``compare()`` sees two netlists that agree within the caller's
    tolerance.

    Re-resolves every device from ``reference_netlist`` by
    ``(circuit name, device id)`` -- see :class:`_ToleratedParam` for why the
    logger's own object references cannot be mutated. A device that no longer
    resolves is skipped rather than raising: the worst case is that the next
    ``compare()`` still reports the difference, which is the safe direction.
    """
    for snap in snaps:
        circuit = reference_netlist.circuit_by_name(snap.circuit_name)
        if circuit is None:
            continue
        device = circuit.device_by_id(snap.device_id)
        if device is None:
            continue
        device.set_parameter(snap.param_id, snap.layout_value)


def _tolerance_disclosure(snap: _ToleratedParam, tolerance: float) -> dict[str, Any]:
    """The ``severity: "warning"`` :data:`CATEGORY_DEVICE_PARAMETER_TOLERATED`
    entry disclosing one absorbed parameter difference (issue #589).

    Carries both original values in ``property`` (the reference side's
    pre-snap number, never the snapped one) so the caller can always recover
    what was actually compared -- the same "disclose, never silently absorb"
    discipline ``device.bulk_reconciled``/``device.body_unverified`` apply.
    """
    from .lvs import CATEGORY_DEVICE_PARAMETER_TOLERATED

    return _mismatch(
        CATEGORY_DEVICE_PARAMETER_TOLERATED,
        "warning",
        f"matched device parameter '{snap.display_name}' differs by "
        f"{snap.relative_delta * 100:.4g}%, within the requested "
        f"options.parameter_tolerance -- the reference value was snapped to "
        f"the layout value for the comparison, so this dimension of the "
        f"compare was verified only to that tolerance, not exactly (see "
        "docs/cli/lvs.md, 'device.parameter_tolerated')",
        "both",
        device={
            "layout": snap.layout_device,
            "reference": snap.reference_device,
            "class": snap.device_class,
        },
        property_={
            "name": snap.display_name,
            "layout": snap.layout_value,
            "reference": snap.reference_value,
        },
        details={
            "relative_delta": snap.relative_delta,
            "tolerance": tolerance,
        },
    )


#: A node in the merge/split locality graph (issue #1533): which netlist the
#: net came from, plus its :data:`_NetKey` (compare scope + expanded name).
_NetNode = tuple[str, int, str]


class _UnionFind:
    """Minimal union-find over hashable nodes, for the merge/split locality
    grouping in :func:`_net_mismatch_pools` (issue #1533)."""

    def __init__(self) -> None:
        self._parent: dict[Any, Any] = {}

    def find(self, node: Any) -> Any:
        parent = self._parent
        if node not in parent:
            parent[node] = node
            return node
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:
            parent[node], node = root, parent[node]
        return root

    def union(self, a: Any, b: Any) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self._parent[root_b] = root_a


def _adjacent_nets(net: Any) -> list[Any]:
    """Every net that shares a device terminal or a subcircuit-instance pin
    with ``net`` -- one hop in the netlist's own connectivity graph.

    Traversal never leaves ``net``'s circuit: a ``Device``/``SubCircuit`` a
    net touches belongs to the same circuit the net does, and
    ``SubCircuit.net_for_pin`` resolves to nets of that *parent* circuit, not
    of the instantiated one. That is what lets :func:`_net_mismatch_pools`
    key its nodes by the compare scope the event was logged in.
    """
    adjacent: list[Any] = []
    for ref in net.each_terminal():
        device = ref.device()
        for terminal in device.device_class().terminal_definitions():
            other = device.net_for_terminal(terminal.id())
            if other is not None:
                adjacent.append(other)
    for ref in net.each_subcircuit_pin():
        subcircuit = ref.subcircuit()
        for pin in subcircuit.circuit_ref().each_pin():
            other = subcircuit.net_for_pin(pin.id())
            if other is not None:
                adjacent.append(other)
    return adjacent


def _explore_net_component(
    groups: _UnionFind, visited: set[_NetNode], side: str, scope: int, net: Any
) -> None:
    """Union every net reachable from ``net`` (within its own circuit and its
    own side's netlist) into one component."""
    node: _NetNode = (side, scope, net.expanded_name())
    if node in visited:
        return
    stack = [(node, net)]
    while stack:
        current_node, current_net = stack.pop()
        if current_node in visited:
            continue
        visited.add(current_node)
        groups.find(current_node)
        for other in _adjacent_nets(current_net):
            other_node: _NetNode = (side, scope, other.expanded_name())
            groups.union(current_node, other_node)
            if other_node not in visited:
                stack.append((other_node, other))


def _net_mismatch_pools(
    tagged: list[tuple[tuple[Any, Any], tuple[_NetKey | None, _NetKey | None]]],
    matched_net_keys: Sequence[tuple[_NetKey, _NetKey]] = (),
) -> list[list[tuple[tuple[Any, Any], tuple[_NetKey | None, _NetKey | None]]]]:
    """Partition ``net_mismatch`` events into independently-classifiable
    pools -- one per weakly-connected component of the compared netlists
    (issue #1533).

    The merge/split heuristic (see this module's docstring and
    :func:`_classify_net_mismatches`) reads a *pattern* of co-occurring
    events: a one-sided leftover net plus a differently-named both-sided
    pairing. Pooling every event of a whole compare run together made that
    pattern global -- composing an electrically unrelated block into the same
    top circuit could turn an otherwise-isolated ``net.unmatched`` into a
    ``net.split`` (and, via the ``explained_*_nets`` downgrade the
    ``net.split`` branch does not apply, an already-tolerated
    ``severity: "warning"`` into an ``"error"``) without any change to the
    first block's own geometry or connectivity.

    The pools are the components of the graph whose nodes are
    ``(side, compare scope, expanded net name)`` and whose edges are:

    * **within a side** -- two nets sharing a device terminal or a
      subcircuit-instance pin (:func:`_adjacent_nets`), explored transitively
      from every net named by an event;
    * **across the sides** -- every net pairing the comparer itself made,
      matched (``matched_net_keys``) or reported as a both-sided
      ``net_mismatch``.

    Gluing the two sides along the comparer's own pairings is what keeps a
    *genuine* split classified as one: splitting a net severs the layout-side
    connection between its fragments by construction (a series chain broken in
    two leaves two layout components), but both fragments still reach the one
    reference net they came from through the surrounding matched nets -- so
    they land in the same pool, exactly as before. Only content with no
    connectivity *and* no comparer-made pairing linking it to the rest -- an
    independently-composed, galvanically isolated block -- separates out.

    Falls back to a single pool (today's whole-run behaviour) whenever the
    graph cannot be built: no compare-scope keys, net objects that do not
    expose the netlist graph (the fake-logger classification unit tests), or
    any error raised while walking it. Grouping is an accuracy refinement --
    it must never be the reason a compare fails to produce a report.
    """
    if not tagged:
        return []

    # The pattern match below only reads pool membership when a one-sided
    # leftover and a renamed both-sided pairing are both present -- with
    # either missing, every pool reaches the same verdict a single pool would.
    # Short-circuit there so a routine compare never pays for the graph walk.
    if not any((a is None) != (b is None) for (a, b), _key in tagged) or not any(
        a is not None and b is not None and a.expanded_name() != b.expanded_name()
        for (a, b), _key in tagged
    ):
        return [tagged]

    groups = _UnionFind()
    visited: set[_NetNode] = set()
    try:
        for (a, b), key in tagged:
            for side, index, net in (("layout", 0, a), ("reference", 1, b)):
                if net is None:
                    continue
                net_key = key[index]
                if net_key is None or not hasattr(net, "each_terminal"):
                    return [tagged]
                _explore_net_component(groups, visited, side, net_key[0], net)
            if key[0] is not None and key[1] is not None:
                groups.union(("layout", *key[0]), ("reference", *key[1]))
        for layout_key, reference_key in matched_net_keys:
            groups.union(("layout", *layout_key), ("reference", *reference_key))

        pools: dict[Any, list[Any]] = {}
        for event, key in tagged:
            net_key = key[0] if key[0] is not None else key[1]
            if net_key is None:
                return [tagged]
            side = "layout" if key[0] is not None else "reference"
            root = groups.find((side, *net_key))
            pools.setdefault(root, []).append((event, key))
    except Exception:  # pragma: no cover - defensive, see docstring
        return [tagged]

    return list(pools.values())


def _classify_net_mismatches(
    events: list[tuple[Any, Any]],
    *,
    event_keys: list[tuple[_NetKey | None, _NetKey | None]] | None = None,
    explained_layout_nets: frozenset[_NetKey] = frozenset(),
    explained_reference_nets: frozenset[_NetKey] = frozenset(),
    hint_reported_pairs: frozenset[tuple[_NetKey, _NetKey]] = frozenset(),
    matched_net_keys: Sequence[tuple[_NetKey, _NetKey]] = (),
) -> list[dict[str, Any]]:
    """Classify raw ``net_mismatch`` events into ``net.unmatched``/
    ``net.merged``/``net.split``/``topology`` entries -- see this module's
    docstring for the heuristic and its documented limitation.

    ``explained_*_nets`` (issue #282) name the one-sided nets whose
    unmatched-ness is entirely collateral from a device pair reported
    separately as ``device.property`` (see :func:`_degraded_param_pair`).
    Those keep their category -- the event really did happen, and dropping it
    would make ``mismatches[]`` disagree with the comparer's own log -- but
    report ``severity: "warning"``, so a caller filtering on ``"error"``
    reads the parameter defect instead of four fine nets. ``event_keys`` is
    the compare logger's key list, parallel to ``events`` (omitted by the
    fake-logger unit tests, in which case nothing is ever "explained").

    ``hint_reported_pairs`` (issue #1484, from
    :func:`_same_nets_hint_outcomes`) names the both-sided net-mismatch events
    already reported as ``hints.rejected`` by :func:`_build_mismatches`
    because the caller declared that exact pair via ``hints.same_nets``. A
    renamed pairing in that set is *not* also reported as a ``topology``
    "name/identity conflict": both entries describe the one comparer event,
    and the ``hints.rejected`` one is strictly more informative (it names the
    caller's own assertion). Without this, declaring a ``hints.same_nets``
    entry for such a pair could only ever *raise* the mismatch count. Pairs in
    this set still count as renamed pairings for the merge/split heuristic
    above -- suppressing a duplicate report does not make a leftover one-sided
    net stop being a split or a merge.

    ``matched_net_keys`` (issue #1533) is the compare logger's list of net
    pairings; it is only read to glue the two sides' connectivity graphs
    together in :func:`_net_mismatch_pools`, which decides *which* events may
    corroborate each other's merge/split classification. The pattern matching
    below is unchanged -- it just runs once per weakly-connected component
    instead of once per compare run, so an unrelated, galvanically isolated
    block composed into the same top circuit cannot reclassify another
    block's nets.
    """
    keys: list[tuple[_NetKey | None, _NetKey | None]] = (
        list(event_keys) if event_keys is not None else [(None, None)] * len(events)
    )
    tagged = list(zip(events, keys, strict=True))
    entries: list[dict[str, Any]] = []
    for pool in _net_mismatch_pools(tagged, matched_net_keys):
        entries.extend(
            _classify_net_mismatch_pool(
                pool,
                explained_layout_nets=explained_layout_nets,
                explained_reference_nets=explained_reference_nets,
                hint_reported_pairs=hint_reported_pairs,
            )
        )
    return entries


def _classify_net_mismatch_pool(
    tagged: list[tuple[tuple[Any, Any], tuple[_NetKey | None, _NetKey | None]]],
    *,
    explained_layout_nets: frozenset[_NetKey],
    explained_reference_nets: frozenset[_NetKey],
    hint_reported_pairs: frozenset[tuple[_NetKey, _NetKey]],
) -> list[dict[str, Any]]:
    """Apply the merge/split pattern match to one pool of co-located
    ``net_mismatch`` events (see :func:`_net_mismatch_pools`)."""
    from .lvs import (
        _COLLATERAL_NET_DESCRIPTION,
        CATEGORY_NET_MERGED,
        CATEGORY_NET_SPLIT,
        CATEGORY_NET_UNMATCHED,
        CATEGORY_TOPOLOGY,
        _name_or_none,
    )

    one_sided_layout = [
        (a, key) for (a, b), key in tagged if a is not None and b is None
    ]
    one_sided_reference = [
        (b, key) for (a, b), key in tagged if a is None and b is not None
    ]
    both_sided_renamed = [
        (a, b, key)
        for (a, b), key in tagged
        if a is not None and b is not None and a.expanded_name() != b.expanded_name()
    ]

    entries: list[dict[str, Any]] = []

    if one_sided_layout and both_sided_renamed:
        for a, _key in one_sided_layout:
            entries.append(
                _mismatch(
                    CATEGORY_NET_SPLIT,
                    "error",
                    "a reference net's role is divided across multiple layout nets",
                    "layout",
                    net={"layout": _name_or_none(a), "reference": None},
                )
            )
    elif one_sided_layout:
        for a, key in one_sided_layout:
            explained = key[0] in explained_layout_nets
            entries.append(
                _mismatch(
                    CATEGORY_NET_UNMATCHED,
                    "warning" if explained else "error",
                    _COLLATERAL_NET_DESCRIPTION
                    if explained
                    else "layout net has no reference counterpart",
                    "layout",
                    net={"layout": _name_or_none(a), "reference": None},
                )
            )

    if one_sided_reference and both_sided_renamed:
        for b, _key in one_sided_reference:
            entries.append(
                _mismatch(
                    CATEGORY_NET_MERGED,
                    "error",
                    "multiple reference nets were collapsed into one layout net",
                    "reference",
                    net={"layout": None, "reference": _name_or_none(b)},
                )
            )
    elif one_sided_reference:
        for b, key in one_sided_reference:
            explained = key[1] in explained_reference_nets
            entries.append(
                _mismatch(
                    CATEGORY_NET_UNMATCHED,
                    "warning" if explained else "error",
                    _COLLATERAL_NET_DESCRIPTION
                    if explained
                    else "reference net has no layout counterpart",
                    "reference",
                    net={"layout": None, "reference": _name_or_none(b)},
                )
            )

    # Both-sided events are absorbed into the merge/split entries above when
    # a one-sided leftover exists on either side (they are the same root
    # cause, reported once); a differing-name pairing with no accompanying
    # leftover is its own, otherwise-unreported finding, standalone.
    if not one_sided_layout and not one_sided_reference:
        for a, b, key in both_sided_renamed:
            if key in hint_reported_pairs:
                # Issue #1484: this exact event is already reported, more
                # specifically, as the `hints.rejected` entry for the
                # caller's own `hints.same_nets` declaration.
                continue
            entries.append(
                _mismatch(
                    CATEGORY_TOPOLOGY,
                    "error",
                    "nets were paired despite a name/identity conflict",
                    "both",
                    net={"layout": _name_or_none(a), "reference": _name_or_none(b)},
                )
            )

    return entries


def _sort_key(mismatch: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    """``(category, side, device.layout, device.reference, net.layout,
    net.reference)`` per spike section 2b, with ``None``/absent fields
    sorted first (empty string) for a total order."""
    device = mismatch["device"] or {}
    net = mismatch["net"] or {}
    return (
        mismatch["category"],
        mismatch["side"],
        device.get("layout") or "",
        device.get("reference") or "",
        net.get("layout") or "",
        net.get("reference") or "",
    )
