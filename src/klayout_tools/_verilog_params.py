"""Verilog top-level parameter overrides for Yosys scripts (issue #3466).

``klt synthesize`` (and the ``klt equiv`` gold side it gates through) accept an
optional ``parameters`` object -- ``name -> value`` -- applied with Yosys's
``chparam`` between ``read_verilog`` and ``hierarchy``. The values land in a
generated ``.ys`` script, so they are validated tightly here: nothing that
could end a script line or break out of a quoted string is accepted.
"""

from __future__ import annotations

import re
from typing import Any

_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
#: Characters a string-valued parameter may carry: no quotes, backslashes,
#: whitespace control characters, ``;`` or ``#`` (script separators/comments).
_STRING_VALUE_RE = re.compile(r"[A-Za-z0-9_ .,:/+\-=@%]*")


def validate_parameters(
    parameters: Any, error_cls: type[Exception], field: str
) -> dict[str, int | str]:
    """Validate the optional ``parameters`` object; ``{}`` when omitted.

    Keys must be Verilog identifiers; values must be integers or plain
    strings. Booleans and floats are rejected: ``chparam`` has no unambiguous
    rendering for either, and a silently-coerced ``true`` would be a
    wrong-config result.
    """
    if parameters is None:
        return {}
    if not isinstance(parameters, dict):
        raise error_cls(f"{field} must be a JSON object (name -> integer or string)")
    for key, value in parameters.items():
        if not isinstance(key, str) or not _NAME_RE.fullmatch(key):
            raise error_cls(f"{field} keys must be Verilog identifiers; got {key!r}")
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise error_cls(
                f"{field}[{key!r}] must be an integer or a string -- got "
                f"{type(value).__name__}"
            )
        if isinstance(value, str) and not _STRING_VALUE_RE.fullmatch(value):
            raise error_cls(
                f"{field}[{key!r}] string value {value!r} has characters "
                "outside the supported set (letters, digits, space and "
                "_ . , : / + - = @ %)"
            )
    return dict(parameters)


def chparam_lines(parameters: dict[str, int | str], top: str) -> list[str]:
    """The ``chparam`` script lines for ``parameters`` on module ``top``
    (empty when there is nothing to override)."""
    if not parameters:
        return []
    sets = " ".join(
        f"-set {name} {value}" if isinstance(value, int) else f'-set {name} "{value}"'
        for name, value in parameters.items()
    )
    return [f"chparam {sets} {top}"]


#: Characters a define value may carry: no quotes, backslashes, whitespace,
#: ``;`` or ``#`` (script separators/comments) or control characters.
_DEFINE_VALUE_RE = re.compile(r"[A-Za-z0-9_.,:/+\-=@%]*")


def validate_defines(
    defines: Any, error_cls: type[Exception], field: str
) -> dict[str, str | None]:
    """Validate the optional ``defines`` object; ``{}`` when omitted (issue
    #4196).

    Keys must be Verilog identifiers; values are a plain string
    (``-D NAME=VALUE``) or ``null`` (bare ``-D NAME``). The values land in a
    generated ``.ys`` script, so nothing that could end a script line or
    break out of a token is accepted.
    """
    if defines is None:
        return {}
    if not isinstance(defines, dict):
        raise error_cls(f"{field} must be a JSON object (name -> string or null)")
    for key, value in defines.items():
        if not isinstance(key, str) or not _NAME_RE.fullmatch(key):
            raise error_cls(f"{field} keys must be Verilog identifiers; got {key!r}")
        if value is None:
            continue
        if not isinstance(value, str):
            raise error_cls(
                f"{field}[{key!r}] must be a string or null -- got "
                f"{type(value).__name__}"
            )
        if not _DEFINE_VALUE_RE.fullmatch(value):
            raise error_cls(
                f"{field}[{key!r}] value {value!r} has characters outside the "
                "supported set (letters, digits and _ . , : / + - = @ %)"
            )
    return dict(defines)


def read_verilog_line(
    path: object, defines: dict[str, str | None] | None = None
) -> str:
    """The Yosys ``read_verilog`` line for RTL source ``path`` (issue #3797).

    ``.sv``/``.svh`` sources (case-insensitive) are read with ``-sv`` so
    ordinary SystemVerilog (``logic``, ``always_ff``) parses; every other
    extension keeps the plain Verilog-2005 front end, so existing ``.v``
    designs are parsed exactly as before.

    ``defines`` (issue #4196) adds ``-D NAME[=VALUE]`` preprocessor defines,
    in sorted-name order so the script is deterministic.
    """
    text = str(path)
    flags = "-sv " if text.lower().endswith((".sv", ".svh")) else ""
    for name in sorted(defines or {}):
        value = (defines or {})[name]
        flags += f"-D {name} " if value is None else f"-D {name}={value} "
    return f"read_verilog {flags}{text}"
