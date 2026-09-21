"""The shared ``devices[]`` spec fragment: "this drawn body is not wire".

A conductor role declared in a connectivity spec (``stackup``/``vias``)
carries more than wire. A poly resistor body, a poly fuse, a MiM/MOM
capacitor plate -- each is real geometry drawn on a layer a spec must
declare if that layer carries any routing at all, and a
``LayoutToNetlist``-based connectivity model conducts straight across it.
A rail-to-rail device string (a power-on-reset divider, a brown-out
detector, a supply-referenced bias string) is then read as a dead short
between the two supplies it deliberately spans.

``klt erc`` grew the ``devices[]`` carve-out for exactly that (issue
#2183): each entry names a device-body marker layer (the same marker a PDK
deck already uses for device recognition) plus the already-declared role
the body sits on, and that geometry is subtracted from the role before
connectivity is traced.

``klt power`` declares the same kind of roles over the same geometry with
the same connectivity model, and needed the identical carve-out (issue
#2260) -- with worse consequences, because it does not merely mislabel the
net, it *solves* it: a resistor body read as wire becomes a low-resistance
path in the R network, and the IR-drop and EM verdicts are computed on a
rail that does not exist. So the schema fragment, its validation, the
geometry subtraction, and the area accounting all live here, once, and a
caller can hand the *same* ``devices[]`` declaration to either verb --
following the precedent ``_paths.py``'s own ``_validate_via_entries``
already set for the identically-shaped ``vias[]`` fragment. (The geometry
half of this needs ``_layout.region``, and ``_paths.py`` is deliberately
free of any ``klayout.db``/layout dependency -- hence a module of its own
rather than another ``_paths.py`` helper.)

**The semantics are deliberately narrow.** A ``devices[]`` entry says
"this drawn body is not wire". It does *not* say "this is a 3.4 kOhm
resistor" -- modelling the device's own impedance in ``klt power``'s
resistor network is a separate, larger question this fragment does not
answer. The body is removed from the conductor, and both terminals are
left as what they are: two separate nets.

Each caller still owns its own exception class (``ErcError`` /
``PowerError``), its own report key (``klt erc`` nests the echo under
``provenance.devices``; ``klt power`` has no ``provenance`` block and
carries a top-level ``devices``), and its own verb name in the stderr
warning -- passed in, exactly as ``_paths.py``'s helpers take
``error_cls``.
"""

from __future__ import annotations

import sys
from typing import Any

from ._layout import region as _region
from ._paths import _parse_layer_datatype


def _validate_device_entry(
    entry: Any,
    spec_path: str,
    index: int,
    conductor_names: list[str],
    error_cls: type[Exception],
) -> dict[str, Any]:
    """One ``devices[]`` entry (issue #2183). Split out of
    :func:`_validate_devices` to keep that function under the repo's C901
    complexity ratchet."""
    if not isinstance(entry, dict):
        raise error_cls(f"spec '{spec_path}': devices[{index}] must be a JSON object")
    for key in ("body_layer", "on"):
        if key not in entry:
            raise error_cls(f"spec '{spec_path}': devices[{index}] missing {key!r}")

    body_layer = _parse_layer_datatype(
        str(entry["body_layer"]), spec_path, f"devices[{index}].body_layer", error_cls
    )
    on = str(entry["on"])
    if on not in conductor_names:
        raise error_cls(
            f"spec '{spec_path}': devices[{index}].on must name a 'stackup' or "
            f"'vias' entry (got {on!r}; declared: {', '.join(conductor_names)})"
        )

    return {
        "name": str(entry.get("name", f"device{index}")),
        "body_layer": body_layer,
        "on": on,
    }


def _validate_devices(
    spec: dict[str, Any],
    spec_path: str,
    conductor_names: list[str],
    error_cls: type[Exception],
) -> list[dict[str, Any]]:
    """Validate the optional ``devices`` array (issues #2183/#2260): where a
    drawn *device body* sits on an already-declared conductor role, so the
    connectivity model stops reading it as a wire.

    Each entry is ``{"name" (optional, defaults to "device<index>"),
    "body_layer": "<layer>/<datatype>", "on": "<stackup or vias name>"}``.
    ``body_layer`` is the PDK's own device-body marker layer (gf180mcu's
    ``Resistor``/``RES_MK``/``SAB`` for a poly resistor, ``CAP_MK``/
    ``MIM_L_MK``/``FuseTop`` for a MiM cap -- the same markers a deck
    already uses for device recognition and ``klt extract`` already
    consults), and ``on`` names the role that body's geometry is drawn on:
    a ``stackup`` role for a poly/metal body, or a ``vias`` entry for a
    device whose *bridge* between two declared roles is the via-role
    geometry itself (a MiM cap's top-plate/fuse layer). ``conductor_names``
    is the caller's own ``stackup`` names followed by its ``vias`` names.
    Omitted or empty -> no carve-out, today's behaviour exactly.

    A ``body_layer`` absent from the given layout is not an error -- it
    subtracts nothing, and is reported with ``body_area_um2: 0.0`` so a
    caller can see the declaration matched no geometry -- matching the same
    convention ``stackup``/``vias`` already follow for a layer a particular
    fixture doesn't use. A ``body_layer`` that *is* drawn here but nowhere
    near its declared ``on`` role reports the same ``0.0`` (issue #2226) --
    that number is the intersection with the role, not the marker's own
    area -- plus a stderr warning, since that case is a spec bug rather
    than an unused layer."""
    raw = spec.get("devices", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise error_cls(f"spec '{spec_path}': 'devices' must be an array")

    entries: list[dict[str, Any]] = []
    names: list[str] = []
    for i, entry in enumerate(raw):
        device = _validate_device_entry(entry, spec_path, i, conductor_names, error_cls)
        if device["name"] in names:
            raise error_cls(
                f"spec '{spec_path}': duplicate device name {device['name']!r}"
            )
        names.append(device["name"])
        entries.append(device)
    return entries


def _conductor_role_layers(
    stackup: list[dict[str, Any]], vias: list[dict[str, Any]]
) -> dict[str, tuple[int, int]]:
    """``stackup``/``vias`` role name -> declared ``(layer, datatype)``, in
    declaration order -- the map :func:`_device_body_cuts` resolves each
    declared role's own conductor region from, so it can report the area a
    ``devices[]`` entry *actually* subtracted (issue #2226).

    ``klt erc`` additionally matches a curated extraction deck's own
    device-conductor layers against this map (issue #2204's exact-layer-
    equality rule), which is why it is built unconditionally there rather
    than only when a spec declares ``devices[]``."""
    layers: dict[str, tuple[int, int]] = {
        entry["name"]: entry["layer"] for entry in stackup
    }
    layers.update({via["name"]: via["layer"] for via in vias})
    return layers


def _warn_device_body_missed_role(
    verb: str, name: str, body_layer: str, role: str
) -> None:
    """The stderr warning printed once per ``devices[]`` declaration whose
    marker layer *is* drawn on this layout but does not touch the role it
    was declared ``on`` (issue #2226) -- so it subtracts nothing at all.

    That combination is almost always a spec bug (the wrong ``on`` role, or
    a marker drawn on a different datatype than the one declared) rather
    than a deliberate no-op, and the report alone makes it quiet: a
    ``body_area_um2`` of ``0.0`` is only visible to a caller who thinks to
    look. Printed to stderr, following
    :func:`klayout_tools._provenance._warn_klayout_version_mismatch`'s
    precedent, so a caller piping ``--format json`` to a file still sees it
    (JSON goes to stdout only -- ``docs/json-contract.md``). ``verb`` is the
    calling command (``"klt erc"``, ``"klt power"``), so the line names the
    tool the caller actually ran."""
    print(
        f"{verb}: warning: devices[] entry {name!r} declares body_layer "
        f"{body_layer} on role {role!r}, but that marker layer carries "
        f"geometry which does not overlap the region drawn for {role!r} -- "
        "the carve-out subtracted nothing (body_area_um2: 0.0). Check the "
        "'on' role and the marker's layer/datatype.",
        file=sys.stderr,
    )


def _device_body_cuts(
    layout: Any,
    top_cell: Any,
    devices: list[dict[str, Any]],
    role_layers: dict[str, tuple[int, int]],
    *,
    verb: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Resolve ``devices[]`` into ``(cuts, applied)`` (issues #2183/#2260).

    ``cuts`` maps a ``stackup``/``vias`` role name to the merged region of
    every declared device body on it -- what each caller subtracts from
    that role's conductor region before registering it with
    ``LayoutToNetlist``, so a drawn device body breaks the net rather than
    bridging it. Two devices declared ``on`` the same role are unioned, not
    the last one winning.

    ``applied`` is the per-declaration echo for the caller's own report
    (``klt erc``'s ``provenance.devices``, ``klt power``'s top-level
    ``devices``): name, the ``"<layer>/<datatype>"`` string as declared, the
    role, and the **actual** subtracted area in um^2.

    That last field is the honest part, and honest means *intersected*
    (issue #2226). It is ``area(marker & the role's own drawn conductor
    region)``, not ``area(marker)``: a device-body marker is conventionally
    drawn with enclosure past the conductor it marks, so the marker's own
    area over-states the carve-out for a well-formed declaration -- and,
    worse, is identically non-zero for a declaration whose ``on`` names a
    role the marker never touches, which is the one failure the field
    exists to expose. Intersected, ``0.0`` means what the docs say it
    means: *this declaration changed nothing* -- whether because its marker
    layer is absent from this layout, drawn on a different datatype, or
    declared ``on`` the wrong role.

    ``role_layers`` (from :func:`_conductor_role_layers`) supplies each
    declared role's own ``(layer, datatype)`` so that conductor region can
    be resolved here; it is read once per role that a declaration actually
    names. The intersection is computed per declaration against the role's
    *pre-cut* drawn region -- never against the accumulating ``cuts[role]``
    union -- so two declarations sharing one ``on`` each report their own
    subtracted area rather than the union's.

    ``cuts`` still carries the raw marker region: ``region - marker`` and
    ``region - (marker & region)`` are the same set, so connectivity is
    byte-identical to computing it either way. Only the reported area
    changes.

    A declaration that subtracts nothing *while its marker layer is drawn
    somewhere on this layout* additionally gets a one-line stderr warning
    (:func:`_warn_device_body_missed_role`) -- the wrong-``on`` case the
    report alone renders too quietly."""
    dbu2_um2 = layout.dbu * layout.dbu
    cuts: dict[str, Any] = {}
    applied: list[dict[str, Any]] = []
    conductors: dict[str, Any] = {}
    for device in devices:
        body = _region(layout, top_cell, device["body_layer"]).merged()
        role = device["on"]
        if role not in conductors:
            conductors[role] = _region(layout, top_cell, role_layers.get(role))
        subtracted = (body & conductors[role]).merged()
        cuts[role] = (cuts[role] + body).merged() if role in cuts else body
        layer, datatype = device["body_layer"]
        body_layer = f"{layer}/{datatype}"
        if subtracted.is_empty() and not body.is_empty():
            _warn_device_body_missed_role(verb, device["name"], body_layer, role)
        applied.append(
            {
                "name": device["name"],
                "body_layer": body_layer,
                "on": role,
                "body_area_um2": round(subtracted.area() * dbu2_um2, 9),
            }
        )
    return cuts, applied


def _cut_device_bodies(region: Any, cut: Any | None) -> Any:
    """``region`` minus the declared device bodies on the same role (issue
    #2183), or ``region`` unchanged when this role declares none."""
    if cut is None:
        return region
    return (region - cut).merged()
