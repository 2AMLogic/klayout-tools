"""Work-list validation for the common coverage block (split out of
``coverage.py`` to keep that file under its size ceiling, issue #5343).

``inapplicable`` and ``skipped`` records may represent multiple units with
``count``. All work records may carry a non-empty ``missing`` list.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, TypedDict

from ._jsonnum import is_int


class _RequiredWorkRecord(TypedDict):
    id: str
    reason: str


class CoverageWorkRecord(_RequiredWorkRecord, total=False):
    count: int | float
    missing: list[str]


def coverage_work_count(record: Mapping[str, Any]) -> int:
    """Number of units represented by a validated work record."""
    return int(record.get("count", 1))


def _positive_count(value: Any) -> bool:
    # JSON Schema's integer type includes numbers such as 2.0.
    return (
        is_int(value) or isinstance(value, float) and value.is_integer()
    ) and value >= 1


def _identity_list_error(
    value: Any, *, records: bool, counted: bool = False
) -> str | None:
    if not isinstance(value, list):
        return "work must be an array"
    required = {"id", "reason"}
    allowed = required | ({"missing", "count"} if counted else {"missing"})
    identities: list[str] = []
    for item in value:
        if records:
            if not isinstance(item, dict) or not (required <= item.keys() <= allowed):
                return "work records require id and reason and disallow extra fields"
            if "count" in item and not _positive_count(item["count"]):
                return "work record count must be a positive integer"
            missing = item.get("missing")
            if "missing" in item and (
                not isinstance(missing, list)
                or not missing
                or not all(isinstance(m, str) and m.strip() for m in missing)
            ):
                return "work record missing must be a non-empty list of strings"
            reason = item["reason"]
            if not isinstance(reason, str) or not re.fullmatch(
                r"[a-z][a-z0-9_]*", reason
            ):
                return "work reason must be a nonempty stable code"
            item = item["id"]
        if not isinstance(item, str) or not item.strip():
            return "work identity must be a nonempty string"
        identities.append(item)
    if len(set(identities)) != len(identities):
        return "work identities must be unique"
    return None


def _work_lists_error(block: dict[str, Any]) -> str | None:
    identities: list[str] = []
    for field in ("checked", "skipped", "inapplicable", "unknown"):
        error = _identity_list_error(
            block.get(field),
            records=field != "checked",
            counted=field in {"skipped", "inapplicable"},
        )
        if error:
            return f"{field}: {error}"
        identities.extend(
            block[field] if field == "checked" else [r["id"] for r in block[field]]
        )
    if len(set(identities)) != len(identities):
        return "work identity occurs in more than one coverage category"
    return None
