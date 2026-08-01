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
import os
from typing import TYPE_CHECKING, Any

from .decks import ExtractionDeck, UnknownExtractionDeckError, get_extraction_deck
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

    netlist, top_cell_name, dbu_um, warnings = extract_netlist_from_layout(
        path, deck_name, top=top
    )

    import klayout.db as kdb

    netlist_path = output if output is not None else _default_output_path(path)
    out_dir = os.path.dirname(os.path.abspath(netlist_path))
    if not os.path.isdir(out_dir):
        raise ExtractError(f"output directory does not exist: {out_dir}")

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

    # `netlist.purge()` (in `_extract_netlist`) drops a circuit entirely when
    # it has no devices, no pins, and no subcircuits -- e.g. a layout with no
    # extractable devices and no named nets. That is a legitimate "nothing
    # extracted" result, not an error: report zero devices/nets rather than
    # dereferencing a `None` circuit.
    circuit = netlist.circuit_by_name(top_cell_name)
    if circuit is not None:
        devices, device_counts = _describe_devices(circuit)
        nets = _describe_nets(circuit)
    else:
        devices, device_counts, nets = [], {}, []

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

    return result


def extract_netlist_from_layout(
    path: str, deck_name: str, top: str | None = None
) -> tuple[kdb.Netlist, str, float, list[str]]:
    """Core extraction: read ``path``, resolve ``deck_name`` and the top
    cell, and run flat device + connectivity extraction. Returns
    ``(netlist, top_cell_name, dbu_um, warnings)``.

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
    netlist, warnings = _extract_netlist(layout, top_cell, deck)
    return netlist, top_cell.name, layout.dbu, warnings


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
    layout: kdb.Layout, top_cell: kdb.Cell, deck: ExtractionDeck
) -> tuple[kdb.Netlist, list[str]]:
    """Build a flat ``LayoutToNetlist`` connectivity graph for ``deck`` and
    run device + netlist extraction.

    Flat (not hierarchical) extraction, deliberately: every layer is a
    single flattened ``Region``/``Texts`` collection over ``top_cell`` (via
    ``begin_shapes_rec``), the same whole-layout flattening idiom
    ``drc.py`` uses -- see ``docs/cli/extract.md``'s limitation note.

    Returns ``(netlist, warnings)``. ``warnings`` is built from the
    extractor's own log entries (e.g. a gate touching no diffusion) --
    non-fatal notes surfaced in the JSON response's ``warnings`` field.
    """
    import klayout.db as kdb

    active = _region(layout, top_cell, deck.active)
    poly = _region(layout, top_cell, deck.poly)
    nwell = _region(layout, top_cell, deck.nwell)
    tap = _region(layout, top_cell, deck.tap)
    contact = _region(layout, top_cell, deck.contact)
    well_label = _texts(layout, top_cell, deck.well_label)
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
        l2n.register(region, name)
    for index, region in enumerate(metals):
        l2n.register(region, f"metal{index}")
    for index, region in enumerate(vias):
        l2n.register(region, f"via{index}")
    l2n.register(well_label, "well_label")
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
    # `l2n` (and the Region/Texts objects it owns) would otherwise be
    # garbage-collected once this function returns, which invalidates the
    # netlist it produced (KLayout raises on subsequent use) -- `dup()`
    # detaches an independently-owned copy.
    return netlist.dup(), warnings


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
