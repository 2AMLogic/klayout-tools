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
#: A ``parameters`` key: ``PARAM`` (top module) or ``module.PARAM`` (issue
#: #4698: a parameter of another module).
_PARAM_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)?")
#: Characters a string-valued parameter may carry: no quotes, backslashes,
#: whitespace control characters, ``;`` or ``#`` (script separators/comments).
_STRING_VALUE_RE = re.compile(r"[A-Za-z0-9_ .,:/+\-=@%]*")


def validate_parameters(
    parameters: Any, error_cls: type[Exception], field: str
) -> dict[str, int | str]:
    """Validate the optional ``parameters`` object; ``{}`` when omitted.

    Keys must be Verilog identifiers, optionally module-qualified as
    ``module.PARAM`` (issue #4698); values must be integers or plain
    strings. Booleans and floats are rejected: ``chparam`` has no unambiguous
    rendering for either, and a silently-coerced ``true`` would be a
    wrong-config result.
    """
    if parameters is None:
        return {}
    if not isinstance(parameters, dict):
        raise error_cls(f"{field} must be a JSON object (name -> integer or string)")
    for key, value in parameters.items():
        if not isinstance(key, str) or not _PARAM_KEY_RE.fullmatch(key):
            raise error_cls(
                f"{field} keys must be Verilog identifiers, optionally "
                f"module-qualified as 'module.PARAM'; got {key!r}"
            )
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


def has_submodule_parameters(parameters: dict[str, int | str] | None) -> bool:
    """True when ``parameters`` carries a module-qualified (``module.PARAM``)
    key (issue #4698)."""
    return any("." in name for name in (parameters or {}))


def chparam_lines(parameters: dict[str, int | str], top: str) -> list[str]:
    """The ``chparam`` script lines for ``parameters`` (empty when there is
    nothing to override).

    Unqualified keys target module ``top``; ``module.PARAM`` keys target
    ``module`` (issue #4698) and get their own line (preceded by a
    ``select -assert-any`` existence check), in sorted-module order after the
    top-level line, so the script is deterministic.
    """
    if not parameters:
        return []
    by_module: dict[str, list[str]] = {}
    for key, value in parameters.items():
        module, _, name = key.rpartition(".")
        rendered = f"{value}" if isinstance(value, int) else f'"{value}"'
        by_module.setdefault(module or top, []).append(f"-set {name} {rendered}")
    order = ([top] if top in by_module else []) + sorted(set(by_module) - {top})
    lines: list[str] = []
    for module in order:
        if module != top:
            # chparam only warns on an unknown module; assert so the run fails
            # with "selection is empty" instead of a later error. `-defer`
            # (see `read_verilog_line`) names each RTL module `$abstract\<m>`.
            lines.append(f"select -assert-any $abstract\\{module}")
        lines.append(f"chparam {' '.join(by_module[module])} {module}")
    return lines


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
    path: object,
    defines: dict[str, str | None] | None = None,
    parameters: dict[str, int | str] | None = None,
) -> str:
    """The Yosys ``read_verilog`` line for RTL source ``path`` (issue #3797).

    ``.sv``/``.svh`` sources (case-insensitive) are read with ``-sv`` so
    ordinary SystemVerilog (``logic``, ``always_ff``) parses; every other
    extension keeps the plain Verilog-2005 front end, so existing ``.v``
    designs are parsed exactly as before.

    ``defines`` (issue #4196) adds ``-D NAME[=VALUE]`` preprocessor defines,
    in sorted-name order so the script is deterministic.

    ``parameters`` (issue #4698): when any key is module-qualified the source
    is read with ``-defer``, so modules stay unelaborated until ``hierarchy``
    and ``chparam`` can reach a sub-module's parameter before anything (e.g.
    ``$readmemh``) evaluates it. Top-level-only overrides are unchanged.
    """
    text = str(path)
    flags = "-sv " if text.lower().endswith((".sv", ".svh")) else ""
    if has_submodule_parameters(parameters):
        flags += "-defer "
    for name in sorted(defines or {}):
        value = (defines or {})[name]
        flags += f"-D {name} " if value is None else f"-D {name}={value} "
    return f"read_verilog {flags}{text}"
