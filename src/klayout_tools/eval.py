"""``klt eval``: a single scored gate an optimizer can loop on.

An optimization/hill-climbing loop wanting to compare two layout candidates
today has to fan out ``klt drc``/``klt lvs``/``klt sim``/``klt
layout-metrics`` as four separate subprocesses, reconcile four different
exit-code vocabularies (see ``docs/json-contract.md``'s "Adding a new
command" section and each verb's own exit-code table), and hand-roll the
scoring -- every block repo writes that glue again, slightly differently
(issue #387).

This module is pure orchestration: it never re-implements DRC/LVS/sim/
metrics logic, it calls each verb's own library entry point
(:func:`~klayout_tools.drc.run_drc`, :func:`~klayout_tools.lvs.run_lvs`,
:func:`~klayout_tools.sim.run_sim`,
:func:`~klayout_tools.layout_metrics.layout_metrics_report`) and reconciles
their outcomes into one envelope::

    {
      "schema_version": 1,
      "valid": false,
      "gates": [{"check": "drc", "status": "fail", "exit_code": 3, "count": 3}],
      "objective": {"check": "layout-metrics", "name": "cell_count",
                     "value": 42, "polarity": "minimize"},
      "metrics": {"drc": {...full drc report...}, ...},
      "candidate": {"layout": "path/given/via/--candidate"}
    }

``valid`` is the hard gate: ``false`` means at least one declared gate check
failed (a non-clean DRC, an LVS mismatch, a failed sim measurement, or a
caller-declared metric threshold), and the objective must not be used to
rank candidates against a passing one. ``objective`` is one scalar with a
declared polarity (``"minimize"``/``"maximize"``) so a loop can compare two
candidates without domain knowledge of what the number means.

**Which checks constitute the gate, and what the objective is, come from a
descriptor -- never hardcoded here.** A descriptor declares a ``checks``
object (one entry per check name, holding that check's own invocation
arguments -- deck name, request path, etc.), a ``gates`` array (the subset
of ``checks`` names that must pass for ``valid`` to be true), and an
``objective`` object (``check``, ``metric``, ``polarity``). Adding a check
kind this module doesn't yet know about is a registry addition
(:data:`_REGISTRY`), not an envelope change -- see #391/#398 (a future
synthesis/place-and-route/functional-verification descriptor plugs into the
exact same shape).

``layout-metrics`` has no exit-code-driven pass/fail concept of its own (see
the verified exit-code table on issue #387) -- a gate entry naming it
derives pass/fail from a caller-declared ``threshold`` on one of its emitted
metrics (``{"metric": "...", "max": ...}`` and/or ``{"min": ...}``), not
from an exit code. That gate's ``exit_code`` is reported as ``null``.

Candidate substitution: a check's argument values may contain ``{key}``
placeholders (e.g. ``"file": "{layout}"``), filled in from the
``candidate`` mapping the caller passes in (the ``--candidate KEY=VALUE``
CLI flags) -- so an optimizer loop can swap just the candidate file between
iterations without rewriting the whole descriptor. A placeholder with no
matching candidate value is a broken invocation (:class:`EvalError`, exit
1), never a silently-empty substitution.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable
from typing import Any

from .drc import DrcError, run_drc
from .layout_metrics import LayoutMetricsError, layout_metrics_report
from .lvs import LvsError, run_lvs
from .sim import SimError, run_sim


class EvalError(Exception):
    """Raised when ``klt eval`` cannot even be attempted: a missing/malformed
    descriptor, an unknown check name, a candidate placeholder with no
    supplied value, or any named check's own library function raising its
    own error (a bad request, unresolvable layout/netlist input, unknown
    deck, unsupported engine, engine error).

    This is deliberately distinct from ``valid: false`` (a documented,
    trustworthy verdict that a candidate failed one or more gates) -- an
    optimizer must never read this exception's exit code (1) as a bad
    score. See ``docs/cli/eval.md``'s "Exit codes" section.
    """


_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")


def _substitute(value: Any, candidate: dict[str, str]) -> Any:
    """Recursively fill ``{key}`` placeholders in string values from ``candidate``.

    Raises :class:`EvalError` if a placeholder names a key not present in
    ``candidate`` -- a missing candidate value must never resolve to an
    empty/literal string.
    """
    if isinstance(value, str):

        def repl(match: re.Match) -> str:
            key = match.group(1)
            if key not in candidate:
                raise EvalError(
                    f"descriptor references candidate key '{key}' but no "
                    f"--candidate {key}=<value> was given"
                )
            return str(candidate[key])

        return _PLACEHOLDER_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _substitute(v, candidate) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, candidate) for v in value]
    return value


def _lookup_metric(report: dict[str, Any], dotted_key: str) -> Any:
    """Look up a (possibly dotted, e.g. ``"drc.violation_count"``) key path.

    Returns ``None`` if any segment of the path is absent -- a missing
    metric is reported as ``objective.value: null``, not an error, since the
    checks that *did* run are still a trustworthy result (see this module's
    docstring on ``valid``).
    """
    node: Any = report
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


# --------------------------------------------------------------------------- #
# Per-check-kind runner + native-gate functions
# --------------------------------------------------------------------------- #


def _run_drc(args: dict[str, Any]) -> dict[str, Any]:
    try:
        return run_drc(args["file"], args["deck"])
    except KeyError as exc:
        raise EvalError(f"drc check missing required field: {exc}") from exc
    except DrcError as exc:
        raise EvalError(f"drc check failed to run: {exc}") from exc


def _gate_drc(report: dict[str, Any]) -> tuple[str, int, int]:
    status = "pass" if report["status"] == "clean" else "fail"
    return status, report["violation_count"], (0 if status == "pass" else 3)


def _run_lvs(args: dict[str, Any]) -> dict[str, Any]:
    try:
        return run_lvs(args["request"])
    except KeyError as exc:
        raise EvalError(f"lvs check missing required field: {exc}") from exc
    except LvsError as exc:
        raise EvalError(f"lvs check failed to run: {exc}") from exc


def _gate_lvs(report: dict[str, Any]) -> tuple[str, int, int]:
    status = "pass" if report["status"] == "match" else "fail"
    return status, report["mismatch_count"], (0 if status == "pass" else 3)


def _run_sim(args: dict[str, Any]) -> dict[str, Any]:
    try:
        request = args["request"]
    except KeyError as exc:
        raise EvalError(f"sim check missing required field: {exc}") from exc
    try:
        return run_sim(
            request,
            artifacts_dir=args.get("artifacts_dir"),
            backend=args.get("backend"),
            max_workers=args.get("max_workers"),
            hosts=args.get("hosts"),
        )
    except SimError as exc:
        raise EvalError(f"sim check failed to run: {exc}") from exc


def _gate_sim(report: dict[str, Any]) -> tuple[str, int, int]:
    status = "pass" if report["status"] == "pass" else "fail"
    count = report["failed"] + report["errored"]
    if report["status"] == "error":
        exit_code = 4
    elif report["status"] == "fail":
        exit_code = 3
    else:
        exit_code = 0
    return status, count, exit_code


def _run_layout_metrics(args: dict[str, Any]) -> dict[str, Any]:
    try:
        block = args["block"]
    except KeyError as exc:
        raise EvalError(f"layout-metrics check missing required field: {exc}") from exc
    try:
        return layout_metrics_report(block, deck=args.get("deck"))
    except LayoutMetricsError as exc:
        raise EvalError(f"layout-metrics check failed to run: {exc}") from exc


def _gate_layout_metrics(report: dict[str, Any]) -> None:
    # No native exit-code-driven pass/fail concept (see this module's
    # docstring) -- always None so the caller falls through to the
    # caller-declared `threshold` path.
    return None


#: Registry mapping a descriptor check name to its runner + native-gate
#: functions and the argument keys that hold filesystem paths (resolved
#: against the descriptor file's own directory, mirroring `klt lvs`/`klt
#: sim`'s request-relative-path convention). Adding a new check kind here
#: never changes the envelope shape -- see this module's docstring.
_REGISTRY: dict[str, dict[str, Any]] = {
    "drc": {"run": _run_drc, "gate": _gate_drc, "path_keys": ("file",)},
    "lvs": {"run": _run_lvs, "gate": _gate_lvs, "path_keys": ("request",)},
    "sim": {"run": _run_sim, "gate": _gate_sim, "path_keys": ("request",)},
    "layout-metrics": {
        "run": _run_layout_metrics,
        "gate": _gate_layout_metrics,
        "path_keys": ("block",),
    },
}


def _apply_threshold(
    name: str, report: dict[str, Any], threshold: Any
) -> tuple[str, int, None]:
    """Derive pass/fail for a check with no native gate concept (currently
    only ``layout-metrics``) from a caller-declared threshold.

    ``threshold`` must be an object naming a ``metric`` (a top-level or
    dotted-path key into that check's report) plus a ``max`` and/or ``min``
    bound. Raises :class:`EvalError` if the threshold is missing/malformed
    or the named metric is absent/non-numeric -- this is a broken
    invocation (the descriptor is unusable for gating), not a failing
    candidate.
    """
    if not isinstance(threshold, dict) or "metric" not in threshold:
        raise EvalError(
            f"check '{name}' has no native pass/fail outcome; its gate "
            "entry needs a 'threshold': {'metric': <name>, 'max'|'min': <number>}"
        )
    if "max" not in threshold and "min" not in threshold:
        raise EvalError(
            f"check '{name}' threshold must declare a 'max' and/or 'min' bound"
        )
    metric = threshold["metric"]
    value = _lookup_metric(report, metric)
    if value is None:
        raise EvalError(
            f"check '{name}' threshold references unknown metric '{metric}'"
        )
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise EvalError(
            f"check '{name}' threshold metric '{metric}' is not numeric: {value!r}"
        )

    failed = False
    if "max" in threshold and value > threshold["max"]:
        failed = True
    if "min" in threshold and value < threshold["min"]:
        failed = True

    status = "fail" if failed else "pass"
    count = 1 if failed else 0
    return status, count, None


# --------------------------------------------------------------------------- #
# Descriptor loading + validation
# --------------------------------------------------------------------------- #


def _load_descriptor(value: str) -> tuple[dict[str, Any], str | None]:
    """Resolve the ``klt eval`` CLI ``descriptor`` argument into a descriptor
    dict plus the directory relative check paths inside it resolve against.

    ``value`` accepts the same three forms as ``klt lvs``/``klt sim``'s
    request argument (see docs/cli/eval.md): a path to a descriptor JSON
    file, ``"-"`` to read it from stdin, or an inline JSON object string.
    """
    if value == "-":
        try:
            data = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            raise EvalError(f"stdin descriptor is not valid JSON: {exc}") from exc
        return data, None

    if os.path.isdir(value):
        raise EvalError(f"not a file: {value}")

    if os.path.isfile(value):
        try:
            with open(value, encoding="utf-8") as fh:
                data = json.load(fh)
        except OSError as exc:
            raise EvalError(f"cannot read descriptor '{value}': {exc}") from exc
        except json.JSONDecodeError as exc:
            raise EvalError(f"descriptor '{value}' is not valid JSON: {exc}") from exc
        return data, os.path.dirname(os.path.abspath(value))

    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise EvalError(
            f"descriptor '{value}' is neither an existing file (file not "
            f"found) nor valid inline JSON: {exc}"
        ) from exc
    return data, None


def _validate_descriptor(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise EvalError("descriptor must be a JSON object")

    checks = data.get("checks")
    if not isinstance(checks, dict) or not checks:
        raise EvalError("descriptor must declare a non-empty 'checks' object")

    gates = data.get("gates")
    if not isinstance(gates, list):
        raise EvalError("descriptor must declare a 'gates' array of check names")

    objective = data.get("objective")
    if not isinstance(objective, dict):
        raise EvalError("descriptor must declare an 'objective' object")
    for key in ("check", "metric", "polarity"):
        if key not in objective:
            raise EvalError(f"descriptor 'objective' is missing required field '{key}'")
    if objective["polarity"] not in ("minimize", "maximize"):
        raise EvalError(
            "descriptor 'objective.polarity' must be 'minimize' or 'maximize'"
        )

    return data


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def run_eval(
    descriptor_arg: str, candidate: dict[str, str] | None = None
) -> dict[str, Any]:
    """Run the gate/objective composition declared by ``descriptor_arg``.

    ``descriptor_arg`` accepts the same three forms as ``klt lvs``/``klt
    sim``'s request argument -- a path to a descriptor JSON file, ``"-"`` to
    read it from stdin, or an inline JSON object string (see
    :func:`_load_descriptor`). ``candidate`` fills ``{key}`` placeholders in
    each check's own argument values (the ``--candidate KEY=VALUE`` CLI
    flags), letting an optimizer loop swap just the candidate file between
    iterations without rewriting the descriptor.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/eval.md``). Raises :class:`EvalError` for anything that
    prevents a trustworthy result from being produced at all -- a
    malformed/unreadable descriptor, an unknown check name, a candidate
    placeholder with no supplied value, or any named check's own library
    function raising its own error. A documented ``valid: false`` is a
    successful run, not an error -- see this module's docstring.
    """
    data, base_dir = _load_descriptor(descriptor_arg)
    _validate_descriptor(data)
    candidate = dict(candidate) if candidate else {}

    checks_cfg: dict[str, Any] = data["checks"]
    report_cache: dict[str, dict[str, Any]] = {}

    def get_report(name: str) -> dict[str, Any]:
        if name in report_cache:
            return report_cache[name]
        if name not in checks_cfg:
            raise EvalError(f"check '{name}' is not declared in descriptor 'checks'")
        if name not in _REGISTRY:
            raise EvalError(
                f"unknown check '{name}' (supported: {', '.join(sorted(_REGISTRY))})"
            )

        spec = checks_cfg[name]
        if not isinstance(spec, dict):
            raise EvalError(f"check '{name}' config must be a JSON object")

        entry = _REGISTRY[name]
        raw_args = {k: v for k, v in spec.items() if k != "threshold"}
        args = _substitute(raw_args, candidate)

        for path_key in entry["path_keys"]:
            if path_key in args and isinstance(args[path_key], str):
                path_value = args[path_key]
                if base_dir and not os.path.isabs(path_value):
                    args[path_key] = os.path.normpath(
                        os.path.join(base_dir, path_value)
                    )

        report = entry["run"](args)
        report_cache[name] = report
        return report

    gates: list[dict[str, Any]] = []
    for name in data["gates"]:
        if not isinstance(name, str):
            raise EvalError("descriptor 'gates' entries must be strings")

        report = get_report(name)
        spec = checks_cfg.get(name, {})
        gate_fn: Callable[[dict[str, Any]], tuple[str, int, int | None] | None]
        gate_fn = _REGISTRY[name]["gate"]
        native = gate_fn(report)
        if native is not None:
            status, count, exit_code = native
        else:
            status, count, exit_code = _apply_threshold(
                name, report, spec.get("threshold")
            )

        gates.append(
            {"check": name, "status": status, "exit_code": exit_code, "count": count}
        )

    valid = all(gate["status"] == "pass" for gate in gates)

    objective_spec = data["objective"]
    obj_check = objective_spec["check"]
    if obj_check not in _REGISTRY:
        raise EvalError(
            f"objective references unknown check '{obj_check}' "
            f"(supported: {', '.join(sorted(_REGISTRY))})"
        )
    obj_report = get_report(obj_check)
    metric_name = objective_spec["metric"]
    objective = {
        "check": obj_check,
        "name": metric_name,
        "value": _lookup_metric(obj_report, metric_name),
        "polarity": objective_spec["polarity"],
    }

    return {
        "schema_version": 1,
        "valid": valid,
        "gates": gates,
        "objective": objective,
        "metrics": report_cache,
        "candidate": candidate,
    }
