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

A second engine, ``"netgen"`` (issue #343), wraps the open-flow LVS
comparator ``RTimothyEdwards/netgen`` as a subprocess (``netgen -batch
lvs``) in **netlist-vs-netlist mode only** -- the same layout/reference SPICE
netlists this module already resolves for the ``"klayout"`` engine, written
to temporary files for the subprocess to read. Per the accepted spike
(``docs/design/lvs-extraction-spike.md`` section 1, "netgen (contrast
candidate)"), netgen has no layout front-end of its own -- the open flow
pairs it with ``magic`` for extraction -- so this module deliberately does
**not** wire up a second extraction backend; it only tests whether the
``mismatches[]`` contract generalises to a second, independent comparator
implementation (comparator/contract independence), not whether a second
*extraction* engine agrees with `klt extract` (extraction independence,
explicitly out of scope). See ``_run_netgen_lvs``/``_parse_netgen_report``
for the invocation and the (empirically-verified against netgen 1.5.323,
built from source) report-parsing contract, and the same design doc's
2026-08-02 addendum for the invocation quirks and report-format findings
this issue's own acceptance criteria asked to be written up.

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

Minimal-cell parameter recovery (issue #282): the comparer pairs devices from
the *surrounding* net structure and only then compares parameters, so on a
cell small enough that a device's own terminals are that structure (a
two-device inverter with its own substrate/well nets), a parameter-only
defect degrades into one unmatched device per side plus a collateral
unmatched net for each net only those devices touched -- and no parameter
event at all. ``_degraded_param_pair`` recovers the intended
``device.property`` entry from exactly that pattern and downgrades the
collateral to ``severity: "warning"``; see its docstring for the (deliberately
narrow) conditions and ``docs/cli/lvs.md`` -> "Negative controls" for the
caller-facing statement of it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from typing import TYPE_CHECKING, Any, NamedTuple

from ._provenance import build_provenance, sha256_file
from .decks import deck_source_path, get_extraction_deck
from .extract import ExtractError, extract_netlist_from_layout

if TYPE_CHECKING:
    import klayout.db as kdb

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: ``klayout`` (in-process ``NetlistComparer``) is the primary engine;
#: ``netgen`` (issue #343) is a second, independent comparator wrapped as a
#: subprocess in netlist-vs-netlist mode only -- see this module's docstring
#: for the scope boundary the accepted spike drew around it.
SUPPORTED_ENGINES = ("klayout", "netgen")

#: Accepted ``request.reference.form`` values (issue #280).
#: ``"plain-element"`` (default) is the schematic-equivalent form ``klt lvs``
#: has always required; ``"subckt-call"`` opts into converting a PDK
#: schematic flow's simulation-form netlist first (see ``docs/cli/lvs.md``).
_REFERENCE_FORMS = ("plain-element", "subckt-call")

#: Stable mismatch-category ids (spike section 2b). Never renumbered/
#: repurposed once shipped -- the same contract guarantee a DRC rule id
#: carries.
CATEGORY_NET_UNMATCHED = "net.unmatched"
CATEGORY_NET_MERGED = "net.merged"
CATEGORY_NET_SPLIT = "net.split"
CATEGORY_DEVICE_UNMATCHED = "device.unmatched"
CATEGORY_DEVICE_CLASS = "device.class"
#: Issue #504: a layout-side and reference-side device class share a name
#: but declare a different terminal list (e.g. a deck's `*WithBulk` device
#: extractor's three-terminal resistor class vs. a plain-element reference's
#: two-terminal one) -- see `_device_class_arity_mismatch`.
CATEGORY_DEVICE_CLASS_ARITY = "device.class_arity"
#: Issue #506: `request.reference.device_bulk` reconciled a reference device
#: class up to the layout side's terminal list before comparing -- the
#: disclosure that a match on that class rests on a caller assertion, not on
#: connectivity read from the reference netlist (see
#: `_apply_reference_device_bulk`).
CATEGORY_DEVICE_BULK_RECONCILED = "device.bulk_reconciled"
CATEGORY_DEVICE_PROPERTY = "device.property"
CATEGORY_DEVICE_BODY_UNVERIFIED = "device.body_unverified"
CATEGORY_DEVICE_COMBINE_INCOMPLETE = "device.combine_incomplete"
CATEGORY_PIN_UNMATCHED = "pin.unmatched"
CATEGORY_TOPOLOGY = "topology"
#: Issue #499: a `hints.same_nets` pairing the caller asserted with
#: `must_match=True` that the comparer refused to confirm -- see
#: `_build_mismatches`'s `same_nets_hints` handling.
CATEGORY_HINTS_REJECTED = "hints.rejected"

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

#: Description carried by a ``net.unmatched`` entry that is pure collateral
#: from a single unmatched device pair already reported as
#: ``device.property`` (issue #282, see ``_degraded_param_pair``).
_COLLATERAL_NET_DESCRIPTION = (
    "net has no counterpart on the other side, but only because the one "
    "device pair reported as 'device.property' failed to pair -- no other "
    "device or subcircuit touches this net, so it is collateral, not an "
    "independent connectivity defect"
)


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

    return _validate_request_shape(request, "request file")


def _validate_request_shape(data: Any, source: str) -> dict[str, Any]:
    """Shared ``layout``/``reference`` shape check for a JSON-decoded
    request, however it was sourced (file, inline JSON, stdin). ``source``
    is folded into the "must be a JSON object" error for context.
    """
    if not isinstance(data, dict):
        raise LvsError(f"{source} must contain a JSON object")

    for field in ("layout", "reference"):
        if field not in data:
            raise LvsError(f"request is missing required field: {field}")

    return data


def load_request_arg(value: str) -> tuple[dict[str, Any], str]:
    """Resolve the ``klt lvs`` CLI ``request`` argument into a request dict
    plus the directory relative paths inside it should resolve against.

    ``value`` is one of three forms (see docs/cli/lvs.md):

    - ``"-"`` -- read the request JSON document from stdin. Relative paths
      inside it resolve against the current working directory, since there
      is no request *file* to anchor them to.
    - a path to an existing, readable file -- read and parse that file
      (delegates to :func:`load_request`, unchanged). Relative paths
      resolve against the file's own directory, exactly as before.
    - anything else -- parsed as an inline JSON object string. This mirrors
      ``klt gen --params``'s ``load_params_arg`` (``gen.py``): an existing
      file always wins first, so this only applies once ``os.path.isfile``
      has already said no. Relative paths resolve against the current
      working directory, same as the stdin form.

    Raises :class:`LvsError` for any read/parse/shape failure -- the same
    exception type :func:`load_request` raises, so callers (``run_lvs``,
    ``cli/lvs_cmd.py``) do not need to distinguish the three forms.
    """
    if value == "-":
        try:
            data = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            raise LvsError(f"stdin request is not valid JSON: {exc}") from exc
        return _validate_request_shape(data, "stdin request"), os.getcwd()

    if os.path.isfile(value):
        return load_request(value), os.path.dirname(os.path.abspath(value))

    if os.path.isdir(value):
        raise LvsError(f"not a file: {value}")

    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise LvsError(
            f"request '{value}' is neither an existing file (file not "
            f"found) nor valid inline JSON: {exc}"
        ) from exc
    return _validate_request_shape(data, "inline request"), os.getcwd()


def run_lvs(request: str) -> dict[str, Any]:
    """Run the netlist compare declared by ``request``.

    ``request`` accepts the same three forms every ``klt lvs`` request
    argument does (see :func:`load_request_arg` / docs/cli/lvs.md): a path
    to a request JSON file, ``"-"`` to read the request from stdin, or an
    inline JSON object string. Relative paths *inside* the request document
    (``layout.file``, ``reference.netlist``, etc.) resolve against the
    request file's own directory for the file form, or against the current
    working directory for the stdin/inline forms -- there is no request
    file to anchor them to in that case.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/lvs.md`` / ``docs/design/lvs-extraction-spike.md`` section
    2b). Raises :class:`LvsError` for anything that prevents a trustworthy
    verdict from being produced at all (bad request, unresolvable layout/
    reference input, unknown deck, unsupported engine, engine error) --
    a documented ``status: "mismatch"`` is a successful run, not an error
    (see this module's docstring).

    ``device_classes`` (issue #221) echoes the layout-side
    :attr:`~klayout_tools.decks.ExtractionDeck.device_classes` -- what that
    deck can structurally recognise, not what this particular compare found
    -- when ``layout.file`` + ``layout.deck`` (inline extraction) was given;
    ``null`` when ``layout.netlist`` (pre-extracted, no deck involved) was
    given instead.

    The response also carries the shared ``provenance`` block (see
    :func:`klayout_tools._provenance.build_provenance`); its ``deck`` is the
    layout-side extraction deck (``null`` for the pre-extracted-netlist form,
    matching ``device_classes``), and ``pdk`` is ``null`` (LVS is topological
    and resolves no PDK).

    Same inline-extraction condition also gates ``device.body_unverified``
    (issue #281, see :func:`_body_net_warnings`): non-blocking
    ``severity: "warning"`` ``mismatches[]`` entries noting that some MOS
    body terminals were compared against a deck-synthesized net rather than
    a real schematic one -- never emitted for the pre-extracted
    ``layout.netlist`` form, and never affecting ``status``.

    ``request.reference.device_bulk`` (issue #506) is the reconciliation
    counterpart of that disclosure: it normalises a named reference device
    class up to the layout side's terminal list before comparing (see
    :func:`_apply_reference_device_bulk`), and every class it reconciles
    yields its own ``severity: "warning"``
    ``device.bulk_reconciled`` ``mismatches[]`` entry, so a ``"match"``
    reached through the hook is never silently indistinguishable from one
    reached independently.
    """
    request, request_dir = load_request_arg(request)

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
    combine_devices = bool(options.get("combine_devices", False))

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
    reference_form = reference_spec.get("form", "plain-element")
    if reference_form not in _REFERENCE_FORMS:
        raise LvsError(
            f"request.reference.form must be one of "
            f"{', '.join(repr(f) for f in _REFERENCE_FORMS)}; got "
            f"{reference_form!r}"
        )
    reference_device_map = reference_spec.get("device_map")
    if reference_device_map is not None and not isinstance(reference_device_map, dict):
        raise LvsError("request.reference.device_map must be a JSON object")
    reference_device_bulk = reference_spec.get("device_bulk")
    if reference_device_bulk is not None and not isinstance(
        reference_device_bulk, dict
    ):
        raise LvsError(
            "request.reference.device_bulk must be a JSON object mapping a "
            "device-class/model name to the reference net its implicit bulk "
            "terminal carries"
        )
    reference_netlist = _read_reference_netlist(
        reference_netlist_path,
        form=reference_form,
        deck=reference_spec.get("deck"),
        device_map=reference_device_map,
    )

    layout_circuit = _select_circuit(layout_netlist, layout_spec.get("top"), "layout")
    reference_circuit = _select_circuit(
        reference_netlist, reference_spec.get("top"), "reference"
    )

    _prune_extra_top_circuits(layout_netlist, layout_circuit)
    _prune_extra_top_circuits(reference_netlist, reference_circuit)

    combine_warnings: list[dict[str, Any]] = []
    if combine_devices:
        # Opt-in (issue #261): `Netlist.combine_devices()` merges devices
        # that a device class's own `combine_devices` logic recognises as
        # combinable (e.g. parallel/series MOSFETs with matching gate/S/D/B
        # connectivity) -- exactly the folded/multi-finger and split/
        # interleaved layout constructions standard to analog matching and
        # drive-strength splitting. It is a whole-`Netlist` method (not
        # scoped to one `Circuit`), so it is applied once per netlist here.
        # Applied symmetrically to *both* sides so a reference netlist that
        # already lumps a device is not penalized relative to one that
        # doesn't (and vice versa). Left opt-in, not unconditional: it would
        # also collapse genuinely-distinct parallel devices (e.g. a DAC
        # array's intentionally-separate legs) some callers want reported
        # individually -- see docs/cli/lvs.md's `options.combine_devices`
        # entry. Run after pruning (so it only ever touches the selected top
        # circuit's hierarchy) and before the comparer is constructed, so
        # every subsequent step (`same_circuits`, hints, `compare()`) sees
        # the already-combined device set.
        #
        # Wrapped per netlist (issue #466): KLayout's own `combine_devices()`
        # can raise an unhandled internal-consistency `RuntimeError` on a
        # partial-match device group -- N real (matching-relevant) instances
        # plus M dummy instances that all share two of three terminals, but
        # only the N real ones also share the third (e.g. a matched
        # bipolar/MOS array's flanking dummies). That is a `klayout.db`
        # behavior this module merely surfaces; letting it propagate as a
        # bare traceback would violate this module's own JSON-envelope
        # contract. `_combine_devices_safely` degrades gracefully instead:
        # whatever `combine_devices()` already merged before hitting the
        # error stays merged, the rest are left as individual devices, and a
        # `device.combine_incomplete` warning is added to `mismatches[]`.
        layout_warning = _combine_devices_safely(layout_netlist, "layout")
        if layout_warning is not None:
            combine_warnings.append(layout_warning)
        reference_warning = _combine_devices_safely(reference_netlist, "reference")
        if reference_warning is not None:
            combine_warnings.append(reference_warning)

        # combine_devices() folds matched device arrays but leaves the
        # interior nets it emptied (0 terminals, 0 pins) behind in the
        # circuit -- e.g. the N-1 interior nodes of a series string it
        # collapsed into a single device. Left in place they inflate
        # counts.nets.* (computed off each_net() below) and surface in
        # mismatches[] as spurious net.unmatched findings no caller can act
        # on (issue #500). Purge them symmetrically on both sides -- mirroring
        # the symmetric combine above and running only when combine actually
        # ran -- so counts and mismatches reflect the post-combine topology,
        # not combine_devices()'s internal bookkeeping. Scoped to genuinely
        # empty nets only, so a real (if unused) top-level pin's net is never
        # dropped and counts.pins.* is unaffected.
        _purge_emptied_nets(layout_netlist)
        _purge_emptied_nets(reference_netlist)

    bulk_warnings: list[dict[str, Any]] = []

    if engine == "klayout":
        # Issue #506: normalise the reference side's device classes up to the
        # layout side's terminal list *before* the comparer is constructed, so
        # a deck's bulk-terminal device flavour (e.g. a `bulk_to_substrate`
        # resistor's three-terminal `RES_X`) can be compared against a
        # schematic-derived reference that does not model that terminal at
        # all. Runs after the `combine_devices()` step above so combining
        # still sees each side's own, unmodified device classes (the
        # status-quo behaviour), and returns the `severity: "warning"`
        # disclosure entries appended to `mismatches[]` further down.
        bulk_warnings = _apply_reference_device_bulk(
            reference_device_bulk,
            layout_netlist,
            reference_netlist,
        )

        logger = _make_compare_logger(layout_circuit, reference_circuit)
        comparer = kdb.NetlistComparer(logger)
        # `_select_circuit` + `_prune_extra_top_circuits` above already guarantee
        # `layout_circuit`/`reference_circuit` are each netlist's *sole*
        # remaining top circuit, so these two are unambiguously the pair the
        # request declared -- pin that pairing explicitly instead of leaving it
        # to `NetlistComparer`'s default name-based matching, which silently
        # degrades to a generic "could not be matched to a counterpart" finding
        # on both sides whenever `layout.top`/`reference.top` name different
        # circuits (issue #231). Safe unconditionally: there is no other
        # circuit either one could be confused with post-pruning.
        comparer.same_circuits(layout_circuit, reference_circuit)
        same_nets_hints = _apply_hints(
            comparer, request.get("hints") or {}, layout_circuit, reference_circuit
        )

        # `logger` is already bound via the `NetlistComparer(logger)` constructor
        # above, so the 2-arg overload is used here (not the 3-arg one, which
        # would pass a second, redundant logger reference).
        compare_result = comparer.compare(layout_netlist, reference_netlist)

        mismatches = _build_mismatches(
            logger,
            layout_netlist,
            reference_netlist,
            same_nets_hints=same_nets_hints,
        )
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
                    "details": None,
                }
            ]
        status = "match" if compare_result else "mismatch"
        engine_version = _engine_version()
        net_correspondence = _build_net_correspondence(logger)
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
    else:
        # `engine == "netgen"` -- the only other `SUPPORTED_ENGINES` member.
        # Netlist-vs-netlist only (see this module's docstring): no magic
        # extraction backend, no per-net/per-device `hints` hook (netgen has
        # no equivalent to `same_nets`/`equivalent_pins` in this scope), and
        # -- unlike the `klayout` engine's in-process compare -- an external
        # subprocess whose own exit code is not trustworthy on its own (see
        # `_run_netgen_lvs`).
        if request.get("hints"):
            raise LvsError(
                "request.hints (same_nets/equivalent_pins) is only supported "
                "for engine 'klayout' -- the netgen engine has no equivalent "
                "hook in this issue's netlist-vs-netlist scope (see "
                'docs/cli/lvs.md, "Engine")'
            )
        if reference_device_bulk:
            # Issue #506: same boundary as `hints` above -- the reconciliation
            # is a `klayout.db`-side device-class normalisation applied to the
            # in-memory reference netlist, and netgen reads its own SPICE
            # files through its own device-class model.
            raise LvsError(
                "request.reference.device_bulk is only supported for engine "
                "'klayout' -- the netgen engine compares SPICE files through "
                "its own device model and has no equivalent hook (see "
                'docs/cli/lvs.md, "Engine")'
            )
        setup_file = _resolve_netgen_setup(options, request_dir)
        timeout_s = float(options.get("netgen_timeout_s", _NETGEN_DEFAULT_TIMEOUT_S))
        status, mismatches, engine_version = _run_netgen_lvs(
            layout_netlist=layout_netlist,
            layout_circuit=layout_circuit,
            reference_netlist=reference_netlist,
            reference_circuit=reference_circuit,
            setup_file=setup_file,
            timeout_s=timeout_s,
        )
        layout_net_count = sum(1 for _ in layout_circuit.each_net())
        reference_net_count = sum(1 for _ in reference_circuit.each_net())
        layout_device_count = sum(1 for _ in layout_circuit.each_device())
        reference_device_count = sum(1 for _ in reference_circuit.each_device())
        layout_pin_count = layout_circuit.pin_count()
        reference_pin_count = reference_circuit.pin_count()
        # Known limitation (see docs/cli/lvs.md, "Engine" -> netgen):
        # `_parse_netgen_report` classifies netgen's text report into
        # `mismatches[]`, but does not reconstruct a full per-net/per-device
        # correspondence the way the `klayout` engine's `NetlistComparer`
        # callbacks do. On a `"match"` verdict the matched count is exact by
        # construction (a unique match requires equal cardinality on both
        # sides); on `"mismatch"` it is intentionally left at the
        # conservative floor (`0`) rather than a fabricated estimate --
        # never overstating how much of the netlist was actually verified.
        # `net_correspondence` is `[]` for the same reason, which keeps the
        # documented `len(net_correspondence) == counts.nets.matched`
        # invariant intact for this engine too (both sides of that equation
        # are `0` together on a mismatch).
        matched_nets = layout_net_count if status == "match" else 0
        matched_devices = layout_device_count if status == "match" else 0
        matched_pins = layout_pin_count if status == "match" else 0
        net_correspondence = []
        counts = {
            "nets": {
                "layout": layout_net_count,
                "reference": reference_net_count,
                "matched": matched_nets,
            },
            "devices": {
                "layout": layout_device_count,
                "reference": reference_device_count,
                "matched": matched_devices,
            },
            "pins": {
                "layout": layout_pin_count,
                "reference": reference_pin_count,
                "matched": matched_pins,
            },
        }

    # What the layout-side deck can structurally recognise
    # (`ExtractionDeck.device_classes`, issue #221) -- `null` when
    # `layout.netlist` (pre-extracted) was given instead of `layout.file` +
    # `layout.deck`, since no deck is involved in that shape. Already
    # validated by `_resolve_layout` above (an unknown deck would have
    # raised `LvsError` before reaching this point), so re-fetching it here
    # cannot itself raise.
    layout_deck_name = layout_spec.get("deck")
    layout_deck = get_extraction_deck(layout_deck_name) if layout_deck_name else None
    device_classes = (
        list(layout_deck.device_classes) if layout_deck is not None else None
    )

    if layout_deck is not None:
        # Issue #281: MOS body terminals extracted onto deck-synthesized nets
        # (never a real schematic net -- see `_body_net_warnings`) are only a
        # structural property of the *deck* used for inline extraction, not
        # of this particular compare run. Appended (and the list re-sorted)
        # rather than folded into `_build_mismatches`, since these entries
        # never come from a `NetlistComparer` event and do not participate in
        # the `compare_result`/safety-net invariant above -- they are purely
        # additive, non-blocking notes.
        mismatches.extend(_body_net_warnings(layout_circuit, layout_deck))

    if combine_warnings:
        # Issue #466: same rationale as `_body_net_warnings` above -- these
        # never come from a `NetlistComparer` event either, so they are
        # appended (and the list re-sorted) rather than folded into
        # `_build_mismatches`. Unlike the deck-structural body-net warnings,
        # this fires for any request (pre-extracted `layout.netlist` and
        # `"netgen"` engine included), since `combine_devices()` runs before
        # the engine branch above.
        mismatches.extend(combine_warnings)

    if bulk_warnings:
        # Issue #506: same rationale again -- a `reference.device_bulk`
        # disclosure records a *request-side* normalisation applied before the
        # compare, not a `NetlistComparer` event, so it is appended here
        # rather than folded into `_build_mismatches`. Always
        # `severity: "warning"`: it never changes `status`, it only keeps a
        # match achieved through the hook from being indistinguishable from a
        # fully independent one.
        mismatches.extend(bulk_warnings)

    if layout_deck is not None or combine_warnings or bulk_warnings:
        mismatches.sort(key=_sort_key)

    category_counts: dict[str, int] = {}
    for mismatch in mismatches:
        category_counts[mismatch["category"]] = (
            category_counts.get(mismatch["category"], 0) + 1
        )

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
        "device_classes": device_classes,
        "environment": {
            "engine": engine,
            "engine_version": engine_version,
            "layout_sha256": sha256_file(layout_hash_source),
            "reference_sha256": sha256_file(reference_netlist_path),
            "extracted_netlist": extracted_netlist_path,
        },
        "provenance": build_provenance(
            deck_name=layout_deck_name,
            deck_path=(
                deck_source_path(layout_deck_name) if layout_deck_name else None
            ),
        ),
        "mismatches": mismatches,
        "net_correspondence": net_correspondence,
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

        # Issue #291: `top_cell_pins` keeps nets named only by a label inside an
        # instanced sub-cell internal, instead of promoting them to top-level
        # pins the reference netlist would then have to declare as ports. LVS is
        # topological, so the emitted extraction warning is not surfaced here
        # (the flag is the fix); the pin counts simply match without polluting
        # the reference interface.
        top_cell_pins_only = bool(layout_spec.get("top_cell_pins", False))

        # Issue #514: `declared_pins` is the per-*net* analogue of
        # `top_cell_pins` above -- every promoted pin not named in this set
        # is demoted back to an internal net (it keeps its name). Naming an
        # internal node of a lumped schematic device (e.g. one tap of a
        # metal-option ladder) for documentation no longer promotes it to a
        # pin `options.combine_devices` cannot fold through.
        declared_pins_spec = layout_spec.get("declared_pins")
        declared_pins: frozenset[str] | None = None
        if declared_pins_spec is not None:
            if not isinstance(declared_pins_spec, list) or not all(
                isinstance(name, str) for name in declared_pins_spec
            ):
                raise LvsError(
                    "request.layout.declared_pins must be a list of net name strings"
                )
            declared_pins = frozenset(declared_pins_spec)
            if not declared_pins:
                raise LvsError(
                    "request.layout.declared_pins must not be empty when given "
                    "-- omit the field entirely to keep every named net promoted"
                )
        try:
            # LVS is topological -- no parasitics_deck, so the 5th return
            # (parasitic_nets) is always None here and is ignored. The 6th
            # return (black_box_regions, issue #293) still takes effect --
            # any reserved-annotation-layer region in the layout is excluded
            # from connectivity the same as `klt extract` -- but is not
            # surfaced in `klt lvs`'s own response, out of scope here. The 7th
            # return (dummy_devices_dropped, #295) is a report-only count
            # surfaced by `klt extract`; the compare only cares that dummy
            # gates never became devices, which the suppression already
            # guarantees. The 8th return (unmodelled_poly, #324) is likewise a
            # report-only structured view of `klt extract`'s own warnings,
            # not surfaced in `klt lvs`'s response.
            (
                netlist,
                top_cell_name,
                _dbu_um,
                _warnings,
                _parasitics,
                _black_box_regions,
                _dummy,
                _unmodelled_poly,
            ) = extract_netlist_from_layout(
                layout_file,
                deck_name,
                top=layout_spec.get("top"),
                top_cell_pins_only=top_cell_pins_only,
                declared_pins=declared_pins,
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


def _read_reference_netlist(
    path: str,
    *,
    form: str = "plain-element",
    deck: str | None = None,
    device_map: dict[str, str] | None = None,
) -> kdb.Netlist:
    """Parse ``path`` via ``NetlistSpiceReader``, in the reference netlist's
    declared ``form`` (issue #280).

    ``form="plain-element"`` (default) reads the file as-is -- the
    schematic-equivalent form ``klt lvs`` requires (see ``docs/cli/lvs.md``).
    Before reading, it scans for the *simulation* (subcircuit-call) form and,
    if a curated PDK device (e.g. ``sky130_fd_pr__nfet_01v8``, ``nfet_03v3``)
    is instantiated via an undefined ``X`` subcircuit call, raises a specific
    :class:`LvsError` naming the form mismatch -- instead of letting the
    reader silently degrade the netlist into the confusing
    ``net.merged``/``topology`` cascade this issue describes.

    ``form="subckt-call"`` converts the file from the simulation form to the
    plain-element form first (see
    :mod:`klayout_tools.netlist_normalize`), resolving device names through the
    curated :mod:`klayout_tools.pdk_models` table (via ``deck`` and/or
    ``device_map``), then reads the converted text.

    Note: on genuinely malformed input, ``NetlistSpiceReader`` does not raise
    -- it prints a ``"Warning: Line ignored..."`` diagnostic (to the
    process's real stdout, a pre-existing KLayout engine behaviour this
    module does not attempt to suppress) and returns an empty netlist (zero
    circuits). That surfaces reliably as :class:`LvsError` a moment later, in
    :func:`_select_circuit`'s "no top circuit" check -- exercised directly in
    ``tests/test_lvs.py``.
    """
    import klayout.db as kdb

    from .netlist_normalize import (
        NormalizeError,
        detect_subckt_call_devices,
        normalize_reference_netlist,
    )

    try:
        # `errors="replace"` so a non-SPICE binary reference (a mis-pointed
        # path) never crashes the detection scan on a decode error -- it flows
        # through to `NetlistSpiceReader`, which tolerates garbage and yields
        # an empty netlist that surfaces as the "no top circuit" LvsError, the
        # pre-existing behaviour this hook preserves.
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        raise LvsError(f"could not read reference netlist '{path}': {exc}") from exc

    read_path = path
    tmp_path: str | None = None

    if form == "subckt-call":
        try:
            converted = normalize_reference_netlist(
                text, deck=deck, device_map=device_map
            )
        except NormalizeError as exc:
            raise LvsError(
                f"could not convert subckt-call reference netlist "
                f"'{path}' to plain-element form: {exc}"
            ) from exc
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".spice", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(converted)
            tmp_path = tmp.name
        read_path = tmp_path
    else:
        offending = detect_subckt_call_devices(text)
        if offending:
            raise LvsError(
                f"reference netlist '{path}' is in the simulation "
                f"(subcircuit-call) form: it instantiates curated PDK device "
                f"subcircuit(s) {', '.join(offending)} via undefined 'X' "
                "cards, which 'klt lvs' cannot compare against extracted "
                "plain-element devices (it would silently degrade into a "
                "net.merged/topology mismatch). Set request.reference.form to "
                '"subckt-call" to convert it automatically, or convert it to '
                "the plain-element form (M-card) before comparing -- see "
                'docs/cli/lvs.md, "Netlist form".'
            )

    netlist = kdb.Netlist()
    reader = kdb.NetlistSpiceReader()
    try:
        netlist.read(read_path, reader)
    except Exception as exc:
        raise LvsError(f"could not parse reference netlist '{path}': {exc}") from exc
    finally:
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    return netlist


def _combine_devices_safely(netlist: kdb.Netlist, side: str) -> dict[str, Any] | None:
    """Call ``netlist.combine_devices()``, degrading gracefully instead of
    letting KLayout's internal-consistency ``RuntimeError`` abort the whole
    ``klt lvs`` run (issue #466).

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

    Whatever ``combine_devices()`` already merged before hitting the error
    stays merged (KLayout raises only once it discovers the invariant
    violation while removing an already-combined device -- earlier,
    unrelated combines in the same call are not undone); this netlist's
    remaining, not-yet-combined devices are simply left as individual
    devices for the rest of this run, exactly as they would be with
    ``options.combine_devices: false``.

    Returns a ``severity: "warning"`` ``mismatches[]`` entry
    (``category: "device.combine_incomplete"``) to append to the report when
    this happened, or ``None`` when ``combine_devices()`` completed cleanly.
    Only catches this one KLayout-internal error shape (matched narrowly on
    ``_COMBINE_DEVICES_ERROR_MARKER``, the ``"...in Netlist.combine_devices"``
    suffix every instance of it carries) -- any other ``RuntimeError``
    propagates unchanged, so an unrelated failure is never silently
    swallowed.
    """
    try:
        netlist.combine_devices()
    except RuntimeError as exc:
        if _COMBINE_DEVICES_ERROR_MARKER not in str(exc):
            raise
        return _mismatch(
            CATEGORY_DEVICE_COMBINE_INCOMPLETE,
            "warning",
            "options.combine_devices could not fully combine devices on "
            f"the {side} netlist: KLayout's Netlist.combine_devices() hit "
            "an internal-consistency error on a partial-match device group "
            "(instances sharing only some, not all, of their matching "
            "terminals) and stopped -- devices it had already combined "
            "before the error remain combined, but the rest of this "
            f"netlist's devices were left uncombined ({exc})",
            side,
        )
    return None


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
) -> list[tuple[str, str]]:
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

    Returns the declared ``same_nets`` pairs as ``(layout_net.expanded_name(),
    reference_net.expanded_name())`` tuples (issue #499) -- the caller passes
    this to :func:`_build_mismatches` so it can tell, after ``compare()``
    runs, which of these hard assertions the comparer actually confirmed
    (``must_match=True`` is passed unconditionally above, so a hint the
    comparer disagrees with is a real finding, not a no-op). Deliberately
    excludes ``equivalent_pins``: it declares swappable pins, not an
    assertion about a specific pairing, so it has no "rejected" outcome to
    detect.
    """
    same_nets_declared: list[tuple[str, str]] = []
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
        same_nets_declared.append((net_a.expanded_name(), net_b.expanded_name()))

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

    return same_nets_declared


# --------------------------------------------------------------------------- #
# Reference-side device-class normalisation: reference.device_bulk (issue #506)
# --------------------------------------------------------------------------- #


def _find_device_class(netlist: Any, name: str) -> Any:
    """The device class named ``name`` in ``netlist``, matched exactly first
    and then case-insensitively -- ``NetlistSpiceReader`` upper-cases a
    ``.model``/element model name (``res_x`` -> ``RES_X``), so a request
    naturally written in the netlist's own lower-case spelling still
    resolves. ``None`` when no class matches."""
    exact = netlist.device_class_by_name(name)
    if exact is not None:
        return exact
    lowered = name.lower()
    for candidate in netlist.each_device_class():
        if candidate.name.lower() == lowered:
            return candidate
    return None


def _device_class_names(netlist: Any) -> list[str]:
    """Every device-class name registered on ``netlist``, sorted -- used to
    make an unresolvable ``reference.device_bulk`` key's error message
    actionable."""
    return sorted(device_class.name for device_class in netlist.each_device_class())


def _find_or_create_net(circuit: Any, name: str) -> tuple[Any, bool]:
    """``(net, created)`` for the net named ``name`` on ``circuit``, matched
    exactly first and then case-insensitively (same reason as
    :func:`_find_device_class`), creating it when the reference netlist does
    not model that node at all -- the ordinary case for a bulk terminal a
    schematic reference simply does not carry."""
    exact = circuit.net_by_name(name)
    if exact is not None:
        return exact, False
    lowered = name.lower()
    for candidate in circuit.each_net():
        if (candidate.name or "").lower() == lowered:
            return candidate, False
    return circuit.create_net(name), True


def _apply_reference_device_bulk(
    spec: Any,
    layout_netlist: Any,
    reference_netlist: Any,
) -> list[dict[str, Any]]:
    """Reconcile a reference device class that is one terminal short of the
    layout side's same-named class (issue #506, issue #504's option 1).

    ``spec`` is ``request.reference.device_bulk``:
    ``{"<device class / model name>": "<reference net name>"}``. For each
    entry, the reference-side class of that name is given the one terminal
    the layout-side class declares and it does not (typically the deck's
    bulk/well/collector terminal, e.g. the ``W`` of a
    ``bulk_to_substrate`` resistor flavour's three-terminal ``RES_X``), and
    every reference-side instance of that class has the new terminal tied to
    the named net -- created on the instance's own circuit when the reference
    netlist does not model that node at all.

    This is the *reconciliation* :data:`CATEGORY_DEVICE_CLASS_ARITY` (issue
    #505) deliberately stopped short of: with it, ``NetlistComparer`` can pair
    the two sides' devices and the run can legitimately report
    ``status: "match"``; without it, no request whose layout side uses a
    bulk-terminal device flavour against a schematic reference that does not
    model that terminal can ever match. Applied before the comparer is built
    (see ``run_lvs``), so the classes are already the same arity by the time
    ``compare()`` runs and ``_device_class_arity_mismatch`` no longer fires
    for the reconciled class.

    Returns one ``severity: "warning"``
    :data:`CATEGORY_DEVICE_BULK_RECONCILED` entry per reconciled class, which
    ``run_lvs`` appends to ``mismatches[]``. The disclosure is the point: the
    added terminal's connectivity is a caller *assertion*, not something read
    off the reference netlist, so a match reached this way is never silently
    indistinguishable from a fully independent one -- exactly the discipline
    :data:`CATEGORY_DEVICE_BODY_UNVERIFIED` (issue #281) applies to an
    unverified MOS body.

    Every malformed or inapplicable entry is an :class:`LvsError`, never a
    silent no-op -- the same convention ``hints.same_nets`` follows: a class
    name that resolves on neither side, a reference class that is not
    actually missing a terminal, and a class missing more than one terminal
    (this hook reconciles exactly one extra terminal per class, since the
    entry names exactly one net) all raise.
    """
    entries: list[dict[str, Any]] = []
    if not spec:
        return entries

    import klayout.db as kdb

    for model, net_name in spec.items():
        if not isinstance(net_name, str) or not net_name:
            raise LvsError(
                f"request.reference.device_bulk['{model}'] must be a non-empty "
                "reference net name"
            )

        reference_class = _find_device_class(reference_netlist, model)
        if reference_class is None:
            present = ", ".join(_device_class_names(reference_netlist)) or "none"
            raise LvsError(
                f"request.reference.device_bulk: device class '{model}' not "
                f"found in the reference netlist (classes present: {present})"
            )
        layout_class = _find_device_class(layout_netlist, model)
        if layout_class is None:
            present = ", ".join(_device_class_names(layout_netlist)) or "none"
            raise LvsError(
                f"request.reference.device_bulk: device class '{model}' not "
                f"found in the layout netlist (classes present: {present}) -- "
                "there is no layout-side terminal list to reconcile the "
                "reference class against"
            )

        layout_terminals = _terminal_names(layout_class)
        reference_terminals = _terminal_names(reference_class)
        missing = [
            terminal
            for terminal in layout_terminals
            if terminal not in reference_terminals
        ]
        if not missing:
            raise LvsError(
                f"request.reference.device_bulk: reference device class "
                f"'{reference_class.name}' already declares every terminal the "
                f"layout-side class does ({reference_terminals}) -- there is no "
                "implicit bulk terminal to reconcile; remove this entry"
            )
        if len(missing) > 1:
            raise LvsError(
                f"request.reference.device_bulk: reference device class "
                f"'{reference_class.name}' declares {reference_terminals} against "
                f"the layout side's {layout_terminals} -- {len(missing)} terminals "
                f"({missing}) apart. This hook reconciles exactly one extra "
                "(bulk/well/collector) terminal per class, since the entry names "
                "exactly one net"
            )

        terminal_name = missing[0]
        description = next(
            (
                terminal.description
                for terminal in layout_class.terminal_definitions()
                if terminal.name == terminal_name
            ),
            "",
        )
        reference_class.add_terminal(
            kdb.DeviceTerminalDefinition(terminal_name, description)
        )
        terminal_id = reference_class.terminal_id(terminal_name)

        connected = 0
        net_created = False
        for circuit in reference_netlist.each_circuit():
            devices = [
                device
                for device in circuit.each_device()
                if device.device_class() is reference_class
            ]
            if not devices:
                continue
            net, created = _find_or_create_net(circuit, net_name)
            net_created = net_created or created
            for device in devices:
                device.connect_terminal(terminal_id, net)
                connected += 1

        entries.append(
            _mismatch(
                CATEGORY_DEVICE_BULK_RECONCILED,
                "warning",
                f"request.reference.device_bulk reconciled reference device "
                f"class '{reference_class.name}' with the layout side: a "
                f"'{terminal_name}' terminal was added to the reference class "
                f"(layout: {layout_terminals}, reference was: "
                f"{reference_terminals}) and tied to reference net "
                f"'{net_name}' on {connected} device instance(s), "
                + (
                    "a net created for this compare"
                    if net_created
                    else "an existing reference net"
                )
                + " -- that terminal's connectivity was asserted by the "
                "request, not read from the reference netlist, so this "
                "dimension of the compare is not independently verified (see "
                "docs/cli/lvs.md, 'device.bulk_reconciled')",
                "reference",
                device={
                    "layout": None,
                    "reference": None,
                    "class": reference_class.name,
                },
                details={
                    "terminal": terminal_name,
                    "reference_net": net_name,
                    "reference_net_created": net_created,
                    "devices": connected,
                    "layout_terminals": layout_terminals,
                    "reference_terminals": reference_terminals,
                },
            )
        )

    return entries


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
    ``substrate_net`` global (no ring drawn, or ``deck.tap is None`` and no
    split is possible at all, e.g. gf180mcu) is structurally unverified. The
    NMOS warning therefore counts only devices whose body net name equals
    ``deck.substrate_net`` (or resolves to no net at all), not every NMOS
    device. The PMOS warning only fires when the deck also has no distinct
    well-tap layer (``deck.tap is None``) -- a deck that draws a real tap
    ties PMOS bodies to a genuine, named net unconditionally (no ring
    required, since every PMOS sits inside an ``nwell`` by construction), so
    no warning is warranted there.

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

    if deck.tap is None:
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
    mismatches: list[dict[str, Any]] = []

    # Issue #282: on a minimal circuit the comparer may decline to pair two
    # otherwise-identical devices whose only difference is a parameter,
    # reporting `device_mismatch` on each side (plus collateral net
    # mismatches) instead of the `match_devices_with_different_parameters`
    # event that yields `device.property`. Recover that here.
    degraded = _degraded_param_pair(logger)

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

    # Issue #499: a `hints.same_nets` pairing is a hard assertion --
    # `_apply_hints` calls `comparer.same_nets(..., must_match=True)` for
    # every declared pair. If the comparer did not end up confirming that
    # pair as a match, the caller's assertion was refused, and that
    # disagreement is reported here rather than silently dropped. Detected
    # structurally (declared pair vs. `logger.matched_net_keys`, both keyed
    # by the top circuit's `top_scope`), not by parsing the comparer's own
    # `log_entry` text -- this field's own contract (docs/cli/lvs.md,
    # "mismatches[].description") requires a curated description, never raw
    # `NetlistComparer` log text.
    top_scope = getattr(logger, "top_scope", None)
    matched_net_keys = set(getattr(logger, "matched_net_keys", None) or ())
    for layout_name, reference_name in same_nets_hints or ():
        pair = ((top_scope, layout_name), (top_scope, reference_name))
        if top_scope is None or pair not in matched_net_keys:
            mismatches.append(
                _mismatch(
                    CATEGORY_HINTS_REJECTED,
                    "error",
                    "hints.same_nets declared this pairing, but the "
                    "comparer did not confirm it as a topological match",
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


def _classify_net_mismatches(
    events: list[tuple[Any, Any]],
    *,
    event_keys: list[tuple[_NetKey | None, _NetKey | None]] | None = None,
    explained_layout_nets: frozenset[_NetKey] = frozenset(),
    explained_reference_nets: frozenset[_NetKey] = frozenset(),
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
    """
    keys: list[tuple[_NetKey | None, _NetKey | None]] = (
        list(event_keys) if event_keys is not None else [(None, None)] * len(events)
    )
    tagged = list(zip(events, keys, strict=True))
    one_sided_layout = [
        (a, key) for (a, b), key in tagged if a is not None and b is None
    ]
    one_sided_reference = [
        (b, key) for (a, b), key in tagged if a is None and b is not None
    ]
    both_sided = [(a, b) for a, b in events if a is not None and b is not None]
    both_sided_renamed = [
        (a, b) for a, b in both_sided if a.expanded_name() != b.expanded_name()
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


# --------------------------------------------------------------------------- #
# netgen engine (issue #343): subprocess invocation + report parsing
# --------------------------------------------------------------------------- #

#: Default ``netgen -batch lvs`` wall-clock budget -- the same idiom as
#: ``sim.py``'s ``DEFAULT_TIMEOUT_S``/``options.timeout_s``, but a separate,
#: larger default: an LVS graph-match on a real block can run longer than a
#: single SPICE corner. Overridable per request via ``options.netgen_timeout_s``.
_NETGEN_DEFAULT_TIMEOUT_S = 300.0

#: netgen's own startup banner (``tclnetgen.c``'s ``netgen_AppInit``, verified
#: against a from-source build of netgen 1.5.323 for this issue): ``"Netgen
#: 1.5.323 compiled on <date>"``, printed to stdout on every invocation --
#: mirrors ``sim.py``'s ``_ENGINE_VERSION_RE`` (``sim.py:1081``) for ngspice's
#: analogous ``"ngspice-<version>"`` banner.
_NETGEN_ENGINE_VERSION_RE = re.compile(r"Netgen\s+([\w.]+)")

#: A matched device pair's parameter-difference block, as netgen's
#: ``PrintPropertyResults`` (``base/netcmp.c``) writes it to the log file,
#: e.g.::
#:
#:     pmos:1 vs. pmos:1:
#:      W circuit1: 1e-06   circuit2: 2e-06   (delta=66.7%, cutoff=1%)
#:
#: Verified against a from-source netgen 1.5.323 build for this issue (see
#: the dated addendum in docs/design/lvs-extraction-spike.md).
#:
#: The index after the colon is numeric (``1``, ``2``, ...) only for
#: primitive *devices*. For a *subcircuit instance*, netgen uses the
#: instance name instead, e.g.::
#:
#:     sub:i1 vs. sub:i1:
#:      w circuit1: 1e-06   circuit2: 2e-06   (delta=66.7%, cutoff=0%)
#:
#: so the index group must accept any non-whitespace token, not just
#: digits (issue #363).
_NETGEN_PROPERTY_BLOCK_RE = re.compile(
    r"^(\S+):(\S+) vs\. (\S+):(\S+):\n((?: .+\n)+)", re.MULTILINE
)

#: One parameter-difference line inside a :data:`_NETGEN_PROPERTY_BLOCK_RE`
#: body. netgen emits (at least) two trailing-qualifier shapes, both verified
#: against a from-source netgen 1.5.323 build::
#:
#:      W circuit1: 1e-06   circuit2: 2e-06   (delta=66.7%, cutoff=1%)
#:      model circuit1: "fast"   circuit2: "slow"   (exact match req'd)
#:
#: -- the numeric form for a tolerance-compared property, and the "exact
#: match req'd" form for a string-valued one (``PropertyErrorCheck``'s
#: non-numeric branch). The qualifier is therefore captured as free text and
#: interpreted afterwards by :func:`_describe_netgen_property_delta`, rather
#: than hard-requiring the ``delta=…, cutoff=…`` shape: a line whose
#: qualifier wording changes across netgen versions must still parse, because
#: a property line silently failing to parse is exactly how a real property
#: error turned into a false ``"match"`` verdict (issue #343 review).
_NETGEN_PROPERTY_LINE_RE = re.compile(
    r"^\s*(\S+)\s+circuit1:\s*(.+?)\s+circuit2:\s*(.+?)"
    r"(?:\s*\(([^)]*)\))?\s*$"
)

#: The ``delta=…, cutoff=…`` qualifier shape, when netgen used it.
_NETGEN_PROPERTY_DELTA_RE = re.compile(
    r"^delta=([^,]+),\s*cutoff=(.+)$",
)

#: netgen's own declarations that a matched netlist nonetheless carries
#: parameter (property) errors -- the summary line printed with the
#: per-circuit verdict, and the trailing marker printed after ``Final
#: result:``. These are the *authoritative* signal that property errors
#: exist: :func:`_parse_netgen_report` keys the match -> mismatch downgrade
#: on them directly, never on whether :data:`_NETGEN_PROPERTY_LINE_RE`
#: happened to parse the supporting evidence (issue #343 review -- a
#: string-valued property difference parsed to nothing and the report was
#: reported as a clean ``"match"``).
_NETGEN_PROPERTY_ERROR_MARKERS: tuple[str, ...] = (
    "Property errors were found.",
    "match uniquely with property errors",
    "had property errors",
)

#: Side-by-side report section headers netgen prints ahead of a topology
#: mismatch, and the ``mismatches[]`` category/label each buckets into when
#: this module does not attempt to parse the column-aligned table itself
#: (see ``_parse_netgen_section_blocks``'s docstring for why: a fixed-width,
#: pipe-delimited table with filler cells like ``"(no matching net)"`` is
#: brittle to parse precisely across netgen versions, so the raw block is
#: preserved in ``details.raw`` instead of guessing at a per-net/per-device
#: split that could be wrong in an unbounded way).
_NETGEN_SECTION_HEADERS: tuple[tuple[str, str, str], ...] = (
    ("NET mismatches:", CATEGORY_NET_UNMATCHED, "net mismatch(es)"),
    ("DEVICE mismatches:", CATEGORY_DEVICE_UNMATCHED, "device mismatch(es)"),
)

#: Boundary markers used to find the end of a ``_NETGEN_SECTION_HEADERS``
#: block: the next section (of either kind), the "Subcircuit pins:" report
#: that always follows the mismatch tables, or the terminal "Final result:"
#: line -- whichever appears first.
_NETGEN_SECTION_BOUNDARIES: tuple[str, ...] = (
    "NET mismatches:",
    "DEVICE mismatches:",
    "Subcircuit pins:",
    "Final result:",
)


def _resolve_netgen_setup(options: dict[str, Any], request_dir: str) -> str | None:
    """Resolve ``options.netgen_setup`` (an explicit path to a netgen LVS
    setup ``.tcl`` file) against ``request_dir``, or ``None`` when omitted.

    ``klt lvs`` deliberately resolves no PDK on its own (this module's
    docstring, and ``provenance.pdk`` is always ``null``) -- so, unlike
    ``pdk.netgen_setup_file`` (issue #343's PDK-side lookup), this function
    does not itself call ``find_pdk``. A caller wanting the PDK-native setup
    resolved automatically composes the two: pass
    ``pdk.netgen_setup_file(variant=...)``'s result as this field. Omitting
    it runs netgen with no setup file (its own documented "trivial default
    setup" -- device/net comparison still works, but PDK-specific device-class
    merging/property tolerances from the setup script do not apply).
    """
    value = options.get("netgen_setup")
    if value is None:
        return None
    resolved = _resolve_relative(value, request_dir)
    if not os.path.isfile(resolved):
        raise LvsError(f"options.netgen_setup not found: {resolved}")
    return resolved


def _run_netgen_lvs(
    *,
    layout_netlist: kdb.Netlist,
    layout_circuit: kdb.Circuit,
    reference_netlist: kdb.Netlist,
    reference_circuit: kdb.Circuit,
    setup_file: str | None,
    timeout_s: float,
) -> tuple[str, list[dict[str, Any]], str | None]:
    """Invoke ``netgen -batch lvs`` headlessly in netlist-vs-netlist mode and
    return ``(status, mismatches, engine_version)``.

    Writes ``layout_netlist``/``reference_netlist`` (already selected,
    pruned, and -- when ``options.combine_devices`` was set -- combined,
    identically to what the ``klayout`` engine compares) to temporary SPICE
    files via ``klayout.db.NetlistSpiceWriter``, then runs::

        netgen -batch lvs "<layout.spice> <top>" "<reference.spice> <top>" \\
            <setup_file_or_""> <log_path>

    -- the syntax ``netgen::lvs`` (``tcltk/netgen.tcl.in``) expects: a
    ``"<file> <cell>"`` pair per side (a single argv token containing a
    space, which Tcl's ``eval $argv`` re-splits into the 2-element list the
    proc parses), an empty string for "no setup file" (netgen's own
    documented behaviour, verified directly against a from-source build for
    this issue), and the report log path.

    Never trusts the subprocess's exit code alone: like ``sim.py``'s
    ``_run_corner``, netgen exits ``0`` regardless of match/mismatch/most
    errors (verified empirically for this issue) -- the log file's own
    "Final result:" text is the only trustworthy verdict signal, parsed by
    :func:`_parse_netgen_report`, which raises :class:`LvsError` rather than
    guessing when that text is missing or unrecognised (the "must not
    silently produce a false match on unparseable output" requirement this
    issue exists to satisfy).
    """
    import klayout.db as kdb

    work_dir = tempfile.mkdtemp(prefix="klt-lvs-netgen-")
    try:
        layout_path = os.path.join(work_dir, "layout.spice")
        reference_path = os.path.join(work_dir, "reference.spice")
        log_path = os.path.join(work_dir, "comp.out")

        writer = kdb.NetlistSpiceWriter()
        writer.use_net_names = True
        try:
            layout_netlist.write(
                layout_path, writer, "klt lvs -- netgen engine layout netlist"
            )
            reference_netlist.write(
                reference_path, writer, "klt lvs -- netgen engine reference netlist"
            )
        except Exception as exc:
            raise LvsError(
                f"could not write a netlist for the netgen engine: {exc}"
            ) from exc

        cmd = [
            "netgen",
            "-batch",
            "lvs",
            f"{layout_path} {layout_circuit.name}",
            f"{reference_path} {reference_circuit.name}",
            setup_file or "",
            log_path,
        ]
        try:
            completed = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s
            )
        except FileNotFoundError as exc:
            raise LvsError(
                "could not launch netgen: binary not found on PATH. Install "
                "netgen (https://github.com/RTimothyEdwards/netgen) or use "
                f"engine 'klayout' instead. ({exc})"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise LvsError(
                f"netgen did not complete within {timeout_s}s (raise "
                "options.netgen_timeout_s to allow more time)"
            ) from exc

        engine_version = None
        version_match = _NETGEN_ENGINE_VERSION_RE.search(completed.stdout or "")
        if version_match:
            engine_version = version_match.group(1)

        if not os.path.isfile(log_path):
            # netgen exits 0 even when it never got as far as comparing
            # anything (e.g. a malformed netlist file) -- verified empirically
            # for this issue (see the design-doc addendum). No report file at
            # all means no trustworthy verdict is possible; surface netgen's
            # own stdout (its errors go there, not to the log file) rather
            # than a bare "no report" message.
            raise LvsError(
                "netgen did not produce a report file -- it likely failed to "
                "read one of the input netlists. netgen's own output:\n"
                + (completed.stdout or completed.stderr or "").strip()
            )
        try:
            with open(log_path, encoding="utf-8", errors="replace") as handle:
                log_text = handle.read()
        except OSError as exc:
            raise LvsError(f"could not read netgen report '{log_path}': {exc}") from exc

        status, mismatches = _parse_netgen_report(log_text)
        return status, mismatches, engine_version
    finally:
        _cleanup_netgen_work_dir(work_dir)


def _cleanup_netgen_work_dir(work_dir: str) -> None:
    import shutil

    shutil.rmtree(work_dir, ignore_errors=True)


def _parse_netgen_report(log_text: str) -> tuple[str, list[dict[str, Any]]]:
    """Classify netgen's ``comp.out`` log text into ``(status, mismatches)``.

    Verdict text (verified empirically against a from-source netgen 1.5.323
    build for this issue -- see the design-doc addendum for the four
    scenarios exercised):

    - ``"Final result: Circuits match uniquely."`` -- a unique topological
      match. Still downgraded to ``"mismatch"`` if a parameter difference was
      also found (``"Property errors were found."``), consistent with the
      ``klayout`` engine's own ``device.property`` semantics: a real
      parameter defect is never reported as a clean match.
    - ``"Final result: Netlists do not match."`` / ``"...Circuits do not
      match."`` -- a topology mismatch.
    - ``"Final result: Top level cell failed pin matching."`` -- a pin/port
      mismatch (the ``lvs`` Tcl proc's own pre-``verify`` short-circuit).
    - ``"Final result: Subcell(s) failed matching."`` -- a black-boxed
      subcircuit mismatch (same short-circuit).
    - ``"...Circuits match uniquely with port errors."`` -- topologically
      unique but with a pin-count/order disagreement; treated as a mismatch
      (never let a pin disagreement read as a clean match).

    The property-error downgrade is keyed on netgen's own declaration
    (:data:`_NETGEN_PROPERTY_ERROR_MARKERS`), not on whether the supporting
    per-parameter lines parsed: when netgen says property errors exist but no
    structured entry could be recovered, a generic ``device.property`` entry
    carrying netgen's raw text in ``details.raw`` is emitted and the verdict is
    still ``"mismatch"``. A recognised verdict line with unparseable *evidence*
    must never read as a clean match either (issue #343 review).

    Raises :class:`LvsError` when no ``"Final result:"`` section is found at
    all, or its text matches none of the above **and** no other structured
    evidence (a parsed parameter-difference block) was found either -- this
    is the "must fail loud, not soft, on unparseable output" requirement:
    never let unrecognised report text default to ``"match"``.
    """
    marker = "Final result:"
    idx = log_text.rfind(marker)
    if idx == -1:
        raise LvsError(
            "could not parse netgen's LVS report: no 'Final result:' section "
            "found -- the report format may be unrecognised or netgen may "
            "have exited before completing the compare. Raw report "
            "(last 2000 chars):\n" + log_text.strip()[-2000:]
        )
    tail = log_text[idx + len(marker) :].strip()
    first_line = tail.splitlines()[0] if tail else ""

    mismatches = _parse_netgen_property_errors(log_text)

    # netgen's own declaration that property errors exist is the authoritative
    # signal for the match -> mismatch downgrade -- NOT whether the supporting
    # per-parameter lines happened to parse. Keying the downgrade on the parse
    # result is how a real string-valued property difference
    # (`(exact match req'd)`, which the old line regex could not match) became
    # a clean `"match"` with an empty `mismatches[]` (issue #343 review).
    if _declares_netgen_property_errors(log_text) and not mismatches:
        mismatches.append(
            _mismatch(
                CATEGORY_DEVICE_PROPERTY,
                "error",
                "netgen reported property errors on one or more matched "
                "devices, but the per-parameter detail lines could not be "
                "parsed -- see the 'details.raw' field for netgen's own text",
                "both",
                details={"raw": _netgen_property_error_context(log_text)},
            )
        )

    is_clean_unique_match = tail.startswith("Circuits match uniquely.") and (
        "port errors" not in tail
    )
    if is_clean_unique_match:
        if mismatches:
            return "mismatch", mismatches
        return "match", []

    mismatches.extend(_parse_netgen_section_blocks(log_text))

    if tail.startswith("Top level cell failed pin matching."):
        mismatches.append(
            _mismatch(
                CATEGORY_PIN_UNMATCHED,
                "error",
                "netgen: top-level cell failed pin matching",
                "both",
            )
        )
    elif tail.startswith("Subcell(s) failed matching."):
        mismatches.append(
            _mismatch(
                CATEGORY_TOPOLOGY,
                "error",
                "netgen: one or more subcircuits failed to match",
                "both",
            )
        )
    elif "do not match" in tail or ("match uniquely" in tail and "port errors" in tail):
        # "Netlists do not match." / "Circuits do not match." / "Circuits
        # match uniquely with port errors." -- if the section-block parse
        # above already found NET/DEVICE mismatch blocks (the common case
        # for a topology mismatch), those are the detailed findings; only
        # add a generic entry when nothing more specific was recovered, so
        # the caller has *something* rather than an empty `mismatches[]` on
        # a documented mismatch verdict.
        if not mismatches:
            mismatches.append(
                _mismatch(
                    CATEGORY_TOPOLOGY,
                    "error",
                    f"netgen: {first_line}",
                    "both",
                )
            )
    elif not mismatches:
        # An unrecognised, non-empty "Final result:" text with no other
        # structured evidence at all: never guess this is a match.
        raise LvsError(
            "could not classify netgen's LVS verdict: unrecognised 'Final "
            f"result:' text {first_line!r}. Raw report tail:\n" + tail[:2000]
        )

    return "mismatch", mismatches


def _declares_netgen_property_errors(log_text: str) -> bool:
    """Whether netgen itself declared parameter (property) errors anywhere in
    the report -- see :data:`_NETGEN_PROPERTY_ERROR_MARKERS`."""
    return any(marker in log_text for marker in _NETGEN_PROPERTY_ERROR_MARKERS)


def _netgen_property_error_context(log_text: str) -> str:
    """Best-effort raw text for a property error netgen declared but whose
    per-parameter lines this module could not structure.

    Returns the ``"...match uniquely with property errors"`` summary line and
    the indented block that follows it when present (netgen's own evidence),
    otherwise the tail of the report -- so ``details.raw`` always carries
    something a human/agent can act on rather than an empty string.
    """
    lines = log_text.splitlines()
    for line_no, line in enumerate(lines):
        if "match uniquely with property errors" not in line:
            continue
        block = [line.strip()]
        for following in lines[line_no + 1 :]:
            if not following.strip():
                break
            block.append(following.rstrip())
        return "\n".join(block)
    return log_text.strip()[-2000:]


def _describe_netgen_property_delta(qualifier: str | None) -> str:
    """Render netgen's trailing per-property qualifier for a ``description``.

    Handles both observed shapes -- ``delta=66.7%, cutoff=1%`` (numeric
    tolerance compare) and ``exact match req'd`` (string-valued property) --
    and passes any other wording through verbatim rather than dropping the
    line, so an unfamiliar qualifier still yields a structured entry.
    """
    if not qualifier:
        return "netgen reported a property difference"
    delta_match = _NETGEN_PROPERTY_DELTA_RE.match(qualifier.strip())
    if delta_match is not None:
        delta, cutoff = delta_match.groups()
        return f"delta={delta}, cutoff={cutoff}"
    return qualifier.strip()


def _parse_netgen_property_errors(log_text: str) -> list[dict[str, Any]]:
    """Parse netgen's parameter-difference block(s) into ``device.property``
    ``mismatches[]`` entries -- see :data:`_NETGEN_PROPERTY_BLOCK_RE` for the
    exact text shape this matches.

    A body line that does not match :data:`_NETGEN_PROPERTY_LINE_RE` at all is
    **not** dropped: it becomes a best-effort entry carrying the raw line in
    ``details.raw``. Silently discarding evidence netgen printed is what
    allowed a declared property error to surface as a clean ``"match"``
    (issue #343 review); the caller's marker-based guard is the backstop, and
    this is the per-line half of the same rule.
    """
    entries: list[dict[str, Any]] = []
    for block_match in _NETGEN_PROPERTY_BLOCK_RE.finditer(log_text):
        class1, index1, class2, index2, body = block_match.groups()
        device_layout = f"{class1}:{index1}"
        device_reference = f"{class2}:{index2}"

        def _device(
            layout: str = device_layout,
            reference: str = device_reference,
            device_class: str = class1,
        ) -> dict[str, Any]:
            # A fresh dict per entry: `mismatches[]` entries must not share
            # mutable sub-objects across the list.
            return {
                "layout": layout,
                "reference": reference,
                "class": device_class,
            }

        for line in body.splitlines():
            if not line.strip():
                continue
            line_match = _NETGEN_PROPERTY_LINE_RE.match(line)
            if line_match is None:
                entries.append(
                    _mismatch(
                        CATEGORY_DEVICE_PROPERTY,
                        "error",
                        "netgen reported a matched-device property difference "
                        "in a form this parser does not structure -- see the "
                        "'details.raw' field for netgen's own text",
                        "both",
                        device=_device(),
                        details={"raw": line.strip()},
                    )
                )
                continue
            name, layout_value, reference_value, qualifier = line_match.groups()
            entries.append(
                _mismatch(
                    CATEGORY_DEVICE_PROPERTY,
                    "error",
                    f"netgen: matched device parameter '{name}' differs "
                    f"({_describe_netgen_property_delta(qualifier)})",
                    "both",
                    device=_device(),
                    property_={
                        "name": name,
                        "layout": layout_value,
                        "reference": reference_value,
                    },
                )
            )
    return entries


def _parse_netgen_section_blocks(log_text: str) -> list[dict[str, Any]]:
    """Bucket netgen's ``NET mismatches:``/``DEVICE mismatches:`` side-by-side
    report tables into one generic entry per section, with the raw block
    preserved verbatim in ``details.raw``.

    These tables are fixed-width, pipe-delimited, and use filler cells like
    ``"(no matching net)"`` for a one-sided row -- parsing them into precise
    per-net/per-device entries (mirroring the ``klayout`` engine's
    ``net.unmatched``/``device.unmatched`` granularity) would require
    trusting column alignment that is not a documented, versioned contract
    of netgen's own report format. Per this issue's scope ("fields that
    don't map cleanly onto that shape go into a mismatch-level `details`
    object, not a schema fork"), this module buckets instead of guessing.
    """
    entries: list[dict[str, Any]] = []
    for header, category, label in _NETGEN_SECTION_HEADERS:
        start = log_text.find(header)
        if start == -1:
            continue
        end = len(log_text)
        for boundary in _NETGEN_SECTION_BOUNDARIES:
            boundary_pos = log_text.find(boundary, start + len(header))
            if boundary_pos != -1:
                end = min(end, boundary_pos)
        block = log_text[start:end].strip()
        entries.append(
            _mismatch(
                category,
                "error",
                f"netgen reported one or more {label} -- see the "
                "'details.raw' field for netgen's own side-by-side report",
                "both",
                details={"raw": block},
            )
        )
    return entries
