"""``klt eval``: score a candidate against a per-block descriptor, in one call.

Pure orchestration -- this module never re-implements DRC/LVS/sim/metrics
logic. It imports and calls the existing library entry points
(:func:`~klayout_tools.drc.run_drc`, :func:`~klayout_tools.lvs.run_lvs`,
:func:`~klayout_tools.sim.run_sim`,
:func:`~klayout_tools.layout_metrics.layout_metrics_report`) the same way
``klt drc``/``klt lvs``/``klt sim``/``klt layout-metrics`` themselves do, and
reconciles their four separate exit-code vocabularies into one envelope:

    {
        "schema_version": 1,
        "valid": false,
        "gates": [{"check": "drc", "name": "drc", "status": "fail",
                    "exit_code": 3, "count": 3}],
        "objective": {"name": "area_um2", "value": 1240.5,
                      "polarity": "minimize"},
        "metrics": {}
    }

This is the "scorer" an agent needs to hill-climb blindly across hundreds of
turns (issue #387): a hard ``valid`` gate that can't be argued with, plus one
scalar ``objective`` with a declared polarity so two candidates can be
compared without domain knowledge.

Descriptor-driven, never hardcoded
-----------------------------------

Which checks constitute the gate, and what the objective is, come from a
*descriptor* document -- never a fixed check list baked into this module.
That is what lets this verb generalise beyond today's four analog checks
(``drc``/``lvs``/``sim``/``layout-metrics``) to a future descriptor naming
digital-flow checks (synthesis, functional verification -- see issues
#391/#398) without a code change here, only a new entry in
:data:`_INVOKE_FNS`.

Descriptor shape (see ``docs/cli/eval.md`` for the full field reference)::

    {
      "gates": [
        {"check": "drc", "args": {"file": "${layout}", "deck": "sky130"}},
        {"check": "lvs", "args": {"request": "lvs_request.json"}},
        {"check": "layout-metrics", "args": {"block": "${block}"},
         "threshold": {"metric": "cell_count", "max": 500}}
      ],
      "objective": {
        "check": "layout-metrics", "metric": "cell_count",
        "polarity": "minimize", "args": {"block": "${block}"}
      },
      "metrics": [
        {"name": "layout_metrics", "check": "layout-metrics",
         "args": {"block": "${block}"}}
      ]
    }

A *candidate* (the paths that change every hill-climbing turn, while the
descriptor's gate/objective composition stays fixed) is a separate, small
JSON object of ``${name}``-style substitution values -- ``${layout}``/
``${block}`` above -- applied to every ``args`` dict before invoking its
check. This is what lets one descriptor be reused, unmodified, across many
candidates in an optimizer loop, rather than being rewritten per turn.

``layout-metrics`` has no gate semantics of its own (see
``docs/cli/layout-metrics.md`` -- no exit code above 2), so a gate naming it
*must* declare a ``threshold`` (a ``metric`` path plus ``min``/``max``/
``equals``) to derive pass/fail -- it is deliberately excluded from
:data:`_DEFAULT_STATUS_FNS`, so omitting ``threshold`` on a ``layout-metrics``
gate raises :class:`EvalError` rather than silently always passing. Any gate
may declare a ``threshold`` to override its check's default status derivation
(e.g. gating ``drc`` on a violation-count ceiling instead of "any violation
fails").

Exit codes (mirrored in ``cli/eval_cmd.py`` -- see ``docs/cli/eval.md``):
    0 - ran, ``valid: true``
    1 - failed to run at all (bad descriptor/candidate, unresolvable
        candidate path, unknown check, an underlying `klt` subcommand's own
        "failed to run" error) -- raised here as :class:`EvalError`
    3 - ran successfully, ``valid: false`` (at least one gate failed)
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand.) An optimizer must never read exit 1 (crash) as exit 3 (a bad,
but real, score) -- see the acceptance criteria in issue #387.
"""

from __future__ import annotations

import json
import os
import string
import sys
from collections.abc import Callable
from typing import Any

from .drc import DrcError, run_drc
from .layout_metrics import LayoutMetricsError, layout_metrics_report
from .lvs import LvsError, run_lvs
from .sim import SimError, run_sim

SCHEMA_VERSION = 1

_UNDERLYING_ERRORS = (DrcError, LvsError, SimError, LayoutMetricsError)


class EvalError(Exception):
    """Raised for anything that prevents a trustworthy envelope from being
    produced at all: a malformed/missing descriptor or candidate, an unknown
    check name, an unresolvable candidate path, or an underlying `klt`
    subcommand's own "failed to run" error. Never raised for a check that ran
    successfully and reported a failing verdict -- that is a ``gates[]``
    entry with ``status: "fail"``, not an error (see this module's
    docstring)."""


# ---------------------------------------------------------------------------
# descriptor / candidate loading (same file / "-" / inline-JSON convention as
# `klt lvs`'s `load_request_arg` -- see lvs.py)
# ---------------------------------------------------------------------------


def _load_json_arg(value: str, label: str) -> tuple[Any, str]:
    """Resolve a `klt eval` CLI argument (`descriptor` or `--candidate`) into
    a parsed JSON value plus the directory relative paths inside it should
    resolve against -- the same three forms `klt lvs`'s `request` argument
    accepts (see `docs/cli/eval.md`): a path to a JSON file, `"-"` to read
    from stdin, or an inline JSON value string."""
    if value == "-":
        try:
            data = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            raise EvalError(f"stdin {label} is not valid JSON: {exc}") from exc
        return data, os.getcwd()

    if os.path.isfile(value):
        try:
            with open(value) as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise EvalError(f"{label} file '{value}' is not valid JSON: {exc}") from exc
        return data, os.path.dirname(os.path.abspath(value))

    if os.path.isdir(value):
        raise EvalError(f"{label} '{value}' is a directory, not a file")

    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise EvalError(
            f"{label} '{value}' is neither an existing file (file not found) "
            f"nor valid inline JSON: {exc}"
        ) from exc
    return data, os.getcwd()


# ---------------------------------------------------------------------------
# candidate substitution
# ---------------------------------------------------------------------------


def _substitute(obj: Any, candidate: dict[str, Any]) -> Any:
    """Recursively replace ``${name}`` placeholders in every string leaf of
    ``obj`` using ``candidate`` (``string.Template``'s convention -- also
    accepts the bare ``$name`` form). Raises :class:`EvalError` naming the
    missing key when the descriptor references a candidate key that was not
    provided -- an unresolvable candidate path must fail loudly (exit 1),
    never resolve to a literal ``"${name}"`` string handed to a check."""
    if isinstance(obj, str):
        try:
            return string.Template(obj).substitute(candidate)
        except KeyError as exc:
            raise EvalError(
                f"descriptor references candidate key {exc} that 'klt eval' "
                "was not given -- unresolvable candidate path"
            ) from exc
    if isinstance(obj, dict):
        return {key: _substitute(val, candidate) for key, val in obj.items()}
    if isinstance(obj, list):
        return [_substitute(val, candidate) for val in obj]
    return obj


def _resolve_path(value: Any, base_dir: str, field: str) -> str:
    if not isinstance(value, str):
        raise EvalError(f"'{field}' must be a string, got {type(value).__name__}")
    return (
        value
        if os.path.isabs(value)
        else os.path.normpath(os.path.join(base_dir, value))
    )


def _resolve_request(value: Any, base_dir: str) -> str:
    """Resolve a `lvs`/`sim` check's `request` arg into the string form
    `run_lvs`/`run_sim` accept. An inline object is serialised to JSON
    (resolves its own relative paths against the current working directory,
    same as `klt lvs`/`klt sim`'s own inline-request convention); a string
    that is not `"-"` or inline JSON text is treated as a file path and
    resolved against the descriptor's own directory, mirroring `file`/
    `block` path resolution below."""
    if isinstance(value, dict):
        return json.dumps(value)
    if not isinstance(value, str):
        raise EvalError(
            f"'request' must be a string or object, got {type(value).__name__}"
        )
    if value == "-" or value.lstrip().startswith("{"):
        return value
    return _resolve_path(value, base_dir, "request")


# ---------------------------------------------------------------------------
# per-check invoke adapters -- the only place this module calls into another
# `klt` subcommand's library entry point (never re-implements one)
# ---------------------------------------------------------------------------


def _invoke_drc(args: dict[str, Any], base_dir: str) -> dict[str, Any]:
    if "file" not in args or "deck" not in args:
        raise EvalError("'drc' check args require 'file' and 'deck'")
    return run_drc(_resolve_path(args["file"], base_dir, "file"), args["deck"])


def _invoke_lvs(args: dict[str, Any], base_dir: str) -> dict[str, Any]:
    if "request" not in args:
        raise EvalError("'lvs' check args require 'request'")
    return run_lvs(_resolve_request(args["request"], base_dir))


def _invoke_sim(args: dict[str, Any], base_dir: str) -> dict[str, Any]:
    if "request" not in args:
        raise EvalError("'sim' check args require 'request'")
    return run_sim(
        _resolve_request(args["request"], base_dir),
        artifacts_dir=args.get("artifacts_dir"),
        backend=args.get("backend"),
        max_workers=args.get("max_workers"),
        hosts=args.get("hosts"),
    )


def _invoke_layout_metrics(args: dict[str, Any], base_dir: str) -> dict[str, Any]:
    if "block" not in args:
        raise EvalError("'layout-metrics' check args require 'block'")
    return layout_metrics_report(
        _resolve_path(args["block"], base_dir, "block"), deck=args.get("deck")
    )


_INVOKE_FNS: dict[str, Callable[[dict[str, Any], str], dict[str, Any]]] = {
    "drc": _invoke_drc,
    "lvs": _invoke_lvs,
    "sim": _invoke_sim,
    "layout-metrics": _invoke_layout_metrics,
}


# ---------------------------------------------------------------------------
# per-check default status derivation -- mirrors each check's own `*_cmd.py`
# exit-code logic exactly, so a cited `exit_code` matches what `klt <check>`
# itself would have returned for the same report.
# ---------------------------------------------------------------------------


def _status_drc(report: dict[str, Any]) -> tuple[str, int, Any]:
    status = "pass" if report["status"] == "clean" else "fail"
    return status, (0 if status == "pass" else 3), report.get("violation_count")


def _status_lvs(report: dict[str, Any]) -> tuple[str, int, Any]:
    status = "pass" if report["status"] == "match" else "fail"
    return status, (0 if status == "pass" else 3), report.get("mismatch_count")


def _status_sim(report: dict[str, Any]) -> tuple[str, int, Any]:
    if report["status"] == "pass":
        return "pass", 0, 0
    if report["status"] == "error":
        return "fail", 4, report.get("errored")
    return "fail", 3, report.get("failed")


# Deliberately excludes "layout-metrics" -- it has no exit code above 2 (no
# gate semantics of its own), so a gate naming it must declare `threshold`;
# see `_derive_status` and this module's docstring.
_DEFAULT_STATUS_FNS: dict[str, Callable[[dict[str, Any]], tuple[str, int, Any]]] = {
    "drc": _status_drc,
    "lvs": _status_lvs,
    "sim": _status_sim,
}


def _extract_metric(report: dict[str, Any], key: str, check: str) -> Any:
    """Dotted-path extraction (``"measurements.0.worst_case.value"``-style,
    dict keys and list indices) of a metric out of a check's report."""
    value: Any = report
    for part in key.split("."):
        if isinstance(value, list):
            try:
                index = int(part)
            except ValueError as exc:
                raise EvalError(
                    f"metric path '{key}' expects a list index at '{part}' "
                    f"(in the '{check}' report)"
                ) from exc
            try:
                value = value[index]
            except IndexError as exc:
                raise EvalError(
                    f"metric path '{key}' index {index} out of range "
                    f"(in the '{check}' report)"
                ) from exc
        elif isinstance(value, dict):
            if part not in value:
                raise EvalError(
                    f"metric path '{key}' has no field '{part}' in the '{check}' report"
                )
            value = value[part]
        else:
            raise EvalError(
                f"metric path '{key}' cannot descend into '{part}' -- the "
                f"'{check}' report value at that point is not an object/list"
            )
    return value


def _derive_status(
    check: str, report: dict[str, Any], gate: dict[str, Any]
) -> tuple[str, int, Any]:
    """Pass/fail + cited exit code + an optional headline count for one gate.

    A ``threshold`` on the gate (``{"metric": ..., "min"/"max"/"equals": ...}``)
    always overrides a check's own default status derivation -- required for
    ``layout-metrics`` (no built-in gate semantics), optional for
    ``drc``/``lvs``/``sim`` (e.g. gating on a violation-count ceiling instead
    of "any violation fails"). The exit code cited for a threshold-derived
    gate is always ``0`` -- the underlying check itself ran and reported
    successfully; the fail/pass split comes from the threshold comparison,
    not from that check's own exit-code vocabulary.
    """
    threshold = gate.get("threshold")
    if threshold is not None:
        if not isinstance(threshold, dict) or "metric" not in threshold:
            name = gate.get("name", check)
            raise EvalError(
                f"gate '{name}' threshold must be an object with a 'metric' field"
            )
        if not any(key in threshold for key in ("min", "max", "equals")):
            name = gate.get("name", check)
            raise EvalError(
                f"gate '{name}' threshold needs at least one of 'min'/'max'/'equals'"
            )
        value = _extract_metric(report, threshold["metric"], check)
        ok = True
        if threshold.get("min") is not None:
            ok = ok and value >= threshold["min"]
        if threshold.get("max") is not None:
            ok = ok and value <= threshold["max"]
        if threshold.get("equals") is not None:
            ok = ok and value == threshold["equals"]
        return ("pass" if ok else "fail"), 0, value

    status_fn = _DEFAULT_STATUS_FNS.get(check)
    if status_fn is None:
        name = gate.get("name", check)
        raise EvalError(
            f"gate '{name}': check '{check}' has no built-in gate pass/fail "
            "semantics -- declare a 'threshold' on this gate"
        )
    return status_fn(report)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def run_eval(descriptor_arg: str, candidate_arg: str | None = None) -> dict[str, Any]:
    """Evaluate the candidate declared by ``candidate_arg`` against the gate/
    objective composition declared by ``descriptor_arg``.

    Both accept the same three forms as `klt lvs`'s ``request`` argument: a
    path to a JSON file, ``"-"`` to read from stdin, or an inline JSON value
    string (see `docs/cli/eval.md`). ``candidate_arg`` may be omitted when
    the descriptor's ``args`` need no ``${name}`` substitution.

    Returns a dict matching the documented JSON schema (see
    `docs/cli/eval.md`). Raises :class:`EvalError` for anything that
    prevents a trustworthy envelope from being produced at all -- a bad
    descriptor/candidate, an unknown check, an unresolvable candidate path,
    or an underlying `klt` subcommand's own "failed to run" error. A
    successful run that finds a real problem (a DRC violation, an LVS
    mismatch, a failed sim limit, a threshold miss) is never an error -- it
    is reported as ``valid: false`` with the failing gate cited by name.
    """
    descriptor, descriptor_dir = _load_json_arg(descriptor_arg, "descriptor")
    if not isinstance(descriptor, dict):
        raise EvalError("descriptor must be a JSON object")

    candidate: dict[str, Any] = {}
    if candidate_arg is not None:
        candidate_data, _ = _load_json_arg(candidate_arg, "candidate")
        if not isinstance(candidate_data, dict):
            raise EvalError("candidate must be a JSON object")
        candidate = candidate_data

    gates_spec = descriptor.get("gates")
    if not isinstance(gates_spec, list) or not gates_spec:
        raise EvalError("descriptor must declare a non-empty 'gates' list")

    objective_spec = descriptor.get("objective")
    if not isinstance(objective_spec, dict):
        raise EvalError("descriptor must declare an 'objective' object")
    if "check" not in objective_spec or "metric" not in objective_spec:
        raise EvalError("descriptor 'objective' must declare 'check' and 'metric'")
    polarity = objective_spec.get("polarity", "minimize")
    if polarity not in ("minimize", "maximize"):
        raise EvalError(
            f"objective 'polarity' must be 'minimize' or 'maximize', got {polarity!r}"
        )

    metrics_spec = descriptor.get("metrics", [])
    if not isinstance(metrics_spec, list):
        raise EvalError("descriptor 'metrics' must be a list")

    cache: dict[str, dict[str, Any]] = {}

    def _run_check(check: Any, raw_args: Any, label: str) -> dict[str, Any]:
        if not isinstance(check, str) or check not in _INVOKE_FNS:
            raise EvalError(
                f"{label}: unknown check {check!r} (supported: {sorted(_INVOKE_FNS)})"
            )
        if raw_args is None:
            raw_args = {}
        if not isinstance(raw_args, dict):
            raise EvalError(f"{label}: 'args' must be an object")
        substituted = _substitute(raw_args, candidate)
        cache_key = f"{check}:{json.dumps(substituted, sort_keys=True, default=str)}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            report = _INVOKE_FNS[check](substituted, descriptor_dir)
        except _UNDERLYING_ERRORS as exc:
            raise EvalError(f"{label} ('{check}') failed to run: {exc}") from exc
        cache[cache_key] = report
        return report

    gates: list[dict[str, Any]] = []
    all_pass = True
    for idx, gate in enumerate(gates_spec):
        if not isinstance(gate, dict) or "check" not in gate:
            raise EvalError(f"gates[{idx}] must be an object with a 'check' field")
        check = gate["check"]
        name = gate.get("name", check)
        report = _run_check(check, gate.get("args"), f"gates[{idx}]")
        status, exit_code, count = _derive_status(check, report, gate)
        entry: dict[str, Any] = {
            "check": check,
            "name": name,
            "status": status,
            "exit_code": exit_code,
        }
        if count is not None:
            entry["count"] = count
        gates.append(entry)
        if status != "pass":
            all_pass = False

    objective_report = _run_check(
        objective_spec["check"], objective_spec.get("args"), "objective"
    )
    objective_value = _extract_metric(
        objective_report, objective_spec["metric"], objective_spec["check"]
    )
    objective = {
        "name": objective_spec.get("name", objective_spec["metric"]),
        "value": objective_value,
        "polarity": polarity,
    }

    metrics: dict[str, Any] = {}
    for idx, entry_spec in enumerate(metrics_spec):
        if not isinstance(entry_spec, dict) or "check" not in entry_spec:
            raise EvalError(f"metrics[{idx}] must be an object with a 'check' field")
        check = entry_spec["check"]
        name = entry_spec.get("name", check)
        report = _run_check(check, entry_spec.get("args"), f"metrics[{idx}]")
        if "metric" in entry_spec:
            metrics[name] = _extract_metric(report, entry_spec["metric"], check)
        else:
            metrics[name] = report

    return {
        "schema_version": SCHEMA_VERSION,
        "valid": all_pass,
        "gates": gates,
        "objective": objective,
        "metrics": metrics,
    }
