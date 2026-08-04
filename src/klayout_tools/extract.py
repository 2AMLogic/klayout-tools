"""Extract a schematic-equivalent netlist from a GDSII/OASIS layout, headless.

Pure library: :func:`run_extract` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints, mirroring ``drc.py``/
``sim.py``. Serialisation and human-readable formatting live in the CLI
command module (``cli/extract_cmd.py``).

This is phase 2 of Epic #153 (``klt lvs``/``klt extract``), the build carried
by the accepted spike, ``docs/design/lvs-extraction-spike.md`` -- read that
document first; it settles the engine choice (KLayout's own
``klayout.db.LayoutToNetlist``/``NetlistSpiceWriter``, already this repo's
sole runtime dependency) and the request/response contract this module
implements (its section 2a, ``klt extract``). Scope: **schematic-equivalent**
extraction only -- devices and connectivity, no parasitic R/C on the
interconnect (explicitly deferred by the spike's "Out of scope" section).

Deviation from the spike: the spike's proposed invocation is flag-only
(``klt extract <file> --deck sky130|gf180mcu``), with no PDK-resolver
involvement -- extraction, like ``klt drc``, was scoped as a whole-layout
operation against a curated rule set, not an installed-PDK operation. This
module keeps ``--deck`` as the (required) selector of the curated
connectivity + device-extraction deck (see
``klayout_tools.decks.ExtractionDeck``), self-contained exactly like `klt
drc`'s decks -- no PDK install is required to run it. It additionally accepts
optional ``--pdk``/``--pdk-root`` flags, resolved through the one shared
resolver every other PDK-aware verb uses
(:func:`klayout_tools.pdk.find_pdk`), mirroring ``klt sim``'s optional
``models.pdk``/``models.pdk_root`` resolution (see ``sim.py``): when given,
an unresolvable PDK is an application error (exit 1); when omitted,
resolution is skipped entirely and extraction runs from the curated deck
alone, so CI needs no PDK install (matching ``klt drc``'s and ``klt gen``'s
test posture -- see ``tests/test_extract.py``'s fabricated installs).

When a PDK resolves, ``--pdk``/``--pdk-root`` also change what the *written
SPICE file* looks like (not just the JSON response's provenance-only
``pdk`` field, which is all they did before issue #209): each extracted MOS
device is written as an ``X`` subcircuit call against the resolved PDK's own
curated device library, e.g. ``sky130_fd_pr__nfet_01v8``, instead of the
curated deck's bare ``nfet``/``pfet`` ``M``-card class label -- see
``klayout_tools.pdk_models`` for the curated
``(deck_name, pdk_variant_family)`` model-name table, the
``kdb.NetlistSpiceWriterDelegate`` subclass that performs the rewrite, and
the exact provenance of each bound subcircuit name. A resolved PDK whose
family/deck pairing has no curated table entry is an :class:`ExtractError`
naming what was tried -- never a silent fallback to the bare ``M``-card
form. When ``--pdk``/``--pdk-root`` are omitted, the written SPICE is
unchanged from before #209 (the bare ``M``-card form, byte-identical to the
existing golden tests).

Device recognition: each deck's ``active``/``poly``/``nwell`` layers extract
NMOS (``active - nwell``) and PMOS (``active & nwell``) via KLayout's native
``DeviceExtractorMOS4Transistor`` -- one generic ``nfet``/``pfet`` device
class per deck (no voltage-flavor distinction, e.g. no ``nfet_01v8`` vs.
``nfet_g5v0`` split), the same "curated starter subset, not the full device
zoo" scope guard ``docs/cli/drc.md`` documents for the DRC decks. A deck may
additionally declare *drawn* precision resistors (issue #222,
``klayout_tools.decks.ResistorDevice``): a conductor segment covered by the
PDK's resistor-ID layer is cut out of that conductor's connectivity region
and extracted through KLayout's native ``DeviceExtractorResistor`` /
``DeviceExtractorResistorWithBulk`` instead of being left as a short between
its two heads. See ``klayout_tools.decks.sky130``/``gf180mcu`` for the exact
per-family layer roles, the resistor sheet-resistance provenance, and their
known connectivity-fidelity limitations (well-tie handling in particular).

A deck may additionally declare one or more vertical-BJT device-recognition
entries (``ExtractionDeck.bipolars``, issue #223): the deck's ``nwell``/
``active`` layers restricted to a PDK-specific bipolar device-mark layer
(e.g. sky130's ``pnp.drawing`` 82/44, gf180mcu's ``DRC_BJT`` 127/5), fed to
KLayout's native ``DeviceExtractorBJT3Transistor``. See
:class:`klayout_tools.decks.BipolarDevice` for the layer-role contract and
:func:`_extract_netlist`'s bipolar wiring block for how the marker layer
scopes recognition to genuine device-cell instances.

A deck may also declare one or more drawn MiM-capacitor device-recognition
entries (``ExtractionDeck.capacitors``, issue #225): two independent
plate layers (a purpose-drawn top plate, a bottom plate on an ordinary
conductor -- optionally derived through a PDK-specific "virtual bottom
plate" sizing step) fed straight to KLayout's native
``DeviceExtractorCapacitor``. Each plate is registered as its own
self-connected node, and is wired into the rest of the deck's metal stack
per plate, where the deck declares how (issue #314): the bottom plate joins
the ``metals[]`` node whose layer its ``bottom_plate`` matches, and the top
plate joins the ``metals[]`` node named by ``top_plate_via_metal`` through
the ``top_plate_via`` layer when the deck declares both. A plate for which
the deck declares neither -- e.g. sky130's MiM top plates, whose real via
lands on a metal this curated deck does not track -- stays an isolated node:
the device and its capacitance are still extracted correctly, only that
plate's net connectivity carries the documented approximation. See
:class:`klayout_tools.decks.CapacitorDevice` for the layer-role contract, the
capacitance-per-area provenance each deck must cite, and the exact scope of
that per-plate limitation. A ``top_plate_via`` placed per the PDK's own
minimum-overlap rule for that via necessarily overlaps the bottom plate in
plan view; :func:`_exclude_capacitor_top_via_overlap` (issue #364) excludes
that overlap from the deck's generic ``vias[]`` layers before the generic
per-layer connectivity loop runs, so the DRM-legal via wires the top plate
to ``top_plate_via_metal`` without also shorting it to the bottom plate.

Every connectivity layer above (``poly``, ``contact``, ``metals``, ...) is
wired up unconditionally, regardless of whether any device extractor above
claims the geometry drawn on it: geometry for a device class the deck does
not (yet) implement is absorbed into ordinary interconnect -- a silent short
between what should be distinct terminals -- rather than skipped or flagged
(issue #288). ``warnings`` gains one narrowly-scoped heuristic diagnostic for
the most common shape of this problem, split (issue #299) into a distinct
string for "carries no declared resistor-marker layer at all" versus "carries
a marker this deck knows about, but no declared ``ResistorDevice`` claims it"
(a deck-coverage gap): see :func:`_detect_unmodelled_poly_bodies` and
``docs/cli/extract.md``'s "Known limitation: unmodelled device geometry" for
the exact signature it looks for and its documented false-negative surface.

Verified compatible with ``klt sim``'s netlist convention (see
``docs/cli/sim.md`` -> "Netlist convention"): the written SPICE is a
``.SUBCKT ... .ENDS`` circuit body with no top-level ``.control``/``.end``
card -- confirmed directly against KLayout's ``NetlistSpiceWriter`` output
(it never emits a top-level ``.END`` for a single-circuit netlist), and
exercised by ``tests/test_extract.py``.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any

from ._annotation import is_reserved_annotation_layer
from ._provenance import build_provenance, sha256_file
from .decks import (
    BipolarDevice,
    CapacitorDevice,
    ExtractionDeck,
    ParasiticsDeck,
    ResistorDevice,
    UnknownExtractionDeckError,
    deck_source_path,
    get_extraction_deck,
    get_parasitics_deck,
)
from .pdk import PdkNotFoundError, find_pdk
from .pdk_models import (
    DeviceBinding,
    ModelBindingError,
    create_model_binding_delegate,
    equivalent_rectangle_um,
    resolve_device_bindings,
)

if TYPE_CHECKING:
    import klayout.db as kdb

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: Decimal places `devices[].params` (`w_um`/`l_um`) are rounded to -- clears
#: floating-point noise from KLayout's internal dbu -> um conversion (e.g.
#: `0.14999999999999997`) without losing meaningful precision (sub-nm, well
#: below any curated deck's dbu grid).
_PARAM_PRECISION_UM = 6

#: Decimal places a drawn capacitor's `devices[].params.c_f` (in **Farads**)
#: is rounded to -- same floating-point-noise cleanup as `_PARAM_PRECISION_UM`,
#: but a MiM cap's capacitance sits in the femtofarad-to-picofarad range
#: (roughly 1e-14 to 1e-11 F for the curated decks' modelled plate sizes), so
#: clearing noise at the same *absolute* micrometre-scale precision would
#: zero out the whole value; this rounds at a correspondingly smaller
#: absolute scale instead (still far more precision than any real dbu-grid
#: geometry needs).
_PARAM_PRECISION_FARAD = 21

#: Decimal places a drawn resistor's `devices[].params.r_ohm` is rounded to
#: -- same floating-point-noise cleanup as `_PARAM_PRECISION_UM`, applied to
#: an ohms-valued parameter rather than a micrometre-valued one.
_PARAM_PRECISION_OHM = 6

#: Lower bound (ohms) clamped onto an emitted parasitic series resistor so a
#: net whose interconnect resistance rounds to ~0 still writes a well-formed,
#: simulator-safe `R` card (a literal 0-ohm resistor is a degenerate short
#: some readers reject) -- negligible against any real net's resistance.
_MIN_PARASITIC_R_OHM = 1e-3


class ExtractError(Exception):
    """Raised when a layout cannot be extracted: bad file, unknown deck,
    unresolvable PDK, missing/ambiguous top cell, or an engine error.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback.
    """


def run_extract(
    path: str,
    deck_name: str,
    output: str | None = None,
    top: str | None = None,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
    parasitics: bool = False,
    top_cell_pins_only: bool = False,
) -> dict[str, Any]:
    """Extract a schematic-equivalent netlist from the layout at ``path``.

    ``deck_name`` selects the curated :class:`~klayout_tools.decks.ExtractionDeck`
    (currently ``"sky130"``/``"gf180mcu"``). ``output`` overrides the written
    SPICE path (default: ``path`` with its extension replaced by
    ``.spice``, next to the input -- the "next to the input" convention
    ``klt render``/``klt sim`` already use). ``top`` selects the top cell
    when the stream has more than one (required in that case; otherwise
    optional and must name the sole top cell if given).

    ``top_cell_pins_only`` (the ``--top-cell-pins`` flag) controls how
    labelled nets become top-level pins (issue #291). Extraction is flat, so
    ``make_top_level_pins()`` would otherwise promote *every* named net --
    including nets that are only named because a label sits inside an
    instanced sub-cell, which are ordinary internal nodes once instanced.
    When ``True``, only labels drawn directly in the top cell are promoted to
    pins; a net named solely by a label found below an instance boundary
    keeps its name but stays internal. Independent of the flag, a
    ``warnings`` entry is emitted whenever such a below-top label was
    promoted (default) or kept internal (flag set), so the promotion is
    always visible rather than silently inferred from an unexpected pin
    count. Off by default, so a flat layout (or any layout whose pin labels
    all live in the top cell) is byte-for-byte unchanged.

    ``pdk_variant``/``pdk_root`` (the ``--pdk``/``--pdk-root`` flags) are
    optional: when either is given, the PDK is resolved via
    :func:`klayout_tools.pdk.find_pdk` and an unresolvable PDK is an
    :class:`ExtractError`; when both are omitted, resolution is skipped
    entirely (see the module docstring's "Deviation from the spike").

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/extract.md`` / ``docs/design/lvs-extraction-spike.md``
    section 2a)::

        {
            "schema_version": 1,
            "file": <path as provided>,
            "deck": <deck name>,
            "top": <top cell name>,
            "dbu_um": <database unit in micrometres, float>,
            "netlist_path": <resolved output path>,
            "netlist_sha256": <hex digest of the written netlist file>,
            "status": "extracted",
            "device_count": <int>,
            "net_count": <int>,
            "pin_count": <int>,
            "device_counts": {<device class>: <int>, ...},
            "ignored_layers": [
                {"layer": int, "datatype": int, "shapes": int}, ...
            ],
            "device_classes": [<device class role>, ...],
            "devices": [
                {
                    "name": str, "class": str,
                    # MOS: {"s", "g", "d", "b"}; drawn resistor: {"a", "b"}
                    # (plus "w" for a bulk-terminal resistor).
                    "nets": {<terminal>: str | None, ...},
                    # MOS: {"w_um", "l_um"}; drawn resistor adds "r_ohm".
                    "params": {<name>: float, ...},
                },
                ...
            ],
            "nets": [{"name": str, "pin": bool, "device_count": int}, ...],
            "warnings": [str, ...],
            "black_box_regions": [
                {
                    "bbox_um": {
                        "left": float, "bottom": float,
                        "right": float, "top": float,
                    },
                    "shapes_excluded": int,
                },
                ...
            ],
            "unmodelled_poly": [
                {
                    "bbox_um": {
                        "left": float, "bottom": float,
                        "right": float, "top": float,
                    },
                    "reason": "unmarked" | "marked_unrecognised",
                },
                ...
            ],
            "pdk": {"variant": str, "root": str, "version": str | None} | None,
            "parasitics": {...} | None,
            "provenance": {  # shared reproducibility block, see _provenance.py
                "klt_version": <str | None>,
                "klayout_version": <str | None>,
                "pdk": {"name", "source", "version"} | None,
                "deck": {"name": <deck name>, "content_hash": "sha256:..."},
            },
        }

    ``devices``/``nets`` are sorted by name for deterministic, diff-clean
    output (same discipline as ``drc.py``'s ``violations`` sort).

    ``device_classes`` is what the *deck* is structurally capable of
    recognising (:attr:`klayout_tools.decks.ExtractionDeck.device_classes`)
    -- independent of what this particular layout happens to contain, unlike
    ``device_counts`` (issue #221). A consumer that needs to know ahead of
    time whether a deck can even produce a given device class (e.g. before
    pairing it with a reference netlist for ``klt lvs``) reads this field
    instead of inferring "not supported" from a zero count.

    ``ignored_layers`` (issue #220) lists ``(layer, datatype)`` pairs that
    carry shapes in the input stream but are *not* read by this deck's
    connectivity graph (:attr:`ExtractionDeck.connectivity_layers`), each with
    its stream shape count. It is the extraction-side analogue of ``klt
    drc``'s ``coverage.layers_in_stream_without_rules``: geometry on such a
    layer is invisible to extraction, so a block routed on a metal level the
    deck does not declare silently extracts as a pile of disconnected nets.
    A non-empty ``ignored_layers`` with a material shape count is the signal
    that a downstream ``klt lvs`` mismatch is a deck-coverage gap, not a
    layout bug. Empty when every shape-bearing layer is one the deck reads.

    ``black_box_regions`` (issue #293) reports every black-box/abstract
    region this run excluded from connectivity: a shape drawn on any
    reserved annotation layer (990-999, any datatype -- issue #289, see
    ``docs/cli/extract.md``'s "Reserved annotation layer") marks a region
    whose contents are deliberately out of scope -- a sub-cell that will be
    drawn later, or a drawn region deliberately out of scope for a compare.
    Everything geometrically inside it is excluded from the connectivity
    graph *before* device extraction runs, rather than left undrawn (losing
    the hierarchy/area record) or documented only in prose outside the GDS.
    One entry per geometrically separate marker shape (non-touching shapes
    are never merged into one bbox), each ``{"bbox_um": {"left", "bottom",
    "right", "top"}, "shapes_excluded": <int>}`` -- ``shapes_excluded`` counts
    the conductor/label shapes actually removed by the exclusion, the signal
    that it did something rather than that a marker shape merely exists.
    Always a list, empty when the layout draws no reserved-layer geometry
    (byte-identical to the response before this field existed, other than
    the field's own presence).

    ``unmodelled_poly`` (issue #324) reports every poly shape the
    "unmodelled device" diagnostic flagged -- see
    :func:`_detect_unmodelled_poly_bodies` for the exact signature it looks
    for (a poly component touching no recognised MOS gate or recognised
    resistor body, contacted at 2+ geometrically separate points) and
    ``docs/cli/extract.md``'s "Known limitation: unmodelled device geometry"
    for the heuristic's documented false-negative/false-positive surface.
    One entry per flagged component, each ``{"bbox_um": {"left", "bottom",
    "right", "top"}, "reason": "unmarked" | "marked_unrecognised"}`` --
    ``reason`` distinguishes the two ``warnings`` cases (#288's "no marker at
    all" versus #299's "carries a marker but no declared entry claims it")
    without a consumer having to parse the prose warning string. Sorted by
    ``(left, bottom)`` for deterministic, diff-clean output. Always a list,
    empty whenever ``warnings`` carries no unmodelled-device entry (which
    includes every layout that draws no such geometry at all) -- a consumer
    can enumerate and triage the exact flagged shapes instead of
    re-implementing the heuristic against the stream.

    Raises :class:`ExtractError` if the file is missing/unreadable, the deck
    name is unknown, the PDK (when given) does not resolve, the top cell is
    missing/ambiguous, or the output path's parent directory cannot be
    created (e.g. it exists as a non-directory file). The output path's
    parent directory is created automatically when missing (matching ``klt
    render``/``klt lvs``), including any missing intermediate directories.
    """
    pdk_info: dict[str, Any] | None = None
    # Populated only when a PDK resolves: `{<deck's device class name>:
    # DeviceBinding}` for every device class this deck extracts that has a
    # curated binding (MOS + resistor + capacitor on both decks, plus sky130's
    # bipolar; gf180mcu's bipolar is a documented carve-out and stays absent --
    # see `klayout_tools.pdk_models`). Drives the `X`-card model-binding writer
    # below -- see the module docstring's "--pdk-triggered model binding" note.
    model_bindings: dict[str, DeviceBinding] | None = None
    if pdk_variant is not None or pdk_root is not None:
        try:
            pdk_info = find_pdk(variant=pdk_variant, root=pdk_root)
        except PdkNotFoundError as exc:
            raise ExtractError(str(exc)) from exc

        try:
            deck_for_models = get_extraction_deck(deck_name)
        except UnknownExtractionDeckError as exc:
            raise ExtractError(str(exc)) from exc
        try:
            model_bindings = resolve_device_bindings(
                deck_name, pdk_info["variant"], deck_for_models
            )
        except ModelBindingError as exc:
            raise ExtractError(str(exc)) from exc

    # `--parasitics` resolves the curated per-PDK RC coefficient table for
    # this deck (see `klayout_tools.decks.ParasiticsDeck`); when the flag is
    # omitted the whole parasitics path is skipped and the written SPICE /
    # JSON are byte-identical to before this feature existed (additive, off
    # by default -- see docs/cli/extract.md and issue #216's addendum).
    parasitics_deck: ParasiticsDeck | None = None
    if parasitics:
        try:
            parasitics_deck = get_parasitics_deck(deck_name)
        except UnknownExtractionDeckError as exc:
            raise ExtractError(str(exc)) from exc

    (
        netlist,
        top_cell_name,
        dbu_um,
        warnings,
        parasitic_nets,
        black_box_regions,
        dummy_devices_dropped,
        unmodelled_poly,
    ) = extract_netlist_from_layout(
        path,
        deck_name,
        top=top,
        parasitics_deck=parasitics_deck,
        top_cell_pins_only=top_cell_pins_only,
    )

    import klayout.db as kdb

    netlist_path = output if output is not None else _default_output_path(path)
    out_dir = os.path.dirname(os.path.abspath(netlist_path))
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        raise ExtractError(f"cannot create output directory {out_dir}: {exc}") from exc

    # `netlist.purge()` (in `_extract_netlist`) drops a circuit entirely when
    # it has no devices, no pins, and no subcircuits -- e.g. a layout with no
    # extractable devices and no named nets. That is a legitimate "nothing
    # extracted" result, not an error: report zero devices/nets rather than
    # dereferencing a `None` circuit.
    #
    # `devices[]`/`nets[]` are built from the schematic-equivalent netlist
    # *before* any parasitic R/C is injected, so they carry their exact
    # documented meaning whether or not `--parasitics` was given (the
    # additive-contract requirement from issue #216's addendum): parasitic
    # elements never appear in `device_count`/`devices[]`, and the internal
    # parasitic nodes never appear in `net_count`/`nets[]` -- they live only
    # in the written SPICE and in the separate `parasitics` block below.
    # Already validated by `extract_netlist_from_layout` above (it would have
    # raised `ExtractError` on an unknown deck before reaching this point),
    # so re-fetching it here to read its static device-class coverage
    # (`device_classes`, issue #221) and its `substrate_net` cannot itself
    # raise.
    deck = get_extraction_deck(deck_name)

    circuit = netlist.circuit_by_name(top_cell_name)
    if circuit is not None:
        devices, device_counts = _describe_devices(circuit)
        nets = _describe_nets(circuit)
    else:
        devices, device_counts, nets = [], {}, []

    # Layers carrying shapes the deck's connectivity graph never reads (issue
    # #220): geometry there is invisible to extraction, so surface it rather
    # than let it become a silent LVS mismatch downstream.
    ignored_layers = _describe_ignored_layers(path, deck)

    parasitics_report: dict[str, Any] | None = None
    if parasitic_nets is not None:
        if circuit is not None and parasitic_nets:
            ground_net = deck.substrate_net
            parasitics_report = _inject_parasitics(
                kdb, circuit, parasitic_nets, ground_net
            )
        else:
            parasitics_report = {
                "r_count": 0,
                "c_count": 0,
                "total_resistance_ohm": 0.0,
                "total_capacitance_ff": 0.0,
                "nets": [],
            }

    writer = (
        kdb.NetlistSpiceWriter(create_model_binding_delegate(model_bindings))
        if model_bindings is not None
        else kdb.NetlistSpiceWriter()
    )
    writer.use_net_names = True
    try:
        netlist.write(
            netlist_path, writer, f"extracted by klt extract --deck {deck_name}"
        )
    except Exception as exc:
        raise ExtractError(f"could not write netlist '{netlist_path}': {exc}") from exc

    netlist_sha256 = sha256_file(netlist_path)

    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "file": path,
        "deck": deck_name,
        "top": top_cell_name,
        "dbu_um": dbu_um,
        "netlist_path": netlist_path,
        "netlist_sha256": netlist_sha256,
        "status": "extracted",
        "device_count": len(devices),
        "net_count": len(nets),
        "pin_count": sum(1 for net in nets if net["pin"]),
        "device_counts": dict(sorted(device_counts.items())),
        "dummy_devices_dropped": dummy_devices_dropped,
        "ignored_layers": ignored_layers,
        "device_classes": list(deck.device_classes),
        "devices": devices,
        "nets": nets,
        "warnings": warnings,
        # Additive field (issue #293): always a list, empty when the layout
        # draws no reserved-annotation-layer geometry -- see run_extract's
        # docstring for the field's full meaning.
        "black_box_regions": black_box_regions,
        # Additive field (issue #324): always a list, empty when `warnings`
        # carries no unmodelled-device entry -- see run_extract's docstring
        # and `_detect_unmodelled_poly_bodies` for the field's full meaning.
        "unmodelled_poly": unmodelled_poly,
    }
    if pdk_info is not None:
        result["pdk"] = {
            "variant": pdk_info["variant"],
            "root": pdk_info["root"],
            "version": pdk_info["version"],
        }
    else:
        result["pdk"] = None

    result["provenance"] = build_provenance(
        deck_name=deck_name,
        deck_path=deck_source_path(deck_name),
        pdk=pdk_info,
        input_path=path,
    )

    # Additive, independently-optional field (issue #216 addendum): `null`
    # unless `--parasitics` was given, a `parasitics` summary block otherwise.
    result["parasitics"] = parasitics_report

    return result


def extract_netlist_from_layout(
    path: str,
    deck_name: str,
    top: str | None = None,
    parasitics_deck: ParasiticsDeck | None = None,
    top_cell_pins_only: bool = False,
) -> tuple[
    kdb.Netlist,
    str,
    float,
    list[str],
    list[dict[str, Any]] | None,
    list[dict[str, Any]],
    int,
    list[dict[str, Any]],
]:
    """Core extraction: read ``path``, resolve ``deck_name`` and the top
    cell, and run flat device + connectivity extraction. Returns
    ``(netlist, top_cell_name, dbu_um, warnings, parasitic_nets,
    black_box_regions, dummy_devices_dropped, unmodelled_poly)``.

    ``top_cell_pins_only`` (issue #291): when ``True``, only labels drawn
    directly in the top cell are promoted to top-level pins -- a net named
    solely by a label found below an instance boundary keeps its name but
    stays internal. Independent of the flag, ``warnings`` gains an entry
    whenever a below-top label named a promoted pin. See :func:`run_extract`
    for the full rationale.

    ``parasitics_deck`` is optional: when ``None`` (the default, and what
    ``klt lvs``'s inline-extraction path always passes -- LVS is topological
    and takes no parasitics), no per-net RC geometry is computed and the
    returned ``parasitic_nets`` is ``None``. When a
    :class:`~klayout_tools.decks.ParasiticsDeck` is given, ``parasitic_nets``
    is a list of ``{"net", "resistance_ohm", "capacitance_ff"}`` dicts (one
    per net carrying non-zero parasitics) computed from
    ``LayoutToNetlist.polygons_of_net`` per-net/per-layer geometry -- the
    caller (:func:`run_extract`) injects them into the netlist as ``R``/``C``
    devices before writing. The netlist itself is unchanged by this
    computation (the parasitics are returned as data, not yet injected).

    Shared by :func:`run_extract` (this module, which additionally writes the
    netlist to disk and builds the ``devices``/``nets`` convenience view) and
    ``klt lvs``'s inline-extraction path (``lvs.py``'s ``layout.file`` +
    ``layout.deck`` request shape, per
    ``docs/design/lvs-extraction-spike.md`` section 2b), which composes this
    with ``NetlistComparer`` instead of ``NetlistSpiceWriter`` -- no need to
    round-trip through a written SPICE file just to compare it.

    Raises :class:`ExtractError` for a bad file, unknown deck, or missing/
    ambiguous top cell -- identical error semantics to ``run_extract``.
    """
    if not os.path.exists(path):
        raise ExtractError(f"file not found: {path}")
    if os.path.isdir(path):
        raise ExtractError(f"not a file: {path}")

    try:
        deck = get_extraction_deck(deck_name)
    except UnknownExtractionDeckError as exc:
        raise ExtractError(str(exc)) from exc

    # Imported lazily (after the cheap checks above) so `klt --version` and
    # argument parsing never pay the cost of loading the KLayout database
    # module -- same discipline as `_layout.load_layout`.
    import klayout.db as kdb

    layout = kdb.Layout()
    try:
        layout.read(path)
    except Exception as exc:  # klayout raises RuntimeError for bad/unknown streams
        raise ExtractError(f"could not read layout '{path}': {exc}") from exc

    top_cell = _resolve_top_cell(layout, top, path)
    (
        netlist,
        warnings,
        parasitic_nets,
        black_box_regions,
        dummy_devices_dropped,
        unmodelled_poly,
    ) = _extract_netlist(
        layout, top_cell, deck, parasitics_deck, top_cell_pins_only=top_cell_pins_only
    )
    return (
        netlist,
        top_cell.name,
        layout.dbu,
        warnings,
        parasitic_nets,
        black_box_regions,
        dummy_devices_dropped,
        unmodelled_poly,
    )


def _resolve_top_cell(layout: kdb.Layout, top: str | None, path: str) -> kdb.Cell:
    """Pick the extraction top cell: ``top`` by name if given, else the
    layout's sole top cell (an ambiguous/missing choice is an
    :class:`ExtractError`)."""
    if top is not None:
        cell = layout.cell(top)
        if cell is None:
            raise ExtractError(f"cell '{top}' not found in '{path}'")
        return cell

    top_cells = list(layout.top_cells())
    if len(top_cells) == 0:
        raise ExtractError(f"'{path}' has no top cell")
    if len(top_cells) > 1:
        names = ", ".join(sorted(cell.name for cell in top_cells))
        raise ExtractError(
            f"'{path}' has {len(top_cells)} top cells ({names}); "
            "pass --top to select one"
        )
    return top_cells[0]


def _default_output_path(path: str) -> str:
    """``<file>`` with its extension replaced by ``.spice`` (spike section 2a)."""
    stem, _ext = os.path.splitext(path)
    return f"{stem}.spice"


def _region(
    layout: kdb.Layout, cell: kdb.Cell, layer: tuple[int, int] | None
) -> kdb.Region:
    """A flattened ``Region`` for ``layer`` under ``cell`` (same flattening
    idiom ``drc.py`` uses via ``begin_shapes_rec``), or an empty ``Region``
    when ``layer`` is ``None``/absent from the stream."""
    import klayout.db as kdb

    if layer is None:
        return kdb.Region()
    layer_index = layout.find_layer(*layer)
    if layer_index is None:
        return kdb.Region()
    return kdb.Region(cell.begin_shapes_rec(layer_index))


def _texts(
    layout: kdb.Layout, cell: kdb.Cell, layer: tuple[int, int] | None
) -> kdb.Texts:
    """A flattened ``Texts`` collection for ``layer`` under ``cell``, or empty
    when ``layer`` is ``None``/absent from the stream."""
    import klayout.db as kdb

    if layer is None:
        return kdb.Texts()
    layer_index = layout.find_layer(*layer)
    if layer_index is None:
        return kdb.Texts()
    return kdb.Texts(cell.begin_shapes_rec(layer_index))


def _label_layer_strings(
    layout: kdb.Layout,
    cell: kdb.Cell,
    layers: list[tuple[int, int] | None],
    *,
    recursive: bool,
) -> set[str]:
    """The set of text strings on ``layers`` under ``cell`` (issue #291).

    ``recursive=False`` reads only shapes drawn *directly* in ``cell``
    (``cell.shapes``); ``recursive=True`` reads the whole sub-tree
    (``begin_shapes_rec``, the same flatten :func:`_texts` uses). The
    difference -- strings that appear recursively but not directly in the top
    cell -- is exactly the set of labels that live below an instance boundary,
    i.e. sub-cell port names that are internal nodes once instanced.

    ``None`` layers (a deck that declares no such label layer) and layers
    absent from the stream contribute nothing.
    """
    import klayout.db as kdb

    strings: set[str] = set()
    for layer in layers:
        if layer is None:
            continue
        layer_index = layout.find_layer(*layer)
        if layer_index is None:
            continue
        shapes = (
            cell.begin_shapes_rec(layer_index)
            if recursive
            else cell.shapes(layer_index)
        )
        for text in kdb.Texts(shapes).each():
            strings.add(text.string)
    return strings


def _reconcile_top_pins(
    netlist: kdb.Netlist,
    top_name: str,
    below_top_labels: set[str],
    *,
    demote: bool,
) -> list[str]:
    """Reconcile the top circuit's pins against ``below_top_labels`` (issue
    #291), the label strings that name a net only from below an instance
    boundary.

    ``make_top_level_pins()`` has already promoted every named net. This finds
    the promoted pins whose net name is a below-top label and, when
    ``demote`` is set, removes those pins (the net keeps its name and stays an
    internal node). Returns the sorted, de-duplicated net names affected --
    the input to the caller's ``warnings`` entry, whether or not they were
    actually demoted.

    Global/substrate nets (named by ``connect_global``, not by any drawn text)
    are never in ``below_top_labels``, so a substrate pin is left untouched.
    """
    circuit = netlist.circuit_by_name(top_name)
    if circuit is None or not below_top_labels:
        return []

    affected: set[str] = set()
    to_remove: list[int] = []
    for pin in circuit.each_pin():
        net = circuit.net_for_pin(pin.id())
        if net is None:
            continue
        name = net.name
        if name and name in below_top_labels:
            affected.add(name)
            if demote:
                to_remove.append(pin.id())

    for pin_id in to_remove:
        circuit.remove_pin(pin_id)

    return sorted(affected)


def _resolve_black_box_regions(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    active: kdb.Region,
    poly: kdb.Region,
    nwell: kdb.Region,
    tap: kdb.Region,
    contact: kdb.Region,
    metals: list[kdb.Region],
    vias: list[kdb.Region],
    well_label: kdb.Texts,
    poly_label: kdb.Texts,
    metal_labels: list[kdb.Texts],
) -> tuple[
    list[dict[str, Any]],
    kdb.Region,
    kdb.Region,
    kdb.Region,
    kdb.Region,
    kdb.Region,
    list[kdb.Region],
    list[kdb.Region],
    kdb.Texts,
    kdb.Texts,
    list[kdb.Texts],
]:
    """Resolve black-box/abstract regions (issue #293) against this layout.

    A shape drawn on any reserved annotation layer (990-999, any datatype --
    see :func:`is_reserved_annotation_layer` in ``_annotation.py``) directly
    under ``top_cell`` marks a region whose contents are deliberately out of
    scope for connectivity: a sub-cell that will be drawn later (its
    hierarchy/area needs to be recorded now, its content doesn't yet), or a
    drawn region that is deliberately out of scope for a compare. Everything
    geometrically inside such a marker shape is excluded from the
    connectivity graph *before* any device extractor runs -- the same "cut a
    hole in the conductor region" shape :func:`_resolve_resistors` below
    uses for a single conductor layer, generalised here to every conductor/
    label layer this deck's connectivity graph reads.

    Returns ``(black_box_regions, active, poly, nwell, tap, contact, metals,
    vias, well_label, poly_label, metal_labels)`` where ``black_box_regions``
    is the JSON response's new field (one entry per geometrically separate
    marker shape -- non-touching marker shapes are reported individually,
    never merged into one bbox) and the remaining values are the caller's
    originals with every black-box region **subtracted**. A layout with no
    reserved-layer geometry returns the inputs unchanged and an empty list,
    so extraction of a layout that never uses this feature is bit-for-bit
    what it was before this feature existed.

    ``black_box_regions[].shapes_excluded`` counts, per marked region, the
    conductor/label shapes that actually overlap it (summed across every
    layer subtracted below) -- the signal that the exclusion did something,
    not just that a marker shape exists somewhere in the stream.

    The marker layer itself is never registered with ``l2n`` (nothing else in
    this module calls :func:`_region`/:func:`_texts` for a reserved layer),
    so it stays absent from ``ignored_layers`` exactly as it was before this
    feature existed -- see ``docs/cli/extract.md``'s "Reserved annotation
    layer".
    """
    import klayout.db as kdb

    marker = kdb.Region()
    for layer_index in layout.layer_indexes():
        info = layout.get_info(layer_index)
        if is_reserved_annotation_layer(info.layer, info.datatype):
            marker += kdb.Region(top_cell.begin_shapes_rec(layer_index))
    marker = marker.merged()

    if marker.is_empty():
        return (
            [],
            active,
            poly,
            nwell,
            tap,
            contact,
            metals,
            vias,
            well_label,
            poly_label,
            metal_labels,
        )

    dbu = layout.dbu
    conductor_regions = [active, poly, nwell, tap, contact, *metals, *vias]
    label_collections = [well_label, poly_label, *metal_labels]

    black_box_regions: list[dict[str, Any]] = []
    for component in marker.each():
        component_region = kdb.Region(component)
        shapes_excluded = sum(
            region.interacting(component_region).count() for region in conductor_regions
        ) + sum(
            texts.interacting(component_region).count() for texts in label_collections
        )
        box = component.bbox()
        black_box_regions.append(
            {
                "bbox_um": {
                    "left": round(box.left * dbu, _PARAM_PRECISION_UM),
                    "bottom": round(box.bottom * dbu, _PARAM_PRECISION_UM),
                    "right": round(box.right * dbu, _PARAM_PRECISION_UM),
                    "top": round(box.top * dbu, _PARAM_PRECISION_UM),
                },
                "shapes_excluded": shapes_excluded,
            }
        )

    black_box_regions.sort(
        key=lambda entry: (entry["bbox_um"]["left"], entry["bbox_um"]["bottom"])
    )

    return (
        black_box_regions,
        active - marker,
        poly - marker,
        nwell - marker,
        tap - marker,
        contact - marker,
        [region - marker for region in metals],
        [region - marker for region in vias],
        well_label.not_interacting(marker),
        poly_label.not_interacting(marker),
        [texts.not_interacting(marker) for texts in metal_labels],
    )


def _resolve_resistors(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    deck: ExtractionDeck,
    poly: kdb.Region,
    active: kdb.Region,
    metals: list[kdb.Region],
) -> tuple[
    list[tuple[ResistorDevice, kdb.Region, kdb.Region]],
    kdb.Region,
    kdb.Region,
    list[kdb.Region],
]:
    """Resolve the deck's drawn-resistor declarations against this layout.

    Returns ``(resistors, poly, active, metals)`` where ``resistors`` is one
    ``(spec, body_region, terminal_region)`` triple per *recognised* device
    class and the three conductor regions are the deck's originals with
    every recognised resistor body **subtracted** -- so the caller's
    connectivity graph (and its MOS gate/source-drain split) sees the
    resistor's heads as ordinary conductor and the resistive segment as a
    hole, instead of one continuous short (see
    :class:`~klayout_tools.decks.ResistorDevice`).

    A resistor body is ``body_layer & marker & all(requires) - any(excludes)``.
    A spec whose body region comes out empty on this layout (the common case
    -- no PDK resistor marker drawn anywhere) is dropped entirely and
    subtracts nothing, so extraction of a resistor-free layout is bit-for-bit
    what it was before this feature existed.

    Raises :class:`ExtractError` for a deck-authoring mistake (a ``body``/
    ``terminal`` layer that is not one of the deck's own conductor layers),
    since the terminal region must be a layer the connectivity graph already
    carries.
    """
    if not deck.resistors:
        return [], poly, active, metals

    # Keyed by drawn conductor layer so a resistor declared on `poly` is cut
    # out of the very same Region the connectivity graph uses.
    bases: dict[tuple[int, int], kdb.Region] = {deck.poly: poly, deck.active: active}
    for index, layer in enumerate(deck.metals):
        bases.setdefault(layer, metals[index])

    def _conductor(layer: tuple[int, int], field: str, name: str) -> kdb.Region:
        try:
            return bases[layer]
        except KeyError:
            raise ExtractError(
                f"resistor '{name}': {field} layer {layer[0]}/{layer[1]} is not one "
                "of the deck's conductor layers (active/poly/metals)"
            ) from None

    recognised: list[tuple[ResistorDevice, kdb.Region, tuple[int, int]]] = []
    for spec in deck.resistors:
        base = _conductor(spec.body, "body", spec.name)
        terminal_layer = spec.terminal if spec.terminal is not None else spec.body
        _conductor(terminal_layer, "terminal", spec.name)

        body = base & _region(layout, top_cell, spec.marker)
        for layer in spec.requires:
            body = body & _region(layout, top_cell, layer)
        for layer in spec.excludes:
            body = body - _region(layout, top_cell, layer)
        if body.is_empty():
            continue
        recognised.append((spec, body, terminal_layer))

    for spec, body, _terminal_layer in recognised:
        bases[spec.body] = bases[spec.body] - body

    resistors = [
        (spec, body, bases[terminal_layer]) for spec, body, terminal_layer in recognised
    ]
    return (
        resistors,
        bases[deck.poly],
        bases[deck.active],
        [bases[layer] for layer in deck.metals],
    )


def _capacitor_plate_regions(
    layout: kdb.Layout, top_cell: kdb.Cell, capacitor: CapacitorDevice
) -> tuple[kdb.Region, kdb.Region]:
    """The recognised ``(top_region, bottom_region)`` pair for one
    :class:`CapacitorDevice` entry against this layout.

    Shared between the main capacitor-recognition loop in
    ``_extract_netlist`` and :func:`_exclude_capacitor_top_via_overlap`
    below (issue #364), so the two never drift apart on what counts as
    "this capacitor's bottom plate" -- the overlap exclusion must be
    computed against exactly the same (possibly virtual-bottom-plate-
    clipped, requires/excludes-narrowed) region the capacitor device itself
    is later registered and extracted against.

    Either region comes back empty when the capacitor's markers are not
    drawn anywhere on this layout (the common case -- no PDK cap marker
    drawn at all).
    """
    top_region = _region(layout, top_cell, capacitor.top_plate)
    for layer in capacitor.top_plate_requires:
        top_region = top_region & _region(layout, top_cell, layer)
    for layer in capacitor.top_plate_excludes:
        top_region = top_region - _region(layout, top_cell, layer)

    bottom_conductor = _region(layout, top_cell, capacitor.bottom_plate)
    for layer in capacitor.bottom_plate_requires:
        bottom_conductor = bottom_conductor & _region(layout, top_cell, layer)
    for layer in capacitor.bottom_plate_excludes:
        bottom_conductor = bottom_conductor - _region(layout, top_cell, layer)

    if capacitor.bottom_plate_oversize_um:
        # "Virtual bottom plate" derivation (e.g. gf180mcu's MiM stack): only
        # bottom-conductor shapes that already touch the *unsized* top plate
        # count (`interacting`), then clipped to the top plate's oversized
        # outline for the exact overlap area -- the same two-step derivation
        # the PDK's own official KLayout LVS deck uses (see
        # `CapacitorDevice`'s docstring).
        oversize_dbu = int(round(capacitor.bottom_plate_oversize_um / layout.dbu))
        bottom_region = bottom_conductor.interacting(top_region) & (
            top_region.sized(oversize_dbu)
        )
    else:
        bottom_region = bottom_conductor

    return top_region, bottom_region


def _exclude_capacitor_top_via_overlap(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    deck: ExtractionDeck,
    vias: list[kdb.Region],
) -> list[kdb.Region]:
    """Exclude each capacitor's own ``top_plate_via ∩ bottom_plate`` overlap
    from the deck's generic ``vias[]`` layers (issue #364), before those
    layers are registered into the connectivity graph and consumed by
    ``_extract_netlist``'s generic per-layer ``metals[i]``/``vias[i]`` loop.

    A capacitor's ``top_plate_via`` (#314) is wired directly to the
    recognised top-plate region and on to ``top_plate_via_metal`` -- but
    without this exclusion, that *same* via shape also reaches the deck's
    generic per-layer connectivity loop, which connects every ``vias[i]``
    shape to whichever ``metals[i]``/``metals[i + 1]`` conductor it
    geometrically touches. A top-plate via placed per the PDK's own
    minimum-overlap rule for that via (which *requires* the bottom plate to
    enclose/overlap it, not clear it) inevitably touches the bottom-plate
    conductor beneath it in plan view, so the generic loop reads that
    DRM-legal overlap as an ordinary via shorting the two plates together --
    a false short (the extraction engine has no notion of the dielectric
    that keeps the via from actually reaching the bottom plate in real
    silicon).

    Only the *geometric intersection* of the via footprint with the
    capacitor's own recognised bottom-plate region is cut, not the whole via
    shape/component: a via landing pad that only partially overlaps the
    bottom plate keeps the rest of its footprint in the generic connectivity
    graph, and any *other* via shape drawn on the same physical via layer
    elsewhere in the layout -- ordinary routing unrelated to this capacitor
    -- is left untouched.

    Returns a new ``vias`` list (the input list/regions are not mutated); a
    deck with no capacitor declaring ``top_plate_via``, or whose declared
    ``top_plate_via`` is not one of the deck's own ``vias`` layers (so it
    never reaches the generic loop in the first place), returns the input
    list unchanged.
    """
    import klayout.db as kdb

    exclusions: dict[int, kdb.Region] = {}
    for capacitor in deck.capacitors:
        if capacitor.top_plate_via is None:
            continue
        if capacitor.top_plate_via not in deck.vias:
            # Not one of this deck's tracked via layers -- the generic loop
            # below never touches it, so there is nothing to exclude (the
            # deck-authoring validation for a mismatched
            # `top_plate_via`/`top_plate_via_metal` pair is the main
            # capacitor loop's job, not this helper's).
            continue
        via_index = deck.vias.index(capacitor.top_plate_via)
        top_via_region = _region(layout, top_cell, capacitor.top_plate_via)
        if top_via_region.is_empty():
            continue
        _top_region, bottom_region = _capacitor_plate_regions(
            layout, top_cell, capacitor
        )
        if bottom_region.is_empty():
            continue
        overlap = top_via_region & bottom_region
        if overlap.is_empty():
            continue
        exclusions[via_index] = exclusions.get(via_index, kdb.Region()) + overlap

    if not exclusions:
        return vias
    return [
        region - exclusions[index] if index in exclusions else region
        for index, region in enumerate(vias)
    ]


#: Minimum number of geometrically separate `contact` clusters a candidate
#: poly component must touch to be flagged by `_detect_unmodelled_poly_bodies`
#: -- the "resistor-body signature": a two-terminal conductor segment
#: contacted at *each* end, rather than routing with a single landing pad.
_UNMODELLED_POLY_MIN_CONTACT_CLUSTERS = 2


def _detect_unmodelled_poly_bodies(
    poly: kdb.Region,
    contact: kdb.Region,
    nfet_gate: kdb.Region,
    pfet_gate: kdb.Region,
    resistor_markers: kdb.Region | None = None,
    resistor_bodies: kdb.Region | None = None,
    dbu: float = 1.0,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Flag ``poly`` connected components that no MOS gate extractor claims
    and that touch ``contact`` at two or more geometrically separate
    locations -- the resistor-body signature (issue #288).

    This deck's device extractors are a fixed, curated subset (today:
    ``nfet``/``pfet``, an optional declared drawn resistor/bipolar/
    capacitor). Geometry drawn for any device class the deck does not
    (yet) recognise is still built out of ordinary connectivity layers
    (``poly``, ``contact``, ...), so :func:`_extract_netlist`'s blanket
    ``l2n.connect(poly, contact)`` (and friends) absorbs it into ordinary
    interconnect -- silently shorting two terminals a schematic keeps
    distinct, with zero signal in ``warnings`` today. This is a narrowly
    scoped *diagnostic* heuristic, not a device extractor: it identifies
    the shape, not the missing device class, and is deliberately
    conservative to avoid false positives on real layouts (see
    ``docs/cli/extract.md``'s "Known limitation: unmodelled device
    geometry").

    Called with ``poly`` as it stands *after* :func:`_resolve_resistors`
    has already subtracted out every resistor body this deck's own
    ``ResistorDevice`` declarations recognise -- so a properly marked
    drawn resistor never reaches this heuristic at all. What is left is
    only genuinely unrecognised geometry: a resistor drawn without (or
    excluded from) the deck's marker layer, or any other not-yet-modelled
    device class that happens to share poly + contact.

    A component is skipped unconditionally if it touches ``nfet_gate``/
    ``pfet_gate`` anywhere -- a real MOS gate (with or without a
    legitimate poly-contacted gate strap) or ordinary poly routing between
    two recognised gates (poly needs no via to route on itself, so both
    ends of such a run are one merged polygon together with the gates
    themselves), never a candidate unmodelled-device body. It is likewise
    skipped unconditionally if it touches ``resistor_bodies`` (issue #324) --
    the recognised body regions ``_resolve_resistors`` already cut out of
    ``poly`` -- since a poly component *abutting* one of those bodies is that
    resistor's own terminal head, by construction: the head survives as a
    separate connected component once its body is subtracted, and legitimately
    carries a normal (2+) contact array with no device-recognition gap behind
    it at all.

    ``resistor_markers`` (issue #299) is the union of every ``ResistorDevice``
    marker layer this deck declares on ``poly`` (regardless of any
    ``requires``/``excludes`` narrowing) -- the raw resistor-ID geometry, not
    the narrower recognised-body region ``_resolve_resistors`` already cut
    out. It distinguishes two different reasons a flagged component reaches
    this heuristic at all: a component overlapping it carries a resistor
    marker this deck *knows about* but whose ``requires``/``excludes``
    conditions this specific segment did not satisfy (a **deck-coverage
    gap** -- e.g. gf180mcu's ``RES_MK`` present without the ``SAB``/``Pplus``
    combination any declared entry needs), versus a component that carries
    none of this deck's resistor markers at all (an **unmarked** shape, the
    original #288 case -- some other, entirely undeclared device class, or a
    resistor with no marker drawn). ``None`` (the default) treats every
    flagged component as unmarked, matching this function's behaviour before
    #299.

    ``resistor_bodies`` (issue #324) is the union of every *recognised*
    ``ResistorDevice`` body region ``_resolve_resistors`` returned for this
    layout (already narrowed by each entry's own ``requires``/``excludes``) --
    distinct from ``resistor_markers`` above, which is the raw, unnarrowed
    marker geometry. ``None`` (the default) skips no component on this basis,
    matching this function's behaviour before #324.

    ``dbu`` converts each flagged component's bounding box from database
    units to micrometres for ``unmodelled_poly[]`` (see below); defaults to
    ``1.0`` (i.e. no conversion) for callers that only need the warning
    strings.

    Returns ``(warnings, unmodelled_poly)``. ``warnings`` has up to two
    strings (empty when nothing is flagged) -- at most one for unmarked
    shapes and one for marked-but-unrecognised ones -- each naming how many
    components were flagged and pointing at the documented limitation rather
    than guessing a device name. ``unmodelled_poly`` (issue #324) is one
    entry per flagged component -- ``{"bbox_um": {"left", "bottom", "right",
    "top"}, "reason": "unmarked" | "marked_unrecognised"}`` -- sorted by
    ``(left, bottom)`` for deterministic output, so a consumer can enumerate
    and triage the exact flagged shapes instead of re-deriving them by
    re-implementing this heuristic against the stream. Always a list, empty
    when ``warnings`` is empty.
    """
    import klayout.db as kdb

    gate_regions = nfet_gate + pfet_gate
    bodies = resistor_bodies if resistor_bodies is not None else kdb.Region()
    markers = resistor_markers if resistor_markers is not None else kdb.Region()
    unmarked = 0
    marked_unrecognised = 0
    unmodelled_poly: list[dict[str, Any]] = []
    for component in poly.merged().each():
        candidate = kdb.Region(component)
        if not candidate.interacting(gate_regions).is_empty():
            continue
        if not candidate.interacting(bodies).is_empty():
            continue
        contact_clusters = (candidate & contact).merged().count()
        if contact_clusters < _UNMODELLED_POLY_MIN_CONTACT_CLUSTERS:
            continue
        if candidate.interacting(markers).is_empty():
            unmarked += 1
            reason = "unmarked"
        else:
            marked_unrecognised += 1
            reason = "marked_unrecognised"
        box = component.bbox()
        unmodelled_poly.append(
            {
                "bbox_um": {
                    "left": round(box.left * dbu, _PARAM_PRECISION_UM),
                    "bottom": round(box.bottom * dbu, _PARAM_PRECISION_UM),
                    "right": round(box.right * dbu, _PARAM_PRECISION_UM),
                    "top": round(box.top * dbu, _PARAM_PRECISION_UM),
                },
                "reason": reason,
            }
        )

    unmodelled_poly.sort(
        key=lambda entry: (entry["bbox_um"]["left"], entry["bbox_um"]["bottom"])
    )

    warnings: list[str] = []
    if unmarked:
        shape_word = "shape" if unmarked == 1 else "shapes"
        warnings.append(
            f"{unmarked} poly-layer {shape_word} not part of any recognised nfet/pfet "
            "gate touch contact at 2+ separate points (the resistor-body "
            "signature) and carry no resistor-marker layer at all; this deck "
            "may not model the device class drawn here, and its terminals "
            "have been absorbed into ordinary interconnect as an unintended "
            "short -- see docs/cli/extract.md's 'Known limitation: unmodelled "
            "device geometry'."
        )
    if marked_unrecognised:
        shape_word = "shape" if marked_unrecognised == 1 else "shapes"
        warnings.append(
            f"{marked_unrecognised} poly-layer {shape_word} not part of any "
            "recognised nfet/pfet gate touch contact at 2+ separate points "
            "(the resistor-body signature) and carry a resistor-marker layer, "
            "but do not match any of this deck's declared ResistorDevice "
            "requires/excludes conditions (a deck-coverage gap, not unmarked "
            "geometry); their terminals have been absorbed into ordinary "
            "interconnect as an unintended short -- see docs/cli/extract.md's "
            "'Known limitation: unmodelled device geometry'."
        )
    return warnings, unmodelled_poly


def _reject_degenerate_substrate_bipolar(
    bipolar: BipolarDevice,
    bipolar_base: kdb.Region,
    bipolar_emitter: kdb.Region,
) -> None:
    """Raise a clean :class:`ExtractError` for bipolar geometry KLayout's
    ``DeviceExtractorBJT3Transistor`` cannot form a substrate collector from
    (issue #432).

    A :class:`~klayout_tools.decks.BipolarDevice` with no drawn ``collector``
    layer models a vertical device whose collector *is* the substrate: the
    extractor forms the ``C`` terminal from the base area left **outside** the
    emitter. When a base region is entirely covered by its own emitter -- what
    a device-mark layer drawn exactly coincident with the emitter pad produces,
    since both curated decks derive ``base = well & marker`` and
    ``emitter = active & base`` -- no such area exists, and
    ``LayoutToNetlist.extract_netlist`` aborts with a raw ``RuntimeError``
    ("Terminal 'C' ... isn't connected"). Diagnosing it here turns that into
    the documented error envelope with an actionable message, before the
    cryptic engine-level failure can surface.
    """
    import klayout.db as kdb

    degenerate = 0
    for polygon in bipolar_base.merged().each():
        if (kdb.Region(polygon) - bipolar_emitter).is_empty():
            degenerate += 1
    if not degenerate:
        return
    region_word = "region" if degenerate == 1 else "regions"
    raise ExtractError(
        f"bipolar device '{bipolar.class_name}': {degenerate} base {region_word} "
        f"(layer {bipolar.base[0]}/{bipolar.base[1]} & marker "
        f"{bipolar.marker[0]}/{bipolar.marker[1]}) fully covered by the emitter "
        f"(layer {bipolar.emitter[0]}/{bipolar.emitter[1]}) -- no base area is "
        "left outside the emitter for this deck's substrate collector terminal; "
        "grow the device-mark layer so the base strictly encloses the emitter"
    )


def _extract_netlist(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    deck: ExtractionDeck,
    parasitics_deck: ParasiticsDeck | None = None,
    top_cell_pins_only: bool = False,
) -> tuple[
    kdb.Netlist,
    list[str],
    list[dict[str, Any]] | None,
    list[dict[str, Any]],
    int,
    list[dict[str, Any]],
]:
    """Build a flat ``LayoutToNetlist`` connectivity graph for ``deck`` and
    run device + netlist extraction.

    Flat (not hierarchical) extraction, deliberately: every layer is a
    single flattened ``Region``/``Texts`` collection over ``top_cell`` (via
    ``begin_shapes_rec``), the same whole-layout flattening idiom
    ``drc.py`` uses -- see ``docs/cli/extract.md``'s limitation note.

    Returns ``(netlist, warnings, parasitic_nets, black_box_regions,
    dummy_devices_dropped, unmodelled_poly)``.
    ``warnings`` is built from the extractor's own log entries (e.g. a gate
    touching no diffusion) -- non-fatal notes surfaced in the JSON response's
    ``warnings`` field. ``parasitic_nets`` is ``None`` unless
    ``parasitics_deck`` is given, in which case it is the per-net lumped-RC
    data computed from ``LayoutToNetlist.polygons_of_net`` while ``l2n`` is
    still alive (see :func:`_compute_parasitics`). ``black_box_regions`` is
    the JSON response's field (issue #293) -- see
    :func:`_resolve_black_box_regions` -- always a list, empty when the
    layout draws no reserved-annotation-layer geometry.
    ``dummy_devices_dropped`` is the number of MOS gates suppressed by the
    deck's optional ``dummy`` marker layer (issue #295), ``0`` when no
    ``dummy`` layer is configured or no dummy geometry is drawn.
    ``unmodelled_poly`` is the JSON response's field (issue #324) -- see
    :func:`_detect_unmodelled_poly_bodies` -- always a list, one entry per
    poly component flagged by the unmodelled-device diagnostic, empty when
    ``warnings`` carries no unmodelled-device entry.
    """
    import klayout.db as kdb

    active = _region(layout, top_cell, deck.active)
    poly = _region(layout, top_cell, deck.poly)
    nwell = _region(layout, top_cell, deck.nwell)
    tap = _region(layout, top_cell, deck.tap)
    contact = _region(layout, top_cell, deck.contact)
    well_label = _texts(layout, top_cell, deck.well_label)
    poly_label = _texts(layout, top_cell, deck.poly_label)
    metals = [_region(layout, top_cell, layer) for layer in deck.metals]
    metal_labels = [_texts(layout, top_cell, layer) for layer in deck.metal_labels]
    vias = [_region(layout, top_cell, layer) for layer in deck.vias]

    # Black-box/abstract regions (#293), resolved *before* everything else
    # below (including the resistor resolution that follows): geometry
    # inside a marker shape on a reserved annotation layer (990-999, any
    # datatype -- issue #289) is masked out of every conductor/label region
    # first, so a resistor marker (or any other device-recognition geometry)
    # that happens to sit inside a black-box region is excluded outright
    # rather than "found" by a later device extractor and only then
    # short-circuited.
    (
        black_box_regions,
        active,
        poly,
        nwell,
        tap,
        contact,
        metals,
        vias,
        well_label,
        poly_label,
        metal_labels,
    ) = _resolve_black_box_regions(
        layout,
        top_cell,
        active,
        poly,
        nwell,
        tap,
        contact,
        metals,
        vias,
        well_label,
        poly_label,
        metal_labels,
    )

    # Drawn precision resistors (#222), resolved *before* the MOS split
    # below: a recognised resistor body is cut out of its own conductor
    # layer, so (a) the two heads are no longer shorted through it, and (b)
    # a poly resistor crossing diffusion cannot also be mistaken for a gate
    # -- the same ordering both PDKs' own KLayout LVS decks use (sky130's
    # `tgate = poly.and(diff).not(poly_res)...`).
    resistors, poly, active, metals = _resolve_resistors(
        layout, top_cell, deck, poly, active, metals
    )

    # MiM top-plate-via / bottom-plate overlap exclusion (issue #364): must
    # run before `vias` is registered into the netlist graph below and
    # before the generic per-layer `metals[i]`/`vias[i]` connectivity loop
    # consumes it -- see `_exclude_capacitor_top_via_overlap`'s docstring.
    # Without this, a capacitor's own `top_plate_via` (#314), placed per the
    # PDK's DRM-legal minimum-overlap requirement against its bottom plate,
    # is read by that generic loop as an ordinary via shorting the two
    # plates together.
    vias = _exclude_capacitor_top_via_overlap(layout, top_cell, deck, vias)

    # NMOS is active outside the well; PMOS is active inside it -- KLayout's
    # standard "well marks the flip side" MOS-splitting idiom (see
    # `ExtractionDeck`'s docstring). Splitting SD from the gate polygon
    # (rather than passing the undivided active region) is required by
    # `DeviceExtractorMOS4Transistor`'s "SD" input contract: it expects two
    # disjoint source/drain polygons per gate, which only exist once the
    # gate area is subtracted out.
    nfet_active = active - nwell
    pfet_active = active & nwell
    nfet_gate = nfet_active & poly
    pfet_gate = pfet_active & poly
    nfet_sd = nfet_active - poly
    pfet_sd = pfet_active - poly

    # Unmodelled-device diagnostic (issue #288, split by marker presence in
    # #299, resistor-body/routing false positives narrowed in #324):
    # computed against `poly` as it stands right here -- *after*
    # `_resolve_resistors` has already cut out every resistor body this deck
    # *does* recognise, and *before* the blanket `l2n.connect(poly, contact)`
    # (and friends) below absorbs whatever is left into ordinary
    # interconnect. `poly_resistor_markers` is the raw union of every
    # declared `ResistorDevice.marker` on `poly` (unnarrowed by that entry's
    # own `requires`/`excludes`), letting the heuristic tell a "carries a
    # marker this deck knows about, but requires/excludes ruled it out" gap
    # apart from "carries no marker at all" -- see the function's docstring.
    # `poly_resistor_bodies` is the union of every *recognised* resistor body
    # `_resolve_resistors` returned above (already narrowed by
    # requires/excludes) -- a poly component abutting one of these is that
    # resistor's own terminal head, by construction, so it must never be
    # flagged as a candidate unmodelled-device body (issue #324).
    poly_resistor_markers = kdb.Region()
    for spec in deck.resistors:
        if spec.body == deck.poly:
            poly_resistor_markers += _region(layout, top_cell, spec.marker)
    poly_resistor_bodies = kdb.Region()
    for spec, body, _terminal in resistors:
        if spec.body == deck.poly:
            poly_resistor_bodies += body
    unmodelled_device_warnings, unmodelled_poly = _detect_unmodelled_poly_bodies(
        poly,
        contact,
        nfet_gate,
        pfet_gate,
        poly_resistor_markers,
        poly_resistor_bodies,
        layout.dbu,
    )

    # Dummy-device suppression (issue #295): a deck may declare an optional
    # `dummy` marker layer (see `ExtractionDeck.dummy`) covering drawn-but-
    # non-functional dummy devices -- the matched-pair/array edge fill whose
    # gate and diffusions are tied off to a rail. A MOS gate lying under that
    # marker must not become a device in the extracted netlist (otherwise
    # every dummy is a spurious `device.unmatched` under `klt lvs`), so
    # subtract the marker from the NMOS/PMOS gate regions *before* device
    # recognition: a gate fully covered by the marker is never handed to
    # `extract_devices` and so is never recognised as a device at all. Only
    # the gate is cut -- the dummy's diffusions (`nfet_sd`/`pfet_sd`) and its
    # gate poly stay in `poly`, so they still participate in ordinary
    # connectivity below and tie off to the rail exactly as drawn.
    #
    # Ordered *after* the unmodelled-device diagnostic above (so a dummy gate
    # is still recognised as a gate there and never misflagged as unmodelled
    # poly) and *before* registration/extraction below (so the suppressed gate
    # area reaches neither the device extractor nor the parasitics pass).
    # `dummy_devices_dropped` counts gate components fully consumed by the
    # marker -- a device that genuinely vanishes -- using the same
    # `region.merged().each()` connected-component idiom as
    # `_detect_unmodelled_poly_bodies`. A marker only partially covering a
    # gate is a clean geometric cut, not a dropped device: the remaining gate
    # area still extracts, so it is not counted.
    dummy = _region(layout, top_cell, deck.dummy)
    dummy_devices_dropped = 0
    if not dummy.is_empty():
        for gate in (nfet_gate, pfet_gate):
            for component in gate.merged().each():
                if (kdb.Region(component) - dummy).is_empty():
                    dummy_devices_dropped += 1
        nfet_gate = nfet_gate - dummy
        pfet_gate = pfet_gate - dummy

    l2n = kdb.LayoutToNetlist(top_cell.name, layout.dbu)
    # `register` returns the layer index `polygons_of_net(net, index)` needs
    # for the per-net geometry the parasitics pass reads back (see
    # `_compute_parasitics`); capture the ones the RC roles map to.
    layer_index: dict[str, int] = {}
    for name, region in [
        ("nfet_sd", nfet_sd),
        ("nfet_gate", nfet_gate),
        ("pfet_sd", pfet_sd),
        ("pfet_gate", pfet_gate),
        ("poly", poly),
        ("contact", contact),
        ("nwell", nwell),
        ("tap", tap),
    ]:
        layer_index[name] = l2n.register(region, name)
    metal_index: list[int] = []
    for index, region in enumerate(metals):
        metal_index.append(l2n.register(region, f"metal{index}"))
    for index, region in enumerate(vias):
        l2n.register(region, f"via{index}")
    l2n.register(well_label, "well_label")
    l2n.register(poly_label, "poly_label")
    for index, texts in enumerate(metal_labels):
        l2n.register(texts, f"metal{index}_label")

    # NMOS body has no drawn substrate-tap geometry in this curated deck (see
    # the family deck's docstring); tie it to the deck's global substrate
    # net instead of leaving it floating. The same empty, globally-connected
    # region doubles as the bulk terminal of any declared resistor with
    # `bulk_to_substrate` (#222), which carries the identical approximation.
    nfet_body = kdb.Region()
    l2n.register(nfet_body, "nfet_body")

    nfet_extractor = kdb.DeviceExtractorMOS4Transistor(deck.nfet_class)
    pfet_extractor = kdb.DeviceExtractorMOS4Transistor(deck.pfet_class)
    l2n.extract_devices(nfet_extractor, {"SD": nfet_sd, "G": nfet_gate, "W": nfet_body})
    l2n.extract_devices(pfet_extractor, {"SD": pfet_sd, "G": pfet_gate, "W": nwell})

    # Bipolar (BJT) device recognition (issue #223): each of the deck's
    # optional `bipolars` entries (see `BipolarDevice` in `decks/__init__.py`)
    # scopes recognition to genuine device-cell instances by intersecting the
    # deck's own MOS-recognition `base`/`emitter` layers with the PDK's
    # bipolar device-mark `marker` layer *before* handing them to KLayout's
    # `DeviceExtractorBJT3Transistor` -- without that intersection, every
    # ordinary PMOS nwell in the layout would be misrecognised as a bipolar
    # base. `bipolar_regions` carries the built regions through to the
    # connectivity section below (registration/extraction must happen once
    # per entry, before any layer can be used in a `connect()` call).
    bipolar_regions: list[tuple[BipolarDevice, kdb.Region, kdb.Region, kdb.Region]] = []
    for bipolar in deck.bipolars:
        bipolar_base_layer = _region(layout, top_cell, bipolar.base)
        bipolar_marker = _region(layout, top_cell, bipolar.marker)
        bipolar_emitter_layer = _region(layout, top_cell, bipolar.emitter)
        bipolar_base = bipolar_base_layer & bipolar_marker
        bipolar_emitter = bipolar_emitter_layer & bipolar_base
        # Narrow the emitter the same requires/excludes idiom the resistor
        # (`_resolve_resistors`) and capacitor blocks use, so a base-contact
        # ring drawn on the *same* diffusion layer inside the same marked base
        # is not misrecognised as a second emitter/device (issue #302). No-op
        # when the entry declares neither field (both default to `()`).
        for layer in bipolar.emitter_requires:
            bipolar_emitter = bipolar_emitter & _region(layout, top_cell, layer)
        for layer in bipolar.emitter_excludes:
            bipolar_emitter = bipolar_emitter - _region(layout, top_cell, layer)
        l2n.register(bipolar_base, f"{bipolar.class_name}_base")
        l2n.register(bipolar_emitter, f"{bipolar.class_name}_emitter")
        if bipolar.collector is not None:
            bipolar_collector_layer = _region(layout, top_cell, bipolar.collector)
            bipolar_collector = bipolar_collector_layer & bipolar_base
        else:
            # No drawn collector layer for this PDK's vertical bipolar --
            # `DeviceExtractorBJT3Transistor` treats an empty `C` input as
            # "collector formed by the substrate" and outputs the base
            # region's own footprint onto it (see `BipolarDevice`'s
            # docstring); `connect_global` below ties that footprint to the
            # deck's substrate net, mirroring `nfet_body` above.
            bipolar_collector = kdb.Region()
        l2n.register(bipolar_collector, f"{bipolar.class_name}_collector")

        if bipolar.collector is None:
            _reject_degenerate_substrate_bipolar(bipolar, bipolar_base, bipolar_emitter)

        bjt_extractor = kdb.DeviceExtractorBJT3Transistor(bipolar.class_name)
        l2n.extract_devices(
            bjt_extractor,
            {"B": bipolar_base, "E": bipolar_emitter, "C": bipolar_collector},
        )
        bipolar_regions.append(
            (bipolar, bipolar_base, bipolar_emitter, bipolar_collector)
        )

    # MiM capacitor device recognition (issue #225): each of the deck's
    # optional `capacitors` entries (see `CapacitorDevice` in
    # `decks/__init__.py`) derives its own top-plate/bottom-plate geometry --
    # narrowed by device-specific `requires`/`excludes` layers, and (for a
    # PDK whose bottom plate is an ordinary routing metal rather than a
    # purpose-drawn cap layer) the PDK's own "virtual bottom plate" oversize
    # derivation -- then hands the two plate regions straight to KLayout's
    # native `DeviceExtractorCapacitor`. Unlike the bipolar block above,
    # neither plate layer is one of this deck's own MOS-recognition layers,
    # so there is nothing to intersect against other than the device's own
    # declared layers.
    for capacitor in deck.capacitors:
        # Deck-authoring validation (issue #314): checked unconditionally,
        # like `_resolve_resistors`'s own `_conductor` helper, so a mistake
        # in a deck module is caught even on a cap-free layout rather than
        # only surfacing once someone draws a MiM cap.
        if (capacitor.top_plate_via is None) != (capacitor.top_plate_via_metal is None):
            raise ExtractError(
                f"capacitor '{capacitor.name}': top_plate_via and "
                "top_plate_via_metal must both be set or both be left unset"
            )
        if (
            capacitor.top_plate_via_metal is not None
            and capacitor.top_plate_via_metal not in deck.metals
        ):
            layer, datatype = capacitor.top_plate_via_metal
            raise ExtractError(
                f"capacitor '{capacitor.name}': top_plate_via_metal "
                f"{layer}/{datatype} is not one of the deck's metals[] layers"
            )

        # Plate geometry derivation shared with the top-plate-via/bottom-
        # plate overlap exclusion above (#364) -- see
        # `_capacitor_plate_regions`'s docstring for why the two must never
        # drift apart on what counts as "this capacitor's bottom plate".
        top_region, bottom_region = _capacitor_plate_regions(
            layout, top_cell, capacitor
        )

        if top_region.is_empty() or bottom_region.is_empty():
            # No PDK cap marker drawn anywhere on this layout -- the common
            # case. Registering/extracting an empty device would be a no-op
            # anyway, but skipping it entirely keeps a cap-free layout's
            # extraction bit-for-bit what it was before this feature existed.
            continue

        # Each plate is its own new, self-connected node: `connect()` merges
        # polygons of the *same* plate that touch (e.g. a shared bottom
        # plate across several caps).
        l2n.register(bottom_region, f"{capacitor.name}_bottom")
        l2n.register(top_region, f"{capacitor.name}_top")
        l2n.connect(bottom_region)
        l2n.connect(top_region)

        # Bottom-plate connectivity (issue #314): when the declared
        # `bottom_plate` conductor is one of this deck's own tracked
        # `metals[]` layers, tie the recognised (possibly virtual-plate-
        # clipped) bottom region into that metal's connectivity node, so
        # ordinary contact/via/metal routing to that metal reaches this
        # terminal instead of leaving it an isolated, anonymous net -- see
        # `CapacitorDevice`'s docstring.
        if capacitor.bottom_plate in deck.metals:
            bottom_metal_index = deck.metals.index(capacitor.bottom_plate)
            l2n.connect(bottom_region, metals[bottom_metal_index])

        # Top-plate connectivity (issue #314): when the deck declares the
        # via layer that lands on the top plate and the metal it lands on
        # (`top_plate_via`/`top_plate_via_metal`), wire the top plate
        # through that via into the corresponding `metals[]` node -- the
        # top-plate analogue of the bottom-plate wiring above. Left unwired
        # (isolated node, documented) when the deck declares neither field.
        if capacitor.top_plate_via is not None:
            top_via_region = _region(layout, top_cell, capacitor.top_plate_via)
            l2n.register(top_via_region, f"{capacitor.name}_top_via")
            l2n.connect(top_via_region)
            l2n.connect(top_region, top_via_region)
            top_via_metal_index = deck.metals.index(capacitor.top_plate_via_metal)
            l2n.connect(top_via_region, metals[top_via_metal_index])

        l2n.extract_devices(
            kdb.DeviceExtractorCapacitor(capacitor.name, capacitor.area_cap_f_um2),
            {"P1": bottom_region, "P2": top_region},
        )

    # Drawn resistors: `R` is the recognised resistive segment, `C` the
    # terminal (contacted head) region -- the same conductor layer the
    # segment was cut out of, already part of the connectivity graph below,
    # so the heads pick up their nets from ordinary contact/metal routing.
    # The body itself is deliberately never `connect`ed to anything: that is
    # precisely what stops it being a short.
    for index, (spec, body, terminal) in enumerate(resistors):
        l2n.register(body, f"res{index}_body")
        if spec.bulk_to_substrate:
            l2n.extract_devices(
                kdb.DeviceExtractorResistorWithBulk(spec.name, spec.sheet_rho_ohm_sq),
                {"R": body, "C": terminal, "W": nfet_body},
            )
        else:
            l2n.extract_devices(
                kdb.DeviceExtractorResistor(spec.name, spec.sheet_rho_ohm_sq),
                {"R": body, "C": terminal},
            )

    warnings = [
        str(entry.message) for entry in l2n.each_log_entry()
    ] + unmodelled_device_warnings

    # Connectivity. Deliberately does *not* connect `nwell`/`tap` to
    # `contact` as a blanket rule -- see `ExtractionDeck`'s docstring: the
    # well is a background region spanning the whole PMOS area, so a
    # blanket well<->contact connect would short every terminal inside the
    # well together. Only a genuinely distinct `tap` region (present only
    # when the deck declares one) is safe to tie the well to directly.
    l2n.connect(nfet_sd)
    l2n.connect(pfet_sd)
    l2n.connect(nfet_gate)
    l2n.connect(pfet_gate)
    l2n.connect(poly)
    l2n.connect(nfet_gate, poly)
    l2n.connect(pfet_gate, poly)
    l2n.connect(nwell)
    if deck.tap is not None:
        l2n.connect(tap)
        l2n.connect(nwell, tap)
        l2n.connect(tap, contact)
    l2n.connect(nwell, well_label)
    # Name a poly/gate node directly off a text on the poly-label layer -- the
    # only way a bare-poly gate (no contact/metal landing pad) can carry a
    # `klt gen-compose` `pins[]` label into extraction as a named pin (#210).
    # No-op when the deck declares no `poly_label` (empty Texts) or no text is
    # drawn on it.
    l2n.connect(poly, poly_label)
    l2n.connect(contact)
    l2n.connect(nfet_sd, contact)
    l2n.connect(pfet_sd, contact)
    l2n.connect(poly, contact)

    if metals:
        l2n.connect(contact, metals[0])
        l2n.connect(metals[0])
        if metal_labels and metal_labels[0] is not None:
            l2n.connect(metals[0], metal_labels[0])
        for index in range(len(vias)):
            l2n.connect(metals[index], vias[index])
            l2n.connect(vias[index])
            l2n.connect(vias[index], metals[index + 1])
            l2n.connect(metals[index + 1])
            if index + 1 < len(metal_labels) and metal_labels[index + 1] is not None:
                l2n.connect(metals[index + 1], metal_labels[index + 1])

    l2n.connect_global(nfet_body, deck.substrate_net)

    for bipolar, bipolar_base, bipolar_emitter, bipolar_collector in bipolar_regions:
        # Base shares net identity with the deck's own `nwell` (`bipolar_base`
        # is always a geometric subset of it, further restricted by the
        # marker), so a base contact/pin already wired to `nwell` elsewhere in
        # this function (`well_label`, `tap`, ...) correctly names the
        # extracted base terminal's net.
        l2n.connect(bipolar_base, nwell)
        l2n.connect(bipolar_emitter, contact)
        if bipolar.collector is not None:
            l2n.connect(bipolar_collector, contact)
        else:
            l2n.connect_global(bipolar_collector, deck.substrate_net)

    # Backstop for any *other* engine-level failure the checks above do not
    # anticipate: `docs/cli/extract.md` promises a clean stderr message and
    # exit 1 ("No Python traceback is printed"), so a raw KLayout
    # `RuntimeError` escaping here would break the documented CLI contract
    # (issue #432).
    try:
        l2n.extract_netlist()
    except RuntimeError as exc:
        raise ExtractError(f"netlist extraction failed: {exc}") from exc
    netlist = l2n.netlist()

    # Flat extraction (`begin_shapes_rec`) means `make_top_level_pins()` would
    # promote *every* named net to a top-level pin -- including nets that are
    # only named because a label sits inside an instanced sub-cell, which are
    # ordinary internal nodes once instanced (issue #291). Identify those
    # below-top labels *before* promotion: strings present in the recursive
    # flatten of a label layer but never drawn directly in the top cell.
    label_layers = [deck.well_label, deck.poly_label, *deck.metal_labels]
    top_label_strings = _label_layer_strings(
        layout, top_cell, label_layers, recursive=False
    )
    all_label_strings = _label_layer_strings(
        layout, top_cell, label_layers, recursive=True
    )
    below_top_labels = all_label_strings - top_label_strings

    netlist.make_top_level_pins()
    demoted = _reconcile_top_pins(
        netlist, top_cell.name, below_top_labels, demote=top_cell_pins_only
    )
    if demoted:
        joined = ", ".join(demoted)
        if top_cell_pins_only:
            warnings.append(
                f"kept {len(demoted)} label-named net(s) internal: their naming "
                f"label(s) are drawn below the top cell (inside instanced "
                f"sub-cells), not in the top cell itself ({joined})"
            )
        else:
            warnings.append(
                f"promoted {len(demoted)} net(s) to top-level pins from label(s) "
                f"found only below the top cell (inside instanced sub-cells): "
                f"{joined} -- these are internal nodes once instanced; pass "
                f"top_cell_pins_only (--top-cell-pins) to keep them internal "
                f"(issue #291)"
            )

    netlist.purge()

    # Parasitics geometry must be read *before* `l2n` (which owns the shape
    # database `polygons_of_net` reads) is garbage-collected below, so compute
    # it here while the graph is still live. Returned as plain data; the R/C
    # devices are injected into the netlist by `run_extract` after it has
    # already captured the schematic-equivalent `devices[]`/`nets[]` view.
    parasitic_nets: list[dict[str, Any]] | None = None
    if parasitics_deck is not None:
        circuit = netlist.circuit_by_name(top_cell.name)
        parasitic_nets = _compute_parasitics(
            l2n,
            circuit,
            layout.dbu,
            deck,
            parasitics_deck,
            layer_index,
            metal_index,
        )

    # `l2n` (and the Region/Texts objects it owns) would otherwise be
    # garbage-collected once this function returns, which invalidates the
    # netlist it produced (KLayout raises on subsequent use) -- `dup()`
    # detaches an independently-owned copy.
    return (
        netlist.dup(),
        warnings,
        parasitic_nets,
        black_box_regions,
        dummy_devices_dropped,
        unmodelled_poly,
    )


def _n_squares(area_um2: float, perimeter_um: float) -> float:
    """Estimate the number of resistive *squares* of a net's copper on one
    layer from its total area and perimeter.

    First-order geometric approximation: model the layer's shapes as one
    equivalent rectangle with the same area ``A`` and perimeter ``P``, whose
    side lengths ``L`` and ``W`` are the roots of ``t^2 - (P/2) t + A = 0``;
    the square count is then ``L / W`` (``>= 1``). This is exact for a single
    rectangular wire and reduces to ``1`` for a square. When the shapes are
    "rounder" than any rectangle allows (negative discriminant -- e.g. a
    single square-ish pad, or fragmented geometry), it clamps to ``1``.

    Deliberately simple and fixed (issue #216: "a single, fixed, first-order
    lumped model -- no fast/accurate mode selector"); it over-counts squares
    for L-shaped or multi-fragment nets, which biases the resulting series
    resistance conservatively high rather than low.
    """
    if area_um2 <= 0.0 or perimeter_um <= 0.0:
        return 0.0
    dims = equivalent_rectangle_um(area_um2, perimeter_um)
    if dims is None:
        # "Rounder" than any rectangle allows (negative discriminant -- e.g. a
        # single square-ish pad, or fragmented geometry): clamp to one square.
        return 1.0
    length, width = dims
    return max(1.0, length / width)


def _net_area_perim_um(
    l2n: kdb.LayoutToNetlist,
    net: kdb.Net,
    dbu: float,
    indices: list[int],
    subtract_indices: list[int] | None = None,
) -> tuple[float, float]:
    """Total ``(area_um2, perimeter_um)`` of ``net``'s shapes across the
    given registered layer ``indices`` (each an index returned by
    ``LayoutToNetlist.register``), with any ``subtract_indices`` layers
    geometrically removed first.

    ``subtract_indices`` lets the poly role exclude the transistor gate
    regions from a net's poly shapes before measuring (issue #226): the gate
    sits over the channel, not the substrate the coefficients describe, and
    its capacitance is already captured by the device model. The subtraction
    is a purely local operation on the per-net ``Region`` returned by
    ``polygons_of_net`` -- it registers no extra ``LayoutToNetlist`` layer, so
    the connectivity graph is untouched."""
    import klayout.db as kdb

    region = kdb.Region()
    for index in indices:
        region += l2n.polygons_of_net(net, index)
    for index in subtract_indices or ():
        region -= l2n.polygons_of_net(net, index)
    area_um2 = region.area() * dbu * dbu
    perim_um = region.perimeter() * dbu
    return area_um2, perim_um


def _compute_parasitics(
    l2n: kdb.LayoutToNetlist,
    circuit: kdb.Circuit | None,
    dbu: float,
    deck: ExtractionDeck,
    parasitics_deck: ParasiticsDeck,
    layer_index: dict[str, int],
    metal_index: list[int],
) -> list[dict[str, Any]]:
    """Compute one first-order lumped ``(R, C)`` per net from the extracted
    per-net/per-layer geometry.

    For each net (except the deck's substrate/ground net, which is the AC
    ground the capacitances return to):

    - **C to ground** = sum over conductor roles of ``area_um2 * cap_area +
      perimeter_um * cap_perim`` (femtofarads), the lumped ground capacitance
      of the net's interconnect.
    - **series R** = sum over conductor roles of ``sheet_res * n_squares``
      (ohms), the net's lumped interconnect resistance.

    Roles map to the registered geometry layers: ``poly`` the poly region with
    the transistor gate regions subtracted out (issue #226 -- gate capacitance
    lives in the device model), and ``metals[i]`` each metal-stack layer
    (index-aligned with the deck's ``metals``). The optional ``diffusion`` role
    (NMOS+PMOS source/drain) is left unset by the shipped decks: the M cards'
    ``AS``/``AD``/``PS``/``PD`` already feed the device model's junction
    capacitance, so a diffusion cap term would double-count it. Net-to-net
    coupling capacitance is explicitly **not** modeled (issue #216: ground
    capacitance only in the first increment).

    Returns a list (sorted by net name for deterministic output) of
    ``{"net", "resistance_ohm", "capacitance_ff"}`` for every net with a
    non-zero ground capacitance; nets with no eligible interconnect geometry
    are omitted.
    """
    if circuit is None:
        return []

    # (LayerRC, [include indices], [subtract indices]) for every role that has
    # both a coefficient set and at least one present layer. The subtract list
    # is empty except for the poly role, which removes the transistor gate
    # regions (see below).
    roles: list[tuple[Any, list[int], list[int]]] = []
    if parasitics_deck.diffusion is not None:
        roles.append(
            (
                parasitics_deck.diffusion,
                [layer_index["nfet_sd"], layer_index["pfet_sd"]],
                [],
            )
        )
    if parasitics_deck.poly is not None:
        # Exclude the transistor gate regions from the poly role (issue #226):
        # gate poly sits over the channel (not the substrate these coefficients
        # describe) and its capacitance is already in the device model, so the
        # nfet/pfet gate shapes are subtracted from the net's poly shapes before
        # measuring. These registrations back the device connectivity too and
        # are left untouched -- only the parasitic measurement subtracts them.
        roles.append(
            (
                parasitics_deck.poly,
                [layer_index["poly"]],
                [layer_index["nfet_gate"], layer_index["pfet_gate"]],
            )
        )
    for i, layer_rc in enumerate(parasitics_deck.metals):
        if layer_rc is not None and i < len(metal_index):
            roles.append((layer_rc, [metal_index[i]], []))

    results: list[dict[str, Any]] = []
    for net in circuit.each_net():
        name = net.expanded_name()
        if name == deck.substrate_net:
            continue
        r_ohm = 0.0
        c_ff = 0.0
        for layer_rc, indices, subtract in roles:
            area_um2, perim_um = _net_area_perim_um(l2n, net, dbu, indices, subtract)
            if area_um2 <= 0.0:
                continue
            c_ff += (
                area_um2 * layer_rc.cap_area_ff_um2
                + perim_um * layer_rc.cap_perim_ff_um
            )
            r_ohm += layer_rc.sheet_res_ohm_sq * _n_squares(area_um2, perim_um)
        if c_ff <= 0.0:
            # No ground capacitance means no load to hang a series R off of --
            # a bare series R to nothing is meaningless, so skip the net.
            continue
        results.append(
            {
                "net": name,
                "resistance_ohm": round(r_ohm, 4),
                "capacitance_ff": round(c_ff, 6),
            }
        )

    results.sort(key=lambda entry: entry["net"])
    return results


def _inject_parasitics(
    kdb: Any,
    circuit: kdb.Circuit,
    parasitic_nets: list[dict[str, Any]],
    ground_net_name: str,
) -> dict[str, Any]:
    """Inject one lumped-RC ``Gamma``-section per net into ``circuit`` and
    return the JSON ``parasitics`` summary block.

    For each entry, a series resistor connects the net to a fresh internal
    parasitic node, and a capacitor connects that node to the deck's ground
    net (created if absent): ``net --R--> net.par --C--> ground``. This is
    purely additive -- no existing device instance, net name, or pin is
    touched -- so the schematic-equivalent connectivity `devices[]`/`nets[]`
    reported (built before this call) is unchanged, and the written SPICE
    stays a `.SUBCKT` body directly consumable by ``klt sim`` (the parasitic
    nodes are internal; the subcircuit's pin interface is untouched).

    The resistor and capacitor device classes are added unnamed so KLayout's
    ``NetlistSpiceWriter`` emits bare ``R``/``C`` cards with no trailing model
    token (simulator-safe).
    """
    res_class = kdb.DeviceClassResistor()
    cap_class = kdb.DeviceClassCapacitor()
    netlist = circuit.netlist()
    netlist.add(res_class)
    netlist.add(cap_class)

    ground = circuit.net_by_name(ground_net_name)
    if ground is None:
        ground = circuit.create_net(ground_net_name)

    # Keyed by `expanded_name()` rather than looked up via `net_by_name()`
    # per entry: `net_by_name()` only resolves *named* nets (an explicit
    # layout label), so it silently returns `None` -- and drops the entry --
    # for every genuinely internal/unlabelled net, whose `expanded_name()` is
    # KLayout's auto-generated `$<n>` form rather than a real `.name`. Issue
    # #283: `_compute_parasitics` already measures these nets correctly (the
    # geometry is there), so this lookup must resolve them too or the R/C it
    # computed for them is discarded right here.
    nets_by_name = {net.expanded_name(): net for net in circuit.each_net()}
    existing_names = set(nets_by_name)

    report_nets: list[dict[str, Any]] = []
    total_r = 0.0
    total_c_ff = 0.0
    for entry in parasitic_nets:
        net = nets_by_name.get(entry["net"])
        if net is None:
            continue
        internal_name = _unique_net_name(entry["net"], existing_names)
        existing_names.add(internal_name)
        internal = circuit.create_net(internal_name)

        r_ohm = max(entry["resistance_ohm"], _MIN_PARASITIC_R_OHM)
        c_farad = entry["capacitance_ff"] * 1e-15

        instance_name = _sanitize_instance_name(entry["net"])
        r_dev = circuit.create_device(res_class, instance_name)
        r_dev.connect_terminal("A", net)
        r_dev.connect_terminal("B", internal)
        r_dev.set_parameter("R", r_ohm)

        c_dev = circuit.create_device(cap_class, instance_name)
        c_dev.connect_terminal("A", internal)
        c_dev.connect_terminal("B", ground)
        c_dev.set_parameter("C", c_farad)

        total_r += r_ohm
        total_c_ff += entry["capacitance_ff"]
        report_nets.append(
            {
                "net": entry["net"],
                "resistance_ohm": entry["resistance_ohm"],
                "capacitance_ff": entry["capacitance_ff"],
                "internal_node": internal_name,
            }
        )

    return {
        "r_count": len(report_nets),
        "c_count": len(report_nets),
        "total_resistance_ohm": round(total_r, 4),
        "total_capacitance_ff": round(total_c_ff, 6),
        "nets": report_nets,
    }


_INSTANCE_NAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_]")


def _sanitize_instance_name(name: str) -> str:
    """Map every character outside ``[A-Za-z0-9_]`` in a parasitic *device
    instance* name to ``_`` (issue #312).

    Device instance names are cosmetic handles -- nothing downstream matches
    an ``R``/``C`` card's instance name against the raw net name it was
    derived from (the ``devices[]``/``nets[]`` JSON and the netlist's ``*
    device instance ...`` comment carry the real identity; *node* names are
    escaped separately, by KLayout's own ``NetlistSpiceWriter``). Left
    unsanitized, a net's ``expanded_name()`` can carry characters a SPICE
    reader treats as syntax rather than an opaque token: ``$`` (KLayout's
    anonymous/unlabelled-net placeholder, e.g. ``$12``) and ``,`` (KLayout's
    join character when multiple text labels land on one net, e.g.
    ``Y,Y2``). ngspice does not reject either -- it silently splits the
    comma-joined form into extra tokens, corrupting the card's arity and
    surfacing a confusing error against an unrelated node.
    """
    return _INSTANCE_NAME_UNSAFE_RE.sub("_", name)


def _unique_net_name(base: str, existing: set[str]) -> str:
    """A SPICE-safe internal parasitic-node name derived from ``base`` that
    does not collide with any already-present net name (an underscore suffix,
    not a dot, so ngspice never mistakes it for a hierarchy separator)."""
    candidate = f"{base}__par"
    if candidate not in existing:
        return candidate
    counter = 2
    while f"{base}__par{counter}" in existing:
        counter += 1
    return f"{base}__par{counter}"


def _describe_devices(
    circuit: kdb.Circuit,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Build the response's ``devices[]`` array and ``device_counts`` map."""
    devices: list[dict[str, Any]] = []
    device_counts: dict[str, int] = {}

    for device in circuit.each_device():
        device_class = device.device_class()
        class_name = device_class.name

        nets: dict[str, str | None] = {}
        for terminal in device_class.terminal_definitions():
            net = device.net_for_terminal(terminal.id())
            nets[terminal.name.lower()] = (
                net.expanded_name() if net is not None else None
            )

        params: dict[str, float] = {}
        for param in device_class.parameter_definitions():
            if param.name == "W":
                params["w_um"] = round(
                    device.parameter(param.id()), _PARAM_PRECISION_UM
                )
            elif param.name == "L":
                params["l_um"] = round(
                    device.parameter(param.id()), _PARAM_PRECISION_UM
                )
            elif param.name == "C":
                # Drawn-capacitor device classes only (#225): KLayout's
                # `DeviceClassCapacitor` reports capacitance in farads. MOS/
                # bipolar classes have no `C` parameter, so this branch never
                # fires for them.
                params["c_f"] = round(
                    device.parameter(param.id()), _PARAM_PRECISION_FARAD
                )
            elif param.name == "A":
                # Capacitor plate-overlap area in square micrometres -- the
                # geometry `c_f` above was computed from (`C = A * area_cap`,
                # see `CapacitorDevice`'s docstring), reported alongside it so
                # a consumer can sanity-check the extracted value without
                # re-deriving it from the layout. `DeviceClassCapacitor`'s own
                # area/perimeter parameters are named `A`/`P` -- distinct from
                # `DeviceClassBJT3Transistor`'s `AE`/`AB`/`AC`/`PE`/`PB`/`PC`
                # (see "Bipolar (BJT) device recognition"), so this branch
                # never fires for a bipolar device.
                params["area_um2"] = round(
                    device.parameter(param.id()), _PARAM_PRECISION_UM
                )
            elif param.name == "R":
                # Drawn-resistor device classes only (#222): KLayout's
                # `R = L / W * sheet_rho`, in ohms. MOS classes have no `R`
                # parameter, so this branch never fires for them.
                params["r_ohm"] = round(
                    device.parameter(param.id()), _PARAM_PRECISION_OHM
                )

        devices.append(
            {
                "name": device.expanded_name(),
                "class": class_name,
                "nets": nets,
                "params": params,
            }
        )
        device_counts[class_name] = device_counts.get(class_name, 0) + 1

    devices.sort(key=lambda entry: entry["name"])
    return devices, device_counts


def _describe_nets(circuit: kdb.Circuit) -> list[dict[str, Any]]:
    """Build the response's ``nets[]`` array."""
    pin_nets = {pin.expanded_name() for pin in _each_pin_net(circuit)}

    nets: list[dict[str, Any]] = []
    for net in circuit.each_net():
        name = net.expanded_name()
        nets.append(
            {
                "name": name,
                "pin": name in pin_nets,
                "device_count": net.terminal_count(),
            }
        )

    nets.sort(key=lambda entry: entry["name"])
    return nets


def _describe_ignored_layers(path: str, deck: ExtractionDeck) -> list[dict[str, Any]]:
    """Build the response's ``ignored_layers[]`` array (issue #220).

    Enumerates the input stream's layers (reusing ``layers.py``'s existing
    per-layer walk, the same one ``klt drc``'s coverage report leans on) and
    returns the shape-bearing ``(layer, datatype)`` pairs that are *not* in
    ``deck.connectivity_layers`` -- geometry the extraction connectivity graph
    never reads. Each entry carries its stream ``shapes`` count so a consumer
    can judge whether the amount is material (a stray annotation vs. a whole
    block routed on an undeclared metal level). Empty-layer entries (``shapes
    == 0``) are skipped; the list is sorted by ``(layer, datatype)``.
    """
    from .layers import layers_report

    read_layers = deck.connectivity_layers
    ignored: list[dict[str, Any]] = []
    for entry in layers_report(path)["layers"]:
        if entry["shapes"] <= 0:
            continue
        if (entry["layer"], entry["datatype"]) in read_layers:
            continue
        ignored.append(
            {
                "layer": entry["layer"],
                "datatype": entry["datatype"],
                "shapes": entry["shapes"],
            }
        )
    ignored.sort(key=lambda e: (e["layer"], e["datatype"]))
    return ignored


def _each_pin_net(circuit: kdb.Circuit) -> list[kdb.Net]:
    """The distinct nets exposed as circuit pins."""
    result = []
    for pin in circuit.each_pin():
        net = circuit.net_for_pin(pin.id())
        if net is not None:
            result.append(net)
    return result
