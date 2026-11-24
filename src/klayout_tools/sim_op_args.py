"""``op`` analyses take no arguments, so ``args`` is optional (issue #5078)."""

from __future__ import annotations

from typing import Any


def default_op_args(analysis: Any) -> Any:
    """``analysis`` with ``args`` defaulted to ``""`` when it is an ``op``
    analysis dict that omitted it (on a copy); anything else is returned as is,
    so a request that supplies ``args`` keeps its own object."""
    kind = analysis.get("kind") if isinstance(analysis, dict) else None
    if isinstance(kind, str) and kind.lower() == "op" and "args" not in analysis:
        return {**analysis, "args": ""}
    return analysis
