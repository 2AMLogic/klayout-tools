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

from . import _spice_number as spice_number
from ._spice_text import strip_spice_inline_comment

#: Waveform keywords this module can rewrite.
SCALABLE_KINDS = ("pwl", "pulse")

_WAVE_RE = re.compile(r"\b(pwl|pulse)\s*\(([^()]*)\)", re.IGNORECASE)
_ANY_WAVE_RE = re.compile(
    r"\b(pwl|pulse|sin|exp|sffm|am|trnoise|trrandom|external)\b", re.IGNORECASE
)


def _cards(text: str) -> list[tuple[int, int, str]]:
    """Top-level (outside ``.subckt``) logical cards of ``text`` as
    ``(first_line, last_line, joined_text)`` -- ``+`` continuations joined,
    ``;``/``$`` tail comments dropped by the shared
    :func:`strip_spice_inline_comment`. Line indices are 0-based and
    inclusive."""
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
        joined = strip_spice_inline_comment(joined)
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
        values = [spice_number.parse(token) for token in args]
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


def parse_supply_waveforms(
    corners_spec: dict[str, Any],
    supply_spec: dict[str, list[float]],
    error_cls: type[Exception],
    *,
    units_mode: bool = False,
) -> tuple[str, ...]:
    """Validate ``corners.supply_waveforms`` (issue #2902): an array of
    ``corners.supply_v`` keys whose PWL/PULSE source is scaled per corner.

    With ``units_mode`` (``corners.units``, issue #4981) only the shape is
    checked here; each unit's ``supply_v`` is checked by
    :func:`require_unit_keys`."""
    raw = corners_spec.get("supply_waveforms")
    if raw is None:
        return ()
    if (
        not isinstance(raw, list)
        or not all(isinstance(n, str) and n.strip() for n in raw)
        or len(set(raw)) != len(raw)
    ):
        raise error_cls(
            "corners.supply_waveforms must be an array of unique "
            "corners.supply_v source names"
        )
    stray = [n for n in raw if n not in supply_spec]
    if stray and not units_mode:
        raise error_cls(
            f"corners.supply_waveforms names {stray!r}, which "
            "corners.supply_v does not sweep: every entry must also be a "
            "corners.supply_v key"
        )
    return tuple(raw)


def require_unit_keys(
    names: tuple[str, ...],
    unit_supply_v: dict[str, Any],
    index: int,
    error_cls: type[Exception],
) -> None:
    """``corners.units`` mode: every ``supply_waveforms`` entry must be a
    ``supply_v`` key of unit ``index``."""
    missing = [n for n in names if n not in unit_supply_v]
    if missing:
        raise error_cls(
            f"corners.supply_waveforms names {missing!r}, which "
            f"corners.units[{index}].supply_v does not set: with "
            "corners.units every entry must be a supply_v key of every unit"
        )
