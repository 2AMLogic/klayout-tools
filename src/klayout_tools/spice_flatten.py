"""SPICE-text -> SPICE-text subcircuit flattener (issue #2889).

``klt extract``'s output is always flat -- one `.SUBCKT` wrapper around PDK
leaf devices only -- while a design's schematic netlist is normally
hierarchical: user subcircuits instantiated from the top subcircuit. ``klt
pex --flatten-schematic`` uses :func:`flatten_spice` to inline every
*locally defined* user subcircuit down to leaf device cards before the
schematic-side simulation, so both legs (and the ``model_mismatch``
diagnostic) compare like with like.

This is deliberately a *text* flattener, not KLayout's in-process
``Netlist.flatten()`` (what ``klt lvs``'s ``options.flatten_reference``
uses): the output must stay a simulable ngspice deck -- device cards,
``.model`` cards, parameter expressions and file-level control cards all
survive -- which a parsed-and-rewritten ``db.Netlist`` does not guarantee.

Rules (each one is a documented contract, tested in
``tests/test_spice_flatten.py``):

- **What is inlined.** Every `X` card whose target names a `.subckt`
  defined in the same text is replaced by that subcircuit's body,
  recursively. An `X` card naming a subcircuit *not* defined in the text
  (a PDK primitive such as ``sky130_fd_pr__nfet_01v8``, defined in the
  model library the testbench loads) is a **leaf** and is emitted as-is
  (renamed/re-noded like any other device). SPICE text alone cannot tell a
  PDK primitive from a missing user subcircuit, so by default every
  undefined target is a leaf and is listed in
  :attr:`FlattenResult.external_subckts` for the caller to audit; pass
  ``leaf_subckts`` to make an undefined target outside that set a hard
  ``undefined_subckt`` error instead.
- **What is kept.** The *root* subcircuits -- those no other card in the
  text instantiates, plus ``top`` if given -- are emitted as flattened
  `.subckt` definitions with their header verbatim. Non-root definitions are
  dropped (their content now lives inline in every root that used them).
  Every file-level card outside a `.subckt` is emitted verbatim, except a
  file-level `X` call of a local subcircuit, which is inlined too.
- **Naming.** An inlined device ``R1`` reached through instance path
  ``X1`` -> ``X2`` is renamed ``R.X1.X2.R1`` (its first letter, which
  selects the SPICE element type, is preserved), and an internal node
  ``mid`` becomes ``X1.X2.mid`` -- the same ``.``-joined instance-path
  convention ngspice's own internal flattener uses. Subcircuit pins map to
  the caller's actual nodes. Ground (``0``, ``gnd``) and every `.global`
  net are never renamed.
- **Parameters.** An inlined subcircuit's parameter environment is its
  header defaults, overridden by the instance's own ``name=value``
  arguments (themselves evaluated in the caller's environment), then its
  body `.param` cards in order. Every reference to one of those parameters
  inside a ``name=value`` value or a braced/quoted expression of an inlined
  card is substituted textually (parenthesised, inside ``{...}``); cards
  with no parameter references are emitted verbatim apart from renaming.
  A root subcircuit's own parameters are left for ngspice to bind.
- **Multiplicity.** An ``m=`` argument on an inlined instance whose
  subcircuit does not itself declare an ``m`` parameter is ngspice's
  instance multiplier: it is pushed down onto every inlined device card
  (``m={(old)*(mult)}``, or a new ``m=`` argument).
- **`.model` cards** inside an inlined body are hoisted to the enclosing
  root (or file level), de-duplicated by identical text.
- **Hard errors, never a partial flatten** (:class:`SpiceFlattenError`,
  with a machine-readable ``code``): a recursion cycle (``cycle``), an `X`
  call whose node count differs from the subcircuit's pin count
  (``pin_count_mismatch``), an undefined target outside ``leaf_subckts``
  (``undefined_subckt``), a card inside an inlined body this flattener
  cannot rename safely -- a behavioural ``B`` source, a polynomial/
  expression-form ``E``/``G``, a transmission line, a node-naming control
  card such as ``.ic``/``.nodeset``, a nested `.subckt`, an ``.include``
  (``unsupported_card``) -- and unbalanced `.subckt`/`.ends`
  (``malformed``).
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass, field

from ._paths import fold_spice_continuations

__all__ = ["FlattenResult", "SpiceFlattenError", "flatten_spice"]


class SpiceFlattenError(ValueError):
    """The text cannot be flattened faithfully. ``code`` is one of
    ``cycle``, ``pin_count_mismatch``, ``undefined_subckt``,
    ``unsupported_card``, ``malformed``."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class FlattenResult:
    """What :func:`flatten_spice` produced."""

    text: str
    #: Root `.subckt` names kept as (flattened) definitions, file order.
    roots: list[str] = field(default_factory=list)
    #: How many `X` calls of a local subcircuit were inlined (all depths).
    inlined_instances: int = 0
    #: Local subcircuit names that were inlined at least once, sorted.
    inlined_subckts: list[str] = field(default_factory=list)
    #: `X` targets with no local definition, emitted as leaf cards, sorted.
    external_subckts: list[str] = field(default_factory=list)


_GROUND_NODES = frozenset({"0", "gnd"})

_NUMBER_RE = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?[a-zA-Z]*$")
_IDENT_RE = re.compile(r"(?<![\w.$])([A-Za-z_][\w]*)(?![\w(])")

#: Element letters whose leading tokens after the name are exactly N nodes.
_FIXED_NODE_COUNT = {
    "R": 2,
    "C": 2,
    "L": 2,
    "V": 2,
    "I": 2,
    "D": 2,
    "E": 4,
    "G": 4,
    "S": 4,
}
#: Elements whose nodes are "every positional token but the trailing model".
_POSITIONAL_NODES = frozenset("MQJZX")
#: Two nodes followed by the *name* of a controlling voltage source.
_CONTROLLED_BY_SOURCE = frozenset("FHW")
#: Element letters that accept an ``m=`` multiplier.
_MULTIPLIABLE = frozenset("RCLMQDJZX")

_HIERARCHY_SEP = "."

#: ngspice inline-comment rule: ``;`` anywhere, or a ``$`` with whitespace
#: before it and whitespace (or end of line) after it.
_INLINE_COMMENT = re.compile(r"\s\$(?=\s|$)|;")


# --------------------------------------------------------------------------- #
# Lexing
# --------------------------------------------------------------------------- #


def _logical_lines(text: str) -> list[str]:
    """Comment-stripped lines with ``+`` continuations folded.

    Inline comments follow ngspice: ``;`` anywhere, or ``$`` with whitespace
    on both sides (or whitespace before and end of line).  A ``$`` that is
    glued to the next token -- ``.include $PDK_ROOT/...`` -- is *not* a
    comment and survives intact.
    """
    lines: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("*"):
            lines.append("")
            continue
        lines.append(_INLINE_COMMENT.split(raw, maxsplit=1)[0].rstrip())
    return fold_spice_continuations([line for line in lines if line.strip()])


def _lex(line: str) -> list[str]:
    """Whitespace split keeping quoted/braced/parenthesised expressions whole."""
    tokens: list[str] = []
    current: list[str] = []
    quote = ""
    depth = 0
    for ch in line:
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch in "{(":
            depth += 1
        elif ch in "})":
            depth = max(depth - 1, 0)
        elif ch.isspace() and depth == 0:
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(ch)
    if current:
        tokens.append("".join(current))
    return tokens


def _split_tokens(line: str) -> list[str]:
    """:func:`_lex`, then re-join ``name = value`` / ``name= value`` /
    ``name =value`` spellings into one ``name=value`` token."""
    tokens = _lex(line)
    merged: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        has_next = index + 1 < len(tokens)
        if token == "=" and merged and has_next:
            merged[-1] = f"{merged[-1]}={tokens[index + 1]}"
            index += 2
        elif token.endswith("=") and token != "=" and has_next:
            merged.append(token + tokens[index + 1])
            index += 2
        elif token.startswith("=") and token != "=" and merged:
            merged[-1] = merged[-1] + token
            index += 1
        else:
            merged.append(token)
            index += 1
    return merged


def _is_assignment(token: str) -> bool:
    return "=" in token and not token.startswith(("'", '"', "{"))


def _is_params_keyword(token: str) -> bool:
    return token.lower() in ("params:", "param:")


def _split_assignment(token: str) -> tuple[str, str]:
    key, _, value = token.partition("=")
    return key, value


def _unwrap(value: str) -> str:
    """The bare expression inside ``{...}``/``'...'``/``"..."``."""
    if len(value) >= 2 and (
        (value[0] == "{" and value[-1] == "}")
        or (value[0] == value[-1] and value[0] in "'\"")
    ):
        return value[1:-1].strip()
    return value


def _is_literal(expr: str) -> bool:
    return bool(_NUMBER_RE.match(expr.strip()))


def _is_trailing_positional(token: str) -> bool:
    """An area/`off`/expression argument that follows a model name."""
    return _is_literal(token) or token.lower() == "off" or token[:1] in "{'\""


# --------------------------------------------------------------------------- #
# Parameter substitution
# --------------------------------------------------------------------------- #


def _substitute(expr: str, env: dict[str, str]) -> str:
    """``expr`` with every identifier bound in ``env`` replaced by its
    value (parenthesised unless a plain literal or the whole expression)."""
    if not env:
        return expr
    stripped = expr.strip()
    if stripped.lower() in env and _IDENT_RE.fullmatch(stripped):
        return env[stripped.lower()]

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        value = env.get(name.lower())
        if value is None:
            return name
        return value if _is_literal(value) else f"({value})"

    return _IDENT_RE.sub(_replace, expr)


def _render_value(original: str, substituted: str) -> str:
    """Emit a substituted expression: the original token when nothing
    changed, a bare literal, or a braced expression."""
    if substituted == _unwrap(original):
        return original
    if _is_literal(substituted):
        return substituted.strip()
    return "{" + substituted + "}"


def _substitute_token(token: str, env: dict[str, str]) -> str:
    """Substitute inside a ``name=value`` value, or a braced/quoted
    positional expression; any other token is returned unchanged."""
    if not env:
        return token
    if _is_assignment(token):
        key, value = _split_assignment(token)
        unwrapped = _unwrap(value)
        if _is_literal(unwrapped):
            return token
        return f"{key}={_render_value(value, _substitute(unwrapped, env))}"
    if token[:1] in ("{", "'", '"'):
        return _render_value(token, _substitute(_unwrap(token), env))
    return token


def _combine_mult(outer: str | None, inner: str | None) -> str | None:
    if outer is None:
        return inner
    if inner is None:
        return outer
    return f"({outer})*({inner})"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


@dataclass
class _Subckt:
    name: str
    pins: list[str]
    header: str
    #: Ordered ``(lower name, expression)`` header defaults.
    defaults: list[tuple[str, str]]
    body: list[str]


@dataclass
class _Parsed:
    #: File-level items: ``("line", text)`` or ``("subckt", lower name)``.
    items: list[tuple[str, str]]
    subckts: dict[str, _Subckt]
    globals: frozenset[str]


def _parse_header(line: str) -> _Subckt:
    tokens = _split_tokens(line.strip())[1:]
    if not tokens:
        raise SpiceFlattenError(
            f"`.subckt` header with no name: `{line.strip()}`", code="malformed"
        )
    name = tokens[0]
    pins: list[str] = []
    defaults: list[tuple[str, str]] = []
    in_params = False
    for token in tokens[1:]:
        if _is_params_keyword(token):
            in_params = True
            continue
        if _is_assignment(token):
            in_params = True
            key, value = _split_assignment(token)
            defaults.append((key.lower(), _unwrap(value)))
            continue
        if in_params:
            raise SpiceFlattenError(
                f"`.subckt {name}` header has a positional token `{token}` "
                "after its parameters",
                code="malformed",
            )
        pins.append(token)
    return _Subckt(
        name=name, pins=pins, header=line.strip(), defaults=defaults, body=[]
    )


def _directive(line: str) -> str:
    stripped = line.strip()
    if not stripped.startswith("."):
        return ""
    return stripped.split(None, 1)[0].lower()


def _parse(text: str) -> _Parsed:
    items: list[tuple[str, str]] = []
    subckts: dict[str, _Subckt] = {}
    globals_: set[str] = set()
    current: _Subckt | None = None
    for line in _logical_lines(text):
        directive = _directive(line)
        if directive == ".global":
            globals_.update(token.lower() for token in line.split()[1:])
        if directive == ".subckt":
            if current is not None:
                raise SpiceFlattenError(
                    f"nested `.subckt` definition inside `.subckt {current.name}` "
                    "is not supported",
                    code="unsupported_card",
                )
            current = _parse_header(line)
            continue
        if directive == ".ends":
            if current is None:
                raise SpiceFlattenError(
                    "`.ends` with no open `.subckt`", code="malformed"
                )
            key = current.name.lower()
            if key in subckts:
                raise SpiceFlattenError(
                    f"`.subckt {current.name}` is defined more than once",
                    code="malformed",
                )
            subckts[key] = current
            items.append(("subckt", key))
            current = None
            continue
        if current is not None:
            current.body.append(line.strip())
        else:
            items.append(("line", line.strip()))
    if current is not None:
        raise SpiceFlattenError(
            f"`.subckt {current.name}` has no matching `.ends`", code="malformed"
        )
    return _Parsed(items=items, subckts=subckts, globals=frozenset(globals_))


def _x_call(tokens: list[str]) -> tuple[list[str], str, list[str]]:
    """``(nodes, target, parameter tokens)`` of an `X` card."""
    positional: list[str] = []
    params: list[str] = []
    for token in tokens[1:]:
        if _is_params_keyword(token):
            continue
        if _is_assignment(token) or params:
            params.append(token)
        else:
            positional.append(token)
    if not positional:
        raise SpiceFlattenError(
            f"`X` card with no subcircuit name: `{' '.join(tokens)}`",
            code="malformed",
        )
    return positional[:-1], positional[-1], params


# --------------------------------------------------------------------------- #
# Flattening
# --------------------------------------------------------------------------- #


@dataclass
class _Scope:
    """How cards of one subcircuit body are rewritten when emitted."""

    path: str  # "" for a root / file level (no renaming)
    node_map: dict[str, str]
    env: dict[str, str]
    mult: str | None


class _Flattener:
    def __init__(
        self,
        parsed: _Parsed,
        leaf_subckts: Collection[str] | None,
    ) -> None:
        self.parsed = parsed
        self.leaf = (
            None if leaf_subckts is None else {name.lower() for name in leaf_subckts}
        )
        self.inlined_instances = 0
        self.inlined: set[str] = set()
        self.external: dict[str, str] = {}

    # -- naming --------------------------------------------------------- #

    def _node(self, node: str, scope: _Scope) -> str:
        key = node.lower()
        if key in scope.node_map:
            return scope.node_map[key]
        if not scope.path or key in _GROUND_NODES or key in self.parsed.globals:
            return node
        return f"{scope.path}{_HIERARCHY_SEP}{node}"

    @staticmethod
    def _device(name: str, scope: _Scope) -> str:
        if not scope.path:
            return name
        return f"{name[0]}{_HIERARCHY_SEP}{scope.path}{_HIERARCHY_SEP}{name}"

    # -- card rewriting --------------------------------------------------- #

    def _apply_mult(self, tokens: list[str], scope: _Scope) -> list[str]:
        if scope.mult is None:
            return tokens
        letter = tokens[0][0].upper()
        if letter not in _MULTIPLIABLE:
            raise SpiceFlattenError(
                f"cannot push an instance multiplier m={scope.mult} down onto "
                f"`{tokens[0]}` (a `{letter}` element takes no `m=`) inside "
                f"instance path {scope.path}",
                code="unsupported_card",
            )
        out = list(tokens)
        for index, token in enumerate(out):
            if _is_assignment(token):
                key, value = _split_assignment(token)
                if key.lower() == "m":
                    out[index] = f"{key}={{({_unwrap(value)})*({scope.mult})}}"
                    return out
        mult = scope.mult
        out.append(f"m={mult}" if _is_literal(mult) else f"m={{{mult}}}")
        return out

    def _unsupported(self, card: str, scope: _Scope, why: str) -> SpiceFlattenError:
        return SpiceFlattenError(
            f"cannot inline `{card}` inside instance path {scope.path}: {why}",
            code="unsupported_card",
        )

    def _rewrite_card(self, card: str, scope: _Scope) -> str:
        """One non-`X`-local device card, renamed/re-noded/substituted."""
        if not scope.path:
            return card
        tokens = _split_tokens(card)
        letter = tokens[0][0].upper()
        if letter in _FIXED_NODE_COUNT:
            rest = self._rewrite_fixed(
                card, tokens[1:], _FIXED_NODE_COUNT[letter], scope
            )
        elif letter in _CONTROLLED_BY_SOURCE:
            rest = self._rewrite_controlled(card, tokens[1:], scope)
        elif letter == "K":
            rest = self._rewrite_coupling(card, tokens[1:], scope)
        elif letter in _POSITIONAL_NODES:
            rest = self._rewrite_positional(card, tokens[1:], scope)
        else:
            raise self._unsupported(
                card,
                scope,
                f"`{letter}` elements are not supported by the flattener "
                "(their expressions or ports may name nodes it cannot rename)",
            )
        out = [self._device(tokens[0], scope), *rest]
        return " ".join(self._apply_mult(out, scope))

    def _substituted(self, tokens: list[str], scope: _Scope) -> list[str]:
        return [_substitute_token(token, scope.env) for token in tokens]

    def _rewrite_fixed(
        self, card: str, rest: list[str], count: int, scope: _Scope
    ) -> list[str]:
        """``count`` plain nodes, then values/parameters (R/C/L/V/I/D/E/G/S)."""
        nodes = rest[:count]
        if len(nodes) < count or any(
            _is_assignment(node) or "(" in node or node.lower() == "poly"
            for node in nodes
        ):
            raise self._unsupported(
                card, scope, f"expected {count} plain node names after the name"
            )
        return [self._node(node, scope) for node in nodes] + self._substituted(
            rest[count:], scope
        )

    def _rewrite_controlled(
        self, card: str, rest: list[str], scope: _Scope
    ) -> list[str]:
        """Two nodes, then the controlling source's device name (F/H/W)."""
        if len(rest) < 3:
            raise self._unsupported(card, scope, "missing controlling source")
        return [
            self._node(rest[0], scope),
            self._node(rest[1], scope),
            self._device(rest[2], scope),
            *self._substituted(rest[3:], scope),
        ]

    def _rewrite_coupling(self, card: str, rest: list[str], scope: _Scope) -> list[str]:
        """Inductor device names, then the coupling value (K)."""
        names = [token for token in rest if not _is_assignment(token)][:-1]
        if len(names) < 2:
            raise self._unsupported(card, scope, "expected two inductor names")
        return [self._device(name, scope) for name in names] + self._substituted(
            rest[len(names) :], scope
        )

    def _rewrite_positional(
        self, card: str, rest: list[str], scope: _Scope
    ) -> list[str]:
        """Nodes, the model/subcircuit name, then trailing positional
        area/`off` arguments and parameters (M/Q/J/Z/leaf X)."""
        positional: list[str] = []
        for token in rest:
            if _is_assignment(token) or _is_params_keyword(token):
                break
            positional.append(token)
        model_index = len(positional) - 1
        while model_index > 0 and _is_trailing_positional(positional[model_index]):
            model_index -= 1
        if model_index < 1:
            raise self._unsupported(card, scope, "no nodes before the model name")
        return [
            *(self._node(node, scope) for node in positional[:model_index]),
            positional[model_index],
            *self._substituted(rest[model_index + 1 :], scope),
        ]

    # -- bodies ----------------------------------------------------------- #

    def _emit_body(
        self,
        body: list[str],
        scope: _Scope,
        stack: list[str],
        out: list[str],
        models: dict[str, str],
    ) -> None:
        for card in body:
            directive = _directive(card)
            if directive:
                if not scope.path:
                    out.append(card)
                    continue
                if directive in (".param", ".global"):
                    # `.param`: folded into scope.env by _inline; `.global`:
                    # already collected file-wide by _parse.
                    continue
                if directive == ".model":
                    self._hoist_model(card, models, scope)
                    continue
                raise self._unsupported(
                    card,
                    scope,
                    f"`{directive}` cards are not supported inside an "
                    "inlined subcircuit",
                )
            tokens = _split_tokens(card)
            if tokens[0][0].upper() == "X":
                nodes, target, params = _x_call(tokens)
                key = target.lower()
                if key in self.parsed.subckts:
                    self._inline(
                        tokens[0], nodes, key, params, scope, stack, out, models
                    )
                    continue
                if self.leaf is not None and key not in self.leaf:
                    raise SpiceFlattenError(
                        f"`{tokens[0]}` instantiates subcircuit `{target}`, which "
                        "is neither defined in the netlist nor a known leaf device",
                        code="undefined_subckt",
                    )
                self.external.setdefault(key, target)
            out.append(self._rewrite_card(card, scope))

    def _hoist_model(self, card: str, models: dict[str, str], scope: _Scope) -> None:
        tokens = card.split()
        if len(tokens) < 2:
            raise self._unsupported(card, scope, "`.model` card with no name")
        key = tokens[1].lower()
        existing = models.get(key)
        if existing is None:
            models[key] = card
        elif existing != card:
            raise SpiceFlattenError(
                f"two inlined subcircuits define `.model {tokens[1]}` differently "
                f"(`{existing}` vs `{card}`); hoisting both would collide",
                code="unsupported_card",
            )

    def _inline(
        self,
        instance: str,
        nodes: list[str],
        key: str,
        params: list[str],
        parent: _Scope,
        stack: list[str],
        out: list[str],
        models: dict[str, str],
    ) -> None:
        subckt = self.parsed.subckts[key]
        if key in stack:
            chain = " -> ".join([*stack[stack.index(key) :], key])
            raise SpiceFlattenError(
                f"subcircuit instantiation cycle: {chain}", code="cycle"
            )
        if len(nodes) != len(subckt.pins):
            raise SpiceFlattenError(
                f"`{instance}` passes {len(nodes)} node(s) to `.subckt "
                f"{subckt.name}`, which declares {len(subckt.pins)} pin(s) "
                f"({' '.join(subckt.pins)})",
                code="pin_count_mismatch",
            )

        declared = {name for name, _ in subckt.defaults}
        # ngspice scopes parameters hierarchically: a nested body may use a
        # parameter bound on an enclosing instance, so the child's scope
        # starts from the parent's resolved params and its own header
        # defaults, instance overrides and body `.param` layer on top.
        env: dict[str, str] = dict(parent.env)
        for name, expr in subckt.defaults:
            env[name] = _substitute(expr, env)
        mult_expr: str | None = None
        for token in params:
            name, value = _split_assignment(token)
            resolved = _substitute(_unwrap(value), parent.env)
            if name.lower() == "m" and "m" not in declared:
                mult_expr = resolved
                continue
            env[name.lower()] = resolved
        for card in subckt.body:
            if _directive(card) == ".param":
                for token in _split_tokens(card)[1:]:
                    if _is_assignment(token):
                        name, value = _split_assignment(token)
                        env[name.lower()] = _substitute(_unwrap(value), env)

        path_name = (
            f"{parent.path}{_HIERARCHY_SEP}{instance}" if parent.path else instance
        )
        scope = _Scope(
            path=path_name,
            node_map={
                pin.lower(): self._node(node, parent)
                for pin, node in zip(subckt.pins, nodes, strict=True)
            },
            env=env,
            mult=_combine_mult(parent.mult, mult_expr),
        )
        self.inlined_instances += 1
        self.inlined.add(subckt.name)
        self._emit_body(subckt.body, scope, [*stack, key], out, models)

    def emit_root(self, key: str | None, body: list[str]) -> list[str]:
        out: list[str] = []
        models: dict[str, str] = {}
        stack = [key] if key is not None else []
        self._emit_body(
            body, _Scope(path="", node_map={}, env={}, mult=None), stack, out, models
        )
        return list(models.values()) + out


def _instantiated(parsed: _Parsed) -> set[str]:
    called: set[str] = set()
    bodies = [s.body for s in parsed.subckts.values()]
    bodies.append([text for kind, text in parsed.items if kind == "line"])
    for body in bodies:
        for card in body:
            tokens = _split_tokens(card)
            if tokens and tokens[0][0].upper() == "X":
                _nodes, target, _params = _x_call(tokens)
                if target.lower() in parsed.subckts:
                    called.add(target.lower())
    return called


def flatten_spice(
    text: str,
    *,
    top: str | None = None,
    leaf_subckts: Collection[str] | None = None,
    banner: str | None = None,
) -> FlattenResult:
    """Inline every locally defined subcircuit of ``text`` (see the module
    docstring for the full rule set). ``top`` names a subcircuit to keep as
    a (flattened) definition even if something else instantiates it.
    ``leaf_subckts``, when given, turns an `X` target that is neither
    defined locally nor in that set into a hard ``undefined_subckt`` error.
    ``banner`` is emitted as a leading ``*`` comment line.

    Raises :class:`SpiceFlattenError` rather than ever returning a partial
    flatten.
    """
    parsed = _parse(text)
    called = _instantiated(parsed)
    keep = {key for key in parsed.subckts if key not in called}
    if top is not None and top.lower() in parsed.subckts:
        keep.add(top.lower())

    flattener = _Flattener(parsed, leaf_subckts)
    lines: list[str] = [f"* {banner}"] if banner else []
    roots: list[str] = []

    # File-level cards outside any `.subckt` are one pseudo-root, emitted in
    # place; each kept `.subckt` is emitted flattened where it was defined.
    file_level: list[str] = []
    for kind, value in parsed.items:
        if kind == "line":
            file_level.append(value)
            continue
        if file_level:
            lines += flattener.emit_root(None, file_level)
            file_level = []
        if value not in keep:
            continue
        subckt = parsed.subckts[value]
        roots.append(subckt.name)
        lines.append(subckt.header)
        lines += flattener.emit_root(value, subckt.body)
        lines.append(f".ends {subckt.name}")
    if file_level:
        lines += flattener.emit_root(None, file_level)

    return FlattenResult(
        text="\n".join(lines) + "\n",
        roots=roots,
        inlined_instances=flattener.inlined_instances,
        inlined_subckts=sorted(flattener.inlined, key=str.lower),
        external_subckts=sorted(flattener.external.values(), key=str.lower),
    )
