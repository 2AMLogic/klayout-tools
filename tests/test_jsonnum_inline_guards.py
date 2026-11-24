"""No inline "a number, but not a bool" guard may live outside ``_jsonnum``
(issue #5580).

``klayout_tools._jsonnum`` is the single source of truth for ``is_number`` /
``is_int`` / ``is_finite_number``. Three earlier sweeps (#4672, #4687, #5374)
each enumerated the leftover inline copies with a single-line regex and each
missed what the regex could not see: conditions split over several lines, the
``not isinstance(x, (int, float)) or isinstance(x, bool)`` operand order, and
named helpers whose whole body is the guard. This test walks the AST instead,
so a re-introduced copy fails CI whatever its layout.

A hit is a ``BoolOp`` that combines ``isinstance(x, bool)`` with
``isinstance(x, (int, float))`` / ``isinstance(x, int)`` -- the shape every
inline copy takes, in either operand order and with or without ``not``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import klayout_tools

_NUMBER_KINDS = {"(int, float)", "(float, int)", "int"}

#: ``(file name, enclosing function)`` pairs allowed to keep the inline shape.
#: ``artifact_verify`` is stdlib-only so it can be vendored and run under
#: ``python -I -S`` (``test_artifact_verify.py::test_module_imports_stdlib_only``),
#: so it cannot import ``_jsonnum``; its ``_is_int`` twin is pinned to
#: ``_jsonnum.is_int`` by ``test_jsonnum.py``.
_VENDORED_TWINS = {("artifact_verify.py", "_is_int")}


def _is_isinstance_of(node: ast.AST, kinds: set[str]) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "isinstance"
        and len(node.args) == 2
        and ast.unparse(node.args[1]) in kinds
    )


class _GuardWalker(ast.NodeVisitor):
    def __init__(self) -> None:
        self.hits: list[tuple[int, str]] = []
        self._functions: list[str] = []

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._functions.append(node.name)
        self.generic_visit(node)
        self._functions.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        operands = [v.operand if isinstance(v, ast.UnaryOp) else v for v in node.values]
        if any(_is_isinstance_of(v, {"bool"}) for v in operands) and any(
            _is_isinstance_of(v, _NUMBER_KINDS) for v in operands
        ):
            self.hits.append(
                (node.lineno, self._functions[-1] if self._functions else "")
            )
        self.generic_visit(node)


def inline_number_guards(source: str) -> list[tuple[int, str]]:
    """``(line, enclosing function name)`` of each inline guard in ``source``."""
    walker = _GuardWalker()
    walker.visit(ast.parse(source))
    return walker.hits


@pytest.mark.parametrize(
    "snippet",
    [
        "ok = not isinstance(x, bool) and isinstance(x, (int, float))",
        "ok = isinstance(x, (int, float)) and not isinstance(x, bool)",
        "bad = isinstance(x, bool) or not isinstance(x, (int, float))",
        "bad = not isinstance(x, (int, float)) or isinstance(x, bool)",
        "bad = isinstance(x, bool) or isinstance(x, (float, int))",
        "ok = not isinstance(x, bool) and isinstance(x, int)",
        # a condition split over several lines, with a range check folded in
        (
            "bad = (\n    isinstance(x, bool)\n"
            "    or not isinstance(x, (int, float))\n    or x <= 0\n)"
        ),
        # a named helper whose whole body is the guard
        (
            "def _is_number(v):\n"
            "    return isinstance(v, (int, float)) and not isinstance(v, bool)"
        ),
    ],
)
def test_walk_flags_an_inline_guard(snippet):
    assert inline_number_guards(snippet)


@pytest.mark.parametrize(
    "snippet",
    [
        "ok = is_number(x) and x > 0",
        "ok = not is_finite_number(x) or x <= 0",
        "ok = isinstance(x, (int, float))",
        "ok = isinstance(x, str) and not isinstance(x, bool)",
        "ok = isinstance(x, bool) or isinstance(x, str)",
    ],
)
def test_walk_leaves_the_shared_helpers_and_unrelated_checks_alone(snippet):
    assert not inline_number_guards(snippet)


def test_walk_reports_the_enclosing_function():
    source = (
        "def outer():\n"
        "    def _is_int(v):\n"
        "        return not isinstance(v, bool) and isinstance(v, int)\n"
    )
    assert inline_number_guards(source) == [(3, "_is_int")]


def test_no_inline_number_guard_outside_jsonnum():
    package = Path(klayout_tools.__file__).parent
    offenders = []
    for path in sorted(package.rglob("*.py")):
        if path.name == "_jsonnum.py":
            continue
        for lineno, function in inline_number_guards(path.read_text(encoding="utf-8")):
            if (path.name, function) not in _VENDORED_TWINS:
                offenders.append(f"{path.relative_to(package)}:{lineno}")
    assert not offenders, (
        "inline 'a number, but not a bool' guard(s) -- use is_number / is_int / "
        "is_finite_number from klayout_tools._jsonnum instead: " + ", ".join(offenders)
    )


def test_the_vendored_twin_is_still_the_only_exemption():
    """If ``artifact_verify._is_int`` stops matching the walk, drop the exemption."""
    package = Path(klayout_tools.__file__).parent
    source = (package / "artifact_verify.py").read_text(encoding="utf-8")
    assert [fn for _, fn in inline_number_guards(source)] == ["_is_int"]
