"""Design-owned (injected) per-instance mismatch for ``klt sim`` Monte Carlo
(issue #2911).

Some PDK resistor/capacitor models have no per-instance mismatch hook, so a
caller who needs matching in a Monte Carlo run has to inject it from the
netlist body. ``request.monte_carlo.injected`` makes that a first-class,
reported construct: the caller declares *which* instances vary and by how
much, klt owns the seeded draws, and the draws are recorded per sample.

Application contract. klt does not rewrite the netlist. For every declared
``(instance, parameter)`` it emits, per sample, a plain deck-level ``.param``
named :func:`param_name` carrying the drawn multiplier ``1 + delta`` (and
``<name>_delta`` carrying ``delta``); the netlist body references it, e.g.
``XR1 a b rh m='mc_inj_xr1_m'``. A declaration whose param is never
referenced anywhere in the netlist's ``.include`` closure is rejected up
front, so a family is never reported ``active`` while nothing consumes it.

Draw contract. Each draw is a SHA-256 function of the sample's
``mismatch_seed`` (itself derived from ``monte_carlo.seed``), the instance
name and the parameter -- never of ``n``, of the other axes, or of ngspice's
``agauss`` stream order -- so changing ``n`` does not shift other samples'
draws, and the values are klt-recorded rather than engine-dependent.
"""

from __future__ import annotations

import fnmatch
import hashlib
import math
import re
from typing import Any

SUPPORTED_DISTRIBUTIONS = ("gaussian", "uniform")

_ALLOWED_KEYS = {"instance", "pattern", "parameter", "relative_sigma", "distribution"}
_NAME_RE = re.compile(r"[^a-z0-9]+")
_MODULUS = 2_147_483_647


class InjectedError(ValueError):
    """Invalid ``monte_carlo.injected`` declaration (callers re-raise as
    ``SimError`` with the same message)."""


def param_name(instance: str, parameter: str) -> str:
    """The deck ``.param`` a netlist body references for this injection:
    ``mc_inj_<instance>_<parameter>``, lower-cased with any run of
    non-alphanumerics (e.g. the ``.`` of a hierarchical name) as ``_``."""
    return "mc_inj_" + _NAME_RE.sub("_", f"{instance}_{parameter}".lower()).strip("_")


def _validate_entry(index: int, entry: Any) -> dict[str, Any]:
    where = f"request.monte_carlo.injected[{index}]"
    if not isinstance(entry, dict):
        raise InjectedError(f"{where} must be an object")
    unknown = sorted(set(entry) - _ALLOWED_KEYS)
    if unknown:
        raise InjectedError(
            f"{where} has unknown key(s) {', '.join(unknown)}; allowed: "
            f"{', '.join(sorted(_ALLOWED_KEYS))}"
        )
    if ("instance" in entry) == ("pattern" in entry):
        raise InjectedError(f"{where} needs exactly one of 'instance'/'pattern'")
    selector = "instance" if "instance" in entry else "pattern"
    target = entry[selector]
    if not isinstance(target, str) or not target.strip():
        raise InjectedError(f"{where}.{selector} must be a non-empty string")
    parameter = entry.get("parameter")
    if not isinstance(parameter, str) or not parameter.strip():
        raise InjectedError(f"{where}.parameter must be a non-empty string")
    sigma = entry.get("relative_sigma")
    if (
        isinstance(sigma, bool)
        or not isinstance(sigma, (int, float))
        or not 0 < sigma < 1
    ):
        raise InjectedError(
            f"{where}.relative_sigma must be a number in (0, 1) (got {sigma!r})"
        )
    distribution = entry.get("distribution", "gaussian")
    if distribution not in SUPPORTED_DISTRIBUTIONS:
        raise InjectedError(
            f"{where}.distribution must be one of "
            f"{', '.join(SUPPORTED_DISTRIBUTIONS)} (got {distribution!r})"
        )
    return {
        "selector": selector,
        "target": target.strip(),
        "parameter": parameter.strip(),
        "relative_sigma": float(sigma),
        "distribution": distribution,
    }


def validate(mc_spec: dict[str, Any]) -> tuple[list[dict[str, Any]], float]:
    """Validate ``injected``/``injected_scale``; return ``(specs, scale)``
    (``([], 1.0)`` when ``injected`` is absent)."""
    scale = mc_spec.get("injected_scale", 1)
    if "injected_scale" in mc_spec and "injected" not in mc_spec:
        raise InjectedError(
            "request.monte_carlo.injected_scale requires request.monte_carlo.injected"
        )
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) or scale < 0:
        raise InjectedError(
            f"request.monte_carlo.injected_scale must be a non-negative number "
            f"(got {scale!r})"
        )
    raw = mc_spec.get("injected")
    if raw is None:
        return [], 1.0
    if not isinstance(raw, list) or not raw:
        raise InjectedError(
            "request.monte_carlo.injected must be a non-empty array of objects"
        )
    specs = [_validate_entry(i, e) for i, e in enumerate(raw)]
    return specs, float(scale)


def _element_cards(text: str) -> dict[str, str]:
    """``{lower-case element name: card text}`` for the element cards of one
    netlist file (comments, continuations and dot-commands skipped). Hierarchy
    is flat: an element inside a ``.subckt`` body is named as written."""
    cards: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("*", ";", "+", ".")):
            continue
        cards.setdefault(line.split()[0].lower(), line)
    return cards


def resolve(
    specs: list[dict[str, Any]], closure_texts: list[str]
) -> list[dict[str, Any]]:
    """Expand each declaration to concrete instances against the netlist
    closure and check each is consumed. Raises :class:`InjectedError` for a
    selector matching zero instances, or an injection whose
    :func:`param_name` the netlist never references."""
    cards: dict[str, str] = {}
    for text in closure_texts:
        for name, card in _element_cards(text).items():
            cards.setdefault(name, card)
    haystack = "\n".join(closure_texts).lower()
    resolved: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, spec in enumerate(specs):
        target = spec["target"].lower()
        if spec["selector"] == "instance":
            names = [target] if target in cards else []
        else:
            names = sorted(n for n in cards if fnmatch.fnmatchcase(n, target))
        if not names:
            raise InjectedError(
                f"request.monte_carlo.injected[{index}].{spec['selector']} "
                f"{spec['target']!r} matches no instance in the netlist"
            )
        for name in names:
            key = (name, spec["parameter"].lower())
            if key in seen:
                raise InjectedError(
                    f"request.monte_carlo.injected declares {name!r} "
                    f"parameter {spec['parameter']!r} more than once"
                )
            seen.add(key)
            pname = param_name(name, spec["parameter"])
            if pname not in haystack:
                raise InjectedError(
                    f"request.monte_carlo.injected[{index}] targets {name!r} but "
                    f"the netlist never references .param {pname}; klt emits it "
                    f"per sample and the body must consume it "
                    f"(e.g. {spec['parameter']}='{pname}')"
                )
            resolved.append(
                {
                    "instance": name,
                    "parameter": spec["parameter"],
                    "relative_sigma": spec["relative_sigma"],
                    "distribution": spec["distribution"],
                    "param": pname,
                    "card": cards[name],
                }
            )
    return resolved


def _uniform(mismatch_seed: int, name: str, parameter: str, which: int) -> float:
    payload = f"{mismatch_seed}:injected:{name}:{parameter}:{which}".encode()
    value = int.from_bytes(hashlib.sha256(payload).digest()[:4], "big") % _MODULUS
    return (value + 1) / (_MODULUS + 1)  # strictly inside (0, 1)


def draw(
    resolved: list[dict[str, Any]], mismatch_seed: int, scale: float
) -> list[dict[str, Any]]:
    """One sample's recorded draws: per injection the unit draw ``z`` (unit
    variance), the applied ``relative_delta = scale * sigma * z`` and the
    ``value = 1 + relative_delta`` emitted to the deck."""
    out: list[dict[str, Any]] = []
    for item in resolved:
        u1 = _uniform(mismatch_seed, item["instance"], item["parameter"], 0)
        if item["distribution"] == "gaussian":
            u2 = _uniform(mismatch_seed, item["instance"], item["parameter"], 1)
            z = math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)
        else:
            z = (2.0 * u1 - 1.0) * math.sqrt(3.0)
        delta = scale * item["relative_sigma"] * z
        out.append(
            {
                "instance": item["instance"],
                "parameter": item["parameter"],
                "param": item["param"],
                "scale": scale,
                "draw": z,
                "relative_delta": delta,
                "value": 1.0 + delta,
            }
        )
    return out


def deck_lines(draws: list[dict[str, Any]]) -> list[str]:
    """The ``.param`` cards a sample's deck carries for its draws."""
    lines = [f".param mc_inj_scale={draws[0]['scale']!r}"] if draws else []
    for item in draws:
        lines.append(f".param {item['param']}_delta={item['relative_delta']!r}")
        lines.append(f".param {item['param']}={item['value']!r}")
    return lines
