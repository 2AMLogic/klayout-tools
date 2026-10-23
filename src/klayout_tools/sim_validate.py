"""Pure-input validation of ``klt sim``'s measurement specs (split out of sim.py).

Everything here validates the ``measurements[]`` / ``options.fail_on_diagnostic``
request fields and raises :class:`~klayout_tools.sim_errors.SimError` on a bad
spec; nothing depends on ``run_sim`` or the corner runner. ``sim.py`` re-exports
every name (self-aliased) so ``klayout_tools.sim.<name>`` keeps working for
callers and tests, and ``sim_steps`` imports from here directly instead of via
function-local ``from .sim import ...`` (issue #3689; precedent
``sim_remote.py``, issue #1764).
"""

from __future__ import annotations

import re
from typing import Any

from . import (
    sim_crossings,
    sim_cycle_mean,
    sim_energy,
    sim_noise,
    sim_search,
    sim_snm_meas,
    sim_tian,
    sim_transfer_meas,
)
from .sim_errors import SimError

#: A `.meas` card's declared analysis type (the token right after
#: ``.meas``/``.measure``), e.g. ``tran`` in ``.meas tran vout find v(out)``.
_MEAS_CARD_TYPE_RE = re.compile(r"^\s*\.meas(?:ure)?\s+(\S+)", re.IGNORECASE)

#: A value position in a `.meas` card: an identifier right after ``=`` that is
#: not a function call (``v(x)``/``i(x)`` never match -- the negative
#: lookahead rejects a following ``(``). Issue #3038.
_MEAS_VALUE_REF_RE = re.compile(r"=\s*([A-Za-z_]\w*)\b(?!\s*\()")

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


def _validate_meas_card(name: str, spice: str) -> None:
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
            "'energy' or 'noise'"
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

    Split across three helpers (:func:`_validate_measurement_form`,
    :func:`_validate_expr_body`, :func:`_validate_expr_names_unique`) rather
    than written as one loop, so no single function carries the whole rule
    set's branch count -- see ``scripts/check_complexity_baseline.py``.
    """
    for spec in measurements_spec:
        _validate_measurement_form(spec)
    _validate_expr_names_unique(measurements_spec)


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
    """Issues #2947, #3033: ``cycle_mean`` and ``energy`` are mutually
    exclusive with every other form. Validates the one declared (``present``
    maps each form name to whether ``spec`` declares it) and returns True, or
    returns False when neither is declared."""
    for form in ("cycle_mean", "energy"):
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
        else:
            sim_energy.validate_energy_spec(name, spec["energy"])
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
            "'noise' (a spot/band-integrated noise-spectrum value) or "
            "'transfer' (an AC return-ratio metric), "
            "'tian' (a dual-injection AC stability metric) or "
            "'snm' (a butterfly static noise margin of a DC sweep) or "
            "'search' (a bisection boundary over a source value)"
        )


def _validate_measurement_form(spec: dict[str, Any]) -> None:
    """One ``request.measurements[]`` entry's form: a ``name`` plus exactly
    one of ``spice``/``expr``, and -- for an ``expr`` -- a name that survives
    the round trip through the deck and back out of the log. See
    :func:`_validate_measurement_forms` for why each rule exists."""
    if sim_search.validate_form_exclusive(spec):
        # Issue #4121: an eighth, mutually exclusive form.
        return
    if sim_snm_meas.validate_form_exclusive(spec):
        # Issue #3895: a seventh, mutually exclusive form.
        return
    if sim_tian.validate_form_exclusive(spec):
        # Issue #3160: a sixth, mutually exclusive form.
        return
    if sim_transfer_meas.validate_form_exclusive(spec):
        # Issue #2923: a fifth, mutually exclusive form.
        return
    name = spec.get("name")
    has_spice = spec.get("spice") is not None
    has_expr = spec.get("expr") is not None
    has_crossings = sim_crossings.is_crossings(spec)
    _validate_optional_flag(name, spec)
    has_cycle_mean = sim_cycle_mean.is_cycle_mean(spec)
    has_energy = sim_energy.is_energy(spec)
    if sim_noise.is_noise(spec):
        has_other = (
            has_spice or has_expr or has_crossings or has_cycle_mean or has_energy
        )
        _validate_noise_measurement(name, spec, has_other)
        return
    _require_measurement_form(
        name, has_spice or has_expr or has_crossings or has_cycle_mean or has_energy
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
