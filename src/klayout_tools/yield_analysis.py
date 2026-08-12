"""Statistical yield analysis (``klt yield``, issue #816, Phase 1a of the
statistical/yield epic #710).

Turns a Monte Carlo **sample set** plus **spec limits** into a yield estimate
that always carries its confidence interval, its Cpk/sigma-to-spec, and a
sample-size verdict. The tool never emits a bare point estimate: a request
that could only produce one (a single sample, a confidence level of 0 or 1, a
measurement with no limits) is an **error**, not a warning.

Pure library: :func:`run_yield` returns plain Python data (a
JSON-serialisable ``dict``) and never prints -- serialisation and
human-readable formatting live in ``cli/yield_cmd.py``, matching every other
``klt`` verb.

The Rust/Python split mirrors ``mom.py``/``congestion.py``: everything
statistical (distribution fit, the exact and parametric estimators, their
confidence intervals, Cp/Cpk, the sample-size verdict) runs in the
``klt_yield_native`` extension (``native/yield/``, this repo's third Rust
component). This module's job is only to read the input document -- either a
`klt sim` Monte Carlo report or a plain sample-set document -- into the JSON
request that extension expects, and to hand its response back out as the
command's payload.

**No new intermediate format.** A `klt sim` MC report (``docs/cli/sim.md``'s
"Monte Carlo sampling") is consumed directly: the per-sample values come from
``corners[]`` entries carrying a non-null ``monte_carlo`` block, and the spec
limits from the report's own ``measurements[].limits``. That is the record
format the canary blocks' MC harnesses already produce, so `klt yield` runs
against real canary output with nothing in between.

See ``docs/cli/yield.md`` for the full input/output schema.
"""

from __future__ import annotations

import json
import os
from typing import Any

#: Mirrors ``native/yield/src/contract.rs``'s ``SCHEMA_VERSION`` -- kept in
#: sync manually, the same convention ``mom.py`` uses for the constants it
#: shares with its own crate.
SCHEMA_VERSION = 1

#: Mirrors ``native/yield/src/contract.rs``'s defaults. Repeated here so the
#: CLI can report the effective value even when the request omits it.
DEFAULT_CONFIDENCE = 0.95
DEFAULT_TARGET_CI_HALFWIDTH = 0.01
ABSOLUTE_MIN_SAMPLES = 2


class YieldError(Exception):
    """Raised when ``klt yield`` cannot run: a bad/missing input document, a
    request the native extension rejects, a sample set too small to carry a
    confidence interval, or the native extension not being built.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- see ``docs/json-contract.md``.
    """


def _load_native() -> Any:
    try:
        import klt_yield_native
    except ImportError as exc:
        raise YieldError(
            "the klt_yield_native extension is not installed -- from a repo "
            "checkout, run `maturin develop --release` inside native/yield/ "
            "(or `uv sync --group yield`); see "
            "docs/cli/yield.md#building-the-native-extension"
        ) from exc
    return klt_yield_native


def _load_json(path: str, what: str) -> Any:
    if not os.path.exists(path):
        raise YieldError(f"{what} file not found: {path}")
    if os.path.isdir(path):
        raise YieldError(f"not a file: {path}")
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise YieldError(f"could not read {what} '{path}': {exc}") from exc


# --------------------------------------------------------------------------- #
# Input readers
# --------------------------------------------------------------------------- #


def _limits_from(raw: Any, name: str, where: str) -> dict[str, Any]:
    """Normalise one measurement's limits object, rejecting junk early."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise YieldError(f"{where}: measurement '{name}' has non-object limits")
    limits: dict[str, Any] = {}
    for key in ("min", "max", "target_yield"):
        value = raw.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise YieldError(
                f"{where}: measurement '{name}' limits.{key} must be a number"
            )
        limits[key] = float(value)
    return limits


def _measurements_from_sim_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Read a `klt sim` Monte Carlo report into this module's request shape.

    Only corners carrying a non-null ``monte_carlo`` block contribute samples
    -- a report that mixes a plain PVT matrix with a Monte Carlo request has
    both, and the deterministic corners are not draws from any distribution.
    """
    corners = report.get("corners")
    if not isinstance(corners, list):
        raise YieldError(
            "sim report has no 'corners' array -- is this a "
            "`klt sim --format json` report?"
        )
    mc_corners = [
        c for c in corners if isinstance(c, dict) and c.get("monte_carlo") is not None
    ]
    if not mc_corners:
        raise YieldError(
            "sim report declares no Monte Carlo samples (no corner carries a "
            "'monte_carlo' block) -- re-run `klt sim` with a `monte_carlo` "
            "request block, or pass a sample-set document instead"
        )

    rollup = report.get("measurements")
    if not isinstance(rollup, list) or not rollup:
        raise YieldError("sim report has no 'measurements' rollup to take limits from")

    out: list[dict[str, Any]] = []
    for entry in rollup:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise YieldError("sim report has a malformed 'measurements' entry")
        name = entry["name"]
        samples: list[float] = []
        errored = 0
        source_corners: list[str] = []
        for corner in mc_corners:
            # Strip the `/mc<sample_index>` suffix `klt sim` appends to a
            # sampled corner's id, so `source_corners` names the originating
            # (pre-sampling) corners the draw was pooled from.
            corner_id = corner.get("corner_id")
            if isinstance(corner_id, str):
                origin = corner_id.rsplit("/mc", 1)[0]
                if origin not in source_corners:
                    source_corners.append(origin)
            for m in corner.get("measurements") or []:
                if not isinstance(m, dict) or m.get("name") != name:
                    continue
                value = m.get("value")
                if value is None:
                    errored += 1
                elif isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise YieldError(
                        f"sim report: measurement '{name}' has a non-numeric value "
                        f"in corner {corner.get('corner_id')!r}"
                    )
                else:
                    samples.append(float(value))
        out.append(
            {
                "name": name,
                "unit": entry.get("unit"),
                "samples": samples,
                "errored": errored,
                "limits": _limits_from(entry.get("limits"), name, "sim report"),
                "source_corners": source_corners,
            }
        )
    return out


def _measurements_from_sample_set(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Read a plain sample-set document into this module's request shape."""
    entries = doc.get("measurements")
    if not isinstance(entries, list) or not entries:
        raise YieldError(
            "sample set has no non-empty 'measurements' array -- see "
            "docs/cli/yield.md's 'Sample-set document' section"
        )
    out: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise YieldError("sample set has a malformed 'measurements' entry")
        name = entry["name"]
        raw_samples = entry.get("samples")
        if not isinstance(raw_samples, list):
            raise YieldError(f"sample set: measurement '{name}' has no 'samples' array")
        samples: list[float] = []
        errored = int(entry.get("errored") or 0)
        for value in raw_samples:
            if value is None:
                errored += 1
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise YieldError(
                    f"sample set: measurement '{name}' has a non-numeric sample value"
                )
            samples.append(float(value))
        source_corners = entry.get("source_corners") or []
        if not isinstance(source_corners, list):
            raise YieldError(
                f"sample set: measurement '{name}' has a non-array 'source_corners'"
            )
        out.append(
            {
                "name": name,
                "unit": entry.get("unit"),
                "samples": samples,
                "errored": errored,
                "limits": _limits_from(entry.get("limits"), name, "sample set"),
                "source_corners": [str(c) for c in source_corners],
            }
        )
    return out


def _read_samples(path: str) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Return ``(kind, measurements, source_info)`` for the input document."""
    doc = _load_json(path, "samples")
    if not isinstance(doc, dict):
        raise YieldError(f"samples '{path}' must be a JSON object")

    if isinstance(doc.get("corners"), list):
        measurements = _measurements_from_sim_report(doc)
        environment = doc.get("environment")
        mc = environment.get("monte_carlo") if isinstance(environment, dict) else None
        source = {
            "kind": "sim-report",
            "netlist": doc.get("netlist"),
            "monte_carlo": mc,
        }
        return "sim-report", measurements, source

    if isinstance(doc.get("measurements"), list):
        measurements = _measurements_from_sample_set(doc)
        return (
            "sample-set",
            measurements,
            {"kind": "sample-set", "netlist": None, "monte_carlo": None},
        )

    raise YieldError(
        f"samples '{path}' is neither a `klt sim` report (no 'corners' array) nor "
        "a sample-set document (no 'measurements' array) -- see docs/cli/yield.md"
    )


def _read_spec(path: str) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Read a spec-limits file into ``(per_measurement_limits, run_defaults)``."""
    doc = _load_json(path, "limits")
    if not isinstance(doc, dict):
        raise YieldError(f"limits '{path}' must be a JSON object")

    defaults: dict[str, Any] = {}
    for key in ("confidence", "target_ci_halfwidth", "min_samples", "target_yield"):
        if doc.get(key) is not None:
            defaults[key] = doc[key]

    raw = doc.get("measurements")
    if raw is None:
        raise YieldError(
            f"limits '{path}' has no 'measurements' object -- see docs/cli/yield.md's "
            "'Spec-limits file' section"
        )
    if not isinstance(raw, dict):
        raise YieldError(
            f"limits '{path}': 'measurements' must be an object keyed by "
            "measurement name"
        )
    limits = {
        name: _limits_from(value, name, f"limits '{path}'")
        for name, value in raw.items()
    }
    return limits, defaults


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def run_yield(
    samples_path: str,
    *,
    limits_path: str | None = None,
    confidence: float | None = None,
    target_ci_halfwidth: float | None = None,
    min_samples: int | None = None,
    measurements: list[str] | None = None,
) -> dict[str, Any]:
    """Estimate yield for every measurement in ``samples_path``.

    ``samples_path`` is either a `klt sim` Monte Carlo report or a plain
    sample-set document (auto-detected). ``limits_path`` optionally supplies
    or overrides the spec limits, plus run-level defaults. ``measurements``
    restricts the analysis to the named measurements -- an explicitly named
    measurement with no limits is an error rather than a silent skip.

    Returns this command's JSON payload (see ``docs/cli/yield.md``); raises
    :class:`YieldError` for anything that makes the analysis unrunnable.
    """
    kind, entries, source = _read_samples(samples_path)

    spec_limits: dict[str, dict[str, Any]] = {}
    spec_defaults: dict[str, Any] = {}
    if limits_path is not None:
        spec_limits, spec_defaults = _read_spec(limits_path)

    by_name = {e["name"]: e for e in entries}
    if measurements is not None:
        missing = [n for n in measurements if n not in by_name]
        if missing:
            raise YieldError(
                f"no such measurement in {samples_path}: {', '.join(sorted(missing))} "
                f"(available: {', '.join(sorted(by_name)) or 'none'})"
            )
        selected = [by_name[n] for n in measurements]
    else:
        selected = entries

    run_target_yield = spec_defaults.get("target_yield")
    warnings: list[str] = []
    request_measurements: list[dict[str, Any]] = []
    for entry in selected:
        name = entry["name"]
        limits = dict(entry["limits"])
        # A spec-limits file wins over whatever the sample document carried:
        # it is the caller's explicit statement of the spec being verified.
        limits.update(spec_limits.get(name, {}))
        if run_target_yield is not None and "target_yield" not in limits:
            limits["target_yield"] = run_target_yield
        if limits.get("min") is None and limits.get("max") is None:
            if measurements is not None:
                raise YieldError(
                    f"measurement '{name}' was requested explicitly but declares no "
                    "spec limits -- there is no yield to estimate without a limit "
                    "to estimate it against; supply one via --limits"
                )
            warnings.append(
                f"measurement '{name}' declares no spec limits and was skipped; "
                "supply limits via --limits to include it"
            )
            continue
        request_measurements.append({**entry, "limits": limits})

    if not request_measurements:
        raise YieldError(
            "no measurement has spec limits to estimate a yield against -- supply "
            "them via --limits (see docs/cli/yield.md)"
        )

    request: dict[str, Any] = {
        "confidence": _resolve(
            confidence, spec_defaults.get("confidence"), DEFAULT_CONFIDENCE
        ),
        "target_ci_halfwidth": _resolve(
            target_ci_halfwidth,
            spec_defaults.get("target_ci_halfwidth"),
            DEFAULT_TARGET_CI_HALFWIDTH,
        ),
        "measurements": request_measurements,
    }
    resolved_min = _resolve(
        min_samples, spec_defaults.get("min_samples"), ABSOLUTE_MIN_SAMPLES
    )
    request["min_samples"] = int(resolved_min)

    native = _load_native()
    try:
        response_json = native.analyze_yield_json(json.dumps(request))
    except ValueError as exc:
        raise YieldError(str(exc)) from exc
    response = json.loads(response_json)

    payload: dict[str, Any] = {
        "samples": samples_path,
        "limits": limits_path,
        "source": {
            **source,
            "kind": kind,
            "sample_count": sum(len(m["samples"]) for m in request_measurements),
        },
    }
    payload.update(response)
    # The native core owns the payload's shape version; this module's
    # constant mirrors it (and is asserted against it below) so a crate-side
    # bump that Python never noticed cannot ship silently.
    if response.get("schema_version") != SCHEMA_VERSION:
        raise YieldError(
            "klt_yield_native reports schema_version "
            f"{response.get('schema_version')}, but this klt build expects "
            f"{SCHEMA_VERSION} -- rebuild the extension (`uv sync --group yield`)"
        )
    payload["schema_version"] = SCHEMA_VERSION
    payload["warnings"] = warnings + list(response.get("warnings") or [])
    return payload


def _resolve(explicit: Any, from_spec: Any, fallback: Any) -> Any:
    """CLI flag beats the spec file, which beats the documented default."""
    if explicit is not None:
        return explicit
    if from_spec is not None:
        return from_spec
    return fallback
