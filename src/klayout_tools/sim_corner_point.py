"""The expanded corner point, its report fields, and the ``corners.params`` axis.

Moved verbatim out of ``sim.py`` (issue #5428) so that module stays under its
per-file size ratchet. Holds :class:`CornerPoint` -- one expanded
(process, supply, temperature) point -- the ``report`` field helpers that
render it, parameter name/value validation and formatting, and
``corners.params`` axis expansion.

Every public name here is re-exported from ``klayout_tools.sim``, so
``sim.<name>`` keeps resolving for existing callers.
"""

from __future__ import annotations

import math
import re
from typing import Any

from .sim_echo_messages import zipped_length_message
from .sim_errors import SimError
from .sim_slug import _readable_slug, _slugify_corner_id


class CornerPoint:
    """One expanded (process, supply, temperature) point, pre-run.

    ``sample_index``/``mc_seed`` are populated only when this point is one of
    a Monte Carlo sample sequence (see :func:`_expand_monte_carlo`) --
    ``None``/``None`` for an ordinary PVT corner point. Keeping them on
    :class:`CornerPoint` itself (rather than a parallel structure) is why
    sampling needs no new execution backend: every backend already consumes
    a plain ``list[CornerPoint]`` and calls :func:`_run_corner` per point,
    so a sampled point flows through exactly the same path as an
    unsampled one.

    ``params`` (issue #3035) is the normalized ``{name: scalar}`` row of the
    ``corners.params`` axis this point sits on -- ``{}`` for a request with
    no parameter axis, in which case :attr:`corner_id` and :attr:`slug` are
    exactly the legacy ones.

    ``nodeset`` (issue #3036) is the normalized ``{lowercase node: volts}``
    initial-guess map :func:`_apply_nodeset_rules` selected for this point --
    ``{}`` for a point no ``options.nodeset`` rule matched. It is a solver
    hint, not part of the point's identity: it never changes :attr:`corner_id`.

    ``initial_conditions`` (issue #3881) is the same shape for
    ``options.initial_conditions``: the ``{lowercase node: volts}`` transient
    start state, rendered as ``.ic`` cards plus ``uic`` on the analysis.
    Like ``nodeset`` it never changes :attr:`corner_id`.

    ``analysis_args`` (issue #4970) is the ``analysis_overrides`` args string
    selected for this point (``None`` when no rule matched); it replaces
    ``analysis.args`` for this point only and never changes :attr:`corner_id`.

    ``options`` (issue #3125) is the same for the ``corners.options`` axis: the
    normalized ``{solver option: number}`` row, ``{}`` when the request has
    no such axis (and then the IDs/slugs are untouched).

    ``process_sections`` is populated only when the request's
    ``corners.process[]`` entry for this point was the multi-section bundle
    object form (``{"name": str, "sections": [...]}``, see
    :func:`_parse_process_entry`) rather than a bare string -- ``None`` for
    an ordinary single-section (or process-less) corner. ``process`` always
    holds the corner's display name either way (the bundle's ``name``, or
    the bare string itself), so `corner_id`/`slug` labeling, the response's
    ``process`` field, and `_matches_exclude` need no bundle-awareness of
    their own.

    ``process_section_libs``/``process_section_libs_resolved`` (issue #2522)
    are the per-section model-library overrides of that bundle -- both
    ``None`` unless at least one ``sections[]`` entry used the
    ``{"lib": str, "section": str}`` object form, in which case both are
    lists **parallel to** ``process_sections`` whose entry is ``None``
    wherever that section takes the request's own ``models.lib``:

    - ``process_section_libs`` holds the refs exactly **as declared**
      (relative / ``$VAR`` / ``~``). This is what crosses the fleet wire
      (:func:`_corner_points_to_wire`): a remote shard resolves them against
      its *own* box, the same way it resolves ``models.lib`` itself, so
      this host's absolute paths must never be baked in here.
    - ``process_section_libs_resolved`` holds the absolute paths
      :func:`_resolve_corner_section_libs` settled on for *this* host. It is
      deliberately **not** serialised -- it is a local execution detail, not
      part of the point's identity.
    """

    __slots__ = (
        "process",
        "process_sections",
        "process_section_libs",
        "process_section_libs_resolved",
        "param_file",
        "supply_v",
        "temperature_c",
        "params",
        "nodeset",
        "initial_conditions",
        "analysis_args",
        "options",
        "sample_index",
        "mc_seed",
        "variant",
        "mc_injected",
        "supply_waveforms",
    )

    def __init__(
        self,
        process: str | None,
        supply_v: dict[str, float],
        temperature_c: float,
        *,
        process_sections: list[str] | None = None,
        process_section_libs: list[str | None] | None = None,
        process_section_libs_resolved: list[str | None] | None = None,
        params: dict[str, int | float] | None = None,
        nodeset: dict[str, float] | None = None,
        initial_conditions: dict[str, float] | None = None,
        analysis_args: str | None = None,
        options: dict[str, int | float] | None = None,
        sample_index: int | None = None,
        mc_seed: dict[str, Any] | None = None,
        variant: str | None = None,
        mc_injected: list[dict[str, Any]] | None = None,
        supply_waveforms: tuple[str, ...] = (),
    ) -> None:
        self.variant = variant
        self.process = process
        self.params: dict[str, int | float] = params or {}
        self.nodeset: dict[str, float] = nodeset or {}
        self.initial_conditions: dict[str, float] = initial_conditions or {}
        self.analysis_args: str | None = analysis_args
        self.options: dict[str, int | float] = options or {}
        self.process_sections = process_sections
        self.process_section_libs = process_section_libs
        self.process_section_libs_resolved = process_section_libs_resolved
        # Issue #4759: host-absolute ``models.param_file``; never serialised.
        self.param_file: str | None = None
        self.supply_v = supply_v
        self.temperature_c = temperature_c
        self.sample_index = sample_index
        self.mc_seed = mc_seed
        # Issue #2911: this sample's recorded design-owned mismatch draws
        # (`sim_injected.draw`); ``None`` unless `monte_carlo.injected`.
        self.mc_injected = mc_injected
        # Issue #2902: `supply_v` keys whose PWL/PULSE source is rescaled in
        # the corner's netlist copy instead of `alter`ed.
        self.supply_waveforms = supply_waveforms

    def section_lib(self, index: int, models_lib: str | None) -> str | None:
        """The library path the ``.lib`` card for ``process_sections[index]``
        must name: this bundle's per-section override when it declared one
        for that section (issue #2522 -- the absolute path resolved by
        :func:`_resolve_corner_section_libs` when this run resolved it,
        otherwise the ref exactly as declared, so a direct caller that never
        resolved still names what it asked for rather than silently binding
        ``models.lib``), else the request's own ``models.lib``.
        """
        resolved = self.process_section_libs_resolved
        if resolved is not None and resolved[index] is not None:
            return resolved[index]
        declared = self.process_section_libs
        if declared is not None and declared[index] is not None:
            return declared[index]
        return models_lib

    @property
    def corner_id(self) -> str:
        """``<process>/<supply>V/<temp>C``, per the spike's response schema,
        with a ``/mc<sample_index>`` suffix appended for a Monte Carlo sample
        point (see :func:`_expand_monte_carlo`) so per-sample corner IDs and
        artifact paths never collide with each other or with the unsampled
        corner they were drawn from.

        ``process`` defaults to ``"default"`` when the request declares no
        process axis. Multiple supply rails (an extension beyond the spike's
        single-``vdd`` example) are joined ``key=value`` pairs, sorted by key
        for determinism.
        """
        process_label = self.process if self.process is not None else "default"
        if not self.supply_v:
            supply_label = "novdd"
        elif len(self.supply_v) == 1:
            (value,) = self.supply_v.values()
            supply_label = f"{value:.3f}V"
        else:
            supply_label = (
                "_".join(f"{k}={v:.3f}" for k, v in sorted(self.supply_v.items())) + "V"
            )
        temp_label = _format_number(self.temperature_c)
        base = f"{process_label}/{supply_label}/{temp_label}C"
        if self.variant is not None:
            base = f"{base}/@{self.variant}"
        if self.params:
            base = f"{base}/params:{_format_params_label(self.params)}"
        if self.options:
            base = f"{base}/options:{_format_params_label(self.options)}"
        if self.sample_index is None:
            return base
        return f"{base}/mc{self.sample_index}"

    @property
    def slug(self) -> str:
        """Filesystem-safe artifact-directory name for this corner, derived
        from :attr:`corner_id` (e.g. ``ss/1.620V/125C`` -> ``ss_1p620V_125C``).
        See :func:`_slugify_corner_id` for the parameter-axis and bounded forms."""
        return _slugify_corner_id(self.corner_id)

    @property
    def readable_slug(self) -> str:
        """The unbounded :attr:`slug` (before the long-name hash fallback)."""
        return _readable_slug(self.corner_id)


def _params_report_field(point: CornerPoint) -> dict[str, Any]:
    """The additive ``params`` field of a corner report: present only for a
    point on a ``corners.params`` axis, so every other report is unchanged."""
    return {"params": dict(point.params)} if point.params else {}


def _ic_report_field(point: CornerPoint) -> dict[str, Any]:
    """The additive ``initial_conditions`` field of a corner report (issue
    #3881): present only for a point an ``options.initial_conditions`` rule
    selected."""
    if not point.initial_conditions:
        return {}
    return {"initial_conditions": dict(point.initial_conditions)}


def _nodeset_report_field(point: CornerPoint) -> dict[str, Any]:
    """The additive ``nodeset`` field of a corner report (issue #3036):
    present only for a point an ``options.nodeset`` rule selected."""
    return {"nodeset": dict(point.nodeset)} if point.nodeset else {}


def _options_report_field(point: CornerPoint) -> dict[str, Any]:
    """The additive ``options`` field of a corner report (issue #3125):
    present only for a point on a ``corners.options`` axis."""
    return {"options": dict(point.options)} if point.options else {}


def _format_number(value: float) -> str:
    """Render an int-valued float without a trailing ``.0`` (e.g. temperatures)."""
    if float(value).is_integer():
        return str(int(value))
    return str(value)


_PARAM_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _format_param_value(value: int | float) -> str:
    """Canonical text of one ``corners.params`` value (issue #3035): the
    shortest round-trip JSON decimal spelling, an integral value rendered as
    an integer, negative zero as ``0`` -- never a rounded or unit-suffixed
    form, so close-valued parameters keep distinct IDs."""
    return repr(_normalize_param_value(value))


def _normalize_param_value(value: int | float) -> int | float:
    """The scalar a parameter coordinate is carried as everywhere (report,
    wire, deck): an integer when the value is integral, else the float."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _format_params_label(params: dict[str, int | float]) -> str:
    """``name=value`` pairs, sorted by (lowercase) name, comma-joined."""
    return ",".join(
        f"{name}={_format_param_value(value)}" for name, value in sorted(params.items())
    )


def _validate_param_scalar(value: Any, where: str) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise SimError(f"{where} must be a finite number (booleans excluded)")
    return _normalize_param_value(value)


def _validate_param_name(name: Any, where: str) -> str:
    """Lowercased parameter name, or a :class:`SimError` for a malformed or
    reserved (``mc_*``) one."""
    if not isinstance(name, str) or not _PARAM_NAME_RE.fullmatch(name):
        raise SimError(
            f"{where} name {name!r} must match [A-Za-z_][A-Za-z0-9_]* "
            "(a bare SPICE parameter name)"
        )
    lowered = name.lower()
    if lowered.startswith("mc_"):
        raise SimError(
            f"{where} name {name!r} is reserved: mc_* parameters are "
            "generated by monte_carlo"
        )
    return lowered


def _normalize_param_row(
    raw: Any, where: str, supply_v: dict[str, Any] | None = None
) -> dict[str, int | float]:
    """One scalar ``{name: value}`` parameter row (a report/wire ``params``
    map), validated and normalized; ``supply_v`` names may not repeat."""
    if not isinstance(raw, dict):
        raise SimError(f"{where} must be an object mapping names to numbers")
    row: dict[str, int | float] = {}
    supply_names = {str(key).lower() for key in (supply_v or {})}
    for name, value in raw.items():
        lowered = _validate_param_name(name, where)
        if lowered in row:
            raise SimError(f"{where} declares {lowered!r} more than once")
        if lowered in supply_names:
            raise SimError(
                f"{where} name {name!r} is also a supply_v key: a name may "
                "not be in both corners.params and corners.supply_v"
            )
        row[lowered] = _validate_param_scalar(value, f"{where}.{name}")
    return dict(sorted(row.items()))


def _temperature_param_name(corners_spec: dict[str, Any]) -> str | None:
    """The validated, lowercased ``corners.temperature_param`` name (issue
    #3994), or ``None`` when the request does not bind one. It is refused if
    ``corners.params`` or ``corners.supply_v`` already uses the name."""
    raw = corners_spec.get("temperature_param")
    if raw is None:
        return None
    if "units" in corners_spec:
        raise SimError(
            "corners.temperature_param cannot be combined with corners.units: "
            "put the parameter in each unit's `params` instead"
        )
    name = _validate_param_name(raw, "corners.temperature_param")
    declared = {str(key).lower() for key in (corners_spec.get("params") or {})}
    if name in declared:
        raise SimError(
            f"corners.temperature_param name {name!r} is also a corners.params "
            "key: a name may be bound by only one of the two"
        )
    supply = {str(key).lower() for key in (corners_spec.get("supply_v") or {})}
    if name in supply:
        raise SimError(
            f"corners.temperature_param name {name!r} is also a "
            "corners.supply_v key: a name may be in only one of the two maps"
        )
    return name


def _bind_temperature_param(
    param_row: dict[str, int | float],
    temperature_param: str | None,
    temperature_c: float,
) -> dict[str, int | float]:
    """``param_row`` with the grid temperature bound to ``temperature_param``
    (issue #3994), name-sorted like every params row; ``param_row`` unchanged
    when the request binds no temperature parameter."""
    if not temperature_param:
        return param_row
    bound = {**param_row, temperature_param: _normalize_param_value(temperature_c)}
    return dict(sorted(bound.items()))


def _expand_param_axis(
    corners_spec: dict[str, Any],
) -> list[dict[str, int | float]]:
    """The zipped ``corners.params`` rows (issue #3035), or ``[{}]`` for a
    request with no parameter axis. Names zip by index; every array must be
    nonempty and the same length; duplicate normalized rows are rejected."""
    spec = corners_spec.get("params")
    if spec is None or spec == {}:
        return [{}]
    if not isinstance(spec, dict):
        raise SimError(
            "corners.params must be an object mapping parameter names to "
            "equal-length arrays of numbers"
        )
    columns: dict[str, list[int | float]] = {}
    for name, values in spec.items():
        lowered = _validate_param_name(name, "corners.params")
        if lowered in columns:
            raise SimError(
                f"corners.params declares {lowered!r} more than once "
                "(names are case-insensitive)"
            )
        if not isinstance(values, list) or not values:
            raise SimError(f"corners.params.{name} must be a nonempty array of numbers")
        columns[lowered] = [
            _validate_param_scalar(value, f"corners.params.{name}[{i}]")
            for i, value in enumerate(values)
        ]
    if len({len(values) for values in columns.values()}) != 1:
        raise SimError(zipped_length_message("corners.params", columns))
    supply_names = {str(key).lower() for key in (corners_spec.get("supply_v") or {})}
    clash = sorted(set(columns) & supply_names)
    if clash:
        raise SimError(
            f"corners.params name {clash[0]!r} is also a corners.supply_v key: "
            "a name may be in only one of the two maps (supply_v addresses a "
            "source with `alter`, params address a .param)"
        )
    count = len(next(iter(columns.values())))
    rows = [
        dict(sorted((name, values[i]) for name, values in columns.items()))
        for i in range(count)
    ]
    seen: set[str] = set()
    for row in rows:
        label = _format_params_label(row)
        if label in seen:
            raise SimError(f"corners.params declares the row {label!r} twice")
        seen.add(label)
    return rows
