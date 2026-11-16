"""Work-list validation for the common coverage block (split out of
``coverage.py`` to keep that file under its size ceiling, issue #5343).

``inapplicable``/``skipped``/``unknown`` records carry ``id`` and ``reason``
and may carry an optional non-empty ``missing`` list of strings.
"""

from __future__ import annotations

import re
from typing import Any


def _identity_list_error(value: Any, *, records: bool) -> str | None:
    if not isinstance(value, list):
        return "work must be an array"
    identities: list[str] = []
    for item in value:
        if records:
            if not isinstance(item, dict) or not (
                set(item) == {"id", "reason"}
                or set(item) == {"id", "reason", "missing"}
            ):
                return "work records require id and reason (and optional missing)"
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
        error = _identity_list_error(block.get(field), records=field != "checked")
        if error:
            return f"{field}: {error}"
        identities.extend(
            block[field] if field == "checked" else [r["id"] for r in block[field]]
        )
    if len(set(identities)) != len(identities):
        return "work identity occurs in more than one coverage category"
    return None
