"""Tests for the residual `.meas` lint rewrite shapes (issue #6622).

Two shapes found while judging #6619 left the refusal's *first* suggested
card malformed or still refused, both outside #6444's tables: a
two-argument ``max``/``min`` in a signal position (the span wrapped only
the argument list and left a bare ``max`` beside it), and a signed right
operand (the span stopped before the sign, so the suggestion was refused
again on the next pass). Both now widen like every #6444 shape, so the
first suggestion holds the author's whole local expression and re-lints
accepted in one pass -- plus a bounded expression fuzz checking that no
suggestion changes the measured value.
"""

from __future__ import annotations

import ast
import json
import math
import random
import re
import subprocess

import pytest

from helpers.tool_probes import SKIP_NO_NGSPICE
from klayout_tools import _meas_lint, sim, sim_steps
from klayout_tools._meas_lint_expression import control_expression
from test_sim_meas_lint import _file_scope_rewrite


@pytest.mark.parametrize(
    ("group", "expected"),
    [
        ("max(a,b", None),
        ("min(a,b", None),
        ("max(v(a,b))", None),
        ("min(v(a),v(b))", 0),
    ],
)
def test_min_max_callee_requires_a_complete_group(group, expected):
    assert _meas_lint._callee_start(group, group.index("(")) == expected


@pytest.mark.parametrize(
    ("expression", "quoted"),
    [
        # A two-argument max/min in a signal position is an ngspice
        # function *call*, so the span holds the callee and its whole
        # argument list in one piece (issue #6622: the callee used to be
        # dropped as a card keyword, re-writing the card to the malformed
        # `max par('(v(a)-v(b), v(c))')` -- the comma is what tells the
        # call form from the `max (expr)` keyword reading).
        ("max(v(a)-v(b), v(c))", "max(v(a)-v(b), v(c))"),
        ("min(v(a)-v(b), v(c))", "min(v(a)-v(b), v(c))"),
        # nested calls widen to the outermost callee, and the walk keeps
        # going over an outer operator once the call is one span
        ("max(min(v(a),v(b)), v(c)-v(d))", "max(min(v(a),v(b)), v(c)-v(d))"),
        ("max(v(a)-v(b), v(c))*2", "max(v(a)-v(b), v(c))*2"),
        ("2*max(v(a)-v(b), v(c))", "2*max(v(a)-v(b), v(c))"),
        # A signed right operand joins the span on the first pass (issue
        # #6622: the walk stopped at the sign and suggested e.g.
        # `par('(v(a)-v(b))')*-v(a)`, which the lint itself refused
        # again). All four operators over a bare group ...
        ("(v(a)-v(b))+-v(a)", "(v(a)-v(b))+-v(a)"),
        ("(v(a)-v(b))--v(a)", "(v(a)-v(b))--v(a)"),
        ("(v(a)-v(b))*-v(a)", "(v(a)-v(b))*-v(a)"),
        ("(v(a)-v(b))/-v(a)", "(v(a)-v(b))/-v(a)"),
        # ... over a call ...
        ("abs(v(a)-v(b))+-v(a)", "abs(v(a)-v(b))+-v(a)"),
        ("abs(v(a)-v(b))--v(a)", "abs(v(a)-v(b))--v(a)"),
        ("abs(v(a)-v(b))*-v(a)", "abs(v(a)-v(b))*-v(a)"),
        ("abs(v(a)-v(b))/-v(a)", "abs(v(a)-v(b))/-v(a)"),
        # ... and over a two-node call, whose difference rewrite stays
        # inside the span (v(a,b) means v(a)-v(b), parentheses or not)
        ("abs(v(a,b))+-v(a)", "abs(v(a)-v(b))+-v(a)"),
        ("abs(v(a,b))--v(a)", "abs(v(a)-v(b))--v(a)"),
        ("abs(v(a,b))*-v(a)", "abs(v(a)-v(b))*-v(a)"),
        ("abs(v(a,b))/-v(a)", "abs(v(a)-v(b))/-v(a)"),
        # a `+` sign is one operand too, and so are spaced signs
        ("(v(a)-v(b))*+v(a)", "(v(a)-v(b))*+v(a)"),
        ("(v(a)-v(b))*  - v(a)", "(v(a)-v(b))*  - v(a)"),
        # a signed group or a signed number on the right joins as well
        ("(v(a)-v(b))*-(v(a)+v(b))", "(v(a)-v(b))*-(v(a)+v(b))"),
        ("(v(a)-v(b))*-2", "(v(a)-v(b))*-2"),
    ],
)
def test_validate_meas_card_first_rewrite_holds_whole_expression(expression, quoted):
    # The first suggestion must be the whole local expression in one
    # par('...') span, and the lint must accept its own suggestion.
    rewrite = _file_scope_rewrite(f".meas tran t when {expression}=0.1")
    assert rewrite == f".meas tran t when par('{quoted}')=0.1"
    sim._validate_meas_card("t", rewrite)  # must not raise


@pytest.mark.parametrize(
    ("card", "rewrite"),
    [
        # a single-argument group after max/min is the keyword reading,
        # not a call: the keyword stays outside the span (issue #5651),
        # with or without the space before the group
        (
            ".meas tran t max (v(a)-v(b)) from=0 to=20u",
            ".meas tran t max par('(v(a)-v(b))') from=0 to=20u",
        ),
        (
            ".meas tran t when min(v(a)-v(b))=0.1",
            ".meas tran t when min par('(v(a)-v(b))')=0.1",
        ),
        # a comma *inside* a reference (the two-node v(a,b)) is not an
        # argument separator: still the keyword reading, and the span
        # holds the group with the difference rewrite inside it
        (
            ".meas tran t when max(v(a,b))=0.1",
            ".meas tran t when max par('(v(a)-v(b))')=0.1",
        ),
    ],
)
def test_validate_meas_card_keyword_reading_keeps_max_min_outside(card, rewrite):
    # Issue #6622 widens only the two-argument call form; the keyword
    # readings every #5651/#6444 case pinned must not move.
    assert _file_scope_rewrite(card) == rewrite
    sim._validate_meas_card("t", rewrite)  # must not raise


#: Node values for the fuzz: none is zero, none is equal to another, so a
#: rebound difference is heard, not cancelled into silence.
_FUZZ_NODES = {"a": 1.3, "b": 2.7, "c": -0.6}

_FUZZ_NUMBERS = ("2", "0.5", "1e-3")


def _fuzz_operand(rng: random.Random, depth: int = 0) -> str:
    """One random operand: a vector reference, a two-node voltage, a
    number, or (nested) a parenthesised or abs/max/min-wrapped one."""
    roll = rng.random()
    if depth < 3:
        if roll < 0.12:
            return f"({_fuzz_operand(rng, depth + 1)})"
        if roll < 0.24:
            return f"abs({_fuzz_operand(rng, depth + 1)})"
        if roll < 0.36:
            name = rng.choice(("max", "min"))
            return (
                f"{name}({_fuzz_operand(rng, depth + 1)},"
                f"{_fuzz_operand(rng, depth + 1)})"
            )
    if roll < 0.48:
        first, second = rng.choice(tuple(_FUZZ_NODES)), rng.choice(tuple(_FUZZ_NODES))
        return f"v({first},{second})"
    if roll < 0.6:
        return rng.choice(_FUZZ_NUMBERS)
    return f"v({rng.choice(tuple(_FUZZ_NODES))})"


def _fuzz_expression(rng: random.Random) -> str:
    """A random `.meas` signal-position expression, sometimes with a
    signed right operand (the issue #6622 shape)."""
    text = _fuzz_operand(rng)
    for _ in range(rng.randrange(1, 4)):
        operator = rng.choice("+-*/")
        right = _fuzz_operand(rng)
        if rng.random() < 0.35:  # a signed right operand joins the span
            right = f"-{right}"
        text = f"{text}{operator}{right}"
    return text


def _fuzz_apply(op: ast.operator, left: float, right: float) -> float:
    """The four arithmetic operators the generator emits."""
    operations = {
        ast.Add: lambda: left + right,
        ast.Sub: lambda: left - right,
        ast.Mult: lambda: left * right,
        ast.Div: lambda: left / right,
    }
    for kind, operation in operations.items():
        if isinstance(op, kind):
            return operation()
    raise AssertionError(f"unexpected operator {op!r}")


def _fuzz_call(callee: str, arguments: list[float]) -> float:
    """abs/max/min are the only calls the generator emits."""
    if callee == "abs":
        return abs(arguments[0])
    if callee == "max":
        return max(arguments)
    if callee == "min":
        return min(arguments)
    raise AssertionError(f"unexpected call {callee}(...)")


def _fuzz_value(text: str) -> float:
    """The value of a suggested span's expression: vector references
    substituted numerically, then evaluated over the AST (no ``eval``,
    which the lint config refuses in shipped code and tests alike)."""
    substituted = re.sub(
        r"\b[vV]\s*\(\s*(\w+)\s*,\s*(\w+)\s*\)",
        lambda m: f"({_FUZZ_NODES[m.group(1)]}-{_FUZZ_NODES[m.group(2)]})",
        text,
    )
    substituted = re.sub(
        r"\b[vV]\s*\(\s*(\w+)\s*\)",
        lambda m: str(_FUZZ_NODES[m.group(1)]),
        substituted,
    )

    def visit(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant):
            return float(node.value)
        if isinstance(node, ast.UnaryOp):
            value = visit(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp):
            return _fuzz_apply(node.op, visit(node.left), visit(node.right))
        if isinstance(node, ast.Call):
            return _fuzz_call(node.func.id, [visit(a) for a in node.args])
        raise AssertionError(f"unexpected node {node!r} in {text!r}")

    return visit(ast.parse(substituted, mode="eval"))


def test_validate_meas_card_fuzz_finds_no_value_changing_suggestion():
    # Issue #6622 value-preservation check: a bounded, seeded expression
    # fuzz over the lint; every suggested span must evaluate to exactly
    # the author's own value (the rewrite is the author's text, only
    # regrouped -- never a different number).
    rng = random.Random(6622)
    checked = 0
    for _ in range(600):
        expression = _fuzz_expression(rng)
        card = f".meas tran t when {expression}=0.1"
        try:
            sim._validate_meas_card("t", card)
            continue  # not refused: nothing to suggest
        except sim.SimError as excinfo:
            match = re.search(r"span: `(.*?)`\. For a whole", str(excinfo), re.S)
            if match is None:
                continue
            suggestion = match.group(1)
        try:
            expected = _fuzz_value(expression)
        except ZeroDivisionError:
            continue  # the author's own card divides by zero: skip it
        sim._validate_meas_card("t", suggestion)  # one pass, or a defect
        inner = re.search(r"par\('(.*)'\)", suggestion)
        assert inner is not None, suggestion
        suggested_value = _fuzz_value(inner.group(1))
        # exact for every rewrite (the span is the author's own text); the
        # one deliberate exception in #6619 -- a two-node difference as
        # the right operand of a `+`, `X+v(a,b)` -> `X+v(a)-v(b)` -- is
        # equal in exact arithmetic and one rounding apart in IEEE
        # doubles, while any real rebinding (the #6444 bug class) is
        # orders of magnitude larger than this tolerance
        assert math.isclose(suggested_value, expected, rel_tol=1e-9, abs_tol=1e-12)
        checked += 1
    # the corpus must actually exercise refused shapes, not skip them all
    assert checked > 200


@pytest.mark.parametrize("operator", ["+", "-", "*", "/"])
@pytest.mark.parametrize("signs", ["--", "+-", "-+", "++"])
@pytest.mark.parametrize(
    ("space", "between_signs"), [("", ""), (" ", ""), ("", " "), (" ", " ")]
)
@pytest.mark.parametrize("left", ["(v(a)-v(b))", "abs(v(a)-v(b))", "abs(v(a,b))"])
def test_chained_signs_on_right_operand(left, operator, signs, space, between_signs):
    right = between_signs.join(signs) + "v(a)"
    expression = f"{left}{space}{operator}{space}{right}"
    quoted = expression.replace("v(a,b)", "v(a)-v(b)")
    rewrite = _file_scope_rewrite(f".meas tran t when {expression}=0.1")
    assert rewrite == f".meas tran t when par('{quoted}')=0.1"
    sim._validate_meas_card("t", rewrite)


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        (" * --v(a)", -3),
        (" / --v(a)", 7 / 3),
        (" * +-v(a)", 9),
        (" * -+v(a)", 9),
        (" * ++v(a)", -3),
        (" * - - v(a)", -3),
        (" * ---v(a)", 9),
        (" * --(v(a)+v(b))", -7),
        (" * --2", -1),
    ],
)
def test_chained_signs_preserve_precedence_in_first_suggestion(
    suffix, expected, monkeypatch
):
    monkeypatch.setitem(_FUZZ_NODES, "a", 3)
    monkeypatch.setitem(_FUZZ_NODES, "b", 2)
    expression = "v(a)-v(b)" + suffix
    card = f".meas tran t when {expression}=0.1"
    rewrite = _file_scope_rewrite(card)
    assert rewrite == f".meas tran t when par('{expression}')=0.1"
    assert _fuzz_value(expression) == pytest.approx(expected)
    sim._validate_meas_card("t", rewrite)
    with pytest.raises(sim.SimError) as excinfo:
        sim._validate_meas_card("t", card, control_scope=True)
    assert _fuzz_value(_control_suggestion(card)["expr"]) == pytest.approx(expected)
    assert "`.meas tran t when vdiff=0.1`" in str(excinfo.value)


def _control_suggestion(card):
    with pytest.raises(sim.SimError) as exc:
        sim._validate_meas_card("t", card, control_scope=True)
    message = str(exc.value)
    spec, _ = json.JSONDecoder().raw_decode(
        message[message.index('{"name": "vdiff"') :]
    )
    return spec


def test_control_suggestion_keeps_signs_in_names_and_exponents():
    expr = "v(net+-x)-v(b) * +-par('v(a)*1e-3')"
    assert _control_suggestion(f".meas tran t find {expr} at=2u")["expr"] == (
        "v(net+-x)-v(b) * -(v(a)*1e-3)"
    )


@pytest.mark.parametrize(
    ("expression", "folded"),
    [
        # issue #7314: a lone `+` is unary in three positions (expression
        # start, after `(`, after an argument comma -- spacing free) ...
        ("+v(a)*v(b)", "v(a)*v(b)"),
        ("+ v(a)", " v(a)"),
        ("(+v(a))", "(v(a))"),
        ("max(v(a), +v(b))", "max(v(a), v(b))"),
        ("max(v(a),+v(b))", "max(v(a),v(b))"),
        ("min(v(a), + v(b))", "min(v(a),  v(b))"),
        ("+1e+3", "1e+3"),
        # ... and binary everywhere else: the operand's own text stays
        ("v(a) + v(b)", "v(a) + v(b)"),
        ("v(a)+v(b)", "v(a)+v(b)"),
        # signs chained behind an operator were folded before and still are
        ("v(a)-+v(b)", "v(a)-v(b)"),
        ("v(a)* +v(b)", "v(a)* v(b)"),
        # exponent literals and vector names keep their own signs
        ("1e+3*v(a)", "1e+3*v(a)"),
        ("v(net+x)", "v(net+x)"),
    ],
)
def test_control_expression_folds_lone_unary_plus_positions(expression, folded):
    assert control_expression(expression) == folded


@SKIP_NO_NGSPICE
@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("v(a)-v(b) * ++v(a,b)", 1),
        ("v(a)-v(b) * - - v(a,b)", 1),
        ("v(a)-v(b) + --v(a,b)", 2),
        ("v(a)-v(b) - +-v(a,b)", 2),
        ("v(a)-v(b) * ---v(a,b)", 5),
        ("v(a)-v(b) * -+v(a,b)", 5),
        ("(v(a)-v(b)) / --par('v(a)*v(b)')", 1 / 6),
        ("(v(a)-v(b)) / --par('v(a)/v(b)')", 2 / 3),
        ("v(a)-v(b) * ++v(a)", -3),
        ("v(a)-v(b) * +-v(a)", 9),
        ("v(a)-v(b) * -+v(a)", 9),
        ("max(v(a), +-v(b))", 3),
        ("abs(+-v(a))*v(b)", 6),
        ("+-v(a)*v(b)", -6),
        ("v(a)*(+-v(b))", -6),
        ("--v(a)*v(b)", 6),
        ("v(a)*(++v(b))", 6),
        ("max(+ - v(a), - - v(b))", 2),
        # issue #7314: a lone unary `+` at the start of the expression,
        # after a `(`, or after an argument comma used to survive the
        # control-scope suggestion, and ngspice rejects the resulting let
        ("+v(a)*v(b)", 6),
        ("max(v(a), +v(b))", 3),
        ("abs(+v(a))*v(b)", 6),
        ("v(a)*(+v(b))", 6),
    ],
)
def test_engine_agrees_with_file_and_control_suggestions(tmp_path, expr, expected):
    card = f".meas tran t find {expr} at=2u"
    suggestion = _file_scope_rewrite(card)
    sim._validate_meas_card("t", suggestion)
    (let,) = sim_steps._flatten_step_measurement(
        _control_suggestion(card), {"name": "probe"}, {}
    )["deck_lines"]
    deck = tmp_path / "probe.cir"
    deck.write_text(
        "signed measurement\nVa a 0 3\nVb b 0 2\n.tran 1u 4u\n"
        f"{suggestion}\n.control\nrun\n{let}\nprint vdiff[0]\nquit\n.endc\n.end\n"
    )
    result = subprocess.run(
        ["ngspice", "-n", "-b", str(deck)], capture_output=True, text=True, check=True
    )
    for name in ("t", "vdiff[0]"):
        match = re.search(
            rf"^{re.escape(name)}\s*=\s*([-+0-9.eE]+)", result.stdout, re.M
        )
        assert match, result.stdout + result.stderr
        assert float(match[1]) == pytest.approx(expected, rel=1e-5)
