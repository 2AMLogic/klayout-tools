"""Compare an extracted/reference netlist pair headlessly and report
structured mismatches, mirroring ``klt drc``'s ``violations[]`` shape.

Pure library: :func:`run_lvs` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints, mirroring ``drc.py`` /
``extract.py``. Serialisation and human-readable formatting live in the CLI
command module (``cli/lvs_cmd.py``).

This is phase 3 of Epic #153 (``klt lvs``/``klt extract``), the build carried
by the accepted spike, ``docs/design/lvs-extraction-spike.md`` -- read that
document first; it settles the engine choice (KLayout's own
``klayout.db.NetlistComparer``/``NetlistSpiceReader``) and the request/
response contract this module implements (its section 2b, ``klt lvs``).
Scope: **schematic-equivalent, topological compare only** -- this module does
not read ``matched_group_id`` (out of scope per the spike's section 4) and
does not do any layout-vs-layout geometric diffing.

Unlike ``klt extract``/``klt drc``, ``klt lvs`` takes a **request document**
(like ``klt sim``/``klt gen``), not positional file args -- it binds two
netlist inputs plus optional matching hints, richer than a flag line carries
cleanly.

Engine: ``klayout.db.NetlistComparer`` fed by a custom
``GenericNetlistCompareLogger`` subclass (see ``_make_compare_logger``) that
captures every raw compare event (net/device/pin/circuit mismatches,
parameter/class differences) into structured Python objects, post-processed
into the documented ``mismatches[]`` shape. The authoritative match/mismatch
verdict is always ``NetlistComparer.compare()``'s own boolean return value -- never
re-derived from how many mismatch entries this module manages to classify.
This is the module's central correctness invariant (the issue's own
complexity note: a comparator report that is too permissive could silently
report "match" on a real mismatch): ``status`` can only be ``"match"`` when
the engine itself says the netlists are equivalent, regardless of any gap in
this module's own event-to-category mapping.

Net-merge/net-split classification (a known simplification): KLayout's
comparer log stream does not label a net mismatch as "merged" or "split" --
it only reports individual net/device mismatch events. This module
distinguishes them heuristically from the *pattern* of events within one
compare run (see ``_classify_net_mismatches``): a leftover, one-sided net on
the **layout** side (no reference counterpart) co-occurring with a
differently-named both-sided pairing is classified ``net.split`` (one
reference net's role divided across more layout nets than expected); the
mirror case on the **reference** side is ``net.merged``. A one-sided leftover
net with no co-occurring renamed pairing is the unambiguous ``net.unmatched``
case. This heuristic is verified against synthetic merge/split fixtures in
``tests/test_lvs.py`` but is not a formal proof for arbitrary multi-defect
inputs -- documented here as a known limitation, the same way ``extract.py``
documents its own curated-deck connectivity-fidelity limits.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import TYPE_CHECKING, Any

from .extract import ExtractError, extract_netlist_from_layout

if TYPE_CHECKING:
    import klayout.db as kdb

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: ``klayout`` (in-process ``NetlistComparer``) is the only implemented
#: engine in v1 -- the spike's engine survey found no reason to wrap a
#: second one (see this module's docstring and the spike's section 1).
SUPPORTED_ENGINES = ("klayout",)

#: Stable mismatch-category ids (spike section 2b). Never renumbered/
#: repurposed once shipped -- the same contract guarantee a DRC rule id
#: carries.
CATEGORY_NET_UNMATCHED = "net.unmatched"
CATEGORY_NET_MERGED = "net.merged"
CATEGORY_NET_SPLIT = "net.split"
CATEGORY_DEVICE_UNMATCHED = "device.unmatched"
CATEGORY_DEVICE_CLASS = "device.class"
CATEGORY_DEVICE_PROPERTY = "device.property"
CATEGORY_PIN_UNMATCHED = "pin.unmatched"
CATEGORY_TOPOLOGY = "topology"

#: Parameter-name -> reported-property-name map, mirroring ``extract.py``'s
#: own ``w_um``/``l_um`` convention for the two parameters every consumer
#: cares about; every other declared parameter is reported under its own
#: lower-cased name (see this module's docstring: no unit suffix is assumed
#: beyond the two names ``extract.py`` already documents in micrometres).
_PARAM_DISPLAY_NAMES = {"W": "w_um", "L": "l_um"}

#: A parameter difference below this (absolute-or-relative) threshold is
#: floating-point round-trip noise (e.g. a SPICE writer's decimal
#: truncation), not a real design difference -- mirrors ``extract.py``'s own
#: ``_PARAM_PRECISION_UM`` rounding intent, applied here as a compare-time
#: tolerance instead of an output rounding.
_PARAM_ABS_EPSILON = 1e-9
_PARAM_REL_EPSILON = 1e-6


class LvsError(Exception):
    """Raised when an LVS run cannot even be attempted: a missing/malformed
    request file, an unresolvable/unreadable layout or reference netlist, an
    unknown extraction deck, or an unsupported engine.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback. Distinct from a documented ``status: "mismatch"`` response --
    that is a trustworthy verdict, not a failure to run (see this module's
    docstring).
    """


def load_request(request_path: str) -> dict[str, Any]:
    """Read and minimally validate a ``klt lvs`` request JSON file.

    Raises :class:`LvsError` if the file is missing/unreadable, not valid
    JSON, or missing a required top-level field (``layout``, ``reference``).
    Does not require a ``schema`` field, matching ``klt sim``'s
    ``load_request`` (user-authored input, never emitted by this tool).
    """
    if not os.path.exists(request_path):
        raise LvsError(f"file not found: {request_path}")
    if os.path.isdir(request_path):
        raise LvsError(f"not a file: {request_path}")

    try:
        with open(request_path, encoding="utf-8") as handle:
            request = json.load(handle)
    except (OSError, UnicodeDecodeError) as exc:
        raise LvsError(f"could not read request file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise LvsError(f"request file is not valid JSON: {exc}") from exc

    if not isinstance(request, dict):
        raise LvsError("request file must contain a JSON object")

    for field in ("layout", "reference"):
        if field not in request:
            raise LvsError(f"request is missing required field: {field}")

    return request


def run_lvs(request_path: str) -> dict[str, Any]:
    """Run the netlist compare declared by the request at ``request_path``.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/lvs.md`` / ``docs/design/lvs-extraction-spike.md`` section
    2b). Raises :class:`LvsError` for anything that prevents a trustworthy
    verdict from being produced at all (bad request, unresolvable layout/
    reference input, unknown deck, unsupported engine, engine error) --
    a documented ``status: "mismatch"`` is a successful run, not an error
    (see this module's docstring).
    """
    request = load_request(request_path)
    request_dir = os.path.dirname(os.path.abspath(request_path))

    engine = request.get("engine", "klayout")
    if engine not in SUPPORTED_ENGINES:
        raise LvsError(
            f"unsupported engine '{engine}' (supported: {', '.join(SUPPORTED_ENGINES)})"
        )

    layout_spec = request["layout"]
    if not isinstance(layout_spec, dict):
        raise LvsError("request.layout must be a JSON object")
    reference_spec = request["reference"]
    if not isinstance(reference_spec, dict):
        raise LvsError("request.reference must be a JSON object")

    options = request.get("options") or {}
    keep_extracted = bool(options.get("keep_extracted", False))

    import klayout.db as kdb

    (
        layout_netlist,
        layout_echo,
        layout_hash_source,
        extracted_netlist_path,
    ) = _resolve_layout(layout_spec, request_dir, keep_extracted)

    reference_netlist_path = _require_path(
        reference_spec, "netlist", "reference", request_dir
    )
    reference_echo = reference_spec["netlist"]
    reference_netlist = _read_reference_netlist(reference_netlist_path)

    layout_circuit = _select_circuit(layout_netlist, layout_spec.get("top"), "layout")
    reference_circuit = _select_circuit(
        reference_netlist, reference_spec.get("top"), "reference"
    )

    _prune_extra_top_circuits(layout_netlist, layout_circuit)
    _prune_extra_top_circuits(reference_netlist, reference_circuit)

    logger = _make_compare_logger()
    comparer = kdb.NetlistComparer(logger)
    _apply_hints(
        comparer, request.get("hints") or {}, layout_circuit, reference_circuit
    )

    # `logger` is already bound via the `NetlistComparer(logger)` constructor
    # above, so the 2-arg overload is used here (not the 3-arg one, which
    # would pass a second, redundant logger reference).
    compare_result = comparer.compare(layout_netlist, reference_netlist)

    mismatches = _build_mismatches(logger, layout_netlist, reference_netlist)
    if not compare_result and not mismatches:
        # Safety net for the correctness invariant this module's docstring
        # states: `compare()` is always authoritative. If the engine says
        # "mismatch" but this module's own event classification produced
        # nothing (a gap in event coverage, not a clean run), never let the
        # response silently look like a match -- report a generic, honest
        # finding instead of dropping the verdict.
        mismatches = [
            {
                "category": CATEGORY_TOPOLOGY,
                "severity": "error",
                "description": (
                    "netlists do not match (no further detail available "
                    "from the comparer's event log)"
                ),
                "side": "both",
                "net": None,
                "device": None,
                "property": None,
            }
        ]

    status = "match" if compare_result else "mismatch"

    category_counts: dict[str, int] = {}
    for mismatch in mismatches:
        category_counts[mismatch["category"]] = (
            category_counts.get(mismatch["category"], 0) + 1
        )

    counts = {
        "nets": {
            "layout": sum(1 for _ in layout_circuit.each_net()),
            "reference": sum(1 for _ in reference_circuit.each_net()),
            "matched": logger.matched_nets,
        },
        "devices": {
            "layout": sum(1 for _ in layout_circuit.each_device()),
            "reference": sum(1 for _ in reference_circuit.each_device()),
            "matched": logger.matched_devices,
        },
        "pins": {
            "layout": layout_circuit.pin_count(),
            "reference": reference_circuit.pin_count(),
            "matched": logger.matched_pins,
        },
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "engine": engine,
        "layout": layout_echo,
        "reference": reference_echo,
        "top": layout_circuit.name,
        "status": status,
        "mismatch_count": len(mismatches),
        "category_counts": dict(sorted(category_counts.items())),
        "counts": counts,
        "environment": {
            "engine": engine,
            "engine_version": _engine_version(),
            "layout_sha256": _sha256_file(layout_hash_source),
            "reference_sha256": _sha256_file(reference_netlist_path),
            "extracted_netlist": extracted_netlist_path,
        },
        "mismatches": mismatches,
    }


# --------------------------------------------------------------------------- #
# Request-side resolution: layout (inline extraction or pre-extracted), reference
# --------------------------------------------------------------------------- #


def _resolve_relative(path: str, base_dir: str) -> str:
    """Expand env vars/``~`` in ``path``; join relative paths against ``base_dir``
    (same idiom as ``sim.py``'s ``_resolve_relative``)."""
    expanded = os.path.expanduser(os.path.expandvars(path))
    if os.path.isabs(expanded):
        return expanded
    return os.path.join(base_dir, expanded)


def _require_path(spec: dict[str, Any], field: str, side: str, request_dir: str) -> str:
    value = spec.get(field)
    if value is None:
        raise LvsError(f"request.{side}.{field} is required")
    resolved = _resolve_relative(value, request_dir)
    if not os.path.isfile(resolved):
        raise LvsError(f"{side} {field} not found: {resolved}")
    return resolved


def _resolve_layout(
    layout_spec: dict[str, Any], request_dir: str, keep_extracted: bool
) -> tuple[kdb.Netlist, str, str, str | None]:
    """Resolve ``request.layout`` to ``(netlist, echo, hash_source_path,
    extracted_netlist_path_or_none)``.

    Two supported shapes (spike section 2b): ``{"file", "deck", "top"}`` runs
    inline extraction (composing ``extract.py``'s core function); ``{"netlist",
    "top"}`` reads a pre-extracted SPICE file directly. Exactly one of
    ``file``/``netlist`` must be given.
    """
    import klayout.db as kdb

    has_file = "file" in layout_spec
    has_netlist = "netlist" in layout_spec
    if has_file and has_netlist:
        raise LvsError("request.layout must have exactly one of 'file' or 'netlist'")
    if not has_file and not has_netlist:
        raise LvsError("request.layout requires 'file' or 'netlist'")

    if has_file:
        layout_file = _resolve_relative(layout_spec["file"], request_dir)
        if not os.path.isfile(layout_file):
            raise LvsError(f"layout file not found: {layout_file}")
        deck_name = layout_spec.get("deck")
        if not deck_name:
            raise LvsError("request.layout.deck is required when layout.file is given")

        try:
            netlist, top_cell_name, _dbu_um, _warnings = extract_netlist_from_layout(
                layout_file, deck_name, top=layout_spec.get("top")
            )
        except ExtractError as exc:
            raise LvsError(str(exc)) from exc

        extracted_netlist_path: str | None = None
        if keep_extracted:
            extracted_netlist_path = os.path.join(
                request_dir, ".klt", "lvs", f"{top_cell_name}.spice"
            )
            os.makedirs(os.path.dirname(extracted_netlist_path), exist_ok=True)
            writer = kdb.NetlistSpiceWriter()
            writer.use_net_names = True
            try:
                netlist.write(
                    extracted_netlist_path,
                    writer,
                    f"extracted by klt lvs (deck {deck_name})",
                )
            except Exception as exc:
                raise LvsError(
                    f"could not write extracted netlist "
                    f"'{extracted_netlist_path}': {exc}"
                ) from exc

        return netlist, layout_spec["file"], layout_file, extracted_netlist_path

    layout_netlist_path = _require_path(layout_spec, "netlist", "layout", request_dir)
    netlist = kdb.Netlist()
    reader = kdb.NetlistSpiceReader()
    try:
        netlist.read(layout_netlist_path, reader)
    except Exception as exc:
        raise LvsError(
            f"could not parse layout netlist '{layout_netlist_path}': {exc}"
        ) from exc

    return netlist, layout_spec["netlist"], layout_netlist_path, None


def _read_reference_netlist(path: str) -> kdb.Netlist:
    """Parse ``path`` via ``NetlistSpiceReader``.

    Note: on genuinely malformed input, ``NetlistSpiceReader`` does not raise
    -- it prints a ``"Warning: Line ignored..."`` diagnostic (to the
    process's real stdout, a pre-existing KLayout engine behaviour this
    module does not attempt to suppress) and returns an empty netlist (zero
    circuits). That surfaces reliably as :class:`LvsError` a moment later, in
    :func:`_select_circuit`'s "no top circuit" check -- exercised directly in
    ``tests/test_lvs.py``.
    """
    import klayout.db as kdb

    netlist = kdb.Netlist()
    reader = kdb.NetlistSpiceReader()
    try:
        netlist.read(path, reader)
    except Exception as exc:
        raise LvsError(f"could not parse reference netlist '{path}': {exc}") from exc
    return netlist


def _select_circuit(netlist: kdb.Netlist, top: str | None, side: str) -> kdb.Circuit:
    """Pick the circuit to compare: ``top`` by name if given, else the
    netlist's sole top circuit (an ambiguous/missing choice is an
    :class:`LvsError`) -- the netlist-compare analogue of ``extract.py``'s
    ``_resolve_top_cell``."""
    if top is not None:
        circuit = netlist.circuit_by_name(top)
        if circuit is None:
            raise LvsError(f"top cell/subcircuit '{top}' not found in {side} netlist")
        return circuit

    top_circuits = list(netlist.top_circuits())
    if len(top_circuits) == 0:
        raise LvsError(f"{side} netlist has no top circuit")
    if len(top_circuits) > 1:
        names = ", ".join(sorted(circuit.name for circuit in top_circuits))
        raise LvsError(
            f"{side} netlist has {len(top_circuits)} top circuits ({names}); "
            "pass 'top' to select one"
        )
    return top_circuits[0]


def _prune_extra_top_circuits(netlist: kdb.Netlist, keep: kdb.Circuit) -> None:
    """Remove every top-level circuit other than ``keep`` from ``netlist``.

    A reference/layout netlist file may declare unrelated top-level circuits
    (e.g. a library SPICE file with several ``.subckt``s, only one of which
    is the design under comparison). Left in place, each would surface as
    its own spurious top-level circuit mismatch. Safe to prune: by
    definition, a *top* circuit is not referenced by anything else in the
    netlist, so removing one never affects ``keep``'s own hierarchy.
    """
    for circuit in list(netlist.top_circuits()):
        if circuit.cell_index != keep.cell_index:
            netlist.purge_circuit(circuit)


# --------------------------------------------------------------------------- #
# Hints: same_nets / equivalent_pins
# --------------------------------------------------------------------------- #


def _apply_hints(
    comparer: kdb.NetlistComparer,
    hints: dict[str, Any],
    layout_circuit: kdb.Circuit,
    reference_circuit: kdb.Circuit,
) -> None:
    """Wire ``request.hints`` into the comparer, per spike section 2b.

    ``same_nets``: ``[[layout_net_name, reference_net_name], ...]`` -- ties a
    named net in the layout's top circuit to a named net in the reference's
    top circuit (``NetlistComparer.same_nets(circuit_a, circuit_b, net_a,
    net_b, must_match=True)``). A hint naming a net that does not exist on
    the stated side is a malformed request (:class:`LvsError`), not a silent
    no-op -- a typo'd hint should be visible, not swallowed.

    ``equivalent_pins``: ``{"<subcircuit name>": [[pin_a, pin_b], ...], ...}``
    -- declares a group of swappable pins on the *reference*-side circuit of
    that name (``NetlistComparer.equivalent_pins`` only accepts circuits from
    the second netlist passed to ``compare()``, which is always the
    reference netlist in this module's ``compare(layout, reference)`` call
    order -- see ``run_lvs``).
    """
    same_nets = hints.get("same_nets") or []
    for entry in same_nets:
        if not isinstance(entry, list) or len(entry) != 2:
            raise LvsError(
                "hints.same_nets entries must be [layout_net, reference_net]"
            )
        layout_name, reference_name = entry
        net_a = layout_circuit.net_by_name(layout_name)
        if net_a is None:
            raise LvsError(f"hints.same_nets: layout net '{layout_name}' not found")
        net_b = reference_circuit.net_by_name(reference_name)
        if net_b is None:
            raise LvsError(
                f"hints.same_nets: reference net '{reference_name}' not found"
            )
        comparer.same_nets(layout_circuit, reference_circuit, net_a, net_b, True)

    equivalent_pins = hints.get("equivalent_pins") or {}
    for subcircuit_name, pin_groups in equivalent_pins.items():
        reference_netlist = reference_circuit.netlist()
        target_circuit = reference_netlist.circuit_by_name(subcircuit_name)
        if target_circuit is None:
            raise LvsError(
                f"hints.equivalent_pins: circuit '{subcircuit_name}' not found "
                "in reference netlist"
            )
        for group in pin_groups:
            pin_ids = []
            for pin_name in group:
                pin = target_circuit.pin_by_name(pin_name)
                if pin is None:
                    raise LvsError(
                        f"hints.equivalent_pins: pin '{pin_name}' not found on "
                        f"circuit '{subcircuit_name}'"
                    )
                pin_ids.append(pin.id())
            comparer.equivalent_pins(target_circuit, pin_ids)


# --------------------------------------------------------------------------- #
# Compare event capture
# --------------------------------------------------------------------------- #


def _name_or_none(obj: Any) -> str | None:
    if obj is None:
        return None
    if hasattr(obj, "expanded_name"):
        return obj.expanded_name()
    if hasattr(obj, "name"):
        name = obj.name
        return name() if callable(name) else name
    return None


def _make_compare_logger() -> Any:
    """Build a ``klayout.db.GenericNetlistCompareLogger`` subclass instance
    that captures every compare event into plain Python records for
    post-processing into the documented ``mismatches[]`` shape.

    Built lazily (inside a function, not a module-level class) since it must
    subclass ``klayout.db.GenericNetlistCompareLogger``, which requires the
    ``klayout`` module to already be imported -- this module keeps that
    import lazy, matching ``extract.py``'s discipline of not paying that
    cost for ``klt --version``/argument parsing.
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
            self.matched_nets = 0
            self.matched_devices = 0
            self.matched_pins = 0

        def match_nets(self, a: Any, b: Any) -> None:
            self.matched_nets += 1

        def match_ambiguous_nets(self, a: Any, b: Any, msg: str) -> None:
            self.matched_nets += 1
            self.ambiguous_net_matches.append((a, b))

        def net_mismatch(self, a: Any, b: Any, msg: str) -> None:
            self.net_mismatches.append((a, b))

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
) -> dict[str, Any]:
    return {
        "category": category,
        "severity": severity,
        "description": description,
        "side": side,
        "net": net,
        "device": device,
        "property": property_,
    }


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


def _build_mismatches(
    logger: Any,
    layout_netlist: Any | None = None,
    reference_netlist: Any | None = None,
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []

    mismatches.extend(_classify_net_mismatches(logger.net_mismatches))

    for a, b in logger.device_mismatches:
        side = "layout" if b is None else "reference"
        class_name = None
        for obj in (a, b):
            if obj is not None:
                class_name = obj.device_class().name
                break
        mismatches.append(
            _mismatch(
                CATEGORY_DEVICE_UNMATCHED,
                "error",
                "device has no counterpart on the other side",
                side,
                device={
                    "layout": _name_or_none(a),
                    "reference": _name_or_none(b),
                    "class": class_name,
                },
            )
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
        mismatches.append(
            _mismatch(
                CATEGORY_TOPOLOGY,
                "warning",
                "nets were paired ambiguously; the comparer resolved it "
                "structurally (consider a hints.same_nets entry to pin this down)",
                "both",
                net={
                    "layout": _name_or_none(a),
                    "reference": _name_or_none(b),
                },
            )
        )

    mismatches.sort(key=_sort_key)
    return mismatches


def _classify_param_mismatch(a: Any, b: Any) -> list[dict[str, Any]]:
    """Turn one ``match_devices_with_different_parameters`` event into one
    ``device.property`` mismatch entry per parameter that actually differs
    (see this module's docstring: the comparer flags the *device pair*, not
    which specific parameter -- this module identifies that itself)."""
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
    return abs(a_value - b_value) > max(
        _PARAM_ABS_EPSILON, _PARAM_REL_EPSILON * max(abs(a_value), abs(b_value))
    )


def _classify_net_mismatches(events: list[tuple[Any, Any]]) -> list[dict[str, Any]]:
    """Classify raw ``net_mismatch`` events into ``net.unmatched``/
    ``net.merged``/``net.split``/``topology`` entries -- see this module's
    docstring for the heuristic and its documented limitation."""
    one_sided_layout = [(a, b) for a, b in events if a is not None and b is None]
    one_sided_reference = [(a, b) for a, b in events if a is None and b is not None]
    both_sided = [(a, b) for a, b in events if a is not None and b is not None]
    both_sided_renamed = [
        (a, b) for a, b in both_sided if a.expanded_name() != b.expanded_name()
    ]

    entries: list[dict[str, Any]] = []

    if one_sided_layout and both_sided_renamed:
        for a, _b in one_sided_layout:
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
        for a, _b in one_sided_layout:
            entries.append(
                _mismatch(
                    CATEGORY_NET_UNMATCHED,
                    "error",
                    "layout net has no reference counterpart",
                    "layout",
                    net={"layout": _name_or_none(a), "reference": None},
                )
            )

    if one_sided_reference and both_sided_renamed:
        for _a, b in one_sided_reference:
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
        for _a, b in one_sided_reference:
            entries.append(
                _mismatch(
                    CATEGORY_NET_UNMATCHED,
                    "error",
                    "reference net has no layout counterpart",
                    "reference",
                    net={"layout": None, "reference": _name_or_none(b)},
                )
            )

    # Both-sided events are absorbed into the merge/split entries above when
    # a one-sided leftover exists on either side (they are the same root
    # cause, reported once); a differing-name pairing with no accompanying
    # leftover is its own, otherwise-unreported finding, standalone.
    if not one_sided_layout and not one_sided_reference:
        for a, b in both_sided_renamed:
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


# --------------------------------------------------------------------------- #
# Environment / reproducibility
# --------------------------------------------------------------------------- #


def _engine_version() -> str | None:
    import klayout

    return getattr(klayout, "__version__", None)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()
