"""Resolve LEF/DEF layer mappings and stage conductor geometry for top-level pins."""

from __future__ import annotations

from typing import Any


def _lef_net_layer_gds_map(
    map_path: str | None, purposes: tuple[str, ...] = ("NET",)
) -> dict[str, tuple[int, int]]:
    """``{<LEF layer name>: (<GDS layer>, <GDS datatype>)}`` for the given
    purposes of every entry in the open_pdks KLayout LEF/DEF layer-map file
    at ``map_path`` -- by default the ``NET`` purpose, i.e. the exact
    ``(layer, datatype)`` KLayout's own DEF reader draws that LEF layer's
    *routed net* geometry on, which is also the ``(layer, datatype)`` an
    extraction deck lists in its own ``metals`` (sky130: ``li1`` ->
    ``67/20``, ``met1`` -> ``68/20``, ...). A caller that needs via cut
    layers too (the ``power_stack_repair`` pass, issue #6909) passes
    ``("NET", "VIA")``: cut layers carry no ``NET`` entry in these maps.

    File shape is the same whitespace-delimited
    ``<lef_layer_name> <comma-separated purposes> <gds_layer> <gds_datatype>``
    :func:`~klayout_tools.pdk.load_gds_to_lef_layer_map` parses -- this is
    the same file read in the opposite direction (LEF name -> GDS pair,
    restricted to the purposes that name drawn geometry).

    Returns ``{}`` for ``None`` (no map file resolved) or an unreadable/
    malformed file -- callers treat that as "cannot resolve", never as an
    error, matching :func:`~klayout_tools.pdk.resolve_klayout_layer_map`'s
    own degrade-gracefully posture.
    """
    if map_path is None:
        return {}
    mapping: dict[str, tuple[int, int]] = {}
    try:
        with open(map_path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.split("#", 1)[0].split()
                if len(parts) < 4 or parts[0] in ("NAME", "DIEAREA"):
                    continue
                if not set(parts[1].split(",")) & set(purposes):
                    continue
                try:
                    mapping.setdefault(parts[0], (int(parts[-2]), int(parts[-1])))
                except ValueError:
                    continue
    except OSError:
        return {}
    return mapping


def _pin_conductor_map_entries(map_path: str | None) -> list[tuple[str, int, int]]:
    """``[(<LEF layer>, <net GDS layer>, <net GDS datatype>), ...]`` for every
    LEF layer whose KLayout layer map declares a ``NET`` purpose on one line
    and a ``PIN`` purpose on a *different* line (issue #5700).

    That shape makes KLayout's DEF reader draw a top-level ``PINS`` rectangle
    on the pin-purpose ``(layer, datatype)`` only -- never on the conductor
    pair an extraction deck connects -- so the pin's name label can sit on
    bare, non-conductor geometry and name nothing. A layer whose single line
    already lists both purposes (sky130: ``met1 LEFPIN,NET,SPNET,PIN,VIA 68
    20``), or that declares only one of them, needs nothing and is skipped.

    Returns ``[]`` for ``None`` or an unreadable/malformed file, matching
    :func:`~klayout_tools.place_and_route_gds_merge._lef_net_layer_gds_map`'s
    degrade-gracefully posture.
    """
    if map_path is None:
        return []
    net_pairs: dict[str, tuple[int, int]] = {}
    pin_pairs: dict[str, tuple[int, int]] = {}
    try:
        with open(map_path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.split("#", 1)[0].split()
                if len(parts) < 4 or parts[0] in ("NAME", "DIEAREA"):
                    continue
                purposes = parts[1].split(",")
                try:
                    pair = (int(parts[-2]), int(parts[-1]))
                except ValueError:
                    continue
                if "NET" in purposes:
                    net_pairs.setdefault(parts[0], pair)
                    # One line covering both: the pin is already on conductor.
                    if "PIN" in purposes:
                        pin_pairs.setdefault(parts[0], pair)
                elif "PIN" in purposes:
                    pin_pairs.setdefault(parts[0], pair)
    except OSError:
        return []
    return [
        (layer, net_pair[0], net_pair[1])
        for layer, net_pair in net_pairs.items()
        if layer in pin_pairs and pin_pairs[layer] != net_pair
    ]


def _stage_pin_conductor_layer_map(
    map_path: str | None, staged_path: str
) -> dict[str, Any]:
    """Write a derived copy of the layer map at ``staged_path`` that also
    draws every top-level DEF pin on its layer's conductor pair (issue #5700).

    The copy keeps every original line verbatim (the pin-purpose entries and
    every ``NAME`` label entry included -- the deck still reads the name text
    off the pin purpose) and appends one ``<layer> PIN <net_layer>
    <net_datatype>`` line per layer from :func:`_pin_conductor_map_entries`.
    KLayout then draws the pin rectangle on both pairs and the label only on
    the pin purpose. The PDK's own file is never modified.

    Returns ``{"status": "staged"|"unchanged"|"skipped", "layers": [...]}``;
    only ``"staged"`` means ``staged_path`` was written and should be used.
    """
    if map_path is None:
        return {"status": "skipped", "reason": "no layer map resolved", "layers": []}
    entries = _pin_conductor_map_entries(map_path)
    if not entries:
        return {"status": "unchanged", "layers": []}
    try:
        with open(map_path, encoding="utf-8", errors="replace") as handle:
            original = handle.read()
        addition = "".join(
            f"{layer} PIN {gds_layer} {gds_datatype}\n"
            for layer, gds_layer, gds_datatype in entries
        )
        if original and not original.endswith("\n"):
            original += "\n"
        with open(staged_path, "w", encoding="utf-8") as handle:
            handle.write(original + "# klt: top-level pins also drawn on conductor\n")
            handle.write(addition)
    except OSError as exc:
        return {"status": "skipped", "reason": f"could not stage: {exc}", "layers": []}
    return {
        "status": "staged",
        "layers": [
            {"layer_name": layer, "layer": gds_layer, "datatype": gds_datatype}
            for layer, gds_layer, gds_datatype in entries
        ],
    }
