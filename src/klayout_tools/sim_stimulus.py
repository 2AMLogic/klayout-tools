"""Declarative timed stimulus for ``klt sim`` (issue #2977).

A bench that drives digital control lines with timed edges used to hand-expand
each edge list into ``PWL`` sources inside the netlist and keep a side-car
schedule in sync "by convention". A request may instead declare the schedule::

    "stimulus": {
      "signals": [
        {"source": "vclk", "node": "clk", "initial": 0.0,
         "rise_s": 1e-10, "fall_s": 1e-10, "skew_s": 2e-10,
         "events": [{"t_s": 1e-9, "level": 1.8},
                    {"t_s": 5e-9, "level": 0.0, "transition_s": 5e-11}]}
      ]
    }

``klt sim`` expands each signal into one independent voltage source
``<source> <node> <ref> PWL(...)`` appended to every corner's deck right after
the netlist ``.include``, and echoes the schedule **as applied** (skew and
transition times resolved, PWL points included) into
``environment.stimulus`` so an analyzer reads event times from the report, not
from a side-car file.

Edge semantics: an event at ``t_s`` (plus ``skew_s``) is the instant the edge
*starts*; the level is reached ``transition_s`` later (``rise_s`` for an
increasing level, ``fall_s`` for a decreasing one, ``transition_s`` on the
event overriding both). A transition of ``0`` is applied as
:data:`MIN_TRANSITION_S` -- PWL time points must be strictly increasing, so
an ideal step cannot be written -- and the echo reports the value applied.

This module is pure (no ngspice, no filesystem): it validates the declaration
and renders text. Netlist-aware checks (a source name already defined in the
netlist) take the caller's set of existing element names.
"""

from __future__ import annotations

import math
import re
from typing import Any

from . import sim_steps

#: Request key that carries the declaration, and the report key that echoes it.
REQUEST_KEY = "stimulus"

#: Smallest transition time ever written to a PWL source (1 fs). A declared
#: ``0`` (an ideal step) is applied as this.
MIN_TRANSITION_S = 1e-15

#: Machine-readable ``SimError.code`` values (issue #2977).
CODE_INVALID = "stimulus_invalid"
CODE_BAD_TIME = "stimulus_bad_time"
CODE_COLLISION = "stimulus_source_collision"
CODE_OVERLAP = "stimulus_edge_overlap"
CODE_AFTER_STOP = "stimulus_edge_after_stop"
CODE_SUPPLY_CONFLICT = "stimulus_supply_conflict"
CODE_UNSUPPORTED = "stimulus_unsupported"

_SIGNAL_KEYS = frozenset(
    {"source", "node", "ref", "initial", "rise_s", "fall_s", "skew_s", "events"}
)
_EVENT_KEYS = frozenset({"t_s", "level", "transition_s"})
_SOURCE_RE = re.compile(r"^[vV][A-Za-z0-9_.$#\-]*$")
_NODE_RE = re.compile(r"^[A-Za-z0-9_.$#\-:\[\]<>!/]+$")
_PAIRS_PER_LINE = 6


class StimulusError(Exception):
    """A malformed or inconsistent ``stimulus`` declaration. ``code`` is one
    of the ``CODE_*`` values; ``sim`` re-raises it as a coded ``SimError``."""

    def __init__(self, message: str, code: str = CODE_INVALID) -> None:
        super().__init__(message)
        self.code = code


def _number(value: Any, field: str, *, minimum: float | None = None) -> float:
    """``value`` as a finite float; a bool or non-number is refused by name."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StimulusError(f"{field} must be a number (got {value!r})")
    number = float(value)
    if not math.isfinite(number):
        raise StimulusError(f"{field} must be finite (got {value!r})")
    if minimum is not None and number < minimum:
        code = CODE_BAD_TIME if field.endswith("_s") else CODE_INVALID
        raise StimulusError(f"{field} must be >= {minimum:g} (got {number:g})", code)
    return number


def _reject_unknown(obj: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise StimulusError(
            f"{where} has unknown key(s) {', '.join(map(repr, unknown))} "
            f"(supported: {', '.join(sorted(allowed))})"
        )


def _name(obj: dict[str, Any], key: str, pattern: re.Pattern[str], where: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not pattern.match(value):
        hint = " and start with 'V'" if key == "source" else ""
        raise StimulusError(
            f"{where}.{key} must be a non-empty SPICE name{hint} (got {value!r})"
        )
    return value


def _tstop_s(analysis: dict[str, Any]) -> float | None:
    """The scalar ``tran`` analysis' stop time in seconds, when it can be read
    (``args`` is ``"<tstep> <tstop> [...]"`` with optional SI suffixes)."""
    args = analysis.get("args")
    if not isinstance(args, str):
        return None
    tokens = args.split()
    if len(tokens) < 2:
        return None
    match = re.fullmatch(
        r"([+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)(meg|mil|[tgkmunpf])?[a-z]*",
        tokens[1].lower(),
    )
    if match is None:
        return None
    scale = {
        "t": 1e12, "g": 1e9, "meg": 1e6, "k": 1e3, "mil": 25.4e-6, "m": 1e-3,
        "u": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15, None: 1.0,
    }  # fmt: skip
    return float(match.group(1)) * scale[match.group(2)]


def _resolve_signal(raw: Any, index: int, tstop: float | None) -> dict[str, Any]:
    where = f"request.stimulus.signals[{index}]"
    if not isinstance(raw, dict):
        raise StimulusError(f"{where} must be an object")
    _reject_unknown(raw, _SIGNAL_KEYS, where)
    source = _name(raw, "source", _SOURCE_RE, where)
    node = _name(raw, "node", _NODE_RE, where)
    ref = raw.get("ref", "0")
    if not isinstance(ref, str) or not _NODE_RE.match(ref):
        raise StimulusError(f"{where}.ref must be a node name (got {ref!r})")
    initial = _number(raw.get("initial", 0.0), f"{where}.initial")
    rise = _number(raw.get("rise_s", 0.0), f"{where}.rise_s", minimum=0.0)
    fall = _number(raw.get("fall_s", 0.0), f"{where}.fall_s", minimum=0.0)
    skew = _number(raw.get("skew_s", 0.0), f"{where}.skew_s")
    events = raw.get("events")
    if not isinstance(events, list) or not events:
        raise StimulusError(f"{where}.events must be a non-empty list")

    pwl: list[list[float]] = [[0.0, initial]]
    level = initial
    busy_until = 0.0  # when the previous edge finishes
    previous_t: float | None = None
    resolved: list[dict[str, Any]] = []
    for e_index, event in enumerate(events):
        ewhere = f"{where}.events[{e_index}]"
        if not isinstance(event, dict):
            raise StimulusError(f"{ewhere} must be an object")
        _reject_unknown(event, _EVENT_KEYS, ewhere)
        if "t_s" not in event or "level" not in event:
            raise StimulusError(f"{ewhere} requires 't_s' and 'level'")
        t_s = _number(event["t_s"], f"{ewhere}.t_s", minimum=0.0)
        target = _number(event["level"], f"{ewhere}.level")
        if previous_t is not None and t_s <= previous_t:
            raise StimulusError(
                f"{ewhere}.t_s={t_s:g} is not after the previous event "
                f"({previous_t:g}); events must be strictly increasing in time",
                CODE_BAD_TIME,
            )
        previous_t = t_s
        start = t_s + skew
        if start < 0:
            raise StimulusError(
                f"{ewhere}: t_s + skew_s = {start:g} is negative", CODE_BAD_TIME
            )
        edge = "rise" if target > level else "fall" if target < level else "hold"
        declared_tr = event.get("transition_s")
        if declared_tr is not None:
            transition = _number(declared_tr, f"{ewhere}.transition_s", minimum=0.0)
        else:
            transition = rise if edge == "rise" else fall
        applied = max(transition, MIN_TRANSITION_S)
        if start < busy_until:
            raise StimulusError(
                f"{ewhere}: edge starts at {start:g}s, before the previous edge "
                f"finishes at {busy_until:g}s; shorten the transition or space "
                "the events further apart",
                CODE_OVERLAP,
            )
        if tstop is not None and start >= tstop:
            raise StimulusError(
                f"{ewhere}: edge starts at {start:g}s, at or after the "
                f"transient stop time {tstop:g}s, so it can never be observed",
                CODE_AFTER_STOP,
            )
        if edge != "hold":
            if start > pwl[-1][0]:
                pwl.append([start, level])
            pwl.append([start + applied, target])
            busy_until = start + applied
        level = target
        resolved.append(
            {
                "t_s": t_s,
                "t_applied_s": start,
                "level": target,
                "edge": edge,
                "transition_s": applied if edge != "hold" else 0.0,
            }
        )
    return {
        "source": source,
        "node": node,
        "ref": ref,
        "initial": initial,
        "skew_s": skew,
        "events": resolved,
        "pwl": pwl,
    }


def resolve_stimulus(
    request: dict[str, Any], analysis: dict[str, Any]
) -> list[dict[str, Any]]:
    """The request's resolved signal list (empty when it declares none).

    Raises :class:`StimulusError` for any structural problem before a corner is
    dispatched. ``analysis`` is the resolved analysis object: a scalar
    non-``tran`` analysis is refused, and a readable ``tran`` stop time bounds
    the (skewed) edge times.
    """
    spec = request.get(REQUEST_KEY)
    if spec is None:
        return []
    if not isinstance(spec, dict):
        raise StimulusError("request.stimulus must be an object")
    _reject_unknown(spec, frozenset({"signals"}), "request.stimulus")
    signals = spec.get("signals")
    if not isinstance(signals, list) or not signals:
        raise StimulusError("request.stimulus.signals must be a non-empty list")
    kind = analysis.get("kind")
    if kind != "tran" and not sim_steps.is_steps_analysis(analysis):
        raise StimulusError(
            "request.stimulus declares timed edges, which need analysis.kind "
            f'"tran" or an analysis_steps sequence (got {kind!r})',
            CODE_UNSUPPORTED,
        )
    tstop = _tstop_s(analysis) if kind == "tran" else None
    resolved = [_resolve_signal(raw, i, tstop) for i, raw in enumerate(signals)]
    seen: set[str] = set()
    for signal in resolved:
        key = signal["source"].lower()
        if key in seen:
            raise StimulusError(
                f"request.stimulus declares source {signal['source']!r} twice"
            )
        seen.add(key)
    return resolved


def check_collisions(signals: list[dict[str, Any]], existing: set[str]) -> None:
    """Refuse a stimulus source whose name the netlist already defines
    (``existing``: lower-cased element names): a silent override of -- or a
    duplicate-instance clash with -- the netlist's own source is never wanted."""
    for signal in signals:
        if signal["source"].lower() in existing:
            raise StimulusError(
                f"stimulus source {signal['source']!r} is already defined in "
                "the netlist (or its .include closure); rename the stimulus "
                "source or remove the netlist's definition",
                CODE_COLLISION,
            )


def check_supply_conflict(signals: list[dict[str, Any]], targets: set[str]) -> None:
    """Refuse a ``corners.supply_v`` override aimed at a stimulus source:
    ``alter`` has no effect on a PWL waveform."""
    for signal in signals:
        if signal["source"].lower() in targets:
            raise StimulusError(
                f"corners.supply_v overrides {signal['source']!r}, which is a "
                "request.stimulus PWL source; `alter` cannot change a PWL "
                "waveform",
                CODE_SUPPLY_CONFLICT,
            )


def _fmt(value: float) -> str:
    return repr(float(value))


def deck_lines(signals: list[dict[str, Any]]) -> tuple[str, ...]:
    """The SPICE cards for ``signals``, one ``PWL`` source each (continuation
    lines keep every card short)."""
    lines: list[str] = []
    for signal in signals:
        pairs = [f"{_fmt(t)} {_fmt(v)}" for t, v in signal["pwl"]]
        chunks = [
            " ".join(pairs[i : i + _PAIRS_PER_LINE])
            for i in range(0, len(pairs), _PAIRS_PER_LINE)
        ]
        head = f"{signal['source']} {signal['node']} {signal['ref']} PWL("
        body = ("\n+ ").join(chunks)
        lines.append(f"{head}{body})")
    return tuple(lines)


def report_echo(signals: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``environment.stimulus`` block: the schedule as applied."""
    return {"signals": signals}
