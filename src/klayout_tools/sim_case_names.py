"""Duplicate ``measurements[].name`` rejection for ``klt sim`` (issues #5373, #6289)."""

from __future__ import annotations

from typing import Any

from .sim_errors import SimError

# Forms whose own validators already reject duplicate names (any case) with a
# more specific message; the generic check defers to them.
_SELF_CHECKING_FORMS = ("snm", "slope_band", "transfer", "tian", "search")


def validate_case_variant_names(measurements_spec: list[dict[str, Any]]) -> None:
    """No two entries may share a ``name``.

    ngspice lower-cases ``.meas`` names, so ``ta`` and ``tA`` are one
    measurement to it: the later card shadows the earlier and both response
    entries report the same value. Byte-identical duplicates are refused too
    (issue #6289): they shadow exactly the same way, and before that issue
    such a request escaped the JSON error envelope as an unhandled
    ``ValueError`` from coverage's work-identity check instead of failing
    here with a ``SimError``.
    """
    if any(f in spec for spec in measurements_spec for f in _SELF_CHECKING_FORMS):
        return
    spellings: dict[str, str] = {}
    seen: set[str] = set()
    for spec in measurements_spec:
        name = spec.get("name")
        if not isinstance(name, str):
            continue
        if name in seen:
            raise SimError(
                f"measurement '{name}' is declared more than once: the "
                "later declaration would shadow the earlier and both "
                "response entries would report the same value -- give "
                "each measurement a unique name"
            )
        seen.add(name)
        first = spellings.setdefault(name.lower(), name)
        if first != name:
            raise SimError(
                f"measurements '{first}' and '{name}' differ only by letter "
                "case: measurement names are compared case-insensitively "
                "(ngspice lower-cases them), so the later one would shadow "
                "the earlier and both would report the same value -- give "
                "each measurement a distinct name"
            )
