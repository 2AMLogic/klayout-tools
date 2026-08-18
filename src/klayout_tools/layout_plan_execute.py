"""Execute a validated ``klt.layout_plan.request/1`` document into a real,
generated/placed/routed layout (issue #1155, Phase C of
``docs/design/netlist-driven-layout-spike.md`` -- read that document's
section 3 ("Execution: extend ``gen_compose.compose()``, do not build a new
engine") before touching this module; it settles the compilation strategy
below.

**What this module does, in one sentence.** It turns a plan (intent --
matched device groups, row order, abutment) plus the netlist digest it was
validated against (Phase A, :mod:`klayout_tools.netlist_digest`) into the
inputs an existing :func:`klayout_tools.gen.generate` call per group and one
existing :func:`klayout_tools.gen_compose.compose` call already know how to
consume -- never a second, parallel placement/routing engine.

**Reuses Phase B's validator, not a second validation pass.**
:func:`execute_layout_plan` takes an already-built netlist digest and calls
:func:`klayout_tools.layout_plan.validate_layout_plan` itself (the same pure
function :func:`klayout_tools.layout_plan.validate_layout_plan_document`
calls) so this module never re-implements request-shape/reference checking;
:func:`execute_layout_plan_document` is the convenience wrapper that also
builds the digest, mirroring Phase B's own two-tier API shape. It does *not*
call ``validate_layout_plan_document`` directly, because that wrapper builds
the digest internally and discards it -- and this module needs the full
digest (device terminals/params) for netlist-derived sizing and connectivity
derivation, not just the validated echo. Building the digest once here and
handing it to the *same* ``validate_layout_plan`` reference validator avoids
parsing the netlist SPICE file twice while still never duplicating any
validation logic.

**Scope decisions this phase makes** (the acceptance criteria leave several
choices to the implementer; recorded here rather than re-litigated in a PR
description alone):

1. **Netlist-derived sizing** is implemented for the device families that
   actually carry a length/width-style parameter in the Phase A digest *and*
   map one netlist device onto one generated unit device: ``mos_array``/
   ``diff_pair``/``esd_device`` (MOS ``L``/``W`` -> ``l_um``/``w_um``, or
   ``finger_width_um`` for ``esd_device``) and ``res_array`` (resistor
   ``L``/``W`` -> ``length_um``/``width_um``). ``resistor_strip`` (the
   phase-1, PDK-agnostic reference generator ``res_array`` superseded, see
   ``docs/cli/gen.md``) draws its ``num`` unit resistors as **one** 2-port
   (``P1``/``P2``) composite cell with no per-unit port at all, so it has no
   per-device mapping to derive sizing (or connectivity, scope decision 3
   below) from either -- same treatment as ``guard_ring``/``cap_array``/
   ``bond_pad``. ``bjt_array`` gets no netlist-derived sizing either -- a
   real schematic-flow bipolar device (sky130's fixed-geometry ``pnp_05v5``
   family) carries no length/width call-site parameter at all (see
   ``netlist_normalize.py``'s own "Device family coverage" note), so there
   is nothing to derive. Every one of these still accepts
   ``device_groups[].params`` overrides (or falls back to the generator's
   own documented defaults) -- only the *automatic* netlist-derived half is
   scoped out for these four families.
2. **``device_groups[].encloses`` is not consumed for automatic
   enclosure sizing/placement in this increment.** The acceptance criteria's
   checklist covers per-group generation, row-of-rows placement, abutment,
   derived connectivity, and the terminal ``compose()`` call -- it does not
   ask this phase to auto-size a ``guard_ring``-shaped group's
   ``inner_width_um``/``inner_height_um`` from the groups it encloses (the
   capability §2's field table *describes* for the full contract, but which
   this checklist does not require). So every ``device_groups[]`` entry that
   needs a placed position must appear in ``rows[]`` and/or a resolvable
   ``abutment[]`` chain anchored to a row-placed group; one that has neither
   is an application error (exit 1) rather than silently omitted from the
   composed layout.
3. **Connectivity/port derivation assumes a plan author declares
   ``device_groups[].devices`` in the same relative order the named
   generator numbers its own unit instances.** This module never
   reconstructs a generator's internal numbering (e.g. ``mos_array``'s
   ``topology: "common_centroid"`` nearest-center-first pairing) -- it
   clusters each group's own reported ``ports[]`` by terminal-name suffix,
   in the order ``generate()`` already emitted them (which is always
   per-unit-device contiguous), and maps declaration position ``k`` to the
   ``k``-th cluster. This is exact for ``topology: "array"`` (row-major, the
   same order a caller would naturally declare devices in) and for every
   non-reordering generator (``res_array``, ``cap_array``, ``bjt_array``
   with ``topology: "array"``); a ``common_centroid`` plan still resolves
   deterministically, just only correctly-paired when the author's own
   declaration order already matches the intended electrical pairing --
   not verified here, and not silently wrong either: an index past the end
   of a suffix's candidate list (e.g. every unit-topology mismatch, or
   ``mos_array``'s ``finger_topology: "series"`` mode, whose per-finger port
   names never end in a bare ``_S``/``_D``/``_G`` suffix) degrades to a
   ``warnings[]`` entry and that one pin is left off its net, never a wrong
   silent binding.
4. **Open question -- abutment vs. ``rows[].spacing_um`` conflict on the
   same pair**: resolved in favour of abutment. Row-of-rows offsets are
   computed first; ``abutment[]`` constraints are then applied as a final
   pass, translating the constrained pair along *only* the abutment's own
   axis (the perpendicular axis -- whatever a preceding row placement, or an
   earlier abutment resolution, already gave it -- is left untouched). A
   pair that is both row-adjacent and abutted therefore keeps its
   row-derived alignment on one axis and gets the abutment's exact edge/gap
   on the other; a ``warnings[]`` entry records every time abutment actually
   changes an already-placed group's offset, so the override is never
   silent.
5. **Open question -- how ``unmapped_netlist_nets[]`` treats a
   deliberately-unrouted supply net**: no ``netlist.ignore_nets[]``-style
   opt-out is added -- Phase B's already-merged schema and validator have no
   such field, and this phase does not extend the request shape to add one.
   A net every one of whose touching device terminals falls outside this
   module's known per-generator terminal map (MOS ``B``/bulk, BJT ``C``/
   collector -- both conventionally tied to a shared substrate/well tap a
   ``guard_ring``/``bjt_array`` collector ring already owns, not a
   per-device port) lands in ``unmapped_netlist_nets[]`` unconditionally,
   ``VDD``/``VSS``-style supply nets included, per the response field's own
   "always report the array, never silently drop... let the caller judge it
   benign" description in the spike.
6. **No ``klt`` CLI subcommand is added this phase**, mirroring Phase B's
   own decision and for the same reason: :func:`execute_layout_plan`/
   :func:`execute_layout_plan_document` are a complete library surface, and
   there is no clear win yet over calling this module directly from a larger
   pipeline. Revisit once a real caller wants a shell-level entry point.
7. **Routing layer/width**: Phase B's merged schema has no ``routing``
   field. This module accepts an optional, additive ``request.routing``
   field (the identical shape ``gen_compose.compose()`` already takes --
   ``layer_role``/``width_um``), defaulting to
   ``{"layer_role": "metal", "width_um": 0.17}`` when omitted. Every other
   request field is untouched -- Phase B's validator already ignores
   unknown top-level keys (``additionalProperties: true``), so a document
   carrying ``routing`` still validates unchanged against Phase B's schema.

**Exit-code trichotomy** (per the issue's acceptance criteria): this module
raises :class:`LayoutPlanExecuteError` for every application-error condition
(exit 1) -- an unresolvable generation/PDK/placement failure. There is no
usage-error tier here (exit 2 is reserved for a future CLI's own argparse
layer, per the issue; this phase adds none). Partial success (exit 3 --
placed, but ``unrouted_nets``/``unmapped_netlist_nets`` non-empty) is not an
exception at all -- it is a normal, successful return value; call
:func:`partial_success`/:func:`exit_code_for` on the response to decide.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

from . import layout_plan
from ._paths import _resolve_relative
from .gen import GenError, generate
from .gen_compose import GenComposeError, _translate_bbox, compose, compute_row_offsets
from .netlist_digest import build_netlist_digest
from .pdk import PdkNotFoundError

#: Bumped only on a non-additive (breaking) change to this module's own
#: response JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: Default routing spec applied when ``request.routing`` is omitted and
#: derived connectivity is non-empty (see the module docstring's scope
#: decision 7). ``0.17`` is the same "narrowest legal metal" default value
#: used throughout this project's own gen-compose examples/docs
#: (``docs/cli/gen-compose.md``).
_DEFAULT_ROUTING = {"layer_role": "metal", "width_um": 0.17}

#: Which digest terminal names map onto which generated-port suffix, per
#: generator -- see the module docstring's scope decision 3. A generator
#: absent from this table (``guard_ring``, ``bond_pad``, ``resistor_strip``
#: -- the last has no per-unit port at all, see scope decision 1) has no
#: per-device port concept for connectivity derivation; a terminal name
#: absent from a present generator's own sub-dict (MOS ``B``, BJT ``C``) is
#: a deliberately unmapped terminal, not an oversight -- see scope decision
#: 5.
_TERMINAL_SUFFIXES: dict[str, dict[str, str]] = {
    "mos_array": {"S": "S", "D": "D", "G": "G"},
    "diff_pair": {"S": "S", "D": "D", "G": "G"},
    "esd_device": {"S": "S", "D": "D", "G": "G"},
    "bjt_array": {"E": "E", "B": "B"},
    "res_array": {"A": "A", "B": "B"},
    "cap_array": {"A": "BOT", "B": "TOP"},
}

#: Which resolved digest params (MOS/resistor ``L``/``W``) map onto which
#: ``klt gen`` params field, per generator -- see the module docstring's
#: scope decision 1.
_SIZE_PARAM_TARGETS: dict[str, dict[str, str]] = {
    "mos_array": {"L": "l_um", "W": "w_um"},
    "diff_pair": {"L": "l_um", "W": "w_um"},
    "esd_device": {"L": "l_um", "W": "finger_width_um"},
    "res_array": {"L": "length_um", "W": "width_um"},
}

#: Opposite edge, used when resolving an ``abutment[]`` pair from the side
#: that is already placed (see :func:`_abutment_target_offset`).
_INVERT_EDGE = {"top": "bottom", "bottom": "top", "left": "right", "right": "left"}

#: Generators that actually declare a ``params.topology`` ``klt gen`` field
#: (``gen.py``'s ``_build_pcell_classes()`` -- grep the two ``"topology"``
#: PCellDeclarationHelper params there) -- see :func:`_resolve_group_params`.
#: ``diff_pair`` is deliberately excluded even though
#: ``layout_plan._GENERATOR_TOPOLOGY_SUPPORT`` accepts a
#: ``device_groups[].topology: "common_centroid"`` declaration against it:
#: ``diff_pair`` always draws a common-centroid cross-quad pattern and takes
#: no ``params.topology`` of its own (``docs/cli/gen.md``), so a declared
#: ``"common_centroid"`` topology on a ``diff_pair`` group is a plan-level
#: assertion that already matches what the generator draws, not a value that
#: needs to (or can) reach ``klt gen``'s ``params`` -- injecting it would
#: make ``gen._resolve_params()`` hard-reject the call as an unknown param
#: (issue #1160).
_TOPOLOGY_PARAM_GENERATORS = frozenset({"mos_array", "bjt_array"})


class LayoutPlanExecuteError(Exception):
    """Raised when a validated plan cannot be executed: an unresolvable PDK,
    a generation failure (invalid resolved ``params``), a ``device_groups[]``
    entry with no reachable placement (no ``rows[]`` entry and no resolvable
    ``abutment[]`` chain), or a ``gen_compose.compose()`` failure. Exit code
    1 -- see the module docstring's "Exit-code trichotomy" note.
    """


def partial_success(response: dict[str, Any]) -> bool:
    """``True`` when ``response`` placed everything but left at least one
    net unrouted or unmapped -- the exit-3 condition (see the module
    docstring).
    """
    return bool(response.get("unrouted_nets")) or bool(
        response.get("unmapped_netlist_nets")
    )


def exit_code_for(response: dict[str, Any]) -> int:
    """Map an :func:`execute_layout_plan`/:func:`execute_layout_plan_document`
    response to the exit code a caller (e.g. a future CLI wrapper) should
    use: ``3`` for partial success, ``0`` otherwise. A raised
    :class:`LayoutPlanExecuteError` (or
    :class:`klayout_tools.layout_plan.LayoutPlanError`) is always exit 1,
    outside this function's scope (there is no response to inspect).
    """
    return 3 if partial_success(response) else 0


def _resolve_routing_spec(raw: Any) -> dict[str, Any]:
    if raw is None:
        return dict(_DEFAULT_ROUTING)
    if not isinstance(raw, dict):
        raise LayoutPlanExecuteError("request.routing must be a JSON object when given")

    layer_role = raw.get("layer_role", _DEFAULT_ROUTING["layer_role"])
    if not isinstance(layer_role, str) or not layer_role:
        raise LayoutPlanExecuteError(
            "request.routing.layer_role must be a non-empty string when given"
        )
    width_um = raw.get("width_um", _DEFAULT_ROUTING["width_um"])
    if (
        isinstance(width_um, bool)
        or not isinstance(width_um, (int, float))
        or width_um <= 0
    ):
        raise LayoutPlanExecuteError(
            "request.routing.width_um must be a positive number when given"
        )
    return {"layer_role": layer_role, "width_um": float(width_um)}


def _netlist_derived_size_params(
    generator: str, device_params: dict[str, Any]
) -> dict[str, float]:
    """Pick the ``L``/``W``-style digest params a given generator's own
    ``klt gen`` params can absorb, per :data:`_SIZE_PARAM_TARGETS`.

    Only a **positive** digest value counts as "the netlist specified this
    device's geometry" -- ``NetlistSpiceReader`` reports ``L``/``W`` as
    ``0.0`` (KLayout's own structural default for an unset device-class
    param) for a resistor written in the bare, purely-electrical
    ``R<name> n1 n2 <ohms>`` SPICE form (no ``L=``/``W=`` at all), which is
    "the netlist did not specify a geometric size for this device," not "the
    netlist specifies a zero-sized device" -- treating it as the latter
    would hand a real generator a rejected (``> 0`` required) param instead
    of falling back to its own documented default.
    """
    targets = _SIZE_PARAM_TARGETS.get(generator)
    if not targets:
        return {}
    resolved: dict[str, float] = {}
    for digest_key, gen_param in targets.items():
        value = device_params.get(digest_key)
        is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
        if is_number and value > 0:
            resolved[gen_param] = float(value)
    return resolved


def _extract_raw_params_by_id(request: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``{device_groups[].id: device_groups[].params}`` read from the
    **original** request, not the validated response.

    :func:`klayout_tools.layout_plan.validate_layout_plan`'s own response
    deliberately echoes only the fields it validates in detail (``id``,
    ``generator``, ``topology``, ``devices``, ``encloses``) -- ``params`` is
    checked for JSON-object shape only and not echoed back, so this module
    reads it from ``request`` directly (already known well-typed, since it
    passed Phase B's own validation) rather than trying to smuggle it
    through the validated response.
    """
    raw_groups = request.get("device_groups")
    if not isinstance(raw_groups, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for raw_group in raw_groups:
        if not isinstance(raw_group, dict):
            continue
        group_id = raw_group.get("id")
        params = raw_group.get("params")
        if isinstance(group_id, str) and isinstance(params, dict):
            result[group_id] = params
    return result


def _resolve_group_params(
    group: dict[str, Any],
    digest_devices_by_key: dict[tuple[str, str], dict[str, Any]],
    overrides: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Resolve one ``device_groups[]`` entry's ``klt gen`` ``params`` --
    netlist-derived sizing first (from every device the group declares, per
    the module docstring's scope decision 1), then ``overrides`` (the
    group's own ``params``, read from the original request -- see
    :func:`_extract_raw_params_by_id`) layered on top, every divergence
    between the two surfaced as a ``warnings[]`` entry (never silent, per
    the spike's own precedence rule).
    """
    warnings: list[str] = []
    derived: dict[str, float] = {}

    for device_ref in group["devices"]:
        key = (device_ref["name"], device_ref["device_class"])
        digest_device = digest_devices_by_key.get(key)
        if digest_device is None:
            continue
        for gen_param, value in _netlist_derived_size_params(
            group["generator"], digest_device["params"]
        ).items():
            if gen_param not in derived:
                derived[gen_param] = value
            elif abs(derived[gen_param] - value) > 1e-9:
                warnings.append(
                    f"device_groups (id '{group['id']}'): devices disagree on "
                    f"netlist-derived '{gen_param}' (device '{device_ref['name']}' "
                    f"reports {value}, an earlier device in this group already "
                    f"resolved {derived[gen_param]}) -- using {derived[gen_param]}"
                )

    resolved = dict(derived)
    # `device_groups[].topology` is a top-level plan field (Phase B's own
    # matching-pattern concept, already validated against the named
    # generator's own support table) but only `mos_array`/`bjt_array` accept
    # it as an ordinary `params.topology` -- map it here so a plan's
    # topology declaration actually reaches those generators, rather than
    # silently falling back to the generator's own default topology.
    # `params` overrides (below) still win if a caller redundantly repeats
    # it there.
    #
    # A generator outside `_TOPOLOGY_PARAM_GENERATORS` (today, only
    # `diff_pair`) that still declares a topology is left alone here: Phase
    # B's own `layout_plan._GENERATOR_TOPOLOGY_SUPPORT` already limits which
    # generator/value pairs reach this point at all, and the one pair it
    # allows through for such a generator -- `diff_pair`/`common_centroid`
    # -- is a plan-level assertion that already matches what `diff_pair`
    # always draws, so accepting it silently (no `params` injection, no
    # warning) is correct rather than a silent drop of a value that would
    # otherwise change the drawn layout. If a future generator is added to
    # `_GENERATOR_TOPOLOGY_SUPPORT` with a *non-default* topology value that
    # it cannot accept via `params.topology`, that combination would need a
    # `warnings[]` entry here instead of silent acceptance -- no such case
    # exists today.
    if group.get("topology") and group["generator"] in _TOPOLOGY_PARAM_GENERATORS:
        resolved["topology"] = group["topology"]
    for param_name, value in overrides.items():
        if param_name in derived and derived[param_name] != value:
            warnings.append(
                f"device_groups (id '{group['id']}').params.{param_name} override "
                f"({value!r}) diverges from the netlist-derived value "
                f"({derived[param_name]!r})"
            )
        resolved[param_name] = value

    return resolved, warnings


def _device_owned_port_names(ports: list[dict[str, Any]], suffix: str) -> list[str]:
    """Every ``ports[]`` entry whose name ends with ``_<suffix>``, in the
    order ``generate()`` reported them -- see the module docstring's scope
    decision 3 for why this is generator-agnostic (no hardcoded name
    prefix) and why it degrades gracefully (rather than mis-binding) for a
    port-naming shape it does not expect (e.g. ``mos_array``'s
    ``finger_topology: "series"`` mode).
    """
    marker = f"_{suffix}"
    return [
        port["name"]
        for port in ports
        if isinstance(port, dict)
        and isinstance(port.get("name"), str)
        and port["name"].endswith(marker)
    ]


def _build_connectivity(
    device_groups: list[dict[str, Any]],
    digest: dict[str, Any],
    generated: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Derive ``gen_compose``-shaped ``connectivity[]`` from the netlist
    digest's own device-to-net map (the module docstring's core job) --
    returns ``(connectivity, unmapped_netlist_nets, warnings)``.

    A net with two resolved pins becomes an ordinary two-pin
    ``connectivity[]`` entry; a net with three or more becomes a bundle
    entry -- both go through the exact same list shape, since
    ``gen_compose.compose()`` already routes every ``connectivity[]`` entry
    through :func:`klayout_tools.gen_compose.route_bundle` regardless of pin
    count (issue #1073) -- this module never calls ``route_bundle`` or
    ``route_two_pin`` itself.
    """
    digest_devices_by_key = {
        (d["name"], d["device_class"]): d for d in digest["devices"]
    }
    warnings: list[str] = []

    # Per group, per terminal suffix: the ordered candidate port names
    # (see _device_owned_port_names). Built once per group up front.
    candidates_by_group: dict[str, dict[str, list[str]]] = {}
    for group in device_groups:
        suffix_map = _TERMINAL_SUFFIXES.get(group["generator"])
        if suffix_map is None:
            if group["devices"]:
                warnings.append(
                    f"device_groups (id '{group['id']}'): generator "
                    f"'{group['generator']}' has no known per-device "
                    "port-naming convention for connectivity derivation -- "
                    "its declared devices[] will not be wired"
                )
            continue
        ports = generated[group["id"]].get("ports") or []
        candidates_by_group[group["id"]] = {
            suffix: _device_owned_port_names(ports, suffix)
            for suffix in set(suffix_map.values())
        }

    # net name -> ordered, deduplicated list of {"block", "port"} pins.
    net_pins: dict[str, list[dict[str, str]]] = {}
    net_pin_seen: dict[str, set[tuple[str, str]]] = {}

    for group in device_groups:
        suffix_map = _TERMINAL_SUFFIXES.get(group["generator"])
        if suffix_map is None:
            continue
        candidates = candidates_by_group[group["id"]]
        for index, device_ref in enumerate(group["devices"]):
            digest_device = digest_devices_by_key.get(
                (device_ref["name"], device_ref["device_class"])
            )
            if digest_device is None:
                continue
            for terminal_name, net_name in digest_device["terminals"].items():
                if net_name is None:
                    continue
                suffix = suffix_map.get(terminal_name)
                if suffix is None:
                    continue
                port_names = candidates.get(suffix, [])
                if index >= len(port_names):
                    warnings.append(
                        f"device_groups (id '{group['id']}'): could not "
                        f"resolve a generated port for device "
                        f"'{device_ref['name']}' (device_class "
                        f"'{device_ref['device_class']}') terminal "
                        f"'{terminal_name}' -- generator produced fewer "
                        f"'{suffix}'-suffixed ports than declared devices"
                    )
                    continue
                port_name = port_names[index]
                seen = net_pin_seen.setdefault(net_name, set())
                pin_key = (group["id"], port_name)
                if pin_key in seen:
                    continue
                seen.add(pin_key)
                net_pins.setdefault(net_name, []).append(
                    {"block": group["id"], "port": port_name}
                )

    connectivity: list[dict[str, Any]] = []
    for net in digest["nets"]:
        pins = net_pins.get(net["name"], [])
        if len(pins) >= 2:
            connectivity.append({"net": net["name"], "pins": pins})

    unmapped_netlist_nets = sorted(
        net["name"] for net in digest["nets"] if not net_pins.get(net["name"])
    )

    return connectivity, unmapped_netlist_nets, warnings


def _abutment_target_offset(
    edge: str,
    gap_um: float,
    anchor_bbox_raw: dict[str, float],
    anchor_offset: dict[str, float],
    target_bbox_raw: dict[str, float],
    target_offset_hint: dict[str, float] | None,
) -> dict[str, float]:
    """The other side's ``offset_um`` so its bbox sits exactly ``gap_um``
    past the anchor's ``edge`` (see the module docstring's scope decision
    4). ``target_offset_hint`` -- the target's own already-placed offset,
    when it has one -- supplies the perpendicular axis unchanged; ``None``
    (never placed yet) centers the target on the anchor along that axis
    instead.
    """
    anchor_bbox = _translate_bbox(anchor_bbox_raw, anchor_offset)
    offset = (
        dict(target_offset_hint)
        if target_offset_hint is not None
        else {
            "x": 0.0,
            "y": 0.0,
        }
    )

    def _center_perp(axis: str) -> float:
        if axis == "x":
            anchor_center = (anchor_bbox["x0"] + anchor_bbox["x1"]) / 2
            target_center = (target_bbox_raw["x0"] + target_bbox_raw["x1"]) / 2
        else:
            anchor_center = (anchor_bbox["y0"] + anchor_bbox["y1"]) / 2
            target_center = (target_bbox_raw["y0"] + target_bbox_raw["y1"]) / 2
        return anchor_center - target_center

    if edge == "top":
        offset["y"] = (anchor_bbox["y1"] + gap_um) - target_bbox_raw["y0"]
        if target_offset_hint is None:
            offset["x"] = _center_perp("x")
    elif edge == "bottom":
        offset["y"] = (anchor_bbox["y0"] - gap_um) - target_bbox_raw["y1"]
        if target_offset_hint is None:
            offset["x"] = _center_perp("x")
    elif edge == "right":
        offset["x"] = (anchor_bbox["x1"] + gap_um) - target_bbox_raw["x0"]
        if target_offset_hint is None:
            offset["y"] = _center_perp("y")
    else:  # "left"
        offset["x"] = (anchor_bbox["x0"] - gap_um) - target_bbox_raw["x1"]
        if target_offset_hint is None:
            offset["y"] = _center_perp("y")
    return offset


def _resolve_placement(
    rows: list[dict[str, Any]],
    abutment: list[dict[str, Any]],
    bboxes_um: dict[str, dict[str, float]],
    group_ids: set[str],
) -> tuple[dict[str, dict[str, float]], list[str]]:
    """Row-of-rows placement (compiled onto ``gen_compose.compute_row_offsets``
    per row, then stacked vertically by each preceding row's tallest group),
    followed by ``abutment[]`` applied as a final constraint pass -- see the
    module docstring's "core idea" and scope decision 4.

    Raises :class:`LayoutPlanExecuteError` if any ``group_ids`` entry is
    unreachable from ``rows[]``/``abutment[]`` (scope decision 2).
    """
    warnings: list[str] = []
    offsets_um: dict[str, dict[str, float]] = {}

    cumulative_y = 0.0
    for row in rows:
        order = row["order"]
        x_offsets = compute_row_offsets(order, bboxes_um, row["spacing_um"])
        row_height = max(
            bboxes_um[group_id]["y1"] - bboxes_um[group_id]["y0"] for group_id in order
        )
        align = row["align"]
        for group_id in order:
            bbox = bboxes_um[group_id]
            if align == "bottom":
                y = cumulative_y - bbox["y0"]
            elif align == "top":
                y = (cumulative_y + row_height) - bbox["y1"]
            else:  # "center"
                y = (cumulative_y + row_height / 2) - (bbox["y0"] + bbox["y1"]) / 2
            offsets_um[group_id] = {"x": x_offsets[group_id]["x"], "y": y}
        cumulative_y += row_height

    pending = list(abutment)
    made_progress = True
    while pending and made_progress:
        made_progress = False
        still_pending: list[dict[str, Any]] = []
        for entry in pending:
            a_id, b_id = entry["a"], entry["b"]
            a_offset = offsets_um.get(a_id)
            b_offset = offsets_um.get(b_id)
            if a_offset is not None:
                new_b_offset = _abutment_target_offset(
                    entry["edge"],
                    entry["gap_um"],
                    bboxes_um[a_id],
                    a_offset,
                    bboxes_um[b_id],
                    b_offset,
                )
                if b_offset is not None and new_b_offset != b_offset:
                    warnings.append(
                        f"abutment (a: '{a_id}', b: '{b_id}', edge: "
                        f"'{entry['edge']}') overrides device_groups "
                        f"'{b_id}''s row-derived placement"
                    )
                offsets_um[b_id] = new_b_offset
                made_progress = True
            elif b_offset is not None:
                new_a_offset = _abutment_target_offset(
                    _INVERT_EDGE[entry["edge"]],
                    entry["gap_um"],
                    bboxes_um[b_id],
                    b_offset,
                    bboxes_um[a_id],
                    None,
                )
                offsets_um[a_id] = new_a_offset
                made_progress = True
            else:
                still_pending.append(entry)
        pending = still_pending

    unresolved = sorted(group_ids - set(offsets_um))
    if unresolved:
        raise LayoutPlanExecuteError(
            "device_groups "
            + ", ".join(repr(group_id) for group_id in unresolved)
            + " could not be placed -- no rows[] entry and no resolvable "
            "abutment[] chain reaches a placed group (device_groups[]."
            "encloses alone does not place a group at this phase; see "
            "device_groups[]'s own row/abutment placement guidance)"
        )
    return offsets_um, warnings


def execute_layout_plan(
    request: dict[str, Any],
    digest: dict[str, Any],
    *,
    request_dir: str | None = None,
    work_dir: str | None = None,
) -> dict[str, Any]:
    """Execute an already-parsed ``klt.layout_plan.request/1`` document
    against ``digest`` (a :func:`klayout_tools.netlist_digest.build_netlist_digest`
    result the request's own ``netlist`` fields were ingested from).

    Runs :func:`klayout_tools.layout_plan.validate_layout_plan` first (never
    a second, independent validation pass -- see the module docstring),
    then, per ``device_groups[]`` entry: resolves netlist-derived +
    override ``params`` and calls :func:`klayout_tools.gen.generate`;
    compiles ``rows[]``/``abutment[]`` into one ``placement.strategy:
    "explicit"`` offset map; derives ``connectivity[]`` from the digest's
    device-to-net map; and calls :func:`klayout_tools.gen_compose.compose`
    for the actual generation/placement/routing pass.

    ``work_dir`` is the directory each per-group ``klt gen`` GDS artifact is
    written to before ``compose()`` reads them back in -- defaults to a
    fresh, auto-cleaned temporary directory (the composed output at
    ``options.output`` is fully self-contained afterwards, so nothing under
    ``work_dir`` needs to survive the call); pass an explicit directory to
    inspect the intermediate per-group artifacts (it is never cleaned up in
    that case).

    Returns the response envelope described in the module docstring (see
    also ``docs/cli/layout-plan-execute.md``). Raises
    :class:`klayout_tools.layout_plan.LayoutPlanError` for a plan-shape/
    reference problem (Phase B's own validation), or
    :class:`LayoutPlanExecuteError` for an execution-time failure.
    """
    validated = layout_plan.validate_layout_plan(request, digest)

    device_groups = validated["device_groups"]
    if not device_groups:
        raise LayoutPlanExecuteError(
            "request.device_groups is empty -- nothing to generate or place"
        )

    routing_spec = _resolve_routing_spec(request.get("routing"))
    options = validated["options"]
    pdk_spec = validated["pdk"]

    warnings: list[str] = []
    raw_params_by_id = _extract_raw_params_by_id(request)

    def _run(work_directory: str) -> dict[str, Any]:
        digest_devices_by_key = {
            (d["name"], d["device_class"]): d for d in digest["devices"]
        }

        generated: dict[str, dict[str, Any]] = {}
        resolved_params_by_id: dict[str, dict[str, Any]] = {}
        for group in device_groups:
            params, param_warnings = _resolve_group_params(
                group, digest_devices_by_key, raw_params_by_id.get(group["id"], {})
            )
            warnings.extend(param_warnings)
            resolved_params_by_id[group["id"]] = params
            gen_request = {
                "generator": group["generator"],
                "pdk": pdk_spec,
                "params": params,
                "options": {
                    "cell_name": f"{group['id']}_0",
                    "output": os.path.join(work_directory, f"{group['id']}.gds"),
                },
            }
            try:
                generated[group["id"]] = generate(gen_request)
            except GenError as exc:
                raise LayoutPlanExecuteError(
                    f"device_groups (id '{group['id']}'): generation failed: {exc}"
                ) from exc
            except PdkNotFoundError as exc:
                raise LayoutPlanExecuteError(str(exc)) from exc

        bboxes_um = {
            group["id"]: generated[group["id"]]["bbox_um"] for group in device_groups
        }
        group_ids = {group["id"] for group in device_groups}
        offsets_um, placement_warnings = _resolve_placement(
            validated["rows"], validated["abutment"], bboxes_um, group_ids
        )
        warnings.extend(placement_warnings)

        connectivity, unmapped_netlist_nets, connectivity_warnings = (
            _build_connectivity(device_groups, digest, generated)
        )
        warnings.extend(connectivity_warnings)

        order = [group["id"] for group in device_groups]
        compose_request: dict[str, Any] = {
            "pdk": pdk_spec,
            "blocks": [
                {"id": group_id, "generator_report": generated[group_id]}
                for group_id in order
            ],
            "placement": {
                "strategy": "explicit",
                "order": order,
                "origins_um": offsets_um,
            },
            "connectivity": connectivity,
            "options": {
                "cell_name": options.get("cell_name") or "layout_plan_0",
                "output": options.get("output"),
            },
        }
        if connectivity:
            compose_request["routing"] = routing_spec

        try:
            composed = compose(compose_request, request_dir=request_dir)
        except GenComposeError as exc:
            raise LayoutPlanExecuteError(f"composition failed: {exc}") from exc

        blocks_by_id = {block["id"]: block for block in composed["blocks"]}
        device_groups_response = [
            {
                "id": group["id"],
                "generator": group["generator"],
                "devices": group["devices"],
                "resolved_params": resolved_params_by_id[group["id"]],
                "offset_um": blocks_by_id[group["id"]]["offset_um"],
                "bbox_um": blocks_by_id[group["id"]]["bbox_um"],
            }
            for group in device_groups
        ]

        return {
            "schema_version": SCHEMA_VERSION,
            "cell_name": composed["cell_name"],
            "gds_path": composed["gds_path"],
            "pdk": composed["pdk"],
            "bbox_um": composed["bbox_um"],
            "device_groups": device_groups_response,
            "nets": composed["nets"],
            "pins": composed["pins"],
            "unrouted_nets": composed["unrouted_nets"],
            "unmapped_netlist_nets": unmapped_netlist_nets,
            "drc_hints": composed["drc_hints"],
            "warnings": [*warnings, *composed["warnings"]],
        }

    if work_dir is not None:
        os.makedirs(work_dir, exist_ok=True)
        return _run(work_dir)

    with tempfile.TemporaryDirectory(prefix="klt-layout-plan-") as tmp_dir:
        return _run(tmp_dir)


def execute_layout_plan_document(
    request: dict[str, Any],
    *,
    request_dir: str | None = None,
    work_dir: str | None = None,
) -> dict[str, Any]:
    """:func:`execute_layout_plan`, but ingesting ``request.netlist`` itself
    (via :func:`klayout_tools.netlist_digest.build_netlist_digest`) rather
    than requiring the caller to have already built a digest -- mirrors
    :func:`klayout_tools.layout_plan.validate_layout_plan_document`'s own
    convenience-wrapper shape.

    A netlist that fails to ingest is a
    :class:`klayout_tools.layout_plan.LayoutPlanError` (exit 1), the same
    treatment :func:`klayout_tools.layout_plan.validate_layout_plan_document`
    already gives this case.
    """
    if not isinstance(request, dict):
        raise layout_plan.LayoutPlanUsageError("request must be a JSON object")
    if "netlist" not in request:
        raise layout_plan.LayoutPlanUsageError(
            "request is missing required field: netlist"
        )

    netlist_spec = layout_plan._parse_netlist(request["netlist"])
    resolved_path = _resolve_relative(netlist_spec["path"], request_dir or os.getcwd())

    from .lvs import LvsError

    try:
        digest = build_netlist_digest(
            resolved_path,
            top=netlist_spec["top"],
            form=netlist_spec["form"],
            deck=netlist_spec["deck"],
            device_map=netlist_spec["device_map"],
        )
    except LvsError as exc:
        raise layout_plan.LayoutPlanError(
            f"request.netlist could not be ingested: {exc}"
        ) from exc

    return execute_layout_plan(
        request, digest, request_dir=request_dir, work_dir=work_dir
    )
