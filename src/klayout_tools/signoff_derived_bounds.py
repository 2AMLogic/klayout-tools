"""Bound-check evidence for a cross-corner ``derive`` measurement (issue #7184).

``klt sim`` grades a ``measurements[].derive`` entry once per *group* of
corners, so its report's checked-work record carries one
``derived_limit:[name, group_id, key]`` identity per group and bound -- never
the per-corner ``limit`` identities the failed-envelope row credit of issue
#6027 looks for (:func:`klayout_tools.signoff_derived_rows._bounds_checked`).
:func:`derived_bounds_checked` is that check's ``derive`` counterpart. It
lives in its own module to keep ``signoff_derived_rows`` under the per-file
size ratchet.
"""

from __future__ import annotations

from typing import Any

from .coverage import work_id


def derived_bounds_checked(
    measurement: dict[str, Any],
    record: tuple[set[str], set[str]] | None,
    keys: list[str],
) -> bool:
    """Whether every declared bound in ``keys`` was checked at every group of
    the ``derive`` rollup entry ``measurement``.

    ``record`` is ``(checked work ids, measurement names with a skipped
    bound)``; ``None`` is a counter-only report, taken at its word exactly as
    the per-corner check does. An entry without a non-empty ``groups`` list is
    no evidence either way, and a malformed group fails the check rather than
    raising."""
    groups = measurement["derive"].get("groups")
    if not isinstance(groups, list) or not groups:
        return False
    if record is None:
        return True
    checked, skipped = record
    if measurement["name"] in skipped:
        return False
    return all(
        isinstance(group, dict)
        and isinstance(group.get("group_id"), str)
        and all(
            work_id("derived_limit", measurement["name"], group["group_id"], key)
            in checked
            for key in keys
        )
        for group in groups
    )
