"""Liberty boolean-function parsing and combinational timing-arc derivation
for ``klt characterize`` (issue #2502).

A characterization run needs two things the caller's pin metadata does not
state directly:

1. **Which arcs exist.** An arc is an (output pin, related input pin) pair
   where toggling the input can toggle the output. That is a property of the
   output's Liberty ``function``, not of the pin list -- ``sg13g2_a21o_1``'s
   ``function : "(A1 A2) + B1"`` has three arcs, ``sg13g2_inv_1``'s
   ``function : "!(A)"`` has one, and a tie cell has none.
2. **A sensitizing vector.** Measuring the A->Y arc of a NAND2 with B low
   measures nothing: the output never moves. The other inputs must be held
   at values under which the related input actually propagates -- what a
   commercial characterizer calls the arc's side-input state.

Both fall out of exhaustively evaluating the parsed function over the input
cube. Standard cells have at most a handful of inputs (the widest in IHP's
``sg13g2_stdcell`` has five), so ``2**n`` evaluation is cheap and, unlike a
hand-written table of per-cell-family non-controlling values, cannot be
wrong for a cell nobody anticipated. :func:`derive_arcs` is deterministic:
side-input vectors are enumerated in a fixed order and the first sensitizing
one wins, so the same metadata always produces the same arc list and the
same stimulus.

``timing_sense`` is reported from the *whole* set of sensitizing vectors
(all-rising => ``positive_unate``, all-falling => ``negative_unate``, mixed
=> ``non_unate``), while ``measured_sense`` reports the polarity at the one
vector the run will actually drive -- the two differ exactly for a
non-unate arc (XOR/XNOR), and it is ``measured_sense`` that tells the
stimulus builder which input edge produces the rising output edge. See
``docs/cli/characterize.md``'s "Non-unate arcs" for what a single-vector
non-unate characterization does and does not claim.

Grammar accepted (Liberty's own ``function`` expression syntax, precedence
highest-first):

| Construct | Spelling |
|---|---|
| grouping | ``( ... )`` |
| invert | prefix ``!X``, postfix ``X'`` |
| AND | ``X & Y``, ``X * Y``, or juxtaposition ``X Y`` |
| XOR | ``X ^ Y`` |
| OR | ``X + Y``, ``X \\| Y`` |
| constant | ``0``, ``1`` |

Not accepted: ``when``-qualified expressions, bus/bundle subscripting beyond
a literal ``name[i]`` identifier, and the sequential-cell
``next_state``/``clocked_on`` attributes -- a function referencing anything
that is not a declared input pin is a hard error rather than a silently
dropped term.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: One token of a Liberty function expression. Identifiers may carry a bus
#: subscript (``A[0]``) because ``pin`` names legally do.
_TOKEN_RE = re.compile(r"\s*([A-Za-z_][A-Za-z_0-9]*(?:\[\d+\])?|[()!&*+|^'01])")


class FunctionError(Exception):
    """Raised for a Liberty ``function`` expression this module cannot parse,
    or one referencing a name that is not a declared input pin."""


@dataclass(frozen=True)
class DerivedArc:
    """One combinational timing arc, plus the stimulus state it must be
    measured under.

    - ``timing_sense`` is what the emitted ``.lib`` declares: the arc's
      polarity across *every* sensitizing vector.
    - ``measured_sense`` is the polarity at ``side_inputs`` specifically --
      ``"positive_unate"`` means driving the related pin high drives the
      output high, so the *rising* input edge is the one that produces
      ``cell_rise``/``rise_transition``. Equal to ``timing_sense`` except on
      a ``non_unate`` arc.
    - ``side_inputs`` maps every *other* input pin the function references to
      the constant value it is held at for the whole measurement.
    """

    output_pin: str
    related_pin: str
    timing_sense: str
    measured_sense: str
    side_inputs: tuple[tuple[str, bool], ...]

    @property
    def side_input_map(self) -> dict[str, bool]:
        return dict(self.side_inputs)


def parse_function(expression: str, *, allowed_pins: frozenset[str]) -> _Expr:
    """Parse a Liberty ``function`` expression into an evaluatable tree.

    ``allowed_pins`` is the set of names the expression may reference (the
    cell's declared input pins). A reference to anything else raises
    :class:`FunctionError` -- that is the signal that the cell is sequential
    (``function`` naming an internal ``IQ``/``IQN`` node), that a pin is
    missing from the request's metadata, or that the expression is for a
    different cell entirely. Silently treating an unknown name as a free
    variable would produce a plausible-looking arc list for the wrong cell.
    """
    tokens = _tokenize(expression)
    parser = _Parser(tokens, expression, allowed_pins)
    expr = parser.parse_or()
    parser.expect_end()
    return expr


def derive_arcs(
    *,
    output_pin: str,
    function: str,
    input_pins: tuple[str, ...],
) -> tuple[DerivedArc, ...]:
    """Derive every combinational arc into ``output_pin`` from its Liberty
    ``function``, in ``input_pins`` order.

    Returns an empty tuple for a function that depends on no input (a tie
    cell, or a constant expression) -- a caller that requires at least one
    arc should say so itself, with its own error wording.
    """
    expr = parse_function(function, allowed_pins=frozenset(input_pins))
    referenced = tuple(pin for pin in input_pins if pin in expr.pins)

    arcs: list[DerivedArc] = []
    for related in referenced:
        side = tuple(pin for pin in referenced if pin != related)
        senses: list[str] = []
        chosen: tuple[tuple[str, bool], ...] | None = None
        for assignment in _enumerate_assignments(side):
            state = dict(assignment)
            state[related] = False
            low = expr.evaluate(state)
            state[related] = True
            high = expr.evaluate(state)
            if low == high:
                continue
            sense = "positive_unate" if high else "negative_unate"
            senses.append(sense)
            if chosen is None:
                chosen = (assignment, sense)
        if chosen is None:
            continue
        assignment, measured_sense = chosen
        distinct = set(senses)
        timing_sense = distinct.pop() if len(distinct) == 1 else "non_unate"
        arcs.append(
            DerivedArc(
                output_pin=output_pin,
                related_pin=related,
                timing_sense=timing_sense,
                measured_sense=measured_sense,
                side_inputs=assignment,
            )
        )
    return tuple(arcs)


def _enumerate_assignments(
    side: tuple[str, ...],
) -> list[tuple[tuple[str, bool], ...]]:
    """Every assignment of ``side``, enumerated so that all-low comes first
    and each name's ``False`` value is tried before its ``True`` value.

    The order is what makes :func:`derive_arcs` deterministic, and it also
    happens to pick the conventional non-controlling value for the common
    gates: a NAND/AND's other inputs settle on high (all-low is not
    sensitizing), an NOR/OR's on low.
    """
    assignments: list[tuple[tuple[str, bool], ...]] = []
    for code in range(1 << len(side)):
        assignments.append(
            tuple((name, bool((code >> index) & 1)) for index, name in enumerate(side))
        )
    return assignments


# --------------------------------------------------------------------------- #
# Expression tree
# --------------------------------------------------------------------------- #


class _Expr:
    """Base class for a parsed function expression."""

    #: Every input-pin name this subtree references.
    pins: frozenset[str] = frozenset()

    def evaluate(self, state: dict[str, bool]) -> bool:  # pragma: no cover
        raise NotImplementedError


class _Const(_Expr):
    def __init__(self, value: bool) -> None:
        self.value = value

    def evaluate(self, state: dict[str, bool]) -> bool:
        return self.value


class _Pin(_Expr):
    def __init__(self, name: str) -> None:
        self.name = name
        self.pins = frozenset({name})

    def evaluate(self, state: dict[str, bool]) -> bool:
        return state[self.name]


class _Not(_Expr):
    def __init__(self, operand: _Expr) -> None:
        self.operand = operand
        self.pins = operand.pins

    def evaluate(self, state: dict[str, bool]) -> bool:
        return not self.operand.evaluate(state)


class _Binary(_Expr):
    #: ``"and"`` / ``"or"`` / ``"xor"``.
    op = ""

    def __init__(self, operands: list[_Expr]) -> None:
        self.operands = operands
        self.pins = frozenset().union(*(operand.pins for operand in operands))


class _And(_Binary):
    op = "and"

    def evaluate(self, state: dict[str, bool]) -> bool:
        return all(operand.evaluate(state) for operand in self.operands)


class _Or(_Binary):
    op = "or"

    def evaluate(self, state: dict[str, bool]) -> bool:
        return any(operand.evaluate(state) for operand in self.operands)


class _Xor(_Binary):
    op = "xor"

    def evaluate(self, state: dict[str, bool]) -> bool:
        result = False
        for operand in self.operands:
            result ^= operand.evaluate(state)
        return result


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #

_PRIMARY_START = frozenset({"(", "!", "0", "1"})


def _tokenize(expression: str) -> list[str]:
    tokens: list[str] = []
    position = 0
    while position < len(expression):
        if expression[position].isspace():
            # Whitespace is significant in Liberty (juxtaposition is AND),
            # but only as a *separator*: the parser recovers the implicit AND
            # from two adjacent primaries, so the token stream does not need
            # to carry the space itself.
            position += 1
            continue
        match = _TOKEN_RE.match(expression, position)
        if match is None:
            raise FunctionError(
                f"cannot parse Liberty function {expression!r}: unexpected "
                f"character {expression[position]!r} at offset {position}"
            )
        tokens.append(match.group(1))
        position = match.end()
    if not tokens:
        raise FunctionError("cannot parse an empty Liberty function expression")
    return tokens


class _Parser:
    def __init__(
        self,
        tokens: list[str],
        expression: str,
        allowed_pins: frozenset[str],
    ) -> None:
        self.tokens = tokens
        self.position = 0
        self.expression = expression
        self.allowed_pins = allowed_pins

    def peek(self) -> str | None:
        if self.position >= len(self.tokens):
            return None
        return self.tokens[self.position]

    def next(self) -> str:
        token = self.peek()
        if token is None:
            raise FunctionError(
                f"cannot parse Liberty function {self.expression!r}: "
                "expression ends unexpectedly"
            )
        self.position += 1
        return token

    def expect_end(self) -> None:
        if self.position != len(self.tokens):
            raise FunctionError(
                f"cannot parse Liberty function {self.expression!r}: "
                f"unexpected trailing token {self.tokens[self.position]!r}"
            )

    def parse_or(self) -> _Expr:
        operands = [self.parse_xor()]
        while self.peek() in ("+", "|"):
            self.next()
            operands.append(self.parse_xor())
        return operands[0] if len(operands) == 1 else _Or(operands)

    def parse_xor(self) -> _Expr:
        operands = [self.parse_and()]
        while self.peek() == "^":
            self.next()
            operands.append(self.parse_and())
        return operands[0] if len(operands) == 1 else _Xor(operands)

    def parse_and(self) -> _Expr:
        operands = [self.parse_unary()]
        while True:
            token = self.peek()
            if token in ("&", "*"):
                self.next()
                operands.append(self.parse_unary())
                continue
            # Juxtaposition: `A B` and `(A1 A2) + B1` are ANDs with no
            # operator token at all.
            if token is not None and (token in _PRIMARY_START or _is_identifier(token)):
                operands.append(self.parse_unary())
                continue
            break
        return operands[0] if len(operands) == 1 else _And(operands)

    def parse_unary(self) -> _Expr:
        if self.peek() == "!":
            self.next()
            return _Not(self.parse_unary())
        return self.parse_postfix()

    def parse_postfix(self) -> _Expr:
        expr = self.parse_primary()
        while self.peek() == "'":
            self.next()
            expr = _Not(expr)
        return expr

    def parse_primary(self) -> _Expr:
        token = self.next()
        if token == "(":
            expr = self.parse_or()
            closing = self.next()
            if closing != ")":
                raise FunctionError(
                    f"cannot parse Liberty function {self.expression!r}: "
                    f"expected ')' but found {closing!r}"
                )
            return expr
        if token in ("0", "1"):
            return _Const(token == "1")
        if not _is_identifier(token):
            raise FunctionError(
                f"cannot parse Liberty function {self.expression!r}: "
                f"unexpected token {token!r}"
            )
        if token not in self.allowed_pins:
            raise FunctionError(
                f"Liberty function {self.expression!r} references "
                f"{token!r}, which is not a declared input pin of this cell "
                f"(declared: {', '.join(sorted(self.allowed_pins)) or 'none'})"
                " -- a sequential cell or missing pin metadata, not a "
                "combinational arc this command can characterize"
            )
        return _Pin(token)


def _is_identifier(token: str) -> bool:
    return token[0].isalpha() or token[0] == "_"
