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
import sys
from typing import TYPE_CHECKING, Any, NamedTuple

from ._provenance import build_provenance, sha256_file
from .decks import deck_source_path, get_extraction_deck
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
CATEGORY_DEVICE_PROPERTY = "device.property"
CATEGORY_DEVICE_BODY_UNVERIFIED = "device.body_unverified"
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
        layout_netlist.combine_devices()
        reference_netlist.combine_devices()

    logger = _make_compare_logger()
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
        mismatches.sort(key=_sort_key)

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
        "device_classes": device_classes,
        "environment": {
            "engine": engine,
            "engine_version": _engine_version(),
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
            # guarantees.
            (
                netlist,
                top_cell_name,
                _dbu_um,
                _warnings,
                _parasitics,
                _black_box_regions,
                _dummy,
            ) = extract_netlist_from_layout(
                layout_file,
                deck_name,
                top=layout_spec.get("top"),
                top_cell_pins_only=top_cell_pins_only,
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

        def _net_key(self, net: Any) -> _NetKey | None:
            return None if net is None else (self.scope, net.expanded_name())

        def begin_circuit(self, a: Any, b: Any) -> None:
            self.scope += 1

        def match_nets(self, a: Any, b: Any) -> None:
            self.matched_nets += 1
            key_a = self._net_key(a)
            key_b = self._net_key(b)
            if key_a is not None and key_b is not None:
                self.matched_net_keys.append((key_a, key_b))

        def match_ambiguous_nets(self, a: Any, b: Any, msg: str) -> None:
            self.matched_nets += 1
            key_a = self._net_key(a)
            key_b = self._net_key(b)
            if key_a is not None and key_b is not None:
                self.matched_net_keys.append((key_a, key_b))
            self.ambiguous_net_matches.append((a, b))

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


def _body_net_warnings(layout_circuit: Any, deck: Any) -> list[dict[str, Any]]:
    """Issue #281: flag, as non-blocking ``severity: "warning"`` entries, the
    MOS body terminals that ``extract.py``'s inline extraction ties to a
    deck-synthesized net rather than deriving from real drawn tap/well-label
    geometry -- so a caller recording a clean ``klt lvs`` verdict can also
    record that this dimension went structurally unverified (see this
    module's own docstring reference, ``extract.py``'s ``nfet_body``/
    ``connect_global`` handling, and ``docs/cli/extract.md`` -> "Coverage").

    Deck-structural, not per-instance: every curated deck ties **every**
    NMOS body to the deck's global substrate net (``deck.substrate_net``,
    via ``connect_global`` -- no curated deck draws a distinct NMOS tap
    layer today), so the NMOS warning always fires when the layout side has
    one or more NMOS devices. The PMOS warning only fires when the deck also
    has no distinct well-tap layer (``deck.tap is None``, e.g. gf180mcu's
    shared ``Comp`` layer) -- a deck that draws a real tap (e.g. sky130's
    ``tap=(65, 44)``) ties PMOS bodies to a genuine, named net, so no warning
    is warranted there.

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
    )
    if nfet_count:
        entries.append(
            _mismatch(
                CATEGORY_DEVICE_BODY_UNVERIFIED,
                "warning",
                f"{nfet_count} NMOS device body terminal(s) were compared "
                f"against the '{deck.substrate_net}' deck-synthesized "
                "substrate net, not a real schematic net -- this deck draws "
                "no distinct NMOS substrate/tap layer (see "
                'docs/cli/extract.md, "Coverage")',
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
