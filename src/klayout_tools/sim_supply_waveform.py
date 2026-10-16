"""Per-corner level scaling of PWL/PULSE supply sources (issue #2902).

``corners.supply_v`` is applied as ngspice ``alter <src>=<value>``, which
silently has no effect on a source declared with an explicit waveform, so
``klt sim`` refuses such a request (#2706). ``corners.supply_waveforms``
opts a named source into the mechanism implemented here instead: the
source's own card is rewritten in a per-corner copy of the netlist so that
its **peak level equals the corner's supply value**, every other level moving
proportionally (a ramp ``0 -> 1.8`` swept to ``1.62`` becomes ``0 -> 1.62``;
a mid-ramp shelf at ``0.9`` becomes ``0.81``).

Deliberately lexical and fail-closed, like the rejection it relaxes: only a
top-level ``PWL(...)``/``PULSE(...)`` card whose level tokens are plain
numeric literals is rewritten. Anything else (SIN/EXP/..., ``{param}`` or
quoted expressions, ``file=`` PWL, a source declared only in an ``.include``)
raises :class:`klayout_tools.sim.SimError`-compatible errors via the
``error_cls`` the caller passes, never a silent nominal-waveform run.
"""

from __future__ import annotations

import re
from typing import Any

#: Waveform keywords this module can rewrite.
SCALABLE_KINDS = ("pwl", "pulse")

_SUFFIXES: tuple[tuple[str, float], ...] = (
    ("meg", 1e6),
    ("t", 1e12),
    ("g", 1e9),
    ("k", 1e3),
    ("m", 1e-3),
    ("u", 1e-6),
    ("n", 1e-9),
    ("p", 1e-12),
    ("f", 1e-15),
    ("a", 1e-18),
)
_NUMBER_RE = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)([a-zA-Z]*)$")
_WAVE_RE = re.compile(r"\b(pwl|pulse)\s*\(([^()]*)\)", re.IGNORECASE)
_ANY_WAVE_RE = re.compile(
    r"\b(pwl|pulse|sin|exp|sffm|am|trnoise|trrandom|external)\b", re.IGNORECASE
)


def _number(token: str) -> float | None:
    match = _NUMBER_RE.match(token)
    if match is None:
        return None
    value = float(match.group(1))
    suffix = match.group(2).lower()
    for name, scale in _SUFFIXES:
        if suffix.startswith(name):
            return value * scale
    return value


def _cards(text: str) -> list[tuple[int, int, str]]:
    """Top-level (outside ``.subckt``) logical cards of ``text`` as
    ``(first_line, last_line, joined_text)`` -- ``+`` continuations joined,
    ``;`` tail comments dropped. Line indices are 0-based and inclusive."""
    lines = text.split("\n")
    cards: list[tuple[int, int, str]] = []
    depth = 0
    index = 0
    while index < len(lines):
        raw = lines[index].strip()
        first = index
        index += 1
        if not raw or raw.startswith(("*", "+")):
            continue
        joined = raw
        while index < len(lines) and lines[index].lstrip().startswith("+"):
            joined += " " + lines[index].strip()[1:].strip()
            index += 1
        joined = re.split(r";|\s\$", joined, maxsplit=1)[0].strip()
        lowered = joined.lower()
        if lowered.startswith("."):
            word = lowered.split(None, 1)[0]
            if word == ".subckt":
                depth += 1
            elif word == ".ends":
                depth = max(0, depth - 1)
            continue
        if depth == 0:
            cards.append((first, index - 1, joined))
    return cards


def find_source(
    text: str, name: str, *, option: str, error_cls: type
) -> dict[str, Any]:
    """Locate and parse the top-level ``name`` source for rewriting.

    Returns ``{"first", "last", "card", "kind", "span", "levels", "values"}``
    where ``span`` is the ``(start, end)`` of the parenthesised argument text
    inside ``card``, ``levels`` the indices into the argument tokens that are
    voltage levels, and ``values`` those levels as floats. Raises
    ``error_cls`` naming ``option`` for anything it cannot rewrite exactly."""
    want = name.strip().lower()
    for first, last, card in _cards(text):
        tokens = card.split()
        if tokens[0].lower() != want or want[0] != "v":
            continue
        if len(tokens) < 4:
            break
        rest = card.split(None, 3)[3]
        match = _WAVE_RE.search(rest)
        if match is None:
            other = _ANY_WAVE_RE.search(rest)
            if other is not None:
                raise error_cls(
                    f"{option} source {name!r} declares a "
                    f"{other.group(1).upper()} waveform"
                    + (
                        " without a parenthesised argument list"
                        if other.group(1).lower() in SCALABLE_KINDS
                        else f"; only {'/'.join(k.upper() for k in SCALABLE_KINDS)} "
                        "levels can be scaled"
                    )
                    + ". Remove it from corners.supply_waveforms and keep the "
                    "nominal waveform, or declare the source as PWL(...)/PULSE(...)"
                )
            raise error_cls(
                f"{option} source {name!r} has no PWL/PULSE waveform to scale; "
                "a plain DC source takes corners.supply_v directly "
                "(remove it from corners.supply_waveforms)"
            )
        kind = match.group(1).lower()
        args = match.group(2).replace(",", " ").split()
        values = [_number(token) for token in args]
        if not args or any(v is None for v in values):
            raise error_cls(
                f"{option} source {name!r}: {kind.upper()} arguments must be "
                f"plain numeric literals to be scaled (found "
                f"{match.group(2).strip()!r}); "
                "parameters, expressions and options inside the argument list "
                "are not rewritten"
            )
        if kind == "pwl":
            if len(args) < 2 or len(args) % 2:
                raise error_cls(
                    f"{option} source {name!r}: PWL needs an even number of "
                    "time/value arguments"
                )
            levels = list(range(1, len(args), 2))
        else:
            if len(args) < 2:
                raise error_cls(
                    f"{option} source {name!r}: PULSE needs at least V1 and V2"
                )
            levels = [0, 1]
        offset = len(card) - len(rest) + match.start(2)
        return {
            "first": first,
            "last": last,
            "card": card,
            "kind": kind,
            "span": (offset, offset + len(match.group(2))),
            "args": args,
            "levels": levels,
            "values": [values[i] for i in levels],
        }
    raise error_cls(
        f"{option} source {name!r} was not found as a top-level independent "
        "voltage source in the netlist (a source declared in an .include'd "
        "file or inside a .subckt cannot be rewritten)"
    )


def _fmt(value: float) -> str:
    return f"{value:.12g}"


def scale_netlist(
    text: str, supply_v: dict[str, float], names: tuple[str, ...], *, error_cls: type
) -> tuple[str, dict[str, Any]]:
    """Rewrite the ``names`` sources of ``text`` so each one's peak level
    equals ``supply_v[name]``. Returns ``(new_text, disclosure)`` where
    ``disclosure`` maps each source to the rule and the nominal/effective
    levels applied (the report's ``supply_waveforms`` block)."""
    lines = text.split("\n")
    disclosure: dict[str, Any] = {}
    edits: list[tuple[int, int, str]] = []
    for name in names:
        parsed = find_source(
            text, name, option="corners.supply_waveforms", error_cls=error_cls
        )
        target = float(supply_v[name])
        # Signed: the level of largest magnitude keeps its sign, so a negative
        # rail swept to a negative value scales by a positive factor.
        peak = max(parsed["values"], key=abs)
        if peak == 0.0:
            raise error_cls(
                f"corners.supply_waveforms source {name!r}: every "
                f"{parsed['kind'].upper()} level is zero, so there is no peak "
                "to scale to the supply value"
            )
        if target != 0.0 and (target < 0.0) != (peak < 0.0):
            raise error_cls(
                f"corners.supply_waveforms source {name!r}: supply_v value "
                f"{target:g} V has the opposite sign of the {parsed['kind'].upper()} "
                f"peak level {peak:g} V; scaling would mirror the waveform. "
                "Use a supply_v value with the same polarity as the source"
            )
        args = list(parsed["args"])
        effective = []
        for index, nominal in zip(parsed["levels"], parsed["values"], strict=True):
            scaled = target * (nominal / peak)
            args[index] = _fmt(scaled)
            effective.append(scaled)
        start, end = parsed["span"]
        card = parsed["card"][:start] + " ".join(args) + parsed["card"][end:]
        edits.append((parsed["first"], parsed["last"], card))
        disclosure[name] = {
            "waveform": parsed["kind"],
            "rule": "scale-peak-to-supply",
            "supply_v": target,
            "nominal_peak_v": peak,
            "scale": target / peak,
            "nominal_levels_v": list(parsed["values"]),
            "effective_levels_v": effective,
        }
    for first, last, card in sorted(edits, reverse=True):
        lines[first : last + 1] = [card]
    return "\n".join(lines), disclosure
