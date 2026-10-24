"""``measurements[].search`` for ``klt sim`` (issue #4121).

A ``measurements[]`` entry declared with ``search`` grades the boundary of a
monotone pass/fail predicate over one independent source's value -- "the
smallest drive that still flips the cell" -- as one scalar::

    {"name": "min_flip_drive_ua",
     "search": {"source": "Ibl", "lo": 0.0, "hi": 100e-6, "tol": 1e-7,
                "max_iter": 20,
                "analysis": {"kind": "tran", "args": "10p 5n"},
                "predicate": {"expr": "v(q) > 0.9*vdd", "at": "end"}},
     "unit": "A", "limits": {"min": 1e-6}}

The bisection runs *inside the corner's one ngspice process*, as a ``while``
loop in the generated ``.control`` block (so it behaves identically on every
backend and composes with corners, ``monte_carlo``, ``limits`` and
``klt yield``, which all read the ordinary measurement result). Each probe is
``alter <source> = <x>``, the declared analysis, and the predicate evaluated at
the last point of its vector. Both bracket ends are probed first; the endpoint
outcomes must differ. The loop's state lives in the ``const`` plot (every
probe starts from ``setplot const`` so a failed analysis can never be read as
the previous probe's plot) and is printed once at the end::

    klt_s<k>_lo = ...   klt_s<k>_hi = ...   klt_s<k>_n = ...
    klt_s<k>_st = ...   klt_s<k>_x = ...

``st`` is 0 (converged) or the failure state decoded by :func:`_outcome`.
``value`` is the midpoint of the final bracket; an additive ``search`` sibling
carries ``{iterations, lo, hi, converged}``. Every failure is
``status: "error"``, ``value: null`` with a ``code: "measurement"`` diagnostic
whose message starts with a ``[<reason>]`` token from :data:`ERROR_CODES`.

ngspice expands ``$&name`` in a command with 6 significant digits, so the
value handed to ``alter`` is rounded to that; :func:`validate_search_spec`
therefore refuses a ``tol`` finer than that resolution.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from . import sim_passes, sim_twoport
from .sim_errors import SimError

#: Reason tokens a failed ``search`` entry's diagnostic message leads with.
ERROR_CODES = (
    "search_bracket_invalid",
    "search_probe_failed",
    "search_not_converged",
    "search_no_result",
)

#: Hard upper bound on ``search.max_iter`` (each iteration is one analysis).
MAX_ITER_CAP = 64

#: ``alter`` precision floor: ``tol`` must exceed this fraction of the larger
#: bracket magnitude (``$&`` text expansion keeps 6 significant digits).
_TOL_RESOLUTION = 1e-5

_KEYS = ("source", "lo", "hi", "tol", "max_iter", "analysis", "predicate")
_ALLOWED_KEYS = frozenset((*_KEYS, "direction", "report"))
_DIRECTIONS = ("rising", "falling")
_ANALYSIS_KINDS = ("tran", "dc", "op")

#: An independent source name, as ``analysis_steps[].alter`` accepts.
_SOURCE_RE = re.compile(r"^[VIvi][A-Za-z0-9_]*$")

#: The other mutually exclusive measurement forms.
_FORMS = (
    "spice",
    "expr",
    "crossings",
    "cycle_mean",
    "energy",
    "noise",
    "transfer",
    "tian",
    "snm",
)

# ngspice reads ``<`` and ``>`` in a control command as redirections; its
# comparison operators are words.
_OPERATORS = (
    (">=", " ge "),
    ("<=", " le "),
    ("==", " eq "),
    ("!=", " ne "),
    (">", " gt "),
    ("<", " lt "),
)
_OPERATOR_RE = re.compile("|".join(re.escape(op) for op, _ in _OPERATORS))
_OPERATOR_WORDS = dict(_OPERATORS)

_PRINT_RE = re.compile(
    r"^\s*klt_s(\d+)_(lo|hi|n|st|x)\s*=\s*([-+0-9.eE]+(?:inf|nan)?)\s*$",
    re.MULTILINE,
)


def is_search(spec: dict[str, Any]) -> bool:
    return spec.get("search") is not None


def search_measurements(
    measurements_spec: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [spec for spec in measurements_spec if is_search(spec)]


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


def validate_form_exclusive(spec: dict[str, Any]) -> bool:
    """For an entry declaring ``search``: refuse any other form beside it and
    validate the body. Returns ``True`` when the entry is a search entry (so
    the caller has nothing more to validate)."""
    if not is_search(spec):
        return False
    name = spec.get("name")
    if name is None:
        raise SimError("each request.measurements[] entry requires 'name'")
    others = [form for form in _FORMS if spec.get(form) is not None]
    if others:
        raise SimError(
            f"measurement '{name}' declares 'search' together with "
            f"'{others[0]}': they are alternative forms of a measurement -- "
            "declare exactly one (use a separate measurements[] entry for "
            "the other)"
        )
    validate_search_spec(name, spec["search"])
    return True


def _number(name: Any, body: dict[str, Any], key: str) -> float:
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SimError(
            f"measurement '{name}': search.{key} must be a finite number; got {value!r}"
        )
    if not math.isfinite(value):
        raise SimError(
            f"measurement '{name}': search.{key} must be a finite number; got {value!r}"
        )
    return float(value)


def _validate_bracket(name: Any, body: dict[str, Any]) -> tuple[float, float, float]:
    source = body["source"]
    if not isinstance(source, str) or not _SOURCE_RE.match(source):
        raise SimError(
            f"measurement '{name}': search.source must name a top-level "
            "independent V or I source (V/I followed by [A-Za-z0-9_]); got "
            f"{source!r}"
        )
    lo, hi, tol = (_number(name, body, key) for key in ("lo", "hi", "tol"))
    if not hi > lo:
        raise SimError(
            f"measurement '{name}': search.hi ({hi:g}) must be greater than "
            f'search.lo ({lo:g}); use search.direction "falling" when the '
            "predicate is true at lo"
        )
    if not tol > 0:
        raise SimError(f"measurement '{name}': search.tol must be > 0; got {tol:g}")
    if tol >= hi - lo:
        raise SimError(
            f"measurement '{name}': search.tol ({tol:g}) must be smaller than "
            f"the bracket width hi - lo ({hi - lo:g})"
        )
    if tol < _TOL_RESOLUTION * max(abs(lo), abs(hi)):
        raise SimError(
            f"measurement '{name}': search.tol ({tol:g}) is finer than the "
            "6-significant-digit resolution ngspice's alter receives "
            f"(need tol >= {_TOL_RESOLUTION * max(abs(lo), abs(hi)):g}); "
            "narrow the bracket or relax tol"
        )
    return lo, hi, tol


def _validate_max_iter(
    name: Any, body: dict[str, Any], width: float, tol: float
) -> None:
    max_iter = body["max_iter"]
    if isinstance(max_iter, bool) or not isinstance(max_iter, int):
        raise SimError(
            f"measurement '{name}': search.max_iter must be an integer; "
            f"got {max_iter!r}"
        )
    if not 1 <= max_iter <= MAX_ITER_CAP:
        raise SimError(
            f"measurement '{name}': search.max_iter must be in "
            f"[1, {MAX_ITER_CAP}]; got {max_iter}"
        )
    needed = math.ceil(math.log2(width / tol))
    if needed > max_iter:
        raise SimError(
            f"measurement '{name}': search.max_iter {max_iter} can never "
            f"reach tol {tol:g} from a bracket of {width:g} (needs "
            f"{needed} halvings); raise max_iter or relax tol"
        )


def _validate_options(name: Any, body: dict[str, Any]) -> None:
    if body.get("direction", "rising") not in _DIRECTIONS:
        raise SimError(
            f"measurement '{name}': search.direction must be one of "
            f"{list(_DIRECTIONS)}; got {body.get('direction')!r}"
        )
    if body.get("report", "boundary") != "boundary":
        raise SimError(
            f"measurement '{name}': search.report must be \"boundary\"; "
            f"got {body.get('report')!r}"
        )


def validate_search_spec(name: Any, body: Any) -> None:
    if not isinstance(body, dict):
        raise SimError(
            f"measurement '{name}': 'search' must be an object, e.g. "
            '{"source": "Ibl", "lo": 0, "hi": 1e-4, "tol": 1e-7, '
            '"max_iter": 20, "analysis": {"kind": "tran", "args": "10p 5n"}, '
            '"predicate": {"expr": "v(q) > 0.5"}}'
        )
    unknown = sorted(set(body) - _ALLOWED_KEYS)
    if unknown:
        raise SimError(
            f"measurement '{name}': unknown search key(s) {unknown}; "
            f"allowed: {sorted(_ALLOWED_KEYS)}"
        )
    missing = [key for key in _KEYS if body.get(key) is None]
    if missing:
        raise SimError(
            f"measurement '{name}': search requires {list(_KEYS)}; missing {missing}"
        )
    lo, hi, tol = _validate_bracket(name, body)
    _validate_max_iter(name, body, hi - lo, tol)
    _validate_options(name, body)
    _validate_analysis(name, body["analysis"])
    _validate_predicate(name, body["predicate"])


def _no_control_chars(text: str) -> bool:
    return not any(ord(ch) < 32 or ord(ch) == 127 for ch in text)


def _validate_analysis(name: Any, analysis: Any) -> None:
    if not isinstance(analysis, dict) or set(analysis) - {"kind", "args"}:
        raise SimError(
            f"measurement '{name}': search.analysis must be an object with "
            'only "kind" and "args"'
        )
    kind = analysis.get("kind")
    if not isinstance(kind, str) or kind.lower() not in _ANALYSIS_KINDS:
        raise SimError(
            f"measurement '{name}': search.analysis.kind must be one of "
            f"{list(_ANALYSIS_KINDS)}; got {kind!r}"
        )
    args = analysis.get("args", "")
    if not isinstance(args, str) or not _no_control_chars(args):
        raise SimError(
            f"measurement '{name}': search.analysis.args must be a single-line string"
        )
    if kind.lower() != "op" and not args.strip():
        raise SimError(
            f"measurement '{name}': search.analysis.args is required for kind {kind!r}"
        )
    if "{" in args or "}" in args:
        raise SimError(
            f"measurement '{name}': search.analysis.args may not use a "
            "{param} brace reference (ngspice does not expand it in a "
            ".control command)"
        )


def _validate_predicate(name: Any, predicate: Any) -> None:
    if not isinstance(predicate, dict) or set(predicate) - {"expr", "at"}:
        raise SimError(
            f"measurement '{name}': search.predicate must be an object with "
            '"expr" and optionally "at"'
        )
    expr = predicate.get("expr")
    if (
        not isinstance(expr, str)
        or not expr.strip()
        or not _no_control_chars(expr)
        or "$" in expr
        or "=" in _OPERATOR_RE.sub("", expr)
    ):
        raise SimError(
            f"measurement '{name}': search.predicate.expr must be a "
            "single-line ngspice expression that yields a truth value "
            '(comparisons such as "v(q) > 0.5" are accepted; no "$", '
            "no assignment)"
        )
    if predicate.get("at", "end") != "end":
        raise SimError(
            f"measurement '{name}': search.predicate.at must be \"end\"; "
            f"got {predicate.get('at')!r}"
        )


def validate_search_context(
    measurements_spec: list[dict[str, Any]], analysis: dict[str, Any]
) -> None:
    """Search entries ride the scalar single-analysis deck: refuse them with
    ``analysis.passes`` or a two-port analysis (which emit their own decks),
    and require unique measurement names so each response entry is
    unambiguous. (``analysis_steps[]`` is refused in ``sim_steps``.)"""
    declared = search_measurements(measurements_spec)
    if not declared:
        return
    name = declared[0]["name"]
    if sim_passes.is_passes_analysis(analysis) or sim_twoport.is_twoport(analysis):
        raise SimError(
            f"measurement '{name}' declares 'search', which is not supported "
            "with analysis.passes or a twoport analysis -- use a scalar "
            "analysis"
        )
    seen: set[str] = set()
    for spec in measurements_spec:
        key = str(spec.get("name")).lower()
        if key in seen:
            raise SimError(
                f"duplicate measurement name '{spec.get('name')}': names must "
                "be unique when measurements[] declares a search entry"
            )
        seen.add(key)


def validate_search_targets(
    measurements_spec: list[dict[str, Any]],
    waveforms: dict[str, str],
    supply_targets: set[str],
) -> None:
    """Pre-dispatch netlist check: an ``alter`` aimed at a corner supply
    source, or at one declared with an explicit waveform, has no effect (every
    probe would run the same circuit and the search would report a boundary
    of nothing)."""
    for spec in search_measurements(measurements_spec):
        source = spec["search"]["source"].lower()
        if source in supply_targets:
            raise SimError(
                f"measurement '{spec['name']}': search.source "
                f"'{spec['search']['source']}' is also a corners.supply_v "
                "axis -- the search would silently override the corner's own "
                "value; search a different source"
            )
        waveform = waveforms.get(source)
        if waveform is not None:
            raise SimError(
                f"measurement '{spec['name']}': search.source "
                f"'{spec['search']['source']}' is declared with an explicit "
                f"{waveform.upper()} waveform, and ngspice's alter has no "
                "effect on it -- declare it as a plain DC value"
            )


# --------------------------------------------------------------------------- #
# Deck emission
# --------------------------------------------------------------------------- #


def predicate_expr(expr: str) -> str:
    """``expr`` with C-style comparison operators spelled as ngspice words."""
    return _OPERATOR_RE.sub(lambda m: _OPERATOR_WORDS[m.group(0)], expr).strip()


def _probe_lines(k: int, body: dict[str, Any], x_vec: str) -> list[str]:
    """One probe at the value in vector ``x_vec``; leaves its truth value
    (``0``/``1``, or ``-1`` when the analysis or predicate failed) in the
    ``klt_s<k>_f`` vector of the ``const`` plot."""
    pre = f"klt_s{k}"
    kind = body["analysis"]["kind"].lower()
    args = body["analysis"].get("args", "").strip()
    expr = predicate_expr(body["predicate"]["expr"])
    return [
        "setplot const",
        f"alter {body['source']} = $&{x_vec}",
        f"{kind} {args}".strip(),
        f"set {pre}_fv = -1",
        f"let {pre}_p = {expr}",
        # An `op` result is a scalar, which ngspice refuses to index.
        f"let {pre}_l = ({pre}_p ne 0)"
        if kind == "op"
        else f"let {pre}_l = ({pre}_p[length({pre}_p)-1] ne 0)",
        f"set {pre}_fv = $&{pre}_l",
        "setplot const",
        f"let {pre}_f = ${pre}_fv",
    ]


def control_lines(measurements_spec: list[dict[str, Any]]) -> list[str]:
    """The bisection loop of every search entry plus one ``print`` of its
    state. Empty without a search entry (deck byte-identical to before)."""
    lines: list[str] = []
    for k, spec in enumerate(search_measurements(measurements_spec)):
        body = spec["search"]
        pre = f"klt_s{k}"
        lines.extend(
            [
                "setplot const",
                f"let {pre}_lo = {body['lo']!r}",
                f"let {pre}_hi = {body['hi']!r}",
                f"let {pre}_tol = {body['tol']!r}",
                f"let {pre}_n = 0",
                f"let {pre}_st = 0",
                f"let {pre}_f0 = -1",
                f"let {pre}_f1 = -1",
                f"let {pre}_x = {pre}_lo",
            ]
        )
        lines.extend(_probe_lines(k, body, f"{pre}_x"))
        lines.extend(
            [
                f"let {pre}_f0 = {pre}_f",
                f"if {pre}_f0 lt 0",
                f"  let {pre}_st = 1",
                "end",
                f"if {pre}_st eq 0",
                f"  let {pre}_x = {pre}_hi",
            ]
        )
        lines.extend("  " + line for line in _probe_lines(k, body, f"{pre}_x"))
        lines.extend(
            [
                f"  let {pre}_f1 = {pre}_f",
                f"  if {pre}_f1 lt 0",
                f"    let {pre}_st = 1",
                "  else",
                f"    if {pre}_f1 eq {pre}_f0",
                f"      let {pre}_st = 2",
                "    end",
                "  end",
                "end",
                f"let {pre}_run = ({pre}_st eq 0)",
                f"while {pre}_run eq 1",
                f"  let {pre}_x = ({pre}_lo + {pre}_hi) / 2",
            ]
        )
        lines.extend("  " + line for line in _probe_lines(k, body, f"{pre}_x"))
        lines.extend(
            [
                f"  if {pre}_f lt 0",
                f"    let {pre}_st = 1",
                f"    let {pre}_run = 0",
                "  else",
                f"    if {pre}_f eq {pre}_f0",
                f"      let {pre}_lo = {pre}_x",
                "    else",
                f"      let {pre}_hi = {pre}_x",
                "    end",
                f"    let {pre}_n = {pre}_n + 1",
                f"    if abs({pre}_hi - {pre}_lo) le {pre}_tol",
                f"      let {pre}_run = 0",
                "    else",
                f"      if {pre}_n ge {body['max_iter']}",
                f"        let {pre}_st = 3",
                f"        let {pre}_run = 0",
                "      end",
                "    end",
                "  end",
                "end",
                "set numdgt=15",
                f"print {pre}_lo {pre}_hi {pre}_n {pre}_st {pre}_x",
                "unset numdgt",
            ]
        )
    return lines


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


@dataclass
class SearchOutcome:
    """One search measurement's result, or why there is none."""

    value: float | None = None
    error: str | None = None
    code: str | None = None
    iterations: int | None = None
    lo: float | None = None
    hi: float | None = None

    def report(self, spec: dict[str, Any]) -> dict[str, Any]:
        body = spec["search"]
        return {
            "source": body["source"],
            "direction": body.get("direction", "rising"),
            "iterations": self.iterations,
            "lo": self.lo,
            "hi": self.hi,
            "converged": self.value is not None,
        }


def _parse_state(log_text: str) -> dict[int, dict[str, float]]:
    states: dict[int, dict[str, float]] = {}
    for match in _PRINT_RE.finditer(log_text):
        try:
            number = float(match.group(3))
        except ValueError:
            continue
        states.setdefault(int(match.group(1)), {})[match.group(2)] = number
    return states


def _outcome(spec: dict[str, Any], state: dict[str, float] | None) -> SearchOutcome:
    name = spec["name"]
    if state is None or not {"lo", "hi", "n", "st"} <= set(state):
        return SearchOutcome(
            code="search_no_result",
            error=(
                f"measurement '{name}': the search loop produced no result "
                "(the ngspice control block ended before it finished)"
            ),
        )
    lo, hi, n, st = state["lo"], state["hi"], int(state["n"]), int(state["st"])
    body = spec["search"]
    common: dict[str, Any] = {"iterations": n, "lo": lo, "hi": hi}
    if st == 0:
        return SearchOutcome(value=(lo + hi) / 2, **common)
    if st == 1:
        at = state.get("x")
        where = f" at {body['source']} = {at:g}" if at is not None else ""
        return SearchOutcome(
            code="search_probe_failed",
            error=(
                f"measurement '{name}': the probe analysis{where} failed or "
                "its predicate vector did not resolve -- check the node names "
                "and that options.save_mode does not omit them"
            ),
            **common,
        )
    if st == 2:
        return SearchOutcome(
            code="search_bracket_invalid",
            error=(
                f"measurement '{name}': the predicate has the same outcome at "
                f"search.lo ({body['lo']:g}) and search.hi ({body['hi']:g}); "
                "the bracket must straddle the boundary"
            ),
            **common,
        )
    return SearchOutcome(
        code="search_not_converged",
        error=(
            f"measurement '{name}': bracket {hi - lo:g} wide after "
            f"{n} iterations (max_iter {body['max_iter']}) is not within "
            f"tol {body['tol']:g}"
        ),
        **common,
    )


def extract_corner_searches(
    measurements_spec: list[dict[str, Any]], log_text: str
) -> dict[int, SearchOutcome]:
    """``{position in measurements_spec: outcome}`` for every search entry of
    one finished corner, read from the ngspice log. Never raises."""
    declared = search_measurements(measurements_spec)
    if not declared:
        return {}
    states = _parse_state(log_text)
    outcomes: dict[int, SearchOutcome] = {}
    for position, spec in enumerate(measurements_spec):
        if is_search(spec):
            outcomes[position] = _outcome(spec, states.get(declared.index(spec)))
    return outcomes
