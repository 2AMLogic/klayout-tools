"""``options.measurement_cases``: templated measurements over a case list (issue #5731).

A replicated-deck transient grid -- N independent copies of one circuit in a
single netlist, each copy carrying its own node/source names, its own ``.ic``
cards and its own stimulus -- solves all N cases in one ngspice process per
PVT point. Everything on the deck side of such a grid is ordinary static deck
text (per-copy names make per-copy ``.ic``/``PWL`` cards literal); the gap was
on the request side: every reduction had to be hand-declared against every
copy's own node names, so the request and its report grew as N x M
declarations. This module closes that gap.

A request declares the N copy scopes once::

    {"analysis": {"kind": "tran", "args": "10n 2u"},
     "options": {"measurement_cases": ["case0_", "case1_", "case2_"]},
     "measurements": [
       {"name": "tpd_{case}",
        "spice": ".meas tran tpd_{case} TRIG v({case}in) VAL=0.5 RISE=1 "
                 "TARG v({case}out) VAL=0.5 RISE=1"}]}

:func:`expand_request` rewrites each templated entry into one ordinary
``spice``/``expr`` spec per case -- substituting every ``{case}`` occurrence
in its ``name``/``spice``/``expr`` -- and stamps each expanded copy with a
``case`` provenance field (reported alongside ``step``/``derived_from`` by
:func:`klayout_tools.sim_steps.measurement_provenance`). Every downstream
stage (form validation, the case-insensitive name-collision rule, deck
emission, log harvesting, grading, Monte Carlo) then sees ordinary per-case
specs and needs no new machinery.

Expansion runs before any corner is dispatched and before the request
document is forwarded to an off-host backend, so a ``batch``/``remote`` runner
needs no new capability: the forwarded document carries only the expanded
plain measurements (the ``options.measurement_cases`` key itself is dropped),
and ``sim_batch_contract_spec.py`` accordingly has no runner-feature entry for
this option. A request that declares neither the option nor any placeholder
is returned **unchanged** -- decks, corner IDs, resume fingerprints and
reports stay byte-identical.

Split out of ``sim.py`` for the reason ``sim_waveform_opts.py`` gives: keep
the size ratchet on ``sim.py`` from growing.
"""

from __future__ import annotations

import re
from typing import Any

from .sim_errors import SimError

#: The option's request field: ``options.measurement_cases``.
OPTION = "measurement_cases"

#: The substituted token. Every occurrence in a templated entry's
#: substitutable fields is replaced with the case's scope string.
PLACEHOLDER = "{case}"

#: Spec fields the placeholder may appear in. Any other field (``unit``,
#: ``limits``, ``plausible_range``, ``crossings``, ...) carrying it is refused:
#: substituting there has no defined meaning (a ``unit`` cannot vary per case
#: without a second ``unit`` vocabulary; a form's sub-object is a selector, not
#: deck text).
SUBSTITUTABLE_FIELDS = ("name", "spice", "expr")

#: Error-envelope ``error.code`` values (issue #2721) for the refusals, so a
#: caller can distinguish the malformed combinations mechanically.
CASES_INVALID_CODE = "measurement_cases_invalid"
TEMPLATE_WITHOUT_CASES_CODE = "measurement_template_without_cases"
CASES_WITHOUT_TEMPLATE_CODE = "measurement_cases_without_template"
FIELD_UNSUPPORTED_CODE = "measurement_template_field_unsupported"
STEPS_COMBINATION_CODE = "measurement_template_with_analysis_steps"

#: A case scope must be valid simultaneously as (part of) an ngspice vector
#: name -- expanded ``expr`` names are read back out of the log -- and as
#: part of a node/source name spliced into ``spice`` card text.
_CASE_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _contains_placeholder(value: Any) -> bool:
    """Whether ``value`` (recursively into dicts/lists) contains the token."""
    if isinstance(value, str):
        return PLACEHOLDER in value
    if isinstance(value, dict):
        return any(_contains_placeholder(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_placeholder(item) for item in value)
    return False


def placeholder_fields(spec: dict[str, Any]) -> list[str]:
    """The spec's top-level fields carrying the ``{case}`` placeholder, in
    declared order. Empty for an untemplated entry."""
    return [key for key, value in spec.items() if _contains_placeholder(value)]


def expand_request(request: dict[str, Any]) -> dict[str, Any]:
    """Validate ``options.measurement_cases`` plus the templated
    ``measurements[]`` entries and rewrite ``request["measurements"]`` to the
    expanded per-case specs.

    Returns ``request`` **itself** -- unchanged, not a copy -- when the
    request declares neither the option nor any placeholder, so every existing
    deck, corner ID, resume fingerprint and report stays byte-identical. Every
    malformed combination raises :class:`~klayout_tools.sim_errors.SimError`
    before anything runs, naming the offending field and carrying one of the
    :data:`*_CODE` values.
    """
    options = request.get("options")
    cases_raw = options.get(OPTION) if isinstance(options, dict) else None
    templated = _templated_entries(request.get("measurements"))
    if cases_raw is None:
        if templated:
            raise _placeholder_without_cases(*templated[0])
        return request
    cases = _validate_cases(cases_raw)
    if not templated:
        raise SimError(
            f"options.{OPTION} declares {len(cases)} case(s) but no "
            "measurements[] entry contains the "
            f"{PLACEHOLDER} placeholder: template at least one entry (write "
            f"{PLACEHOLDER} where the copy's own node/source names go, e.g. "
            '"name": "tpd_{case}") or drop the option',
            code=CASES_WITHOUT_TEMPLATE_CODE,
        )
    if request.get("analysis_steps") is not None:
        raise SimError(
            f"options.{OPTION} is not supported with analysis_steps: steps "
            "are an ordered sequence of solves within one corner's deck, not "
            "independent cases -- declare a scalar analysis for the "
            "replicated-deck grid, or split the cases into separate requests",
            code=STEPS_COMBINATION_CODE,
        )
    rewritten = dict(request)
    rewritten_options = dict(options or {})
    rewritten_options.pop(OPTION, None)
    rewritten["options"] = rewritten_options
    rewritten["measurements"] = _expand_all(request.get("measurements"), cases)
    return rewritten


def _templated_entries(measurements: Any) -> list[tuple[dict[str, Any], list[str]]]:
    """``(spec, placeholder fields)`` per templated entry, in declared order."""
    return [
        (spec, fields)
        for spec in measurements or []
        if isinstance(spec, dict) and (fields := placeholder_fields(spec))
    ]


def _expand_all(measurements: Any, cases: list[str]) -> list[Any]:
    """Every entry expanded (templated ones into per-case copies), in the
    request's declared order. Non-dict entries pass through untouched -- the
    existing form validators own their shape refusal."""
    expanded: list[Any] = []
    for spec in measurements or []:
        expanded.extend(_expand_one(spec, cases))
    return expanded


def _expand_one(spec: Any, cases: list[str]) -> list[Any]:
    """One spec's expansion: itself when untemplated, otherwise one
    substituted copy per case, each stamped with its ``case`` provenance
    field."""
    if not isinstance(spec, dict):
        return [spec]
    fields = placeholder_fields(spec)
    if not fields:
        return [spec]
    unsupported = [
        f
        for f in fields
        if f not in SUBSTITUTABLE_FIELDS or not isinstance(spec[f], str)
    ]
    if unsupported:
        raise SimError(
            f"measurement '{spec.get('name')}' has the {PLACEHOLDER} "
            f"placeholder in measurements[].{unsupported[0]}: only "
            "'name', 'spice' and 'expr' support substitution -- move the "
            "per-case scope into the card text or the name",
            code=FIELD_UNSUPPORTED_CODE,
        )
    return [
        {**spec, "case": case}
        | {field: spec[field].replace(PLACEHOLDER, case) for field in fields}
        for case in cases
    ]


def _validate_cases(cases_raw: Any) -> list[str]:
    """The validated case scopes, in declared order, duplicates refused."""
    if not isinstance(cases_raw, list) or not cases_raw:
        raise SimError(
            f"options.{OPTION} must be a non-empty array of case scope "
            f'strings, e.g. ["case0_", "case1_"] (the {PLACEHOLDER} '
            "placeholder is replaced with each in turn)",
            code=CASES_INVALID_CODE,
        )
    cases: list[str] = []
    for entry in cases_raw:
        if not isinstance(entry, str) or not _CASE_RE.match(entry):
            raise SimError(
                f"options.{OPTION} entries must be strings matching "
                f"[A-Za-z0-9_]+ (got {entry!r}): each case is substituted "
                "into measurement names and into SPICE card text, so it has "
                "to be valid in both",
                code=CASES_INVALID_CODE,
            )
        if entry in cases:
            raise SimError(
                f"options.{OPTION} repeats {entry!r}: cases must be unique "
                "(two identical cases would emit identical measurement cards "
                "and identical results)",
                code=CASES_INVALID_CODE,
            )
        cases.append(entry)
    return cases


def _placeholder_without_cases(spec: dict[str, Any], fields: list[str]) -> SimError:
    return SimError(
        f"measurement '{spec.get('name')}' uses the {PLACEHOLDER} placeholder "
        f"in measurements[].{fields[0]} but options.{OPTION} is not "
        "declared: declare the case list to expand the entry into one "
        "measurement per case, or write the copy's own names literally",
        code=TEMPLATE_WITHOUT_CASES_CODE,
    )
