"""Per-instance mismatch gating for ``klt sim`` Monte Carlo (issue #2995).

``request.monte_carlo.instances`` selects which top-level subcircuit
instances keep the PDK's local-mismatch switch on. Every other ``X``
instance is wrapped in a generated one-instance subcircuit that shadows the
switch with ``.param <switch>=0`` locally -- the same hand-written wrapper
users otherwise maintain per project -- but built here so that:

- the instance line (nodes, model, **every** parameter including ``m``) is
  moved verbatim *inside* the wrapper, so nothing is forwarded across the
  extra hierarchy level and nothing can be dropped there; and
- the report states which instances were randomised and which were not.

Scope (first increment, refused up front otherwise): top-level ``X``
instances of the request's netlist file itself, on a PDK with a known switch
(:data:`MISMATCH_SWITCH`), local backends only.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any

#: ``pdk_variant_family`` -> the model-level ``.param`` that turns per-instance
#: mismatch on (docs/cli/sim.md, "Known-good mismatch mechanisms"). sky130
#: sets it through its ``*_mm`` corner sections; gf180mcu takes it as a plain
#: parameter.
MISMATCH_SWITCH = {"sky130": "mc_mm_switch", "gf180mcu": "sw_stat_mismatch"}

_END_RE = re.compile(r"^\s*\.end\s*$", re.IGNORECASE)
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_]")


class GateError(ValueError):
    """Invalid or unsupported ``monte_carlo.instances`` request."""


def validate_instances_spec(spec: Any) -> tuple[list[str], list[str]]:
    """Validate ``monte_carlo.instances`` and return ``(include, exclude)``
    glob lists (lower-cased: SPICE instance names are case-insensitive)."""
    if not isinstance(spec, dict):
        raise GateError("request.monte_carlo.instances must be an object")
    unknown = sorted(set(spec) - {"include", "exclude"})
    if unknown:
        raise GateError(
            "request.monte_carlo.instances has unknown key(s) "
            f"{', '.join(unknown)} (supported: include, exclude)"
        )
    lists: dict[str, list[str]] = {}
    for key in ("include", "exclude"):
        value = spec.get(key, [])
        if not isinstance(value, list) or not all(
            isinstance(g, str) and g.strip() for g in value
        ):
            raise GateError(
                f"request.monte_carlo.instances.{key} must be an array of "
                "non-empty instance-name globs"
            )
        lists[key] = [g.strip().lower() for g in value]
    if not lists["include"] and not lists["exclude"]:
        raise GateError(
            "request.monte_carlo.instances must declare a non-empty 'include' "
            "and/or 'exclude' list"
        )
    for glob in lists["include"] + lists["exclude"]:
        if "." in glob or "/" in glob:
            raise GateError(
                f"request.monte_carlo.instances glob {glob!r} names a "
                "hierarchical path; only top-level instances of the request "
                "netlist are supported"
            )
    return lists["include"], lists["exclude"]


def _split_tokens(line: str) -> list[str]:
    """Whitespace split that keeps ``{...}`` and quoted expressions whole."""
    tokens: list[str] = []
    cur: list[str] = []
    depth = 0
    quote = ""
    for ch in line:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
            cur.append(ch)
        elif ch == "{":
            depth += 1
            cur.append(ch)
        elif ch == "}":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif ch.isspace() and depth == 0:
            if cur:
                tokens.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        tokens.append("".join(cur))
    return tokens


def _logical_lines(text: str) -> list[tuple[int, int, str]]:
    """``(first, last, joined)`` line index ranges, folding ``+`` continuations."""
    raw = text.split("\n")
    out: list[tuple[int, int, str]] = []
    i = 0
    while i < len(raw):
        j = i
        joined = raw[i]
        while j + 1 < len(raw) and raw[j + 1].lstrip().startswith("+"):
            j += 1
            joined += " " + raw[j].lstrip()[1:]
        out.append((i, j, joined))
        i = j + 1
    return out


def _scan_top_level(
    text: str,
) -> tuple[list[tuple[int, int, str, list[str]]], list[str]]:
    """Top-level elements of ``text`` outside ``.subckt``/``.control`` blocks:
    ``(x_instances, other_element_names)``, where each X instance is
    ``(first_line, last_line, name, tokens)``."""
    in_subckt = 0
    in_control = False
    x_instances: list[tuple[int, int, str, list[str]]] = []
    other_names: list[str] = []
    for first, last, joined in _logical_lines(text):
        stripped = joined.strip()
        low = stripped.lower()
        if low.startswith(".control"):
            in_control = True
        elif low.startswith(".endc"):
            in_control = False
        elif low.startswith(".subckt"):
            in_subckt += 1
        elif low.startswith(".ends"):
            in_subckt = max(0, in_subckt - 1)
        if in_control or in_subckt or not stripped or stripped[0] in "*;.":
            continue
        tokens = _split_tokens(stripped)
        if stripped[0] in "xX":
            x_instances.append((first, last, tokens[0], tokens))
        else:
            other_names.append(tokens[0])
    return x_instances, other_names


def _check_globs_match(
    include: list[str],
    exclude: list[str],
    x_names: list[str],
    other_names: list[str],
) -> None:
    """Raise :class:`GateError` for a glob matching no top-level X instance."""
    names = {n.lower() for n in x_names}
    other_lower = [n.lower() for n in other_names]
    for label, globs in (("include", include), ("exclude", exclude)):
        for glob in globs:
            if any(fnmatch.fnmatchcase(n, glob) for n in names):
                continue
            if any(fnmatch.fnmatchcase(n, glob) for n in other_lower):
                raise GateError(
                    f"request.monte_carlo.instances.{label} glob {glob!r} "
                    "matches only non-subcircuit elements; per-instance "
                    "gating supports top-level X (subcircuit) instances"
                )
            raise GateError(
                f"request.monte_carlo.instances.{label} glob {glob!r} matches "
                "no top-level X instance of the request netlist"
            )


def _is_randomised(name: str, include: list[str], exclude: list[str]) -> bool:
    low = name.lower()
    if include and not any(fnmatch.fnmatchcase(low, g) for g in include):
        return False
    return not any(fnmatch.fnmatchcase(low, g) for g in exclude)


def _parse_instance(name: str, tokens: list[str]) -> tuple[list[str], str, list[str]]:
    """Split an X instance into ``(nodes, model, body)`` where ``body`` is
    every token after the instance name (nodes, model, then parameters)."""
    body = tokens[1:]
    params_at = next(
        (k for k, t in enumerate(body) if t.lower() == "params:" or "=" in t),
        len(body),
    )
    positional = body[:params_at]
    if len(positional) < 2:
        raise GateError(
            f"cannot parse instance {name!r}: expected nodes followed by "
            "a subcircuit name"
        )
    return positional[:-1], positional[-1], body


def _wrap_instance(
    name: str, nodes: list[str], body: list[str], switch: str
) -> tuple[str, list[str]]:
    """``(wrapper_name, wrapper_subckt_lines)`` moving the instance line
    (parameters included) inside a subcircuit that zeroes ``switch``."""
    wrapper = "klt_mmoff_" + _SAFE_NAME_RE.sub("_", name)
    pins = [f"p{k}" for k in range(len(nodes))]
    lines = [
        f".subckt {wrapper} {' '.join(pins)}",
        f".param {switch}=0",
        " ".join([f"X{name.lstrip('xX') or 'inner'}_klt", *pins, *body[len(nodes) :]]),
        ".ends",
    ]
    return wrapper, lines


def _apply_replacements(
    lines: list[str], replacements: dict[int, tuple[int, str]], wrappers: list[str]
) -> str:
    """Substitute the rewritten instance lines and append the wrapper
    subcircuits before the final ``.end``."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        if i in replacements:
            last, new = replacements[i]
            out.append(new)
            i = last + 1
        else:
            out.append(lines[i])
            i += 1
    if wrappers:
        end_at = next(
            (k for k in range(len(out) - 1, -1, -1) if _END_RE.match(out[k])),
            len(out),
        )
        out[end_at:end_at] = [
            "* klt sim monte_carlo.instances: mismatch-off wrappers (#2995)",
            *wrappers,
        ]
    return "\n".join(out)


def gate_netlist(
    text: str,
    include: list[str],
    exclude: list[str],
    switch: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Rewrite ``text`` so only the selected top-level ``X`` instances keep
    mismatch on. Returns ``(new_text, instance_report)``.

    Selection: an instance is randomised when it matches ``include`` (or
    ``include`` is empty) and does not match ``exclude``. Every other ``X``
    instance is wrapped. Raises :class:`GateError` when a glob matches no
    ``X`` instance, or matches a non-``X`` element.
    """
    x_instances, other_names = _scan_top_level(text)
    _check_globs_match(include, exclude, [x[2] for x in x_instances], other_names)

    report: list[dict[str, Any]] = []
    wrappers: list[str] = []
    replacements: dict[int, tuple[int, str]] = {}
    for first, last, name, tokens in x_instances:
        nodes, model, body = _parse_instance(name, tokens)
        keep = _is_randomised(name, include, exclude)
        report.append({"instance": name, "model": model, "randomised": keep})
        if keep:
            continue
        wrapper, wrapper_lines = _wrap_instance(name, nodes, body, switch)
        wrappers += wrapper_lines
        replacements[first] = (last, " ".join([name, *nodes, wrapper]))
    return _apply_replacements(text.split("\n"), replacements, wrappers), report
