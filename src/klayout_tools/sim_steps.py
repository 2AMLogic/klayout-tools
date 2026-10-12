"""Ordered analysis steps inside one corner's deck (``analysis_steps[]``,
issue #2482).

A ``klt sim`` request historically declared exactly one ``analysis`` per
request, so a figure of merit defined *across* several solves of the same
corner -- a line-regulation or load-regulation figure, a sensitivity
``d(out)/d(in)`` between two source values, a ratio of two DC solves -- had no
request shape at all. Callers hand-rolled their own ``.control`` loops, which
also locked those grids out of every ``klt sim`` backend (``remote``/``batch``
included), because a deck that is not expressible as a request cannot be
shipped as one.

``analysis_steps[]`` is the additive, ngspice-only answer. The scalar
``analysis`` form is untouched; the two are mutually exclusive. Each step is

- a stable unique ``name`` (an ngspice-vector-safe identifier),
- an ``analysis`` (``kind`` + ``args``, exactly like the scalar form),
- optional ``alter`` source-value overrides applied before that solve, and
- optional step-scoped ``measurements[]`` (``expr`` or a ``.meas`` card whose
  analysis type matches the step's own ``kind``).

The request's top-level ``measurements[]`` become **derived** measurements:
``expr`` entries evaluated after every step, combining step results through
step-qualified references (``<step>.<measurement>``). Both kinds are graded
through the existing ``limits``/``plausible_range``/Monte Carlo machinery and
reported in the existing measurement vocabulary; step-scoped results carry a
``step`` provenance field and any result computed from other results carries
``derived_from``.

Everything is resolved to deck text before any corner is dispatched, so the
off-host backends need nothing new: the worker re-reads ``analysis_steps``
from the forwarded request document and generates the same deck.

How the generated ``.control`` block keeps results correctly paired (the
failure mode that motivated doing this inside ``klt`` rather than leaving it
to hand-rolled decks -- a plausible but mis-paired number):

1. Every step opens a fresh, empty plot (``setplot new``) before its
   analysis. ngspice adds a *new* plot per analysis, so a solve that fails to
   produce one leaves the empty sentinel current -- the step's own
   measurements then find no vectors and come back empty, instead of
   silently reading the *previous* step's solve.
2. ``echo`` markers around the analysis record the plot name before and
   after; :func:`step_outcomes` reads them back from the log, so a step that
   produced no plot is reported as failed (``code: "step_failed"``) rather
   than inferred from its measurements.
3. The step's plot name is captured into ``klt_plot_<index>``; a
   step-qualified reference ``lo.vout`` is rewritten to
   ``{$klt_plot_<index>}.vout`` -- ngspice's own plot-qualified vector syntax
   -- so it always addresses that step's solve, at full double precision
   (never a printed, rounded copy).
4. Each step-scoped value is copied to a unique, stable log key
   ``<step>__<measurement>`` and ``print``ed, so two steps both measuring
   ``vout`` are harvested separately.
5. Derived measurements run last, in another fresh empty plot, so a bare
   node reference cannot silently resolve against whichever step happened
   to run last.

Failure propagation is decided in Python, not inferred from ngspice output: a
measurement whose step failed, or any of whose inputs produced no value, is
reported ``status: "error"`` with a ``derived_input_unavailable`` (or the
step's ``step_failed``) diagnostic -- never graded -- while every independent
result in the same corner stays inspectable.
"""

from __future__ import annotations

import math
import re
from typing import Any

#: The internal ``analysis["kind"]`` a resolved step sequence carries. Never
#: a request value: the request declares ``analysis_steps[]`` instead, and
#: :func:`resolve_analysis_steps` folds it into this one internal object so
#: every existing ``analysis``-carrying seam (backends, checkpoint
#: fingerprint, deck writer) passes the whole ordered definition through
#: unchanged.
STEPS_KIND = "steps"

#: ngspice analyses a step may run. Restricted (unlike the scalar form,
#: which passes ``kind`` through verbatim) because every step must produce a
#: plot of its own for the pairing guarantees in this module's docstring.
STEP_ANALYSIS_KINDS = ("op", "dc", "ac", "tran", "noise", "sp", "tf", "pz", "sens")

#: Step names and step-local measurement names: both become part of ngspice
#: vector names (``<step>__<name>``) and of ``<step>.<name>`` references.
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: An ``alter`` target: a top-level independent source or a passive
#: element's default value (``V``/``I``/``R``/``C``/``L``) -- what ngspice's
#: ``alter <name>=<value>`` changes. Hierarchical (``x1.r1``) and parameter
#: (``@m1[w]``) targets are out of scope.
_ALTER_TARGET_RE = re.compile(r"^[VIRCLvircl][A-Za-z0-9_]*$")

#: A step-scoped ``.meas``/``.measure`` card: type, name, remainder.
_MEAS_CARD_RE = re.compile(r"^\s*\.meas(?:ure)?\s+(\S+)\s+(\S+)(.*)$", re.IGNORECASE)

#: One token of an expression this module cares about: a signal accessor
#: (``v(x1.out)``, ``i(vdd)``, ``vdb(out)``) whose argument may legitimately
#: contain dots and is passed through untouched; or a step-qualified
#: reference ``<step>.<measurement>``. The look-behind keeps ``1.5``,
#: ``@m.x1.m0[id]`` and an already-qualified ``{$p}.x`` from matching.
_TOKEN_RE = re.compile(
    r"(?P<signal>\b[vi][a-z]{0,2}\s*\([^()]*\))"
    r"|(?<![\w.$}@])(?P<step>[A-Za-z_][A-Za-z0-9_]*)\.(?P<meas>[A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE,
)

#: A bare identifier that is not a function call and not a plot/step
#: qualifier -- used only to find a derived measurement's references to an
#: earlier derived measurement.
_BARE_NAME_RE = re.compile(r"(?<![\w.$}@])([A-Za-z_][A-Za-z0-9_]*)\b(?!\s*[.(])")

#: ``echo`` marker emitted around each step's analysis: ``klt_step_begin 0
#: unknown1`` / ``klt_step_end 0 op1``.
_STEP_MARKER_RE = re.compile(
    r"^\s*klt_step_(begin|end)\s+(\d+)\s+(\S+)\s*$", re.MULTILINE
)


def _error(message: str) -> Exception:
    from .sim import SimError

    return SimError(message)


def is_steps_analysis(analysis: dict[str, Any]) -> bool:
    """Whether a resolved ``analysis`` object is a step sequence."""
    return analysis.get("kind") == STEPS_KIND and "steps" in analysis


def measurement_provenance(spec: dict[str, Any]) -> dict[str, Any]:
    """The additive provenance fields a measurement result/rollup entry
    carries: ``step`` for a step-scoped measurement, ``derived_from`` for one
    computed from other results. Empty for every scalar-``analysis``
    measurement, so those report entries keep their exact shape."""
    fields: dict[str, Any] = {}
    if "step" in spec:
        fields["step"] = spec["step"]
    if "derived_from" in spec:
        fields["derived_from"] = list(spec["derived_from"])
    return fields


# --------------------------------------------------------------------------- #
# Request resolution and validation
# --------------------------------------------------------------------------- #


def resolve_analysis_steps(
    request: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate ``request.analysis_steps`` (plus the top-level derived
    ``measurements[]``) and resolve them into ``(analysis, measurements_spec)``.

    ``analysis`` is the internal :data:`STEPS_KIND` object carrying the
    normalised ordered steps; ``measurements_spec`` is the flattened list --
    every step's measurements in step order, named ``<step>.<name>``, then
    the derived measurements -- each carrying its precomputed deck lines and
    log key. Every structural error (duplicate step/measurement names,
    unknown/forward references, invalid override targets, ambiguous log
    keys, an empty sequence) raises before any corner is dispatched.
    """
    if request.get("analysis") is not None:
        raise _error(
            "request declares both 'analysis' and 'analysis_steps': they are "
            "alternative forms (one analysis per corner vs. an ordered "
            "sequence of named steps) -- declare exactly one"
        )
    raw_steps = request.get("analysis_steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise _error(
            "request.analysis_steps must be a non-empty array of step objects "
            '({"name", "analysis": {"kind", "args"}, "alter"?, "measurements"?})'
        )
    steps: list[dict[str, Any]] = []
    flattened: list[dict[str, Any]] = []
    known: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_steps):
        step = _resolve_step(raw, index, known)
        step_specs = _resolve_step_measurements(raw, step, index, known)
        known[step["name"].lower()] = {
            "index": index,
            "name": step["name"],
            "measurements": {spec["local_name"].lower(): spec for spec in step_specs},
        }
        steps.append(step)
        flattened.extend(step_specs)
    flattened.extend(_resolve_derived_measurements(request, known))
    _check_log_keys_unique(flattened)
    return {"kind": STEPS_KIND, "args": "", "steps": steps}, flattened


def _resolve_step(
    raw: Any, index: int, known: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """One step's name, analysis and overrides, normalised."""
    if not isinstance(raw, dict):
        raise _error(f"analysis_steps[{index}] must be an object")
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise _error(
            f"analysis_steps[{index}].name must match [A-Za-z_][A-Za-z0-9_]* "
            f"(got {name!r}): it becomes part of ngspice vector names and of "
            "'<step>.<measurement>' references"
        )
    if name.lower() in known:
        raise _error(
            f"analysis_steps[{index}] reuses step name {name!r} (compared "
            "case-insensitively): step names must be unique"
        )
    kind, args = _resolve_step_analysis(raw.get("analysis"), name)
    return {
        "name": name,
        "kind": kind,
        "args": args,
        "alter": _resolve_alter(raw.get("alter"), name),
    }


def _resolve_step_analysis(analysis: Any, step: str) -> tuple[str, str]:
    if (
        not isinstance(analysis, dict)
        or "kind" not in analysis
        or "args" not in analysis
    ):
        raise _error(f"step {step!r}: analysis requires 'kind' and 'args'")
    kind = analysis["kind"]
    args = analysis["args"]
    if not isinstance(kind, str) or kind.lower() not in STEP_ANALYSIS_KINDS:
        raise _error(
            f"step {step!r}: analysis.kind {kind!r} is not a supported step "
            f"analysis (supported: {', '.join(STEP_ANALYSIS_KINDS)})"
        )
    if not isinstance(args, str) or "\n" in args or "\r" in args:
        raise _error(
            f"step {step!r}: analysis.args must be a single-line string (it is "
            "emitted verbatim as one control command)"
        )
    return kind.lower(), args


def _resolve_alter(alter: Any, step: str) -> list[list[Any]]:
    """``alter`` as ordered ``[target, value]`` pairs (declaration order)."""
    if alter is None:
        return []
    if not isinstance(alter, dict):
        raise _error(
            f"step {step!r}: alter must be an object mapping a source/element "
            'name to its value, e.g. {"VIN": 3.3}'
        )
    pairs: list[list[Any]] = []
    seen: set[str] = set()
    for target, value in alter.items():
        if not _ALTER_TARGET_RE.match(target):
            raise _error(
                f"step {step!r}: alter target {target!r} is not a top-level "
                "independent source or passive element name (V/I/R/C/L "
                "followed by [A-Za-z0-9_]); hierarchical and device-parameter "
                "targets are not supported"
            )
        if target.lower() in seen:
            raise _error(f"step {step!r}: alter target {target!r} is repeated")
        seen.add(target.lower())
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise _error(
                f"step {step!r}: alter value for {target!r} must be a finite "
                f"number (got {value!r})"
            )
        pairs.append([target, value])
    return pairs


def _resolve_step_measurements(
    raw: dict[str, Any],
    step: dict[str, Any],
    index: int,
    known: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """One step's measurements, flattened and named ``<step>.<name>``."""
    from .sim import _validate_measurement_form

    specs = raw.get("measurements")
    if specs is None:
        return []
    if not isinstance(specs, list) or not all(isinstance(s, dict) for s in specs):
        raise _error(f"step {step['name']!r}: measurements must be an array of objects")
    resolved: list[dict[str, Any]] = []
    local_names: set[str] = set()
    for spec in specs:
        _validate_measurement_form(spec)
        local = spec["name"]
        if not isinstance(local, str) or not _NAME_RE.match(local):
            raise _error(
                f"step {step['name']!r}: measurement name {local!r} must match "
                "[A-Za-z_][A-Za-z0-9_]* (it is referenced as "
                f"'{step['name']}.<name>' and becomes an ngspice vector name)"
            )
        if local.lower() in local_names:
            raise _error(
                f"step {step['name']!r}: measurement name {local!r} is declared "
                "more than once (compared case-insensitively)"
            )
        local_names.add(local.lower())
        resolved.append(_flatten_step_measurement(spec, step, index, known))
    return resolved


def _flatten_step_measurement(
    spec: dict[str, Any],
    step: dict[str, Any],
    index: int,
    known: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    local = spec["name"]
    log_key = f"{step['name']}__{local}".lower()
    flat = {k: v for k, v in spec.items() if k not in ("name", "spice", "expr")}
    flat.update(
        {
            "name": f"{step['name']}.{local}",
            "step": step["name"],
            "local_name": local,
            "log_key": log_key,
        }
    )
    if spec.get("spice") is not None:
        flat["meas_card"] = spec["spice"]
        first = _meas_command(spec["spice"], local, step)
        derived_from: list[str] = []
    else:
        expr = spec["expr"]
        flat["expr"] = expr
        deck_expr, derived_from = _substitute_refs(
            expr, known, owner=f"{step['name']}.{local}"
        )
        first = f"let {local} = {deck_expr}"
    flat["deck_lines"] = [first, f"let {log_key} = {local}", f"print {log_key}"]
    if derived_from:
        flat["derived_from"] = derived_from
    return flat


def _meas_command(card: str, local: str, step: dict[str, Any]) -> str:
    """A step-scoped ``.meas`` card as the equivalent ``meas`` control
    command (same syntax minus the dot), evaluated against that step's own
    plot right after its analysis.

    The flattened spec carries the card as ``meas_card``, not ``spice``, so
    ``run_sim``'s own ``.meas``-type check never sees it -- it is applied
    here instead, before the kind-match check, so a ``.meas op`` card on an
    ``op`` step is refused up front exactly like a top-level one."""
    from .sim import _validate_meas_card

    if "\n" in card or "\r" in card:
        raise _error(
            f"step {step['name']!r}: measurement {local!r} spice card must be one line"
        )
    match = _MEAS_CARD_RE.match(card)
    if match is None:
        raise _error(
            f"step {step['name']!r}: measurement {local!r} spice must be a "
            "'.meas <type> <name> ...' card"
        )
    meas_type, card_name, rest = match.groups()
    _validate_meas_card(local, card)
    if meas_type.lower() != step["kind"]:
        raise _error(
            f"step {step['name']!r}: measurement {local!r} is a '.meas "
            f"{meas_type}' card but the step runs '{step['kind']}' -- a "
            "step-scoped card measures that step's own analysis, so its type "
            "must match (use 'expr' for an op quantity)"
        )
    if card_name.lower() != local.lower():
        raise _error(
            f"step {step['name']!r}: measurement {local!r} declares a .meas "
            f"card named {card_name!r} -- the card's name must equal the "
            "measurement's name"
        )
    return f"meas {meas_type} {local}{rest}".rstrip()


def _substitute_refs(
    expr: str, known: dict[str, dict[str, Any]], *, owner: str
) -> tuple[str, list[str]]:
    """Rewrite every ``<step>.<measurement>`` reference in ``expr`` to that
    step's plot-qualified vector, returning ``(deck_expr, references)``.

    ``known`` holds only the steps declared *before* the expression's own
    position, so an unknown name and a forward/self reference are both
    caught here, distinguished by whether the step exists at all."""
    references: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        if match.group("signal") is not None:
            return match.group("signal")
        step_ref, meas_ref = match.group("step"), match.group("meas")
        target = known.get(step_ref.lower())
        if target is None:
            raise _error(
                f"measurement {owner!r} references {step_ref}.{meas_ref}, but "
                f"{step_ref!r} is not an earlier step: a reference may only "
                "name a step declared before it (unknown, forward and "
                "self references are refused)"
            )
        meas = target["measurements"].get(meas_ref.lower())
        if meas is None:
            raise _error(
                f"measurement {owner!r} references {step_ref}.{meas_ref}, but "
                f"step {target['name']!r} declares no measurement {meas_ref!r}"
            )
        if meas["name"] not in references:
            references.append(meas["name"])
        return "{$klt_plot_" + str(target["index"]) + "}." + meas["local_name"]

    return _TOKEN_RE.sub(_replace, expr), references


def _resolve_derived_measurements(
    request: dict[str, Any], known: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """The top-level ``measurements[]`` of a step request: derived ``expr``
    entries evaluated after every step, in declared order."""
    from .sim import _validate_measurement_forms

    specs = request.get("measurements") or []
    _validate_measurement_forms(specs)
    derived_names = [str(spec["name"]) for spec in specs]
    resolved: list[dict[str, Any]] = []
    for position, spec in enumerate(specs):
        name = spec["name"]
        if spec.get("spice") is not None:
            raise _error(
                f"measurement {name!r}: with analysis_steps, a top-level "
                "measurement is a derived expression ('expr'); a file-scope "
                ".meas card would run after every step's analysis -- declare "
                "it in the matching step's own measurements[] instead"
            )
        deck_expr, references = _substitute_refs(spec["expr"], known, owner=name)
        references += _derived_name_refs(
            spec["expr"], name, position, derived_names, known
        )
        if not references:
            raise _error(
                f"measurement {name!r} references no step result: with "
                "analysis_steps, a top-level measurement combines "
                "'<step>.<measurement>' results (e.g. "
                '"abs(hi.vout - lo.vout)")'
            )
        flat = {k: v for k, v in spec.items()}
        flat.update(
            {
                "log_key": str(name).lower(),
                "derived_from": references,
                "deck_lines": [f"let {name} = {deck_expr}", f"print {name}"],
            }
        )
        resolved.append(flat)
    return resolved


def _derived_name_refs(
    expr: str,
    name: str,
    position: int,
    derived_names: list[str],
    known: dict[str, dict[str, Any]],
) -> list[str]:
    """Bare references from one derived expression to *other derived*
    measurements (earlier ones only), plus a clear refusal for a bare
    step-measurement name that forgot its ``<step>.`` qualifier."""
    lowered = [n.lower() for n in derived_names]
    stripped = _TOKEN_RE.sub(" ", expr)
    references: list[str] = []
    for match in _BARE_NAME_RE.finditer(stripped):
        token = match.group(1).lower()
        if token in lowered:
            target = lowered.index(token)
            if target >= position:
                raise _error(
                    f"measurement {name!r} references derived measurement "
                    f"{derived_names[target]!r}, which is not declared before "
                    "it (forward and self references are refused)"
                )
            if derived_names[target] not in references:
                references.append(derived_names[target])
            continue
        owners = [s["name"] for s in known.values() if token in s["measurements"]]
        if owners:
            raise _error(
                f"measurement {name!r} uses the bare name {match.group(1)!r}, "
                "which is a step-scoped measurement: qualify it with its step "
                f"(e.g. {owners[0]}.{match.group(1)}) -- derived measurements "
                "are evaluated after every step, outside any step's plot"
            )
    return references


def _check_log_keys_unique(specs: list[dict[str, Any]]) -> None:
    """Every harvested log key -- ``<step>__<name>`` per step-scoped
    measurement, the name of each derived one -- must be unique, or two
    results would be read back from the same printed line."""
    seen: dict[str, str] = {}
    for spec in specs:
        key = spec["log_key"]
        if key in seen:
            raise _error(
                f"measurements {seen[key]!r} and {spec['name']!r} are "
                f"ambiguous: both are harvested from the log as {key!r} "
                "(step-scoped results print as '<step>__<name>', derived ones "
                "under their own name, compared case-insensitively) -- rename "
                "one of them"
            )
        seen[key] = spec["name"]


def refuse_unsupported_options(
    analysis: dict[str, Any], *, want_waveforms: bool, plot_requested: bool
) -> None:
    """Refuse, by name, the options a step sequence does not implement --
    never silently ignore them."""
    if not is_steps_analysis(analysis):
        return
    if want_waveforms or plot_requested:
        raise _error(
            "options.waveforms / --plot are not supported with analysis_steps: "
            "the waveform artifact captures one analysis per corner, and a "
            "step sequence runs several -- drop the option, or run the step "
            "whose waveforms you need as its own scalar-analysis request"
        )


def validate_alter_targets(
    analysis: dict[str, Any], netlist_path: str, corner_points: list[Any]
) -> None:
    """Refuse a step override that cannot do what it says, before dispatch:
    one aimed at a source the corner axis already sets
    (``corners.supply_v`` -- the step would silently replace the corner's own
    value for every later solve), or at a source declared with an explicit
    transient waveform (ngspice's ``alter`` has no effect on it -- the same
    rule as ``_validate_supply_override_targets``, issue #2706)."""
    if not is_steps_analysis(analysis):
        return
    from .sim import _top_level_waveform_sources

    supply_targets = {
        key.strip().lower() for point in corner_points for key in point.supply_v
    }
    try:
        with open(netlist_path, encoding="utf-8") as handle:
            waveforms = _top_level_waveform_sources(handle.read())
    except (OSError, UnicodeDecodeError):
        waveforms = {}
    for step in analysis["steps"]:
        for target, _value in step["alter"]:
            _check_alter_target(step["name"], target, supply_targets, waveforms)


def _check_alter_target(
    step: str, target: str, supply_targets: set[str], waveforms: dict[str, str]
) -> None:
    if target.lower() in supply_targets:
        raise _error(
            f"step {step!r}: alter target {target!r} is also a "
            "corners.supply_v axis -- the step would silently override the "
            "corner's own supply value; alter a different source, or drop it "
            "from corners.supply_v"
        )
    waveform = waveforms.get(target.lower())
    if waveform is not None:
        raise _error(
            f"step {step!r}: alter target {target!r} is declared with an "
            f"explicit {waveform.upper()} waveform, and ngspice's alter has no "
            "effect on it -- declare it as a plain DC value"
        )


# --------------------------------------------------------------------------- #
# Deck generation
# --------------------------------------------------------------------------- #


def _format_value(value: float) -> str:
    if isinstance(value, int):
        return str(value)
    return repr(float(value))


def control_lines(
    analysis: dict[str, Any], measurements_spec: list[dict[str, Any]]
) -> list[str]:
    """The step-sequence body of the generated ``.control`` block -- placed
    after the corner's own ``alter`` cards and before ``quit``. See this
    module's docstring for why each line exists."""
    lines: list[str] = []
    for index, step in enumerate(analysis["steps"]):
        for target, value in step["alter"]:
            lines.append(f"alter {target}={_format_value(value)}")
        lines.append("setplot new")
        lines.append(f"echo klt_step_begin {index} $curplot")
        lines.append(f"{step['kind']} {step['args']}".rstrip())
        lines.append(f"echo klt_step_end {index} $curplot")
        lines.append(f"set klt_plot_{index} = $curplot")
        for spec in measurements_spec:
            if spec.get("step") == step["name"]:
                lines.extend(spec["deck_lines"])
    derived = [spec for spec in measurements_spec if "step" not in spec]
    if derived:
        lines.append("setplot new")
        for spec in derived:
            lines.extend(spec["deck_lines"])
    return lines


# --------------------------------------------------------------------------- #
# Harvesting and grading
# --------------------------------------------------------------------------- #


def step_outcomes(analysis: dict[str, Any], log_text: str) -> dict[str, bool]:
    """``{step name: completed}`` from the ``echo`` markers in the log. A
    step completed iff both markers are present and the analysis made a new
    plot current; a missing end marker (killed, aborted script) or an
    unchanged plot (the analysis produced none) is a failed step."""
    begin: dict[int, str] = {}
    end: dict[int, str] = {}
    for match in _STEP_MARKER_RE.finditer(log_text):
        target = begin if match.group(1) == "begin" else end
        target[int(match.group(2))] = match.group(3)
    return {
        step["name"]: index in begin and index in end and begin[index] != end[index]
        for index, step in enumerate(analysis["steps"])
    }


def grade_step_measurements(
    measurement_values: dict[str, float],
    measurements_spec: list[dict[str, Any]],
    *,
    analysis: dict[str, Any],
    log_text: str,
    save_mode: str,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, Any]]:
    """One corner's step-sequence results: ``(measurement_results,
    diagnostics, extra_corner_fields)``.

    ``extra_corner_fields`` is the additive ``{"steps": [...]}`` per-step
    outcome list. Results are graded in flattened order (steps, then derived
    measurements), so every input a result depends on is decided first."""
    outcomes = step_outcomes(analysis, log_text)
    diagnostics: list[dict[str, str]] = []
    for step in analysis["steps"]:
        if not outcomes[step["name"]]:
            diagnostics.append(_step_failed_diagnostic(step, measurements_spec))
    results: list[dict[str, Any]] = []
    by_name: dict[str, dict[str, Any]] = {}
    for spec in measurements_spec:
        result, spec_diagnostics = _grade_one(
            spec, measurement_values, outcomes, by_name, save_mode=save_mode
        )
        by_name[spec["name"]] = result
        results.append(result)
        diagnostics.extend(spec_diagnostics)
    steps_field = [
        {
            "name": step["name"],
            "kind": step["kind"],
            "status": "ok" if outcomes[step["name"]] else "error",
        }
        for step in analysis["steps"]
    ]
    return results, diagnostics, {"steps": steps_field}


def _step_failed_diagnostic(
    step: dict[str, Any], measurements_spec: list[dict[str, Any]]
) -> dict[str, str]:
    owned = [s["name"] for s in measurements_spec if s.get("step") == step["name"]]
    suffix = (
        f"; its measurements ({', '.join(owned)}) are reported without a value"
        if owned
        else ""
    )
    return {
        "severity": "error",
        "code": "step_failed",
        "message": (
            f"analysis step {step['name']!r} ({step['kind']} {step['args']}".rstrip()
            + ") produced no analysis plot -- the solve failed or never ran "
            "(see this corner's log)" + suffix
        ),
    }


def _grade_one(
    spec: dict[str, Any],
    measurement_values: dict[str, float],
    outcomes: dict[str, bool],
    by_name: dict[str, dict[str, Any]],
    *,
    save_mode: str,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    from .sim import _grade_measurement_value

    base = {"name": spec["name"], "value": None, "unit": spec.get("unit")}
    errored = {
        **base,
        "status": "error",
        "margin": None,
        **measurement_provenance(spec),
    }
    step = spec.get("step")
    if step is not None and not outcomes[step]:
        # Reported by the step's own `step_failed` diagnostic; never read a
        # value from a step that produced no plot of its own.
        return errored, []
    missing = [
        name
        for name in spec.get("derived_from", [])
        if by_name.get(name, {}).get("value") is None
    ]
    if missing:
        return errored, [
            {
                "severity": "error",
                "code": "derived_input_unavailable",
                "message": (
                    f"measurement {spec['name']!r} was not graded: its "
                    f"input(s) {', '.join(missing)} produced no value in this "
                    "corner"
                ),
            }
        ]
    value = measurement_values.get(spec["log_key"])
    if value is None:
        return errored, [
            {
                "severity": "error",
                "code": "measurement",
                "message": _no_value_message(spec, save_mode),
            }
        ]
    status, margin, grading_diagnostics = _grade_measurement_value(spec, value)
    result = {
        **base,
        "value": value,
        "status": status,
        "margin": margin,
        **measurement_provenance(spec),
    }
    return result, grading_diagnostics


def _no_value_message(spec: dict[str, Any], save_mode: str) -> str:
    from .sim import SAVE_MODE_NETLIST_NO_VALUE_HINT

    if "step" in spec:
        form = (
            f"its .meas card ({spec['meas_card']!r})"
            if "meas_card" in spec
            else f"its 'expr' ({spec['expr']!r})"
        )
        message = (
            f"measurement {spec['name']!r} produced no value: step "
            f"{spec['step']!r} completed, but {form} printed no "
            f"'{spec['log_key']} = <value>' line -- check that it resolves "
            "against that step's saved vectors and reduces to a single real "
            "scalar"
        )
    else:
        message = (
            f"derived measurement {spec['name']!r} produced no value: every "
            f"input ({', '.join(spec['derived_from'])}) was available, but its "
            f"expression ({spec['expr']!r}) did not evaluate to a single real "
            "scalar -- check the expression itself"
        )
    if save_mode == "netlist" and "step" in spec:
        message += SAVE_MODE_NETLIST_NO_VALUE_HINT
    return message
