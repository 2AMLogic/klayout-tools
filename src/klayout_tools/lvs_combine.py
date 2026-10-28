"""``options.combine_devices`` subsystem of ``klt lvs`` (issue #3711).

Option parsing, the KLayout-internal-error-tolerant ``combine_devices()``
wrapper, the parameter-conservation/correction helpers, series-fold
disclosure, and the netlist flatten/purge helpers. Split out of ``lvs.py``
(the same decomposition as ``lvs_power.py``/``lvs_mismatch.py``); the
names are re-imported there, so ``klayout_tools.lvs`` keeps exposing them.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from .lvs_mismatch import _mismatch

if TYPE_CHECKING:
    import klayout.db as kdb

#: Substring KLayout's own ``Netlist.combine_devices()`` internal-consistency
#: ``RuntimeError`` always carries (issue #466) -- e.g. "Internal error:
#: Terminal still connected after removing device in device combination:
#: name=, circuit=<top>, terminal=E in Netlist.combine_devices". Narrows the
#: ``except RuntimeError`` in :func:`_combine_devices_safely` to *this*
#: KLayout-internal invariant violation (a partial-match device group -- N
#: real + M dummy instances sharing two of three terminals, only the N real
#: ones also matching the third) rather than swallowing an unrelated
#: ``RuntimeError`` some other code path might raise.
_COMBINE_DEVICES_ERROR_MARKER = "Netlist.combine_devices"

#: The identifier fields KLayout embeds in the message of the error
#: :data:`_COMBINE_DEVICES_ERROR_MARKER` matches -- e.g. "``... device
#: combination: name=, circuit=<top>, terminal=E in
#: Netlist.combine_devices``" (issue #1370). ``name`` is the failing
#: device's own name (frequently empty: the device is mid-removal when the
#: invariant trips, so KLayout has nothing to print), ``circuit`` is the
#: circuit that device lives in, and ``terminal`` is the terminal that was
#: still connected. Parsed -- rather than passed through as a raw string --
#: so a ``device.combine_incomplete`` entry carries the machine-readable
#: ``circuit``/``device``/``net`` identifiers every other ``mismatches[]``
#: entry carries, instead of only prose a caller has to regex itself.
_COMBINE_DEVICES_ERROR_FIELDS_RE = re.compile(
    r"name=(?P<device>[^,]*),\s*circuit=(?P<circuit>[^,]*),\s*"
    r"terminal=(?P<terminal>[^\s,]*)"
)

#: Bounded retry budget for :func:`_combine_devices_safely` (issue #1185).
#:
#: KLayout's own ``Netlist.combine_devices()``/``Circuit.combine_devices()``
#: groups combination candidates internally in a C++ ``std::map`` keyed on
#: raw ``db::Net *`` pointer values (see ``Circuit::combine_parallel_devices``
#: in KLayout's ``dbCircuit.cc``), and then repeats its parallel/serial
#: combination passes to a fixed point (``Circuit::combine_devices()``'s own
#: ``while (any) { ... }`` loop). Neither the grouping order nor that
#: fixed-point iteration is exposed to, or overridable from, Python -- there
#: is no argument, hook, or per-device-class ordering control on
#: ``combine_devices()``, and the primitives that actually perform a merge
#: (``join_device``/``join_terminals``) are internal-only, never exposed via
#: the ``klayout.db`` GSI bindings (verified against ``klayout==0.30.10``).
#: Because the grouping key depends on each ``db::Net`` object's *process*
#: heap address (which varies run to run, e.g. via ASLR) rather than any
#: property of the netlist's content, two runs against the byte-identical
#: layout GDS + reference netlist can walk a "partial-match device group"
#: (instances sharing only some, not all, of their matching terminals) in a
#: different candidate order and land on a different outcome -- one run
#: combines cleanly, the next hits the internal-consistency ``RuntimeError``
#: this module already degrades gracefully (issue #466). This is a genuine
#: KLayout-internal limitation this module cannot deterministically avoid
#: (confirmed empirically: neither ``PYTHONHASHSEED`` pinning nor
#: reconstructing the reported partial-match shape from Python reproduces or
#: eliminates it on demand -- see the "combine_devices() partial-match
#: RuntimeError" section of ``tests/test_lvs.py`` for the prior, unsuccessful
#: reproduction attempts).
#:
#: What *is* controllable from Python: each retry below runs against an
#: independent ``Netlist.dup()`` copy, so it samples a fresh instance of
#: KLayout's address-dependent ordering rather than reusing whatever ordering
#: the first attempt happened to get. Retrying trades a bounded amount of
#: extra work (at most this many ``combine_devices()`` calls, each on its own
#: full netlist copy) for a large cut in the *observed* flake rate: at the
#: issue's own reported ~1-in-5 (20%) single-attempt failure rate, 5
#: independent attempts fail together only ~0.2**5 = 0.0032% of the time.
#: This does not make the outcome *provably* deterministic -- it cannot, per
#: the paragraph above -- but it converts a caller-visible ~20% flake rate
#: into one small enough that a stable `klt lvs` "match" is once again a
#: practical automation gate. See ``docs/cli/lvs.md``'s
#: `options.combine_devices` and `device.combine_incomplete` sections for the
#: caller-facing disclosure.
_COMBINE_DEVICES_MAX_ATTEMPTS = 5


def _parse_combine_devices(options: Mapping[str, Any]) -> bool | list[str]:
    """Resolve ``options.combine_devices`` into either a bool (the original
    shape, unchanged) or a normalised list of device-class names (issue
    #1370).

    The list shape restricts combining to only the named device classes --
    the escape hatch for a netlist where KLayout's own
    ``Netlist.combine_devices()`` trips its internal-consistency invariant
    *deterministically* on one device class's partial-match group, leaving
    :func:`_combine_devices_safely`'s retry budget (issue #1185) with nothing
    to resample. Naming only the classes that actually need folding lets the
    compare the caller wanted run at all, instead of degrading the whole run.

    A wrong-shaped value is a clean request error rather than a silent
    coercion, mirroring how ``layout.declared_pins``/``layout.deck_options``
    are validated: ``bool(...)`` would happily turn ``"nfet"``, ``0``, or
    ``{}`` into a verdict-shaping flag the caller never meant. An empty list
    is rejected for the same reason ``declared_pins`` rejects one -- it reads
    as "combine nothing", which is what ``false`` already means, so it is far
    more likely a mistake than an intent.
    """

    from .lvs import LvsError

    value = options.get("combine_devices", False)
    if isinstance(value, bool):
        return value
    if isinstance(value, list):
        if not value or not all(
            isinstance(name, str) and name.strip() for name in value
        ):
            raise LvsError(
                "options.combine_devices, when given as a list, must be a "
                "non-empty list of non-empty device-class name strings (e.g. "
                '["NMOS", "PMOS"]) -- use `false` to disable combining '
                "entirely"
            )
        return [name.strip() for name in value]
    raise LvsError(
        "options.combine_devices must be a boolean or a list of device-class "
        f"name strings; got {type(value).__name__}"
    )


def _parse_combine_devices_max_attempts(options: Mapping[str, Any]) -> int:
    """Resolve ``options.combine_devices_max_attempts`` into a positive int,
    defaulting to :data:`_COMBINE_DEVICES_MAX_ATTEMPTS` when omitted (issue
    #1412).

    Exposes :func:`_combine_devices_safely`'s retry budget -- previously a
    private module constant -- as a caller-configurable knob. That constant's
    own derivation (see its docstring) was tuned against one small fixture (a
    single multi-finger NMOS device, ~200 gate fingers, one device class);
    a caller re-measuring against a much larger, multi-class netlist has
    reported the retry-exhaustion fallback (``device.combine_incomplete``)
    firing far more often than that derivation would predict (see
    ``docs/cli/lvs.md``'s ``options.combine_devices`` section for the
    reported observation) -- this option lets such a caller raise the budget
    without a fork, trading runtime for a lower observed exhaustion rate, and
    lets a caller iterating on a small netlist lower it for faster feedback.

    Malformed input raises :class:`LvsError` rather than silently falling
    back to the default, matching this module's other request-side options
    (``options.parameter_tolerance``, ``options.combine_devices`` itself): a
    retry budget the caller believes is in force but is not would defeat the
    whole point of exposing it.
    """

    from .lvs import LvsError

    if "combine_devices_max_attempts" not in options:
        return _COMBINE_DEVICES_MAX_ATTEMPTS
    value = options["combine_devices_max_attempts"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise LvsError(
            "options.combine_devices_max_attempts must be a positive integer; "
            f"got {value!r}"
        )
    if value < 1:
        raise LvsError(
            f"options.combine_devices_max_attempts must be at least 1; got {value!r}"
        )
    return value


def _parse_combine_devices_per_circuit(
    options: Mapping[str, Any],
) -> dict[str, bool] | None:
    """Resolve ``options.combine_devices_per_circuit`` (issue #1552) into an
    ordered ``{<circuit-name-glob>: <bool>}`` mapping, or ``None`` when the
    key is absent.

    Lets a composed design give each of its own subcircuits its own
    ``combine_devices`` choice -- honored *simultaneously*, unlike the
    single whole-request ``options.combine_devices`` boolean/list this
    module has offered since issue #261, which cannot satisfy two macros
    with opposing needs once they are composed into one top-level design:
    one macro's drawn split/interleaved device legs must be re-lumped
    (``true``) to match a lumped schematic reference, while another macro's
    large group of nominally-identical parallel devices must stay
    individually reported (``false``) to avoid issue #1497's silent
    parameter-corruption risk at scale -- and a shared top-level rail
    connecting the two once composed is enough to make whole-netlist
    ``Netlist.combine_devices()`` treat devices from the *unrelated* macro
    as combine candidates too (see :func:`_combine_circuit_devices_safely`).

    Keys are ``fnmatch.fnmatchcase`` glob patterns matched against each
    side's own ``Circuit.name`` (case-sensitive -- the same convention
    ``extract_abstract.py``'s ``--abstract-cells`` glob matching uses).
    Circuit names read back through ``NetlistSpiceReader`` are upper-cased
    (e.g. a ``.subckt macroa`` declaration reads back as circuit name
    ``"MACROA"``), so a glob written in the source SPICE's own lower/mixed
    case will not match -- see
    :func:`_resolve_combine_devices_per_circuit_targets`. Applied in
    **declaration order** (Python/JSON object key order is preserved end to
    end): the first pattern that matches a given circuit name wins, so a
    caller can list specific circuit names ahead of a catch-all ``"*"`` to
    get "combine everything except these", or list only the circuits that
    need combining to get "combine nothing except these" (the default when
    no pattern matches a circuit at all).

    A wrong-shaped value is a clean request error, matching this module's
    other option-parsing convention (see :func:`_parse_combine_devices`):
    the value must be a non-empty JSON object whose keys are non-empty
    strings and whose values are booleans -- no per-circuit device-class
    restriction in this first increment. Mutually exclusive with a truthy
    ``options.combine_devices`` (enforced by ``run_lvs`` itself, not here,
    so the error names both option paths together).
    """

    from .lvs import LvsError

    if "combine_devices_per_circuit" not in options:
        return None
    value = options["combine_devices_per_circuit"]
    if not isinstance(value, dict) or not value:
        raise LvsError(
            "options.combine_devices_per_circuit must be a non-empty JSON "
            "object mapping a circuit-name glob (fnmatch pattern, matched "
            "case-sensitively) to a boolean"
        )
    result: dict[str, bool] = {}
    for key, entry in value.items():
        if not isinstance(key, str) or not key:
            raise LvsError(
                "options.combine_devices_per_circuit keys must be "
                "non-empty circuit-name glob strings"
            )
        if not isinstance(entry, bool):
            raise LvsError(
                f"options.combine_devices_per_circuit[{key!r}] must be a "
                f"boolean; got {type(entry).__name__}"
            )
        result[key] = entry
    return result


def _resolve_combine_devices_per_circuit_targets(
    per_circuit: Mapping[str, bool], netlist: kdb.Netlist
) -> tuple[list[str], list[str]]:
    """Resolve ``per_circuit``'s glob keys against ``netlist``'s own circuit
    names (issue #1552), returning ``(to_combine, unmatched_patterns)``.

    ``to_combine`` is the ordered list of this netlist's own circuit names
    whose first matching glob (in ``per_circuit``'s own declaration order --
    see :func:`_parse_combine_devices_per_circuit`) mapped to ``True``.
    ``unmatched_patterns`` is every glob in ``per_circuit`` that matched zero
    circuits on this netlist -- surfaced by the caller as a
    ``combine_devices_per_circuit.unmatched`` warning so a typo'd circuit
    name (most commonly: written in the request's own source-file case
    instead of the upper-cased form ``NetlistSpiceReader`` reads circuit
    names back as) silently restricting to nothing is visible, mirroring
    :func:`_validate_combine_device_classes`'s equivalent guard for
    ``options.combine_devices``'s list shape -- except a pattern legitimately
    naming a circuit that exists on only one side (e.g. a reference-only
    lumped macro with no layout-side counterpart yet) is not itself a
    mistake, so this only warns, it never raises.
    """
    to_combine: list[str] = []
    matched: dict[str, bool] = dict.fromkeys(per_circuit, False)
    for circuit in netlist.each_circuit():
        for pattern, enabled in per_circuit.items():
            if fnmatch.fnmatchcase(circuit.name, pattern):
                matched[pattern] = True
                if enabled:
                    to_combine.append(circuit.name)
                break
    unmatched_patterns = [pattern for pattern, hit in matched.items() if not hit]
    return to_combine, unmatched_patterns


def _unmatched_combine_devices_per_circuit_warnings(
    unmatched_patterns: Sequence[str], side: str
) -> list[dict[str, Any]]:
    """Build the ``combine_devices_per_circuit.unmatched`` warning entries
    for ``unmatched_patterns`` (issue #1552) -- see
    :func:`_resolve_combine_devices_per_circuit_targets`."""

    from .lvs import CATEGORY_COMBINE_DEVICES_PER_CIRCUIT_UNMATCHED

    return [
        _mismatch(
            CATEGORY_COMBINE_DEVICES_PER_CIRCUIT_UNMATCHED,
            "warning",
            f"options.combine_devices_per_circuit pattern {pattern!r} "
            f"matched no circuit on the {side} netlist -- check the pattern "
            "against this side's own circuit names (NetlistSpiceReader "
            "upper-cases circuit names read back from SPICE, e.g. "
            '"macroa" -> "MACROA")',
            side,
            details={"pattern": pattern},
        )
        for pattern in unmatched_patterns
    ]


def _combine_circuit_devices_safely(
    netlist: kdb.Netlist,
    side: str,
    circuit_names: Sequence[str],
    max_attempts: int = _COMBINE_DEVICES_MAX_ATTEMPTS,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Like :func:`_combine_devices_safely`, but combines only the named
    circuits, one at a time, via ``Circuit.combine_devices()`` rather than
    the whole-netlist ``Netlist.combine_devices()`` -- issue #1552's
    ``options.combine_devices_per_circuit``.

    Combining one circuit at a time, each scoped to its own
    ``Circuit.combine_devices()`` call, means devices in a circuit named
    ``False`` (or not named at all) can never be treated as combine
    candidates alongside devices in a *different* circuit just because a
    shared top-level rail happens to connect them once both are wired into
    one composed design -- the core failure mode #1552 reports against the
    whole-netlist ``Netlist.combine_devices()`` once
    ``options.flatten_layout``/``options.flatten_reference`` (issue #1085)
    collapses circuit boundaries away. This is why ``run_lvs`` calls this
    *before* either flatten runs, while circuit boundaries still exist to
    scope against.

    Each named circuit gets its own bounded retry against fresh
    ``Netlist.dup()`` copies of the *whole* netlist (issue #1185's
    nondeterminism mitigation, scoped down to run per-circuit instead of
    once for the whole netlist): a ``RuntimeError`` combining one circuit
    never discards another circuit's already-succeeded combine, and each
    circuit's own retry budget is independent. Unlike
    :func:`_combine_devices_safely`'s whole-netlist symmetric degrade
    (issue #1370), a circuit that exhausts its retry budget here is simply
    left uncombined and reported -- there is no "roll both sides back"
    behaviour here, because each circuit's own combine choice is already an
    independent, caller-declared decision, not a single request-wide flag
    both sides must agree on. Unlike :func:`_combine_devices_safely`, the
    caught ``RuntimeError`` is not narrowed to
    :data:`_COMBINE_DEVICES_ERROR_MARKER` (that marker text is
    ``Netlist.combine_devices()``'s own; whether ``Circuit.combine_devices()``
    raises byte-identical text is unconfirmed) -- any ``RuntimeError`` here
    is presumed to be this same KLayout-internal partial-match invariant,
    since nothing else in this narrowly-scoped call can raise one.

    Returns ``(warnings, combined_circuit_names)``: ``warnings`` is the list
    of ``device.combine_incomplete`` warnings (one per circuit that
    exhausted its retry budget) to append to ``mismatches[]`` -- empty when
    every named circuit combined cleanly (the common case).
    ``combined_circuit_names`` is the subset of ``circuit_names`` that
    combined *cleanly* (no retry-budget exhaustion) -- issue #1557: this is
    what ``run_lvs`` scopes its post-combine resistor
    ``fixed_offset_ohm``/capacitor ``C`` sum-conservation corrections to, so
    a circuit whose combine was reported incomplete is never "corrected"
    against a fold that never actually completed, mirroring the
    whole-netlist ``combine_devices`` path's own ``if not
    combine_incomplete:`` gating (see ``run_lvs``).
    """

    from .lvs import CATEGORY_DEVICE_COMBINE_INCOMPLETE

    warnings: list[dict[str, Any]] = []
    combined_circuit_names: list[str] = []
    other_side = "reference" if side == "layout" else "layout"
    for name in circuit_names:
        for attempt in range(max_attempts):
            is_last_attempt = attempt == max_attempts - 1
            # Every attempt but the last runs against a fresh, independent
            # whole-netlist copy -- `netlist` itself is never mutated until
            # either a copy succeeds (assigned back below) or the last
            # attempt runs directly against it, mirroring
            # `_combine_devices_safely`'s own retry discipline.
            candidate = netlist if is_last_attempt else netlist.dup()
            circuit = candidate.circuit_by_name(name)
            if circuit is None:  # pragma: no cover -- defensive, shouldn't happen
                break
            try:
                circuit.combine_devices()
            except RuntimeError as exc:
                if not is_last_attempt:
                    continue
                attempts_disclosure = (
                    f" after {max_attempts} attempts against independent "
                    "netlist copies (issue #1185)"
                    if max_attempts > 1
                    else ""
                )
                warnings.append(
                    _mismatch(
                        CATEGORY_DEVICE_COMBINE_INCOMPLETE,
                        "warning",
                        "options.combine_devices_per_circuit could not fully "
                        f"combine devices in circuit {name!r} on the {side} "
                        f"netlist{attempts_disclosure}: KLayout's "
                        "Circuit.combine_devices() hit an internal-"
                        "consistency error on a partial-match device group "
                        "(instances sharing only some, not all, of their "
                        "matching terminals) and stopped -- devices already "
                        "combined in this circuit before the error remain "
                        f"combined, the rest were left uncombined ({exc})",
                        side,
                        circuit={side: name, other_side: None},
                        details={"klayout_error": str(exc)},
                    )
                )
            else:
                if candidate is not netlist:
                    netlist.assign(candidate)
                combined_circuit_names.append(name)
                break
    return warnings, combined_circuit_names


def _validate_combine_device_classes(
    class_names: list[str],
    netlists: Mapping[str, kdb.Netlist],
) -> None:
    """Reject a list-valued ``options.combine_devices`` naming a device class
    that exists on **neither** side (issue #1370).

    Without this, a typo (``"nfet_o1v8"`` for ``"nfet_01v8"``) silently
    restricts combining to nothing at all and the run reports a full
    ``device.unmatched`` cascade that looks exactly like a real design error
    -- the "silently no-op" failure mode this option exists to avoid. Matched
    case-insensitively against every device class registered on either
    netlist, because ``NetlistSpiceReader`` upper-cases class names read back
    from SPICE (``RES_HIGH_PO``) while an in-process extraction deck writes
    them as declared (``res_high_po``) -- the same case-insensitive
    convention :func:`~klayout_tools.extract.apply_resistor_fixed_offset_corrections`
    already uses (issue #585).

    Only "on neither side" is an error: a class present on just one side is
    legitimate (a layout-only parasitic flavour, a reference-only lumped
    model), and restricting to it there is exactly the asymmetry a caller
    reaching for this option may be trying to express.
    """

    from .lvs import LvsError

    known: dict[str, str] = {}
    for netlist in netlists.values():
        for device_class in netlist.each_device_class():
            known.setdefault(device_class.name.lower(), device_class.name)
    unknown = [name for name in class_names if name.lower() not in known]
    if not unknown:
        return
    available = ", ".join(sorted(known.values())) or "(none)"
    raise LvsError(
        "options.combine_devices names device class(es) present in neither "
        f"the layout nor the reference netlist: {', '.join(sorted(unknown))} "
        f"-- device classes available across both sides: {available}"
    )


def _restrict_device_combination(netlist: kdb.Netlist, class_names: list[str]) -> None:
    """Disable device combination on every device class of ``netlist`` that
    is *not* named in ``class_names`` (issue #1370).

    ``klayout.db.Netlist.combine_devices()`` takes no arguments and offers no
    per-class filter (verified against ``klayout==0.30.10``), but each
    ``DeviceClass`` carries the two predicates its combination passes consult
    -- ``supports_parallel_combination`` and ``supports_serial_combination``.
    Clearing both on the other classes makes KLayout's own pass skip them, so
    only the named classes are folded. Matched case-insensitively for the
    same ``NetlistSpiceReader``-upper-cases-class-names reason
    :func:`_validate_combine_device_classes` documents.

    Deliberately one-way: both predicates are **write-only** in KLayout's
    Python bindings (reading either raises ``AttributeError: ... is not
    readable``), so their prior values cannot be saved and restored. That is
    safe here because the only consumers of these predicates are
    ``Circuit::combine_devices()`` -- which is exactly what this is scoping --
    and the device *extractor*, which has already finished by the time
    ``run_lvs`` reaches this point. ``NetlistComparer`` never reads them, and
    every netlist this touches is a per-run, in-memory object discarded when
    ``run_lvs`` returns. ``run_lvs`` additionally holds an untouched
    ``Netlist.dup()`` snapshot of each side taken *before* combining, which is
    what the symmetric degrade restores, so even the predicates come back
    unmodified on that path.
    """
    wanted = {name.lower() for name in class_names}
    for device_class in netlist.each_device_class():
        if device_class.name.lower() in wanted:
            continue
        device_class.supports_parallel_combination = False
        device_class.supports_serial_combination = False


def _combine_failure_identifiers(
    netlist: kdb.Netlist, side: str, message: str
) -> dict[str, Any]:
    """Turn KLayout's own ``combine_devices()`` internal-consistency error
    *message* into the machine-readable ``circuit``/``device``/``net``
    ``mismatches[]`` fields (issue #1370), resolving each against ``netlist``
    where the message alone is not enough.

    The message embeds three fields --
    ``name=<device>, circuit=<circuit>, terminal=<terminal>`` (see
    :data:`_COMBINE_DEVICES_ERROR_FIELDS_RE`) -- which are parsed and then
    resolved as far as the (partially combined) netlist allows:

    * ``circuit`` -- taken straight from the message, on ``side``'s key.
    * ``device`` -- the named device, when KLayout printed a name *and* that
      device is still present in the named circuit; its ``class`` is that
      device's device-class name.
    * ``net`` -- the net wired to the reported terminal of that device.

    KLayout frequently reports an **empty** ``name=`` (the device is mid-
    removal when the invariant trips, so there is nothing to print). In that
    case the device's own name is unrecoverable, but the *class* usually is
    not: if exactly one device class present in the named circuit declares a
    terminal with the reported name, that class is the failing group's class,
    and it is reported as ``device.class`` with both name keys ``null`` (the
    same shape ``device.body_unverified`` already uses for a class-level,
    instance-less finding). Anything that cannot be resolved this way stays
    ``null`` -- never guessed.

    Returns the ``_mismatch`` keyword arguments as a dict, always including
    a ``details`` block with the parsed terminal name and KLayout's raw
    message, so a caller has the unparsed original too.
    """
    match = _COMBINE_DEVICES_ERROR_FIELDS_RE.search(message)
    device_name = (match.group("device").strip() if match else "") or None
    circuit_name = (match.group("circuit").strip() if match else "") or None
    terminal_name = (match.group("terminal").strip() if match else "") or None

    other = "reference" if side == "layout" else "layout"
    circuit_field: dict[str, Any] | None = None
    device_field: dict[str, Any] | None = None
    net_field: dict[str, Any] | None = None

    circuit = netlist.circuit_by_name(circuit_name) if circuit_name else None
    if circuit_name:
        circuit_field = {side: circuit_name, other: None}

    device = None
    if circuit is not None and device_name:
        device = circuit.device_by_name(device_name)
    if device is not None:
        device_class = device.device_class()
        device_field = {
            side: device.expanded_name(),
            other: None,
            "class": device_class.name,
        }
        if terminal_name:
            net = _net_for_terminal_or_none(device, terminal_name)
            if net is not None:
                net_field = {side: net.expanded_name(), other: None}
    elif circuit is not None and terminal_name:
        class_names = _device_classes_declaring_terminal(circuit, terminal_name)
        if len(class_names) == 1:
            device_field = {side: None, other: None, "class": class_names[0]}

    return {
        "circuit": circuit_field,
        "device": device_field,
        "net": net_field,
        "details": {"terminal": terminal_name, "klayout_error": message},
    }


def _net_for_terminal_or_none(device: Any, terminal_name: str) -> Any:
    """``device.net_for_terminal(terminal_name)``, or ``None`` when this
    device's class declares no such terminal (KLayout raises rather than
    returning ``nil`` for an unknown terminal name)."""
    try:
        return device.net_for_terminal(terminal_name)
    except Exception:  # pragma: no cover -- defensive: unknown terminal name
        return None


def _device_classes_declaring_terminal(
    circuit: kdb.Circuit, terminal_name: str
) -> list[str]:
    """The distinct device-class names used by devices in ``circuit`` that
    declare a terminal called ``terminal_name`` -- the fallback identifier
    :func:`_combine_failure_identifiers` uses when KLayout reported an empty
    device ``name=``. Sorted for determinism."""
    names: set[str] = set()
    for device in circuit.each_device():
        device_class = device.device_class()
        if any(
            terminal.name == terminal_name
            for terminal in device_class.terminal_definitions()
        ):
            names.add(device_class.name)
    return sorted(names)


def _device_class_net_signatures(
    netlist: kdb.Netlist,
) -> dict[tuple[Any, ...], set[str]]:
    """Which device classes sit on each (circuit, connected-nets) signature.

    Only classes that share their terminal-name tuple with at least one other
    class are tracked -- a class with a terminal set of its own can never be
    folded into another class's devices, so it cannot be a victim of the
    cross-class fold :func:`_cross_class_folds` looks for. Nets are identified
    by ``Net.expanded_name()`` (see :func:`_combine_group_key` for why) and the
    signature sorts them, so a combine's terminal swap (a symmetric device
    survivor connected the other way round) does not change it.
    """
    by_terminals: dict[tuple[str, ...], list[str]] = {}
    for device_class in netlist.each_device_class():
        terminal_names = tuple(t.name for t in device_class.terminal_definitions())
        by_terminals.setdefault(terminal_names, []).append(device_class.name)
    tracked = {
        name for names in by_terminals.values() if len(names) > 1 for name in names
    }
    signatures: dict[tuple[Any, ...], set[str]] = {}
    if not tracked:
        return signatures
    for circuit in netlist.each_circuit():
        for device in circuit.each_device():
            device_class = device.device_class()
            if device_class.name not in tracked:
                continue
            nets = []
            for terminal in device_class.terminal_definitions():
                net = _net_for_terminal_or_none(device, terminal.name)
                nets.append(net.expanded_name() if net is not None else "")
            key = (circuit.name, tuple(sorted(nets)))
            signatures.setdefault(key, set()).add(device_class.name)
    return signatures


def _cross_class_folds(
    pre_combine: Mapping[tuple[Any, ...], set[str]],
    post_combine: Mapping[tuple[Any, ...], set[str]],
) -> list[tuple[str, tuple[str, ...], list[str]]]:
    """Every signature where a device class present before combining is gone
    afterwards while another class's device still sits on the same nets, as
    ``(circuit, nets, lost class names)``.

    A parallel fold always leaves one survivor of the *same* class on the
    group's nets, and a series fold only removes a class from a signature whose
    interior net carried nothing else -- so a class that vanishes from a
    signature that is still occupied can only have been absorbed by a device of
    a different class. KLayout's ``Netlist.combine_devices()`` does exactly
    that, intermittently, on the netlist the device extractors built in place
    (two capacitor classes in parallel, ``tl::id_of`` of the classes
    apparently colliding): the lost class's devices are summed into the other
    class's survivor. See :func:`_combine_devices_safely`.
    """
    found: list[tuple[str, tuple[str, ...], list[str]]] = []
    for key, classes in pre_combine.items():
        remaining = post_combine.get(key)
        if not remaining:
            continue
        lost = sorted(classes - remaining)
        if lost:
            found.append((key[0], key[1], lost))
    return found


def _move_devices_to_class(
    netlist: kdb.Netlist, from_class_name: str, to_class: Any
) -> None:
    """Re-create every device of the class named ``from_class_name`` on
    ``to_class`` -- same circuit, name, parameters and terminal nets -- and
    drop the original (see :func:`_renew_conflicting_device_classes`)."""
    for circuit in netlist.each_circuit():
        for device in list(circuit.each_device()):
            old_class = device.device_class()
            if old_class.name != from_class_name:
                continue
            replacement = circuit.create_device(to_class, device.name)
            for terminal in old_class.terminal_definitions():
                net = _net_for_terminal_or_none(device, terminal.name)
                if net is not None:
                    replacement.connect_terminal(terminal.name, net)
            for parameter in old_class.parameter_definitions():
                replacement.set_parameter(
                    parameter.name, device.parameter(parameter.name)
                )
            circuit.remove_device(device)


def _renew_conflicting_device_classes(
    netlist: kdb.Netlist, signatures: Mapping[tuple[Any, ...], set[str]]
) -> int:
    """Rebuild, in place, all but one of the device classes that have devices
    on the very same nets (the multi-class entries of ``signatures``), on
    freshly constructed device classes. Returns how many were rebuilt.

    ``Netlist.combine_devices()`` tells device classes apart by an internal
    ``tl::UniqueId`` that a *copied* class (the device extractors' classes,
    and every ``Netlist.dup()`` clone) carries uninitialised, so two such
    classes can compare equal and one class's devices get folded into the
    other's survivor (issue #3019, see :func:`_cross_class_folds`). That is
    heap-state dependent -- resampling on further copies does not reliably
    cure it -- whereas a class built by its Python constructor always gets a
    properly initialised, unique id. With at most one original class left per
    conflicting set, every pair has a fresh member and cannot be confused.

    Each rebuilt class takes over its devices (same circuit, name, parameters
    and terminal nets) under the original name; the emptied original is
    renamed out of the way rather than removed (``Netlist.remove`` refuses
    classes of a copied netlist), and the comparer reports it like any other
    device class that has no devices. Only KLayout's built-in class types can
    be rebuilt; any other class is left alone and
    :func:`_cross_class_folds` still guards the result.
    """
    import klayout.db as kdb_module

    renewable = (
        kdb_module.DeviceClassBJT3Transistor,
        kdb_module.DeviceClassBJT4Transistor,
        kdb_module.DeviceClassCapacitor,
        kdb_module.DeviceClassCapacitorWithBulk,
        kdb_module.DeviceClassDiode,
        kdb_module.DeviceClassInductor,
        kdb_module.DeviceClassMOS3Transistor,
        kdb_module.DeviceClassMOS4Transistor,
        kdb_module.DeviceClassResistor,
        kdb_module.DeviceClassResistorWithBulk,
    )
    conflicting = sorted(
        {name for names in signatures.values() if len(names) > 1 for name in names}
    )
    taken = {device_class.name for device_class in netlist.each_device_class()}
    renewed = 0
    for old_class in list(netlist.each_device_class()):
        # The first conflicting class stays as it is (see above).
        if old_class.name not in conflicting[1:]:
            continue
        if type(old_class) not in renewable:
            continue
        name = old_class.name
        retired_name = f"{name}__renewed"
        while retired_name in taken:
            retired_name += "_"
        taken.add(retired_name)
        old_class.name = retired_name
        new_class = type(old_class)()
        new_class.name = name
        new_class.description = old_class.description
        netlist.add(new_class)
        _move_devices_to_class(netlist, retired_name, new_class)
        renewed += 1
    return renewed


def _combine_devices_safely(
    netlist: kdb.Netlist,
    side: str,
    max_attempts: int = _COMBINE_DEVICES_MAX_ATTEMPTS,
    *,
    device_classes: list[str] | None = None,
) -> dict[str, Any] | None:
    """Call ``netlist.combine_devices()``, retrying against independent
    netlist copies to counter its run-to-run nondeterminism, and degrading
    gracefully instead of letting KLayout's internal-consistency
    ``RuntimeError`` abort the whole ``klt lvs`` run when every attempt is
    unlucky (issues #466, #1185).

    KLayout's own ``Netlist.combine_devices()`` can raise::

        RuntimeError: Internal error: Terminal still connected after
        removing device in device combination: name=, circuit=<top>,
        terminal=E in Netlist.combine_devices

    on a *partial-match* device group: N real (matching-relevant) instances
    plus M dummy instances that all share two of three terminals (e.g. a
    bipolar device's base and collector, tied to an array's common well and
    substrate), but only the N real instances additionally share the third
    (e.g. an emitter bussed to one signal net) -- the M dummy instances each
    have their own, mutually distinct, third terminal. That is a
    ``klayout.db`` behavior this module merely surfaces, not a defect this
    module's own code introduces, so it is not this module's job to make the
    partial-match combine itself succeed -- only to keep an unhandled
    internal exception from breaking this command's JSON-envelope contract
    (``docs/json-contract.md``, CLAUDE.md's "JSON is the contract").

    **Why this retries (issue #1185):** the same layout GDS + reference
    netlist pair can flip between a clean combine and this exact
    ``RuntimeError`` across separate ``klt lvs`` invocations, even though
    nothing about the inputs changed. The root cause is internal to
    KLayout's C++ implementation, not this module or Python's own hashing
    (:data:`_COMBINE_DEVICES_MAX_ATTEMPTS`'s docstring has the full
    investigation) -- and it is not something this module can override or
    reorder from the Python API. What it *can* do is resample: each retry
    below runs ``combine_devices()`` against its own ``Netlist.dup()`` copy
    of the untouched, not-yet-combined netlist, so a failure on one attempt
    does not poison the next -- every attempt is an independent trial of
    KLayout's internal (address-dependent) ordering. The first attempt that
    completes without raising is adopted (via ``Netlist.assign()``) as this
    function's result; note this **replaces** ``netlist``'s circuits in
    place, so any ``kdb.Circuit`` reference a caller took from ``netlist``
    *before* calling this function is invalidated the moment a retry
    succeeds and must be re-fetched afterwards (e.g. via
    ``netlist.circuit_by_name(name)``) -- ``run_lvs`` does this immediately
    after both sides' calls to this function.

    **Why the first attempt runs in place (issue #2374):** a ``dup()`` copy is
    not merely an *independent* trial of KLayout's address-dependent ordering
    -- it is a *worse* one. A freshly allocated copy walks its nets in an
    order that bears no relation to the order they were created in, and on
    roughly a quarter of copies that lands ``combine_devices()`` in a
    different accumulation branch that returns **normally** with silently
    wrong combined parameters (issue #2374 measured ~24% of ``dup()`` copies
    of a ``DeviceClassBJT3Transistor`` array producing an *inverted*
    parameter accumulation, versus 1000/1000 correct runs against the
    netlist itself). So attempt 0 runs directly against ``netlist``, exactly
    as this module behaved before issue #1185 added the retry, and only the
    *retries* -- reached solely when attempt 0 raised the ``RuntimeError``
    above -- resample against independent copies. Those copies are taken from
    a pristine ``snapshot`` captured before attempt 0 ran, not from the
    partially-merged ``netlist``, so every retry is still a genuinely
    independent trial rather than a retry-from-partial-damage. (The residual
    exposure on the retry path is covered separately, and independently of
    *why* KLayout got it wrong, by :func:`_correct_combine_parameters`'s
    post-combine conservation check.)

    Because attempt 0 runs in place, ``netlist`` keeps whatever that attempt
    managed to merge before hitting the error when *every* attempt fails --
    byte-for-byte the same shape issue #466 originally shipped: this
    netlist's remaining, not-yet-combined devices are simply left as
    individual devices for the rest of this run, exactly as they would be
    with ``options.combine_devices: false``. (``run_lvs`` no longer *ships*
    that partially-combined state to the comparer -- issue #1370's symmetric
    degrade rolls both sides back to their pre-combine snapshots when this
    function reports a failure -- but this function's own contract is
    unchanged, so a direct caller still gets exactly the issue #466 shape.)

    ``device_classes`` (issue #1370) restricts combining to the named device
    classes, via :func:`_restrict_device_combination` applied to whichever
    netlist this attempt runs against. ``None`` (the default) combines every
    class, i.e. the plain ``options.combine_devices: true`` behaviour.
    Applied per attempt rather than once up front so a retry against a fresh
    ``Netlist.dup()`` copy -- which carries its own, unrestricted copies of
    the device classes -- is restricted the same way the first attempt was.
    The pristine ``snapshot`` the retries are copied from is captured
    *before* that restriction is applied to ``netlist``, so a retry restricts
    its own copy from the same unrestricted starting point attempt 0 had.

    Returns a ``severity: "warning"`` ``mismatches[]`` entry
    (``category: "device.combine_incomplete"``) to append to the report when
    every attempt hit the error, or ``None`` when some attempt (usually the
    first) completed cleanly. That entry carries the failing attempt's own
    ``circuit``/``device``/``net`` identifiers wherever KLayout's message and
    the netlist can supply them (issue #1370, see
    :func:`_combine_failure_identifiers`) -- not just the raw exception text
    in ``description``. Only catches this one KLayout-internal error
    shape (matched narrowly on ``_COMBINE_DEVICES_ERROR_MARKER``, the
    ``"...in Netlist.combine_devices"`` suffix every instance of it
    carries) -- any other ``RuntimeError``, on any attempt, propagates
    unchanged immediately (no retry), so an unrelated failure is never
    silently swallowed or masked as this issue's shape.
    """

    from .lvs import CATEGORY_DEVICE_COMBINE_INCOMPLETE

    # Issue #2374: captured before attempt 0 mutates `netlist` in place, so
    # every *retry* below copies from the pristine pre-combine state rather
    # than from a partially-merged one. Skipped entirely when there is no
    # retry budget to spend (`max_attempts == 1`), which keeps the
    # single-attempt path allocation-for-allocation identical to the
    # pre-#1185 behaviour.
    pre_combine_signatures = _device_class_net_signatures(netlist)
    # Issue #3019: classes with devices on the same nets must not be copies of
    # each other's identity -- rebuild all but one (before the snapshot, so the
    # retries below start from the rebuilt classes too).
    _renew_conflicting_device_classes(netlist, pre_combine_signatures)
    snapshot = netlist.dup() if max_attempts > 1 else None
    last_error = ""
    cross_class_fold = False
    for attempt in range(max_attempts):
        # Attempt 0 runs directly against `netlist` -- the ordering KLayout
        # produces for a netlist walked in the order it was built, which is
        # the one that combines correctly (issue #2374). Only the retries,
        # reached solely after attempt 0 raised, resample against independent
        # copies of the pristine snapshot (issue #1185's mitigation).
        # `snapshot` is never `None` here: `attempt > 0` implies
        # `max_attempts > 1`, which is exactly the condition it was taken
        # under.
        candidate = netlist if attempt == 0 or snapshot is None else snapshot.dup()
        if candidate is not netlist:
            # A `Netlist.dup()` clone carries copied (uninitialised) class
            # identities again -- see `_renew_conflicting_device_classes`.
            _renew_conflicting_device_classes(
                candidate, _device_class_net_signatures(candidate)
            )
        if device_classes is not None:
            _restrict_device_combination(candidate, device_classes)
        try:
            candidate.combine_devices()
        except RuntimeError as exc:
            if _COMBINE_DEVICES_ERROR_MARKER not in str(exc):
                raise
            last_error = str(exc)
            cross_class_fold = False
            continue
        folds = _cross_class_folds(
            pre_combine_signatures, _device_class_net_signatures(candidate)
        )
        if folds:
            # Issue #3019: no exception, but devices of one class were
            # absorbed into another class's survivor -- a wrong combined
            # netlist that would be compared as if it were right. Treated
            # like the partial-match error: resample on a fresh copy.
            circuit_name, _nets, lost = folds[0]
            last_error = (
                f"device class(es) {', '.join(lost)} were folded into a "
                f"different device class in circuit {circuit_name!r} "
                f"({len(folds)} net group(s) affected)"
            )
            cross_class_fold = True
            continue
        if candidate is not netlist:
            netlist.assign(candidate)
        return None

    # Every attempt hit KLayout's partial-match error. `netlist` still
    # carries attempt 0's own in-place partial merge (issue #466's shape);
    # the identifiers below are therefore resolved against `netlist`, the
    # state the caller is actually left holding.
    attempts_disclosure = (
        f" after {max_attempts} attempts against independent netlist "
        "copies (issue #1185)"
        if max_attempts > 1
        else ""
    )
    restriction_disclosure = (
        f" (restricted to device class(es) {', '.join(device_classes)})"
        if device_classes is not None
        else ""
    )
    failure_description = (
        "KLayout's Netlist.combine_devices() folded devices of one class into a "
        "different device class sitting on the same nets (a wrong combined "
        "netlist, not an error KLayout raised), so the combined result was "
        f"discarded ({last_error})"
        if cross_class_fold
        else "KLayout's Netlist.combine_devices() hit an internal-consistency error "
        "on a partial-match device group (instances sharing only "
        "some, not all, of their matching terminals) and stopped -- "
        "devices it had already combined before the error remain "
        "combined, but the rest of this netlist's devices were left "
        f"uncombined ({last_error})"
    )
    return _mismatch(
        CATEGORY_DEVICE_COMBINE_INCOMPLETE,
        "warning",
        "options.combine_devices could not fully combine devices on "
        f"the {side} netlist{restriction_disclosure}"
        f"{attempts_disclosure}: {failure_description}",
        side,
        # Issue #1370: machine-readable identifiers for the failure, resolved
        # against the netlist the caller keeps rather than left `None`.
        **_combine_failure_identifiers(netlist, side, last_error),
    )


#: Issues #1497/#2374: per-device-class parameter-conservation rules for
#: KLayout's own **parallel** `combine_devices()` fold, as
#: `(device-class attribute names, summed parameters, preserved parameters)`.
#: Each rule says what a combined device's parameters must be, given the
#: pre-combine group it absorbed:
#:
#: * a *summed* parameter's post-combine value is the pre-combine group's
#:   total (it is a conserved quantity -- combining can only redistribute it
#:   across fewer surviving devices, never change it);
#: * a *preserved* parameter's post-combine value is the group's single
#:   shared value (it describes a region the parallel instances have in
#:   common, so it does not accumulate) -- checked only when every member of
#:   the group actually agrees on it, since nothing can be asserted about a
#:   group whose members differ.
#:
#: Capacitors (issue #1497): `C` is a simple per-device sum under parallel
#: combination (`CapacitorDeviceCombiner::parallel` in KLayout's own
#: `dbNetlistDeviceClasses.cc` -- see also `extract.py`'s
#: `_apply_device_parameter_corrections` docstring, which asserts the same
#: for `DeviceExtractorCapacitor`-produced devices). `A`/`P` are secondary
#: and likewise sum, but issue #1497 only ever observed `C` going wrong, so
#: they are deliberately left unasserted here.
#:
#: Bipolars (issue #2374): a parallel `DeviceClassBJT3Transistor` array folds
#: with the emitter parameters summed (`AE`, `PE`, `NE`) and the shared
#: base/collector region parameters left at the array's common value (`AB`,
#: `PB`, `AC`, `PC`) -- verified directly against `klayout==0.30.10` by
#: combining an N-instance parallel array and reading the surviving device
#: back. `DeviceClassBJT4Transistor` (the substrate-terminal flavour)
#: subclasses `DeviceClassBJT3Transistor`, declares the identical parameter
#: set, and folds identically; it is listed explicitly anyway so the rule
#: does not silently depend on that subclass relationship.
#:
#: Deliberately narrow -- a device class is listed here only when *every*
#: combination KLayout will perform on it is a parallel one with a known
#: accumulation rule:
#:
#: * resistors: a parallel combination is not a simple sum
#:   (`1/R = 1/R1 + 1/R2`) and a series one is, so neither `DeviceClass
#:   Resistor` nor `DeviceClassResistorWithBulk` belongs here;
#: * MOS transistors: `combine_devices()` folds them both in parallel (`W`
#:   sums) and in series (`L` sums), and a series fold's surviving device has
#:   terminal connectivity no pre-combine group shares, so a
#:   connectivity-keyed conservation rule cannot describe it.
_COMBINE_PARAMETER_CONSERVATION_RULES: tuple[
    tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...
] = (
    (
        ("DeviceClassCapacitor", "DeviceClassCapacitorWithBulk"),
        ("C",),
        (),
    ),
    (
        ("DeviceClassBJT3Transistor", "DeviceClassBJT4Transistor"),
        ("AE", "PE", "NE"),
        ("AB", "PB", "AC", "PC"),
    ),
)


def _combine_conservation_rules(
    kdb_module: Any,
) -> list[tuple[tuple[type, ...], tuple[str, ...], tuple[str, ...]]]:
    """Resolve :data:`_COMBINE_PARAMETER_CONSERVATION_RULES`'s device-class
    *names* into the live `klayout.db` classes, once per correction pass."""
    return [
        (
            tuple(getattr(kdb_module, name) for name in class_names),
            summed,
            preserved,
        )
        for class_names, summed, preserved in _COMBINE_PARAMETER_CONSERVATION_RULES
    ]


def _combine_conservation_rule_for(
    device_class: Any,
    rules: Sequence[tuple[tuple[type, ...], tuple[str, ...], tuple[str, ...]]],
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """The `(summed, preserved)` parameter names governing `device_class`,
    or `None` when no conservation rule covers it (the common case -- MOS
    transistors, resistors, diodes and every custom class)."""
    for classes, summed, preserved in rules:
        if isinstance(device_class, classes):
            return summed, preserved
    return None


def _combine_parameter_tolerance(expected: float) -> float:
    """Comparison tolerance for one conserved parameter.

    A relative tolerance, floored by a tiny absolute term for the
    ``expected == 0`` edge case -- *not* ``1e-15`` (a plausible
    femtofarad-scale `C` value in its own right, which would mask exactly the
    single-instance-vs-summed-total discrepancy this check exists to catch,
    e.g. `1e-15` vs. a summed `2e-15`).
    """
    return max(1e-21, abs(expected) * 1e-9)


def _combine_group_key(
    circuit_name: str, device: Any, device_class: Any
) -> tuple[Any, ...]:
    """A key identifying the group of parallel devices `device` combines
    with: the same circuit, the same device-class name, and the same set of
    (terminal name, connected net) pairs. A parallel fold only ever merges
    devices that agree on every one of those, so one key names exactly one
    combinable group -- on either side of the combine.

    Identifies each terminal's net by `Net.expanded_name()` -- always
    non-empty (KLayout synthesizes a name like `"$1"` for an internal,
    otherwise-unnamed net) and unaffected by `combine_devices()` itself
    (which only removes devices, never renames or removes nets, until
    `run_lvs`'s later `_purge_emptied_nets` step -- which runs *after* this
    correction, see `run_lvs`) -- rather than by net object identity or
    `Net.cluster_id` (an *L2N* net-region correlation id that is `0` for
    every net on a hand-built or `NetlistSpiceReader`-read netlist, not a
    stable per-net enumerator -- verified against `klayout==0.30.10`), since
    a pre-combine snapshot's net objects are never the same Python/C++
    objects as the post-combine netlist's own nets.
    """
    terminals = []
    for terminal in device_class.terminal_definitions():
        net = _net_for_terminal_or_none(device, terminal.name)
        terminals.append(
            (terminal.name, net.expanded_name() if net is not None else None)
        )
    return (circuit_name, device_class.name, tuple(sorted(terminals)))


def _combine_parameter_expectations(
    netlist: Any,
    rules: Sequence[tuple[tuple[type, ...], tuple[str, ...], tuple[str, ...]]],
) -> dict[tuple[Any, ...], dict[str, Any]]:
    """What every conservation-governed device group in `netlist` says a
    combined device folded from it must look like.

    Keyed by :func:`_combine_group_key`, each value is
    ``{"summed": {name: total}, "common": {name: value | None}}`` -- the
    group's per-parameter total for the rule's *summed* parameters, and its
    single shared value for the rule's *preserved* parameters (``None`` when
    the group's members do not all agree on one, in which case nothing can be
    asserted about that parameter and it is skipped).
    """
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for circuit in netlist.each_circuit():
        for device in circuit.each_device():
            device_class = device.device_class()
            rule = _combine_conservation_rule_for(device_class, rules)
            if rule is None:
                continue
            summed_names, preserved_names = rule
            key = _combine_group_key(circuit.name, device, device_class)
            entry = groups.get(key)
            if entry is None:
                entry = groups[key] = {
                    "summed": {name: 0.0 for name in summed_names},
                    "common": {
                        name: device.parameter(name) for name in preserved_names
                    },
                }
            for name in summed_names:
                entry["summed"][name] += device.parameter(name)
            for name in preserved_names:
                common = entry["common"][name]
                if common is None:
                    continue
                value = device.parameter(name)
                if abs(value - common) > _combine_parameter_tolerance(common):
                    entry["common"][name] = None
    return groups


def _combine_group_survivors(
    netlist: Any,
    rules: Sequence[tuple[tuple[type, ...], tuple[str, ...], tuple[str, ...]]],
    circuit_names: Sequence[str] | None,
) -> list[tuple[tuple[Any, ...], str, Any, Any, tuple[str, ...], tuple[str, ...]]]:
    """Every conservation-governed device in `netlist` that is the **only**
    survivor under its :func:`_combine_group_key`, as
    ``(key, circuit name, device, device class, summed, preserved)``.

    A group with more than one survivor is dropped here rather than in the
    caller: it did not fold to a single device, so the group-total rule is
    not assertable against any of its members -- see
    :func:`_correct_combine_parameters`'s docstring.
    """
    found: list[tuple[tuple[Any, ...], str, Any, Any, tuple[str, ...], tuple[str, ...]]]
    found = []
    counts: dict[tuple[Any, ...], int] = {}
    for circuit in netlist.each_circuit():
        if circuit_names is not None and circuit.name not in circuit_names:
            continue
        for device in circuit.each_device():
            device_class = device.device_class()
            rule = _combine_conservation_rule_for(device_class, rules)
            if rule is None:
                continue
            key = _combine_group_key(circuit.name, device, device_class)
            counts[key] = counts.get(key, 0) + 1
            found.append((key, circuit.name, device, device_class, *rule))
    return [entry for entry in found if counts[entry[0]] == 1]


def _combine_expected_values(
    group: Mapping[str, Any],
    summed: Sequence[str],
    preserved: Sequence[str],
) -> list[tuple[str, float]]:
    """The `(parameter, expected value)` pairs a single-survivor device must
    satisfy: its group's total for each *summed* parameter, and its group's
    common value for each *preserved* parameter the group agrees on."""
    values = [(name, group["summed"][name]) for name in summed]
    values += [
        (name, group["common"][name])
        for name in preserved
        if group["common"][name] is not None
    ]
    return values


def _apply_combine_parameter_expectations(
    circuit_name: str,
    device: Any,
    device_class: Any,
    expectations: Sequence[tuple[str, float]],
) -> list[dict[str, Any]]:
    """Overwrite, in place, every parameter of `device` that disagrees with
    its expected value beyond floating-point tolerance, and describe each
    correction for the `device.combine_parameter_corrected` entry's
    ``details.corrected[]``."""
    corrected: list[dict[str, Any]] = []
    for name, expected_value in expectations:
        actual_value = device.parameter(name)
        if abs(actual_value - expected_value) <= _combine_parameter_tolerance(
            expected_value
        ):
            continue
        device.set_parameter(name, expected_value)
        corrected.append(
            {
                "circuit": circuit_name,
                "device": device.name or None,
                "class": device_class.name,
                "parameter": name,
                "before": actual_value,
                "after": expected_value,
            }
        )
    return corrected


def _correct_combine_parameters(
    pre_combine_netlist: Any,
    netlist: Any,
    side: str,
    *,
    circuit_names: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    """Repair a combined device's parameters left inconsistent by
    `Netlist.combine_devices()` (issues #1497, #2374).

    Every group of parallel devices sharing one (circuit, device class,
    terminal-net-connectivity) identity has a *known* post-combine parameter
    set, given only the group's pre-combine members -- see
    :data:`_COMBINE_PARAMETER_CONSERVATION_RULES` for the per-class rules and
    their provenance. A capacitor group's `C` is a conserved total (combining
    can only redistribute it across fewer surviving devices, never change
    it); a parallel bipolar array's emitter parameters (`AE`/`PE`/`NE`) are
    likewise totals, while its shared base/collector region parameters
    (`AB`/`PB`/`AC`/`PC`) stay at the array's single common value.

    This compares `netlist` (just combined, potentially in place) against
    `pre_combine_netlist` (the untouched snapshot taken before combining ran)
    and overwrites any post-combine parameter that disagrees with its group's
    rule beyond floating-point tolerance -- independent of *why* KLayout's
    own combine got it wrong, and regardless of whether that specific failure
    mode can be forced on demand (see `tests/test_lvs.py`'s combine-
    correction tests, which simulate both reported shapes via a
    monkeypatched `combine_devices()` rather than a live reproduction).

    Only groups that actually **folded to a single surviving device** are
    checked. A group KLayout left uncombined -- because combining was
    restricted away from its class by a list-valued
    ``options.combine_devices`` (issue #1370), or because it is a lone
    device to begin with -- keeps more than one post-combine device under its
    key, and asserting a group *total* against each of those individually
    would corrupt correct values rather than repair wrong ones. That
    single-survivor condition is exactly the shape both reported failures
    take (issue #1497's folded capacitor group, issue #2374's folded bipolar
    array), so nothing this check exists to catch is given up by it.

    Called only when `_combine_devices_safely` reported success (`None`) on
    both sides -- a hard combine failure already rolls both sides back to
    their pre-combine snapshots (issue #1370's symmetric degrade) before
    this would ever run, so this function never has to reconcile a
    combine-in-progress netlist against its own pre-combine snapshot.

    Returns a `severity: "warning"` `mismatches[]` entry
    (:data:`CATEGORY_DEVICE_COMBINE_PARAMETER_CORRECTED`) naming every
    corrected `(device, parameter)` pair, or `None` when every post-combine
    parameter already agreed with its pre-combine group (the expected, common
    case: this is a defensive invariant check applied unconditionally, not a
    response to a failure this module has observed directly against its own
    synthetic netlists -- see this module's docstring / issues #1497 and
    #2374 for the investigations).

    ``circuit_names`` (issue #1557): restricts the post-combine scan to just
    these circuit names -- ``options.combine_devices_per_circuit``'s own
    per-circuit combine, unlike the whole-netlist ``options.combine_devices``
    path (which always passes ``None``, the default, and scans every
    circuit), only wants this check applied to the circuit(s) it actually
    combined *cleanly*. A circuit this ran over unnecessarily would be a
    no-op anyway (its post-combine parameters already agree with their own
    pre-combine groups, since nothing touched them), but the explicit
    restriction keeps a circuit whose combine was reported incomplete
    untouched too, matching this function's own docstring contract ("called
    only when ... reported success") even though nothing here rolls an
    incomplete per-circuit combine back the way the whole-netlist symmetric
    degrade does.
    """

    import klayout.db as kdb_module

    from .lvs import CATEGORY_DEVICE_COMBINE_PARAMETER_CORRECTED

    rules = _combine_conservation_rules(kdb_module)
    expected = _combine_parameter_expectations(pre_combine_netlist, rules)
    survivors = _combine_group_survivors(netlist, rules, circuit_names)

    corrected: list[dict[str, Any]] = []
    for key, circuit_name, device, device_class, summed, preserved in survivors:
        group = expected.get(key)
        if group is None:
            # Defensive only: combining can only merge existing pre-combine
            # connectivity groups, never invent a new one, so every
            # post-combine key should already be present above.
            continue
        corrected += _apply_combine_parameter_expectations(
            circuit_name,
            device,
            device_class,
            _combine_expected_values(group, summed, preserved),
        )

    if not corrected:
        return None

    names = ", ".join(
        f"{entry['circuit']}/{entry['device'] or '<unnamed>'}.{entry['parameter']} "
        f"({entry['before']!r} -> {entry['after']!r})"
        for entry in corrected
    )
    return _mismatch(
        CATEGORY_DEVICE_COMBINE_PARAMETER_CORRECTED,
        "warning",
        "options.combine_devices produced one or more combined devices whose "
        "parameters did not agree with the parallel group KLayout's own "
        "Netlist.combine_devices() folded them from (a summed parameter not "
        "equal to the group's pre-combine total, or a shared-region "
        f"parameter not equal to the group's common value), on the {side} "
        "netlist -- corrected in place (see docs/cli/lvs.md, "
        f"'device.combine_parameter_corrected'): {names}",
        side,
        details={"corrected": corrected},
    )


def _series_chain_lengths(pre_circuit: Any, class_name: str, interior: set[str]):
    """Map ``(net_a, net_b)`` (sorted expanded net names) to the lengths of
    every pure series chain of ``class_name`` devices in `pre_circuit`
    running between those two non-interior nets through `interior` nets
    only (a lone device between two non-interior nets is a chain of 1)."""
    edges: dict[str, list[tuple[Any, str]]] = {}
    for device in pre_circuit.each_device():
        if device.device_class().name != class_name:
            continue
        net_a, net_b = device.net_for_terminal(0), device.net_for_terminal(1)
        if net_a is None or net_b is None:
            continue
        name_a, name_b = net_a.expanded_name(), net_b.expanded_name()
        edges.setdefault(name_a, []).append((device, name_b))
        edges.setdefault(name_b, []).append((device, name_a))

    chains: dict[tuple[str, str], list[int]] = {}
    for start, incident in edges.items():
        if start in interior:
            continue
        for first_device, first_next in incident:
            length, previous, here = 1, first_device, first_next
            while here in interior:
                onward = [e for e in edges.get(here, []) if e[0] is not previous]
                if len(onward) != 1:
                    break  # a branch or dead end, not a pure series chain
                previous, here = onward[0]
                length += 1
            else:
                key = (min(start, here), max(start, here))
                # Each chain is walked from both ends; keep one direction.
                if start <= here:
                    chains.setdefault(key, []).append(length)
    return chains


def _resistor_head_term_ohm(device: Any, fixed_offset: float, end_term: float) -> float:
    """``fixed_offset_ohm + end_term_ohm_um / W`` for one resistor `device`
    (the head term the #559 correction charges once; ``end_term_ohm_um`` is
    skipped when the class has no usable ``W``, as the correction does)."""
    width = device.parameter("W") if end_term else None
    return fixed_offset + (end_term / width if width else 0.0)


def _series_fold_offset_absorbed_warnings(
    pre_combine_netlist: Any,
    netlist: Any,
    deck: Any,
    *,
    combine_incomplete: bool = False,
) -> list[dict[str, Any]]:
    """One ``severity: "warning"``
    :data:`CATEGORY_DEVICE_SERIES_FOLD_OFFSET_ABSORBED` entry per post-combine
    resistor that is a fold of N>1 series primitives of a class with a
    nonzero ``fixed_offset_ohm`` (issue #3017).

    The deferred correction (#559, ``apply_resistor_fixed_offset_corrections``)
    charges the offset once per surviving device, so the fold absorbs
    ``(N-1) * fixed_offset_ohm`` the drawn chain physically carries. N is
    derived from `pre_combine_netlist` topology: the survivor's two end nets
    are walked back through the nets the fold emptied. Only a **pure series
    chain** is reported -- when more than one chain (or a parallel primitive)
    joins the same two end nets, N cannot be attributed to the survivor, so no
    entry is emitted rather than a guessed one.

    Call after the correction ran, so ``fraction_of_r`` uses the final ``R``.
    Returns ``[]`` when `combine_incomplete` (a symmetric degrade folded
    nothing).
    """

    from .lvs import CATEGORY_DEVICE_SERIES_FOLD_OFFSET_ABSORBED

    offsets = {
        resistor.name.lower(): (resistor.fixed_offset_ohm, resistor.end_term_ohm_um)
        for resistor in deck.resistors
        if resistor.fixed_offset_ohm or resistor.end_term_ohm_um
    }
    if combine_incomplete or not offsets:
        return []

    warnings: list[dict[str, Any]] = []
    for circuit in netlist.each_circuit():
        pre_circuit = pre_combine_netlist.circuit_by_name(circuit.name)
        if pre_circuit is None:
            continue
        post_nets = {net.expanded_name() for net in circuit.each_net()}
        interior = {net.expanded_name() for net in pre_circuit.each_net()} - post_nets
        if not interior:
            continue
        chain_cache: dict[str, Any] = {}
        for device in circuit.each_device():
            device_class = device.device_class()
            terms = offsets.get(device_class.name.lower())
            if terms is None:
                continue
            fixed_offset, end_term = terms
            offset = _resistor_head_term_ohm(device, fixed_offset, end_term)
            net_a, net_b = device.net_for_terminal(0), device.net_for_terminal(1)
            if net_a is None or net_b is None:
                continue
            if device_class.name not in chain_cache:
                chain_cache[device_class.name] = _series_chain_lengths(
                    pre_circuit, device_class.name, interior
                )
            name_a, name_b = net_a.expanded_name(), net_b.expanded_name()
            key = (min(name_a, name_b), max(name_a, name_b))
            lengths = chain_cache[device_class.name].get(key, [])
            if len(lengths) != 1 or lengths[0] < 2:
                continue
            folded = lengths[0]
            absorbed = (folded - 1) * offset
            r_final = device.parameter("R")
            fraction = absorbed / r_final if r_final else None
            warnings.append(
                _mismatch(
                    CATEGORY_DEVICE_SERIES_FOLD_OFFSET_ABSORBED,
                    "warning",
                    f"options.combine_devices folded {folded} series "
                    f"{device_class.name} primitives in circuit "
                    f"{circuit.name!r} into one device ({device.name or '<unnamed>'}) "
                    f"and charged the head term ({offset!r} ohm: fixed_offset_ohm "
                    f"+ end_term_ohm_um / W) once, so {absorbed!r} ohm "
                    "((N-1) * head term) the drawn chain "
                    "physically carries was not charged to the compare "
                    "(see docs/cli/lvs.md, "
                    "'device.series_fold_offset_absorbed')",
                    "layout",
                    details={
                        "circuit": circuit.name,
                        "device": device.name or None,
                        "class": device_class.name,
                        "folded_primitives": folded,
                        "fixed_offset_ohm": fixed_offset,
                        "end_term_ohm": offset - fixed_offset,
                        "absorbed_offset_ohm": absorbed,
                        "fraction_of_r": fraction,
                    },
                )
            )
    return warnings


def _flatten_netlist_safely(netlist: kdb.Netlist, side: str) -> dict[str, Any] | None:
    """Call ``netlist.flatten()``, in-process, on ``side``'s netlist (issue
    #1085's ``options.flatten_reference``/``options.flatten_layout``).

    ``klt extract`` always extracts a **flat** layout-side netlist (a single
    top circuit; see ``extract.py``'s own module docstring) -- so a
    hierarchical reference netlist (one leaf ``.subckt`` plus N instance
    calls of it, the shape a large regular macro's schematic naturally
    takes) can never structurally match it: ``NetlistComparer`` sees a flat
    top circuit with N x (devices per leaf) devices on one side and a top
    circuit with *zero* devices plus N subcircuit calls on the other, and
    reports a hard, undiagnosable ``topology`` mismatch on both sides (see
    this module's docstring, and ``docs/cli/lvs.md``'s "`layout.flatten` /
    `reference.flatten`" section) -- not a partial result a caller can act
    on.

    ``Netlist.flatten()`` is KLayout's own whole-netlist flatten: "After
    calling this method, only the top circuits will remain" -- every
    subcircuit-call instance is substituted in-place, in-process. Called
    here, before :func:`_select_circuit`, so a caller-declared ``top`` name
    still resolves normally afterwards whenever it already named a genuine
    top-level circuit (the common case -- the design's own top level, not
    an interior leaf that flatten would have inlined away).

    Symmetric and opt-in on **both** sides (``options.flatten_reference``
    for the reference netlist, ``options.flatten_layout`` for the layout
    netlist -- the pre-extracted ``layout.netlist`` shape can be
    hierarchical too, issue #1085's item 2), so the default (both flags
    omitted) is byte-identical to today's behaviour and a caller who
    genuinely wants a hierarchy-preserving compare is never silently
    flattened out from under them.

    Returns a ``severity: "warning"`` ``mismatches[]`` entry
    (``category: "topology.flattened"``) disclosing how many circuits were
    collapsed, so a ``"match"`` reached after flattening is never silently
    indistinguishable from one reached against the netlist's original
    hierarchy -- or ``None`` when the netlist already had only its top
    circuit(s) (nothing to flatten, e.g. an already-flat ``klt extract``
    layout side), in which case there is nothing to disclose.
    """

    from .lvs import CATEGORY_TOPOLOGY_FLATTENED

    circuits_before = sum(1 for _ in netlist.each_circuit())
    netlist.flatten()
    circuits_after = sum(1 for _ in netlist.each_circuit())
    if circuits_before == circuits_after:
        return None
    return _mismatch(
        CATEGORY_TOPOLOGY_FLATTENED,
        "warning",
        f"options.flatten_{side} flattened the {side} netlist before "
        f"comparing: {circuits_before} circuit(s) were collapsed into "
        f"{circuits_after} top-level circuit(s), substituting every "
        "subcircuit-call instance in place -- this compare verified "
        "topology only after removing the original hierarchy boundaries on "
        f'this side (see docs/cli/lvs.md, "topology.flattened")',
        side,
    )


def _hierarchy_mismatch_warnings(
    layout_netlist: kdb.Netlist,
    reference_netlist: kdb.Netlist,
    flatten_reference: bool,
) -> list[dict[str, Any]]:
    """Issue #4548: advisory ``topology.hierarchy_mismatch`` warning.

    A flat layout netlist (``klt extract`` is flat-only) against a
    hierarchical reference reports only per-circuit ``topology`` "could not
    be matched" errors, which read like a connectivity problem. When the
    layout side is a single circuit, the reference still has more than one
    circuit (call after ``_prune_extra_top_circuits`` on both sides) and
    ``options.flatten_reference`` is not set, disclose that the cause is
    structural and name the option that compares the two. Returns a
    one-entry list, else an empty one. Warning-only: never changes ``status``.
    """

    from .lvs import CATEGORY_TOPOLOGY_HIERARCHY_MISMATCH

    if flatten_reference:
        return []
    layout_circuits = sum(1 for _ in layout_netlist.each_circuit())
    reference_circuits = sum(1 for _ in reference_netlist.each_circuit())
    if layout_circuits != 1 or reference_circuits <= 1:
        return []
    return [
        _mismatch(
            CATEGORY_TOPOLOGY_HIERARCHY_MISMATCH,
            "warning",
            f"the layout netlist is flat (1 circuit) but the reference netlist is "
            f"hierarchical ({reference_circuits} circuits); per-circuit topology "
            "errors here may stem from that structural difference rather than a "
            "connectivity defect -- retry with options.flatten_reference: true to "
            "compare the flattened reference against the flat layout "
            '(see docs/cli/lvs.md, "topology.hierarchy_mismatch")',
            "reference",
        )
    ]


def _purge_emptied_nets(netlist: kdb.Netlist) -> None:
    """Remove nets that ``combine_devices()`` emptied -- nets left with no
    terminals, no pins, and no subcircuit pins after matched device arrays
    were folded (issue #500).

    ``Netlist.combine_devices()`` folds combinable device groups (e.g. a
    series string of N identical devices into one device) but leaves the
    N-1 interior nodes it disconnected behind in the circuit, each now with
    zero connections. Those nets are not part of the netlist's topology by
    any definition, yet they still inflate ``counts.nets.*`` (computed off
    ``each_net()``) and surface as spurious ``net.unmatched`` findings in
    ``mismatches[]`` that no caller can act on -- there is nothing to fix
    about a net with nothing attached to it. Dropping them makes both counts
    and mismatches honest, symmetric with the combine step itself: a caller
    reading ``counts.nets.layout`` to judge how far apart two netlists are
    sees the post-combine topology, not ``combine_devices()``'s internal
    bookkeeping.

    Deliberately narrower than KLayout's own ``Circuit.purge_nets()``: it
    only removes nets that are simultaneously terminal-less, pin-less, and
    subcircuit-pin-less, so a genuinely-unused *top-level pin*'s net (a net
    with zero terminals but a real pin attached) is never dropped -- that
    would silently change ``counts.pins.*`` and remove a pin the comparer
    must still see, which is outside this fix's scope. Applied per circuit
    across the whole netlist, mirroring ``combine_devices()``'s own
    netlist-wide scope so interior nodes emptied in subcircuits below the
    selected top are cleaned up too.
    """
    for circuit in netlist.each_circuit():
        emptied = [
            net
            for net in circuit.each_net()
            if net.terminal_count() == 0
            and net.pin_count() == 0
            and net.subcircuit_pin_count() == 0
        ]
        for net in emptied:
            circuit.remove_net(net)
