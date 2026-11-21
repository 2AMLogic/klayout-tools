"""Corner axes beside ``params``: options, process, units, and the exclude matchers.

Moved verbatim out of ``sim.py`` (issue #5428) so that module stays under its
per-file size ratchet. Holds the ``corners.options`` allow-list and axis,
``corners.process`` entry/section parsing, ``corners.units`` expansion, and
the ``exclude`` selector validation and matchers.
"""

from __future__ import annotations

from typing import Any

from . import sim_supply_waveform
from ._jsonnum import is_number
from .sim_corner_point import (
    CornerPoint,
    _format_params_label,
    _validate_param_scalar,
)
from .sim_echo_messages import zipped_length_message
from .sim_errors import SimError

#: ``corners.options`` (issue #3125): the numeric solver ``.options`` a
#: corner axis may sweep. A subset of :data:`NGSPICE_SOLVER_OPTIONS` -- the
#: flag/token options (``method``, ``noopiter``) are not numbers -- plus
#: ``itl3``. ngspice silently ignores an unknown ``.options`` name, so the
#: allow-list turns a typo into a named error.
CORNER_OPTION_NAMES = (
    "abstol",
    "gmin",
    "gminsteps",
    "itl1",
    "itl2",
    "itl3",
    "itl4",
    "pivtol",
    "reltol",
    "srcsteps",
    "vntol",
)


def _validate_corner_option_name(name: Any, where: str) -> str:
    lowered = name.lower() if isinstance(name, str) else name
    if lowered not in CORNER_OPTION_NAMES:
        raise SimError(
            f"{where} name {name!r} is not a supported numeric solver "
            f"option; supported: {', '.join(CORNER_OPTION_NAMES)}"
        )
    return lowered


def _expand_options_axis(
    corners_spec: dict[str, Any],
) -> list[dict[str, int | float]]:
    """The zipped ``corners.options`` rows (issue #3125), or ``[{}]`` for a
    request with no options axis. Same shape and rules as
    :func:`_expand_param_axis`: names zip by index, every array nonempty and
    the same length, duplicate rows rejected."""
    spec = corners_spec.get("options")
    if spec is None or spec == {}:
        return [{}]
    if not isinstance(spec, dict):
        raise SimError(
            "corners.options must be an object mapping solver option names "
            "to equal-length arrays of numbers"
        )
    columns: dict[str, list[int | float]] = {}
    for name, values in spec.items():
        lowered = _validate_corner_option_name(name, "corners.options")
        if lowered in columns:
            raise SimError(
                f"corners.options declares {lowered!r} more than once "
                "(names are case-insensitive)"
            )
        if not isinstance(values, list) or not values:
            raise SimError(
                f"corners.options.{name} must be a nonempty array of numbers"
            )
        columns[lowered] = [
            _validate_param_scalar(value, f"corners.options.{name}[{i}]")
            for i, value in enumerate(values)
        ]
    if len({len(values) for values in columns.values()}) != 1:
        raise SimError(zipped_length_message("corners.options", columns))
    count = len(next(iter(columns.values())))
    rows = [
        dict(sorted((name, values[i]) for name, values in columns.items()))
        for i in range(count)
    ]
    seen: set[str] = set()
    for row in rows:
        label = _format_params_label(row)
        if label in seen:
            raise SimError(f"corners.options declares the row {label!r} twice")
        seen.add(label)
    return rows


def _normalize_option_row(raw: Any, where: str) -> dict[str, int | float]:
    """One scalar ``{option: number}`` row (a report/wire ``options`` map),
    validated and normalized."""
    if not isinstance(raw, dict):
        raise SimError(f"{where} must be an object mapping names to numbers")
    row: dict[str, int | float] = {}
    for name, value in raw.items():
        lowered = _validate_corner_option_name(name, where)
        if lowered in row:
            raise SimError(f"{where} declares {lowered!r} more than once")
        row[lowered] = _validate_param_scalar(value, f"{where}.{name}")
    return dict(sorted(row.items()))


def _parse_process_entry(
    entry: Any,
) -> tuple[str | None, list[str] | None, list[str | None] | None]:
    """Normalize one ``corners.process[]`` entry into ``(name, sections,
    section_libs)``.

    A bare string is today's single-``.lib``-section corner (e.g. sky130's
    ``"tt"``): returns ``(entry, None, None)`` -- ``process_sections=None``
    signals :func:`_write_corner_deck` to emit its historical single ``.lib``
    line, byte-for-byte unchanged from before this function existed.

    An object ``{"name": str, "sections": [...]}`` is a multi-section corner
    *bundle* -- e.g. gf180mcu's ``sm141064.ngspice``, which has no all-device
    corner sections and instead needs one ``.lib`` card per device family
    (MOS + ``bjt_*`` + ``diode_*`` + ``res_*`` + ``moscap_*`` + ``mimcap_*``)
    to fully select a named corner. :func:`_write_corner_deck` emits one
    ``.lib`` line per section, in declaration order (ordering matters -- the
    gf180 section set has interdependent global switch params).

    Each ``sections[]`` entry is either:

    - a **bare string** -- that section is read from the request's own
      ``models.lib``, exactly as before issue #2522; or
    - an object ``{"lib": str, "section": str}`` -- that section is read
      from ``lib`` instead, for a PDK that ships one corner library *per
      device family* rather than one file carrying every section. Returned
      as the third element, a list parallel to ``sections`` holding the
      declared ref (or ``None`` for a bare-string section).

    ``section_libs`` is ``None`` -- not a list of ``None``s -- when no entry
    used the object form, so an all-bare-string bundle produces exactly the
    ``CornerPoint`` (and fleet wire payload) it produced before #2522.
    """
    if isinstance(entry, str):
        return entry, None, None
    if isinstance(entry, dict):
        name = entry.get("name")
        sections = entry.get("sections")
        if not isinstance(name, str) or not name:
            raise SimError(
                "corners.process bundle entry requires a non-empty string 'name'"
            )
        if not isinstance(sections, list) or not sections:
            raise SimError(
                "corners.process bundle entry requires a non-empty list "
                '\'sections\' of section names or {"lib", "section"} objects'
            )
        parsed = [_parse_process_section(section) for section in sections]
        section_names = [section for section, _ in parsed]
        section_libs = [lib for _, lib in parsed]
        if all(lib is None for lib in section_libs):
            return name, section_names, None
        return name, section_names, section_libs
    raise SimError(
        "corners.process entries must be a string or an object "
        '{"name": str, "sections": list[str | {"lib": str, "section": str}]}'
    )


def _parse_process_section(section: Any) -> tuple[str, str | None]:
    """One ``corners.process[].sections[]`` entry -> ``(section name, library
    ref or None)``.

    A bare string names a section of the request's own ``models.lib``
    (``None`` library ref -- today's behaviour); an object
    ``{"lib": str, "section": str}`` names the model library that section is
    read from instead (issue #2522). The ref is returned exactly as declared;
    :func:`_resolve_corner_section_libs` is what resolves and
    existence-checks it.
    """
    if isinstance(section, str) and section:
        return section, None
    if not isinstance(section, dict):
        raise SimError(
            "corners.process bundle 'sections' entries must be a "
            'non-empty string or an object {"lib": str, "section": str}'
        )
    lib = section.get("lib")
    name = section.get("section")
    if not isinstance(lib, str) or not lib:
        raise SimError(
            "corners.process bundle per-section entry requires a non-empty "
            "string 'lib' (the model library this section is read from)"
        )
    if not isinstance(name, str) or not name:
        raise SimError(
            "corners.process bundle per-section entry requires a non-empty "
            "string 'section'"
        )
    return name, lib


def _validate_exclude_params(
    exclude_spec: list[dict[str, Any]], declared: set[str]
) -> None:
    """Reject an ``exclude[].params`` selector naming a parameter the request
    does not sweep (issue #3035) -- it could never match, so it would
    silently exclude nothing."""
    for entry in exclude_spec:
        if not isinstance(entry, dict) or "params" not in entry:
            continue
        selector = entry["params"]
        if not isinstance(selector, dict):
            raise SimError("exclude[].params must be an object of name: number")
        for name, value in selector.items():
            lowered = name.lower() if isinstance(name, str) else name
            if lowered not in declared:
                raise SimError(
                    f"exclude[].params names {name!r}, which is not a "
                    "corners.params parameter of this request"
                    + (
                        f" (declared: {', '.join(sorted(declared))})"
                        if declared
                        else ""
                    )
                )
            _validate_param_scalar(value, f"exclude[].params.{name}")


def _validate_exclude_options(
    exclude_spec: list[dict[str, Any]], declared: set[str]
) -> None:
    """Reject an ``exclude[].options`` selector naming an option the request
    does not sweep (issue #3125) -- it could never match."""
    for entry in exclude_spec:
        if not isinstance(entry, dict) or "options" not in entry:
            continue
        selector = entry["options"]
        if not isinstance(selector, dict):
            raise SimError("exclude[].options must be an object of name: number")
        for name, value in selector.items():
            lowered = name.lower() if isinstance(name, str) else name
            if lowered not in declared:
                raise SimError(
                    f"exclude[].options names {name!r}, which is not a "
                    "corners.options option of this request"
                    + (
                        f" (declared: {', '.join(sorted(declared))})"
                        if declared
                        else ""
                    )
                )
            _validate_param_scalar(value, f"exclude[].options.{name}")


_CORNER_UNIT_KEYS = ("process", "temperature_c", "supply_v")
_CORNER_UNIT_CROSSED_KEYS = ("process", "supply_v", "temperature_c", "variants")


def _expand_corner_units(
    corners_spec: dict[str, Any], exclude_spec: list[dict[str, Any]]
) -> list[CornerPoint]:
    """Expand ``corners.units`` -- an explicit, non-crossed point list.

    Each record is ``{process?, temperature_c?, supply_v?: {key: number}}``
    and becomes exactly one :class:`CornerPoint`, in record order. Omitted
    axes collapse to the same defaults as the crossed form (``process=None``,
    ``supply_v={}``, ``temperature_c=27``) and ``process`` accepts the same
    string-or-bundle forms (:func:`_parse_process_entry`), so a unit that
    names the same coordinates as a crossed point yields an identical
    ``corner_id`` and checkpoint/``--resume`` reuse works across both forms.

    Mutually exclusive with the crossed axes and with ``exclude`` (a unit
    list is already the exact set wanted). Duplicates are judged on
    ``corner_id``, so two records that differ only below its 3-decimal supply
    formatting are refused rather than silently merged.
    """
    for key in _CORNER_UNIT_CROSSED_KEYS:
        if key in corners_spec:
            raise SimError(
                f"corners.units cannot be combined with corners.{key}: units "
                "are executed as given, not crossed"
            )
    if exclude_spec:
        raise SimError(
            "corners.units cannot be combined with exclude: list only the units to run"
        )
    units = corners_spec["units"]
    if not isinstance(units, list) or not units:
        raise SimError("corners.units must be a non-empty array of objects")

    supply_waveforms = sim_supply_waveform.parse_supply_waveforms(
        corners_spec, {}, SimError, units_mode=True
    )
    points: list[CornerPoint] = []
    seen: dict[str, int] = {}
    for index, record in enumerate(units):
        point = _parse_corner_unit(record, f"corners.units[{index}]")
        sim_supply_waveform.require_unit_keys(
            supply_waveforms, point.supply_v, index, SimError
        )
        point.supply_waveforms = supply_waveforms
        corner_id = point.corner_id
        if corner_id in seen:
            raise SimError(
                f"corners.units[{index}] duplicates corners.units[{seen[corner_id]}] "
                f"(both resolve to corner_id '{corner_id}')"
            )
        seen[corner_id] = index
        points.append(point)
    return points


def _parse_corner_unit(record: Any, where: str) -> CornerPoint:
    """Validate one ``corners.units`` record and build its :class:`CornerPoint`."""
    if not isinstance(record, dict):
        raise SimError(f"{where} must be an object")
    unknown = sorted(set(record) - set(_CORNER_UNIT_KEYS))
    if unknown:
        raise SimError(
            f"{where} has unknown key(s) {', '.join(unknown)} "
            f"(supported: {', '.join(_CORNER_UNIT_KEYS)})"
        )
    raw_process = record.get("process")
    process, sections, section_libs = (
        (None, None, None) if raw_process is None else _parse_process_entry(raw_process)
    )
    temperature_c = record.get("temperature_c", 27)
    if not is_number(temperature_c):
        raise SimError(f"{where}.temperature_c must be a number")
    return CornerPoint(
        process,
        _parse_unit_supply_v(record.get("supply_v") or {}, where),
        temperature_c,
        process_sections=sections,
        process_section_libs=section_libs,
    )


def _parse_unit_supply_v(supply_v: Any, where: str) -> dict[str, Any]:
    """Require a ``corners.units`` ``supply_v`` to be rail -> scalar number."""
    if not isinstance(supply_v, dict):
        raise SimError(f"{where}.supply_v must be an object of scalar numbers")
    for rail, value in supply_v.items():
        if not is_number(value):
            raise SimError(
                f"{where}.supply_v.{rail} must be a scalar number "
                "(arrays are only valid in the crossed corners.supply_v form)"
            )
    return dict(supply_v)


def _is_excluded(point: CornerPoint, exclude_spec: list[dict[str, Any]]) -> bool:
    """A point is excluded if it matches every key of any one exclude entry."""
    for entry in exclude_spec:
        if _matches_exclude(point, entry):
            return True
    return False


def _mapping_matches(actual: dict[str, Any], selector: Any, fold: Any) -> bool:
    """Whether every ``selector`` key (name folded by ``fold``) equals the
    point's value for it -- the subset match of ``exclude[].supply_v`` and
    ``exclude[].params``."""
    if not isinstance(selector, dict):
        return False
    return all(actual.get(fold(name)) == value for name, value in selector.items())


def _fold_lower(name: Any) -> str:
    return str(name).lower()


# exclude-entry key -> predicate(point, selector value); an unknown key never matches.
_EXCLUDE_KEY_MATCHERS: dict[str, Any] = {
    "process": lambda point, value: point.process == value,
    "temperature_c": lambda point, value: point.temperature_c == value,
    "variant": lambda point, value: point.variant == value,
    "supply_v": lambda point, value: _mapping_matches(point.supply_v, value, str),
    "params": lambda point, value: _mapping_matches(point.params, value, _fold_lower),
    "options": lambda point, value: _mapping_matches(point.options, value, _fold_lower),
}


def _matches_exclude(point: CornerPoint, entry: dict[str, Any]) -> bool:
    for key, value in entry.items():
        matcher = _EXCLUDE_KEY_MATCHERS.get(key)
        if matcher is None or not matcher(point, value):
            return False
    return True
