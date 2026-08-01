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
zoo" scope guard ``docs/cli/drc.md`` documents for the DRC decks. See
``klayout_tools.decks.sky130``/``gf180mcu`` for the exact per-family layer
roles and their known connectivity-fidelity limitations (well-tie handling
in particular).

Verified compatible with ``klt sim``'s netlist convention (see
``docs/cli/sim.md`` -> "Netlist convention"): the written SPICE is a
``.SUBCKT ... .ENDS`` circuit body with no top-level ``.control``/``.end``
card -- confirmed directly against KLayout's ``NetlistSpiceWriter`` output
(it never emits a top-level ``.END`` for a single-circuit netlist), and
exercised by ``tests/test_extract.py``.
"""

from __future__ import annotations

import hashlib
import math
import os
from typing import TYPE_CHECKING, Any

from .decks import (
    ExtractionDeck,
    ParasiticsDeck,
    UnknownExtractionDeckError,
    get_extraction_deck,
    get_parasitics_deck,
)
from .pdk import PdkNotFoundError, find_pdk
from .pdk_models import (
    ModelBindingError,
    create_model_binding_delegate,
    resolve_mos_model_table,
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
) -> dict[str, Any]:
    """Extract a schematic-equivalent netlist from the layout at ``path``.

    ``deck_name`` selects the curated :class:`~klayout_tools.decks.ExtractionDeck`
    (currently ``"sky130"``/``"gf180mcu"``). ``output`` overrides the written
    SPICE path (default: ``path`` with its extension replaced by
    ``.spice``, next to the input -- the "next to the input" convention
    ``klt render``/``klt sim`` already use). ``top`` selects the top cell
    when the stream has more than one (required in that case; otherwise
    optional and must name the sole top cell if given).

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
            "device_classes": [<device class role>, ...],
            "devices": [
                {
                    "name": str, "class": str,
                    "nets": {"s": str, "g": str, "d": str, "b": str | None},
                    "params": {"w_um": float, "l_um": float},
                },
                ...
            ],
            "nets": [{"name": str, "pin": bool, "device_count": int}, ...],
            "warnings": [str, ...],
            "pdk": {"variant": str, "root": str, "version": str | None} | None,
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

    Raises :class:`ExtractError` if the file is missing/unreadable, the deck
    name is unknown, the PDK (when given) does not resolve, the top cell is
    missing/ambiguous, or the output path's directory does not exist.
    """
    pdk_info: dict[str, Any] | None = None
    # Populated only when a PDK resolves: `{<deck's device class name>:
    # <resolved PDK subckt name>}` for the MOS classes this deck extracts,
    # e.g. `{"nfet": "sky130_fd_pr__nfet_01v8", "pfet": ...}`. Drives the
    # `X`-card model-binding writer below -- see the module docstring's
    # "--pdk-triggered model binding" note and `klayout_tools.pdk_models`.
    model_class_to_subckt: dict[str, str] | None = None
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
            subckt_names = resolve_mos_model_table(deck_name, pdk_info["variant"])
        except ModelBindingError as exc:
            raise ExtractError(str(exc)) from exc
        model_class_to_subckt = {
            deck_for_models.nfet_class: subckt_names["nfet"],
            deck_for_models.pfet_class: subckt_names["pfet"],
        }

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

    netlist, top_cell_name, dbu_um, warnings, parasitic_nets = (
        extract_netlist_from_layout(
            path, deck_name, top=top, parasitics_deck=parasitics_deck
        )
    )

    import klayout.db as kdb

    netlist_path = output if output is not None else _default_output_path(path)
    out_dir = os.path.dirname(os.path.abspath(netlist_path))
    if not os.path.isdir(out_dir):
        raise ExtractError(f"output directory does not exist: {out_dir}")

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
        kdb.NetlistSpiceWriter(create_model_binding_delegate(model_class_to_subckt))
        if model_class_to_subckt is not None
        else kdb.NetlistSpiceWriter()
    )
    writer.use_net_names = True
    try:
        netlist.write(
            netlist_path, writer, f"extracted by klt extract --deck {deck_name}"
        )
    except Exception as exc:
        raise ExtractError(f"could not write netlist '{netlist_path}': {exc}") from exc

    netlist_sha256 = _sha256_file(netlist_path)

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
        "device_classes": list(deck.device_classes),
        "devices": devices,
        "nets": nets,
        "warnings": warnings,
    }
    if pdk_info is not None:
        result["pdk"] = {
            "variant": pdk_info["variant"],
            "root": pdk_info["root"],
            "version": pdk_info["version"],
        }
    else:
        result["pdk"] = None

    # Additive, independently-optional field (issue #216 addendum): `null`
    # unless `--parasitics` was given, a `parasitics` summary block otherwise.
    result["parasitics"] = parasitics_report

    return result


def extract_netlist_from_layout(
    path: str,
    deck_name: str,
    top: str | None = None,
    parasitics_deck: ParasiticsDeck | None = None,
) -> tuple[kdb.Netlist, str, float, list[str], list[dict[str, Any]] | None]:
    """Core extraction: read ``path``, resolve ``deck_name`` and the top
    cell, and run flat device + connectivity extraction. Returns
    ``(netlist, top_cell_name, dbu_um, warnings, parasitic_nets)``.

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
    netlist, warnings, parasitic_nets = _extract_netlist(
        layout, top_cell, deck, parasitics_deck
    )
    return netlist, top_cell.name, layout.dbu, warnings, parasitic_nets


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


def _extract_netlist(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    deck: ExtractionDeck,
    parasitics_deck: ParasiticsDeck | None = None,
) -> tuple[kdb.Netlist, list[str], list[dict[str, Any]] | None]:
    """Build a flat ``LayoutToNetlist`` connectivity graph for ``deck`` and
    run device + netlist extraction.

    Flat (not hierarchical) extraction, deliberately: every layer is a
    single flattened ``Region``/``Texts`` collection over ``top_cell`` (via
    ``begin_shapes_rec``), the same whole-layout flattening idiom
    ``drc.py`` uses -- see ``docs/cli/extract.md``'s limitation note.

    Returns ``(netlist, warnings, parasitic_nets)``. ``warnings`` is built
    from the extractor's own log entries (e.g. a gate touching no diffusion)
    -- non-fatal notes surfaced in the JSON response's ``warnings`` field.
    ``parasitic_nets`` is ``None`` unless ``parasitics_deck`` is given, in
    which case it is the per-net lumped-RC data computed from
    ``LayoutToNetlist.polygons_of_net`` while ``l2n`` is still alive (see
    :func:`_compute_parasitics`).
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
    # net instead of leaving it floating.
    nfet_body = kdb.Region()
    l2n.register(nfet_body, "nfet_body")

    nfet_extractor = kdb.DeviceExtractorMOS4Transistor(deck.nfet_class)
    pfet_extractor = kdb.DeviceExtractorMOS4Transistor(deck.pfet_class)
    l2n.extract_devices(nfet_extractor, {"SD": nfet_sd, "G": nfet_gate, "W": nfet_body})
    l2n.extract_devices(pfet_extractor, {"SD": pfet_sd, "G": pfet_gate, "W": nwell})

    warnings = [str(entry.message) for entry in l2n.each_log_entry()]

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

    l2n.extract_netlist()
    netlist = l2n.netlist()
    netlist.make_top_level_pins()
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
    return netlist.dup(), warnings, parasitic_nets


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
    half_p = perimeter_um / 2.0
    disc = half_p * half_p - 4.0 * area_um2
    if disc <= 0.0:
        return 1.0
    length = (half_p + math.sqrt(disc)) / 2.0
    width = area_um2 / length
    if width <= 0.0:
        return 1.0
    return max(1.0, length / width)


def _net_area_perim_um(
    l2n: kdb.LayoutToNetlist, net: kdb.Net, dbu: float, indices: list[int]
) -> tuple[float, float]:
    """Total ``(area_um2, perimeter_um)`` of ``net``'s shapes across the
    given registered layer ``indices`` (each an index returned by
    ``LayoutToNetlist.register``)."""
    area_um2 = 0.0
    perim_um = 0.0
    for index in indices:
        polys = l2n.polygons_of_net(net, index)
        area_um2 += polys.area() * dbu * dbu
        perim_um += polys.perimeter() * dbu
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

    Roles map to the registered geometry layers: ``diffusion`` aggregates the
    NMOS+PMOS source/drain regions, ``poly`` the poly region, and
    ``metals[i]`` each metal-stack layer (index-aligned with the deck's
    ``metals``). Net-to-net coupling capacitance is explicitly **not** modeled
    (issue #216: ground capacitance only in the first increment).

    Returns a list (sorted by net name for deterministic output) of
    ``{"net", "resistance_ohm", "capacitance_ff"}`` for every net with a
    non-zero ground capacitance; nets with no eligible interconnect geometry
    are omitted.
    """
    if circuit is None:
        return []

    # (LayerRC, [registered layer indices]) for every role that has both a
    # coefficient set and at least one present layer.
    roles: list[tuple[Any, list[int]]] = []
    if parasitics_deck.diffusion is not None:
        roles.append(
            (
                parasitics_deck.diffusion,
                [layer_index["nfet_sd"], layer_index["pfet_sd"]],
            )
        )
    if parasitics_deck.poly is not None:
        roles.append((parasitics_deck.poly, [layer_index["poly"]]))
    for i, layer_rc in enumerate(parasitics_deck.metals):
        if layer_rc is not None and i < len(metal_index):
            roles.append((layer_rc, [metal_index[i]]))

    results: list[dict[str, Any]] = []
    for net in circuit.each_net():
        name = net.expanded_name()
        if name == deck.substrate_net:
            continue
        r_ohm = 0.0
        c_ff = 0.0
        for layer_rc, indices in roles:
            area_um2, perim_um = _net_area_perim_um(l2n, net, dbu, indices)
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

    existing_names = {net.expanded_name() for net in circuit.each_net()}

    report_nets: list[dict[str, Any]] = []
    total_r = 0.0
    total_c_ff = 0.0
    for entry in parasitic_nets:
        net = circuit.net_by_name(entry["net"])
        if net is None:
            continue
        internal_name = _unique_net_name(entry["net"], existing_names)
        existing_names.add(internal_name)
        internal = circuit.create_net(internal_name)

        r_ohm = max(entry["resistance_ohm"], _MIN_PARASITIC_R_OHM)
        c_farad = entry["capacitance_ff"] * 1e-15

        r_dev = circuit.create_device(res_class, entry["net"])
        r_dev.connect_terminal("A", net)
        r_dev.connect_terminal("B", internal)
        r_dev.set_parameter("R", r_ohm)

        c_dev = circuit.create_device(cap_class, entry["net"])
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


def _each_pin_net(circuit: kdb.Circuit) -> list[kdb.Net]:
    """The distinct nets exposed as circuit pins."""
    result = []
    for pin in circuit.each_pin():
        net = circuit.net_for_pin(pin.id())
        if net is not None:
            result.append(net)
    return result


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()
