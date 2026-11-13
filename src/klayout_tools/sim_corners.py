"""Corner-matrix expansion: the JSON wire format, ``_expand_corners``, nodesets.

Moved verbatim out of ``sim.py`` (issue #5428) so that module stays under its
per-file size ratchet. Holds the :class:`~.sim_corner_point.CornerPoint`
<-> JSON wire round-trip used by issue #906's remote-fleet shard dispatch,
the cross-product expansion that builds the corner matrix from a ``corners``
request object, and ``.nodeset`` value normalisation (its only caller among
the moved code is ``_corner_points_from_wire``).

This is the top of the corner-expansion stack: it draws on
``sim_corner_point``, ``sim_corner_variants`` and ``sim_corner_axes``, and
none of them draws on it.
"""

from __future__ import annotations

import itertools
import math
import re
from typing import Any

from . import sim_analysis_overrides as _sao
from . import sim_echo_messages, sim_supply_waveform
from ._jsonnum import is_number
from .sim_corner_axes import (
    _expand_corner_units,
    _expand_options_axis,
    _is_excluded,
    _normalize_option_row,
    _parse_process_entry,
    _validate_exclude_options,
    _validate_exclude_params,
)
from .sim_corner_point import (
    CornerPoint,
    _bind_temperature_param,
    _expand_param_axis,
    _normalize_param_row,
    _temperature_param_name,
)
from .sim_corner_variants import _parse_variants
from .sim_echo_messages import zipped_length_message
from .sim_errors import SimError


def _corner_points_to_wire(points: list[CornerPoint]) -> list[dict[str, Any]]:
    """Serialise a ``list[CornerPoint]`` verbatim, including any already-
    derived Monte Carlo seed, to the internal ``request["_explicit_points"]``
    wire shape a remote fleet shard's pushed request carries -- see
    ``_build_remote_request``'s ``explicit_points`` parameter and this
    module's ``_corner_points_from_wire`` counterpart.

    ``process_section_libs`` (issue #2522) carries each per-section model
    library **as the request declared it**, never
    ``process_section_libs_resolved``'s absolute paths: the shard resolves
    them against its own box, exactly as it resolves ``models.lib`` from the
    forwarded ``models`` block (see ``_build_remote_request``'s decision 4).
    ``None`` for every pre-#2522 corner list, so those shards' payloads are
    unchanged apart from one added null field.
    """
    wire = []
    for point in points:
        entry: dict[str, Any] = {
            "process": point.process,
            "process_sections": point.process_sections,
            "process_section_libs": point.process_section_libs,
            "supply_v": point.supply_v,
            "temperature_c": point.temperature_c,
            "sample_index": point.sample_index,
            "mc_seed": point.mc_seed,
            "variant": point.variant,
            "mc_injected": point.mc_injected,
            "supply_waveforms": list(point.supply_waveforms),
        }
        if point.params:
            # Issue #3035: only a parameter point carries the field, so every
            # request without a `corners.params` axis serialises exactly as
            # before.
            entry["params"] = point.params
        if point.nodeset:
            # Issue #3036: the already-selected map, never the rules -- a
            # shard holds a subset of the points and could not re-resolve them.
            entry["nodeset"] = point.nodeset
        if point.initial_conditions:
            # Issue #3881: likewise the selected map, never the rules.
            entry["initial_conditions"] = point.initial_conditions
        if point.analysis_args:
            entry["analysis_args"] = point.analysis_args
        if point.options:
            # Issue #3125: likewise additive, only for an options point.
            entry["options"] = point.options
        wire.append(entry)
    return wire


def _corner_points_from_wire(data: Any) -> list[CornerPoint]:
    """Inverse of :func:`_corner_points_to_wire` -- reconstructs the exact
    ``CornerPoint`` list a fleet shard was handed, seeds included, so a
    remote box's own ``run_sim`` never re-derives (and could never
    disagree about) which points are its own.
    """
    if not isinstance(data, list):
        raise SimError("request._explicit_points must be an array")
    points = []
    for entry in data:
        if not isinstance(entry, dict):
            raise SimError("request._explicit_points entries must be objects")
        sections = entry.get("process_sections")
        section_libs = entry.get("process_section_libs")
        if section_libs is not None and (
            not isinstance(sections, list)
            or not isinstance(section_libs, list)
            or len(section_libs) != len(sections)
        ):
            # Issue #2522: the two lists are parallel by construction
            # (`_parse_process_entry`), so a payload where they disagree is a
            # corrupted shard dispatch -- refuse it rather than emit a deck
            # binding some section to the wrong library.
            raise SimError(
                "request._explicit_points[].process_section_libs must be the "
                "same length as process_sections"
            )
        supply_v = entry.get("supply_v") or {}
        raw_params = entry.get("params")
        # Issue #3035: validated here again, at the receiving worker, with
        # the same rules the caller applied (a wire payload is not trusted).
        params = (
            _normalize_param_row(
                raw_params, "request._explicit_points[].params", supply_v
            )
            if raw_params is not None
            else {}
        )
        # Issue #3036: re-validated at the receiving worker as well.
        raw_nodeset = entry.get("nodeset")
        nodeset = (
            _normalize_nodeset_values(raw_nodeset, "request._explicit_points[].nodeset")
            if raw_nodeset is not None
            else {}
        )
        raw_ic = entry.get("initial_conditions")
        initial_conditions = (
            _normalize_nodeset_values(
                raw_ic, "request._explicit_points[].initial_conditions"
            )
            if raw_ic is not None
            else {}
        )
        raw_options = entry.get("options")
        options = (
            _normalize_option_row(raw_options, "request._explicit_points[].options")
            if raw_options is not None
            else {}
        )
        points.append(
            CornerPoint(
                entry.get("process"),
                supply_v,
                entry.get("temperature_c"),
                process_sections=sections,
                process_section_libs=section_libs,
                params=params,
                nodeset=nodeset,
                initial_conditions=initial_conditions,
                analysis_args=_sao.wire_args(entry),
                options=options,
                sample_index=entry.get("sample_index"),
                mc_seed=entry.get("mc_seed"),
                variant=entry.get("variant"),
                mc_injected=entry.get("mc_injected"),
                supply_waveforms=tuple(entry.get("supply_waveforms") or ()),
            )
        )
    return points


def _expand_corners(
    corners_spec: dict[str, Any], exclude_spec: list[dict[str, Any]]
) -> list[CornerPoint]:
    """Expand the declared axes into the deterministic, odometer-style corner list.

    Axis order (outer to inner, per the spike): process, then the supply
    sweep, then temperature, then ``params`` (#3035), then ``options``
    (#3125). A missing axis collapses to a single default point
    (``process=None``, ``supply_v={}``, ``temperature_c=27``) rather than an
    error -- a request that only cares about one axis need not populate the others.

    Each ``corners.process[]`` entry is either a bare string (one ``.lib``
    section, unchanged) or a ``{"name": str, "sections": [...]}`` bundle
    object (multiple ``.lib`` sections under one named corner, each
    optionally naming its own model library) -- see
    :func:`_parse_process_entry`.

    ``corners.supply_v``'s keys sweep together by index (same-length arrays;
    rails move as a set), matching the spike's documented semantics.

    ``corners.units`` (issue #2993) is the alternative, non-crossing form: an
    explicit list of points executed exactly as given -- see `_expand_corner_units`.
    """
    sim_echo_messages.require_corners_object(corners_spec)
    if "units" in corners_spec:
        return _expand_corner_units(corners_spec, exclude_spec)
    raw_processes: list[Any] = corners_spec.get("process") or [None]
    processes: list[tuple[str | None, list[str] | None, list[str | None] | None]] = [
        (None, None, None) if raw is None else _parse_process_entry(raw)
        for raw in raw_processes
    ]

    supply_spec: dict[str, list[float]] = corners_spec.get("supply_v") or {}
    if supply_spec:
        lengths = {len(values) for values in supply_spec.values()}
        if len(lengths) != 1:
            raise SimError(zipped_length_message("corners.supply_v", supply_spec))
        (count,) = lengths
        supply_points = [
            {key: values[i] for key, values in supply_spec.items()}
            for i in range(count)
        ]
    else:
        supply_points = [{}]

    supply_waveforms = sim_supply_waveform.parse_supply_waveforms(
        corners_spec, supply_spec, SimError
    )
    temperatures: list[float] = corners_spec.get("temperature_c") or [27]

    variants = _parse_variants(corners_spec.get("variants"))

    # Issue #3035: the innermost axis. Its rows are validated before the
    # exclusion rules so a selector can be checked against the declared names.
    param_rows = _expand_param_axis(corners_spec)
    temperature_param = _temperature_param_name(corners_spec)
    declared_params = set(param_rows[0]).union(filter(None, [temperature_param]))
    _validate_exclude_params(exclude_spec, declared_params)
    # Issue #3125: the solver-option axis, innermost of all.
    option_rows = _expand_options_axis(corners_spec)
    _validate_exclude_options(exclude_spec, set(option_rows[0]))

    points: list[CornerPoint] = []
    for process, process_sections, process_section_libs in processes:
        for supply_v in supply_points:
            for variant_name, variant_sources in variants:
                point_supply = {**supply_v, **variant_sources}
                for temperature_c in temperatures:
                    for param_row, option_row in itertools.product(
                        param_rows, option_rows
                    ):
                        param_row = _bind_temperature_param(
                            param_row, temperature_param, temperature_c
                        )
                        point = CornerPoint(
                            process,
                            point_supply,
                            temperature_c,
                            process_sections=process_sections,
                            process_section_libs=process_section_libs,
                            params=param_row,
                            options=option_row,
                            variant=variant_name,
                            supply_waveforms=supply_waveforms,
                        )
                        if not _is_excluded(point, exclude_spec):
                            points.append(point)
    return points


#: A node name a ``.nodeset`` card may carry: a bare SPICE identifier (with
#: ``.``/``:`` for hierarchy) or an all-digit node. Anything else -- spaces,
#: newlines, parentheses, operators -- could break out of the one generated
#: card, so it is refused rather than escaped.
_NODESET_NODE_RE = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_.:]*|[0-9]+)")


def _normalize_nodeset_values(raw: Any, where: str) -> dict[str, float]:
    """A nonempty ``{node: volts}`` map, validated: names match
    :data:`_NODESET_NODE_RE`, are lowercased, and may not repeat
    case-insensitively; values are finite numbers (booleans and nulls are
    refused). Used for the request rules and re-run at the receiving worker
    on the wire map."""
    if not isinstance(raw, dict) or not raw:
        raise SimError(f"{where} must be a nonempty object mapping nodes to volts")
    values: dict[str, float] = {}
    for node, volts in raw.items():
        if not isinstance(node, str) or not _NODESET_NODE_RE.fullmatch(node):
            raise SimError(
                f"{where} node {node!r} is not a supported node name "
                "(expected [A-Za-z_][A-Za-z0-9_.:]* or digits only)"
            )
        lowered = node.lower()
        if lowered in values:
            raise SimError(
                f"{where} names node {lowered!r} more than once "
                "(node names are case-insensitive)"
            )
        if not is_number(volts) or not math.isfinite(volts):
            raise SimError(f"{where}.{node} must be a finite number of volts")
        values[lowered] = float(volts)
    return dict(sorted(values.items()))
