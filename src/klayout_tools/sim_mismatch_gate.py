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

Not every PDK gates mismatch with a model-level switch, so there are two
modes (issue #5487):

- **wrapper mode** (:data:`MISMATCH_SWITCH`, sky130/gf180mcu) -- as above;
- **instance-parameter mode** (:data:`MISMATCH_INSTANCE_PARAM`, ihp-sg13g2)
  -- sg13g2 has no global switch to shadow. Its ``*_mismatch.lib`` device
  subcircuits write every term as ``agauss(nom, sigma, (mm_ok != 1 ? 0 : 1))``
  with ``mm_ok=1`` defaulted *inside each subcircuit*, which shadows any
  enclosing ``.param``. Gating is therefore done by rewriting the unselected
  instance's own line to carry ``mm_ok=0``; no wrapper subcircuit is
  generated, so hierarchical probe names are unchanged.

  Because that parameter is only honoured by a subcircuit that declares it,
  the rewrite is applied **only** to an instance whose subcircuit definition
  is known to declare it (:func:`scan_declared_params` /
  :func:`scan_library_declared_params` read the definitions; klt keeps no
  device-name list). ngspice accepts ``mm_ok=0`` silently on a user
  subcircuit that declares some *other* parameter and leaves the devices
  inside it randomised, so guessing here would report ``randomised: false``
  for an instance that is still randomised; an instance whose subcircuit does
  not declare the parameter is refused instead.

Scope (first increment, refused up front otherwise): top-level ``X``
instances of the request's netlist file itself, on a PDK with a known gate
(:data:`MISMATCH_SWITCH` or :data:`MISMATCH_INSTANCE_PARAM`), local backends
only.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any

from .sim_mismatch_declarations import logical_lines, merge_equals, split_tokens

#: ``pdk_variant_family`` -> the model-level ``.param`` that turns per-instance
#: mismatch on (docs/cli/sim.md, "Known-good mismatch mechanisms"). sky130
#: sets it through its ``*_mm`` corner sections; gf180mcu takes it as a plain
#: parameter. These are the families gated by *shadowing* that ``.param``
#: inside a generated wrapper subcircuit.
MISMATCH_SWITCH = {"sky130": "mc_mm_switch", "gf180mcu": "sw_stat_mismatch"}

#: ``pdk_variant_family`` -> the *subcircuit instance parameter* that turns
#: per-instance mismatch on, for families with no global switch to shadow.
#: Deliberately a second table rather than an entry in :data:`MISMATCH_SWITCH`:
#: the two are not interchangeable (``_resolve_mismatch_scale`` reads
#: :data:`MISMATCH_SWITCH` to find a *model-level* parameter, which ``mm_ok``
#: is not).
MISMATCH_INSTANCE_PARAM = {"sg13g2": "mm_ok"}

#: Every family ``monte_carlo.instances`` can gate, either way -- what the
#: "no known mismatch switch" refusal names.
MISMATCH_GATED_FAMILIES = sorted({*MISMATCH_SWITCH, *MISMATCH_INSTANCE_PARAM})

#: Caps on the model-library walk :func:`scan_library_declared_params` does,
#: so a pathological ``.include`` tree cannot turn a refusal check into an
#: unbounded read. Mirrors the posture of the netlist closure's own limits.

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
    for first, last, joined in logical_lines(text):
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
        tokens = split_tokens(stripped)
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


def _set_instance_param(name: str, tokens: list[str], param: str) -> str:
    """The instance line with ``param=0`` on it: an existing assignment of
    ``param`` is replaced in place (never duplicated -- ngspice would take
    one of the two), otherwise the assignment is appended."""
    merged = merge_equals(tokens)
    out: list[str] = []
    replaced = False
    for token in merged:
        if "=" in token and token.split("=", 1)[0].strip().lower() == param:
            out.append(f"{param}=0")
            replaced = True
        else:
            out.append(token)
    if not replaced:
        out.append(f"{param}=0")
    return " ".join(out)


def _require_declared(
    name: str, model: str, param: str, declared: dict[str, set[str]]
) -> None:
    """Refuse an instance whose subcircuit does not declare ``param``.

    Appending ``mm_ok=0`` to such an instance is not a no-op that ngspice
    reports: a subcircuit declaring *no* parameters aborts the run, while one
    declaring some *other* parameter accepts the line silently and keeps the
    devices inside it randomised (measured, 2026-11-13, ngspice-47). Either
    way klt would have reported ``randomised: false`` for an instance it did
    not actually gate, so the request is refused instead.
    """
    params = declared.get(model.lower())
    if params is not None and param in params:
        return
    why = (
        "declares no such parameter"
        if params is not None
        else "was not found in the request netlist closure or the selected "
        "model-library sections"
    )
    raise GateError(
        f"instance {name!r} is not selected by request.monte_carlo.instances, "
        f"but its mismatch cannot be switched off: this PDK gates per-instance "
        f"mismatch with the subcircuit parameter {param!r}, and subcircuit "
        f"{model!r} {why}. Writing {param}=0 onto it would either abort the "
        "run or be silently ignored, leaving the instance randomised while "
        "klt reported otherwise. Select it (or drop it from the netlist); "
        "instances inside user subcircuits are not gated individually."
    )


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
    switch: str | None = None,
    instance_param: str | None = None,
    declared_params: dict[str, set[str]] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Rewrite ``text`` so only the selected top-level ``X`` instances keep
    mismatch on. Returns ``(new_text, instance_report)``.

    Selection: an instance is randomised when it matches ``include`` (or
    ``include`` is empty) and does not match ``exclude``. Raises
    :class:`GateError` when a glob matches no ``X`` instance, or matches a
    non-``X`` element.

    Exactly one gating mode must be named (see the module docstring):

    - ``switch`` -- wrapper mode: every unselected instance is moved inside a
      generated subcircuit that shadows the model-level ``.param`` locally.
    - ``instance_param`` -- instance-parameter mode: every unselected
      instance line is rewritten to carry ``<instance_param>=0``, and no
      wrapper is generated. ``declared_params`` maps lower-cased subcircuit
      names to their declared parameter names (see
      :func:`scan_declared_params`); an unselected instance whose subcircuit
      does not declare ``instance_param`` is refused.
    """
    if (switch is None) == (instance_param is None):
        raise GateError("gate_netlist needs exactly one of switch / instance_param")
    declared = declared_params or {}
    x_instances, other_names = _scan_top_level(text)
    _check_globs_match(include, exclude, [x[2] for x in x_instances], other_names)

    report: list[dict[str, Any]] = []
    wrappers: list[str] = []
    replacements: dict[int, tuple[int, str]] = {}
    for first, last, name, tokens in x_instances:
        if instance_param is not None:
            tokens = merge_equals(tokens)
        nodes, model, body = _parse_instance(name, tokens)
        keep = _is_randomised(name, include, exclude)
        report.append({"instance": name, "model": model, "randomised": keep})
        if keep:
            continue
        if instance_param is not None:
            _require_declared(name, model, instance_param, declared)
            replacements[first] = (
                last,
                _set_instance_param(name, tokens, instance_param),
            )
            continue
        assert switch is not None
        wrapper, wrapper_lines = _wrap_instance(name, nodes, body, switch)
        wrappers += wrapper_lines
        replacements[first] = (last, " ".join([name, *nodes, wrapper]))
    return _apply_replacements(text.split("\n"), replacements, wrappers), report
