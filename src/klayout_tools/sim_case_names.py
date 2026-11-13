"""Case-variant ``measurements[].name`` rejection for ``klt sim`` (issue #5373)."""

from __future__ import annotations

from typing import Any

from .sim_errors import SimError

# Forms whose own validators already reject duplicate names (any case) with a
# more specific message; the generic check defers to them.
_SELF_CHECKING_FORMS = ("snm", "slope_band", "transfer", "tian", "search")


def validate_case_variant_names(measurements_spec: list[dict[str, Any]]) -> None:
    """No two entries may differ *only* by letter case.

    ngspice lower-cases ``.meas`` names, so ``ta`` and ``tA`` are one
    measurement to it: the later card shadows the earlier and both response
    entries report the same value. Byte-identical duplicates are left alone.
    """
    if any(f in spec for spec in measurements_spec for f in _SELF_CHECKING_FORMS):
        return
    spellings: dict[str, str] = {}
    for spec in measurements_spec:
        name = spec.get("name")
        if not isinstance(name, str):
            continue
        first = spellings.setdefault(name.lower(), name)
        if first != name:
            raise SimError(
                f"measurements '{first}' and '{name}' differ only by letter "
                "case: measurement names are compared case-insensitively "
                "(ngspice lower-cases them), so the later one would shadow "
                "the earlier and both would report the same value -- give "
                "each measurement a distinct name"
            )
