#!/usr/bin/env python3
"""Bounded bisection driver: a sequential `klt sim --backend batch` campaign.

The search controller runs on the submitting host; every simulation runs on
the batch fleet. Each probe is ONE single-unit `klt sim` request (one process
corner, one temperature, one literal DC source value, no Monte Carlo) that
blocks until the fleet job's report is collected. The next probe's source
value is derived from the previous probe's measurement.

It finds the source value `v` in `[--lo, --hi]` at which the named
measurement crosses `--target`, to within `--tol`.

Assumptions (the caller owns them; nothing here can verify them):

* The measurement is MONOTONIC in the source value over the bracket, in the
  direction given by `--direction` (`increasing`: larger source value gives a
  larger measurement).
* The bracket is VALID: the crossing lies inside `[lo, hi]`. The endpoints are
  deliberately not probed (that would cost two extra fleet jobs); if the
  crossing is outside the bracket the search converges on the nearer edge.
  Probe the edges yourself first if you are unsure.

Paths: only the template's `netlist` is rebased onto the template's
directory (probe requests are written into `--workdir`). Every other path in
the template (`models.lib` without `models.pdk`, per-corner process libs, an
OSDI preload, an ngspice binary override, ...) must be absolute or
PDK-relative, or it will resolve against `--workdir` and break.

Stop conditions: tolerance reached (`converged`), `--max-probes` spent
(`exhausted`), or ANY unusable probe (`stopped`): malformed/error envelope,
unexpected corner count, error/inconclusive/not_checked/partial status, absent
or non-finite measurement, repeated job identity, poll timeout. After a stop
no further probe is submitted and a missing value is never replaced by zero.
A poll timeout does NOT prove the fleet job stopped; the job id is reported so
you can inspect it. No automatic retry or replacement job is started.

Exit codes: 0 converged, 1 invalid arguments, 2 stopped or exhausted.

Usage (see examples/sim-batch/README.md):

    python examples/sim-batch/adaptive-search.py \\
        --template my-batch.request.json --source vdd --measurement vout_v \\
        --target 0.9 --lo 0.8 --hi 1.8 --tol 0.01 --max-probes 12 \\
        --workdir probes/ --process tt --temperature 27
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: A runner takes an argv and returns (returncode, stdout, stderr). Injected
#: in tests; the default shells out to `klt`.
Runner = Callable[[list[str]], "tuple[int, str, str]"]

#: Report statuses whose measurements may be used. `fail` is usable: a
#: completed measurement missing a declared limit is a valid observation.
_USABLE_STATUS = {"pass", "fail"}


class ProbeError(Exception):
    """A probe result that must stop the campaign."""


def _usable(status: Any) -> bool:
    """True for a usable status string (an unhashable value is just unusable)."""
    return isinstance(status, str) and status in _USABLE_STATUS


def _default_runner(argv: list[str]) -> tuple[int, str, str]:
    done = subprocess.run(argv, capture_output=True, text=True, check=False)
    return done.returncode, done.stdout, done.stderr


def build_probe_request(
    template: dict[str, Any],
    *,
    source: str,
    value: float,
    process: str,
    temperature_c: float,
) -> dict[str, Any]:
    """The template with a single-unit grid: one process, one temperature,
    one literal DC source value; Monte Carlo and any other axes removed."""
    request = copy.deepcopy(template)
    request["corners"] = {
        "process": [process],
        "supply_v": {source: [value]},
        "temperature_c": [temperature_c],
    }
    request.pop("monte_carlo", None)
    request["backend"] = "batch"
    return request


def _field(container: dict[str, Any], key: str, kind: type, default: Any) -> Any:
    """`container[key]` checked to be a `kind`; `default` when absent/null.

    A present value of the wrong JSON type is a malformed report, so it raises
    ProbeError (never AttributeError/TypeError) and takes the stop path."""
    value = container.get(key)
    if value is None:
        return default
    if not isinstance(value, kind):
        raise ProbeError(f"{key!r} is {type(value).__name__}, not {kind.__name__}")
    return value


def _require_usable_envelope(report: Any) -> dict[str, Any]:
    """The report as a dict, or ProbeError for a malformed/error/unusable one."""
    if not isinstance(report, dict):
        raise ProbeError("report is not a JSON object")
    if "error" in report:
        raise ProbeError(f"error envelope: {report['error']}")
    if not _usable(report.get("status")):
        # error, inconclusive, not_checked, pass_partial (partial coverage)
        raise ProbeError(f"report status {report.get('status')!r} is not usable")
    return report


def _single_corner(report: dict[str, Any]) -> dict[str, Any]:
    """The report's only corner, which must be usable and error-free."""
    corners = report.get("corners")
    if not isinstance(corners, list) or len(corners) != 1:
        count = len(corners) if isinstance(corners, list) else None
        raise ProbeError(f"expected exactly one corner, got {count}")
    corner = corners[0]
    if not isinstance(corner, dict) or not _usable(corner.get("status")):
        status = corner.get("status") if isinstance(corner, dict) else None
        raise ProbeError(f"corner status {status!r} is not usable")
    for diag in _field(corner, "diagnostics", list, []):
        if isinstance(diag, dict) and diag.get("severity") == "error":
            raise ProbeError(f"corner diagnostic {diag.get('code')!r}")
    return corner


def _require_applied_value(corner: dict[str, Any], source: str, value: float) -> None:
    """The corner must have run `source` at exactly the requested value."""
    applied = _field(corner, "supply_v", dict, {}).get(source)
    if (
        isinstance(applied, bool)
        or not isinstance(applied, (int, float))
        or not math.isclose(applied, value, rel_tol=1e-9, abs_tol=1e-12)
    ):
        raise ProbeError(f"corner ran {source}={applied!r}, expected {value!r}")


def _named_measurement(corner: dict[str, Any], name: str) -> float:
    """The finite value of the one usable measurement called `name`."""
    matches = [
        m
        for m in _field(corner, "measurements", list, [])
        if isinstance(m, dict) and m.get("name") == name
    ]
    if len(matches) != 1:
        raise ProbeError(f"measurement {name!r} present {len(matches)} times")
    entry = matches[0]
    measured = entry.get("value")
    if (
        isinstance(measured, bool)
        or not isinstance(measured, (int, float))
        or not math.isfinite(measured)
    ):
        raise ProbeError(f"measurement {name!r} value {measured!r} unusable")
    if not _usable(entry.get("status")):
        raise ProbeError(f"measurement status {entry.get('status')!r} not usable")
    return float(measured)


def _job_id_field(report: dict[str, Any]) -> Any:
    """`environment.remote.job_id`; ProbeError if a container is malformed."""
    environment = _field(report, "environment", dict, {})
    return _field(environment, "remote", dict, {}).get("job_id")


def _remote_job_id(report: Any) -> Any:
    """Best-effort job id for the stop path: never raises, None if absent."""
    if not isinstance(report, dict):
        return None
    try:
        return _job_id_field(report)
    except ProbeError:
        return None


def _fresh_job_id(report: dict[str, Any], seen_jobs: set[str]) -> str:
    """The report's batch job id, which must be present and not seen before."""
    job_id = _job_id_field(report)
    if not isinstance(job_id, str) or not job_id:
        raise ProbeError("report carries no batch job id")
    if job_id in seen_jobs:
        raise ProbeError(f"job id {job_id} repeated; refusing a stale report")
    return job_id


def check_report(
    report: Any,
    *,
    source: str,
    value: float,
    measurement: str,
    seen_jobs: set[str],
) -> tuple[float, str]:
    """Validate one probe report; return (measurement value, job id).

    Raises ProbeError on anything that is not a trustworthy single-unit
    observation of `measurement` at `source == value`."""
    envelope = _require_usable_envelope(report)
    corner = _single_corner(envelope)
    _require_applied_value(corner, source, value)
    measured = _named_measurement(corner, measurement)
    return measured, _fresh_job_id(envelope, seen_jobs)


def run_probe(
    request: dict[str, Any],
    index: int,
    workdir: Path,
    runner: Runner,
    klt: list[str],
    record: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path, Path]:
    """Write the request, run `klt sim --backend batch`, keep stdout.

    The exit code is deliberately not gated on: 3 (a limit failed) can carry
    a valid observation. Only a non-JSON or error-envelope stdout stops.
    Once both files are written their paths go into `record` (if given), so
    they are retained even when stdout then fails to parse."""
    request_path = workdir / f"probe-{index:03d}.request.json"
    report_path = workdir / f"probe-{index:03d}.report.json"
    for path in (request_path, report_path):
        if path.exists():
            raise ProbeError(f"{path} already exists; use a fresh --workdir")
    request_path.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
    argv = [*klt, "sim", str(request_path), "--backend", "batch", "--format", "json"]
    returncode, stdout, stderr = runner(argv)
    report_path.write_text(stdout, encoding="utf-8")
    if record is not None:
        record.update(request=str(request_path), report=str(report_path))
    try:
        report = json.loads(stdout)
    except ValueError as exc:
        detail = stderr.strip() or "no stderr"
        raise ProbeError(f"exit {returncode}: stdout is not JSON ({detail})") from exc
    return report, request_path, report_path


def _validate_search_args(
    *,
    target: float,
    lo: float,
    hi: float,
    tol: float,
    max_probes: int,
    direction: str,
) -> None:
    """Raise ValueError for arguments that cannot define a bounded search."""
    values = (target, lo, hi, tol)
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
        raise ValueError("target, lo, hi and tol must be finite numbers")
    if not lo < hi:
        raise ValueError("invalid bracket: need lo < hi")
    if tol <= 0 or max_probes < 1:
        raise ValueError("tol must be > 0 and max-probes >= 1")
    if direction not in ("increasing", "decreasing"):
        raise ValueError("direction must be 'increasing' or 'decreasing'")


def bisect(
    template: dict[str, Any],
    *,
    source: str,
    measurement: str,
    target: float,
    lo: float,
    hi: float,
    tol: float,
    max_probes: int,
    workdir: Path,
    process: str,
    temperature_c: float,
    direction: str = "increasing",
    runner: Runner = _default_runner,
    klt: list[str] | None = None,
) -> dict[str, Any]:
    """Run the bounded search; return a JSON-able summary.

    `outcome` is `converged`, `exhausted` or `stopped`; `probes` retains each
    probe's input value, measurement, request/report paths and job id."""
    _validate_search_args(
        target=target,
        lo=lo,
        hi=hi,
        tol=tol,
        max_probes=max_probes,
        direction=direction,
    )
    klt = klt or ["klt"]
    workdir.mkdir(parents=True, exist_ok=True)

    summary: dict[str, Any] = {
        "outcome": "exhausted",
        "bracket": [lo, hi],
        "estimate": None,
        "reason": None,
        "probes": [],
    }
    seen_jobs: set[str] = set()
    for index in range(max_probes):
        if hi - lo <= tol:
            summary["outcome"] = "converged"
            break
        value = (lo + hi) / 2.0
        probe: dict[str, Any] = {"index": index, "value": value}
        summary["probes"].append(probe)
        request = build_probe_request(
            template,
            source=source,
            value=value,
            process=process,
            temperature_c=temperature_c,
        )
        report: Any = None
        try:
            report, _, _ = run_probe(request, index, workdir, runner, klt, probe)
            measured, job_id = check_report(
                report,
                source=source,
                value=value,
                measurement=measurement,
                seen_jobs=seen_jobs,
            )
        except ProbeError as exc:
            probe["job_id"] = _remote_job_id(report)
            summary.update(outcome="stopped", reason=str(exc))
            break
        seen_jobs.add(job_id)
        probe.update(measured=measured, job_id=job_id)
        above = measured >= target
        if above == (direction == "increasing"):
            hi = value
        else:
            lo = value
        summary["bracket"] = [lo, hi]
    else:
        if hi - lo <= tol:
            summary["outcome"] = "converged"
    if summary["outcome"] == "converged":
        summary["estimate"] = (lo + hi) / 2.0
    return summary


def main(argv: list[str] | None = None, runner: Runner = _default_runner) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--template", required=True, help="caller's batch request")
    parser.add_argument("--source", required=True, help="DC source name to set")
    parser.add_argument("--measurement", required=True, help="named measurement")
    parser.add_argument("--target", required=True, type=float)
    parser.add_argument("--lo", required=True, type=float)
    parser.add_argument("--hi", required=True, type=float)
    parser.add_argument("--tol", required=True, type=float)
    parser.add_argument("--max-probes", required=True, type=int)
    parser.add_argument("--workdir", required=True, help="fresh per-campaign dir")
    parser.add_argument("--process", required=True, help="e.g. tt")
    parser.add_argument("--temperature", required=True, type=float, help="deg C")
    parser.add_argument(
        "--direction", choices=("increasing", "decreasing"), default="increasing"
    )
    parser.add_argument("--klt", default="klt", help='klt command, e.g. "uv run klt"')
    args = parser.parse_args(argv)

    template_path = Path(args.template).resolve()
    try:
        template = json.loads(template_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"adaptive-search: cannot read template: {exc}", file=sys.stderr)
        return 1
    # Probe requests live in --workdir, so resolve the netlist now.
    if isinstance(template.get("netlist"), str):
        template["netlist"] = str(
            Path(os.path.join(template_path.parent, template["netlist"])).resolve()
        )
    try:
        summary = bisect(
            template,
            source=args.source,
            measurement=args.measurement,
            target=args.target,
            lo=args.lo,
            hi=args.hi,
            tol=args.tol,
            max_probes=args.max_probes,
            workdir=Path(args.workdir),
            process=args.process,
            temperature_c=args.temperature,
            direction=args.direction,
            runner=runner,
            klt=shlex.split(args.klt),
        )
    except ValueError as exc:
        print(f"adaptive-search: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    return 0 if summary["outcome"] == "converged" else 2


if __name__ == "__main__":
    sys.exit(main())
