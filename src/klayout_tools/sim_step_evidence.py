"""Step-log evidence for ``analysis_steps[]`` corners (issues #2482, #7072).

Reading ``klt``'s own step markers and ngspice's step-scoped ``meas`` output
back out of a corner log, plus the step grader's no-value message. Split out
of :mod:`klayout_tools.sim_steps` for its per-file size ratchet (the same
reason ``sim_measurement_results`` and ``sim_cause`` left ``sim.py``): the
marker regex, :func:`step_outcomes` and :func:`no_value_message` moved here
behaviour-preserving, beside the unreached-target classification issue #7072
adds -- the step-scope twin of ``sim.py``'s scalar ``on_unreached: "null"``
machinery (issue #3108).

Region rule (why evidence is scoped per step): the ``klt_step_begin`` /
``klt_step_end`` markers surround a step's *solve* only. The step's
``meas`` control commands and their ngspice output land after its end
marker and before the next step's begin marker, so both pieces of failure
evidence for a local card name are found inside that one region -- never
missed by looking between one step's markers, and never paired across two
steps that reuse the local name.
"""

from __future__ import annotations

import re
from typing import Any

from .sim_measurement_grading import SAVE_MODE_NETLIST_NO_VALUE_HINT

#: ``echo`` marker emitted around each step's analysis: ``klt_step_begin 0
#: unknown1`` / ``klt_step_end 0 op1``.
_STEP_MARKER_RE = re.compile(
    r"^\s*klt_step_(begin|end)\s+(\d+)\s+(\S+)\s*$", re.MULTILINE
)

#: ngspice's echo of a failed control-scope ``meas`` command -- the form a
#: step-scoped ``.meas`` card runs as, with no leading dot, e.g.
#: `` meas tran missing when v(x)=2 rise=1 td=4n failed!`` (verified on
#: ngspice 47, issue #7072). Group 1 is the card's local name; the scalar
#: deck's dotted twin is ``sim._MEAS_FAILED_RE``.
_CONTROL_MEAS_FAILED_RE = re.compile(
    r"^\s*meas\s+\S+\s+(\S+)\b.*\bfailed!\s*$", re.IGNORECASE
)

# ngspice evaluates WHEN before looking up FIND's operand. Capture the
# current plot at the card itself: earlier intermediate lets can add vectors,
# and later siblings can create names that were missing when the card ran.
_FIND_VECTORS_RE = re.compile(
    r"^klt_find_vectors (\w+)\n(.*?)^klt_find_vectors_end$", re.MULTILINE | re.DOTALL
)
_DISPLAY_VECTOR_RE = re.compile(
    r"^[ \t]+(\S+)[ \t]+: [^\n]+, (?:real|complex), \d+ long", re.MULTILINE
)


def _find_when_operand(card: str) -> str | None:
    """Read the signal position in a control command or its failed echo."""
    words = card.lower().split()
    if (
        len(words) > 5
        and words[3].strip("'\"") == "find"
        and words[5].strip("'\"") == "when"
    ):
        return words[4].strip("'\"")
    return None


def find_vector_lines(spec: dict[str, Any]) -> list[str]:
    """Inventory the current plot's vector names; display omits sample data."""
    if spec.get("on_unreached") != "null" or _find_when_operand(spec["spice"]) is None:
        return []
    return [
        f"echo klt_find_vectors {spec['name']}",
        "display",
        "echo klt_find_vectors_end",
    ]


def _display_name(operand: str, kind: str) -> str:
    """Map a measurement accessor to the vector name printed by display."""
    voltage = r"v(?:m|r|i|p|db)?" if kind in ("ac", "sp") else "v"
    if match := re.fullmatch(voltage + r"\(([^()]+)\)", operand):
        return match.group(1)
    if match := re.fullmatch(r"i\(([^()]+)\)", operand):
        return match.group(1) + "#branch"
    return operand


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


def step_unreached_locals(
    analysis: dict[str, Any], log_text: str
) -> dict[str, set[str]]:
    """``{step name: local .meas card names confirmed unreached}``: names
    for which that step's own log region carries *both* pieces of ngspice
    failure evidence, the ``out of interval`` reason line and the failed
    control-command echo. The step-scope twin of
    ``sim._parse_meas_unreached_names``, intersected per region so a local
    name two steps share is never confirmed from the other step's output.
    FIND ... WHEN also needs its FIND operand in the card's plot inventory."""
    from .sim import _MEAS_UNREACHED_RE

    regions = _step_regions(log_text)
    confirmed: dict[str, set[str]] = {}
    for index, step in enumerate(analysis["steps"]):
        region = regions.get(index, "")
        vectors = {
            local.lower(): set(_DISPLAY_VECTOR_RE.findall(listing.lower()))
            for local, listing in _FIND_VECTORS_RE.findall(region)
        }
        failed = set()
        for raw in region.splitlines():
            if not (match := _CONTROL_MEAS_FAILED_RE.match(raw.strip())):
                continue
            local = match.group(1).lower()
            operand = _find_when_operand(raw)
            available = vectors.get(local, set())
            if operand is None or _display_name(operand, step["kind"]) in available:
                failed.add(local)
        confirmed[step["name"]] = {
            match.group(1).lower()
            for match in _MEAS_UNREACHED_RE.finditer(region)
            if match.group(1).lower() in failed
        }
    return confirmed


def _step_regions(log_text: str) -> dict[int, str]:
    """Each step's own slice of the log: from its ``klt_step_end`` marker to
    the next step marker, or to the end of the log for the last step (the
    trailing derived-measurement lines belong to no ``meas`` card, so they
    are harmless inside the last region)."""
    regions: dict[int, str] = {}
    matches = list(_STEP_MARKER_RE.finditer(log_text))
    for position, match in enumerate(matches):
        if match.group(1) != "end":
            continue
        stop = (
            matches[position + 1].start()
            if position + 1 < len(matches)
            else len(log_text)
        )
        regions[int(match.group(2))] = log_text[match.end() : stop]
    return regions


def is_step_unreached(
    spec: dict[str, Any],
    *,
    unreached_locals: set[str],
    engine_diagnostics: list[dict[str, Any]] | None,
) -> bool:
    """True when a value-less step-scoped ``.meas`` measurement opted into
    ``on_unreached: "null"``, its own step's region confirmed its local
    card name unreached (both ngspice evidence lines), and the corner
    carries no error-severity engine diagnostic (diverged solve, timeout,
    ...) -- the step-spec twin of ``sim._is_unreached``, over the flattened
    spec's ``meas_card``/``local_name`` instead of ``spice``/``name``."""
    if spec.get("on_unreached") != "null" or spec.get("meas_card") is None:
        return False
    if str(spec["local_name"]).lower() not in unreached_locals:
        return False
    return not any(d["severity"] == "error" for d in (engine_diagnostics or []))


def not_reached_result(
    spec: dict[str, Any],
    *,
    unreached_locals: set[str],
    engine_diagnostics: list[dict[str, Any]] | None,
) -> tuple[dict[str, Any], list[dict[str, str]]] | None:
    """The ``not_reached`` result and ``measurement_not_reached`` info
    diagnostic for a value-less step-scoped ``.meas`` measurement (issue
    #7072), or ``None`` when it does not qualify -- the same result and
    diagnostic contract the scalar branch builds in
    :mod:`klayout_tools.sim_measurement_results`, plus the step
    provenance field ``grade_step_measurements`` attaches."""
    from .sim import NOT_REACHED_STATUS
    from .sim_measurement_results import NOT_REACHED_DIAGNOSTIC_CODE
    from .sim_steps import measurement_provenance

    if not is_step_unreached(
        spec, unreached_locals=unreached_locals, engine_diagnostics=engine_diagnostics
    ):
        return None
    return (
        {
            "name": spec["name"],
            "value": None,
            "unit": spec.get("unit"),
            "status": NOT_REACHED_STATUS,
            "margin": None,
            **measurement_provenance(spec),
        },
        [
            {
                "severity": "info",
                "code": NOT_REACHED_DIAGNOSTIC_CODE,
                "message": (
                    f"measurement '{spec['name']}' target not reached: step "
                    f"'{spec['step']}' completed but ngspice's .meas never "
                    f"satisfied its condition ({spec['meas_card'].strip()})"
                ),
            }
        ],
    )


def no_value_message(spec: dict[str, Any], save_mode: str) -> str:
    """The ``code: "measurement"`` message for a step-sequence result that
    printed no value (moved from ``sim_steps._no_value_message``)."""
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
