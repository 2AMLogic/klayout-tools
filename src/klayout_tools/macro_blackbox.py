"""The ``verilog_blackbox`` side of the shared ``request.macros`` shape:
what a blackbox file declares, and the macro identity read from it.

Every verb's macro validation confirms a supplied ``verilog_blackbox``
against the entry's ``cell`` (:func:`check_blackbox_declares_cell`), and
``klt synthesize`` alone (issue #5612) lets a LEF-less entry take its
whole identity from the blackbox (:func:`resolve_lefless_identity`).
Both answers come from :func:`verilog_declared_modules`, which reads only
the file's ``module`` declarations -- comments and ``(* ... *)``
attributes stripped -- never the logic inside them.

Split out of :mod:`klayout_tools.macros` so the macro validator stays
under its per-file size ceiling while the blackbox-identity machinery
grows; :mod:`klayout_tools.macros` re-exports nothing here that its own
callers need beyond the two functions it imports.
"""

from __future__ import annotations

import re
from typing import Any

_VERILOG_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
_VERILOG_ATTRIBUTE_RE = re.compile(r"\(\*.*?\*\)", re.DOTALL)
_VERILOG_MODULE_RE = re.compile(
    r"(?<![\w$])(?:macro)?module\s+(?:\\(\S+)|([A-Za-z_][\w$]*))"
)


def verilog_declared_modules(text: str) -> list[str]:
    """Module names declared in Verilog ``text``, in order. Comments and
    ``(* ... *)`` attributes are stripped first, so a commented-out
    ``module`` line is not a declaration; escaped identifiers
    (``\\name``) are returned without the backslash."""
    text = _VERILOG_COMMENT_RE.sub(" ", text)
    text = _VERILOG_ATTRIBUTE_RE.sub(" ", text)
    return [m.group(1) or m.group(2) for m in _VERILOG_MODULE_RE.finditer(text)]


def check_blackbox_declares_cell(
    path: str,
    cell: str,
    *,
    label: str,
    error_cls: type[Exception],
) -> None:
    """Reject a ``verilog_blackbox`` that declares no module named ``cell``.

    Instance counts are keyed on the LEF-derived cell name, so a blackbox
    declaring a differently named module would make the design's instances
    silently count as zero macros. A file declaring several modules passes
    when any one equals ``cell``."""
    with open(path, encoding="utf-8", errors="replace") as handle:
        modules = verilog_declared_modules(handle.read())
    if cell not in modules:
        raise error_cls(
            f"{label}.verilog_blackbox declares module(s) {modules} but the "
            f"macro cell is '{cell}' (from its lef); rename the blackbox "
            f"module to '{cell}' so the design's instances resolve to this "
            "macro"
        )


def resolve_lefless_identity(
    entry: dict[str, Any],
    blackbox: str | None,
    *,
    label: str,
    error_cls: type[Exception],
) -> str:
    """Cell name of an abstract-less macro (issue #5612): ``cell`` when given
    (required to be a module the ``verilog_blackbox`` declares), else the
    blackbox's single declared module. Several modules and no ``cell`` is an
    error listing them -- never a guess."""
    if blackbox is None:
        raise error_cls(
            f"{label} gives no lef, so {label}.verilog_blackbox is required: "
            "without a LEF the blackbox is the only source of the macro's "
            "identity and ports"
        )
    with open(blackbox, encoding="utf-8", errors="replace") as handle:
        modules = verilog_declared_modules(handle.read())
    cell = entry.get("cell")
    if cell is not None:
        if not isinstance(cell, str) or not cell:
            raise error_cls(f"{label}.cell must be a non-empty string when given")
        if cell not in modules:
            raise error_cls(
                f"{label}.cell '{cell}' is not a module its verilog_blackbox "
                f"declares (declared: {modules})"
            )
        return cell
    if len(modules) != 1:
        raise error_cls(
            f"{label} gives no lef and no cell, and its verilog_blackbox "
            f"declares {len(modules)} modules {modules}; set {label}.cell to "
            "the macro's module"
        )
    return modules[0]
