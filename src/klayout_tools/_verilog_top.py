"""Request-validation helpers for ``klt functional-verification``'s
``testbench.verilog_top`` path, split out of ``functional_verification`` to keep
that module under its size ceiling. Each takes the caller's error class so the
raised type stays ``FunctionalVerificationError``.
"""

from __future__ import annotations

import re
from typing import Any


def plusargs(value: Any, error: type[Exception]) -> list[str]:
    """``testbench.plusargs`` (issue #5006): ``+name[=value]`` strings appended
    verbatim to the ``vvp`` argv. Absent means ``[]``."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise error("request.testbench.plusargs must be an array of strings")
    for index, item in enumerate(value):
        where = f"request.testbench.plusargs[{index}]"
        if not isinstance(item, str) or not item:
            raise error(f"{where} must be a non-empty string")
        if not item.startswith("+"):
            raise error(f"{where} must start with '+' (a vvp plusarg) -- got {item!r}")
    return list(value)


def require_module_declared(
    top: str, sources: list[str], error: type[Exception]
) -> None:
    module_re = re.compile(rf"^\s*module\s+{re.escape(top)}\b", re.MULTILINE)
    for source in sources:
        try:
            with open(source, encoding="utf-8", errors="replace") as handle:
                if module_re.search(handle.read()):
                    return
        except OSError:
            continue
    raise error(
        f"testbench.verilog_top '{top}' is not a module declared in request.sources"
    )
