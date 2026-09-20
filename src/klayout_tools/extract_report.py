"""Shape a resolved KLayout netlist into ``klt extract``'s JSON report.

Split out of ``extract.py`` (issue #2070), which at ~8400 lines was this
repo's largest source module, following the same precedent as
``extract_abstract.py`` (issue #1303), ``extract_spef.py`` (issue #1195)
and ``extract_parasitics.py`` (issue #1572). This module owns the *report*
layer of ``klt extract``: given an already-resolved ``kdb.Circuit`` /
``kdb.LayoutToNetlist`` (plus the deck that produced it), it builds the
response's ``devices[]``/``nets[]`` arrays, the ``device_counts`` map, and
the layer/net warning lists (``merged_net_labels``,
``unbiased_pmos_body_nets``, ``single_terminal_nets``, ``ignored_layers``,
``device_recognition_only_layers``, and the two ``parasitics`` deck-gap
reports). It performs no geometry work and drives no extraction of its own
-- every input it reads has already been produced by ``extract.py``'s
recognition engine, which is exactly why the two concerns split cleanly.

Every caller lives in ``extract.py``: ``run_extract`` assembles the bulk of
the report from these functions, and ``_extract_netlist`` calls
:func:`_net_label_positions` once while the layout is still open. Three
helpers -- :func:`_describe_layers_in_set`, :func:`_pin_index_by_net_id`
and :func:`_each_pin_net` -- are private to this module (their only callers
are its own siblings), so ``extract.py`` does not import them.

:func:`_describe_devices` still needs the
``_PARAM_PRECISION_UM``/``_PARAM_PRECISION_FARAD``/``_PARAM_PRECISION_OHM``
rounding tunables, which stay in ``extract.py`` because they have other
callers there. That import is deferred into the function body -- the same
convention ``extract_parasitics.py`` already uses for its own
back-references -- because ``extract.py`` imports *this* module at its top
level, so a module-level ``from .extract import ...`` here would be a
circular import evaluated mid-load.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from .decks import ExtractionDeck, ParasiticsDeck
from .extract_parasitics import spice_safe_net_name

if TYPE_CHECKING:
    import klayout.db as kdb


def _describe_devices(
    circuit: kdb.Circuit,
    device_instance_paths: Mapping[int, list[dict[str, Any]]] | None = None,
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

    ``device_instance_paths`` (issue #1666), keyed by ``Device.id()``, is
    :func:`_device_instance_paths`'s result -- folded into each returned
    entry's ``instance_path``, empty (``[]``) for a device that resolved to
    no path (see that function's docstring) or when ``device_instance_paths``
    itself is ``None`` (a caller with none to offer, matching
    ``net_label_positions``'s own default-empty convention below).
    """
    # Deferred (call-time) import, not a module-level one: `extract.py`
    # imports this module at its own module scope (mirroring the
    # `extract_spef.py`/`extract_abstract.py`/`extract_parasitics.py`
    # precedent), so a module-level `from .extract import ...` here would be a
    # circular import evaluated during that very load. The three
    # `_PARAM_PRECISION_*` tunables stay in `extract.py` because they have
    # other callers there -- this defers the back-reference until
    # `_describe_devices` is actually called, by which point `extract.py` has
    # finished importing.
    from .extract import (
        _PARAM_PRECISION_FARAD,
        _PARAM_PRECISION_OHM,
        _PARAM_PRECISION_UM,
    )

    devices: list[dict[str, Any]] = []
    device_counts: dict[str, int] = {}
    instance_paths = device_instance_paths or {}

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
                # expanded_name() yields a bare $<n> for anonymous devices:
                # deliberately NOT backslash-escaped, unlike net names. A
                # device name always follows a class-letter prefix (M$1, R$22)
                # on its SPICE instance line, so ngspice's leading-$ comment
                # hazard that spice_safe_net_name() guards against cannot
                # arise here. See issue #1439 and docs/cli/extract.md.
                "name": device.expanded_name(),
                "class": class_name,
                "nets": nets,
                "params": params,
                # Issue #1666: which GDS-level repeated instance this device
                # positionally resolved to -- see
                # `_device_instance_paths`/`_instance_path_for_point`'s
                # docstrings. `[]` when it resolved to none (drawn directly
                # in the top cell) or `device_instance_paths` was not given.
                "instance_path": instance_paths.get(device.id(), []),
            }
        )
        device_counts[class_name] = device_counts.get(class_name, 0) + 1

    devices.sort(key=lambda entry: entry["name"])
    return devices, device_counts


def _describe_matched_device_groups(
    matched_device_groups: Mapping[str, Sequence[str]] | None,
    devices: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build the response's ``matched_device_groups[]`` array and its
    aggregate ``warnings[]`` entries (issue #1018).

    ``matched_device_groups`` is ``{<group name>: (<instance name>, ...),
    ...}`` -- ``run_extract``'s own parsed form of ``--matched-group
    NAME=INST1,INST2[,...]``. ``devices`` is the already-built ``devices[]``
    array (see :func:`_describe_devices`), so every comparison reads the
    exact same rounded ``params`` values every other consumer of the response
    sees -- this is a report on the extracted netlist, not a second,
    independent measurement.

    For each declared group, every member name is resolved against
    ``devices[].name`` (a KLayout-synthesized ``"$<n>"`` instance name unless
    the deck's writer names it otherwise); a name matching no extracted
    device is collected into that group's ``unresolved_instances`` rather
    than raising -- a caller may legitimately reuse one group declaration
    across several layout variants, the same tolerant convention
    ``--critical-net`` already follows for an unmatched net name. With two or
    more *resolved* members, every parameter name present in **every**
    resolved member's ``params`` (the intersection, so a group mixing device
    classes only compares the fields they actually share) is compared for
    exact equality across the group -- already-rounded values
    (``_PARAM_PRECISION_UM``/``_PARAM_PRECISION_OHM``), so no separate
    numeric-tolerance concept is needed. Fewer than two resolved members
    (nothing to compare) always reports an empty ``mismatched_fields``.

    Returns ``(matched_device_groups_report, warnings)``: the former is one
    entry per declared group, in the order given, each ``{"name": <group
    name>, "instances": [<instance name>, ...], "unresolved_instances":
    [<instance name>, ...], "mismatched_fields": [{"field": <param name>,
    "values": {<instance name>: <float>, ...}}, ...]}`` (``instances`` echoes
    the declaration verbatim, ``unresolved_instances`` sorted); the latter is
    one aggregate prose ``warnings[]`` entry per group with unresolved
    members and one per group with mismatched fields (both empty, `[]`
    overall, when ``matched_device_groups`` is empty/``None``).
    """
    if not matched_device_groups:
        return [], []

    devices_by_name = {device["name"]: device for device in devices}
    groups: list[dict[str, Any]] = []
    warnings: list[str] = []

    for group_name, instance_names in matched_device_groups.items():
        resolved: list[dict[str, Any]] = []
        unresolved: list[str] = []
        for instance_name in instance_names:
            device = devices_by_name.get(instance_name)
            if device is None:
                unresolved.append(instance_name)
            else:
                resolved.append(device)

        mismatched_fields: list[dict[str, Any]] = []
        if len(resolved) >= 2:
            common_fields = set(resolved[0]["params"])
            for device in resolved[1:]:
                common_fields &= set(device["params"])
            for field in sorted(common_fields):
                values = {
                    device["name"]: device["params"][field] for device in resolved
                }
                if len(set(values.values())) > 1:
                    mismatched_fields.append({"field": field, "values": values})

        groups.append(
            {
                "name": group_name,
                "instances": list(instance_names),
                "unresolved_instances": sorted(unresolved),
                "mismatched_fields": mismatched_fields,
            }
        )

        if unresolved:
            names_str = ", ".join(repr(name) for name in sorted(unresolved))
            warnings.append(
                f"matched-group {group_name!r} names instance(s) {names_str} "
                "that match no extracted device in this layout -- geometry "
                "consistency was not checked for them. See "
                "docs/cli/extract.md's 'Matched-device geometry check' "
                "section."
            )
        if mismatched_fields:
            fields_str = "; ".join(
                f"{entry['field']}: "
                + ", ".join(
                    f"{name}={value}" for name, value in entry["values"].items()
                )
                for entry in mismatched_fields
            )
            instances_str = ", ".join(instance_names)
            warnings.append(
                f"matched-group {group_name!r} declares {instances_str} as "
                "intentionally matched, but their extracted geometry "
                f"diverges -- {fields_str} -- this likely breaks the "
                "layout's matching assumption (a hand-edit slip or a "
                "mis-parameterized generator call). See "
                "docs/cli/extract.md's 'Matched-device geometry check' "
                "section."
            )

    return groups, warnings


def _net_label_positions(
    l2n: kdb.LayoutToNetlist,
    circuit: kdb.Circuit,
    dbu: float,
    label_layer_index: list[int],
) -> dict[int, list[dict[str, Any]]]:
    """Per-net drawn-label geometry, keyed by ``Net.cluster_id`` (issue #1540).

    A flat extraction of a layout with internally-repeated sub-cells (e.g. a
    ring of N identical 2-input stages, each stage's output touching the
    next's input) can produce several genuinely distinct nets that all carry
    the identical ``expanded_name()`` -- the collision is structural, not a
    bug (see :func:`spice_safe_net_name`'s docstring and issue #765/#811,
    which hit the same "several distinct nets, one shared label" shape for
    parasitics ground islands). A name-keyed map cannot disambiguate them;
    this one is keyed by ``net.cluster_id`` instead -- unique across every
    net *object* in ``circuit``, exactly like ``net_id`` elsewhere in this
    module -- so a caller with independent floorplan knowledge (e.g. the
    physical (x, y) location it externally routed a wire to, in this
    top cell's own local coordinate frame) can positively identify which of
    several identically-named promoted pins is the one it means, without
    guessing from the name alone or re-running ``klt lvs`` against a
    reference schematic.

    Returns ``{cluster_id: [{"text": str, "x_um": float, "y_um": float},
    ...], ...}`` -- one entry per net that carries at least one drawn label
    on any of ``label_layer_index``'s registered layers (``deck.well_label``/
    ``poly_label``/``metal_labels``, via ``l2n.texts_of_net``); a net with no
    drawn label (an internal, unnamed node) is simply absent from the map.
    Each label's ``(x_um, y_um)`` is its anchor point (``kdb.Text.x``/``.y``),
    converted to micrometres via ``dbu`` -- the same coordinate frame
    ``black_box_regions[].bbox_um`` and ``unmodelled_poly[].bbox_um`` already
    report in. Must be called while ``l2n`` is still alive (``texts_of_net``
    is a live ``LayoutToNetlist`` API, unusable once the owning
    :func:`_extract_netlist` call returns -- the same constraint
    ``polygons_of_net`` has, see :func:`_compute_parasitics`'s docstring).

    A net with ``cluster_id == 0`` (KLayout's sentinel for "not tied to a
    layout cluster", see :func:`_compute_parasitics`'s own ``cluster_id ==
    0`` guard) is skipped -- passing it to ``texts_of_net`` would fault the
    same way it does for ``polygons_of_net``. In the rare case where more
    than one such sentinel-id net carries a label (a synthesized global net,
    e.g. a ``connect_global`` substrate tie with no drawn geometry of its
    own), skipping all of them is the correct, crash-free answer: a
    cluster-less net has no reliable single position to report by
    definition.
    """
    positions: dict[int, list[dict[str, Any]]] = {}
    for net in circuit.each_net():
        if net.cluster_id == 0:
            continue
        entries: list[dict[str, Any]] = []
        for layer_index in label_layer_index:
            texts = l2n.texts_of_net(net, layer_index, False)
            for text in texts.each():
                entries.append(
                    {
                        "text": text.string,
                        "x_um": round(text.x * dbu, 6),
                        "y_um": round(text.y * dbu, 6),
                    }
                )
        if entries:
            entries.sort(
                key=lambda entry: (entry["text"], entry["x_um"], entry["y_um"])
            )
            positions[net.cluster_id] = entries
    return positions


def _pin_index_by_net_id(circuit: kdb.Circuit) -> dict[int, int]:
    """Map each promoted pin's underlying net's ``cluster_id`` to its 0-based
    position in ``circuit.each_pin()`` order -- issue #1540.

    This is the exact order ``kdb.NetlistSpiceWriter`` writes a circuit's
    ``.SUBCKT`` header / instance-line port list in (both iterate the same
    ``Circuit.each_pin()`` sequence), so a caller can resolve a specific
    ``.SUBCKT`` port index straight back to a ``nets[]`` entry via
    ``net_id`` -- positionally, without re-matching by (possibly collided)
    name and without a separate ``klt lvs`` run against a reference
    schematic. Keyed by ``cluster_id`` rather than net identity/index so it
    composes directly with :func:`_net_label_positions`'s own keying and
    with the pre-existing ``net_id`` convention (issue #765/#811). A pin
    whose net has ``cluster_id == 0`` (see :func:`_net_label_positions`'s
    docstring) collapses onto the same dict key as any other such pin --
    an accepted, documented edge case, since a cluster-less net already has
    no position to disambiguate by either.
    """
    result: dict[int, int] = {}
    for index, pin in enumerate(circuit.each_pin()):
        net = circuit.net_for_pin(pin.id())
        if net is not None:
            result[net.cluster_id] = index
    return result


def _describe_nets(
    circuit: kdb.Circuit,
    net_label_positions: Mapping[int, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Build the response's ``nets[]`` array.

    ``net_id``/``pin_index``/``label_positions_um`` (issue #1540) are
    additive fields disclosing, per net, the exact identity KLayout's own
    ``cluster_id`` assigns it, its 0-based position in the written
    ``.SUBCKT``'s port order (``None`` when the net is not a promoted pin),
    and the drawn-label geometry that named it -- see
    :func:`_net_label_positions`/:func:`_pin_index_by_net_id`'s docstrings
    for why a caller needs these when several distinct nets collide on one
    ``name`` (a flat extraction of a layout with internally-repeated
    sub-cells, e.g. a ring of identical stages). ``net_label_positions``
    defaults to an empty mapping -- a caller that has none to offer (or a
    net with no drawn label) simply gets ``label_positions_um: []``.
    """
    pin_nets = {pin.expanded_name() for pin in _each_pin_net(circuit)}
    pin_index_by_net_id = _pin_index_by_net_id(circuit)
    label_positions = net_label_positions or {}

    nets: list[dict[str, Any]] = []
    for net in circuit.each_net():
        raw_name = net.expanded_name()
        nets.append(
            {
                "name": spice_safe_net_name(raw_name),
                "pin": raw_name in pin_nets,
                "device_count": net.terminal_count(),
                "net_id": net.cluster_id,
                "pin_index": pin_index_by_net_id.get(net.cluster_id),
                "label_positions_um": label_positions.get(net.cluster_id, []),
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


#: Every anonymous KLayout-synthesized net name reported anywhere in this
#: module's JSON (``devices[].nets[...]``, ``nets[].name``, etc.) has already
#: passed through :func:`spice_safe_net_name`, which backslash-escapes a
#: raw ``Net.expanded_name()`` leading ``$`` to match the written netlist's
#: own node spelling (issue #1162) -- so the *reported* prefix is ``\$``,
#: not KLayout's own raw ``$``.
_ANONYMOUS_NET_PREFIX = "\\$"


def _detect_unbiased_pmos_body_nets(
    devices: list[dict[str, Any]], deck: ExtractionDeck
) -> list[dict[str, Any]]:
    """Build the response's ``unbiased_pmos_body_nets[]`` array (issue #555).

    Scans the already-built ``devices[]`` array (so this reuses the exact
    terminal-net names ``_describe_devices`` already read off the netlist)
    for every PMOS device (``device["class"] == deck.pfet_class``) whose body
    terminal (``nets["b"]``) is an anonymous, KLayout-synthesized net --
    identified by ``Net.expanded_name()``'s own ``"$<n>"`` placeholder
    convention for a net with no drawn label, reported here already
    backslash-escaped to ``"\\$<n>"`` by :func:`spice_safe_net_name` (issue
    #1162, matching the written netlist's own node spelling -- the same
    convention ``tests/test_extract.py`` already asserts against, e.g.
    ``pfet["nets"]["b"].startswith("\\$")``). A *named* net -- including the
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


def _describe_parasitics_overlap_gaps(
    deck: ExtractionDeck, parasitics_deck: ParasiticsDeck
) -> list[dict[str, Any]]:
    """Build the ``parasitics.overlap_pairs_without_coefficient[]`` array
    (issue #760) -- the ``metals_without_coefficient``-style (#547) gap
    report for the vertical-overlap coupling coefficient family.

    ``_compute_parasitics`` walks ``parasitics_deck.metal_overlaps``
    index-aligned against **adjacent pairs** of ``deck.metals`` (pair ``i``
    is between metal levels ``i`` and ``i+1``) and silently contributes zero
    coupling capacitance for any adjacent pair that has no coefficient --
    either because ``metal_overlaps`` is shorter than ``len(deck.metals) -
    1`` (truncation, including the empty-tuple default every deck starts
    from) or the entry at that pair index is explicitly ``None``. Both are
    the same gap from a caller's perspective: that pair's area still charges
    to ground in full, exactly as if this feature did not exist, with
    nothing else in the JSON to say so -- a silent zero, not a silent
    approximation.

    Returns one entry per gap, ``{"lower_metal_index": int, "upper_metal_index":
    int, "lower_layer": int, "lower_datatype": int, "upper_layer": int,
    "upper_datatype": int}`` (0-based, matching ``deck.metals``' indexing --
    pair index 0 is between the deck's bottom two metal levels), sorted by
    ``lower_metal_index``. Empty when every declared adjacent metal-level
    pair has a coupling coefficient -- true for both shipped decks today
    (each curates one coefficient per adjacent pair its ``metals`` stack
    declares), and always true when ``--parasitics`` was not requested
    (callers only invoke this when ``parasitics_deck is not None``). A deck
    with fewer than two metal levels declared has no adjacent pair at all,
    so this is always empty for it, matching ``deck.metals`` having no
    consecutive-index pair to check.
    """
    gaps: list[dict[str, Any]] = []
    for i in range(len(deck.metals) - 1):
        if (
            i >= len(parasitics_deck.metal_overlaps)
            or parasitics_deck.metal_overlaps[i] is None
        ):
            lower_layer = deck.metals[i]
            upper_layer = deck.metals[i + 1]
            gaps.append(
                {
                    "lower_metal_index": i,
                    "upper_metal_index": i + 1,
                    "lower_layer": lower_layer[0],
                    "lower_datatype": lower_layer[1],
                    "upper_layer": upper_layer[0],
                    "upper_datatype": upper_layer[1],
                }
            )
    return gaps


def _each_pin_net(circuit: kdb.Circuit) -> list[kdb.Net]:
    """The distinct nets exposed as circuit pins."""
    result = []
    for pin in circuit.each_pin():
        net = circuit.net_for_pin(pin.id())
        if net is not None:
            result.append(net)
    return result
