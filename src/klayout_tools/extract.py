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

A deck may likewise declare one or more junction-diode device-recognition
entries (``ExtractionDeck.diodes``, issue #542): a p-doped and an n-doped
drawn layer, each restricted to a PDK-specific diode device-mark layer (e.g.
gf180mcu's ``diode_mk`` 115/5) and narrowed by per-terminal
``requires``/``excludes`` implant layers, fed to KLayout's native
``DeviceExtractorDiode`` -- which forms the device from the two regions'
geometric overlap and reports that overlap's area/perimeter. A terminal the
PDK draws no mask for (the p-substrate side of an n+/p-substrate diode)
is declared ``None`` and tied to the deck's ``substrate_net`` global,
mirroring the collector-less bipolar case. Without such an entry a discrete
diode -- the standard ESD-clamp primitive on every pad ring -- extracts as
no device at all, so ``klt lvs`` cannot verify any diode-based clamp. See
:class:`klayout_tools.decks.DiodeDevice` for the layer-role contract.

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

import fnmatch
import math
import os
import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from ._annotation import is_reserved_annotation_layer
from ._layout import region as _region
from ._layout import texts as _texts
from ._provenance import build_provenance, sha256_file
from .decks import (
    BipolarDevice,
    CapacitorDevice,
    DiodeDevice,
    ExtractionDeck,
    InvalidDeckOptionError,
    ParasiticsDeck,
    ResistorDevice,
    UnknownExtractionDeckError,
    deck_source_path,
    get_extraction_deck,
    get_parasitics_deck,
)
from .lef_header import read_lef_macro_pin_ports
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
#:
#: 2 (issue #592): the `parasitics.nets[]` entry's shape changed from one
#: shunt resistor (`internal_node`) to a star topology -- `internal_node` is
#: replaced by `hub_net` (usually the net itself now, not a fresh node) and a
#: new `terminals[]` array (one entry per device terminal moved onto the
#: star). `parasitics.r_count` also changed meaning: it now counts every
#: emitted resistor (one or more per net), not one per net, so
#: `r_count == c_count` no longer holds in general.
SCHEMA_VERSION = 2

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
    declared_pins: frozenset[str] | None = None,
    apply_resistor_fixed_offset: bool = True,
    deck_options: Mapping[str, str] | None = None,
    abstract_cell_patterns: tuple[str, ...] = (),
    abstract_cell_lef_paths: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Extract a schematic-equivalent netlist from the layout at ``path``.

    ``deck_name`` selects the curated :class:`~klayout_tools.decks.ExtractionDeck`
    (currently ``"sky130"``/``"gf180mcu"``). ``output`` overrides the written
    SPICE path (default: ``path`` with its extension replaced by
    ``.spice``, next to the input -- the "next to the input" convention
    ``klt render``/``klt sim`` already use). ``top`` selects the top cell
    when the stream has more than one (required in that case; otherwise
    optional and must name the sole top cell if given).

    ``deck_options`` (``klt extract --deck-option <key>=<value>``, repeatable;
    issue #595) selects which caller-visible **sheet-rho flavour** of a
    resistor family whose members share *identical* recognition geometry is
    wired for this run -- e.g. gf180mcu's ``Resistor``-marked poly family,
    whose real PDK LVS deck selects one of ``1k``/``2k``/``3k`` via a
    build-time ``POLY_RES`` variable rather than any drawn layer. ``None`` or
    an empty mapping (the default) resolves the deck exactly as before this
    parameter existed. See
    :func:`~klayout_tools.decks.get_extraction_deck`'s and
    :class:`~klayout_tools.decks.ResistorDevice`'s own docstrings for the
    full mechanism, and ``docs/cli/extract.md``'s "Selecting a shared-geometry
    resistor flavour" section for the CLI contract. A key/value this deck's
    declared resistors do not recognise is an :class:`ExtractError` (wrapping
    :class:`~klayout_tools.decks.InvalidDeckOptionError`), not a silent no-op
    or a silently-kept default. The resolved mapping is echoed verbatim in
    the response's ``provenance.deck.options`` so a record can pin exactly
    which flavour a run selected.

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

    ``declared_pins`` (the ``--pins`` flag, issue #514) is a per-*net*
    declaration of the intended interface, orthogonal to
    ``top_cell_pins_only``'s per-*cell* one: when given (a non-``None`` set
    of net names), every promoted pin whose net name is *not* in the set is
    demoted back to an internal net -- it keeps its name, it is simply not
    exposed as a top-level pin. This is the fix for labelling an internal
    node of a lumped schematic device (e.g. one tap of a metal-option
    ladder modelled as a single series device) purely for documentation --
    today that label always promotes the node to a pin, which blocks ``klt
    lvs``'s ``options.combine_devices`` from folding the series chain the
    reference netlist models as one device. Reuses
    :func:`_reconcile_top_pins` exactly as ``top_cell_pins_only`` does, with
    a different demote-set: every currently-promoted pin name minus
    ``declared_pins``. A ``warnings`` entry lists any net demoted this way,
    and a separate entry lists any declared name that matched no promoted
    net (a likely typo, not silently ignored). ``None`` (the default) skips
    this reconciliation entirely -- byte-identical to today's behavior, same
    invariant ``top_cell_pins_only``'s own default preserves. Applied after
    ``top_cell_pins_only``'s own reconciliation, and only ever *further*
    demotes -- it cannot re-promote a net ``top_cell_pins_only`` already
    kept internal.

    ``apply_resistor_fixed_offset`` (issue #559/#585, exposed on the CLI as
    ``klt extract --defer-resistor-fixed-offset`` by issue #588): when
    ``True`` (the default, the behavior every existing caller and every
    ``klt extract`` invocation without that flag gets), each opted-in
    resistor device class's
    :attr:`~klayout_tools.decks.ResistorDevice.fixed_offset_ohm` head/end
    term is added to ``R`` once per drawn primitive at extraction time --
    baked into both the written SPICE and the JSON ``devices[].params``.
    Passing ``False`` **defers** that correction: the returned netlist (and
    the written SPICE) carry only the raw per-primitive body ``R``. This is
    for a caller who intends to read the netlist back through ``klt lvs``'s
    ``layout.netlist`` + ``layout.deck`` + ``options.combine_devices: true``
    shape, where the correction must be applied exactly *once per
    post-combine logical device* rather than once per primitive -- applying
    it here first would double-count it after the series fold (issue #585).
    Mirrors how ``lvs.py``'s inline-extraction path already defers the
    correction internally via ``extract_netlist_from_layout``.

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
            "device_recognition_only_layers": [
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
            "abstracted_cells": [
                {
                    "cell": str, "instance_count": int, "pin_count": int,
                    "resolution_source": "in_cell_labels" | "lef_abstract",
                    "lef_path": str | None,
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
            "voltage_domain_warnings": [
                {"marker": "<layer>/<datatype>", "description": str}, ...
            ],
            "merged_net_labels": [
                {"net": str, "labels": [str, ...]}, ...
            ],
            "unbiased_pmos_body_nets": [
                {"device": str, "net": str}, ...
            ],
            "single_terminal_nets": [
                {
                    "net": str, "device": str, "terminal": str,
                    "terminal_kind": str,
                },
                ...
            ],
            "dead_metal": [
                {
                    "role": str, "layer": int, "datatype": int,
                    "bbox_um": {
                        "left": float, "bottom": float,
                        "right": float, "top": float,
                    },
                    "shapes": int, "area_um2": float,
                },
                ...
            ],
            "pdk": {"variant": str, "root": str, "version": str | None} | None,
            "parasitics": {...} | None,
            "provenance": {  # shared reproducibility block, see _provenance.py
                "klt_version": <str | None>,
                "klayout_version": <str | None>,
                "pdk": {"name", "source", "version"} | None,
                # "options" is present only when `deck_options` was given
                # (issue #595) -- omitted entirely otherwise.
                "deck": {
                    "name": <deck name>,
                    "content_hash": "sha256:...",
                    "options": {<deck option key>: <value>, ...},
                },
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
    Every entry already carries a material (``shapes > 0``) count -- empty
    layers are dropped before they reach this field -- so a non-empty
    ``ignored_layers`` also appends a single aggregate prose entry to
    ``warnings[]`` (issue #666), naming the affected layers and their total
    shape count, so a caller checking only ``warnings[]`` still sees it.

    ``device_recognition_only_layers`` (issue #619) lists ``(layer,
    datatype)`` pairs that carry shapes in the input stream *and are read* by
    this deck (so they never appear in ``ignored_layers`` above) but only for
    a ``bipolars``/``capacitors``/``resistors``/``diodes`` device-recognition
    role -- never as a ``metals``/``vias`` connectivity level, and never one
    of the deck's own MOS-core layers either
    (:attr:`ExtractionDeck.device_recognition_only_layers`). Two nets joined
    only through such a layer will not merge, and this gap is invisible to
    ``ignored_layers`` because the layer genuinely is read, just not for
    net-merging purposes -- exactly how sky130's own met3/met4 (its MiM-cap
    bottom plates) hid a routing-connectivity ceiling behind a clean-looking
    ``ignored_layers`` report before its ``metals`` stack grew to cover them
    too. This is diagnostic context, not a warning: unlike ``ignored_layers``,
    a non-empty list does *not* append to ``warnings[]`` -- a deck's own
    marker/mask geometry (a resistor's marker layer, a bipolar's ID mark, a
    MiM cap's top-plate mark) is expected to be device-recognition-only by
    PDK design, not a coverage gap, so flagging every occurrence would make
    ``warnings[]`` fire on nearly every layout that uses one of these device
    classes. Empty when every device-recognition layer is also a
    ``metals``/``vias`` connectivity level or one of the deck's own MOS-core
    layers (or the deck declares no ``bipolars``/``capacitors``/
    ``resistors``/``diodes`` entries at all).

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

    ``abstract_cell_patterns``/``abstract_cell_lef_paths`` (``klt extract
    --abstract-cells '<glob>'``/``--abstract-cell-lef <path>``, both
    repeatable; issue #620) select a **cell-level black-box** abstraction
    mode, additive to (and independent of) ``black_box_regions`` above: every
    instantiated cell whose name matches one of the ``fnmatch`` glob patterns
    in ``abstract_cell_patterns`` is extracted as an opaque, pinned
    subcircuit instead of being flattened down to its own devices --
    everything *not* matched by a pattern is extracted exactly as it is
    today. A matched cell's pins are resolved, per distinct cell *type*
    (cached across every occurrence): first from that cell's own
    ``metal_labels``/``well_label``/``poly_label`` text, drawn directly in
    its own definition (never promoted from a nested sub-cell); when a
    matched type draws no such label, from a ``MACRO``/``PIN``/``PORT``
    block of the same name in one of the ``abstract_cell_lef_paths`` LEF
    files/directories, in the order given. A matched cell type resolved by
    neither source is an :class:`ExtractError` naming it (a caller must
    either supply a pin source or narrow the pattern) -- see
    :func:`_wire_abstract_cells`. ``abstract_cell_patterns`` empty (the
    default) skips this mode entirely; the written SPICE and every other
    field are then byte-identical to before this feature existed.
    ``abstract_cell_lef_paths`` is only ever consulted as the fallback pin
    source and has no effect when every matched cell type resolves its pins
    from in-cell labels.

    ``abstracted_cells`` reports this mode's own result: one entry per
    *distinct* matched cell type (sorted by cell name), each
    ``{"cell": <cell type name>, "instance_count": <int>, "pin_count":
    <int>, "resolution_source": "in_cell_labels" | "lef_abstract",
    "lef_path": <str | None>}`` -- ``lef_path`` names the specific LEF file
    the pins were resolved from, ``None`` for ``"in_cell_labels"``. Always a
    list, empty when ``abstract_cell_patterns`` is empty or matches no
    instantiated cell.

    The written SPICE gains one ``.SUBCKT <cell type> <pins...> ... .ENDS``
    block per distinct matched cell type (empty body -- a black box declares
    no devices) and one ``X<instance>`` card per matched instance in the top
    circuit's own ``.SUBCKT`` block, wired to the same layout-derived net
    names the un-abstracted portion of the circuit already uses -- KLayout's
    native ``kdb.SubCircuit``/``NetlistSpiceWriter`` machinery emits this
    automatically once the netlist model represents the abstraction (every
    circuit, including the flat top-level one, is already written as its own
    ``.SUBCKT`` block today, so this is a purely additive extension of the
    same writer, not a new SPICE-emission code path). See
    ``docs/cli/extract.md``'s "Cell-level (black-box + pins) abstraction"
    section for worked examples.

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

    ``voltage_domain_warnings`` (issue #552) reports every voltage-domain
    marker layer (registered per-deck via
    :func:`~klayout_tools.decks.get_unmodeled_voltage_markers`, e.g.
    gf180mcu's ``Dualgate`` 55/0) whose geometry overlaps extracted MOS
    device geometry -- see :func:`_detect_voltage_domain_overlap`. This
    deck's ``ExtractionDeck`` derives MOS flavour from the well layer alone
    and never reads such a marker, so a transistor drawn inside it still
    extracts bound to the deck's single (default) model name (e.g.
    ``nfet_03v3``/``pfet_03v3``) -- this field is the loud signal that the
    binding may be wrong, not a corrected one: the model name itself is
    unchanged. One entry per flagged marker, each ``{"marker":
    "<layer>/<datatype>", "description": str}`` -- the same registry entry
    ``klt drc``'s ``coverage.voltage_domain_warnings`` surfaces for the same
    deck, so the wording matches across both commands. A matching prose
    entry is also appended to ``warnings``. Always a list, empty for a deck
    that registers no such marker or a layout that draws none of it
    overlapping MOS geometry.

    ``merged_net_labels`` (issue #470) reports every net whose KLayout-
    assigned name is a comma-joined merge of 2+ distinct labels -- see
    :func:`_detect_merged_net_labels` for the exact heuristic and
    ``docs/cli/extract.md``'s "Merged net labels" section for the false-
    positive limitation. One entry per affected net, each ``{"net": "<full
    joined name>", "labels": [str, ...]}`` -- ``labels`` is the joined name
    split on ``,``, so a consumer does not have to re-derive the label list
    from the string itself. A matching prose entry is also appended to
    ``warnings`` for every affected net. Always a list, empty when no net
    carries multiple labels.

    ``unbiased_pmos_body_nets`` (issue #555) reports every extracted PMOS
    (``deck.pfet_class``) device whose body (``"b"``) terminal ties to an
    anonymous, KLayout-synthesized net -- the ``"$5"``-style placeholder
    ``Net.expanded_name()`` assigns to a net with no drawn label, as opposed
    to the deck's own synthesized (but *named*) global substrate net (e.g.
    ``"vsubs"``, tied via ``connect_global``). This happens today on decks
    whose curated layer set has no distinct well-tie/tap layer separate from
    transistor active (gf180mcu, see ``decks/gf180mcu.py``); sky130's
    ``well_label``/``tap`` fields mean it never fires there. Unlike
    ``devices[].nets["b"]`` (which already carries the same net name, just
    not flagged), this field is the structured, no-grep-required signal that
    the reported net has **no DC bias path at all** -- see
    ``docs/cli/extract.md``'s "Parasitic (RC) extraction" section for the
    simulation-fidelity consequence (a floating body voltage rather than the
    real supply rail every schematic-level netlist assumes). One entry per
    affected device, each ``{"device": "<device instance name>", "net":
    "<anonymous net name>"}``; a single aggregate prose entry (count baked
    in, e.g. ``"148 PMOS devices tie their body to an anonymous net..."``)
    is also appended to ``warnings`` when this field is non-empty -- not one
    line per device (issue #599), so ``warnings`` does not scale with the
    device count on a large design. Always a list, empty when no PMOS
    device's body net is anonymous (which includes every deck whose layer
    set draws a real well-tie/tap). Independent of ``--parasitics``/
    ``--pdk`` -- present under the same condition regardless of either flag,
    since the DC-bias gap exists whether or not parasitics are requested or
    a PDK model is bound.

    ``single_terminal_nets`` (issue #596) reports every net whose
    ``nets[]`` entry has ``device_count == 1`` (``Net.terminal_count()``,
    already reported per net -- see the ``nets`` field above) and
    ``pin: False`` -- a net that touches exactly one device terminal and is
    not a declared top-level pin. There is no DC path through such a node
    from anywhere else in the netlist, so a downstream simulator hits a
    singular matrix on it; this is the structurally-detectable signal for
    that failure, several stages upstream of where a transient solver would
    otherwise report it against an anonymous net name. See
    :func:`_detect_single_terminal_nets` for the exact detection and
    ``terminal_kind`` classification. One entry per affected net, each
    ``{"net": "<net name>", "device": "<owning device instance name>",
    "terminal": "<lower-cased terminal key>", "terminal_kind": "gate" |
    "source" | "drain" | "body" | "<literal terminal key>"}`` --
    ``terminal_kind`` is the MOS terminal name for a MOS-like device (a
    device with a ``"g"`` terminal), else the raw terminal key itself (the
    "resistor-equivalent" case, e.g. a drawn resistor/capacitor's ``"a"``/
    ``"b"``/``"w"``). One aggregate prose entry per ``terminal_kind`` bucket
    (count baked in) is also appended to ``warnings`` when that bucket is
    non-empty -- not one line per net (issue #599), so ``warnings`` does not
    scale with the affected net count on a large design. Up to two such
    entries: one for ``terminal_kind == "gate"``, phrased more strongly (an
    undriven MOS input is essentially always a bug), and one for every other
    terminal kind combined (a single-terminal source/drain/body/resistor-
    style tie can be a legitimate deliberately-unterminated dummy). Always a
    list, empty when every net either has zero or 2+ device terminals, or is
    a declared pin.

    ``dead_metal`` (issue #676) reports every connected cluster of
    routing-stack geometry -- the deck's ``metals``/``vias`` levels -- that
    joins **no** extracted net: nothing in ``nets[]`` mentions it, so it is
    invisible to this report, to ``klt lvs``, and to a resimulation of the
    written netlist. One entry per cluster (not per drawn polygon), each
    ``{"role": "metal<i>" | "via<i>", "layer": int, "datatype": int,
    "bbox_um": {"left", "bottom", "right", "top"}, "shapes": int,
    "area_um2": float}``, sorted by ``(layer, datatype, left, bottom)`` --
    ``role``'s ``<i>`` indexes the deck's own ``metals``/``vias`` tuple
    (``0`` = the bottom-most level), and ``shapes`` counts the drawn shapes
    on that stream layer the cluster covers, so a reviewer knows how much
    geometry to go look at. The netted side of the subtraction comes from the
    extracted connectivity graph, so **XY overlap between adjacent metal
    levels is not connection**: a wire passing over another with no via
    between them stays dead. A non-empty list also appends a single aggregate
    prose entry to ``warnings[]`` (count baked in, issue #599's pattern) --
    dead metal is often deliberate (artwork, fill, a bond-pad blank), which is
    exactly why a reviewer should be told it is there rather than left to
    discover it by rendering the raw geometry. A *labelled* floating cluster
    is not dead: :func:`_purge_preserving_named_nets` keeps it as a real,
    named, pinned net, so power straps/seal rings/bond pads that carry a
    label never appear here. See :func:`_detect_dead_metal` and
    ``docs/cli/extract.md``'s "Dead metal" section. Always a list, empty when
    every metal/via shape joins a net.

    ``parasitics.metals_without_coefficient`` (issue #547) lists every metal
    stack level the deck's ``ExtractionDeck.metals`` declares that has no
    matching entry in the deck's ``ParasiticsDeck.metals`` -- the
    extraction-side analogue of ``ignored_layers``, but for the parasitics
    pass's *own* coefficient table rather than the input stream. A metal
    level in this list silently contributes zero resistance and capacitance
    to every net's parasitics; see :func:`_describe_parasitics_metal_gaps`
    for the exact gap definition. Present only inside the ``parasitics``
    block (so only when ``--parasitics`` was given); a matching prose entry
    is also appended to ``warnings`` when the list is non-empty. Always a
    list, empty when every declared metal level has a coefficient.

    Raises :class:`ExtractError` if the file is missing/unreadable, the deck
    name is unknown, ``deck_options`` names an unrecognised key/value, the
    PDK (when given) does not resolve, the top cell is missing/ambiguous, an
    ``abstract_cell_lef_paths`` entry cannot be read, a matched
    ``abstract_cell_patterns`` cell type resolves no pins from either source,
    or the output path's parent directory cannot be created (e.g. it exists
    as a non-directory file). The output path's parent directory is created
    automatically when missing (matching ``klt render``/``klt lvs``),
    including any missing intermediate directories.
    """
    if abstract_cell_lef_paths and not abstract_cell_patterns:
        raise ExtractError(
            "abstract_cell_lef_paths (--abstract-cell-lef) was given but "
            "abstract_cell_patterns (--abstract-cells) is empty -- "
            "--abstract-cell-lef only has an effect as a pin-resolution "
            "fallback for a cell type --abstract-cells actually matches"
        )
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
            deck_for_models = get_extraction_deck(deck_name, deck_options)
        except (UnknownExtractionDeckError, InvalidDeckOptionError) as exc:
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
        voltage_domain_warnings,
        abstracted_cells,
        dead_metal,
    ) = extract_netlist_from_layout(
        path,
        deck_name,
        top=top,
        parasitics_deck=parasitics_deck,
        top_cell_pins_only=top_cell_pins_only,
        declared_pins=declared_pins,
        apply_resistor_fixed_offset=apply_resistor_fixed_offset,
        deck_options=deck_options,
        abstract_cell_patterns=abstract_cell_patterns,
        abstract_cell_lef_paths=abstract_cell_lef_paths,
    )

    import klayout.db as kdb

    netlist_path = output if output is not None else _default_output_path(path)
    out_dir = os.path.dirname(os.path.abspath(netlist_path))
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        raise ExtractError(f"cannot create output directory {out_dir}: {exc}") from exc

    # `_purge_preserving_named_nets()` (in `_extract_netlist`) still drops a
    # circuit entirely when it has no devices, no named/labelled nets, and no
    # subcircuits -- e.g. a layout with no extractable devices and no named
    # nets. That is a legitimate "nothing extracted" result, not an error:
    # report zero devices/nets rather than dereferencing a `None` circuit. A
    # circuit with named/labelled nets but zero devices survives with those
    # nets/pins intact instead (issue #539) -- `circuit` below is only ever
    # `None` for the genuinely-empty case.
    #
    # `devices[]`/`nets[]` are built from the schematic-equivalent netlist
    # *before* any parasitic R/C is injected, so they carry their exact
    # documented meaning whether or not `--parasitics` was given (the
    # additive-contract requirement from issue #216's addendum): parasitic
    # elements never appear in `device_count`/`devices[]`, and the internal
    # parasitic nodes never appear in `net_count`/`nets[]` -- they live only
    # in the written SPICE and in the separate `parasitics` block below.
    # Already validated by `extract_netlist_from_layout` above (it would have
    # raised `ExtractError` on an unknown deck or an invalid `deck_options`
    # entry before reaching this point), so re-fetching it here (with the
    # same `deck_options`, so a selected resistor flavour's name is reflected
    # consistently) to read its static device-class coverage
    # (`device_classes`, issue #221) and its `substrate_net` cannot itself
    # raise.
    deck = get_extraction_deck(deck_name, deck_options)

    circuit = netlist.circuit_by_name(top_cell_name)
    if circuit is not None:
        # The deck's two-term device-parameter corrections
        # (`CapacitorDevice.perim_cap_f_um`, issue #512;
        # `ResistorDevice.fixed_offset_ohm`, issue #518) were already applied
        # to the `kdb.Device` objects by `_apply_device_parameter_corrections`
        # inside `_extract_netlist` (issue #521), so `_describe_devices` just
        # reads them back -- and the `netlist.write(...)` below therefore
        # writes the *same* corrected values into the SPICE file that
        # `devices[].params` reports.
        devices, device_counts = _describe_devices(circuit)
        nets = _describe_nets(circuit)
    else:
        devices, device_counts, nets = [], {}, []

    # Nets whose KLayout-assigned name is a multi-label merge (issue #470):
    # `Net.expanded_name()` joins every distinct label found on one
    # electrical net with `,` (see `tests/test_extract.py`'s
    # `_make_inverter_layout(extra_y_label=...)` fixture, built for issue
    # #312's SPICE-name-sanitization fix), but this response's `nets[]`
    # (built by `_describe_nets`, above) has already rewritten that to the
    # `|`-joined spelling KLayout's own `NetlistSpiceWriter` uses for the
    # net's node references in the written SPICE (`spice_safe_net_name`,
    # issue #696) -- so `merged_net_labels[].net` below is byte-identical to
    # the netlist's own spelling, not a separately (comma-) spelled alias of
    # it. That is a silent signal that two differently-named nets were
    # shorted together on the layout side -- e.g. a `gen-compose` `pins[]`
    # entry naming a port that other connectivity already reaches. Surfaced
    # two ways: a structured `merged_net_labels[]` entry (so a caller does
    # not have to re-derive the label list from the joined string) and a
    # matching prose `warnings[]` entry (so a caller checking only
    # `warnings[]`, per the documented contract, still sees it). See
    # docs/cli/extract.md's "Merged net labels" section for the
    # false-positive limitation: a label that legitimately contains a
    # literal `|` is indistinguishable from a real collision by this
    # heuristic.
    merged_net_labels = _detect_merged_net_labels(nets)
    for merged_entry in merged_net_labels:
        labels_str = ", ".join(merged_entry["labels"])
        warnings.append(
            f"net '{merged_entry['net']}' merges "
            f"{len(merged_entry['labels'])} distinct labels ({labels_str}) "
            "onto one net -- KLayout joins multiple labels found on the "
            "same electrical net (as '|' here and in the written netlist, "
            "matching NetlistSpiceWriter's own node-name escaping); this "
            "usually means two differently-named nets were shorted "
            "together in the layout -- see docs/cli/extract.md's "
            "'Merged net labels' section."
        )

    # PMOS body terminals tied to an anonymous, unbiased net (issue #555):
    # decks with no distinct well-tie/tap layer (gf180mcu) extract the PMOS
    # body onto a KLayout-synthesized `$<n>` net with no DC bias path at
    # all -- the net name was already readable via `devices[].nets["b"]`,
    # but nothing flagged it as *this specific* gap. Surfaced two ways: a
    # structured `unbiased_pmos_body_nets[]` entry (so a caller does not have
    # to re-derive the anonymous-net convention itself) and a matching prose
    # `warnings[]` entry. The `warnings[]` entry is a single aggregate line
    # with the count baked in, mirroring `_detect_unmodelled_poly_bodies`'s
    # aggregate pattern (issue #599) -- one line per device would blow up
    # `warnings[]` at scale (e.g. 148 entries for 148 floating PMOS bodies)
    # and defeat literal-list pinning by a caller.
    unbiased_pmos_body_nets = _detect_unbiased_pmos_body_nets(devices, deck)
    if unbiased_pmos_body_nets:
        device_word = "device" if len(unbiased_pmos_body_nets) == 1 else "devices"
        warnings.append(
            f"{len(unbiased_pmos_body_nets)} PMOS {device_word} tie their "
            "body to an anonymous net with no DC bias path -- "
            f"'{deck_name}' has no distinct well-tie/tap layer for this "
            "PMOS body, so it is left floating rather than tied to a real "
            "supply rail; resimulating this netlist directly will not "
            "reproduce the schematic-level PMOS body bias -- see "
            "unbiased_pmos_body_nets[] for the full list. See "
            "docs/cli/extract.md's 'Parasitic (RC) extraction' section."
        )

    # `--pdk`-bound device classes whose target subcircuit has no parameter
    # for something the extractor measured (issue #695): today, only a
    # sky130 `pnp` bipolar binding, whose fixed-geometry `pnp_05v5_*` cells
    # take no per-instance base/collector-area/perimeter or emitter-count
    # override at all (see `pdk_models.DeviceBinding.dropped_params` and
    # `_BIPOLAR_DROPPED_PARAMS`'s docstring for the full provenance). MOS
    # bindings carry every measured parameter onto the `X` card as of #695
    # (see `devices[].params`' `as_um2`/`ad_um2`/`ps_um`/`pd_um` above) and so
    # never reach this branch. One aggregate line per affected class (not per
    # device), mirroring `unbiased_pmos_body_nets`'s pattern just above --
    # `device_counts` is keyed by `devices[].class`, independent of `--pdk`,
    # so it is safe to consult here even though it was computed before the
    # `--pdk`-bound netlist is written below.
    if model_bindings is not None:
        for class_name in sorted(model_bindings):
            binding = model_bindings[class_name]
            if not binding.dropped_params:
                continue
            count = device_counts.get(class_name, 0)
            if count == 0:
                continue
            device_word = "device" if count == 1 else "devices"
            params_str = "/".join(binding.dropped_params)
            # `binding.subckt` is empty for a "bipolar" binding -- the real
            # subcircuit name is resolved per device, from `binding.variants`
            # (see `_select_bipolar_variant`), not fixed for the whole class
            # -- so name the *target* generically rather than print an empty
            # quoted string.
            target = f"'{binding.subckt}'" if binding.subckt else "its bound target"
            warnings.append(
                f"--pdk binds {count} '{class_name}' {device_word} onto "
                f"{target}, which has no parameter for the extractor's "
                f"measured {params_str} -- these values are "
                "dropped from the written netlist (rerun without --pdk to "
                "recover them from the bare device-class card form). See "
                "docs/cli/extract.md's 'SPICE model binding' section."
            )

    # Nets touching exactly one device terminal and no declared pin (issue
    # #596): there is no DC path through such a node from anywhere else in
    # the netlist, so a downstream simulator hits a singular matrix on it --
    # several stages past where this is structurally detectable from the
    # extracted netlist alone. Surfaced two ways: a structured
    # `single_terminal_nets[]` entry (so a caller does not have to
    # cross-reference `nets[]`/`devices[]` itself) and a matching prose
    # `warnings[]` entry, phrased more strongly for a gate terminal (almost
    # never intentional) than for source/drain/body/resistor-equivalent
    # terminals (can be a legitimate unterminated dummy tie). Each bucket
    # gets its own single aggregate `warnings[]` line with the count baked
    # in (issue #599), mirroring `_detect_unmodelled_poly_bodies`'s
    # two-message-class aggregate pattern -- one line per net would blow up
    # `warnings[]` at scale.
    single_terminal_nets = _detect_single_terminal_nets(devices, nets)
    single_terminal_gate_count = sum(
        1 for entry in single_terminal_nets if entry["terminal_kind"] == "gate"
    )
    single_terminal_other_count = len(single_terminal_nets) - single_terminal_gate_count
    if single_terminal_gate_count:
        net_word = "net" if single_terminal_gate_count == 1 else "nets"
        warnings.append(
            f"{single_terminal_gate_count} {net_word} connect to exactly "
            "one device terminal -- a MOS gate -- and are not a declared "
            "pin: these gates have no DC path from anywhere else in the "
            "netlist, so they are almost certainly unconnected inputs "
            "rather than legitimate floating nodes; resimulating this "
            "netlist directly will hit a singular matrix on these nets -- "
            "see single_terminal_nets[] for the full list."
        )
    if single_terminal_other_count:
        net_word = "net" if single_terminal_other_count == 1 else "nets"
        warnings.append(
            f"{single_terminal_other_count} {net_word} connect to exactly "
            "one device terminal -- source/drain/body/resistor-equivalent, "
            "not a gate -- and are not a declared pin; this can be a "
            "legitimate single-terminal tie (e.g. an intentionally "
            "unterminated dummy's diffusion tie), but confirm these nets "
            "have no other intended connectivity -- see "
            "single_terminal_nets[] for the full list."
        )

    # Layers carrying shapes the deck's connectivity graph never reads (issue
    # #220): geometry there is invisible to extraction, so surface it rather
    # than let it become a silent LVS mismatch downstream.
    ignored_layers = _describe_ignored_layers(path, deck)

    # A material `ignored_layers` result gets a matching `warnings[]` entry
    # (issue #666): `_describe_layers_in_set` already drops every
    # `shapes == 0` entry before it reaches `ignored_layers`, so a non-empty
    # list here is by construction "material" -- geometry that is genuinely
    # invisible to this extraction's connectivity graph, not a stray empty
    # layer declaration. Before this, `ignored_layers` was a diagnostic-only
    # field: a routed net split across an undeclared metal level extracted
    # "successfully" with no signal in `warnings[]`, the one field
    # `docs/cli/extract.md` documents as the minimal self-check every `klt`
    # command output should get. One aggregate line with the shape total
    # baked in (issue #599's pattern), not one line per layer -- mirroring
    # `metals_without_coefficient`'s `warnings[]` entry a bit further down
    # this function.
    if ignored_layers:
        layer_word = "layer" if len(ignored_layers) == 1 else "layers"
        be_word = "is" if len(ignored_layers) == 1 else "are"
        total_shapes = sum(entry["shapes"] for entry in ignored_layers)
        shape_word = "shape" if total_shapes == 1 else "shapes"
        layers_str = ", ".join(
            f"{entry['layer']}/{entry['datatype']}" for entry in ignored_layers
        )
        warnings.append(
            f"{len(ignored_layers)} {layer_word} ({layers_str}) carrying "
            f"{total_shapes} {shape_word} {be_word} outside '{deck_name}' "
            "deck's connectivity graph -- this geometry is invisible to "
            "extraction, so a net routed only through it extracts as "
            "multiple disconnected nets instead of one, which will "
            "silently mismatch a downstream `klt lvs` reference netlist -- "
            "see ignored_layers[] for the full per-layer shape counts. See "
            "docs/cli/extract.md's 'ignored_layers' field documentation."
        )

    # Layers carrying shapes the deck reads for bipolar/capacitor/resistor/
    # diode device recognition but never treats as a `metals`/`vias`
    # connectivity level (issue #619): such a layer does not appear in
    # `ignored_layers` above (it *is* read), but two nets joined only through
    # it will not merge -- exactly the gap that hid sky130's own met3/met4
    # routing-connectivity ceiling behind a clean-looking `ignored_layers`
    # report before this deck's `metals` stack grew to cover them too. Unlike
    # `ignored_layers`, this is *not* mirrored into `warnings[]`: an
    # `ExtractionDeck.device_recognition_only_layers` entry is the deck's own
    # marker/mask geometry (a resistor's `poly.res` marker, a bipolar's ID
    # mark, a MiM cap's top-plate mark) -- layers that are *never* candidate
    # connectivity levels by PDK design, so their presence is routine, not a
    # gap. Reporting them here is diagnostic context for the rare case where
    # a caller genuinely needs to distinguish "read but not merged" from
    # "never read," not a signal that something is wrong with this
    # extraction. See `ExtractionDeck.device_recognition_only_layers`'s
    # docstring and docs/cli/extract.md's "Device-recognition-only layers"
    # section.
    device_recognition_only_layers = _describe_device_recognition_only_layers(
        path, deck
    )

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
        # `parasitics_deck` is only non-None when `--parasitics` was given
        # (see above), which is exactly when `parasitic_nets is not None`.
        assert parasitics_deck is not None
        metal_gaps = _describe_parasitics_metal_gaps(deck, parasitics_deck)
        parasitics_report["metals_without_coefficient"] = metal_gaps
        if metal_gaps:
            levels = ", ".join(f"Metal{gap['metal_index'] + 1}" for gap in metal_gaps)
            warnings.append(
                f"'{deck_name}' deck's PARASITICS.metals has no R/C "
                f"coefficient for {levels} -- --parasitics reports zero "
                "resistance and capacitance for that metal level on every "
                "net, understating the true value. See docs/cli/extract.md's "
                "'Parasitic (RC) extraction' section."
            )

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
        # Additive field (issue #619): always a list, empty when no layer is
        # read for device recognition only -- see run_extract's docstring and
        # `ExtractionDeck.device_recognition_only_layers` for the field's
        # full meaning.
        "device_recognition_only_layers": device_recognition_only_layers,
        "device_classes": list(deck.device_classes),
        "devices": devices,
        "nets": nets,
        "warnings": warnings,
        # Additive field (issue #293): always a list, empty when the layout
        # draws no reserved-annotation-layer geometry -- see run_extract's
        # docstring for the field's full meaning.
        "black_box_regions": black_box_regions,
        # Additive field (issue #620): always a list, empty unless
        # `abstract_cell_patterns` (--abstract-cells) matched at least one
        # instantiated cell -- see run_extract's docstring for the field's
        # full meaning.
        "abstracted_cells": abstracted_cells,
        # Additive field (issue #324): always a list, empty when `warnings`
        # carries no unmodelled-device entry -- see run_extract's docstring
        # and `_detect_unmodelled_poly_bodies` for the field's full meaning.
        "unmodelled_poly": unmodelled_poly,
        # Additive field (issue #470): always a list, empty when no net
        # carries more than one KLayout-assigned label -- see
        # `_detect_merged_net_labels` and the comment above where it is
        # computed for the field's full meaning.
        "merged_net_labels": merged_net_labels,
        # Additive field (issue #552): always a list, empty when this deck
        # registers no voltage-domain marker or none of it overlaps
        # extracted MOS geometry -- see run_extract's docstring and
        # `_detect_voltage_domain_overlap` for the field's full meaning.
        "voltage_domain_warnings": voltage_domain_warnings,
        # Additive field (issue #555): always a list, empty when no PMOS
        # device's body net is the anonymous, KLayout-synthesized kind -- see
        # run_extract's docstring and `_detect_unbiased_pmos_body_nets` for
        # the field's full meaning.
        "unbiased_pmos_body_nets": unbiased_pmos_body_nets,
        # Additive field (issue #596): always a list, empty when every net
        # either has zero or 2+ device terminals, or is a declared pin -- see
        # run_extract's docstring and `_detect_single_terminal_nets` for the
        # field's full meaning.
        "single_terminal_nets": single_terminal_nets,
        # Additive field (issue #676): always a list, empty when every
        # routing-stack (metals/vias) shape joins an extracted net -- see
        # run_extract's docstring and `_detect_dead_metal` for the field's
        # full meaning. Computed inside `_extract_netlist` (it needs the live
        # `LayoutToNetlist` shape database), which also appends its aggregate
        # `warnings[]` entry.
        "dead_metal": dead_metal,
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
        deck_options=deck_options,
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
    declared_pins: frozenset[str] | None = None,
    apply_resistor_fixed_offset: bool = True,
    deck_options: Mapping[str, str] | None = None,
    abstract_cell_patterns: tuple[str, ...] = (),
    abstract_cell_lef_paths: tuple[str, ...] = (),
) -> tuple[
    kdb.Netlist,
    str,
    float,
    list[str],
    list[dict[str, Any]] | None,
    list[dict[str, Any]],
    int,
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Core extraction: read ``path``, resolve ``deck_name`` and the top
    cell, and run flat device + connectivity extraction. Returns
    ``(netlist, top_cell_name, dbu_um, warnings, parasitic_nets,
    black_box_regions, dummy_devices_dropped, unmodelled_poly,
    voltage_domain_warnings, abstracted_cells, dead_metal)``.

    ``abstract_cell_patterns``/``abstract_cell_lef_paths`` (the
    ``--abstract-cells``/``--abstract-cell-lef`` flags, issue #620): when
    ``abstract_cell_patterns`` is non-empty, every instantiated cell whose
    name matches one of the given ``fnmatch`` glob patterns is treated as a
    black box -- its own device-recognition geometry is erased from the
    layout before extraction (see :func:`_erase_abstracted_cell_geometry`)
    and it becomes a pin-only ``.SUBCKT``/``X`` instance in the returned
    netlist instead of contributing devices to the flat top-level circuit
    (see :func:`_wire_abstract_cells`). ``abstracted_cells`` is the JSON
    response's field: one entry per distinct matched cell type, each
    ``{"cell", "instance_count", "pin_count", "resolution_source",
    "lef_path"}``. Always a list, empty when ``abstract_cell_patterns`` is
    empty (the default) -- byte-identical to today's behavior in that case.
    ``abstract_cell_lef_paths`` is consulted only as the *fallback* pin
    source, when a matched cell type draws no in-cell pin label -- see
    :func:`_resolve_abstract_cell_pins`.

    ``deck_options`` (issue #595): forwarded to
    :func:`~klayout_tools.decks.get_extraction_deck` -- selects a
    caller-visible sheet-rho flavour for any resistor family whose
    ``flavour_option`` it names. ``None``/empty resolves the deck unchanged.
    See :func:`run_extract`'s docstring for the full contract.

    ``apply_resistor_fixed_offset`` (issue #559): forwarded to
    ``_extract_netlist`` -- ``True`` (the default) applies each opted-in
    resistor device class's ``fixed_offset_ohm`` correction here, at
    extraction time (unchanged behavior). ``klt lvs``'s
    ``options.combine_devices`` path passes ``False`` and applies the
    correction itself, once, after combining -- see
    :func:`_extract_netlist` and :func:`apply_resistor_fixed_offset_corrections`.

    ``voltage_domain_warnings`` (issue #552) flags extracted MOS device
    geometry that overlaps a voltage-domain marker layer this deck does not
    model the scoping of (e.g. gf180mcu's ``Dualgate`` 55/0) -- see
    :func:`_detect_voltage_domain_overlap`. A matching prose entry is also
    appended to ``warnings``. Always a list, empty for a deck that registers
    no such marker or a layout that draws none of it overlapping MOS
    geometry.

    ``top_cell_pins_only`` (issue #291): when ``True``, only labels drawn
    directly in the top cell are promoted to top-level pins -- a net named
    solely by a label found below an instance boundary keeps its name but
    stays internal. Independent of the flag, ``warnings`` gains an entry
    whenever a below-top label named a promoted pin. See :func:`run_extract`
    for the full rationale.

    ``declared_pins`` (issue #514): when given, every promoted pin *not*
    named in this set is demoted back to an internal net (it keeps its
    name). ``None`` skips this reconciliation. See :func:`run_extract` for
    the full rationale.

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

    Raises :class:`ExtractError` for a bad file, unknown deck, invalid
    ``deck_options`` entry, or missing/ambiguous top cell -- identical error
    semantics to ``run_extract``.
    """
    if not os.path.exists(path):
        raise ExtractError(f"file not found: {path}")
    if os.path.isdir(path):
        raise ExtractError(f"not a file: {path}")

    try:
        deck = get_extraction_deck(deck_name, deck_options)
    except (UnknownExtractionDeckError, InvalidDeckOptionError) as exc:
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

    # `--abstract-cells` (issue #620): resolved *before* `_extract_netlist`
    # runs, by mutating `layout` in place -- see
    # `_erase_abstracted_cell_geometry`'s docstring for why erasing each
    # matched cell type's own device-recognition geometry here (rather than
    # masking `Region` objects deep inside `_extract_netlist`) is both
    # simpler and correct for every device class, including ones
    # `_extract_netlist` re-reads straight from `layout` (bipolar/diode/
    # capacitor), not just the ones it threads through local `Region`
    # variables (MOS/resistor). `lef_macros` is loaded once here (an
    # `--abstract-cell-lef` path is a filesystem read, not layout data),
    # even though it is consulted per matched cell type inside
    # `_wire_abstract_cells`.
    abstract_instances: list[tuple[int, kdb.ICplxTrans]] = []
    lef_macros: dict[str, tuple[str, dict[str, list[dict[str, Any]]]]] = {}
    if abstract_cell_patterns:
        abstract_instances = _collect_abstract_instances(
            layout, top_cell, abstract_cell_patterns
        )
        if abstract_instances:
            mask_layers = _abstract_cell_mask_layers(deck)
            matched_cell_indices = dict.fromkeys(
                cell_index for cell_index, _trans in abstract_instances
            )
            _erase_abstracted_cell_geometry(layout, matched_cell_indices, mask_layers)
        if abstract_cell_lef_paths:
            lef_macros = _load_abstract_cell_lefs(abstract_cell_lef_paths)

    (
        netlist,
        warnings,
        parasitic_nets,
        black_box_regions,
        dummy_devices_dropped,
        unmodelled_poly,
        abstracted_cells,
        dead_metal,
    ) = _extract_netlist(
        layout,
        top_cell,
        deck,
        parasitics_deck,
        top_cell_pins_only=top_cell_pins_only,
        declared_pins=declared_pins,
        apply_resistor_fixed_offset=apply_resistor_fixed_offset,
        abstract_cell_patterns=abstract_cell_patterns,
        abstract_instances=abstract_instances,
        lef_macros=lef_macros,
    )

    # Voltage-domain marker overlap (issue #552): computed after the main
    # extraction pass, against the same `layout`/`top_cell`/`deck` it just
    # used, so this stays a purely additive diagnostic layered on top of an
    # otherwise-unchanged extraction -- no rule threshold or model binding
    # changes because of it. See `_detect_voltage_domain_overlap`'s
    # docstring for the exact interacting-geometry gate.
    (
        voltage_domain_prose_warnings,
        voltage_domain_warnings,
    ) = _detect_voltage_domain_overlap(layout, top_cell, deck, deck_name)
    warnings = warnings + voltage_domain_prose_warnings

    return (
        netlist,
        top_cell.name,
        layout.dbu,
        warnings,
        parasitic_nets,
        black_box_regions,
        dummy_devices_dropped,
        unmodelled_poly,
        voltage_domain_warnings,
        abstracted_cells,
        dead_metal,
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


def _promote_orphan_named_nets(netlist: kdb.Netlist) -> None:
    """Promote to pins the top-circuit named nets ``make_top_level_pins()``
    silently skips (issue #539).

    ``Netlist.make_top_level_pins()``'s docstring says it "will turn all
    named nets of top-level circuits ... into pins", but empirically
    (verified directly against ``klayout.db``, independent of this module)
    it only promotes a named net that already has at least one device
    terminal or subcircuit pin attached -- the same internal "not floating"
    test ``Net.is_floating()``/``purge()`` use. A net that is *only* named
    (e.g. a bond pad, seal ring, or RDL segment carrying a pin-purpose
    label but touching zero recognised devices) is skipped entirely, even
    though it is real, distinct, and correctly labelled.

    Called right after ``netlist.make_top_level_pins()`` and *before* the
    below-top-label/declared-pins demotion passes, so a net this function
    promotes is still subject to exactly the same demotion rules as a
    "normally" promoted one -- no separate code path for orphan nets to
    silently diverge from.
    """
    for circuit in netlist.top_circuits():
        for net in circuit.each_net():
            if not net.name:
                continue
            if net.pin_count() > 0:
                continue
            if net.terminal_count() or net.subcircuit_pin_count():
                continue
            pin = circuit.create_pin(net.name)
            circuit.connect_pin(pin, net)


def _purge_preserving_named_nets(netlist: kdb.Netlist) -> None:
    """Run ``Netlist.purge()`` without discarding named/labelled nets that
    are currently exposed as a pin but have no device terminal and no
    subcircuit pin attached (issue #539).

    ``Netlist.purge()``'s own floating-net definition -- "no device and no
    subcircuit on it" -- does *not* treat a pin connection as exempting a
    net: verified directly against ``klayout.db`` that manually creating
    and connecting a pin to such a net and then calling ``purge()`` still
    removes the net *and* its pin, and (when nothing else keeps the owning
    circuit alive) the circuit itself. A circuit whose nets are only ever
    named/labelled -- zero recognised devices -- would otherwise purge to
    an empty netlist even though its nets are real, distinct, and
    correctly labelled (the bug this issue reports).

    Snapshots every currently-*pinned* net that would otherwise be treated
    as floating -- name, owning circuit -- *before* calling the real
    ``purge()``, so every other case is still cleaned up exactly as
    before: genuinely floating unnamed junk nets, genuinely empty
    circuits, *and* a named-but-never-promoted internal net (e.g. one the
    below-top-label/declared-pins reconciliation passes deliberately left
    unpinned) -- the latter has no SPICE representation at all (no pin, no
    element references it), so preserving it in the in-memory netlist
    would silently diverge from what a round trip through
    ``NetlistSpiceWriter``/``NetlistSpiceReader`` (as `klt lvs`'s
    pre-extracted-reference path does) can actually reproduce. Restricting
    this guard to pinned nets keeps every consumer's view consistent.

    Restores whatever ``purge()`` removed from the pinned survivors:
    recreating the owning circuit if it was dropped entirely, the net if
    it was dropped, and the pin (reconnected to the recreated net).

    A recreated circuit/net also carries its predecessor's
    ``Circuit.cell_index``/``Net.cluster_id`` across the purge. That pair is
    the key ``LayoutToNetlist`` uses to find a net's shapes, so a rescued net
    stays fully queryable -- ``polygons_of_net`` (and therefore
    :func:`_compute_parasitics`, which runs *after* this pass and iterates
    every surviving net) returns its real geometry instead of faulting inside
    KLayout's hierarchical network processor on the default cluster id ``0``.
    Without it, ``klt extract --parasitics`` crashed with an unhandled
    internal ``RuntimeError`` on exactly the device-less labelled layouts this
    function exists to preserve (bond pads, seal rings, RDL segments,
    power-mesh straps -- all plausible parasitic-extraction targets).

    A circuit with genuinely no devices *and* no named/labelled nets finds
    no survivors to snapshot here, so it purges to nothing exactly as
    before -- this function only ever *adds back* pinned nets ``purge()``
    would otherwise silently drop, never changes behaviour for the
    legitimate "nothing extracted" case.
    """
    import klayout.db as kdb

    survivors: list[tuple[str, int, str, int]] = []
    for circuit in netlist.each_circuit():
        for net in circuit.each_net():
            if not net.name or net.pin_count() == 0:
                continue
            if net.terminal_count() or net.subcircuit_pin_count():
                continue
            survivors.append(
                (circuit.name, circuit.cell_index, net.name, net.cluster_id)
            )

    netlist.purge()

    for circuit_name, cell_index, net_name, cluster_id in survivors:
        circuit = netlist.circuit_by_name(circuit_name)
        if circuit is None:
            circuit = kdb.Circuit()
            circuit.name = circuit_name
            # Re-link the recreated circuit to the layout cell it was
            # extracted from (issue #563): `LayoutToNetlist`'s shape queries
            # (`polygons_of_net`, used by `_compute_parasitics`) look the
            # net's cluster up in the per-cell cluster store keyed by
            # `Circuit.cell_index`. A bare `kdb.Circuit()` leaves that at its
            # default, so every later shape query against a net it owns would
            # fault inside KLayout's hierarchical network processor.
            circuit.cell_index = cell_index
            netlist.add(circuit)

        net = next((n for n in circuit.each_net() if n.name == net_name), None)
        if net is None:
            net = circuit.create_net(net_name)
            # Same rationale as `cell_index` above, for the other half of the
            # (cell, cluster) key: `Net.cluster_id` is what ties a net back to
            # the connectivity cluster `LayoutToNetlist` extracted it from. A
            # freshly created net starts at cluster 0 -- the sentinel KLayout
            # asserts against (`id > 0 was not true in
            # LayoutToNetlist.polygons_of_net`) -- so a rescued net must carry
            # the id its purged predecessor had, or `klt extract --parasitics`
            # crashes on exactly the device-less labelled layouts (bond pads,
            # seal rings, RDL, power straps) issue #539 exists to preserve.
            net.cluster_id = cluster_id

        if net.pin_count() == 0:
            pin = circuit.create_pin(net_name)
            circuit.connect_pin(pin, net)


def _purge_truly_floating_nets(netlist: kdb.Netlist) -> None:
    """Remove every net, on every circuit, that has **no** pin, **no**
    device terminal, and **no** subcircuit pin -- and nothing else (issue
    #620's ``--abstract-cells`` purge path).

    Unlike ``Netlist.purge()`` (and the ``_purge_preserving_named_nets``
    rescue built on top of it), this never removes a circuit or a
    ``SubCircuit`` instance, and never removes a net that carries *any*
    connection at all, regardless of whether that connection eventually
    leads to a real ``kdb.Device`` anywhere in the hierarchy. See the call
    site's comment (in :func:`_extract_netlist`) for why that distinction
    matters once a black-box abstraction -- which is, by definition,
    device-free -- is in the netlist: ``Netlist.purge()`` judges an entire
    subcircuit chain "unused" (and deletes the circuit, the ``SubCircuit``
    instance, and the parent net it was wired to) whenever that chain is not
    transitively connected to a real device, which is *always* true for a
    pure black-box instance.
    """
    for circuit in netlist.each_circuit():
        floating = [
            net
            for net in circuit.each_net()
            if net.pin_count() == 0
            and net.terminal_count() == 0
            and net.subcircuit_pin_count() == 0
        ]
        for net in floating:
            circuit.remove_net(net)


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


# --------------------------------------------------------------------------- #
# Cell-level (black-box + pins) abstraction -- `--abstract-cells`, issue #620
# --------------------------------------------------------------------------- #


def _matches_abstract_pattern(name: str, patterns: tuple[str, ...]) -> bool:
    """``True`` when the cell ``name`` matches any of the ``--abstract-cells``
    glob ``patterns``.

    Case-sensitive (``fnmatch.fnmatchcase``): GDSII/OASIS cell names are
    case-sensitive, and ``fnmatch.fnmatch`` would otherwise fold case on a
    case-insensitive filesystem only -- a platform-dependent match is exactly
    the kind of surprise a layout-processing contract must not have.
    """
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def _collect_abstract_instances(
    layout: kdb.Layout, top_cell: kdb.Cell, patterns: tuple[str, ...]
) -> list[tuple[int, kdb.ICplxTrans]]:
    """Every instance of a pattern-matched cell type under ``top_cell``, as
    ``[(cell_index, transform-into-top-cell-coordinates), ...]``.

    Walks the instance tree explicitly rather than through
    ``begin_shapes_rec``, because the per-instance transform is exactly what
    a pin footprint has to be resolved through (acceptance criterion 6):
    ``CellInstArray.each_cplx_trans()`` yields one ``ICplxTrans`` per array
    element, already carrying that element's rotation/mirror *and* its
    displacement within the array, so a mirrored or rotated instance of the
    same abstracted cell type resolves its pins at the right places for free.

    A matched branch is **never descended into**: an abstracted cell that
    itself instantiates another matched cell type is abstracted as a single
    outermost black box, not twice. The top cell itself is never abstracted
    even if a pattern matches its name -- ``klt extract`` extracts *from* the
    top cell, so black-boxing it would produce an empty netlist rather than a
    hierarchical one.

    Returned in a deterministic order: sorted by ``(cell name, displacement
    x, displacement y, transform string)``, so the ``X<instance>`` cards the
    caller emits carry stable names across runs.
    """
    import klayout.db as kdb

    found: list[tuple[int, kdb.ICplxTrans]] = []

    def walk(cell: kdb.Cell, trans: kdb.ICplxTrans) -> None:
        for inst in cell.each_inst():
            target = layout.cell(inst.cell_index)
            matched = _matches_abstract_pattern(target.name, patterns)
            for element in inst.cell_inst.each_cplx_trans():
                composed = trans * element
                if matched:
                    found.append((inst.cell_index, composed))
                else:
                    walk(target, composed)

    walk(top_cell, kdb.ICplxTrans())

    found.sort(
        key=lambda entry: (
            layout.cell(entry[0]).name,
            entry[1].disp.x,
            entry[1].disp.y,
            str(entry[1]),
        )
    )
    return found


def _abstract_cell_mask_layers(deck: ExtractionDeck) -> set[tuple[int, int]]:
    """Every deck-read layer that must be erased from an abstracted cell's
    own definition so no device is recognised inside it (issue #620).

    Defined as ``ExtractionDeck.connectivity_layers`` (every layer this
    deck's connectivity graph reads at all -- already the curated set behind
    the ``ignored_layers`` diagnostic, so it automatically covers every
    device-recognition/marker layer any current or future deck declares,
    including resistor/bipolar/diode markers and capacitor plates) *minus*:

    - the routing/interconnect layers (``contact``, ``metals``, ``vias``) --
      a parent's connection to a black-boxed cell's pin must still reach
      through the cell's own local interconnect, so these are left intact;
    - the label layers (``well_label``, ``poly_label``, ``metal_labels``) --
      :func:`_resolve_abstract_cell_pins` reads a pin's name and access point
      directly off these, in the cell's own (otherwise-erased) definition.

    A resistor/capacitor whose recognition layer happens to be one of the
    deck's own ``metals`` (a real but rare deck configuration) is a known,
    documented gap: that layer is never erased (it is routing), so such a
    device could still be recognised inside an abstracted cell. Every
    curated deck shipped in this repo declares resistor/capacitor bodies on
    ``poly``/``active``/a dedicated plate layer, never directly on
    ``metals``, so this does not affect ``sky130``/``gf180mcu`` today.
    """
    routing = {deck.contact, *deck.metals, *deck.vias}
    labels = {deck.well_label, deck.poly_label, *deck.metal_labels}
    return {
        layer
        for layer in deck.connectivity_layers
        if layer not in routing and layer not in labels
    }


def _erase_abstracted_cell_geometry(
    layout: kdb.Layout,
    cell_indices: Iterable[int],
    mask_layers: set[tuple[int, int]],
) -> None:
    """Erase every ``mask_layers`` shape from each of ``cell_indices`` and
    every cell it (transitively) calls -- issue #620's black-box
    abstraction.

    Mutates ``layout`` in place: every downstream ``_region()``/``_texts()``
    call (which always re-derives its ``Region``/``Texts`` fresh from
    ``layout``) then sees these cells as if they carried no
    device-recognition geometry at all, with no changes needed anywhere else
    in :func:`_extract_netlist`'s device-recognition passes -- including
    ones (bipolar/diode/capacitor) that re-read straight from ``layout``
    rather than through a locally-masked ``Region`` variable. Safe because
    ``layout`` is loaded fresh, once, for this one extraction run
    (:func:`extract_netlist_from_layout`) and never shared or cached across
    calls.

    A cell type matched by ``--abstract-cells`` is assumed to be used
    *exclusively* as a black box wherever it is instantiated in this stream
    -- erasing its own definition directly affects every instance, not just
    the ones reachable from the extraction top cell, and that is fine (there
    is no cheaper way to erase "this instance only" for a shared cell
    definition, and abstracting the same standard cell/macro differently in
    different places is not a meaningful operation LVS could compare against
    anyway).

    A **called** cell (one of ``cell_indices``' children, transitively) makes
    no such promise, though: KLayout cell definitions are shared across every
    place they are instantiated, and a cell used inside a matched macro may
    also be instantiated independently *outside* every matched subtree (e.g.
    a standard cell reused both inside an abstracted macro and directly at
    top level). Erasing a called cell's definition in place would silently
    destroy that unrelated instance's devices too. So each matched cell's
    *own* instances of children are instead repointed at a private,
    per-child-cell "shadow" duplicate (:meth:`kdb.Cell.copy_tree` -- a fresh,
    unshared copy of the child's entire subtree, at every depth) before
    erasing -- the shadow is never referenced by anything outside a matched
    cell's own hierarchy, so erasing it can never affect a sibling instance
    of the same cell type used elsewhere.
    """

    def clear_mask_layers(cell_index: int) -> None:
        cell = layout.cell(cell_index)
        for layer in mask_layers:
            layer_index = layout.find_layer(*layer)
            if layer_index is not None:
                cell.shapes(layer_index).clear()

    # Original called-cell index -> private, unshared duplicate of its whole
    # subtree. Shared across every matched cell that calls the same child
    # cell type -- both sides of that sharing are themselves matched (about
    # to be erased), so reusing one shadow between them is safe.
    shadow_cells: dict[int, int] = {}

    def shadow_for(child_index: int) -> int:
        shadow_index = shadow_cells.get(child_index)
        if shadow_index is not None:
            return shadow_index
        child_cell = layout.cell(child_index)
        shadow = layout.create_cell(
            layout.unique_cell_name(f"{child_cell.name}$abstract")
        )
        shadow.copy_tree(child_cell)
        shadow_index = shadow.cell_index()
        shadow_cells[child_index] = shadow_index
        for ci in {shadow_index, *shadow.called_cells()}:
            clear_mask_layers(ci)
        return shadow_index

    seen: set[int] = set()
    for cell_index in cell_indices:
        if cell_index in seen:
            continue
        seen.add(cell_index)
        cell = layout.cell(cell_index)
        clear_mask_layers(cell_index)
        for inst in list(cell.each_inst()):
            inst.cell_index = shadow_for(inst.cell_index)


def _texts_excluding_abstract_cells(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    layer: tuple[int, int] | None,
    patterns: tuple[str, ...],
) -> kdb.Texts:
    """:func:`_texts` restricted to labels drawn *outside* every abstracted
    cell type (issue #620).

    An abstracted cell's own in-cell labels are its **pin names**, not the
    parent's net names: left in the flat label collection they would rename
    (or comma-merge into) whatever top-level net the pin happens to touch --
    e.g. every instance of an inverter contributing its internal ``A`` label
    to a different routing net. Stripping them keeps the un-abstracted
    portion's net naming exactly what it would be if the cell were a hard
    macro with no drawn labels at all.
    """
    import klayout.db as kdb

    texts = kdb.Texts()
    if layer is None:
        return texts
    layer_index = layout.find_layer(*layer)
    if layer_index is None:
        return texts

    def walk(cell: kdb.Cell, trans: kdb.ICplxTrans) -> None:
        for text in kdb.Texts(cell.shapes(layer_index)).each():
            texts.insert(text.transformed(trans))
        for inst in cell.each_inst():
            target = layout.cell(inst.cell_index)
            if _matches_abstract_pattern(target.name, patterns):
                continue
            for element in inst.cell_inst.each_cplx_trans():
                walk(target, trans * element)

    walk(top_cell, kdb.ICplxTrans())
    return texts


def _load_abstract_cell_lefs(
    lef_paths: tuple[str, ...],
) -> dict[str, tuple[str, dict[str, list[dict[str, Any]]]]]:
    """Read every ``--abstract-cell-lef`` file into ``{<MACRO name>: (<lef
    path>, {<pin name>: [port box, ...]})}``.

    A path may be a LEF file or a directory (every ``*.lef``/``*.tlef`` file
    directly inside it is read, sorted by name for determinism). A macro
    declared by more than one LEF resolves to the **first** path given, so
    the flag's order is the precedence order -- an explicit block abstract
    passed ahead of a PDK's merged standard-cell LEF wins, rather than the
    result depending on directory iteration order.

    Raises :class:`ExtractError` for an unreadable path -- a mistyped LEF
    path must not silently degrade to "this cell has no LEF fallback".
    """
    macros: dict[str, tuple[str, dict[str, list[dict[str, Any]]]]] = {}
    for path in lef_paths:
        if os.path.isdir(path):
            entries = sorted(
                os.path.join(path, name)
                for name in os.listdir(path)
                if name.endswith((".lef", ".tlef"))
            )
        else:
            entries = [path]
        for entry in entries:
            try:
                parsed = read_lef_macro_pin_ports(entry)
            except OSError as exc:
                raise ExtractError(
                    f"could not read --abstract-cell-lef '{entry}': {exc}"
                ) from exc
            for macro_name, pins in parsed.items():
                macros.setdefault(macro_name, (entry, pins))
    return macros


def _resolve_abstract_cell_pins(
    layout: kdb.Layout,
    cell: kdb.Cell,
    deck: ExtractionDeck,
    lef_macros: dict[str, tuple[str, dict[str, list[dict[str, Any]]]]],
) -> tuple[list[tuple[str, kdb.Point, str | None]], str | None, str | None, list[str]]:
    """Resolve one abstracted cell type's pins (issue #620).

    Returns ``(pins, resolution_source, lef_path, warnings)`` where ``pins`` is
    ``[(pin name, access point in **cell-local dbu**, probe layer role or
    ``None``), ...]`` sorted by pin name (the stable ``.subckt`` pin order),
    and ``resolution_source`` is:

    - ``"in_cell_labels"`` -- the cell draws text on one of the deck's own
      label layers (``metal_labels[i]``/``well_label``/``poly_label``)
      **directly in the cell** (``cell.shapes``, never
      ``begin_shapes_rec``): a label promoted up from a nested sub-cell
      belongs to that sub-cell's interface, not this one's, and the whole
      point of this mode is to stop at *this* cell's boundary. Each label
      carries its own layer role, so the pin is probed on exactly the
      conductor its label names.
    - ``"lef_abstract"`` -- no such label, but a ``--abstract-cell-lef`` LEF
      declares a ``MACRO`` of this cell's name with ``PORT`` geometry. Each
      port's bounding-box centre is the access point, read directly as
      cell-local micrometres converted to dbu -- the standard LEF/GDS
      convention every real PDK standard-cell library follows: a macro's
      ``ORIGIN`` is ``0 0`` and its ``PORT`` coordinates already sit in the
      same local frame as the cell's own drawn GDS geometry (no additional
      shift). A LEF whose macro declares a non-zero ``ORIGIN`` relative to
      its cell's drawn geometry is a known, out-of-scope gap -- not expected
      for a standard-cell/hard-macro LEF generated by (or compatible with)
      real PDK tooling. The LEF layer name is not translated to a GDS layer
      (that would need a PDK layer map ``klt extract`` does not resolve), so
      these pins carry no layer role and are probed against the deck's
      conductor layers bottom-up instead.
    - ``None`` -- neither source resolved anything; the caller turns this
      into an :class:`ExtractError` when the cell type actually has
      instances.

    ``warnings`` (issue #624) is only ever populated on the ``"lef_abstract"``
    path: a LEF-declared ``PIN`` whose :func:`~klayout_tools.lef_header.
    parse_lef_macro_pin_ports` entry resolves zero port boxes (e.g. a pin
    drawn only via ``PATH``/``VIA`` geometry, or a malformed ``POLYGON`` --
    both statement shapes that module's own "declarative header + pin-access-
    point only" scope deliberately never reads, see its module docstring) is
    dropped from the returned pin list rather than raising, mirroring every
    other malformed/partial-LEF tolerance in this codebase -- but *this*
    silent drop is otherwise invisible to a caller, since it never prevents
    the macro overall from resolving (the acceptance criterion only requires
    *some* pin to resolve). One ``warnings`` entry per dropped pin names the
    macro and pin so the gap has a caller-visible signal instead of a
    silently incomplete black-box ``.SUBCKT``. Always ``[]`` on the
    ``"in_cell_labels"``/``None`` paths, where every resolved pin always
    carries a concrete point (an in-cell label is never geometry-less).
    """
    import klayout.db as kdb

    label_roles: list[tuple[tuple[int, int] | None, str]] = [
        (deck.well_label, "nwell"),
        (deck.poly_label, "poly"),
    ]
    label_roles += [
        (layer, f"metal{index}") for index, layer in enumerate(deck.metal_labels)
    ]

    in_cell: dict[str, tuple[kdb.Point, str]] = {}
    for layer, role in label_roles:
        if layer is None:
            continue
        layer_index = layout.find_layer(*layer)
        if layer_index is None:
            continue
        for text in kdb.Texts(cell.shapes(layer_index)).each():
            # First label wins for a repeated name (a pin drawn with two
            # access points): both name the same electrical node, so probing
            # either resolves the same net -- picking deterministically is
            # what matters, and `label_roles` is itself a fixed order.
            in_cell.setdefault(text.string, (kdb.Point(text.x, text.y), role))

    if in_cell:
        pins = [(name, point, role) for name, (point, role) in in_cell.items()]
        pins.sort(key=lambda entry: entry[0])
        return pins, "in_cell_labels", None, []

    entry = lef_macros.get(cell.name)
    if entry is not None:
        lef_path, lef_pins = entry
        dbu = layout.dbu
        lef_resolved: list[tuple[str, kdb.Point, str | None]] = []
        lef_warnings: list[str] = []
        for pin_name in sorted(lef_pins):
            boxes = lef_pins[pin_name]
            if not boxes:
                lef_warnings.append(
                    f"--abstract-cell-lef macro '{cell.name}' pin "
                    f"'{pin_name}' declared no PORT geometry this parser "
                    "reads (RECT/POLYGON only -- PATH/VIA and malformed "
                    "POLYGON statements are skipped) -- pin dropped from "
                    "the abstracted cell's resolved pin list"
                )
                continue
            x0, y0, x1, y1 = boxes[0]["bbox_um"]
            lef_resolved.append(
                (
                    pin_name,
                    kdb.Point(
                        round(((x0 + x1) / 2) / dbu),
                        round(((y0 + y1) / 2) / dbu),
                    ),
                    None,
                )
            )
        if lef_resolved:
            return lef_resolved, "lef_abstract", lef_path, lef_warnings

    return [], None, None, []


def _probe_abstract_pin_net(
    l2n: kdb.LayoutToNetlist,
    point: kdb.Point,
    role: str | None,
    probe_layers: list[tuple[str, kdb.Region]],
) -> kdb.Net | None:
    """The extracted net at ``point``, via
    ``LayoutToNetlist.probe_net(<layer>, <dbu point>)``.

    ``role`` (present for a label-resolved pin) names the conductor the pin's
    own label was drawn on, so that layer is probed first and its answer is
    authoritative. A pin with no role (the LEF fallback, whose LEF layer name
    is not translated to a GDS layer) falls back to probing every conductor
    in ``probe_layers`` order -- metals bottom-up, then poly/nwell/tap -- and
    takes the first hit, since a standard cell's pins land on the lowest
    metal available. Returns ``None`` when no conductor carries geometry at
    that point at all.
    """
    if role is not None:
        for name, region in probe_layers:
            if name == role:
                net = l2n.probe_net(region, point)
                if net is not None:
                    return net
                break
    for _name, region in probe_layers:
        net = l2n.probe_net(region, point)
        if net is not None:
            return net
    return None


def _wire_abstract_cells(
    layout: kdb.Layout,
    deck: ExtractionDeck,
    l2n: kdb.LayoutToNetlist,
    netlist: kdb.Netlist,
    top_circuit: kdb.Circuit,
    instances: list[tuple[int, kdb.ICplxTrans]],
    lef_macros: dict[str, tuple[str, dict[str, list[dict[str, Any]]]]],
    probe_layers: list[tuple[str, kdb.Region]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Wire every ``--abstract-cells``-matched instance into ``netlist`` as a
    black-box ``kdb.SubCircuit`` (issue #620), and return the JSON response's
    ``abstracted_cells[]`` field alongside any ``warnings`` this pass itself
    generates.

    For each *distinct* matched cell type (grouped from ``instances``, first-
    seen order): resolves its pins once (:func:`_resolve_abstract_cell_pins`,
    raising :class:`ExtractError` if neither source resolves any pin -- the
    acceptance criterion that a matched-but-unresolvable cell type must fail
    loudly, not silently drop pins or emit an unconnected instance), then
    creates one pin-only ``kdb.Circuit`` for that cell type (no devices --
    exactly the shell ``NetlistSpiceWriter`` already emits as an empty
    ``.SUBCKT ... .ENDS`` block for a *device-free* circuit; verified
    directly against ``klayout.db``). Every occurrence of that cell type then
    becomes one ``kdb.SubCircuit`` in ``top_circuit``, with each pin
    connected to the net probed at its instance-transformed access point
    (:func:`_probe_abstract_pin_net`) -- the same net object the flat,
    un-abstracted portion of the layout already resolved via ordinary
    ``l2n.connect()`` wiring (this cell's own conductor/contact geometry was
    deliberately left un-erased for exactly this reason, see
    :func:`_abstract_cell_mask_layers`).

    Must run on the *live* ``netlist``/``l2n`` pair, before
    ``Netlist.make_top_level_pins()``/``purge()`` -- connecting a
    ``SubCircuit`` pin to a net gives that net a non-zero
    ``subcircuit_pin_count()``, which is what keeps an otherwise-bare routing
    stub (the abstracted cell's own metal, touching no device outside it)
    from being purged as floating before this pass has a chance to use it.

    A pin whose resolved access point lands on no conductor at all (e.g. an
    unrouted design, or a LEF-fallback coordinate that does not land inside
    the drawn footprint) does not fail the whole run: a fresh, otherwise
    disconnected net is created for it instead, and a ``warnings[]`` entry
    names the instance/pin -- mirroring every other per-instance geometric-
    miss diagnostic this module already reports (e.g.
    ``unbiased_pmos_body_nets``) rather than a hard error for a single
    instance's placement issue.

    A LEF-fallback pin the ``--abstract-cell-lef`` parser could not resolve
    any ``PORT`` geometry for (issue #624 -- see
    :func:`_resolve_abstract_cell_pins`'s own ``warnings`` docs) is dropped
    from the black-box ``.SUBCKT`` entirely rather than wired in; its own
    ``warnings[]`` entry (one per macro/pin, not per instance -- pin
    resolution happens once per cell *type*) is folded into this function's
    returned ``warnings`` alongside the per-instance geometric-miss entries
    above.

    Instance/subckt naming is deterministic: cell types are visited in
    ``instances``'s own order (already sorted by
    :func:`_collect_abstract_instances`); each occurrence is named
    ``"<cell type>_<n>"`` (0-based, per cell type), sanitized for SPICE via
    :func:`_sanitize_instance_name`.
    """
    import klayout.db as kdb

    grouped: dict[int, list[kdb.ICplxTrans]] = {}
    for cell_index, trans in instances:
        grouped.setdefault(cell_index, []).append(trans)

    report: list[dict[str, Any]] = []
    warnings: list[str] = []

    for cell_index, transforms in grouped.items():
        cell = layout.cell(cell_index)
        pins, source, lef_path, pin_warnings = _resolve_abstract_cell_pins(
            layout, cell, deck, lef_macros
        )
        warnings.extend(pin_warnings)
        if source is None:
            raise ExtractError(
                f"--abstract-cells matched cell type '{cell.name}' "
                f"({len(transforms)} instance(s)), but no pins could be "
                "resolved for it: it draws no label directly in its own "
                "definition on any of this deck's well_label/poly_label/"
                "metal_labels layers, and no --abstract-cell-lef declares a "
                f"MACRO named '{cell.name}' -- pass at least one pin source "
                "for this cell type, or narrow --abstract-cells to exclude it"
            )

        black_box_circuit = kdb.Circuit()
        black_box_circuit.name = cell.name
        pin_ids: dict[str, int] = {}
        for pin_name, _point, _role in pins:
            pin = black_box_circuit.create_pin(pin_name)
            net = black_box_circuit.create_net(pin_name)
            black_box_circuit.connect_pin(pin, net)
            pin_ids[pin_name] = pin.id()
        netlist.add(black_box_circuit)

        for index, trans in enumerate(transforms):
            instance_name = _sanitize_instance_name(f"{cell.name}_{index}")
            subcircuit = top_circuit.create_subcircuit(black_box_circuit, instance_name)
            for pin_name, point, role in pins:
                global_point = trans * point
                net = _probe_abstract_pin_net(l2n, global_point, role, probe_layers)
                if net is None:
                    net = top_circuit.create_net(f"{instance_name}__{pin_name}")
                    warnings.append(
                        f"--abstract-cells instance '{instance_name}' (cell "
                        f"'{cell.name}') pin '{pin_name}': no conductor found "
                        "at its resolved access point -- left unconnected to "
                        "any parent net"
                    )
                subcircuit.connect_pin(pin_ids[pin_name], net)

        report.append(
            {
                "cell": cell.name,
                "instance_count": len(transforms),
                "pin_count": len(pins),
                "resolution_source": source,
                "lef_path": lef_path,
            }
        )

    report.sort(key=lambda entry: entry["cell"])
    return report, warnings


def _resolve_resistors(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    deck: ExtractionDeck,
    poly: kdb.Region,
    active: kdb.Region,
    metals: list[kdb.Region],
    dummy: kdb.Region,
) -> tuple[
    list[tuple[ResistorDevice, kdb.Region, kdb.Region]],
    kdb.Region,
    kdb.Region,
    list[kdb.Region],
    kdb.Region,
    int,
]:
    """Resolve the deck's drawn-resistor declarations against this layout.

    Returns ``(resistors, poly, active, metals, poly_candidate_bodies,
    dummy_devices_dropped)`` where ``resistors`` is one ``(spec,
    body_region, terminal_region)`` triple per *recognised* device class and
    the three conductor regions are the deck's originals with every
    recognised resistor body **subtracted** -- so the caller's connectivity
    graph (and its MOS gate/source-drain split) sees the resistor's heads as
    ordinary conductor and the resistive segment as a hole, instead of one
    continuous short (see :class:`~klayout_tools.decks.ResistorDevice`).

    A resistor body is ``body_layer & marker & all(requires) - any(excludes)``.
    A spec whose body region comes out empty on this layout (the common case
    -- no PDK resistor marker drawn anywhere) is dropped entirely and
    subtracts nothing, so extraction of a resistor-free layout is bit-for-bit
    what it was before this feature existed.

    ``dummy`` is the deck's optional dummy-device marker region (see
    ``ExtractionDeck.dummy``, issue #295) -- possibly empty when the deck
    declares no ``dummy`` layer or the layout draws no such geometry.
    Mirroring the MOS gate-suppression idiom in ``_extract_netlist``, a
    resistor body's *candidate* region (after ``requires``/``excludes`` but
    before the ``dummy`` cut) has ``dummy`` subtracted before it is handed
    off as a recognised device: any *connected component* of the candidate
    fully consumed by ``dummy`` is dropped outright (counted into
    ``dummy_devices_dropped``, issue #462) rather than registered as a
    device, while a component only partially covered survives as a clean
    geometric cut -- the same all-or-nothing distinction the MOS path
    already makes. Whatever ``dummy`` removes from a candidate body is *not*
    subtracted from the caller's conductor region, so it stays present as
    ordinary conductor, exactly like a suppressed MOS gate's poly.

    ``poly_candidate_bodies`` is the union of every *candidate* body region
    (post ``requires``/``excludes``, but **before** the ``dummy`` cut) whose
    ``spec.body`` is the deck's ``poly`` layer -- used by
    :func:`_detect_unmodelled_poly_bodies` to recognise a fully
    dummy-suppressed poly resistor's own footprint as "claimed" (so it is
    never misflagged as an unmodelled-device gap, issue #462), distinct from
    the narrower post-dummy body carried in ``resistors`` itself.

    Raises :class:`ExtractError` for a deck-authoring mistake (a ``body``/
    ``terminal`` layer that is not one of the deck's own conductor layers),
    since the terminal region must be a layer the connectivity graph already
    carries.
    """
    import klayout.db as kdb

    if not deck.resistors:
        return [], poly, active, metals, kdb.Region(), 0

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
    poly_candidate_bodies = kdb.Region()
    dummy_devices_dropped = 0
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
        if spec.body == deck.poly:
            poly_candidate_bodies += body
        if not dummy.is_empty():
            for component in body.merged().each():
                if (kdb.Region(component) - dummy).is_empty():
                    dummy_devices_dropped += 1
            body = body - dummy
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
        poly_candidate_bodies,
        dummy_devices_dropped,
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


def _diode_terminal_region(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    marker: kdb.Region,
    layer: tuple[int, int] | None,
    requires: tuple[tuple[int, int], ...],
    excludes: tuple[tuple[int, int], ...],
) -> kdb.Region:
    """The recognised region for **one** terminal of a :class:`DiodeDevice`
    entry (issue #542), against this layout.

    ``layer`` is the terminal's declared drawn layer, scoped to the device's
    already-built ``marker`` region -- the same "intersect with the device
    mark before recognition" guard the bipolar block applies to its base, so
    an ordinary PMOS's p+-in-Nwell diffusion is never misrecognised as a
    diode. ``None`` means the terminal is formed by the substrate (the PDK
    draws no mask for it): the region is then the device's ``marker``
    footprint itself, which the caller ties to the deck's ``substrate_net``
    global. That footprint -- rather than an empty region -- is load-bearing:
    ``kdb.DeviceExtractorDiode`` forms the device from the *overlap* of its
    two inputs, so an empty input silently yields no device at all.

    ``requires``/``excludes`` then narrow the result the same way
    :func:`_capacitor_plate_regions` and ``_resolve_resistors`` narrow
    theirs: every ``requires`` layer must also cover the region, every
    ``excludes`` layer is subtracted. For a substrate-formed terminal this
    is how the deck keeps the substrate side genuinely *outside* every well
    (``anode_excludes=(Nwell, DNWELL)``).

    Always returns a freshly-owned ``Region``: both terminals of the same
    entry can derive from the one ``marker`` object, and each is registered
    into the connectivity graph separately.
    """
    # `&`/`-` below already return fresh regions; the `dup()` branch covers
    # the no-declared-layer, no-narrowing case.
    if layer is None:
        region = marker.dup()
    else:
        region = _region(layout, top_cell, layer) & marker
    for required in requires:
        region = region & _region(layout, top_cell, required)
    for excluded in excludes:
        region = region - _region(layout, top_cell, excluded)
    return region


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


def _detect_voltage_domain_overlap(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    deck: ExtractionDeck,
    deck_name: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Flag extracted MOS device geometry (``deck.active``) drawn inside a
    voltage-domain marker layer this deck does not model the scoping of
    (issue #552).

    Mirrors :func:`_detect_unmodelled_poly_bodies`'s "device carries a
    marker this deck doesn't model" shape, but for a *different* gap: not a
    device class this deck fails to recognise at all, but a device class
    (MOS) it recognises and extracts with the *wrong* model. Some decks
    (today, gf180mcu's ``Dualgate`` 55/0, registered via
    :func:`~klayout_tools.decks.get_unmodeled_voltage_markers`) draw a
    marker selecting a second gate-oxide/voltage domain -- e.g. a 5V/6V
    thick-oxide flavour -- with its own correct MOS model, which this
    deck's ``ExtractionDeck.nfet_class``/``pfet_class`` derivation (well
    layer alone) does not read, so a transistor drawn entirely inside the
    marker still extracts bound to the default (e.g. 3.3V) model name with
    no signal that anything is off.

    A marker/description pair is only flagged when the marker's geometry
    actually *interacts* with ``deck.active`` (the MOS device-recognition
    footprint, evaluated for the whole layout, both flavours combined) --
    not merely present somewhere in the stream -- so a ``Dualgate`` shape
    drawn only over, say, an ESD diode this deck's ``DiodeDevice`` entries
    already scope correctly to it produces no false-positive warning here.

    Returns ``(warnings, voltage_domain_warnings)``: ``warnings`` has one
    prose string per flagged marker (empty when this deck registers no
    marker, or none of what it registers overlaps ``deck.active``);
    ``voltage_domain_warnings`` is the matching structured view -- one
    ``{"marker": "<layer>/<datatype>", "description": str}`` entry per
    flagged marker, mirroring ``klt drc``'s
    ``coverage.voltage_domain_warnings`` shape (same registry, same
    description text) so a caller correlating the two commands' output for
    the same layout sees the identical wording. Always a list, empty when
    nothing is flagged.
    """
    from .decks import get_unmodeled_voltage_markers

    unmodeled_markers = get_unmodeled_voltage_markers(deck_name)
    if not unmodeled_markers:
        return [], []

    active = _region(layout, top_cell, deck.active)
    if active.is_empty():
        return [], []

    warnings: list[str] = []
    voltage_domain_warnings: list[dict[str, Any]] = []
    for marker, description in sorted(unmodeled_markers.items()):
        marker_region = _region(layout, top_cell, marker)
        if marker_region.is_empty():
            continue
        if active.interacting(marker_region).is_empty():
            continue
        marker_label = f"{marker[0]}/{marker[1]}"
        warnings.append(
            f"MOS device geometry overlaps the '{marker_label}' "
            f"voltage-domain marker: {description}"
        )
        voltage_domain_warnings.append(
            {"marker": marker_label, "description": description}
        )
    return warnings, voltage_domain_warnings


def _extract_netlist(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    deck: ExtractionDeck,
    parasitics_deck: ParasiticsDeck | None = None,
    top_cell_pins_only: bool = False,
    declared_pins: frozenset[str] | None = None,
    apply_resistor_fixed_offset: bool = True,
    abstract_cell_patterns: tuple[str, ...] = (),
    abstract_instances: list[tuple[int, kdb.ICplxTrans]] | None = None,
    lef_macros: dict[str, tuple[str, dict[str, list[dict[str, Any]]]]] | None = None,
) -> tuple[
    kdb.Netlist,
    list[str],
    list[dict[str, Any]] | None,
    list[dict[str, Any]],
    int,
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Build a flat ``LayoutToNetlist`` connectivity graph for ``deck`` and
    run device + netlist extraction.

    Flat (not hierarchical) extraction, deliberately: every layer is a
    single flattened ``Region``/``Texts`` collection over ``top_cell`` (via
    ``begin_shapes_rec``), the same whole-layout flattening idiom
    ``drc.py`` uses -- see ``docs/cli/extract.md``'s limitation note.

    ``apply_resistor_fixed_offset`` (issue #559): when ``True`` (the
    default -- unchanged behavior), :func:`_apply_device_parameter_corrections`
    adds each opted-in resistor device class's
    :attr:`~klayout_tools.decks.ResistorDevice.fixed_offset_ohm` to every
    extracted primitive here, before ``klt lvs``'s ``options.combine_devices``
    (if requested) folds series-connected primitives into one logical
    device -- KLayout's native ``Netlist.combine_devices()`` then sums each
    primitive's *already-corrected* ``R``, over-counting the fixed offset
    once per primitive instead of once per logical device. Callers that
    combine (``lvs.py``) pass ``False`` here and instead call
    :func:`apply_resistor_fixed_offset_corrections` themselves *after*
    combining, so the correction lands exactly once per surviving device
    object. The capacitor analogue
    (:attr:`~klayout_tools.decks.CapacitorDevice.perim_cap_f_um`) is
    unaffected by this flag -- it is proportional to each device's own
    perimeter, a quantity that itself sums linearly under
    ``combine_devices()``'s parallel-combine parameter summing, so applying
    it once per primitive is already equivalent to applying it once on the
    combined totals; see :func:`_apply_device_parameter_corrections` for the
    algebra.

    Returns ``(netlist, warnings, parasitic_nets, black_box_regions,
    dummy_devices_dropped, unmodelled_poly, abstracted_cells, dead_metal)``.
    ``warnings`` is built from the extractor's own log entries (e.g. a gate
    touching no diffusion) -- non-fatal notes surfaced in the JSON response's
    ``warnings`` field. ``parasitic_nets`` is ``None`` unless
    ``parasitics_deck`` is given, in which case it is the per-net lumped-RC
    data computed from ``LayoutToNetlist.polygons_of_net`` while ``l2n`` is
    still alive (see :func:`_compute_parasitics`). ``black_box_regions`` is
    the JSON response's field (issue #293) -- see
    :func:`_resolve_black_box_regions` -- always a list, empty when the
    layout draws no reserved-annotation-layer geometry.
    ``dummy_devices_dropped`` is the number of devices suppressed by the
    deck's optional ``dummy`` marker layer -- MOS gates (issue #295),
    drawn resistors, and bipolars (both issue #462) alike -- ``0`` when no
    ``dummy`` layer is configured or no dummy geometry is drawn.
    ``unmodelled_poly`` is the JSON response's field (issue #324) -- see
    :func:`_detect_unmodelled_poly_bodies` -- always a list, one entry per
    poly component flagged by the unmodelled-device diagnostic, empty when
    ``warnings`` carries no unmodelled-device entry. ``dead_metal`` is the
    JSON response's field (issue #676) -- see :func:`_detect_dead_metal` --
    one entry per routing-stack cluster that joins no surviving net, empty
    on a layout whose every metal/via shape is netted.

    ``abstract_cell_patterns``/``abstract_instances``/``lef_macros`` (issue
    #620): ``abstract_instances`` is the already-collected
    ``[(cell_index, transform), ...]`` list for every matched instance (see
    :func:`_collect_abstract_instances`) -- by the time this function runs,
    the *caller* (:func:`extract_netlist_from_layout`) has already erased
    each matched cell type's own device-recognition geometry from ``layout``
    in place, so nothing below this point needs to know about the
    abstraction to correctly extract the un-abstracted portion. This
    function's own responsibility is narrower: (1) exclude each abstracted
    instance's in-cell pin labels from the flat ``well_label``/
    ``poly_label``/``metal_labels`` collections (see
    :func:`_texts_excluding_abstract_cells`) -- otherwise an abstracted
    cell's own pin-name label would rename
    whatever top-level net happens to touch it -- and (2) once the flat
    netlist is extracted, wire every abstracted instance in as a black-box
    ``kdb.SubCircuit`` (:func:`_wire_abstract_cells`). ``abstracted_cells``
    is the JSON response's field -- always a list, empty when
    ``abstract_cell_patterns`` is empty (the default).
    """
    import klayout.db as kdb

    active = _region(layout, top_cell, deck.active)
    poly = _region(layout, top_cell, deck.poly)
    nwell = _region(layout, top_cell, deck.nwell)
    tap = _region(layout, top_cell, deck.tap)
    contact = _region(layout, top_cell, deck.contact)

    # A matched instance's own in-cell pin label is that pin's *name*, not a
    # top-level net name -- left in the flat label collection it would
    # rename (or comma-merge into) whatever net the parent's routing happens
    # to touch at that point (issue #620). No-op (same as `_texts`) when
    # `abstract_cell_patterns` is empty, the default.
    def _label_texts(layer: tuple[int, int] | None) -> kdb.Texts:
        if abstract_cell_patterns:
            return _texts_excluding_abstract_cells(
                layout, top_cell, layer, abstract_cell_patterns
            )
        return _texts(layout, top_cell, layer)

    well_label = _label_texts(deck.well_label)
    poly_label = _label_texts(deck.poly_label)
    metals = [_region(layout, top_cell, layer) for layer in deck.metals]
    metal_labels = [_label_texts(layer) for layer in deck.metal_labels]
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

    # Dummy-device marker layer (issue #295, extended to resistors/bipolars
    # in #462): resolved *before* `_resolve_resistors` below so a resistor
    # recognition pass can subtract it from a candidate body the same way
    # the MOS gate-suppression block (further down) already does for
    # `nfet_gate`/`pfet_gate` -- see `_resolve_resistors`'s docstring for the
    # exact contract. `dummy_devices_dropped` accumulates across all three
    # recognition passes (resistor here, MOS and bipolar further below) into
    # a single JSON-response counter.
    dummy = _region(layout, top_cell, deck.dummy)
    dummy_devices_dropped = 0

    # Drawn precision resistors (#222), resolved *before* the MOS split
    # below: a recognised resistor body is cut out of its own conductor
    # layer, so (a) the two heads are no longer shorted through it, and (b)
    # a poly resistor crossing diffusion cannot also be mistaken for a gate
    # -- the same ordering both PDKs' own KLayout LVS decks use (sky130's
    # `tgate = poly.and(diff).not(poly_res)...`).
    (
        resistors,
        poly,
        active,
        metals,
        poly_resistor_candidate_bodies,
        resistor_dummy_dropped,
    ) = _resolve_resistors(layout, top_cell, deck, poly, active, metals, dummy)
    dummy_devices_dropped += resistor_dummy_dropped

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
    # `poly_resistor_bodies` is the union of every *candidate* resistor body
    # `_resolve_resistors` returned above (already narrowed by
    # requires/excludes, but -- unlike `resistors` itself -- **before** any
    # `dummy` cut, issue #462) -- a poly component abutting one of these is
    # that resistor's own terminal head, by construction, so it must never
    # be flagged as a candidate unmodelled-device body (issue #324), even
    # when the resistor itself was fully dummy-suppressed and so carries no
    # surviving entry in `resistors`.
    poly_resistor_markers = kdb.Region()
    for spec in deck.resistors:
        if spec.body == deck.poly:
            poly_resistor_markers += _region(layout, top_cell, spec.marker)
    poly_resistor_bodies = poly_resistor_candidate_bodies
    unmodelled_device_warnings, unmodelled_poly = _detect_unmodelled_poly_bodies(
        poly,
        contact,
        nfet_gate,
        pfet_gate,
        poly_resistor_markers,
        poly_resistor_bodies,
        layout.dbu,
    )

    # Dummy-device suppression, MOS gates (issue #295; extended to resistor
    # and bipolar recognition in #462 -- see the resistor pass above and the
    # bipolar pass below): a deck may declare an optional `dummy` marker
    # layer (see `ExtractionDeck.dummy`) covering drawn-but-non-functional
    # dummy devices -- the matched-pair/array edge fill whose gate and
    # diffusions are tied off to a rail. A MOS gate lying under that marker
    # must not become a device in the extracted netlist (otherwise every
    # dummy is a spurious `device.unmatched` under `klt lvs`), so subtract
    # the marker (already resolved above, before `_resolve_resistors`) from
    # the NMOS/PMOS gate regions *before* device recognition: a gate fully
    # covered by the marker is never handed to `extract_devices` and so is
    # never recognised as a device at all. Only the gate is cut -- the
    # dummy's diffusions (`nfet_sd`/`pfet_sd`) and its gate poly stay in
    # `poly`, so they still participate in ordinary connectivity below and
    # tie off to the rail exactly as drawn.
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
    via_index: list[int] = []
    for index, region in enumerate(vias):
        via_index.append(l2n.register(region, f"via{index}"))
    l2n.register(well_label, "well_label")
    l2n.register(poly_label, "poly_label")
    for index, texts in enumerate(metal_labels):
        l2n.register(texts, f"metal{index}_label")

    # NMOS body (issue #490). `nfet_body` itself stays a permanently empty
    # placeholder `Region` -- deliberately never touched by an ordinary
    # `l2n.connect()` call anywhere below. It is shared, as-is, by every
    # recognised NMOS device's "W" terminal below, by any declared
    # `bulk_to_substrate` resistor's "W" terminal (#222), and by a
    # collector-less bipolar's collector terminal (`bipolar_collector`
    # further down) -- and KLayout's `LayoutToNetlist` does not tolerate an
    # ordinary inter-layer `connect()` declaration against a region that is
    # simultaneously used as a *shared* device-terminal input across more
    # than one recognised device: it was empirically found to corrupt
    # *unrelated* terminals' connectivity (observed: two independent NMOS
    # devices' gate nets, and their otherwise-distinct real tap-ring nets,
    # all collapsing onto one shared node) rather than raising or silently
    # no-op'ing. `connect_global` alone -- never plain `connect()` -- is the
    # only supported way to give this placeholder a net identity.
    #
    # `tap` (already resolved above) serves double duty in this curated
    # deck: a shape drawn *inside* `nwell` is a PMOS well tie (handled
    # below, unchanged), a shape drawn *outside* `nwell` sits on native
    # P-substrate and is a genuine, drawable substrate tie. `tap_substrate`
    # is that outside-the-well slice -- real, possibly-empty geometry (empty
    # exactly when the deck draws no `tap` layer at all, e.g. gf180mcu, or
    # when no tap shape happens to sit outside every `nwell` in this
    # particular layout) -- registered as its own, ordinary (not a device
    # terminal) layer, so it is safe to `connect()` to `contact`/the metal
    # stack the same way the well-tie slice already is (see the
    # `deck.tap is not None` connectivity block below). It is then tied to
    # `nfet_body`'s shared identity purely via `connect_global` using the
    # *same* global name (`deck.substrate_net`) -- `connect_global` unifies
    # every layer/region tied to a given name into one net regardless of
    # geometric overlap between them, so a drawn tap ring's real, routed net
    # and every device sharing the (still-empty) `nfet_body` placeholder
    # land on that one net together, while a layout with no drawn tap ring
    # at all (`tap_substrate` empty) still falls back to exactly the same
    # synthesized `substrate_net` identity as before this fix.
    nfet_body = kdb.Region()
    l2n.register(nfet_body, "nfet_body")
    tap_substrate = tap - nwell
    l2n.register(tap_substrate, "tap_substrate")

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
    #
    # Dummy-device suppression (issue #295, extended to bipolars in #462):
    # `dummy` (resolved above, before `_resolve_resistors`) is subtracted
    # from `bipolar_base` *before* the emitter/collector regions are derived
    # from it -- mirroring the MOS gate-suppression block's "cut before
    # recognition" idiom, and (because both `bipolar_emitter` and
    # `bipolar_collector` are themselves intersections against `bipolar_base`
    # below) the cut propagates to all three terminals for free, so a
    # dummy-covered bipolar unit is dropped as a single connected whole
    # rather than leaving an orphaned emitter/collector fragment behind. A
    # base component fully consumed by the marker is counted into the
    # shared `dummy_devices_dropped` counter; a component only partially
    # covered survives as a clean geometric cut, matching the MOS behaviour.
    bipolar_regions: list[tuple[BipolarDevice, kdb.Region, kdb.Region, kdb.Region]] = []
    for bipolar in deck.bipolars:
        bipolar_base_layer = _region(layout, top_cell, bipolar.base)
        bipolar_marker = _region(layout, top_cell, bipolar.marker)
        bipolar_emitter_layer = _region(layout, top_cell, bipolar.emitter)
        bipolar_base = bipolar_base_layer & bipolar_marker
        if not dummy.is_empty():
            for component in bipolar_base.merged().each():
                if (kdb.Region(component) - dummy).is_empty():
                    dummy_devices_dropped += 1
            bipolar_base = bipolar_base - dummy
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

        bjt_extractor = kdb.DeviceExtractorBJT3Transistor(bipolar.class_name)
        l2n.extract_devices(
            bjt_extractor,
            {"B": bipolar_base, "E": bipolar_emitter, "C": bipolar_collector},
        )
        bipolar_regions.append(
            (bipolar, bipolar_base, bipolar_emitter, bipolar_collector)
        )

    # Junction-diode device recognition (issue #542): each of the deck's
    # optional `diodes` entries (see `DiodeDevice` in `decks/__init__.py`)
    # builds its two terminal regions -- both scoped to the PDK's diode
    # device-mark layer and narrowed by per-terminal implant
    # `requires`/`excludes` -- and hands them to KLayout's native
    # `DeviceExtractorDiode`, which forms the device from their geometric
    # overlap. Without this, a discrete PN/ESD-clamp diode extracts as no
    # device at all (nothing recognises the diffusion-in-well junction), so
    # `klt lvs` cannot verify any diode-based clamp.
    #
    # `diode_regions` carries the built regions through to the connectivity
    # section below (registration/extraction must happen once per entry,
    # before any layer can be used in a `connect()` call), mirroring
    # `bipolar_regions` above.
    diode_regions: list[tuple[DiodeDevice, kdb.Region, kdb.Region]] = []
    for diode in deck.diodes:
        # Deck-authoring validation, checked unconditionally (like the
        # capacitor block's own `top_plate_via` pairing check) so a mistake
        # in a deck module surfaces even on a diode-free layout: a diode
        # with *no* drawn terminal at all has nothing to overlap and would
        # silently extract nothing.
        if diode.anode is None and diode.cathode is None:
            raise ExtractError(
                f"diode '{diode.name}': at most one of anode/cathode may be "
                "None (the substrate-formed terminal) -- a diode with neither "
                "terminal drawn cannot be recognised"
            )

        diode_marker = _region(layout, top_cell, diode.marker)
        anode_region = _diode_terminal_region(
            layout,
            top_cell,
            diode_marker,
            diode.anode,
            diode.anode_requires,
            diode.anode_excludes,
        )
        cathode_region = _diode_terminal_region(
            layout,
            top_cell,
            diode_marker,
            diode.cathode,
            diode.cathode_requires,
            diode.cathode_excludes,
        )

        # Dummy-device suppression (issue #295, extended to resistors and
        # bipolars in #462, to diodes here): count and cut whole recognised
        # junctions covered by the deck's `dummy` marker. Counting is done
        # against the *recognised junction* (the anode/cathode overlap --
        # exactly what `DeviceExtractorDiode` turns into a device) rather
        # than against the raw marker layer, because a deck may declare
        # several diode flavours sharing one device-mark layer (gf180mcu
        # declares two on `diode_mk`); counting marker components would
        # then charge the same dummy device once per declared flavour.
        # A junction only partially covered survives as a clean geometric
        # cut, matching the MOS/bipolar behaviour.
        if not dummy.is_empty():
            for component in (anode_region & cathode_region).merged().each():
                if (kdb.Region(component) - dummy).is_empty():
                    dummy_devices_dropped += 1
            anode_region = anode_region - dummy
            cathode_region = cathode_region - dummy

        if anode_region.is_empty() or cathode_region.is_empty():
            # No diode marker (or no matching implant geometry) drawn
            # anywhere on this layout -- the common case. Registering and
            # extracting empty regions would be a no-op anyway, but skipping
            # keeps a diode-free layout's extraction bit-for-bit what it was
            # before this feature existed, the same guard the capacitor
            # block below applies.
            continue

        l2n.register(anode_region, f"{diode.name}_anode")
        l2n.register(cathode_region, f"{diode.name}_cathode")
        l2n.extract_devices(
            kdb.DeviceExtractorDiode(diode.name),
            {"P": anode_region, "N": cathode_region},
        )
        diode_regions.append((diode, anode_region, cathode_region))

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
        # Substrate-tie slice of `tap` (issue #490): `tap_substrate` is a
        # *different*, ordinary (non-device-terminal) registered layer than
        # `tap` above, even though its geometry -- where present -- is a
        # literal subset of it, so it needs its own `contact` connection to
        # join the same metal-routed net a tap ring's shapes reach via
        # `tap`/`contact` above. Safe to `connect()` normally here (unlike
        # `nfet_body` above): `tap_substrate` is never passed to
        # `extract_devices` as a terminal.
        l2n.connect(tap_substrate, contact)
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
    # `connect_global` with the *same* name on a second, ordinary (non-
    # device-terminal) layer merges it into the identical net (see the
    # `nfet_body`/`tap_substrate` docstring above) -- the only supported way
    # to give a drawn substrate-tap ring's real net the same identity as
    # every device's `nfet_body` terminal.
    l2n.connect_global(tap_substrate, deck.substrate_net)

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

    # Diode terminal connectivity (issue #542), derived from each terminal's
    # declared layer rather than configured separately per entry -- see
    # `DiodeDevice`'s docstring:
    #
    # - a terminal drawn on the deck's own `nwell` shares that well's net
    #   identity (its region is always a marker-scoped subset of `nwell`),
    #   so a well tap/label elsewhere in this function names it -- the same
    #   wiring the bipolar base gets above;
    # - a substrate-formed terminal (declared `None`) joins the deck's
    #   `substrate_net` global, like the collector-less bipolar collector
    #   and `nfet_body`;
    # - any other terminal layer is a diffusion, so it joins `contact` and
    #   picks its net up from ordinary contact/metal routing -- the same
    #   wiring the bipolar emitter gets above.
    for diode, anode_region, cathode_region in diode_regions:
        for terminal_layer, terminal_region in (
            (diode.anode, anode_region),
            (diode.cathode, cathode_region),
        ):
            if terminal_layer is None:
                l2n.connect_global(terminal_region, deck.substrate_net)
            elif terminal_layer == deck.nwell:
                l2n.connect(terminal_region, nwell)
            else:
                l2n.connect(terminal_region, contact)

    try:
        l2n.extract_netlist()
    except RuntimeError as exc:
        # KLayout's own `LayoutToNetlist.extract_netlist()` raises a bare
        # `RuntimeError` (not one of *this* module's own exception types) for
        # a device whose recognised geometry leaves a terminal with no net at
        # all -- e.g. a bipolar device-mark drawn exactly coincident with its
        # emitter (`base == emitter` geometrically): `DeviceExtractorBJT3Transistor`
        # then has no base-minus-emitter extension from which to derive a
        # collector terminal, so the `bipolar.collector is None` branch above
        # connects an empty region to `deck.substrate_net` -- nothing to
        # connect, so the extracted device's `C` terminal reaches this point
        # still unconnected (issue #432). Converted to `ExtractError` here so
        # it reaches the CLI as the documented clean JSON error envelope
        # (`docs/cli/extract.md`'s "No Python traceback is printed" contract)
        # instead of an unhandled traceback.
        raise ExtractError(
            "device recognition produced a device with an unconnected "
            f"terminal ({exc}) -- this usually means a device-mark layer was "
            "drawn exactly coincident with (rather than strictly enclosing) "
            "the terminal geometry it scopes, leaving no room to derive the "
            "device's other terminal(s)"
        ) from exc
    netlist = l2n.netlist()

    # `--abstract-cells` (issue #620): wire every abstracted instance in as a
    # black-box `kdb.SubCircuit` while `l2n`/`netlist` are still the *live*
    # objects `l2n.probe_net()` and `Circuit.create_subcircuit()` need, and
    # *before* `make_top_level_pins()`/the purge passes below -- connecting a
    # subcircuit pin to a net is what keeps an abstracted cell's own routing
    # stub from being purged as floating. See `_wire_abstract_cells`'s
    # docstring for the full contract.
    abstracted_cells: list[dict[str, Any]] = []
    if abstract_instances:
        top_circuit = netlist.circuit_by_name(top_cell.name)
        assert top_circuit is not None, (
            "top circuit must exist immediately after l2n.extract_netlist()"
        )
        # Metals bottom-up first, then poly/nwell/tap -- matches
        # `_probe_abstract_pin_net`'s own documented fallback order (a
        # standard cell's pins land on the lowest metal available). Getting
        # this backwards is a confirmed correctness bug (PR #622 review): a
        # parent-level well/tap shape (e.g. a guard ring) overlapping a
        # LEF-fallback pin's footprint would silently win over the metal net
        # the pin is actually routed to, since `_probe_abstract_pin_net`
        # takes the first hit. The abstracted cell's *own* nwell/poly/tap
        # cannot cause this -- `_abstract_cell_mask_layers` erases those
        # inside its definition before probing runs -- so the exposure is
        # specifically parent-level geometry.
        probe_layers: list[tuple[str, kdb.Region]] = [
            (f"metal{index}", region) for index, region in enumerate(metals)
        ] + [("poly", poly), ("nwell", nwell), ("tap", tap)]
        abstracted_cells, abstract_cell_warnings = _wire_abstract_cells(
            layout,
            deck,
            l2n,
            netlist,
            top_circuit,
            abstract_instances,
            lef_macros or {},
            probe_layers,
        )
        warnings = warnings + abstract_cell_warnings

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
    _promote_orphan_named_nets(netlist)
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

    # Issue #514: a per-*net* declared-interface reconciliation, orthogonal
    # to the per-*cell* one above. When `declared_pins` is given, demote
    # every currently-promoted pin whose net name is not in the declared
    # set -- the net keeps its name (a human/testbench can still find it),
    # it is simply not exposed as a top-level pin `combine_devices` must
    # treat as un-foldable. Applied *after* the top_cell_pins_only pass, so
    # it can only further restrict the promoted set, never re-promote a net
    # that pass already kept internal. Reuses `_reconcile_top_pins` exactly
    # as top_cell_pins_only does, with a different demote-set.
    if declared_pins is not None:
        top_circuit = netlist.circuit_by_name(top_cell.name)
        promoted_names: set[str] = set()
        if top_circuit is not None:
            for pin in top_circuit.each_pin():
                pin_net = top_circuit.net_for_pin(pin.id())
                if pin_net is not None and pin_net.name:
                    promoted_names.add(pin_net.name)

        non_declared = promoted_names - declared_pins
        demoted_by_declared_pins = _reconcile_top_pins(
            netlist, top_cell.name, non_declared, demote=True
        )
        if demoted_by_declared_pins:
            joined = ", ".join(demoted_by_declared_pins)
            warnings.append(
                f"kept {len(demoted_by_declared_pins)} net(s) internal: not in "
                f"the declared pin set (--pins / layout.declared_pins) "
                f"({joined}) -- issue #514"
            )

        unmatched_declared_pins = sorted(declared_pins - promoted_names)
        if unmatched_declared_pins:
            joined = ", ".join(unmatched_declared_pins)
            count = len(unmatched_declared_pins)
            plural = "s" if count != 1 else ""
            warnings.append(
                f"{count} declared pin name{plural} (--pins / "
                f"layout.declared_pins) matched no promoted net in the "
                f"layout: {joined}"
            )

    # `Netlist.purge()` (used by `_purge_preserving_named_nets` below) judges
    # a net "floating" -- and, transitively, a whole circuit/subcircuit chain
    # "unused" -- against whether it is (indirectly) connected to a real
    # `kdb.Device`, *not* against its own `pin_count()`/`subcircuit_pin_count()`
    # (verified directly against `klayout.db`: a named, pinned net whose only
    # connections are a top-level pin and a `SubCircuit` pin into a
    # device-free circuit is still wiped, along with that circuit and the
    # `SubCircuit` instance itself, exactly as if none of them had ever been
    # connected). A black-box abstraction (issue #620) is *by definition*
    # device-free, so `_purge_preserving_named_nets`'s existing rescue --
    # which only guards *individual* named/pinned nets against `purge()`,
    # not whole subcircuit chains -- is not enough once `--abstract-cells`
    # is in play: every abstracted instance (and the parent nets it is wired
    # to) would otherwise silently vanish, defeating the whole feature.
    # `abstract_instances` truthy therefore skips KLayout's native
    # `purge()`/`_purge_preserving_named_nets` entirely in favour of
    # :func:`_purge_truly_floating_nets`, a narrower, purely net-local pass
    # that removes only what is *unconditionally* junk (no pin, no device
    # terminal, no subcircuit pin -- on any circuit) and never touches a
    # circuit or subcircuit instance. The one accepted trade-off: a
    # genuinely-disconnected, unnamed junk net that `purge()` would normally
    # remove via its device-anchored definition survives when
    # `--abstract-cells` is given (it still shows up in `nets[]` with
    # `device_count: 0`) -- cosmetic noise, not a correctness gap.
    if abstract_instances:
        _purge_truly_floating_nets(netlist)
    else:
        _purge_preserving_named_nets(netlist)

    # Post-extraction device-parameter corrections (issues #512, #518, #521):
    # applied to the live `kdb.Device` objects here -- *before* the netlist is
    # handed to `NetlistSpiceWriter` (`run_extract`) or `NetlistComparer`
    # (`lvs.py`'s inline-extraction path) -- so every consumer sees the same
    # corrected value the JSON `devices[].params` report shows. See
    # `_apply_device_parameter_corrections` for the full rationale.
    #
    # The resistor `fixed_offset_ohm` term is gated by
    # `apply_resistor_fixed_offset` (issue #559): `lvs.py`'s
    # `options.combine_devices` path passes `False` here and applies it
    # itself, once, *after* combining -- see this function's docstring and
    # `apply_resistor_fixed_offset_corrections`.
    _apply_device_parameter_corrections(
        netlist, deck, apply_resistor_fixed_offset=apply_resistor_fixed_offset
    )

    # Dead metal (issue #676): routing-stack geometry left on no surviving
    # net. Like the parasitics pass below, this must run *here* -- after the
    # purge (so "the nets a caller actually sees" is the yardstick) but while
    # `l2n` still owns the shape database `polygons_of_net` reads.
    dead_metal = _detect_dead_metal(
        l2n,
        netlist.circuit_by_name(top_cell.name),
        layout,
        top_cell,
        layout.dbu,
        [
            (f"metal{index}", deck.metals[index], region, metal_index[index])
            for index, region in enumerate(metals)
        ]
        + [
            (f"via{index}", deck.vias[index], region, via_index[index])
            for index, region in enumerate(vias)
        ],
    )
    if dead_metal:
        cluster_word = "cluster" if len(dead_metal) == 1 else "clusters"
        total_shapes = sum(entry["shapes"] for entry in dead_metal)
        shape_word = "shape" if total_shapes == 1 else "shapes"
        layers_str = ", ".join(
            sorted({f"{entry['layer']}/{entry['datatype']}" for entry in dead_metal})
        )
        warnings.append(
            f"{len(dead_metal)} routing-stack {cluster_word} ({total_shapes} "
            f"{shape_word} on {layers_str}) join no extracted net -- no via "
            "lands on this geometry and no same-layer wiring touches it, so "
            "it is invisible to the extracted netlist and to every downstream "
            "`klt lvs`/`klt sim` view; deliberate dead metal (artwork, fill) "
            "is expected here, unexplained dead metal usually means the "
            "connection you intended is missing -- see dead_metal[] for the "
            "per-cluster layer/bbox/shape count."
        )

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
        abstracted_cells,
        dead_metal,
    )


def _parameter_id(device_class: kdb.DeviceClass, name: str) -> int | None:
    """Return ``device_class``'s parameter id for ``name``, or ``None``.

    KLayout's ``Device.parameter(name)``/``set_parameter(name, value)``
    string overloads *raise* for a parameter the class does not define, so
    every read/write below is guarded by this lookup instead: a
    ``DeviceClassMOS4Transistor`` has no ``R``, a ``DeviceClassResistor`` has
    no ``P``, and both must be silently skipped rather than blow up
    extraction.
    """
    for param in device_class.parameter_definitions():
        if param.name == name:
            return param.id()
    return None


def apply_resistor_fixed_offset_corrections(
    netlist: kdb.Netlist, deck: ExtractionDeck
) -> None:
    """Add each opted-in resistor device class's
    :attr:`~klayout_tools.decks.ResistorDevice.fixed_offset_ohm` to ``R`` --
    once per ``kdb.Device`` object currently in ``netlist`` (issue #518,
    #559).

    Public (no leading underscore): shared between
    :func:`_apply_device_parameter_corrections`'s default apply-at-
    extraction-time call (``_extract_netlist``, when
    ``apply_resistor_fixed_offset=True``) and ``klt lvs``'s
    ``options.combine_devices`` path (``lvs.py``), which instead passes
    ``apply_resistor_fixed_offset=False`` to ``_extract_netlist`` and calls
    this function itself *after* ``Netlist.combine_devices()`` has folded
    series-connected primitives into one device object. Because this
    function walks whatever devices exist in ``netlist`` *at the time it
    runs*, calling it post-combine adds the fixed offset exactly once per
    surviving (possibly-folded) logical device, regardless of how many
    drawn primitives fed into it -- fixing the over-count KLayout's native
    series fold otherwise produces by summing each primitive's
    already-corrected ``R`` (issue #559).

    A deck that has not opted in (the default ``fixed_offset_ohm=0.0``) gets
    no write at all. Keyed by device-class *name* (``ResistorDevice.name``
    is the string KLayout reports back as ``DeviceClass.name``), so this is a
    direct lookup, not a positional match. The lookup is **case-insensitive**
    (issue #585): the in-process ``kdb.Netlist`` an inline extraction builds
    reports the deck's name verbatim (lowercase, e.g. ``res_high_po``), but a
    netlist read back from a SPICE file via ``kdb.NetlistSpiceReader`` (the
    ``layout.netlist`` pre-extracted shape in ``lvs.py``) reports every
    device-class name **uppercased** (``RES_HIGH_PO``). A verbatim lookup
    would silently miss the correction for the pre-extracted shape -- no
    error, no warning. Normalizing both sides to lowercase makes the
    post-combine correction fire identically regardless of how the netlist
    was produced. Parasitic R devices (injected by ``run_extract`` *after*
    extraction returns) carry their own generated class names and are never
    reached by this function -- no double-application.
    """
    fixed_offset_lookup = {
        resistor.name.lower(): resistor.fixed_offset_ohm
        for resistor in deck.resistors
        if resistor.fixed_offset_ohm
    }
    if not fixed_offset_lookup:
        return

    for circuit in netlist.each_circuit():
        for device in circuit.each_device():
            device_class = device.device_class()
            fixed_offset_ohm = fixed_offset_lookup.get(device_class.name.lower())
            if fixed_offset_ohm:
                r_id = _parameter_id(device_class, "R")
                if r_id is not None:
                    device.set_parameter(
                        r_id, device.parameter(r_id) + fixed_offset_ohm
                    )


def _apply_device_parameter_corrections(
    netlist: kdb.Netlist,
    deck: ExtractionDeck,
    *,
    apply_resistor_fixed_offset: bool = True,
) -> None:
    """Apply the deck's post-extraction device-parameter corrections to the
    live ``kdb.Device`` objects in ``netlist`` (issue #521).

    KLayout's device extractors compute only the single-term forms:
    ``DeviceExtractorCapacitor`` gives ``C = area_cap_f_um2 * A`` and
    ``DeviceExtractorResistor``/``...ResistorWithBulk`` gives
    ``R = L / W * sheet_rho_ohm_sq``. Two deck fields refine those into the
    two-term forms the real PDK models use:

    * :attr:`~klayout_tools.decks.CapacitorDevice.perim_cap_f_um` (issue
      #512) adds the perimeter/fringe term, so ``C`` becomes
      ``area_cap_f_um2 * A + perim_cap_f_um * P``.
    * :attr:`~klayout_tools.decks.ResistorDevice.fixed_offset_ohm` (issue
      #518) adds the fixed head/end-effect term, so ``R`` becomes
      ``L / W * sheet_rho_ohm_sq + fixed_offset_ohm`` -- applied here via
      :func:`apply_resistor_fixed_offset_corrections`, gated by
      ``apply_resistor_fixed_offset`` (issue #559, see that function's
      docstring for why the capacitor correction below needs no equivalent
      gate).

    Both corrections originally lived in :func:`_describe_devices`, which
    builds only the JSON response's ``devices[]`` array -- so the correction
    reached the report but never the ``kdb.Netlist`` itself (issue #521).
    That left the two consumers that actually matter reading the raw
    single-term value: ``run_extract``'s ``NetlistSpiceWriter`` (and
    therefore ``klt sim``, which consumes the written ``.spice``) and
    ``klt lvs``'s inline-extraction path, whose ``kdb.NetlistComparer``
    reads ``device.parameter(...)`` directly and so reported a spurious
    parameter mismatch against a reference netlist built from the PDK's real
    two-term model.

    Correcting the device object here -- once, inside
    :func:`_extract_netlist`, after ``netlist.purge()`` and before the
    netlist is returned to either consumer -- makes every downstream reader
    agree. :func:`_describe_devices` now simply reads the corrected value
    back rather than recomputing the correction itself, so the JSON
    ``devices[].params`` output is unchanged.

    A deck that has not opted in (the default ``perim_cap_f_um=0.0`` /
    ``fixed_offset_ohm=0.0`` both features were designed around) gets no
    write at all -- the netlist, the written SPICE, and the LVS comparison
    all stay bit-for-bit what they were before this correction existed.

    Corrections are keyed by device-class *name*: ``CapacitorDevice.name`` /
    ``ResistorDevice.name`` are the same strings KLayout reports back as
    ``DeviceClass.name``, so this is a direct lookup, not a positional
    match. Parasitic R/C devices (injected by ``run_extract`` *after*
    extraction returns) carry their own generated class names and are never
    reached by this function -- no double-application.

    Why ``perim_cap_f_um`` needs no ``apply_resistor_fixed_offset``-style
    gate (issue #559 asked this question of the capacitor analogue):
    KLayout's ``combine_devices()`` combines capacitors in *parallel*
    (matching two-terminal nets) by directly summing each device's raw
    parameters -- ``C``, ``A``, *and* ``P`` are each simple per-device sums
    (``dbNetlistDeviceClasses.cc``'s ``CapacitorDeviceCombiner::parallel``).
    Because ``perim_cap_f_um`` scales *with* the per-device geometric
    quantity ``P`` (unlike the resistor's constant ``fixed_offset_ohm``),
    applying it once per primitive and then summing is algebraically
    identical to summing the raw primitives first and applying it once to
    the combined totals: ``sum_i(area_i + perim_cap_f_um * P_i) ==
    sum_i(area_i) + perim_cap_f_um * sum_i(P_i)``. So the parallel-combine
    case this repo's decks actually produce (matched capacitor arrays) is
    unaffected by extraction-time application, and needs no deferral. (A
    *series*-combined capacitor pair -- rare, and not produced by any deck
    in this repo -- combines ``C`` non-linearly (harmonic mean) while still
    summing ``A``/``P`` linearly; that mismatch is a pre-existing
    approximation in KLayout's own multi-term series-capacitor combine,
    unrelated to and unaffected by whether this correction runs before or
    after combining, so it is out of scope here.)
    """
    perim_cap_lookup = {
        capacitor.name: capacitor.perim_cap_f_um
        for capacitor in deck.capacitors
        if capacitor.perim_cap_f_um
    }
    if perim_cap_lookup:
        for circuit in netlist.each_circuit():
            for device in circuit.each_device():
                device_class = device.device_class()
                perim_cap_f_um = perim_cap_lookup.get(device_class.name)
                if perim_cap_f_um:
                    c_id = _parameter_id(device_class, "C")
                    p_id = _parameter_id(device_class, "P")
                    if c_id is not None and p_id is not None:
                        device.set_parameter(
                            c_id,
                            device.parameter(c_id)
                            + perim_cap_f_um * device.parameter(p_id),
                        )

    if apply_resistor_fixed_offset:
        apply_resistor_fixed_offset_corrections(netlist, deck)


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
        if net.cluster_id == 0:
            # Belt-and-braces (issue #563): `cluster_id` is the key
            # `LayoutToNetlist` uses to find a net's shapes, and `0` is the
            # sentinel for "not tied to a layout cluster". Passing such a net
            # to `polygons_of_net` faults inside KLayout's hierarchical
            # network processor with an unhandled internal `RuntimeError`
            # rather than returning an empty region.
            # `_purge_preserving_named_nets` restores the real cluster id on
            # every net it rescues, so no net reaching here from
            # `_extract_netlist` should hit this branch -- but a net with no
            # cluster has no geometry to measure by definition, so skipping
            # it is the correct (and crash-free) answer for any future caller
            # that hands us a synthesised net.
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
                "net": spice_safe_net_name(name),
                "resistance_ohm": round(r_ohm, 4),
                "capacitance_ff": round(c_ff, 6),
            }
        )

    results.sort(key=lambda entry: entry["net"])
    return results


def _detect_dead_metal(
    l2n: kdb.LayoutToNetlist,
    circuit: kdb.Circuit | None,
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    dbu: float,
    routing_layers: list[tuple[str, tuple[int, int], kdb.Region, int]],
) -> list[dict[str, Any]]:
    """Report every routing-stack cluster that joins no extracted net (issue
    #676) -- "dead metal".

    ``routing_layers`` is one ``(role, (layer, datatype), region, index)``
    tuple per registered ``metals``/``vias`` level: ``region`` is the exact
    :class:`kdb.Region` handed to ``LayoutToNetlist.register`` (so black-box
    masking and the MiM top-via exclusion are already applied to it) and
    ``index`` is that call's returned layer index. For each level, the union
    of ``polygons_of_net(net, index)`` over every surviving net is what the
    extraction *did* account for; whatever is left after subtracting it is
    geometry no ``nets[]`` entry mentions.

    **Electrical contact, not projection.** The netted union comes from the
    extracted connectivity graph, which joins two metal levels only through a
    declared via layer -- so a wire passing under or over another wire with no
    via between them stays two nets, and an isolated shape stays dead however
    much it overlaps a live one in XY. That is the distinction a naive
    ``Region.interacting()`` check across adjacent layers gets wrong.

    **Why the surviving netlist is the yardstick.** KLayout gives *every*
    connected cluster a net at ``extract_netlist()`` time, floating ones
    included, so "has a net" is trivially true before the purge and this
    function must run after it. A floating cluster that reaches no device is
    exactly what ``Netlist.purge()`` drops, and a caller reading ``nets[]``
    can no longer find its geometry anywhere -- which is the reported
    complaint. A *labelled* floating cluster (bond pad, seal ring, power
    strap) is rescued by :func:`_purge_preserving_named_nets` with its
    ``cluster_id`` intact, so it stays netted and is deliberately **not**
    reported: a named net is findable, whatever it does or does not touch.

    Returns one entry per connected dead cluster (not per drawn polygon):
    ``{"role", "layer", "datatype", "bbox_um", "shapes", "area_um2"}``, sorted
    by ``(layer, datatype, left, bottom)``. ``role`` is ``metal<i>``/``via<i>``
    with ``<i>`` indexing the deck's own ``metals``/``vias`` tuple (``0`` is
    the bottom-most level), matching the names ``_extract_netlist`` registers
    those layers under. ``shapes`` counts the *drawn* shapes on that stream
    layer interacting with the cluster, so a human knows how much geometry to
    go look at; a cluster that is only a fragment of a drawn shape (the part
    of it left outside a black-box region, say) still counts that whole shape.
    """
    import klayout.db as kdb

    nets = (
        [net for net in circuit.each_net() if net.cluster_id != 0]
        if circuit is not None
        else []
    )

    dead_metal: list[dict[str, Any]] = []
    for role, (layer, datatype), region, index in routing_layers:
        if region.is_empty():
            continue
        netted = kdb.Region()
        for net in nets:
            netted += l2n.polygons_of_net(net, index)
        dead = region - netted
        if dead.is_empty():
            continue
        drawn = _region(layout, top_cell, (layer, datatype))
        # Raw-polygon semantics: with KLayout's default merged semantics two
        # abutting drawn boxes count as one polygon, which would report
        # "1 shape" for a cluster a human has to go edit two shapes to fix.
        drawn.merged_semantics = False
        for component in dead.merged().each():
            cluster = kdb.Region(component)
            box = component.bbox()
            dead_metal.append(
                {
                    "role": role,
                    "layer": layer,
                    "datatype": datatype,
                    "bbox_um": {
                        "left": round(box.left * dbu, _PARAM_PRECISION_UM),
                        "bottom": round(box.bottom * dbu, _PARAM_PRECISION_UM),
                        "right": round(box.right * dbu, _PARAM_PRECISION_UM),
                        "top": round(box.top * dbu, _PARAM_PRECISION_UM),
                    },
                    "shapes": drawn.interacting(cluster).count(),
                    "area_um2": round(cluster.area() * dbu * dbu, _PARAM_PRECISION_UM),
                }
            )

    dead_metal.sort(
        key=lambda entry: (
            entry["layer"],
            entry["datatype"],
            entry["bbox_um"]["left"],
            entry["bbox_um"]["bottom"],
        )
    )
    return dead_metal


def _terminal_star_positions_um(terminal_refs: list[Any]) -> list[tuple[float, float]]:
    """The approximate ``(x_um, y_um)`` location of each of ``terminal_refs``,
    read from its owning device's ``Device.trans`` (already reported in real
    micrometres -- unlike most of this module, no ``dbu`` scaling is needed).

    This is a **coarse** proxy for a terminal's true physical location
    (issue #592): KLayout's connectivity extraction records one placement
    transform per *device* (typically the center of its recognition shape),
    not a distinct position per terminal, so every terminal on the same
    device instance shares that device's single location. Good enough to
    rank a net's terminals by their approximate spread for the star-topology
    resistance split; not a substitute for true per-segment routing
    measurement (out of scope -- issue #592's deferred Option 2)."""
    positions: list[tuple[float, float]] = []
    for ref in terminal_refs:
        disp = ref.device().trans.disp
        positions.append((disp.x, disp.y))
    return positions


def _terminal_star_weights(positions: list[tuple[float, float]]) -> list[float]:
    """Normalized (sum to ``1.0``) per-terminal resistance-split weights for
    the star topology: proportional to each position's Euclidean distance
    from the centroid of ``positions``, so a terminal placed farther from a
    net's other terminals is assigned more of that net's total resistance --
    a coarse, position-aware stand-in for "farther terminals see more
    interconnect" without a full per-segment routing model (issue #592).

    Degenerates to an equal ``1/N`` split when every position coincides
    (including ``N == 1``, where the lone terminal necessarily sits at the
    "centroid") -- this is what makes a single-terminal net's star reduce to
    exactly the pre-#592 Gamma-shunt's one lumped resistor: same total value,
    not a smaller or larger one."""
    n = len(positions)
    if n == 0:
        return []
    cx = sum(p[0] for p in positions) / n
    cy = sum(p[1] for p in positions) / n
    distances = [math.hypot(x - cx, y - cy) for x, y in positions]
    total = sum(distances)
    if total <= 0.0:
        return [1.0 / n] * n
    return [d / total for d in distances]


def _inject_parasitics(
    kdb: Any,
    circuit: kdb.Circuit,
    parasitic_nets: list[dict[str, Any]],
    ground_net_name: str,
) -> dict[str, Any]:
    """Inject a star-topology parasitic RC per net into ``circuit`` and
    return the JSON ``parasitics`` summary block (issue #592).

    For each entry, the net itself becomes the star's **hub**. Every device
    terminal that was connected directly to the net is moved onto a fresh
    per-terminal "leg" net, and a series resistor bridges each leg back to
    the hub -- so two terminals on the same net now sit in series through
    two resistors (``leg_a --R--> hub --R--> leg_b``), instead of sharing one
    node with no resistance between them (the pre-#592 topology this
    replaces). A single capacitor still hangs the net's total lumped ground
    capacitance off the hub (``hub --C--> <substrate_net>``), created if
    absent. Each leg's resistance is the net's total computed resistance
    distributed across its terminals by
    :func:`_terminal_star_weights` -- a terminal farther from the net's
    other connections gets more of the total, and the weights always sum to
    ``1.0`` so a net's leg resistances sum back to its total. A net with no
    device terminal at all (real geometry with nothing electrically
    attached) falls back to exactly the pre-#592 Gamma-shunt: one resistor
    from the net to a fresh internal node, with the capacitor on that node.

    This is purely additive from the perspective of the schematic-equivalent
    view built *before* this call (`devices[]`/`nets[]`, see `run_extract`):
    no existing device instance is removed, no pin is touched, and the
    written SPICE stays a `.SUBCKT` body directly consumable by ``klt sim``
    (every new node is internal; the subcircuit's pin interface is
    untouched). It is *not* additive to the circuit object's own internal
    wiring the way the old shunt topology was: moving a device terminal onto
    a leg net changes which SPICE node that device's `R`/`C`/`M` card names,
    even though the electrical net it represents -- and everything
    `devices[]`/`nets[]` reports about it -- is unchanged.

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

    # Keyed by `spice_safe_net_name(expanded_name())` rather than looked up
    # via `net_by_name()` per entry: `net_by_name()` only resolves *named*
    # nets (an explicit layout label), so it silently returns `None` -- and
    # drops the entry -- for every genuinely internal/unlabelled net, whose
    # `expanded_name()` is KLayout's auto-generated `$<n>` form rather than a
    # real `.name`. Issue #283: `_compute_parasitics` already measures these
    # nets correctly (the geometry is there), so this lookup must resolve
    # them too or the R/C it computed for them is discarded right here. The
    # `spice_safe_net_name` wrap (issue #696) matches `_compute_parasitics`'
    # own conversion of `entry["net"]`, so a merged-label net's `|`-joined
    # key here actually matches instead of silently missing.
    nets_by_name = {
        spice_safe_net_name(net.expanded_name()): net for net in circuit.each_net()
    }
    existing_names = set(nets_by_name)

    report_nets: list[dict[str, Any]] = []
    total_r = 0.0
    total_c_ff = 0.0
    total_r_count = 0
    for entry in parasitic_nets:
        net = nets_by_name.get(entry["net"])
        if net is None:
            continue

        instance_name = _sanitize_instance_name(entry["net"])
        r_total_ohm = max(entry["resistance_ohm"], _MIN_PARASITIC_R_OHM)
        c_farad = entry["capacitance_ff"] * 1e-15

        # Snapshot before mutating: moving a terminal off `net` below changes
        # what `net.each_terminal()` would yield mid-iteration.
        terminal_refs = list(net.each_terminal())

        terminal_reports: list[dict[str, Any]] = []
        if terminal_refs:
            hub = net
            hub_name = entry["net"]
            positions = _terminal_star_positions_um(terminal_refs)
            weights = _terminal_star_weights(positions)
            for i, (term_ref, weight) in enumerate(
                zip(terminal_refs, weights, strict=True)
            ):
                leg_name = _unique_net_name(
                    entry["net"], existing_names, suffix=f"__t{i}"
                )
                existing_names.add(leg_name)
                leg = circuit.create_net(leg_name)

                device = term_ref.device()
                terminal_def = term_ref.terminal_def()
                device.disconnect_terminal(terminal_def.id())
                device.connect_terminal(terminal_def.id(), leg)

                leg_r_ohm = max(r_total_ohm * weight, _MIN_PARASITIC_R_OHM)
                r_dev = circuit.create_device(res_class, f"{instance_name}_t{i}")
                r_dev.connect_terminal("A", leg)
                r_dev.connect_terminal("B", hub)
                r_dev.set_parameter("R", leg_r_ohm)
                total_r_count += 1

                terminal_reports.append(
                    {
                        "device": device.expanded_name(),
                        "terminal": terminal_def.name,
                        "leg_net": leg_name,
                        "resistance_ohm": round(leg_r_ohm, 4),
                    }
                )
        else:
            # No device terminal to fan a star out to (e.g. real routed
            # geometry with nothing electrically attached) -- fall back to
            # the pre-#592 Gamma-shunt so the net's capacitance still has
            # somewhere to attach.
            hub_name = _unique_net_name(entry["net"], existing_names)
            existing_names.add(hub_name)
            hub = circuit.create_net(hub_name)

            r_dev = circuit.create_device(res_class, instance_name)
            r_dev.connect_terminal("A", net)
            r_dev.connect_terminal("B", hub)
            r_dev.set_parameter("R", r_total_ohm)
            total_r_count += 1

        c_dev = circuit.create_device(cap_class, instance_name)
        c_dev.connect_terminal("A", hub)
        c_dev.connect_terminal("B", ground)
        c_dev.set_parameter("C", c_farad)

        total_r += r_total_ohm
        total_c_ff += entry["capacitance_ff"]
        report_nets.append(
            {
                "net": entry["net"],
                "resistance_ohm": entry["resistance_ohm"],
                "capacitance_ff": entry["capacitance_ff"],
                "hub_net": hub_name,
                "terminals": terminal_reports,
            }
        )

    return {
        "r_count": total_r_count,
        "c_count": len(report_nets),
        "total_resistance_ohm": round(total_r, 4),
        "total_capacitance_ff": round(total_c_ff, 6),
        "nets": report_nets,
    }


def spice_safe_net_name(name: str) -> str:
    """Rewrite a KLayout ``Net.expanded_name()`` string to the exact spelling
    KLayout's own ``NetlistSpiceWriter`` writes for that net's *node*
    references in the ``.SUBCKT``/instance lines of the written SPICE file
    (issue #696).

    ``Net.expanded_name()`` joins every distinct text label found on one
    electrical net with ``,`` (see :func:`_detect_merged_net_labels`'s
    docstring, issue #470) -- but a SPICE node token cannot contain a comma
    (a common argument separator), so ``NetlistSpiceWriter`` writes the
    *same* joined net using ``|`` instead wherever it appears as an actual
    node reference (only its leading ``* pin ...``/``* net ...`` comments
    keep the comma-joined form). Before this function existed, every net
    name this module put into the JSON response (``nets[].name``,
    ``devices[].nets[...]``, ``merged_net_labels[].net``,
    ``parasitics.nets[].net``) used the *unescaped* comma form, while the
    written netlist used the escaped pipe form -- the same net, spelled two
    different ways depending which artifact you read it from, with no way
    for a caller to know the two strings named the same node short of
    hard-coding the ``,`` -> ``|`` substitution itself. Calling this on every
    net name before it enters the response makes it byte-identical to the
    netlist's own spelling everywhere it is reported (also applied by
    ``klt lvs``'s ``net_correspondence``/``mismatches[].net`` via
    ``lvs.py``'s ``_name_or_none``, sourced from the same
    ``Net.expanded_name()`` convention).

    A no-op for the overwhelming majority of net names, which contain no
    comma at all.
    """
    return name.replace(",", "|")


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


def _unique_net_name(base: str, existing: set[str], suffix: str = "__par") -> str:
    """A SPICE-safe internal parasitic-node name derived from ``base`` that
    does not collide with any already-present net name (an underscore suffix,
    not a dot, so ngspice never mistakes it for a hierarchy separator).

    ``suffix`` defaults to the original ``__par`` shunt-node suffix (issue
    #216/#283); the star topology (issue #592) also derives per-terminal
    "leg" net names from this same collision-avoidance logic with a
    ``__t<i>``-style suffix."""
    candidate = f"{base}{suffix}"
    if candidate not in existing:
        return candidate
    counter = 2
    while f"{base}{suffix}{counter}" in existing:
        counter += 1
    return f"{base}{suffix}{counter}"


def _describe_devices(
    circuit: kdb.Circuit,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Build the response's ``devices[]`` array and ``device_counts`` map.

    Every reported parameter is read straight off the ``kdb.Device`` object
    -- including ``c_f`` and ``r_ohm``, whose deck-declared two-term
    corrections (``CapacitorDevice.perim_cap_f_um``, issue #512, and
    ``ResistorDevice.fixed_offset_ohm``, issue #518) have already been
    applied to the device itself by
    :func:`_apply_device_parameter_corrections` inside
    :func:`_extract_netlist`. Those corrections used to be computed *here*,
    into the returned dict only, which left the written SPICE netlist and
    ``klt lvs``'s ``NetlistComparer`` reading the uncorrected value (issue
    #521); reading the already-corrected device back keeps this function's
    output identical while making it a report of the netlist rather than a
    second, independent computation.
    """
    devices: list[dict[str, Any]] = []
    device_counts: dict[str, int] = {}

    for device in circuit.each_device():
        device_class = device.device_class()
        class_name = device_class.name

        nets: dict[str, str | None] = {}
        for terminal in device_class.terminal_definitions():
            net = device.net_for_terminal(terminal.id())
            nets[terminal.name.lower()] = (
                spice_safe_net_name(net.expanded_name()) if net is not None else None
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
                # fires for them. Already carries the deck's
                # `perim_cap_f_um * P` perimeter/fringe term when the deck
                # opted in (issue #512), applied at full precision to the
                # device itself by `_apply_device_parameter_corrections`.
                params["c_f"] = round(
                    device.parameter(param.id()), _PARAM_PRECISION_FARAD
                )
            elif param.name == "A":
                # Overlap area in square micrometres: the capacitor's
                # plate-overlap area -- the geometry `c_f` above was computed
                # from (`C = A * area_cap`, see `CapacitorDevice`'s
                # docstring) -- or, for a `DeviceClassDiode` (issue #542),
                # the recognised junction's own area. Reported alongside so a
                # consumer can sanity-check the extracted value without
                # re-deriving it from the layout. `DeviceClassCapacitor`'s and
                # `DeviceClassDiode`'s area/perimeter parameters are both
                # named `A`/`P` -- distinct from
                # `DeviceClassBJT3Transistor`'s `AE`/`AB`/`AC`/`PE`/`PB`/`PC`
                # (see "Bipolar (BJT) device recognition"), so this branch
                # never fires for a bipolar device.
                params["area_um2"] = round(
                    device.parameter(param.id()), _PARAM_PRECISION_UM
                )
            elif param.name == "P":
                # Overlap perimeter in micrometres (issue #512): the
                # capacitor's plate-overlap perimeter, or a diode junction's
                # own perimeter (#542). KLayout's `DeviceClassCapacitor`
                # already computed this alongside `A`, but until #512 it was
                # never read back. Reported for the same sanity-check reason
                # as `area_um2`, and consumed by
                # `_apply_device_parameter_corrections` to correct `C` for a
                # deck that sets a nonzero `perim_cap_f_um`.
                params["perimeter_um"] = round(
                    device.parameter(param.id()), _PARAM_PRECISION_UM
                )
            elif param.name == "R":
                # Drawn-resistor device classes only (#222): KLayout's
                # `R = L / W * sheet_rho`, in ohms. MOS classes have no `R`
                # parameter, so this branch never fires for them. Already
                # carries the deck's fixed head/end-effect
                # `fixed_offset_ohm` term when the deck opted in (issue
                # #518), applied at full precision to the device itself by
                # `_apply_device_parameter_corrections`.
                params["r_ohm"] = round(
                    device.parameter(param.id()), _PARAM_PRECISION_OHM
                )
            elif param.name == "AS":
                # MOS source-diffusion junction area, square micrometres
                # (issue #695). Present regardless of `--pdk`: this is the
                # same value the unbound `M`-card form's `AS=...` already
                # carries and, since #695, the same value a `--pdk`-bound `X`
                # card's own `AS=...` carries -- so a caller reading this
                # field never needs an unbound extraction just to recover it.
                params["as_um2"] = round(
                    device.parameter(param.id()), _PARAM_PRECISION_UM
                )
            elif param.name == "AD":
                # MOS drain-diffusion junction area, square micrometres --
                # see `AS` above (issue #695).
                params["ad_um2"] = round(
                    device.parameter(param.id()), _PARAM_PRECISION_UM
                )
            elif param.name == "PS":
                # MOS source-diffusion junction perimeter, micrometres --
                # see `AS` above (issue #695).
                params["ps_um"] = round(
                    device.parameter(param.id()), _PARAM_PRECISION_UM
                )
            elif param.name == "PD":
                # MOS drain-diffusion junction perimeter, micrometres -- see
                # `AS` above (issue #695).
                params["pd_um"] = round(
                    device.parameter(param.id()), _PARAM_PRECISION_UM
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
        raw_name = net.expanded_name()
        nets.append(
            {
                "name": spice_safe_net_name(raw_name),
                "pin": raw_name in pin_nets,
                "device_count": net.terminal_count(),
            }
        )

    nets.sort(key=lambda entry: entry["name"])
    return nets


def _detect_merged_net_labels(nets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the response's ``merged_net_labels[]`` array (issue #470).

    KLayout's ``Net.expanded_name()`` joins every distinct label found on one
    electrical net with ``,`` (e.g. two labels ``Y`` and ``OUT`` shorted
    together on layout come back as the single net name ``"Y,OUT"``,
    verified for issue #312's SPICE-instance-name-sanitization fix). That
    join is otherwise silent: `nets[]`' ``name`` field carries it, but
    nothing calls it out as the layout asserting a connectivity equality the
    caller may not have intended.

    Scans ``nets`` (the already-built ``nets[]`` array, so this reuses the
    same name this module reports elsewhere rather than re-querying the
    circuit) for any net whose name splits into 2+ parts on ``|`` -- the
    netlist-consistent spelling :func:`spice_safe_net_name` already rewrote
    ``nets[].name`` to (issue #696), matching the written SPICE netlist's own
    ``.SUBCKT``/instance-line node references. Returns one entry per match:
    ``{"net": "<full joined name>", "labels": ["Y", "OUT", ...]}`` -- ``net``
    is therefore a usable key into the written netlist, not a separately
    (comma-) spelled alias of it. Always a list; empty when no net carries
    multiple labels.

    Known limitation (heuristic, not exact): a label that legitimately
    contains a literal ``|`` is indistinguishable from a real multi-label
    collision by this substring split -- see docs/cli/extract.md's "Merged
    net labels" section.
    """
    merged: list[dict[str, Any]] = []
    for net in nets:
        name = net["name"]
        labels = name.split("|")
        if len(labels) < 2:
            continue
        merged.append({"net": name, "labels": labels})
    return merged


_ANONYMOUS_NET_PREFIX = "$"


def _detect_unbiased_pmos_body_nets(
    devices: list[dict[str, Any]], deck: ExtractionDeck
) -> list[dict[str, Any]]:
    """Build the response's ``unbiased_pmos_body_nets[]`` array (issue #555).

    Scans the already-built ``devices[]`` array (so this reuses the exact
    terminal-net names ``_describe_devices`` already read off the netlist)
    for every PMOS device (``device["class"] == deck.pfet_class``) whose body
    terminal (``nets["b"]``) is an anonymous, KLayout-synthesized net --
    identified by ``Net.expanded_name()``'s own ``"$<n>"`` placeholder
    convention for a net with no drawn label (the same convention
    ``tests/test_extract.py`` already asserts against, e.g.
    ``pfet["nets"]["b"].startswith("$")``). A *named* net -- including the
    deck's own synthesized global substrate net (e.g. ``"vsubs"``) -- never
    matches, so an NMOS body (tied via ``connect_global``) or a well-labelled
    PMOS body (a deck with a real ``well_label``/``tap`` layer, e.g. sky130)
    never appears here.

    Returns one entry per affected device, ``{"device": <device instance
    name>, "net": <anonymous net name>}``, sorted by device name for
    deterministic output (matching ``devices[]``/``nets[]``'s own sort
    discipline). Always a list; empty when no PMOS device's body net is
    anonymous.
    """
    unbiased: list[dict[str, Any]] = []
    for device in devices:
        if device["class"] != deck.pfet_class:
            continue
        body_net = device["nets"].get("b")
        if body_net is not None and body_net.startswith(_ANONYMOUS_NET_PREFIX):
            unbiased.append({"device": device["name"], "net": body_net})
    unbiased.sort(key=lambda entry: entry["device"])
    return unbiased


#: Terminal-name labels for a MOS-like device (identified below by the
#: presence of a ``"g"`` terminal key -- the one terminal name no other
#: recognised device class in this repo uses; see
#: :func:`_detect_single_terminal_nets`). Keyed by the same lower-cased
#: terminal name :func:`_describe_devices` already writes into
#: ``devices[].nets``.
_MOS_TERMINAL_KIND_LABELS = {"g": "gate", "s": "source", "d": "drain", "b": "body"}


def _detect_single_terminal_nets(
    devices: list[dict[str, Any]], nets: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Build the response's ``single_terminal_nets[]`` array (issue #596).

    Scans the already-built ``nets`` array (``_describe_nets``'s output,
    each carrying ``device_count`` -- literally ``Net.terminal_count()`` --
    and ``pin``) for every net with ``device_count == 1`` and ``pin: False``:
    a net that touches exactly one device terminal and is not a declared
    top-level pin. There is no DC path through such a node from anywhere
    else in the netlist -- ngspice reports ``singular matrix: check node
    <net>`` on it, several stages downstream of where the defect is
    structurally detectable. A gate terminal is the strongest form of this
    defect (a floating MOS input, essentially never intentional); a
    source/drain/body/resistor-style terminal can legitimately be a
    single-terminal net (e.g. an intentionally unterminated dummy's
    diffusion tie), so this reports every match but lets the caller (and
    the matching ``warnings[]`` prose below) weight ``terminal_kind ==
    "gate"`` more heavily than the rest.

    Cross-references the already-built ``devices`` array (mirroring
    :func:`_detect_unbiased_pmos_body_nets`'s reuse of the same terminal-net
    names ``_describe_devices`` already read off the netlist) to find the
    exact device/terminal that owns each flagged net -- a net's
    ``device_count`` disagreeing with the number of matching
    ``devices[].nets`` entries would mean the two already-built arrays
    disagree with each other, which should not happen; that net is skipped
    rather than guessed at.

    ``terminal_kind`` is ``"gate"``/``"source"``/``"drain"``/``"body"`` for a
    MOS-like device (identified by the presence of a ``"g"`` terminal on
    that device -- the one terminal name no other recognised device class in
    this repo's decks uses: drawn resistors/capacitors use ``"a"``/``"b"``
    (plus ``"w"`` for a bulk terminal), diodes use ``"a"``/``"c"``, bipolar
    devices use ``"c"``/``"b"``/``"e"``), else the literal terminal name
    (e.g. ``"a"``, ``"w"``) -- the "resistor-equivalent" case the issue
    calls out, reported as-is rather than guessed at with a device-specific
    label.

    Returns one entry per affected net, ``{"net": <net name>, "device": <owning
    device instance name>, "terminal": <lower-cased terminal key>,
    "terminal_kind": <str>}``, sorted by net name for deterministic output
    (matching ``nets[]``'s own sort discipline). Always a list; empty when
    every net either has zero or 2+ device terminals, or is a declared pin.
    """
    # net name -> (owning device name, terminal key) for every connected
    # device terminal, built once so a flagged net (device_count == 1) can be
    # traced back to the exact device/terminal without re-scanning `devices`
    # per net.
    owners: dict[str, list[tuple[str, str]]] = {}
    device_terminal_keys: dict[str, set[str]] = {}
    for device in devices:
        device_terminal_keys[device["name"]] = set(device["nets"].keys())
        for terminal_key, net_name in device["nets"].items():
            if net_name is None:
                continue
            owners.setdefault(net_name, []).append((device["name"], terminal_key))

    single_terminal: list[dict[str, Any]] = []
    for net in nets:
        if net["pin"] or net["device_count"] != 1:
            continue
        matches = owners.get(net["name"], [])
        if len(matches) != 1:
            continue
        device_name, terminal_key = matches[0]
        terminal_keys = device_terminal_keys.get(device_name, set())
        if "g" in terminal_keys:
            terminal_kind = _MOS_TERMINAL_KIND_LABELS.get(terminal_key, terminal_key)
        else:
            terminal_kind = terminal_key
        single_terminal.append(
            {
                "net": net["name"],
                "device": device_name,
                "terminal": terminal_key,
                "terminal_kind": terminal_kind,
            }
        )
    single_terminal.sort(key=lambda entry: entry["net"])
    return single_terminal


def _describe_layers_in_set(
    path: str, layer_set: frozenset[tuple[int, int]], *, invert: bool = False
) -> list[dict[str, Any]]:
    """Shared helper behind ``ignored_layers``/``device_recognition_only_
    layers`` (issue #619): enumerate the input stream's layers (reusing
    ``layers.py``'s existing per-layer walk, the same one ``klt drc``'s
    coverage report leans on) and return the shape-bearing ``(layer,
    datatype)`` pairs that are in ``layer_set`` (``invert=False``) or *not* in
    it (``invert=True``). Each entry carries its stream ``shapes`` count.
    Empty-layer entries (``shapes == 0``) are skipped; the list is sorted by
    ``(layer, datatype)``.
    """
    from .layers import layers_report

    described: list[dict[str, Any]] = []
    for entry in layers_report(path)["layers"]:
        if entry["shapes"] <= 0:
            continue
        in_set = (entry["layer"], entry["datatype"]) in layer_set
        if in_set == invert:
            continue
        described.append(
            {
                "layer": entry["layer"],
                "datatype": entry["datatype"],
                "shapes": entry["shapes"],
            }
        )
    described.sort(key=lambda e: (e["layer"], e["datatype"]))
    return described


def _describe_ignored_layers(path: str, deck: ExtractionDeck) -> list[dict[str, Any]]:
    """Build the response's ``ignored_layers[]`` array (issue #220).

    Returns the shape-bearing ``(layer, datatype)`` pairs that are *not* in
    ``deck.connectivity_layers`` -- geometry the extraction connectivity graph
    never reads. Each entry carries its stream ``shapes`` count so a consumer
    can judge whether the amount is material (a stray annotation vs. a whole
    block routed on an undeclared metal level).

    Note this does *not* catch a layer that is read for device recognition
    only, never as a ``metals``/``vias`` connectivity level -- see
    :func:`_describe_device_recognition_only_layers` for that distinct case
    (issue #619).
    """
    return _describe_layers_in_set(path, deck.connectivity_layers, invert=True)


def _describe_device_recognition_only_layers(
    path: str, deck: ExtractionDeck
) -> list[dict[str, Any]]:
    """Build the response's ``device_recognition_only_layers[]`` array
    (issue #619).

    Returns the shape-bearing ``(layer, datatype)`` pairs in
    ``deck.device_recognition_only_layers`` -- layers the deck *reads* (for a
    ``bipolars``/``capacitors``/``resistors``/``diodes`` device-recognition
    role) but never treats as a ``metals``/``vias`` connectivity level, so
    shapes there are invisible to net-merging even though they are not
    "ignored" in the ``ignored_layers`` sense. Each entry carries its stream
    ``shapes`` count. This is the "read but not merged" counterpart to
    ``ignored_layers``'s "never read at all" -- see
    ``ExtractionDeck.device_recognition_only_layers``'s docstring for why the
    distinction matters (sky130's own met3/met4 hid a routing-connectivity
    gap behind this exact ambiguity before its ``metals`` stack grew to
    cover them too).
    """
    return _describe_layers_in_set(
        path, deck.device_recognition_only_layers, invert=False
    )


def _describe_parasitics_metal_gaps(
    deck: ExtractionDeck, parasitics_deck: ParasiticsDeck
) -> list[dict[str, Any]]:
    """Build the ``parasitics.metals_without_coefficient[]`` array (issue #547).

    ``_compute_parasitics`` walks ``parasitics_deck.metals`` index-aligned
    against ``deck.metals`` (the extraction deck's declared metal stack,
    index 0 = the bottom level) and silently contributes zero R/C for any
    stack level that has no coefficient -- either because
    ``parasitics_deck.metals`` is shorter than ``deck.metals`` (truncation)
    or the entry at that index is explicitly ``None``. Both are the same gap
    from a caller's perspective: the level's R and C are missing from every
    net's reported parasitics, with nothing else in the JSON to say so.

    Returns one entry per gap, ``{"metal_index": int, "layer": int,
    "datatype": int}`` (``metal_index`` is 0-based, matching ``deck.metals``
    and ``parasitics_deck.metals``' shared indexing -- index 0 is the deck's
    bottom-most metal level, e.g. gf180mcu's Metal1), sorted by
    ``metal_index``. Empty when every declared metal level has a coefficient
    -- the common case, and always true when ``--parasitics`` was not
    requested (callers only invoke this when ``parasitics_deck is not
    None``).
    """
    gaps: list[dict[str, Any]] = []
    for i, layer in enumerate(deck.metals):
        if i >= len(parasitics_deck.metals) or parasitics_deck.metals[i] is None:
            gaps.append({"metal_index": i, "layer": layer[0], "datatype": layer[1]})
    return gaps


def _each_pin_net(circuit: kdb.Circuit) -> list[kdb.Net]:
    """The distinct nets exposed as circuit pins."""
    result = []
    for pin in circuit.each_pin():
        net = circuit.net_for_pin(pin.id())
        if net is not None:
            result.append(net)
    return result
