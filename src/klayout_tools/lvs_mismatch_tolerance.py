"""``options.parameter_tolerance`` snap-and-recompare helpers for ``klt lvs``.

Split out of ``lvs_mismatch.py`` (issue #4746) to keep that module under its
per-file size ceiling. Pure move: the names are re-exported from
``lvs_mismatch`` (and so from ``lvs``) unchanged. This module never imports
``lvs_mismatch`` or ``lvs`` at module scope (deferred imports inside the
functions that need them), so the import graph stays acyclic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from .lvs_mismatch import ExcludedParameters

# --------------------------------------------------------------------------- #
# options.parameter_tolerance (issue #589): snap-and-recompare
# --------------------------------------------------------------------------- #


def _parse_parameter_tolerance(options: dict[str, Any]) -> float | None:
    """Validate ``options.parameter_tolerance`` into a float, or ``None`` when
    omitted (the default: exact compare, unchanged behaviour).

    A *relative* tolerance expressed as a fraction (``0.001`` is 0.1%), not a
    percentage and not an absolute value -- the parameters this compares span
    micrometres, ohms and farads in one request, so no single absolute number
    would be meaningful across them.

    Everything malformed raises :class:`LvsError` rather than degrading to the
    default, matching every other request-side hook in this module
    (``hints.same_nets``, ``reference.device_bulk``): a tolerance the caller
    believes is in force but is not would be the worst possible failure mode
    for this particular option.
    """
    from .lvs import _MAX_PARAMETER_TOLERANCE, LvsError

    if "parameter_tolerance" not in options:
        return None
    value = options["parameter_tolerance"]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LvsError(
            "options.parameter_tolerance must be a number (a relative "
            "tolerance expressed as a fraction, e.g. 0.001 for 0.1%); got "
            f"{value!r}"
        )
    tolerance = float(value)
    if tolerance < 0:
        raise LvsError(
            f"options.parameter_tolerance must not be negative; got {tolerance!r}"
        )
    if tolerance >= _MAX_PARAMETER_TOLERANCE:
        raise LvsError(
            f"options.parameter_tolerance must be below "
            f"{_MAX_PARAMETER_TOLERANCE} (a relative tolerance expressed as a "
            f"fraction -- 0.001 is 0.1%); got {tolerance!r}, which would call "
            "almost any two values equal"
        )
    return tolerance


class _ToleratedParam(NamedTuple):
    """One reference-side device parameter :func:`_collect_tolerance_snaps`
    resolved to be within ``options.parameter_tolerance`` of its layout-side
    counterpart (issue #589).

    ``circuit_name``/``device_id``/``param_id`` are the *lookup key* rather
    than the reference ``Device`` object itself: the objects a
    ``GenericNetlistCompareLogger`` receives are const references, and
    ``Device.set_parameter`` on one raises ``RuntimeError: Cannot call
    non-const method on a const reference``. The mutation therefore has to
    re-resolve the device from the owning netlist
    (:func:`_apply_tolerance_snaps`).
    """

    circuit_name: str
    device_id: int
    param_id: int
    display_name: str
    layout_value: float
    reference_value: float
    layout_device: str | None
    reference_device: str | None
    device_class: str | None
    relative_delta: float


def _relative_delta(a_value: float, b_value: float) -> float:
    """``|a-b| / max(|a|, |b|)`` -- the same denominator
    :func:`_values_differ`'s relative term uses, so a tolerance and the
    float-noise epsilon are expressed on one scale.

    ``0.0`` when both values are exactly zero; ``1.0`` when exactly one of
    them is (no finite relative tolerance below
    :data:`_MAX_PARAMETER_TOLERANCE` can absorb a zero-vs-nonzero difference,
    which is the intended outcome -- that is a missing/short device, not a
    rounding difference).
    """
    scale = max(abs(a_value), abs(b_value))
    if scale == 0.0:
        return 0.0
    return abs(a_value - b_value) / scale


def _tolerated_device_pair(
    a: Any,
    b: Any,
    tolerance: float,
    excluded_parameters: ExcludedParameters | None = None,
) -> list[_ToleratedParam] | None:
    """The snap records for one layout/reference device pair whose *every*
    differing parameter sits within ``tolerance``, or ``None``.

    All-or-nothing per device pair on purpose: snapping only the in-tolerance
    parameters of a pair that also differs by more than the tolerance
    somewhere else would drop that in-tolerance parameter from
    ``mismatches[]`` while the run still reports ``mismatch`` -- strictly less
    information than today's output for a pair the tolerance cannot rescue
    anyway. A pair with an out-of-tolerance parameter is therefore left
    completely alone, and reports exactly what it reports today.

    Uses a bare ``!=`` rather than :func:`_values_differ` to decide which
    parameters are candidates: ``NetlistComparer``'s own equality tolerance is
    *tighter* than this module's float-noise epsilon, so a difference below
    :data:`_PARAM_REL_EPSILON` can still be what the engine refused to match
    on -- and an explicit tolerance should absorb that too, not just the
    differences this module happens to classify.
    """
    from .lvs import _PARAM_DISPLAY_NAMES, _name_or_none
    from .lvs_mismatch import _param_pair_is_comparable

    device_class = a.device_class()
    try:
        circuit_name = b.circuit().name
        device_id = b.id()
    except (AttributeError, RuntimeError):
        # A stand-in device object (the classification unit tests' fake
        # loggers) has no owning circuit to re-resolve the mutation against.
        return None
    if circuit_name is None:
        return None

    records: list[_ToleratedParam] = []
    for param in device_class.parameter_definitions():
        a_value = a.parameter(param.id())
        b_value = b.parameter(param.id())
        if a_value == b_value:
            continue
        if not _param_pair_is_comparable(
            param.name,
            a_value,
            b_value,
            class_name=device_class.name,
            excluded_parameters=excluded_parameters,
        ):
            # Not a difference to absorb -- and not one that may veto this
            # pair's snap either (issues #2435/#2461; see the helper's
            # docstring). Issue #2461 matters here for the same reason it
            # does in `_classify_param_mismatch`: a placeholder-excluded `R`
            # reading `0` against a real extracted value is a delta of 1.0
            # that no tolerance can absorb, so before this it silently vetoed
            # the whole pair's snap -- an excluded parameter deciding the
            # outcome of a compare it is not part of.
            continue
        delta = _relative_delta(a_value, b_value)
        if delta > tolerance:
            return None
        records.append(
            _ToleratedParam(
                circuit_name=circuit_name,
                device_id=device_id,
                param_id=param.id(),
                display_name=_PARAM_DISPLAY_NAMES.get(param.name, param.name.lower()),
                layout_value=a_value,
                reference_value=b_value,
                layout_device=_name_or_none(a),
                reference_device=_name_or_none(b),
                device_class=device_class.name,
                relative_delta=delta,
            )
        )
    return records or None


def _collect_tolerance_snaps(
    logger: Any,
    tolerance: float,
    excluded_parameters: ExcludedParameters | None = None,
) -> list[_ToleratedParam]:
    """Every reference-side parameter one ``compare()`` pass showed to be
    within ``tolerance`` of its layout-side counterpart (issue #589).

    Covers both routes a parameter-only difference reaches this module by:

    * ``match_devices_with_different_parameters`` -- the clean case, where
      surrounding connectivity was enough for ``NetlistComparer`` to pair the
      two devices before comparing their parameters;
    * :func:`_degraded_param_pair` -- the minimal-cell case (issue #282),
      where the devices' own terminals *are* the surrounding structure, the
      comparer never pairs them at all, and this module reconstructs the
      pairing from the ``device_mismatch``/``net_mismatch`` cascade instead.
      Handling only the first would leave the very shape real extracted
      layouts hit (any body/well net not shorted to a rail) unable to reach
      ``"match"`` however wide the tolerance.
    """
    from .lvs_mismatch import _degraded_param_pair

    pairs: list[tuple[Any, Any]] = list(getattr(logger, "param_mismatches", ()) or ())
    degraded = _degraded_param_pair(logger, excluded_parameters, exact_differences=True)
    if degraded is not None:
        pairs.append((degraded.layout_device, degraded.reference_device))

    snaps: list[_ToleratedParam] = []
    seen: set[tuple[str, int, int]] = set()
    for a, b in pairs:
        if a is None or b is None:
            continue
        records = _tolerated_device_pair(a, b, tolerance, excluded_parameters)
        if records is None:
            continue
        for record in records:
            key = (record.circuit_name, record.device_id, record.param_id)
            if key in seen:
                continue
            seen.add(key)
            snaps.append(record)
    return snaps


def _apply_tolerance_snaps(
    reference_netlist: Any, snaps: list[_ToleratedParam]
) -> None:
    """Set each snapped reference-side parameter to its layout-side value, so
    the next ``compare()`` sees two netlists that agree within the caller's
    tolerance.

    Re-resolves every device from ``reference_netlist`` by
    ``(circuit name, device id)`` -- see :class:`_ToleratedParam` for why the
    logger's own object references cannot be mutated. A device that no longer
    resolves is skipped rather than raising: the worst case is that the next
    ``compare()`` still reports the difference, which is the safe direction.
    """
    for snap in snaps:
        circuit = reference_netlist.circuit_by_name(snap.circuit_name)
        if circuit is None:
            continue
        device = circuit.device_by_id(snap.device_id)
        if device is None:
            continue
        device.set_parameter(snap.param_id, snap.layout_value)


def _tolerance_disclosure(snap: _ToleratedParam, tolerance: float) -> dict[str, Any]:
    """The ``severity: "warning"`` :data:`CATEGORY_DEVICE_PARAMETER_TOLERATED`
    entry disclosing one absorbed parameter difference (issue #589).

    Carries both original values in ``property`` (the reference side's
    pre-snap number, never the snapped one) so the caller can always recover
    what was actually compared -- the same "disclose, never silently absorb"
    discipline ``device.bulk_reconciled``/``device.body_unverified`` apply.
    """
    from .lvs import CATEGORY_DEVICE_PARAMETER_TOLERATED
    from .lvs_mismatch import _mismatch

    return _mismatch(
        CATEGORY_DEVICE_PARAMETER_TOLERATED,
        "warning",
        f"matched device parameter '{snap.display_name}' differs by "
        f"{snap.relative_delta * 100:.4g}%, within the requested "
        f"options.parameter_tolerance -- the reference value was snapped to "
        f"the layout value for the comparison, so this dimension of the "
        f"compare was verified only to that tolerance, not exactly (see "
        "docs/cli/lvs.md, 'device.parameter_tolerated')",
        "both",
        device={
            "layout": snap.layout_device,
            "reference": snap.reference_device,
            "class": snap.device_class,
        },
        property_={
            "name": snap.display_name,
            "layout": snap.layout_value,
            "reference": snap.reference_value,
        },
        details={
            "relative_delta": snap.relative_delta,
            "tolerance": tolerance,
        },
    )
