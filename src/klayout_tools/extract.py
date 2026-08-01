"""Extract a SPICE netlist from a GDSII/OASIS stream, headless.

Pure library: :func:`run_extract` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints, mirroring ``drc.py``.
Serialisation and human-readable formatting live in ``cli/extract_cmd.py``.

This is phase 2 of Epic #153, the build carried by the accepted spike
``docs/design/lvs-extraction-spike.md`` — read that document first. Its §1
settles the engine (KLayout's own ``klayout.db.LayoutToNetlist`` +
``NetlistSpiceWriter``, the pip dependency ``klt drc``/``klt render`` already
wrap) and its §2a settles the request/response contract implemented here.
``klt lvs`` (compare against a reference netlist, ``NetlistComparer``) is
phase 3 and is deliberately absent from this module.

Scope: **schematic-equivalent** extraction — devices and connectivity, no
parasitic R/C on the interconnect. That deferral is the spike's own recorded
decision ("Out of scope for this spike"), and a future parasitic mode is an
additive extension, not a change to this contract.

Headless invariant: uses only the pip ``klayout`` package's batch database
API (``klayout.db``) — no GUI, no Qt, and no dependency on the standalone
KLayout application binary or its LVS-DSL script runner. The connectivity and
device recipe comes from this repo's own curated extraction decks
(:class:`klayout_tools.decks.ExtractionDeck`), not from a PDK-shipped LVS
deck: neither sky130 nor gf180mcu ships a KLayout-native one (both route real
LVS through magic+netgen), which is exactly the gap the spike diagnosed.

Netlist shape: the emitted file is a **circuit body** — one ``.SUBCKT`` per
extracted circuit, no top-level ``.control`` and no top-level ``.end`` card —
so it drops straight into ``klt sim``'s ``netlist`` field (see
``docs/cli/sim.md`` → "Netlist convention"). :func:`_strip_deck_cards`
enforces that at the boundary regardless of what a future KLayout
release's writer chooses to emit.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import TYPE_CHECKING, Any

from ._layout import load_layout
from .decks import (
    BULK_LAYER,
    DeviceSpec,
    ExtractionDeck,
    UnknownDeckError,
    get_extraction_deck,
)
from .pdk import PdkNotFoundError, find_pdk

if TYPE_CHECKING:  # pragma: no cover - typing only
    import klayout.db as kdb

#: Bumped only on a non-additive (breaking) change to this command's own
#: response JSON shape — see docs/json-contract.md.
SCHEMA_VERSION = 1

#: Default extension for the written netlist when ``--output`` is omitted.
NETLIST_SUFFIX = ".spice"

#: Comment written as the first line of every emitted netlist. Purely
#: informational — SPICE comments are legal inside a circuit body.
NETLIST_HEADER = "extracted by klt extract"

#: A bare top-level ``.end`` card (never ``.ends``, which closes a subcircuit
#: and must survive). Matched case-insensitively at the start of a line.
_END_CARD = re.compile(r"^\s*\.end\s*$", re.IGNORECASE)

#: The opening/closing cards of an ngspice ``.control`` block.
_CONTROL_OPEN = re.compile(r"^\s*\.control\b", re.IGNORECASE)
_CONTROL_CLOSE = re.compile(r"^\s*\.endc\b", re.IGNORECASE)

#: ``geo_scaling_exponent`` -> JSON key suffix for a device parameter. KLayout
#: reports MOS geometry in micrometres (``L``/``W``/``PS``/``PD``, exponent 1)
#: and square micrometres (``AS``/``AD``, exponent 2); the suffix carries the
#: unit in the field name, the house rule every ``_um`` field follows.
_UNIT_SUFFIX = {1.0: "_um", 2.0: "_um2"}


class ExtractError(Exception):
    """Raised when a layout cannot be extracted: bad file, unknown deck,
    ambiguous/unknown top cell, unresolvable PDK, or an engine error.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback.
    """


def run_extract(
    path: str,
    deck_name: str | None = None,
    output: str | None = None,
    top: str | None = None,
    pdk: str | None = None,
    pdk_root: str | None = None,
) -> dict[str, Any]:
    """Extract ``path``'s netlist with ``deck_name``'s connectivity recipe.

    ``deck_name`` selects the extraction deck (``"sky130"`` / ``"gf180mcu"``).
    It may be omitted when ``pdk``/``pdk_root`` resolve an install whose
    variant names a known deck family (e.g. ``sky130A`` -> ``sky130``), the
    same family mapping ``klt gen`` uses.

    ``output`` is where the SPICE netlist is written; it defaults to the input
    path with its extension replaced by ``.spice`` (the "next to the input"
    convention ``klt render``/``klt sim`` already use). ``top`` names the top
    cell when the stream has more than one.

    ``pdk``/``pdk_root`` are optional and resolve through the one shared
    resolver every PDK-aware verb uses (:func:`klayout_tools.pdk.find_pdk`);
    this module never implements its own lookup. Extraction itself reads no
    files from the install — the curated deck is self-contained — so the
    resolved install is recorded for provenance (and to derive ``deck_name``),
    and omitting both flags is fully supported, which is what keeps this verb
    runnable in a CI job with no PDK installed.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/extract.md``)::

        {
            "schema_version": 1,
            "file": <path as provided>,
            "deck": <deck name>,
            "top": <top cell name>,
            "dbu_um": <database unit in micrometres, float>,
            "pdk": {"variant": str, "root": str, "version": str | None} | None,
            "netlist_path": <resolved path of the written netlist>,
            "netlist_sha256": <hex digest of the written netlist>,
            "status": "extracted",
            "device_count": int, "net_count": int, "pin_count": int,
            "device_counts": {<device class>: int, ...},
            "devices": [...], "nets": [...], "warnings": [...],
        }

    ``devices`` is sorted by ``(circuit, class, name)`` and ``nets`` by
    ``(circuit, name)``, so repeated runs against the same inputs produce
    byte-identical output — the same canonical-ordering guarantee ``klt drc``
    makes about ``violations``.

    Raises :class:`ExtractError` for any condition that leaves no trustworthy
    netlist: missing/unreadable file, unknown deck, ambiguous or unknown top
    cell, unresolvable PDK, or an engine failure.
    """
    warnings: list[str] = []

    pdk_info = _resolve_pdk(pdk, pdk_root)
    resolved_deck_name = _resolve_deck_name(deck_name, pdk_info)

    try:
        deck = get_extraction_deck(resolved_deck_name)
    except UnknownDeckError as exc:
        raise ExtractError(str(exc)) from exc

    layout = load_layout(path, ExtractError)
    top_cell = _select_top_cell(layout, top)

    netlist_path = output or (os.path.splitext(path)[0] + NETLIST_SUFFIX)

    l2n, regions = _build_extractor(layout, top_cell, deck)
    try:
        l2n.extract_netlist()
    except Exception as exc:  # klayout raises RuntimeError on engine failures
        raise ExtractError(f"extraction failed for '{path}': {exc}") from exc

    warnings.extend(_log_entry_warnings(l2n))

    netlist = l2n.netlist()
    netlist.make_top_level_pins()
    netlist.purge()
    netlist.combine_devices()
    netlist.purge_nets()

    warnings.extend(_name_anonymous_nets(netlist))

    _write_netlist(netlist, netlist_path, warnings)

    report = _build_report(netlist, top_cell.name)
    if report["device_count"] == 0:
        warnings.append(
            f"no devices extracted from top cell '{top_cell.name}' with deck "
            f"'{resolved_deck_name}' — check that the layout uses this PDK's "
            "layer numbers and that the deck recognises its device types"
        )

    # `regions` is retained only until extraction completes: KLayout requires
    # the Region objects stay alive while the extractor holds them.
    del regions

    return {
        "schema_version": SCHEMA_VERSION,
        "file": path,
        "deck": resolved_deck_name,
        "top": top_cell.name,
        "dbu_um": layout.dbu,
        "pdk": pdk_info,
        "netlist_path": netlist_path,
        "netlist_sha256": _sha256(netlist_path),
        "status": "extracted",
        **report,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# PDK / deck / top-cell resolution
# ---------------------------------------------------------------------------


def _resolve_pdk(pdk: str | None, pdk_root: str | None) -> dict[str, Any] | None:
    """Resolve the requested PDK install, or return ``None`` when none was asked
    for.

    Resolution goes through :func:`klayout_tools.pdk.find_pdk` — the one
    resolver behind ``klt pdk find``/``klt pdk env`` — so this verb agrees with
    every other PDK-aware verb about which install is "the" install. A caller
    who names a PDK gets a hard error if it cannot be resolved (an explicit
    request is never silently downgraded); a caller who names none gets
    ``None`` and a fully deck-driven run.
    """
    if pdk is None and pdk_root is None:
        return None
    try:
        info = find_pdk(variant=pdk, root=pdk_root)
    except PdkNotFoundError as exc:
        raise ExtractError(str(exc)) from exc
    return {
        "variant": info["variant"],
        "root": info["root"],
        "version": info["version"],
    }


def _resolve_deck_name(deck_name: str | None, pdk_info: dict[str, Any] | None) -> str:
    """Return the extraction deck to use, deriving it from a resolved PDK
    variant when ``--deck`` was omitted.

    The variant -> family mapping is prefix-based (``sky130A`` -> ``sky130``),
    matching ``klt gen``'s ``_pdk_family``; ``klayout_tools.pdk`` itself has no
    family concept, and this module does not invent one beyond that prefix
    match.
    """
    if deck_name is not None:
        return deck_name
    if pdk_info is None:
        raise ExtractError("--deck is required (or --pdk, to derive it)")

    from .decks import extraction_deck_names

    variant = pdk_info["variant"]
    for family in extraction_deck_names():
        if variant.startswith(family):
            return family
    raise ExtractError(
        f"cannot derive an extraction deck from PDK variant '{variant}' "
        f"(known decks: {', '.join(extraction_deck_names())}) — pass --deck"
    )


def _select_top_cell(layout: kdb.Layout, top: str | None) -> kdb.Cell:
    """Pick the cell to extract from, honouring an explicit ``--top``.

    A stream with several top cells is ambiguous rather than wrong, so it is
    an error naming the candidates instead of a silent pick.
    """
    top_cells = list(layout.top_cells())
    if not top_cells:
        raise ExtractError("layout contains no cells")

    if top is not None:
        for cell in layout.each_cell():
            if cell.name == top:
                return cell
        raise ExtractError(f"top cell '{top}' not found in layout")

    if len(top_cells) > 1:
        names = ", ".join(sorted(cell.name for cell in top_cells))
        raise ExtractError(
            f"layout has {len(top_cells)} top cells ({names}) — use --top to select one"
        )
    return top_cells[0]


# ---------------------------------------------------------------------------
# Deck -> LayoutToNetlist
# ---------------------------------------------------------------------------


def _build_extractor(
    layout: kdb.Layout, top_cell: kdb.Cell, deck: ExtractionDeck
) -> tuple[Any, dict[str, Any]]:
    """Apply ``deck`` to ``layout``'s ``top_cell`` and return the configured
    ``LayoutToNetlist`` plus its region table.

    Order matters and follows KLayout's LVS idiom: bind drawn layers, compute
    derived layers, run the device extractors, then declare connectivity. A
    drawn layer absent from the stream becomes an *empty* region rather than
    an error — the same "layer absent -> nothing to report" posture ``klt drc``
    takes — so a cell that simply does not use met5 extracts cleanly.
    """
    import klayout.db as kdb

    l2n = kdb.LayoutToNetlist(kdb.RecursiveShapeIterator(layout, top_cell, []))

    regions: dict[str, Any] = {}
    for key, (layer, datatype) in deck.layers.items():
        index = layout.find_layer(layer, datatype)
        regions[key] = (
            l2n.make_layer(index, key) if index is not None else l2n.make_layer(key)
        )

    # The substrate placeholder: an always-empty region that gives a MOS4
    # extractor its body terminal where the PDK draws no p-well layer.
    regions[BULK_LAYER] = l2n.make_layer(BULK_LAYER)

    for derived in deck.derived:
        left = _region(regions, derived.left, deck.name)
        right = _region(regions, derived.right, deck.name)
        if derived.op == "and":
            result = left & right
        elif derived.op == "not":
            result = left - right
        else:  # pragma: no cover - guarded by the deck's own construction
            raise ExtractError(
                f"deck '{deck.name}': unsupported derived-layer op '{derived.op}'"
            )
        l2n.register(result, derived.name)
        regions[derived.name] = result

    for device in deck.devices:
        _extract_device(l2n, regions, device, deck.name)

    for key in deck.intra_connect:
        l2n.connect(_region(regions, key, deck.name))
    for a, b in deck.inter_connect:
        l2n.connect(_region(regions, a, deck.name), _region(regions, b, deck.name))
    for key, net_name in deck.global_connect:
        l2n.connect_global(_region(regions, key, deck.name), net_name)

    for region_key, text_key in deck.labels:
        pair = deck.texts.get(text_key)
        if pair is None:  # pragma: no cover - guarded by the deck's construction
            raise ExtractError(f"deck '{deck.name}': unknown text layer '{text_key}'")
        index = layout.find_layer(*pair)
        if index is None:
            continue  # this stream carries no labels on that layer
        texts = l2n.make_text_layer(index, text_key)
        l2n.connect(_region(regions, region_key, deck.name), texts)

    return l2n, regions


def _extract_device(
    l2n: Any, regions: dict[str, Any], device: DeviceSpec, deck_name: str
) -> None:
    """Register one device extractor from a :class:`DeviceSpec`."""
    import klayout.db as kdb

    if device.kind != "mos4":  # pragma: no cover - only kind the decks use
        raise ExtractError(
            f"deck '{deck_name}': unsupported device kind '{device.kind}'"
        )
    extractor = kdb.DeviceExtractorMOS4Transistor(device.name)
    l2n.extract_devices(
        extractor,
        {
            "SD": _region(regions, device.source_drain, deck_name),
            "G": _region(regions, device.gate, deck_name),
            "P": _region(regions, device.gate_conductor, deck_name),
            "W": _region(regions, device.well, deck_name),
        },
    )


def _region(regions: dict[str, Any], key: str, deck_name: str) -> Any:
    try:
        return regions[key]
    except KeyError:  # pragma: no cover - guarded by the deck's construction
        raise ExtractError(f"deck '{deck_name}': unknown layer '{key}'") from None


# ---------------------------------------------------------------------------
# Netlist post-processing + report
# ---------------------------------------------------------------------------


def _log_entry_warnings(l2n: Any) -> list[str]:
    """Turn the extractor's own log entries into contract ``warnings[]`` lines.

    KLayout records soft-connection notes, device-recognition oddities, and
    similar findings here; they are non-fatal by construction (a fatal problem
    raises out of ``extract_netlist``), which is exactly what ``warnings``
    means in this contract.
    """
    entries: list[str] = []
    for entry in l2n.each_log_entry():
        message = entry.message.strip()
        if not message:
            continue
        cell = entry.cell_name
        entries.append(f"{cell}: {message}" if cell else message)
    return sorted(set(entries))


def _name_anonymous_nets(netlist: Any) -> list[str]:
    """Give every unnamed net a stable, SPICE-safe name.

    KLayout leaves a net with no label unnamed and the SPICE writer emits it
    as an escaped ``\\$<id>`` node. That is legal KLayout syntax but hostile to
    a downstream simulator reading the file as a plain circuit body, so each
    unnamed net gets ``net_<cluster id>`` instead — derived from the net's own
    cluster id, so it is deterministic across runs, and de-duplicated against
    the circuit's real net names so a label literally called ``net_7`` cannot
    be shadowed.
    """
    renamed = 0
    for circuit in netlist.each_circuit():
        taken = {net.name for net in circuit.each_net() if net.name}
        for net in circuit.each_net():
            if net.name:
                continue
            candidate = f"net_{net.cluster_id}"
            while candidate in taken:
                candidate += "_x"
            net.name = candidate
            taken.add(candidate)
            renamed += 1
    if not renamed:
        return []
    return [
        f"{renamed} unlabelled net(s) were given generated net_<id> names "
        "(add label/text geometry on the PDK's label layers to name them)"
    ]


def _build_report(netlist: Any, top_name: str) -> dict[str, Any]:
    """Build the ``devices``/``nets``/count half of the response payload.

    Every extracted circuit is reported, not just the top one: a hierarchical
    layout puts its devices in the subcircuits, so a top-only report would say
    "0 devices" about a netlist full of them. Each entry therefore carries a
    ``circuit`` field, and ``pin_count`` stays scoped to the top circuit — the
    pins of the ``.subckt`` a caller instantiates.
    """
    devices: list[dict[str, Any]] = []
    nets: list[dict[str, Any]] = []
    device_counts: dict[str, int] = {}
    pin_count = 0

    for circuit in netlist.each_circuit():
        if circuit.name == top_name:
            pin_count = circuit.pin_count()

        for device in circuit.each_device():
            device_class = device.device_class()
            class_name = device_class.name
            device_counts[class_name] = device_counts.get(class_name, 0) + 1
            devices.append(
                {
                    "circuit": circuit.name,
                    "name": device.expanded_name(),
                    "class": class_name,
                    "nets": _device_nets(device, device_class),
                    "params": _device_params(device, device_class),
                }
            )

        for net in circuit.each_net():
            nets.append(
                {
                    "circuit": circuit.name,
                    "name": net.expanded_name(),
                    "pin": net.pin_count() > 0,
                    "device_count": len(
                        {ref.device().id() for ref in net.each_terminal()}
                    ),
                }
            )

    devices.sort(key=lambda d: (d["circuit"], d["class"], d["name"]))
    nets.sort(key=lambda n: (n["circuit"], n["name"]))

    return {
        "device_count": len(devices),
        "net_count": len(nets),
        "pin_count": pin_count,
        "device_counts": dict(sorted(device_counts.items())),
        "devices": devices,
        "nets": nets,
    }


def _device_nets(device: Any, device_class: Any) -> dict[str, str | None]:
    """Map each of the device class's terminals to the net attached to it.

    Terminal ids are the *engine's* (``S``/``G``/``D``/``B`` for a MOS4), kept
    verbatim rather than case-folded: they are the device class's own naming
    contract, shared with the reference netlist ``klt lvs`` will compare
    against in phase 3.
    """
    result: dict[str, str | None] = {}
    for terminal in device_class.terminal_definitions():
        net = device.net_for_terminal(terminal.id())
        result[terminal.name] = net.expanded_name() if net is not None else None
    return result


def _device_params(device: Any, device_class: Any) -> dict[str, float]:
    """Extract the device's geometry parameters, unit-suffixed.

    KLayout reports each parameter with a ``geo_scaling_exponent`` (1 for a
    length, 2 for an area), which is turned into the ``_um``/``_um2`` field
    suffix the house rule wants; a parameter with any other exponent keeps its
    bare lowercased name rather than claim a unit it does not have.
    """
    params: dict[str, float] = {}
    for definition in device_class.parameter_definitions():
        suffix = _UNIT_SUFFIX.get(definition.geo_scaling_exponent, "")
        params[f"{definition.name.lower()}{suffix}"] = device.parameter(definition.id())
    return dict(sorted(params.items()))


# ---------------------------------------------------------------------------
# Netlist writing
# ---------------------------------------------------------------------------


def _write_netlist(netlist: Any, path: str, warnings: list[str]) -> None:
    """Write ``netlist`` to ``path`` as a ``klt sim``-consumable circuit body."""
    import klayout.db as kdb

    directory = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        raise ExtractError(f"could not create output directory: {exc}") from exc

    writer = kdb.NetlistSpiceWriter()
    writer.use_net_names = True
    writer.with_comments = False
    try:
        netlist.write(path, writer, NETLIST_HEADER)
    except Exception as exc:  # klayout raises RuntimeError on write failures
        raise ExtractError(f"could not write netlist '{path}': {exc}") from exc

    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise ExtractError(f"could not read back netlist '{path}': {exc}") from exc

    stripped, removed = _strip_deck_cards(text)
    if removed:
        warnings.append(
            f"removed {removed} top-level deck card(s) (.end/.control) so the "
            "netlist is a circuit body per docs/cli/sim.md"
        )
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(stripped)
        except OSError as exc:
            raise ExtractError(f"could not write netlist '{path}': {exc}") from exc


def _strip_deck_cards(text: str) -> tuple[str, int]:
    """Remove top-level ``.end`` cards and ``.control``/``.endc`` blocks.

    ``klt sim`` composes its own wrapper deck around the netlist it is given
    (``docs/cli/sim.md`` → "Netlist convention"), so a file carrying its own
    ``.end`` or ``.control`` is explicitly unsupported there. KLayout's
    ``NetlistSpiceWriter`` emits neither today — this is a boundary guarantee,
    not a workaround: the contract fixes "circuit body, no deck cards" at the
    ``klt extract`` boundary, so a future writer change cannot silently break
    a downstream ``klt sim`` run.

    ``.ends`` (subcircuit terminator) is deliberately *not* matched — that is
    the card the circuit body is built from.

    Returns the cleaned text and the number of removed cards.
    """
    out: list[str] = []
    removed = 0
    in_control = False
    for line in text.splitlines(keepends=True):
        if in_control:
            removed += 1
            if _CONTROL_CLOSE.match(line):
                in_control = False
            continue
        if _CONTROL_OPEN.match(line):
            in_control = True
            removed += 1
            continue
        if _END_CARD.match(line):
            removed += 1
            continue
        out.append(line)
    return "".join(out), removed


def _sha256(path: str) -> str:
    """Return the SHA-256 hex digest of the file at ``path``."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()
