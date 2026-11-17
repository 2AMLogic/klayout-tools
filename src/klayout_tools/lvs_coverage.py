"""``device_parameter_coverage`` device-kind classification for ``klt lvs``
(issues #2461/#5653).

Which device-class families participate in the ``device_parameter_coverage``
block, and which of those additionally raise the
``device.geometry_not_compared`` warning. Split out of ``lvs.py`` (the same
decomposition as ``lvs_combine.py``); the names are re-imported there, so
``klayout_tools.lvs`` keeps exposing them.
"""

from __future__ import annotations

from typing import Any

#: Issue #2461: the device-class families whose *geometry* sits entirely
#: outside a default compare, keyed by the `klayout.db` class name (resolved
#: lazily, the way ``_COMBINE_PARAMETER_CONSERVATION_RULES`` is) to the
#: `device_kind` label reported for them. Both are matched with `isinstance`,
#: so each one's `*WithBulk` flavour -- a subclass declaring the identical
#: parameter set -- is covered without being listed.
#:
#: Deliberately *only* these two. A MOS class's own geometry (`L`/`W`) is
#: primary and therefore compared; its secondary `AS`/`AD`/`PS`/`PD` diffusion
#: parameters are a genuinely narrower gap and reporting them here would add a
#: warning to essentially every `klt lvs` run for no new information about
#: whether the device was *sized* correctly. For a resistor/capacitor there is
#: no such consolation: `R`/`C` is the only primary parameter, so no dimension
#: of the drawn device is compared at all.
_GEOMETRY_DISCLOSURE_CLASS_KINDS: tuple[tuple[str, str], ...] = (
    ("DeviceClassResistor", "resistor"),
    ("DeviceClassCapacitor", "capacitor"),
)

#: Issue #5653: the device-class families that appear in the
#: ``device_parameter_coverage`` block *without* ever raising a
#: ``CATEGORY_DEVICE_GEOMETRY_NOT_COMPARED`` warning. A MOS class's
#: ``L``/``W`` are primary and compared, so a warning would be per-run noise
#: (the #2461 decision stands); but the block must still say so positively,
#: or ``[]`` is ambiguous between "verified" and "nothing verified".
#: ``DeviceClassMOS4Transistor`` subclasses the 3-terminal class, so one
#: entry covers both.
_COVERAGE_ONLY_CLASS_KINDS: tuple[tuple[str, str], ...] = (
    ("DeviceClassMOS3Transistor", "mos"),
)


def _geometry_disclosure_kind(device_class: Any, kdb_module: Any) -> str | None:
    """The :data:`_GEOMETRY_DISCLOSURE_CLASS_KINDS` label for ``device_class``
    (``"resistor"``/``"capacitor"``), or ``None`` for every other class."""
    for class_name, kind in _GEOMETRY_DISCLOSURE_CLASS_KINDS:
        klayout_class = getattr(kdb_module, class_name, None)
        if klayout_class is not None and isinstance(device_class, klayout_class):
            return kind
    return None


def _coverage_kind(device_class: Any, kdb_module: Any) -> str | None:
    """The ``device_kind`` for ``device_class``'s ``device_parameter_coverage``
    entry: a warning-bearing kind (:func:`_geometry_disclosure_kind`) or a
    coverage-only one (:data:`_COVERAGE_ONLY_CLASS_KINDS`, issue #5653), else
    ``None``."""
    kind = _geometry_disclosure_kind(device_class, kdb_module)
    if kind is not None:
        return kind
    for class_name, kind in _COVERAGE_ONLY_CLASS_KINDS:
        klayout_class = getattr(kdb_module, class_name, None)
        if klayout_class is not None and isinstance(device_class, klayout_class):
            return kind
    return None
