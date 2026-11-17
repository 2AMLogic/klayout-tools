"""Validation of ``klt components``' caller-supplied conductor, via and
label-layer specs (split out of ``components.py``, issue #5552).

Pure validation: every function here turns one request list into the
normalised internal spec list ``components.run_components_report`` computes
on, or raises :class:`~klayout_tools.components_errors.ComponentsError`. No
KLayout API is touched -- a malformed request is rejected before the layout
is even opened.

Lives beside ``components.py`` rather than inside it because that module is
over the repository's per-file line limit and may not grow; the move is
verbatim, and ``components.py`` remains the only caller.
"""

from __future__ import annotations

from typing import Any

from ._jsonnum import is_int
from .components_errors import ComponentsError


def validate_conductors(conductors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not conductors:
        raise ComponentsError("conductors must contain at least one entry")

    specs: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for entry in conductors:
        name = _require_name(entry, "conductors")
        if name in seen_names:
            raise ComponentsError(f"duplicate conductor name: {name!r}")
        seen_names.add(name)
        layer = _require_layer(entry, "conductors", name)
        spec = {"name": name, "layer": layer}
        spec.update(_require_qualifiers(entry, "conductors", name))
        specs.append(spec)
    return specs


def validate_vias(
    vias: list[dict[str, Any]], conductor_specs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    conductor_names = {spec["name"] for spec in conductor_specs}

    specs: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for entry in vias:
        name = _require_name(entry, "vias")
        if name in seen_names:
            raise ComponentsError(f"duplicate via name: {name!r}")
        seen_names.add(name)
        layer = _require_layer(entry, "vias", name)

        between = entry.get("between")
        if (
            not isinstance(between, list)
            or len(between) != 2
            or not all(isinstance(v, str) for v in between)
        ):
            raise ComponentsError(
                f"via {name!r} 'between' must be a two-element list of "
                f"conductor names, got {between!r}"
            )
        conductor_a, conductor_b = between
        if conductor_a == conductor_b:
            raise ComponentsError(
                f"via {name!r} 'between' must name two different conductors, "
                f"got {between!r}"
            )
        for conductor_name in (conductor_a, conductor_b):
            if conductor_name not in conductor_names:
                raise ComponentsError(
                    f"via {name!r} 'between' references unknown conductor "
                    f"{conductor_name!r}"
                )
        spec = {"name": name, "layer": layer, "between": (conductor_a, conductor_b)}
        spec.update(_require_qualifiers(entry, "vias", name))
        specs.append(spec)
    return specs


def validate_label_layers(
    label_layers: list[dict[str, Any]], conductor_specs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    conductor_names = {spec["name"] for spec in conductor_specs}

    specs: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for entry in label_layers:
        name = _require_name(entry, "label_layers")
        if name in seen_names:
            raise ComponentsError(f"duplicate label layer name: {name!r}")
        seen_names.add(name)
        layer = _require_layer(entry, "label_layers", name)

        conductor = entry.get("conductor")
        if conductor is not None:
            if not isinstance(conductor, str) or not conductor:
                raise ComponentsError(
                    f"label_layers entry {name!r} 'conductor' must be a "
                    f"non-empty string, got {conductor!r}"
                )
            if conductor not in conductor_names:
                raise ComponentsError(
                    f"label_layers entry {name!r} 'conductor' references "
                    f"unknown conductor {conductor!r}"
                )
        specs.append({"name": name, "layer": layer, "conductor": conductor})
    return specs


def _require_name(entry: Any, field: str) -> str:
    if not isinstance(entry, dict):
        raise ComponentsError(f"each {field} entry must be an object, got {entry!r}")
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise ComponentsError(f"each {field} entry requires a non-empty 'name' string")
    return name


def _layer_pair(value: Any) -> tuple[int, int] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(map(is_int, value)):
        return (value[0], value[1])
    return None


def _require_layer(entry: dict[str, Any], field: str, name: str) -> tuple[int, int]:
    layer = entry.get("layer")
    pair = _layer_pair(layer) if isinstance(layer, list) else None
    if pair is None:
        raise ComponentsError(
            f"{field} entry {name!r} 'layer' must be a [layer, datatype] pair "
            f"of integers, got {layer!r}"
        )
    return pair


def _require_qualifiers(
    entry: dict[str, Any], field: str, name: str
) -> dict[str, list[tuple[int, int]]]:
    """Validate the optional ``and``/``not`` boolean layer lists of an entry."""
    qualifiers: dict[str, list[tuple[int, int]]] = {}
    for key in ("and", "not"):
        if key not in entry:
            continue
        value = entry[key]
        pairs = (
            [_layer_pair(v) if isinstance(v, list) else None for v in value]
            if isinstance(value, list)
            else []
        )
        if not pairs or any(p is None for p in pairs):
            raise ComponentsError(
                f"{field} entry {name!r} {key!r} must be a non-empty list of "
                f"[layer, datatype] integer pairs, got {value!r}"
            )
        qualifiers[key] = pairs  # type: ignore[assignment]
    return qualifiers
