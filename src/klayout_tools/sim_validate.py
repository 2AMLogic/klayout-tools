"""Pure-input validation of ``klt sim``'s measurement specs (split out of sim.py).

Validates the ``measurements[]`` / ``options.fail_on_diagnostic`` request fields
and raises :class:`~klayout_tools.sim_errors.SimError`; nothing depends on
``run_sim`` or the corner runner. ``sim.py`` re-exports every name (self-aliased)
and ``sim_steps`` imports from here directly (issue #3689; precedent
``sim_remote.py``, #1764)."""

from __future__ import annotations

import re
from typing import Any

from . import (
    sim_crossings,
    sim_cycle_mean,
    sim_energy,
    sim_eye,
    sim_noise,
    sim_search,
    sim_settled,
    sim_snm_meas,
    sim_step_resolution,
    sim_stress,
    sim_tian,
    sim_transfer_meas,
)
from .sim_case_names import validate_case_variant_names
from .sim_errors import SimError

#: A `.meas` card's declared analysis type (the token right after
#: ``.meas``/``.measure``), e.g. ``tran`` in ``.meas tran vout find v(out)``.
_MEAS_CARD_TYPE_RE = re.compile(r"^\s*\.meas(?:ure)?\s+(\S+)", re.IGNORECASE)

#: A value position in a `.meas` card: an identifier right after ``=`` that is
#: not a function call (``v(x)``/``i(x)`` never match -- the negative
#: lookahead rejects a following ``(``). Issue #3038.
_MEAS_VALUE_REF_RE = re.compile(r"=\s*([A-Za-z_]\w*)\b(?!\s*\()")

#: A ``par('...')`` expression span in a `.meas` card (issue #5651). ngspice
#: lifts the quoted expression into an internal ``pa_NN`` helper vector, so a
#: wrapped expression *is* a single vector reference as far as a signal
#: position is concerned. Matched on the original card both to blank those
#: spans out of the adjacency scan (length-preserving, so offsets still index
#: the original) and to unwrap them again when the refusal quotes the
#: author's own expression. Case-insensitive: ngspice card syntax is.
_MEAS_PAR_SPAN_RE = re.compile(r"par\s*\(\s*'[^']*'\s*\)", re.IGNORECASE)

#: A ``v(...)``/``i(...)``-style vector reference in a `.meas` card, with its
#: contents captured so a two-node ``v(a,b)`` can be detected (ngspice
#: resolves no such vector in a `.meas` signal position -- issue #5651).
#: Run on the par-blanked card, so references inside a ``par('...')`` span
#: are not seen here.
_MEAS_VECTOR_REF_RE = re.compile(r"\b[vViI]\s*\(([^)]*)\)")

#: ngspice's line-continuation marker: a ``+`` as the first non-blank
#: character of a line (measured working on a ``when`` card, issue #5651).
#: Blanked out (length-preserving) before the adjacency scan so the
#: continuation ``+`` is not read as an arithmetic operator.
_MEAS_LINE_CONTINUATION_RE = re.compile(r"(?m)^[ \t]*\+")

#: ngspice's two-node voltage reference: ``v(a,b)``. Accepted in a
#: ``print``/plot context but resolved as no vector in a `.meas` signal
#: position (issue #5651); the expressible equivalent is ``v(a)-v(b)``.
_MEAS_TWO_NODE_REF_RE = re.compile(r"([vViI])\s*\(\s*([^,()\s]+)\s*,\s*([^()]+?)\s*\)")

#: The arithmetic operators a `.meas` signal-position expression may be
#: built from.
_MEAS_ARITH_OPS = ("+", "-", "*", "/")

#: ngspice's own ``.MEASURE`` statement only implements these analysis types
#: (verified empirically against ngspice 46 -- see issue #205). Notably,
#: there is no ``.MEASURE OP``: an operating-point analysis has no sweep
#: variable for a measurement to search over, unlike DC/AC/TRAN/SP.
_SUPPORTED_MEAS_TYPES = frozenset({"dc", "ac", "tran", "sp"})

#: A ``measurements[].expr`` measurement's name (issue #2533). Stricter than
#: ``measurements[].name`` for a ``spice`` card, and deliberately so: an
#: ``expr`` name is *emitted into the deck* as an ngspice vector name
#: (``let <name> = ...``) and then read back out of the log by
#: :data:`_MEAS_VALUE_RE`, so it has to be simultaneously legal on both
#: sides. ngspice vector names admit no ``.`` (which `_MEAS_VALUE_RE` would
#: otherwise accept) and no leading digit; anything else would either fail
#: to parse in the deck or fail to harvest from the log, both of which would
#: surface as the unhelpfully generic "produced no value".
_EXPR_MEAS_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: ``measurements[].on_unreached`` (issue #3108): what a ``spice`` ``.meas``
#: card that the completed analysis never satisfied (a ``WHEN``/``AT`` target
#: outside the swept range) is graded as. ``"error"`` (the default) is today's
#: behaviour byte-for-byte; ``"null"`` opts into :data:`NOT_REACHED_STATUS`.
ON_UNREACHED_VALUES = ("error", "null")


def _blank_par_spans_and_continuations(spice: str) -> str:
    """A length-preserving copy of *spice* with every ``par('...')`` span and
    every line-continuation ``+`` blanked to spaces.

    Offsets in the copy index the original card, while operators *inside* a
    ``par(...)`` expression and continuation markers are invisible to the
    adjacency scan (issue #5651: ``when par('v(a)-v(b)')=0.3`` works, and a
    ``+``-continued ``when`` card is legal).
    """
    blanked = _MEAS_PAR_SPAN_RE.sub(lambda m: " " * len(m.group(0)), spice)
    return _MEAS_LINE_CONTINUATION_RE.sub(lambda m: m.group(0)[:-1] + " ", blanked)


def _par_span_inner(span: str) -> str:
    """The quoted expression of a ``par('...')`` span, without the wrapper."""
    return span[span.index("'") + 1 : span.rindex("'")]


def _unwrap_par_spans(text: str) -> str:
    """Replace every ``par('...')`` span in *text* with its quoted
    expression, so a suggested rewrite holds one ``par('...')`` wrapper
    around the whole expression (``par('v(a)-v(b)')-v(c)`` ->
    ``v(a)-v(b)-v(c)``)."""
    return _MEAS_PAR_SPAN_RE.sub(lambda m: _par_span_inner(m.group(0)), text)


def _two_node_difference(text: str) -> str:
    """``v(a,b)`` -- ngspice's two-node voltage -- as the ``v(a)-v(b)`` a
    ``par('...')`` wrapper can hold (issue #5651: ``v(a,b)`` resolves no
    such vector in a `.meas` signal position). Text that is not exactly a
    two-node reference is returned unchanged."""
    match = _MEAS_TWO_NODE_REF_RE.fullmatch(text.strip())
    if match is None:
        return text
    func, first, second = match.group(1), match.group(2), match.group(3)
    return f"{func}({first})-{func}({second})"


def _token_end(spice: str, start: int) -> int | None:
    """End index of the operand token beginning at *start* -- a quoted span,
    a parenthesised group, or a bare number/identifier run -- or ``None``
    when no operand starts there."""
    if start >= len(spice):
        return None
    ch = spice[start]
    if ch == "'":
        close = spice.find("'", start + 1)
        return None if close == -1 else close + 1
    if ch == "(":
        depth = 0
        for index in range(start, len(spice)):
            if spice[index] == "(":
                depth += 1
            elif spice[index] == ")":
                depth -= 1
                if depth == 0:
                    return index + 1
        return None
    match = re.match(r"[A-Za-z0-9_.]+", spice[start:])
    return None if match is None else start + match.end()


def _token_start(spice: str, end: int) -> int | None:
    """Start index of the operand token whose last character is at *end*,
    or ``None`` when no operand ends there."""
    if end < 0:
        return None
    ch = spice[end]
    if ch == "'":
        open_quote = spice.rfind("'", 0, end)
        return None if open_quote == -1 else open_quote
    if ch == ")":
        depth = 0
        for index in range(end, -1, -1):
            if spice[index] == ")":
                depth += 1
            elif spice[index] == "(":
                depth -= 1
                if depth == 0:
                    return index
        return None
    match = re.search(r"[A-Za-z0-9_.]+$", spice[: end + 1])
    return None if match is None else match.start()


def _extend_over_operands(spice: str, start: int, end: int) -> tuple[int, int]:
    """Widen a lone flagged reference over the operand it is bound to by an
    outer arithmetic operator (``2*v(a)`` or ``v(a)*2``), so the refusal's
    example rewrite wraps the whole local expression in ``par('...')``
    rather than only the reference."""
    while True:
        left = start
        while left > 0 and spice[left - 1].isspace():
            left -= 1
        if left > 0 and spice[left - 1] in _MEAS_ARITH_OPS:
            cursor = left - 1
            while cursor > 0 and spice[cursor - 1].isspace():
                cursor -= 1
            found = _token_start(spice, cursor - 1)
            if found is None:
                break
            start = found
            continue
        right = end
        while right < len(spice) and spice[right].isspace():
            right += 1
        if right < len(spice) and spice[right] in _MEAS_ARITH_OPS:
            cursor = right + 1
            while cursor < len(spice) and spice[cursor].isspace():
                cursor += 1
            found = _token_end(spice, cursor)
            if found is None:
                break
            end = found
            continue
        break
    return start, end


def _find_meas_signal_expression(spice: str) -> tuple[int, int, str] | None:
    """Locate an arithmetic expression in a `.meas` card's signal position.

    ngspice's ``.MEASURE`` accepts only a single vector reference
    (``v(node)``, ``i(source)``, or a bare vector name) in every signal
    position -- ``WHEN``/``FIND``/``AVG``/``MAX``/``TRIG``/``TARG``. The
    rule fires on exactly that and nothing else (issue #5651, every case
    measured on ngspice-47): references or ``par('...')`` spans connected
    by ``+ - * /`` operators (spaces ignored), a comma inside a reference
    (the two-node ``v(a,b)``), or a lone reference bound to an operand by
    an outer operator (``2*v(a)``).

    Returns ``(start, end, text)`` -- the expression's span in *spice* and
    its verbatim text -- or ``None`` when every reference stands alone:
    ``when v(a)=0.5``, ``when a=0.5`` (a bare identifier left-hand side is
    legal), ``i(V1)=-0.0005`` (the ``-`` binds a number), and the #2826
    right-hand-side cases ``when v(a)=0.5*1.62`` / ``find v(a) at='1u+2n'``
    (operators between *numbers*, never adjacent to a reference) all stay
    unflagged.
    """
    blanked = _blank_par_spans_and_continuations(spice)
    items: list[tuple[int, int, bool]] = []  # (start, end, comma inside ref)
    for match in _MEAS_VECTOR_REF_RE.finditer(blanked):
        items.append((match.start(), match.end(), "," in match.group(1)))
    for match in _MEAS_PAR_SPAN_RE.finditer(spice):
        items.append((match.start(), match.end(), False))
    if not items:
        return None
    items.sort()
    # Merge operator-connected neighbours into expression groups.
    groups: list[list[int]] = []  # [start, end, item count, comma inside]
    for start, end, comma in items:
        if groups:
            between = blanked[groups[-1][1] : start].strip()
            if between and all(op in _MEAS_ARITH_OPS for op in between):
                groups[-1][1] = end
                groups[-1][2] += 1
                continue
        groups.append([start, end, 1, comma])
    for start, end, count, comma in groups:
        if count >= 2 or comma:
            return (start, end, spice[start:end])
        before = blanked[:start].rstrip()
        after = blanked[end:].lstrip()
        if before.endswith(_MEAS_ARITH_OPS) or after.startswith(_MEAS_ARITH_OPS):
            lo, hi = _extend_over_operands(spice, start, end)
            return (lo, hi, spice[lo:hi])
    return None


def _validate_meas_card(name: str, spice: str, control_scope: bool = False) -> None:
    """Reject a ``.meas`` card declaring an analysis type ngspice's own
    ``.MEASURE`` statement does not implement.

    Most notably ``"op"``: it is a legitimate ``analysis.kind`` (ngspice's
    operating-point analysis runs fine as a plain ``op`` control-block
    command), but there is no such thing as ``.MEASURE OP`` -- an operating
    point has no sweep variable for a measurement to search over the way
    DC/AC/TRAN/SP do. Pairing ``analysis.kind: "op"`` with a ``.meas op``
    card fails with ngspice's own
    ``Error: unrecognized analysis type 'op' for the following .meas
    statement`` (verified against ngspice 46, see issue #205) -- this
    catches it before the request ever reaches ngspice, with an actionable
    message instead of a raw parser error. Applies to every ``.meas`` card
    regardless of the request's own ``analysis.kind``: a ``.meas`` type
    ngspice does not implement is invalid on its own terms, not just when
    paired with a mismatched analysis.

    It also refuses a card carrying an arithmetic expression in a signal
    position (issue #5651): ngspice's ``.MEASURE`` accepts only a single
    vector reference in every signal position -- ``WHEN``, ``FIND``,
    ``AVG``, ``MAX``, ``TRIG``, ``TARG`` -- so ``when v(a)-v(b)=0.3`` fails
    *every* corner with "no such vector", while the same quantity wrapped
    as ``when par('v(a)-v(b)')=0.3`` works (ngspice lifts the quoted
    expression into an internal helper vector). The refusal is raised
    before any corner runs (``--dry-run`` included), quotes the author's
    own expression and its ``par('...')`` rewrite, and points at
    ``measurements[].crossings`` for the cases that form serves better
    (a crossing *series*, or a supply-relative ``level``) with its
    count-vs-time trade-off stated. With ``control_scope=True``
    (``analysis_steps[]`` cards, emitted as ``meas`` commands inside the
    generated ``.control`` block) the remedy named is instead an
    intermediate ``expr`` measurement referenced by name: ``par('...')``
    is a file-scope feature ngspice rejects in control scope, where a
    ``let`` helper line is the working route.

    The expression rule itself is deliberately narrow -- see
    :func:`_find_meas_signal_expression` -- so none of the measured-good
    shapes is flagged: a bare identifier left-hand side, an operator
    between numbers on the right-hand side (the #2826 case, which stays a
    runtime diagnostic), a ``par('...')``-wrapped expression, upper-case
    spellings, and ``+``-continued cards.

    A card this function does not recognise as a ``.meas``/``.measure``
    line (unexpected shape) is left alone -- validating hand-written SPICE
    syntax in general is out of scope here.
    """
    match = _MEAS_CARD_TYPE_RE.match(spice)
    if match is None:
        return
    meas_type = match.group(1).lower()
    if meas_type not in _SUPPORTED_MEAS_TYPES:
        raise SimError(
            f"measurement '{name}' declares an unsupported .meas analysis "
            f"type '{meas_type}': ngspice has no `.MEASURE {meas_type.upper()}` "
            "analysis type; declare the quantity as an expression instead "
            f'(measurements[].expr, e.g. {{"name": "{name}", "expr": '
            '"v(out)"}), which `klt sim` evaluates after the analysis inside '
            'its own `.control` block -- or use analysis.kind: "tran" with '
            "a short single-step transient and `.meas tran ... at=<t>`"
        )
    found = _find_meas_signal_expression(spice)
    if found is None:
        return
    start, end, expression = found
    inner = _two_node_difference(_unwrap_par_spans(expression))
    rewrite = f"{spice[:start]}par('{inner}'){spice[end:]}"
    if control_scope:
        reference = f"{spice[:start]}vdiff{spice[end:]}"
        raise SimError(
            f"measurement '{name}': a control-scope meas command's signal "
            "position takes a single vector reference, not the arithmetic "
            f"expression '{expression}' -- ngspice resolves no such vector "
            "and the card fails on every corner. The file-scope expression "
            "wrapper is not accepted inside a .control block; define the "
            "expression as an intermediate measurement declared before this "
            f'card ({{"name": "vdiff", "expr": "{inner}", "intermediate": '
            "true} -- `klt sim` emits it as a `let vdiff = "
            f"{inner}` control line in declared order) and reference that "
            f"name in the card instead: `{reference}`"
        )
    raise SimError(
        f"measurement '{name}': a .meas signal position takes a single "
        "vector reference, not the arithmetic expression "
        f"'{expression}' -- ngspice resolves no such vector and the card "
        "fails on every corner. Wrap the whole expression in a par('...') "
        f"span: `{rewrite}`. For a whole crossing series, or a "
        'supply-relative level ({"frac_of": "vdd", "fraction": 0.5}) that '
        "`klt sim` resolves per corner, a measurements[].crossings entry "
        "also fits -- but a crossings entry's value is the *count* of "
        "crossings, not a time (the crossing times are reported on its "
        "crossings.times[] array)"
    )


def _validate_no_dependent_meas_cards(measurements_spec: list[dict[str, Any]]) -> None:
    """Refuse a ``spice`` card whose value field names another measurement.

    A file-scope ``.meas`` card resolves the right-hand side of ``at=``,
    ``from=``, ``to=``, ``td=``, ``val=`` ... as a netlist ``.param``, never as
    an earlier ``.meas`` result, so ``.meas tran vos find v(rmp) at=t_flip``
    kills the whole deck at parse time (``Undefined parameter [t_flip]``) and
    every measurement in the unit reads ``produced no value`` (issue #3038).
    This names the dependent card up front instead.

    Deliberately narrow: only an identifier directly after ``=`` (not a
    function call such as ``v(vout)``) that case-insensitively equals the
    ``name`` of a *different* ``measurements[]`` entry. A card referencing a
    real netlist ``.param`` that happens to share a measurement name is
    refused too -- that is ambiguous in ngspice anyway. Only ``spice`` cards
    are scanned (``expr`` has its own rule, see ``docs/cli/sim.md``);
    ``analysis_steps[]`` never reaches here (control-scope ``meas`` has
    different semantics).
    """
    names = {
        str(spec["name"]).lower(): spec["name"]
        for spec in measurements_spec
        if spec.get("name") is not None
    }
    for spec in measurements_spec:
        spice = spec.get("spice")
        if not isinstance(spice, str):
            continue
        own = str(spec["name"]).lower()
        for ref in _MEAS_VALUE_REF_RE.findall(spice):
            key = ref.lower()
            if key in names and key != own:
                raise SimError(
                    f"measurement '{spec['name']}' references measurement "
                    f"'{names[key]}' in its .meas card: a file-scope .meas "
                    "card cannot read another .meas result (ngspice resolves "
                    f"'{ref}' as a netlist .param and fails with 'Undefined "
                    "parameter'). Fold both into one self-contained card "
                    "(e.g. `.meas tran vos find v(rmp) when v(dn)=0.5 "
                    "rise=1`), measure the interval with trig/targ, or "
                    "compute it with measurements[].expr over saved vectors"
                )


def _validate_noise_measurement(
    name: Any, spec: dict[str, Any], has_other_form: bool
) -> None:
    """Issue #2938: ``noise`` is a form mutually exclusive with the others."""
    if has_other_form or name is None:
        raise SimError(
            f"measurement '{name}' declares 'noise' together with "
            "another form (or has no name): declare a 'name' and "
            "exactly one of 'spice', 'expr', 'crossings', 'cycle_mean', "
            "'energy', 'stress' or 'noise'"
        )
    sim_noise.validate_noise_spec(name, spec["noise"])
    if not _EXPR_MEAS_NAME_RE.match(str(name)):
        raise SimError(
            f"measurement '{name}' cannot be declared with 'noise': its "
            "name becomes an ngspice vector name, so it must match "
            "[A-Za-z_][A-Za-z0-9_]* (no dots, no leading digit)"
        )


def _validate_measurement_forms(measurements_spec: list[dict[str, Any]]) -> None:
    """Validate the *form* of every ``request.measurements[]`` entry: that it
    declares exactly one of ``spice`` (a verbatim top-level ``.meas`` card --
    the only form before issue #2533) or ``expr`` (an ngspice expression
    ``klt sim`` evaluates after the analysis, inside its own ``.control``
    block), and that an ``expr`` entry's name/expression are emittable and
    harvestable.

    Why ``expr`` exists at all (issue #2533): a top-level ``.meas`` card is
    the *only* measurement shape ngspice's batch mode accepts at file scope,
    and ``.MEASURE`` implements no ``op`` analysis type
    (:data:`_SUPPORTED_MEAS_TYPES`) -- so an operating-point quantity, or any
    quantity that is a reduction/combination of saved vectors rather than a
    single card's search, had no expression in a request at all. The
    *harvesting* side already tolerated it: a ``print``ed scalar lands in the
    log as ``<name> = <value>``, the shape :data:`_MEAS_VALUE_RE` reads (see
    that pattern's own note about the print fallback). Only the emitting side
    was closed, because the netlist convention gives ``klt sim`` sole
    ownership of the deck's single ``.control`` block (see this module's
    docstring). ``expr`` is that block's caller-facing seam -- declarative
    (one expression bound to one response name), so it composes with
    ``corners.*``/``monte_carlo`` exactly like a ``.meas`` card does rather
    than needing its own reduction contract.

    Validation rules, and why each is a hard error rather than a best-effort:

    - **Exactly one of ``spice``/``expr``.** Both would emit the measurement
      twice under one name (a file-scope ``.meas`` card *and* a
      ``let``/``print`` pair) and leave which one won up to log ordering.
      Neither leaves nothing to run.
    - **``expr`` is a single non-empty line.** It is spliced verbatim into a
      generated ``.control`` block; an embedded newline would silently turn
      the tail of the expression into a *separate control command*.
    - **``expr`` names match :data:`_EXPR_MEAS_NAME_RE`.** The name is both
      an ngspice vector name in the deck and the key read back out of the
      log; see that pattern's docstring.
    - **An ``expr`` name is unique across the whole ``measurements[]``
      list.** A duplicate ``let`` silently overwrites the earlier vector, so
      both response entries would report the *second* expression's value.
      Only checked for entries involving an ``expr`` -- a request whose
      duplicate names are all ``spice`` cards behaves exactly as it did
      before this issue (ngspice's own ``.meas`` handling owns that case).

    Does **not** validate the expression's ngspice syntax or that it reduces
    to a scalar: neither is knowable without running the engine, and both
    surface at harvest time as the measurement's own ``status: "error"``
    (with the ``expr``-specific hint :func:`_run_corner` attaches). Same
    boundary ``spice`` already has -- hand-written SPICE is passed through,
    not parsed.

    Split into helpers so no one function carries the whole rule set's
    branch count (see ``scripts/check_complexity_baseline.py``).
    """
    for spec in measurements_spec:
        _validate_measurement_form(spec)
    _validate_expr_names_unique(measurements_spec)
    validate_case_variant_names(measurements_spec)


def _validate_optional_flag(name: Any, spec: dict[str, Any]) -> None:
    """Issue #2925: ``optional``, when present, must be a real boolean."""
    if "optional" in spec and not isinstance(spec["optional"], bool):
        raise SimError(
            f"measurement '{name}' has a non-boolean 'optional' "
            f"({spec['optional']!r}): it must be true or false"
        )


def _validate_crossings_form(
    name: Any, spec: dict[str, Any], scalar_form: bool
) -> None:
    """Issue #2868: ``crossings`` is a third, mutually exclusive measurement
    form (``scalar_form`` = the entry also declares ``spice``/``expr``), and
    issue #2925: it cannot be ``optional`` -- a signal with no crossings
    reports a count of 0; a missing series is a read failure, not absent data."""
    if scalar_form:
        raise SimError(
            f"measurement '{name}' declares 'crossings' together with "
            f"'{'spice' if spec.get('spice') is not None else 'expr'}': they are "
            "alternative forms of a measurement -- declare exactly one (use a "
            "separate measurements[] entry for the scalar)"
        )
    if spec.get("optional") is True:
        raise SimError(
            f"measurement '{name}': 'optional' is not supported on "
            "'crossings' (a signal with no crossings reports a count of "
            "0; a missing series is a read failure, not absent data)"
        )
    sim_crossings.validate_crossings_spec(name, spec["crossings"])


def _validate_rawfile_form(
    name: Any, spec: dict[str, Any], present: dict[str, bool]
) -> bool:
    """Issues #2947, #3033, #4592: ``cycle_mean``, ``energy`` and ``stress``
    are mutually exclusive with every other form. Validates the one declared
    (``present`` maps each form name to whether ``spec`` declares it) and
    returns True, or returns False when none is declared."""
    for form in ("cycle_mean", "energy", "stress"):
        if not present[form]:
            continue
        others = [other for other, on in present.items() if on and other != form]
        if others:
            raise SimError(
                f"measurement '{name}' declares '{form}' together with "
                f"'{others[0]}': they are alternative forms of a measurement "
                "-- declare exactly one (use a separate measurements[] entry "
                "for the other)"
            )
        if form == "cycle_mean":
            sim_cycle_mean.validate_cycle_mean_spec(name, spec["cycle_mean"])
        elif form == "energy":
            sim_energy.validate_energy_spec(name, spec["energy"])
        else:
            sim_stress.validate_stress_spec(name, spec["stress"])
        return True
    return False


def _require_measurement_form(name: Any, has_form: bool) -> None:
    """A ``measurements[]`` entry needs a ``name`` and one declared form."""
    if name is None or not has_form:
        raise SimError(
            "each request.measurements[] entry requires 'name' and "
            "exactly one of 'spice' (a verbatim .meas card), 'expr' "
            "(an ngspice expression evaluated after the analysis), "
            "'crossings' (a threshold-crossing time series), "
            "'cycle_mean' (a mean over whole reference cycles), "
            "'energy' (a source's energy over a transient window), "
            "'stress' (a node pair's worst |V| over a transient window), "
            "'noise' (a spot/band-integrated noise-spectrum value) or "
            "'transfer' (an AC return-ratio metric), "
            "'tian' (a dual-injection AC stability metric) or "
            "'snm' (a butterfly static noise margin of a DC sweep) or "
            "'search' (a bisection boundary over a source value)"
        )


_EXCLUSIVE_FORM_VALIDATORS = (
    sim_settled.validate_form_exclusive,
    sim_step_resolution.validate_form_exclusive,
    sim_eye.validate_form_exclusive,
    sim_search.validate_form_exclusive,
    sim_snm_meas.validate_form_exclusive,
    sim_tian.validate_form_exclusive,
    sim_transfer_meas.validate_form_exclusive,
)


def _validate_measurement_form(spec: dict[str, Any]) -> None:
    """One ``request.measurements[]`` entry's form: a ``name`` plus exactly
    one of ``spice``/``expr``, and -- for an ``expr`` -- a name that survives
    the round trip through the deck and back out of the log. See
    :func:`_validate_measurement_forms` for why each rule exists."""
    for validator in _EXCLUSIVE_FORM_VALIDATORS:
        if validator(spec):
            return
    name = spec.get("name")
    has_spice = spec.get("spice") is not None
    has_expr = spec.get("expr") is not None
    has_crossings = sim_crossings.is_crossings(spec)
    _validate_optional_flag(name, spec)
    has_cycle_mean = sim_cycle_mean.is_cycle_mean(spec)
    has_energy = sim_energy.is_energy(spec)
    has_stress = sim_stress.is_stress(spec)
    if sim_noise.is_noise(spec):
        has_other = (
            has_spice
            or has_expr
            or has_crossings
            or has_cycle_mean
            or has_energy
            or has_stress
        )
        _validate_noise_measurement(name, spec, has_other)
        return
    _require_measurement_form(
        name,
        has_spice
        or has_expr
        or has_crossings
        or has_cycle_mean
        or has_energy
        or has_stress,
    )
    _validate_on_unreached(name, spec, has_spice=has_spice)
    if _validate_rawfile_form(
        name,
        spec,
        {
            "spice": has_spice,
            "expr": has_expr,
            "crossings": has_crossings,
            "cycle_mean": has_cycle_mean,
            "energy": has_energy,
            "stress": has_stress,
        },
    ):
        return
    if has_crossings:
        _validate_crossings_form(name, spec, has_spice or has_expr)
        return
    if has_spice and has_expr:
        raise SimError(
            f"measurement '{name}' declares both 'spice' and 'expr': "
            "they are alternative forms of the same measurement (a "
            "top-level .meas card vs. an expression evaluated inside "
            "`klt sim`'s own .control block) -- declare exactly one"
        )
    if not has_expr:
        return
    _validate_expr_body(name, spec["expr"])
    if not _EXPR_MEAS_NAME_RE.match(str(name)):
        raise SimError(
            f"measurement '{name}' cannot be declared with 'expr': its "
            "name is emitted as an ngspice vector name (`let <name> = "
            "...`) and read back from the log, so it must match "
            "[A-Za-z_][A-Za-z0-9_]* (no dots, no leading digit)"
        )


def _validate_on_unreached(name: Any, spec: dict[str, Any], *, has_spice: bool) -> None:
    """``measurements[].on_unreached`` (issue #3108): ``"error"`` | ``"null"``,
    and only on a ``spice`` (``.meas``) measurement -- an ``expr`` or
    ``crossings`` entry has no "target" to leave unreached, so it is rejected
    rather than silently ignored."""
    if "on_unreached" not in spec:
        return
    value = spec["on_unreached"]
    if value not in ON_UNREACHED_VALUES:
        raise SimError(
            f"measurement '{name}' has on_unreached={value!r}: must be one of "
            + ", ".join(repr(v) for v in ON_UNREACHED_VALUES)
        )
    if not has_spice:
        raise SimError(
            f"measurement '{name}' declares 'on_unreached' without 'spice': "
            "it only applies to a .meas card whose WHEN/AT target may go "
            "unreached"
        )


def _validate_expr_body(name: Any, expr: Any) -> None:
    """A ``measurements[].expr`` value itself: one non-empty line of ngspice
    expression text. Nothing about the *expression* is parsed (see
    :func:`_validate_measurement_forms`) -- only that it can be spliced into
    a single ``let`` line without becoming a second control command."""
    if not isinstance(expr, str) or not expr.strip():
        raise SimError(
            f"measurement '{name}' has an empty or non-string 'expr': it "
            "must be a single-line ngspice expression, e.g. "
            '"v(d) - v(s)" or "vecmax(v(out)) - vecmin(v(out))"'
        )
    if "\n" in expr or "\r" in expr:
        raise SimError(
            f"measurement '{name}' has a multi-line 'expr': it is "
            "spliced verbatim into one `let` line of the generated "
            ".control block, so a newline would split it into a "
            "separate control command -- use a single-line expression"
        )
    if "<" in expr or ">" in expr:
        raise SimError(
            f"measurement '{name}' has a bare '<' or '>' in 'expr': in "
            "ngspice's control language these are I/O redirection, not "
            "comparisons, so the measurement would produce no value (and "
            "stray files) -- spell comparisons gt / lt / ge / le / eq / ne, "
            "e.g. `(v(a) lt v(b))*v(a)`"
        )


def _validate_expr_names_unique(measurements_spec: list[dict[str, Any]]) -> None:
    """No ``expr`` measurement shares its ``name`` with any other entry.

    Checked in one pass over the whole list rather than incrementally, so an
    ``expr`` entry colliding with a ``spice`` card declared *later* is caught
    too (declaration order does not change the hazard: ``let <name>``
    overwrites whatever held that name).

    Compared **case-insensitively**, because the deck/log round trip is:
    ngspice lower-cases both a printed vector name and a ``.meas`` name in its
    own report, so ``vmid`` and ``Vmid`` are one name by the time
    :func:`_harvest_measurement_value` reads them back -- and that function's
    lower-cased fallback for an ``expr`` would otherwise be able to pick up
    another entry's value.
    """
    counts: dict[Any, int] = {}
    for spec in measurements_spec:
        key = _measurement_name_key(spec.get("name"))
        counts[key] = counts.get(key, 0) + 1
    for spec in _expr_measurements(measurements_spec):
        name = spec.get("name")
        if counts.get(_measurement_name_key(name), 0) > 1:
            raise SimError(
                f"measurement '{name}' is declared more than once and one "
                "declaration uses 'expr': a second `let " + str(name) + "` "
                "would overwrite the first, so both response entries would "
                "report the same value -- give each measurement a unique name "
                "(compared case-insensitively: ngspice lower-cases the name "
                "it prints back)"
            )


def _measurement_name_key(name: Any) -> Any:
    """A ``measurements[].name`` reduced to the identity ngspice actually
    round-trips: the lower-cased string. A non-string name is returned
    unchanged so this stays total (``_validate_measurement_form`` is what
    rejects one)."""
    return name.lower() if isinstance(name, str) else name


def _expr_measurements(
    measurements_spec: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The ``measurements[]`` entries declared as an ``expr`` (issue #2533),
    in the caller's own declaration order -- which is also the order their
    ``let`` lines are emitted in, so a later expression may reference an
    earlier one's name (verified against ngspice 46: ``let b = a * 2`` after
    ``let a = ...`` resolves)."""
    return [
        spec
        for spec in measurements_spec
        if spec.get("expr") is not None or sim_noise.is_noise(spec)
    ]


def _validate_fail_on_diagnostic(value: Any) -> tuple[str, ...]:
    """Validate and normalise ``options.fail_on_diagnostic`` (issue #2492).

    Returns the requested codes as a de-duplicated tuple in the caller's own
    order; ``None``/absent (the house convention for an undeclared optional
    field) and ``[]`` both normalise to ``()`` -- "no code is
    disqualifying", today's behaviour exactly.

    **An unrecognised code is an application error** (``SimError``, exit
    ``1``), not a silent no-op. The whole point of this option is to make a
    caller's distrust of a *recovered* convergence diagnostic mechanically
    binding; a typo (``"singular-matrix"``, ``"singular_matrx"``) that
    quietly matched nothing would hand that caller a report full of
    confident ``pass`` corners while they believed they had opted into
    stricter grading -- the exact silent-miscount failure mode this option
    exists to remove. Listing a *valid* code that simply never occurs in
    this run is fine and stays a no-op: that is a legitimate standing
    policy, not a mistake.
    """
    from .sim import DIAGNOSTIC_CODES  # defined in sim.py, which imports this module

    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise SimError(
            "options.fail_on_diagnostic must be an array of diagnostic code "
            'strings (e.g. ["singular_matrix", "nonconvergence"])'
        )
    codes: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise SimError(
                "options.fail_on_diagnostic entries must be non-empty "
                "diagnostic code strings"
            )
        code = entry.strip()
        if code not in DIAGNOSTIC_CODES:
            raise SimError(
                f"options.fail_on_diagnostic names an unknown diagnostic code "
                f"{code!r} (known codes: {', '.join(sorted(DIAGNOSTIC_CODES))})"
            )
        if code not in codes:
            codes.append(code)
    return tuple(codes)
