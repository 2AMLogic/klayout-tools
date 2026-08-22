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

#: Default tolerance for the analytic cross-check (issue #817, Phase 1b of
#: the yield epic #710), expressed in units of the analytic model's own
#: ``stddev``: the empirical mean must land within this many analytic
#: sigmas of the analytic mean, and the empirical stddev must be within
#: this same fraction of the analytic stddev. One knob, one unit, so a
#: caller tightening or loosening the check only has one number to reason
#: about.
DEFAULT_ANALYTIC_TOLERANCE_SIGMA = 0.2


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


def _analytic_from(raw: Any, name: str, where: str) -> dict[str, Any] | None:
    """Normalise one measurement's optional analytic cross-check model
    (issue #817): a closed-form ``mean``/``stddev`` -- e.g. a kT/C noise
    term or a mismatch-dominated offset with a known sigma -- to compare
    the empirical Monte Carlo fit against.

    ``None`` when the measurement declares no analytic model; that is the
    common case and is not an error -- not every measurement has a
    closed form to check against.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise YieldError(f"{where}: measurement '{name}' has non-object analytic")
    mean = raw.get("mean")
    stddev = raw.get("stddev")
    if mean is None or stddev is None:
        raise YieldError(
            f"{where}: measurement '{name}' analytic block needs both 'mean' and "
            "'stddev' -- there is no closed-form distribution to cross-check "
            "against without both"
        )
    for key, value in (("mean", mean), ("stddev", stddev)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise YieldError(
                f"{where}: measurement '{name}' analytic.{key} must be a number"
            )
    stddev = float(stddev)
    if stddev < 0:
        raise YieldError(f"{where}: measurement '{name}' analytic.stddev must be >= 0")
    model = raw.get("model")
    if model is not None and not isinstance(model, str):
        raise YieldError(
            f"{where}: measurement '{name}' analytic.model must be a string"
        )
    return {"mean": float(mean), "stddev": stddev, "model": model}


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
                "analytic": _analytic_from(entry.get("analytic"), name, "sim report"),
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
                "analytic": _analytic_from(entry.get("analytic"), name, "sample set"),
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


def _read_spec(
    path: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], dict[str, dict[str, Any]]]:
    """Read a spec-limits file into ``(per_measurement_limits, run_defaults,
    per_measurement_analytic)``.

    ``per_measurement_analytic`` (issue #817) is keyed the same way as
    ``per_measurement_limits`` but only carries an entry for a measurement
    that declares an ``analytic`` block -- most measurements have no closed
    form to check against, so absence is the common case, not an error.
    """
    doc = _load_json(path, "limits")
    if not isinstance(doc, dict):
        raise YieldError(f"limits '{path}' must be a JSON object")

    defaults: dict[str, Any] = {}
    for key in (
        "confidence",
        "target_ci_halfwidth",
        "min_samples",
        "target_yield",
        "analytic_tolerance_sigma",
    ):
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
    analytics: dict[str, dict[str, Any]] = {}
    for name, value in raw.items():
        if not isinstance(value, dict):
            continue
        model = _analytic_from(value.get("analytic"), name, f"limits '{path}'")
        if model is not None:
            analytics[name] = model
    return limits, defaults, analytics


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
    negative_control_path: str | None = None,
    analytic_tolerance_sigma: float | None = None,
) -> dict[str, Any]:
    """Estimate yield for every measurement in ``samples_path``.

    ``samples_path`` is either a `klt sim` Monte Carlo report or a plain
    sample-set document (auto-detected). ``limits_path`` optionally supplies
    or overrides the spec limits, plus run-level defaults. ``measurements``
    restricts the analysis to the named measurements -- an explicitly named
    measurement with no limits is an error rather than a silent skip.

    Issue #817 (Phase 1b of the yield epic #710) adds two reality-grounding
    checks, both reported alongside the yield estimate rather than gating it
    on their own exit code:

    - ``negative_control_path`` -- a seeded, known-bad variant (same input
      shape as ``samples_path``) that should demonstrably show the
      degradation the statistics claim to detect. Omitting it, or supplying
      one that doesn't show the expected degradation, is *flagged* (a
      run-level and per-measurement ``negative_control`` status, plus a
      warning) rather than silently accepted.
    - ``analytic_tolerance_sigma`` -- the tolerance (in analytic-model
      sigmas) an ``analytic`` block declared on a measurement (in the
      sample document or the spec-limits file) is checked against. Only
      measurements that declare one are cross-checked; most don't, and
      that's not an error.

    Returns this command's JSON payload (see ``docs/cli/yield.md``); raises
    :class:`YieldError` for anything that makes the analysis unrunnable.
    """
    kind, entries, source = _read_samples(samples_path)

    spec_limits: dict[str, dict[str, Any]] = {}
    spec_defaults: dict[str, Any] = {}
    spec_analytics: dict[str, dict[str, Any]] = {}
    if limits_path is not None:
        spec_limits, spec_defaults, spec_analytics = _read_spec(limits_path)

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
    # A spec-limits file's analytic model wins over the sample document's own
    # (same precedence as limits), keyed by measurement name; only
    # measurements that end up in `request_measurements` matter here.
    name_to_analytic: dict[str, dict[str, Any]] = {}
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
        analytic = spec_analytics.get(name, entry.get("analytic"))
        if analytic is not None:
            name_to_analytic[name] = analytic

    if not request_measurements:
        raise YieldError(
            "no measurement has spec limits to estimate a yield against -- supply "
            "them via --limits (see docs/cli/yield.md)"
        )

    resolved_confidence = _resolve(
        confidence, spec_defaults.get("confidence"), DEFAULT_CONFIDENCE
    )
    resolved_ci_halfwidth = _resolve(
        target_ci_halfwidth,
        spec_defaults.get("target_ci_halfwidth"),
        DEFAULT_TARGET_CI_HALFWIDTH,
    )
    resolved_min = _resolve(
        min_samples, spec_defaults.get("min_samples"), ABSOLUTE_MIN_SAMPLES
    )
    resolved_analytic_tolerance = _resolve(
        analytic_tolerance_sigma,
        spec_defaults.get("analytic_tolerance_sigma"),
        DEFAULT_ANALYTIC_TOLERANCE_SIGMA,
    )
    if not (float(resolved_analytic_tolerance) > 0):
        raise YieldError(
            "analytic_tolerance_sigma must be > 0 (got "
            f"{resolved_analytic_tolerance}); a non-positive tolerance can "
            "never be satisfied"
        )

    request: dict[str, Any] = {
        "confidence": resolved_confidence,
        "target_ci_halfwidth": resolved_ci_halfwidth,
        "measurements": request_measurements,
    }
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

    primary_by_name = {m["name"]: m for m in payload["measurements"]}

    # Issue #817: negative control -- a seeded known-bad variant that must
    # demonstrably show the degradation the statistics claim to detect.
    nc_overall, nc_by_name = _negative_control_report(
        negative_control_path,
        request_measurements,
        primary_by_name,
        confidence=resolved_confidence,
        target_ci_halfwidth=resolved_ci_halfwidth,
        min_samples=int(resolved_min),
        native=native,
        warnings=warnings,
    )
    payload["negative_control"] = nc_overall

    # Issue #817: analytic cross-check -- compare the empirical fit against
    # a closed-form distribution where the measurement declares one.
    ac_overall, ac_by_name = _analytic_cross_check(
        request_measurements,
        name_to_analytic,
        primary_by_name,
        tolerance_sigma=float(resolved_analytic_tolerance),
        warnings=warnings,
    )
    payload["analytic_cross_check"] = ac_overall

    for m in payload["measurements"]:
        name = m["name"]
        m["negative_control"] = nc_by_name.get(name, {"status": "not_provided"})
        m["analytic_cross_check"] = ac_by_name.get(name, {"status": "not_provided"})

    payload["warnings"] = warnings + list(response.get("warnings") or [])
    return payload


def _negative_control_report(
    negative_control_path: str | None,
    request_measurements: list[dict[str, Any]],
    primary_by_name: dict[str, dict[str, Any]],
    *,
    confidence: float,
    target_ci_halfwidth: float,
    min_samples: int,
    native: Any,
    warnings: list[str],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Check every analysed measurement against a seeded known-bad variant.

    Returns ``(run_level_summary, per_measurement)``. Appends a warning to
    ``warnings`` (in place) for anything short of every measurement showing
    the expected degradation -- issue #817's "flagged rather than silently
    accepted" requirement.
    """
    if negative_control_path is None:
        warnings.append(
            "no negative control was supplied for this campaign -- there is "
            "no seeded known-bad variant demonstrating that this statistical "
            "test can actually detect a real regression; supply one via "
            "--negative-control"
        )
        return {"provided": False, "samples": None, "status": "not_provided"}, {}

    _nc_kind, nc_entries, _nc_source = _read_samples(negative_control_path)
    nc_by_input_name = {e["name"]: e for e in nc_entries}

    per_measurement: dict[str, dict[str, Any]] = {}
    for entry in request_measurements:
        name = entry["name"]
        nc_entry = nc_by_input_name.get(name)
        if nc_entry is None:
            per_measurement[name] = {"status": "missing"}
            warnings.append(
                f"measurement '{name}': no matching entry in the negative "
                f"control samples ({negative_control_path}) -- its "
                "statistical power to detect a real regression is unverified"
            )
            continue

        nc_request = {
            "confidence": confidence,
            "target_ci_halfwidth": target_ci_halfwidth,
            "min_samples": min_samples,
            "measurements": [
                {
                    "name": name,
                    "unit": nc_entry.get("unit"),
                    "samples": nc_entry["samples"],
                    "errored": nc_entry.get("errored", 0),
                    "limits": entry["limits"],
                    "source_corners": nc_entry.get("source_corners", []),
                }
            ],
        }
        try:
            nc_response_json = native.analyze_yield_json(json.dumps(nc_request))
        except ValueError as exc:
            per_measurement[name] = {"status": "error", "detail": str(exc)}
            warnings.append(
                f"measurement '{name}': the negative control could not be "
                f"analyzed ({exc})"
            )
            continue

        nc_measurement = json.loads(nc_response_json)["measurements"][0]
        status = _negative_control_status(nc_measurement, primary_by_name[name])
        per_measurement[name] = {
            "status": status,
            "n": nc_measurement["n"],
            "errored": nc_measurement["errored"],
            "distribution": {
                "mean": nc_measurement["distribution"]["mean"],
                "stddev": nc_measurement["distribution"]["stddev"],
            },
            "yield": nc_measurement["yield"],
            "measurement_status": nc_measurement["status"],
        }
        if status == "not_detected":
            warnings.append(
                f"measurement '{name}': the negative control did not show "
                "the expected degradation -- its yield was not measurably "
                "worse than the primary campaign's at the stated "
                "confidence, so this campaign's power to detect a real "
                "regression is unverified"
            )

    statuses = [m["status"] for m in per_measurement.values()]
    if not statuses:
        overall_status = "not_provided"
    elif any(s in ("not_detected", "error") for s in statuses):
        overall_status = "not_detected"
    elif all(s == "missing" for s in statuses):
        overall_status = "missing"
    elif any(s == "missing" for s in statuses):
        overall_status = "partial"
    else:
        overall_status = "detected"

    overall = {
        "provided": True,
        "samples": negative_control_path,
        "status": overall_status,
    }
    return overall, per_measurement


def _negative_control_status(
    nc_measurement: dict[str, Any],
    primary_measurement: dict[str, Any],
) -> str:
    """``"detected"`` when the negative control demonstrably shows the
    expected degradation relative to the primary campaign, ``"not_detected"``
    otherwise.

    Deliberately **not** "did the negative control fail its own
    ``target_yield``": a negative control campaign is often much smaller
    than the primary one, so a small-N campaign can miss a `target_yield`
    claim on sample-size grounds alone, with no real degradation at all --
    that would flag a perfectly healthy negative control as "detected" for
    the wrong reason (a false positive that defeats the check's purpose).
    Instead, this compares the negative control's empirical yield directly
    against the *primary* campaign's: detection requires the negative
    control's confidence interval to sit strictly below the primary's, with
    no overlap -- a non-parametric, assumption-free demonstration of a real,
    resolved difference between "known good" and "known bad".
    """
    nc_ci = nc_measurement["yield"]["empirical"]["confidence_interval"]
    primary_ci = primary_measurement["yield"]["empirical"]["confidence_interval"]
    return "detected" if nc_ci["high"] < primary_ci["low"] else "not_detected"


def _analytic_cross_check(
    request_measurements: list[dict[str, Any]],
    name_to_analytic: dict[str, dict[str, Any]],
    primary_by_name: dict[str, dict[str, Any]],
    *,
    tolerance_sigma: float,
    warnings: list[str],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Compare the empirical distribution fit against a declared closed-form
    model (issue #817), for every measurement that declares one.

    Returns ``(run_level_summary, per_measurement)``. Appends a warning to
    ``warnings`` (in place) for a measurement whose empirical fit diverges
    from its analytic model by more than ``tolerance_sigma``.
    """
    per_measurement: dict[str, dict[str, Any]] = {}
    for entry in request_measurements:
        name = entry["name"]
        analytic = name_to_analytic.get(name)
        if analytic is None:
            per_measurement[name] = {"status": "not_provided"}
            continue

        dist = primary_by_name[name]["distribution"]
        empirical_mean = dist["mean"]
        empirical_stddev = dist["stddev"]
        analytic_mean = analytic["mean"]
        analytic_stddev = analytic["stddev"]
        delta_mean = empirical_mean - analytic_mean
        delta_stddev = empirical_stddev - analytic_stddev
        if analytic_stddev > 0:
            delta_mean_sigma = delta_mean / analytic_stddev
            delta_stddev_pct = delta_stddev / analytic_stddev
            mean_ok = abs(delta_mean_sigma) <= tolerance_sigma
            stddev_ok = abs(delta_stddev_pct) <= tolerance_sigma
        else:
            # A degenerate (zero-spread) analytic model admits no tolerance
            # band -- only an exact match is "consistent".
            delta_mean_sigma = None
            delta_stddev_pct = None
            mean_ok = delta_mean == 0.0
            stddev_ok = delta_stddev == 0.0
        status = "consistent" if (mean_ok and stddev_ok) else "discrepant"
        per_measurement[name] = {
            "status": status,
            "model": analytic.get("model"),
            "analytic": {"mean": analytic_mean, "stddev": analytic_stddev},
            "empirical": {"mean": empirical_mean, "stddev": empirical_stddev},
            "delta_mean": delta_mean,
            "delta_mean_sigma": delta_mean_sigma,
            "delta_stddev": delta_stddev,
            "delta_stddev_pct": delta_stddev_pct,
            "tolerance_sigma": tolerance_sigma,
        }
        if status == "discrepant":
            label = analytic.get("model") or "analytic model"
            warnings.append(
                f"measurement '{name}': empirical distribution diverges from "
                f"its {label} by more than {tolerance_sigma} sigma (mean "
                f"delta {delta_mean_sigma!r} sigma, stddev delta "
                f"{delta_stddev_pct!r} relative)"
            )

    statuses = [m["status"] for m in per_measurement.values()]
    checked = [s for s in statuses if s != "not_provided"]
    if not checked:
        overall_status = "not_provided"
    elif any(s == "discrepant" for s in checked):
        overall_status = "discrepant"
    else:
        overall_status = "consistent"

    return {
        "tolerance_sigma": tolerance_sigma,
        "status": overall_status,
    }, per_measurement


def _resolve(explicit: Any, from_spec: Any, fallback: Any) -> Any:
    """CLI flag beats the spec file, which beats the documented default."""
    if explicit is not None:
        return explicit
    if from_spec is not None:
        return from_spec
    return fallback
