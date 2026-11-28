"""Corner axes beside ``params``: options, process, units, and the exclude matchers.

Moved verbatim out of ``sim.py`` (issue #5428) so that module stays under its
per-file size ratchet. Holds the ``corners.options`` allow-list and axis,
``corners.process`` entry/section parsing, ``corners.units`` expansion, and
the ``exclude`` selector validation and matchers.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import sim_supply_waveform
from ._jsonnum import is_finite_number, is_number
from .sim_corner_point import (
    CornerPoint,
    _validate_param_scalar,
)
from .sim_errors import SimError

#: ``options.ngspice_options`` (issue #3111): the solver/convergence
#: ``.options`` names a request may set (``chgtol``/``trtol``, issue #7007,
#: complete the transient tolerance set). Moved out of ``sim.py`` (issue
#: #7007) for its size ratchet; re-exported there. ngspice silently ignores
#: an unknown ``.options`` name, so a free-form map would hide a typo as "no
#: effect"; the allowlist turns it into a named error instead.
NGSPICE_SOLVER_OPTIONS = (
    "abstol",
    "chgtol",
    "gmin",
    "gminsteps",
    "itl1",
    "itl2",
    "itl4",
    "method",
    "noopiter",
    "pivtol",
    "reltol",
    "srcsteps",
    "trtol",
    "vntol",
)


#: A string option value lands verbatim in a deck card, so it is limited to
#: a bare token (``method=gear``) -- no whitespace, quotes or newlines.
_NGSPICE_OPTION_TOKEN = re.compile(r"[A-Za-z0-9_.+-]+")


def _validate_ngspice_options(value: Any) -> dict[str, Any]:
    """Validate ``options.ngspice_options`` (issue #3111).

    Returns the declared map unchanged (``{}`` when omitted or ``null``).
    Keys must come from :data:`NGSPICE_SOLVER_OPTIONS`; values must be a
    finite number, a bool or a bare-token string.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SimError(
            "options.ngspice_options must be an object mapping ngspice "
            f".options names to scalar values (got {json.dumps(value, default=repr)})"
        )
    for name, item in value.items():
        if name not in NGSPICE_SOLVER_OPTIONS:
            raise SimError(
                f"options.ngspice_options key {json.dumps(name)} is not a "
                "supported solver option; supported: "
                + ", ".join(NGSPICE_SOLVER_OPTIONS)
            )
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float)):
            if not is_finite_number(item):
                raise SimError(
                    f"options.ngspice_options.{name} must be finite (got {item!r})"
                )
            continue
        if isinstance(item, str) and _NGSPICE_OPTION_TOKEN.fullmatch(item):
            continue
        raise SimError(
            f"options.ngspice_options.{name} must be a number, a boolean or "
            f"a bare-token string (got {json.dumps(item, default=repr)})"
        )
    return dict(value)


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
    :func:`resolve_corner_section_libs` is what resolves and
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


def _validate_exclude_numeric_axis(
    exclude_spec: list[dict[str, Any]], declared: set[str], *, key: str, noun: str
) -> None:
    """Reject selectors naming coordinates this request does not sweep."""
    for entry in exclude_spec:
        if not isinstance(entry, dict) or key not in entry:
            continue
        selector = entry[key]
        if not isinstance(selector, dict):
            raise SimError(f"exclude[].{key} must be an object of name: number")
        for name, value in selector.items():
            lowered = name.lower() if isinstance(name, str) else name
            if lowered not in declared:
                raise SimError(
                    f"exclude[].{key} names {name!r}, which is not a "
                    f"corners.{key} {noun} of this request"
                    + (
                        f" (declared: {', '.join(sorted(declared))})"
                        if declared
                        else ""
                    )
                )
            _validate_param_scalar(value, f"exclude[].{key}.{name}")


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
