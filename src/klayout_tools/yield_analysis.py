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

This module also backs ``klt yield-samples`` (:func:`derive_sample_set`,
issue #2563), which turns a nominal and a known-bad `klt sim` report into a
sample-set document carrying a ``negative_control``. It lives here, not in a
module of its own, so both inputs go through the same sim-report reader
``klt yield`` uses -- the draw-screening rules are never duplicated.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import math
import os
from typing import Any

from ._native import _load_native_extension
from .yield_provenance import (
    _attach_spec_rows,
    _provenance_from,
    _request_without_spec_rows,
    _spec_row_from,
    _spec_rows_for,
    nominal_provenance,
    spec_rows_from_limits,
)
from .yield_sampling_fields import _analytic_cross_check_from, _sampling_from

#: Mirrors ``native/yield/src/contract.rs``'s ``SCHEMA_VERSION`` -- kept in
#: sync manually, the same convention ``mom.py`` uses for the constants it
#: shares with its own crate.
SCHEMA_VERSION = 1

#: Mirrors ``native/yield/src/contract.rs``'s defaults. Repeated here so the
#: CLI can report the effective value even when the request omits it.
DEFAULT_CONFIDENCE = 0.95
DEFAULT_TARGET_CI_HALFWIDTH = 0.01
ABSOLUTE_MIN_SAMPLES = 2

#: Mirrors ``sim._INCONCLUSIVE_STATUS`` -- the single status `klt sim` grades
#: a draw it does not trust with, whichever of the two reasons produced it
#: (``options.fail_on_diagnostic``, issue #2492, or a plausibility-bound
#: violation, issue #2493; see that constant's own note on why they are one
#: token rather than two). Repeated here rather than imported so reading a
#: sim report never drags `klt sim`'s whole module in; a rename on either side
#: is caught by ``tests/test_yield.py``'s cross-module assertion (issue #2507).
_SIM_INCONCLUSIVE_STATUS = "inconclusive"


class YieldError(Exception):
    """Raised when ``klt yield`` cannot run: a bad/missing input document, a
    request the native extension rejects, a sample set too small to carry a
    confidence interval, or the native extension not being built.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- see ``docs/json-contract.md``.
    """


def _load_native() -> Any:
    return _load_native_extension(
        "klt_yield_native",
        YieldError,
        "native/yield/",
        "uv sync --group yield",
        docs_link="docs/cli/yield.md#building-the-native-extension",
    )


def _load_json(path: str, what: str) -> Any:
    if not os.path.exists(path):
        raise YieldError(f"{what} file not found: {path}")
    if os.path.isdir(path):
        raise YieldError(f"not a file: {path}")
    try:
        with open(path, "rb") as raw:
            gzipped = raw.read(2) == b"\x1f\x8b"
        opener = gzip.open if gzipped else open
        with opener(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError) as exc:
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
    _read_qualification_keys(raw, limits, f"{where}: measurement '{name}' limits.")
    # issue #1083: optional strict-inequality flags -- `exclusive_min` /
    # `exclusive_max` make the corresponding `min`/`max` a strict bound
    # (`>`/`<` instead of `>=`/`<=`). Booleans only; the native extension is
    # the single source of truth for the "flag with no matching value" error.
    for key in ("exclusive_min", "exclusive_max"):
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, bool):
            raise YieldError(
                f"{where}: measurement '{name}' limits.{key} must be a boolean"
            )
        limits[key] = value
    return limits


def _read_qualification_keys(
    raw: dict[str, Any], out: dict[str, Any], prefix: str
) -> None:
    """Validate the issue-#2873 keys ``target_sigma`` (a number ``> 0``) and
    ``min_converged_samples`` (a non-negative integer) found in ``raw`` and
    copy them into ``out``. Absent/``null`` keys are skipped, so a spec that
    declares neither is unchanged."""
    k = raw.get("target_sigma")
    if k is not None:
        if isinstance(k, bool) or not isinstance(k, (int, float)):
            raise YieldError(f"{prefix}target_sigma must be a number")
        if not math.isfinite(k) or k <= 0:
            raise YieldError(f"{prefix}target_sigma must be > 0 (got {k})")
        out["target_sigma"] = float(k)
    n = raw.get("min_converged_samples")
    if n is not None:
        if isinstance(n, bool) or not isinstance(n, int):
            raise YieldError(
                f"{prefix}min_converged_samples must be a non-negative integer"
            )
        if n < 0:
            raise YieldError(
                f"{prefix}min_converged_samples must be a non-negative integer "
                f"(got {n})"
            )
        out["min_converged_samples"] = n


def _apply_qualification_defaults(
    limits: dict[str, Any], spec_defaults: dict[str, Any]
) -> None:
    """Fill run-level defaults for the issue-#2873 qualification keys into one
    measurement's ``limits``; a per-measurement value wins, exactly like
    ``target_yield``."""
    for key in ("target_sigma", "min_converged_samples"):
        if key in spec_defaults and key not in limits:
            limits[key] = spec_defaults[key]


def _count_from(raw: Any, field: str, name: str, where: str) -> int:
    """Normalise a no-usable-value draw count (``errored``-style category),
    ``None`` -> 0, else a non-negative ``int``.

    Per-category semantics (denominator treatment is implemented in
    ``native/yield/src/estimate.rs``, not here):

    - ``failed_unmeasurable`` (issue #1095): draws that left the regime a
      measurement is defined in and produced no value. Unlike ``errored``,
      enters the empirical yield's numerator/denominator as a failure; still
      excluded from the distribution fit and Cp/Cpk.
    - ``censored`` (issue #2468): draws whose measurement precondition was
      never satisfied inside the analysis window. Gets ``errored``'s
      denominator treatment (excluded from numerator and denominator).
    - ``inconclusive`` (issue #2507): draws whose value exists but is not
      trustworthy. Caller-supplied on the sample-set path (no per-sample
      status channel; the sim-report path derives it from ``klt sim``
      status grading). Gets ``errored``'s denominator treatment.
    """
    if raw is None:
        return 0
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise YieldError(
            f"{where}: measurement '{name}' {field} must be a non-negative integer"
        )
    return raw


def _not_applicable_control_from(
    raw: dict[str, Any], name: str, where: str
) -> dict[str, Any]:
    """Validate a ``negative_control`` that declares ``not_applicable``.

    Issue #4523: a declared "control not applicable" disposition -- the
    seeded defect cannot degrade this measurement by construction. It
    carries a required reason and no samples; it never reaches the native
    core (see ``run_yield``).
    """
    if raw["not_applicable"] is not True:
        raise YieldError(
            f"{where}: measurement '{name}' negative_control.not_applicable "
            "must be true"
        )
    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise YieldError(
            f"{where}: measurement '{name}' negative_control declares "
            "not_applicable but no non-empty string 'reason'"
        )
    if "samples" in raw:
        raise YieldError(
            f"{where}: measurement '{name}' negative_control cannot declare "
            "both not_applicable and samples"
        )
    return {"not_applicable": True, "reason": reason.strip()}


def _negative_control_from(raw: Any, name: str, where: str) -> dict[str, Any] | None:
    """Normalise one measurement's ``negative_control`` object (issue #817's
    seeded, known-bad-variant self-check), or ``None`` when absent.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise YieldError(
            f"{where}: measurement '{name}' has a non-object negative_control"
        )
    if raw.get("not_applicable") is not None:
        return _not_applicable_control_from(raw, name, where)
    raw_samples = raw.get("samples")
    if not isinstance(raw_samples, list):
        raise YieldError(
            f"{where}: measurement '{name}' negative_control has no 'samples' array"
        )
    samples: list[float] = []
    errored = int(raw.get("errored") or 0)
    for value in raw_samples:
        if value is None:
            errored += 1
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise YieldError(
                f"{where}: measurement '{name}' negative_control has a non-numeric "
                "sample value"
            )
        samples.append(float(value))
    # Issue #1095: a draw that left the measurable regime entirely -- unlike
    # `errored`, this counts as a failure in the negative control's own
    # empirical yield rather than being excluded, so a deliberate defect
    # that drives every draw out of the regime still shows up as a detected
    # degradation.
    failed_unmeasurable = _count_from(
        raw.get("failed_unmeasurable"), "failed_unmeasurable", name, where
    )
    # Issue #2468: the negative control's own `censored` count -- excluded
    # from both the numerator and denominator of its own empirical yield,
    # exactly like `errored` (the opposite polarity from
    # `failed_unmeasurable` above).
    censored = _count_from(raw.get("censored"), "censored", name, where)
    description = raw.get("description")
    if description is not None and not isinstance(description, str):
        raise YieldError(
            f"{where}: measurement '{name}' negative_control.description must be a "
            "string"
        )
    return {
        "samples": samples,
        "errored": errored,
        "failed_unmeasurable": failed_unmeasurable,
        "censored": censored,
        "description": description,
    }


def _draw_from_mc_corners(
    mc_corners: list[dict[str, Any]], name: str
) -> tuple[list[float], int, int, list[str]]:
    """Pool one measurement's Monte Carlo draw out of a sim report's sampled
    corners, as ``(samples, errored, inconclusive, source_corners)``.

    Split out of :func:`_measurements_from_sim_report` so the per-sample
    screening below is a single readable unit rather than a third nesting
    level inside the rollup loop.
    """
    samples: list[float] = []
    errored = 0
    inconclusive = 0
    source_corners: list[str] = []
    for corner in mc_corners:
        # Strip the `/mc<sample_index>` suffix `klt sim` appends to a sampled
        # corner's id, so `source_corners` names the originating
        # (pre-sampling) corners the draw was pooled from.
        corner_id = corner.get("corner_id")
        if isinstance(corner_id, str):
            origin = corner_id.rsplit("/mc", 1)[0]
            if origin not in source_corners:
                source_corners.append(origin)
        for m in corner.get("measurements") or []:
            if not isinstance(m, dict) or m.get("name") != name:
                continue
            if _is_distrusted(corner, m):
                inconclusive += 1
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
    return samples, errored, inconclusive, source_corners


def _is_distrusted(corner: dict[str, Any], measurement: dict[str, Any]) -> bool:
    """Whether `klt sim` declined to vouch for this sample's value (issue
    #2507) -- in which case it is set aside *before* it reaches this module's
    population, mirroring what `klt sim`'s own Monte Carlo rollup already does
    to the same draw (``sim._monte_carlo_rollup._stats``). An untrusted number
    must not be allowed to move the mean/sigma, the Cpk, or the yield fraction
    a spec verdict rests on.

    Two levels need screening, because `klt sim` grades the two reasons at
    different levels: ``options.fail_on_diagnostic`` (issue #2492)
    disqualifies the sample's whole *corner* -- its measurement can still read
    ``pass``/``fail``, having been graded against ``limits`` before the corner
    was disqualified -- while a plausibility-bound violation (issue #2493) is
    graded onto the *measurement* itself. Neither is caught by a
    ``value is None`` filter, because a distrusted sample has a real,
    non-null value; that is exactly what makes it dangerous to the statistics.
    A draw disqualified at both levels is one excluded sample, not two.
    """
    return (
        corner.get("status") == _SIM_INCONCLUSIVE_STATUS
        or measurement.get("status") == _SIM_INCONCLUSIVE_STATUS
    )


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
        samples, errored, inconclusive, source_corners = _draw_from_mc_corners(
            mc_corners, name
        )
        out.append(
            {
                "name": name,
                "unit": entry.get("unit"),
                "samples": samples,
                "errored": errored,
                # Issue #2507: unlike `failed_unmeasurable`/`censored` below,
                # this one *is* derivable from the report -- `klt sim` states
                # the distrust verdict itself, on the corner and on the
                # measurement -- so it is counted here rather than taken from
                # the rollup entry, exactly like `errored` above.
                "inconclusive": inconclusive,
                # Issue #1095: like `negative_control`/`analytic_cross_check`/
                # `sampling` below, `failed_unmeasurable` is metadata about
                # the measurement rather than something derivable from a
                # corner's `value` -- a sim report's per-corner value shape
                # has no way to distinguish a tooling failure from a design
                # failure, so the caller supplies the count directly on the
                # rollup entry.
                "failed_unmeasurable": _count_from(
                    entry.get("failed_unmeasurable"),
                    "failed_unmeasurable",
                    name,
                    "sim report",
                ),
                # Issue #2468: same provenance story as `failed_unmeasurable`
                # above -- a sim report's per-corner value has no way to
                # distinguish an unmet measurement precondition from either
                # a tooling failure or a design failure, so the caller
                # supplies the count directly on the rollup entry.
                "censored": _count_from(
                    entry.get("censored"), "censored", name, "sim report"
                ),
                "limits": _limits_from(entry.get("limits"), name, "sim report"),
                "source_corners": source_corners,
                # A sim report's own corners are the nominal draw; a
                # negative-control variant (issue #817) is supplied
                # alongside it in the rollup entry, not derived from a
                # corner -- there is no "known-bad corner" convention here.
                "negative_control": _negative_control_from(
                    entry.get("negative_control"), name, "sim report"
                ),
                "analytic_cross_check": _analytic_cross_check_from(
                    entry.get("analytic_cross_check"), name, "sim report"
                ),
                # A variance-reduction sampling strategy (issue #907) is
                # metadata about the measurement's draw, supplied the same
                # way regardless of input shape -- same as the two blocks
                # above.
                "sampling": _sampling_from(entry.get("sampling"), name, "sim report"),
                "spec_row": _spec_row_from(entry.get("spec_row"), name, "sim report"),
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
                # Issue #2507: a draw whose value exists but is not
                # trustworthy. A plain sample-set document has no per-sample
                # status channel (the sim-report path derives this from `klt
                # sim`'s own corner/measurement `status` grading), so the
                # count is supplied directly -- the same shape the three
                # other no-usable-value categories already use.
                "inconclusive": _count_from(
                    entry.get("inconclusive"), "inconclusive", name, "sample set"
                ),
                # Issue #1095: a draw that failed the limit without producing
                # a value -- a design failure, not a measurement failure.
                "failed_unmeasurable": _count_from(
                    entry.get("failed_unmeasurable"),
                    "failed_unmeasurable",
                    name,
                    "sample set",
                ),
                # Issue #2468: a draw whose measurement precondition was
                # never satisfied -- nothing failed and no value-defining
                # event was violated, the row is simply undefined.
                "censored": _count_from(
                    entry.get("censored"), "censored", name, "sample set"
                ),
                "limits": _limits_from(entry.get("limits"), name, "sample set"),
                "source_corners": [str(c) for c in source_corners],
                "negative_control": _negative_control_from(
                    entry.get("negative_control"), name, "sample set"
                ),
                "analytic_cross_check": _analytic_cross_check_from(
                    entry.get("analytic_cross_check"), name, "sample set"
                ),
                "sampling": _sampling_from(entry.get("sampling"), name, "sample set"),
                "spec_row": _spec_row_from(entry.get("spec_row"), name, "sample set"),
            }
        )
    return out


def _source_netlist(raw: Any) -> str | None:
    """Extract the plain netlist path a `klt sim` report names, as a string.

    `klt sim`'s own ``netlist`` field changed shape in its ``schema_version``
    2 (issue #1261): a raw path string became a ``{"path", "scope"}`` object
    so an absolute path outside the invocation's repo is never echoed. `klt
    yield` echoes that field into its own ``source.netlist``, whose
    documented contract is a **string** (see ``docs/cli/yield.md``) -- so
    unwrap the object here rather than letting the upstream shape change
    ripple, unannounced, through a second command's contract:

    - schema-v2 sim report -> the resolved repo-relative path string, or
      ``None`` when the netlist lives outside the repo (``scope`` is
      ``external``/``absent``, whose ``path`` is ``null`` by construction).
    - schema-v1 sim report (or any hand-written sample document) -> the raw
      string, unchanged.
    - anything else (missing field, unexpected type) -> ``None``, matching
      the "no netlist recorded" case the sample-set branch already emits.
    """
    if isinstance(raw, dict):
        path = raw.get("path")
        return path if isinstance(path, str) else None
    return raw if isinstance(raw, str) else None


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
            "netlist": _source_netlist(doc.get("netlist")),
            "monte_carlo": mc,
            "provenance": _provenance_from(doc.get("provenance"), "sim report"),
        }
        return "sim-report", measurements, source

    if isinstance(doc.get("measurements"), list):
        measurements = _measurements_from_sample_set(doc)
        return (
            "sample-set",
            measurements,
            {
                "kind": "sample-set",
                "netlist": None,
                "monte_carlo": None,
                "provenance": _provenance_from(doc.get("provenance"), "sample set"),
            },
        )

    raise YieldError(
        f"samples '{path}' is neither a `klt sim` report (no 'corners' array) nor "
        "a sample-set document (no 'measurements' array) -- see docs/cli/yield.md"
    )


def _read_spec(
    path: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], dict[str, str]]:
    """Read a spec-limits file into ``(limits, run_defaults, spec_rows)``."""
    doc = _load_json(path, "limits")
    if not isinstance(doc, dict):
        raise YieldError(f"limits '{path}' must be a JSON object")

    defaults: dict[str, Any] = {}
    for key in ("confidence", "target_ci_halfwidth", "min_samples", "target_yield"):
        if doc.get(key) is not None:
            defaults[key] = doc[key]
    _read_qualification_keys(doc, defaults, f"limits '{path}': ")

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
    spec_rows = spec_rows_from_limits(raw, f"limits '{path}'")
    return limits, defaults, spec_rows


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def _strip_not_applicable(
    entry: dict[str, Any], not_applicable: dict[str, str]
) -> dict[str, Any]:
    """Record and drop a ``not_applicable`` negative control (issue #4523) so
    the native core never sees it; other entries pass through unchanged."""
    control = entry.get("negative_control")
    if control is not None and control.get("not_applicable"):
        not_applicable[entry["name"]] = control["reason"]
        return {**entry, "negative_control": None}
    return entry


def _patch_not_applicable(
    response: dict[str, Any], not_applicable: dict[str, str]
) -> None:
    """Write the declared ``not_applicable`` verdicts into the core response."""
    for m in response.get("measurements") or []:
        if m.get("name") in not_applicable:
            m["negative_control"] = {
                "verdict": "not_applicable",
                "reason": not_applicable[m["name"]],
            }


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
    spec_rows: dict[str, str] = {}
    if limits_path is not None:
        spec_limits, spec_defaults, spec_rows = _read_spec(limits_path)

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
    # Issue #4523: measurements that declared "negative control not
    # applicable" -- carried around the native core and patched into the report.
    not_applicable: dict[str, str] = {}
    for entry in selected:
        name = entry["name"]
        limits = dict(entry["limits"])
        # A spec-limits file wins over whatever the sample document carried:
        # it is the caller's explicit statement of the spec being verified.
        limits.update(spec_limits.get(name, {}))
        if run_target_yield is not None and "target_yield" not in limits:
            limits["target_yield"] = run_target_yield
        _apply_qualification_defaults(limits, spec_defaults)
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
        entry = _strip_not_applicable(entry, not_applicable)
        request_measurements.append({**entry, "limits": limits})

    if not request_measurements:
        raise YieldError(
            "no measurement has spec limits to estimate a yield against -- supply "
            "them via --limits (see docs/cli/yield.md)"
        )
    row_by_name = _spec_rows_for(request_measurements, spec_rows)

    request: dict[str, Any] = {
        "confidence": _resolve(
            confidence, spec_defaults.get("confidence"), DEFAULT_CONFIDENCE
        ),
        "target_ci_halfwidth": _resolve(
            target_ci_halfwidth,
            spec_defaults.get("target_ci_halfwidth"),
            DEFAULT_TARGET_CI_HALFWIDTH,
        ),
        "measurements": _request_without_spec_rows(request_measurements),
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
    _patch_not_applicable(response, not_applicable)

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
    _attach_spec_rows(payload, row_by_name)
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


# --------------------------------------------------------------------------- #
# Sample-set derivation (`klt yield-samples`, issue #2563)
# --------------------------------------------------------------------------- #

#: ``klt yield-samples``' own payload version -- versioned per command, as
#: ``docs/json-contract.md`` requires, independently of ``SCHEMA_VERSION``
#: above (which mirrors the native crate's ``klt yield`` report shape).
SAMPLE_SET_SCHEMA_VERSION = 1


def _load_sim_report_bytes(path: str, role: str) -> tuple[dict[str, Any], str]:
    """Read one ``klt sim`` report for derivation, as ``(report, content_hash)``.

    The bytes are read **once**: the same buffer is both hashed and parsed,
    so the recorded ``sha256:`` hash can never describe a different version
    of the file than the one the samples were taken from (a file rewritten
    between a hash pass and a parse pass would otherwise be misattributed).
    The file is opened read-only; nothing here ever writes to it.
    """
    if not os.path.exists(path):
        raise YieldError(f"{role} sim report not found: {path}")
    if os.path.isdir(path):
        raise YieldError(f"{role} sim report is not a file: {path}")
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        raise YieldError(f"could not read {role} sim report '{path}': {exc}") from exc
    content_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
    try:
        doc = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise YieldError(f"could not read {role} sim report '{path}': {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("corners"), list):
        raise YieldError(
            f"{role} '{path}' is not a `klt sim --format json` report (no "
            "'corners' array) -- `klt yield-samples` derives a sample set from "
            "two Monte Carlo sim reports only"
        )
    return doc, content_hash


def _sim_report_entries(
    report: dict[str, Any], path: str, role: str
) -> list[dict[str, Any]]:
    """Run `klt yield`'s own sim-report reader over one report -- the single
    place the draw-screening rules live -- naming the offending file on any
    error so a two-input command's diagnostics stay actionable."""
    try:
        return _measurements_from_sim_report(report)
    except YieldError as exc:
        raise YieldError(f"{role} '{path}': {exc}") from exc


def _draw_count(entry: dict[str, Any]) -> int:
    """Every draw a measurement entry accounts for, across all five
    categories (``docs/cli/yield.md``'s "the full draw is always accounted
    for" identity)."""
    return (
        len(entry["samples"])
        + entry["errored"]
        + entry["inconclusive"]
        + entry["failed_unmeasurable"]
        + entry["censored"]
    )


def _index_by_name(entries: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group rollup entries by name, keeping duplicates visible so the caller
    can reject an ambiguous match instead of silently taking the last one."""
    by_name: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        by_name.setdefault(entry["name"], []).append(entry)
    return by_name


def _selected_names(
    nominal_entries: list[dict[str, Any]],
    nominal_by_name: dict[str, list[dict[str, Any]]],
    measurements: list[str] | None,
    nominal_path: str,
) -> list[str]:
    """The nominal measurement names to derive, in output order: the
    ``--measurement`` order when given (first occurrence wins), else the
    nominal rollup's own order. An unknown requested name is an error."""
    source = (
        measurements
        if measurements is not None
        else [e["name"] for e in nominal_entries]
    )
    names = list(dict.fromkeys(source))
    missing = [n for n in names if n not in nominal_by_name]
    if missing:
        raise YieldError(
            f"no such measurement in nominal '{nominal_path}': "
            f"{', '.join(sorted(missing))} (available: "
            f"{', '.join(sorted(nominal_by_name)) or 'none'})"
        )
    return names


def _unique_match(
    matches: list[dict[str, Any]], name: str, path: str, role: str
) -> dict[str, Any]:
    if len(matches) > 1:
        raise YieldError(
            f"{role} '{path}' declares measurement '{name}' {len(matches)} times "
            "in its 'measurements' rollup -- names must be unique to match a "
            "negative control by name"
        )
    entry = matches[0]
    if _draw_count(entry) == 0:
        raise YieldError(
            f"{role} '{path}' has no Monte Carlo draws for measurement "
            f"'{name}' -- no sampled corner reports it"
        )
    return entry


def _matched_pair(
    name: str,
    nominal_by_name: dict[str, list[dict[str, Any]]],
    control_by_name: dict[str, list[dict[str, Any]]],
    nominal_path: str,
    control_path: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Match one nominal measurement to its companion by exact name,
    enforcing the uniqueness/unit/no-existing-control/non-empty rules."""
    nominal = _unique_match(nominal_by_name[name], name, nominal_path, "nominal")
    if nominal["negative_control"] is not None:
        raise YieldError(
            f"nominal '{nominal_path}' measurement '{name}' already declares a "
            "negative_control -- refusing to replace it silently; remove it "
            "from the report's rollup entry or exclude the measurement with "
            "--measurement"
        )
    if name not in control_by_name:
        raise YieldError(
            f"negative-control '{control_path}' has no measurement named "
            f"'{name}' (available: "
            f"{', '.join(sorted(control_by_name)) or 'none'}) -- measurements "
            "are matched by exact name; restrict the derivation with "
            "--measurement to skip one the control does not measure"
        )
    control = _unique_match(
        control_by_name[name], name, control_path, "negative-control"
    )
    if nominal["unit"] != control["unit"]:
        raise YieldError(
            f"measurement '{name}' unit mismatch: nominal '{nominal_path}' "
            f"reports {nominal['unit']!r}, negative-control '{control_path}' "
            f"reports {control['unit']!r} -- the control is graded against the "
            "nominal limits, so the units must agree"
        )
    return nominal, control


def _derived_entry(
    nominal: dict[str, Any], control: dict[str, Any], description: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One sample-set entry (the nominal draw, carrying the companion's draw
    as its ``negative_control``) plus its ``derivation`` disclosure."""
    # The negative-control schema has no `inconclusive` field; folding it
    # into `errored` gives a distrusted draw the same exclusion from both
    # numerator and denominator it gets everywhere else -- never counted as
    # a design failure. Both originals are disclosed below.
    control_errored = control["errored"] + control["inconclusive"]
    entry = {
        "name": nominal["name"],
        "unit": nominal["unit"],
        "samples": nominal["samples"],
        "errored": nominal["errored"],
        "inconclusive": nominal["inconclusive"],
        "failed_unmeasurable": nominal["failed_unmeasurable"],
        "censored": nominal["censored"],
        "limits": nominal["limits"],
        "source_corners": nominal["source_corners"],
        "negative_control": {
            "samples": control["samples"],
            "errored": control_errored,
            "failed_unmeasurable": control["failed_unmeasurable"],
            "censored": control["censored"],
            "description": description,
        },
        "analytic_cross_check": nominal["analytic_cross_check"],
        "sampling": nominal["sampling"],
    }
    if nominal.get("spec_row") is not None:
        entry["spec_row"] = nominal["spec_row"]
    disclosure = {
        "name": nominal["name"],
        "negative_control": {
            "source_corners": control["source_corners"],
            "errored": control["errored"],
            "inconclusive": control["inconclusive"],
            "errored_reported": control_errored,
        },
    }
    return entry, disclosure


def derive_sample_set(
    nominal_path: str,
    control_path: str,
    *,
    description: str | None = None,
    measurements: list[str] | None = None,
) -> dict[str, Any]:
    """Derive a sample-set document from two `klt sim` Monte Carlo reports:
    the nominal campaign, and a seeded known-bad variant whose draw becomes
    each selected measurement's ``negative_control`` (issue #2563).

    The returned dict is ``klt yield-samples``' flat JSON payload: a
    sample-set document (``docs/cli/yield.md``'s "Sample-set document") that
    ``klt yield`` reads unchanged, plus ``schema_version`` and an additive
    ``derivation`` audit block the sample-set reader ignores.

    Rules (see ``docs/cli/yield.md``'s "Deriving a sample set from two `klt
    sim` reports"):

    - Both inputs go through :func:`_measurements_from_sim_report`, the same
      reader ``klt yield`` applies to a sim report directly, so which corners
      count as draws, how a ``null`` maps to ``errored`` and how
      ``inconclusive`` is screened are decided in exactly one place.
    - Measurements match by exact name, never by position. Every selected
      nominal measurement needs exactly one companion measurement with the
      same ``unit`` (both absent counts as equal); extra companion
      measurements are ignored.
    - A selected nominal measurement that already declares a
      ``negative_control`` is rejected rather than silently replaced.
    - The negative control has no ``inconclusive`` field, so the companion's
      screened ``inconclusive`` count is **added to** its ``errored`` (the
      same denominator treatment), and both original counts are disclosed in
      ``derivation``. Companion limits are never used: a negative control is
      always graded against the nominal measurement's limits.

    Raises :class:`YieldError` for anything that makes the derivation
    unrunnable. Never writes to either input.
    """
    nominal_report, nominal_hash = _load_sim_report_bytes(nominal_path, "nominal")
    control_report, control_hash = _load_sim_report_bytes(
        control_path, "negative-control"
    )
    nominal_entries = _sim_report_entries(nominal_report, nominal_path, "nominal")
    control_entries = _sim_report_entries(
        control_report, control_path, "negative-control"
    )

    nominal_by_name = _index_by_name(nominal_entries)
    control_by_name = _index_by_name(control_entries)
    selected_names = _selected_names(
        nominal_entries, nominal_by_name, measurements, nominal_path
    )

    out_measurements: list[dict[str, Any]] = []
    disclosures: list[dict[str, Any]] = []
    for name in selected_names:
        nominal, control = _matched_pair(
            name, nominal_by_name, control_by_name, nominal_path, control_path
        )
        entry, disclosure = _derived_entry(nominal, control, description)
        out_measurements.append(entry)
        disclosures.append(disclosure)

    prov = nominal_provenance(nominal_report)
    doc: dict[str, Any] = {"schema_version": SAMPLE_SET_SCHEMA_VERSION}
    if prov:
        doc["provenance"] = prov
    return {
        **doc,
        "measurements": out_measurements,
        "derivation": {
            "nominal": {"path": nominal_path, "content_hash": nominal_hash},
            "negative_control": {
                "path": control_path,
                "content_hash": control_hash,
                "description": description,
            },
            "measurements": disclosures,
        },
    }


def _read_csv_bytes(csv_path: str) -> tuple[str, str]:
    """Read the CSV once, as ``(text, content_hash)``; the same bytes are
    hashed and parsed. A UTF-8 BOM is dropped."""
    if not os.path.exists(csv_path):
        raise YieldError(f"CSV not found: {csv_path}")
    if os.path.isdir(csv_path):
        raise YieldError(f"CSV is not a file: {csv_path}")
    try:
        with open(csv_path, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise YieldError(f"could not read CSV '{csv_path}': {exc}") from exc
    return text, "sha256:" + hashlib.sha256(raw).hexdigest()


def _csv_reader(text: str, csv_path: str, wanted: list[str]) -> csv.DictReader:
    reader = csv.DictReader(io.StringIO(text, newline=""))
    header = reader.fieldnames
    if not header:
        raise YieldError(f"CSV '{csv_path}' is empty (no header row)")
    missing = [c for c in wanted if c not in header]
    if missing:
        raise YieldError(
            f"CSV '{csv_path}' has no column(s) {', '.join(missing)}; "
            f"header is: {', '.join(header)}"
        )
    return reader


def _csv_row_group(
    row: dict[str, Any], where: str, group_by: str, draw_col: str | None, seen: set
) -> str:
    """The row's group key; rejects a missing group or a repeated draw."""
    if None in row:
        raise YieldError(f"{where} has more cells than the header")
    group = (row[group_by] or "").strip()
    if not group:
        raise YieldError(f"{where}: empty '{group_by}'")
    if draw_col is not None:
        key = (group, (row[draw_col] or "").strip())
        if key in seen:
            raise YieldError(f"{where}: duplicate draw '{key[1]}' in group '{group}'")
        seen.add(key)
    return group


def _csv_row_sample(
    row: dict[str, Any], value: str, status_col: str | None, pass_value: str | None
) -> float | None:
    """The row's finite sample, or ``None`` when it counts ``errored``."""
    if status_col is not None and (row[status_col] or "").strip() != pass_value:
        return None
    try:
        number = float((row[value] or "").strip())
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def derive_sample_set_from_csv(
    csv_path: str,
    *,
    group_by: str,
    value: str,
    status_col: str | None = None,
    pass_value: str | None = None,
    draw_col: str | None = None,
    unit: str | None = None,
    limits: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Derive a sample-set document from a long-format per-draw Monte Carlo
    CSV (issue #2931): one row per draw, one measurement per distinct
    ``group_by`` value.

    The result has the same shape as :func:`derive_sample_set`'s -- a
    sample-set document ``klt yield`` reads unchanged, plus ``schema_version``
    and an additive ``derivation`` block -- so there is no parallel path into
    the estimator.

    Rules (see ``docs/cli/yield.md``'s "Deriving a sample set from a
    per-draw CSV"):

    - The file is read once; the same bytes are hashed and parsed. A UTF-8
      BOM and CRLF line endings are accepted.
    - With ``status_col``, a row whose status differs from ``pass_value``
      is counted ``errored`` and its ``value`` cell is not read.
    - A passing row whose value cell is empty, non-numeric or non-finite is
      also counted ``errored`` -- never dropped, never a sample.
    - Samples keep file order; the grading counts do not depend on it. With
      ``draw_col``, a repeated (group, draw) pair is rejected.
    - A group with no usable sample is an error, not an empty measurement.
    """
    if (status_col is None) != (pass_value is None):
        raise YieldError("--status-col and --pass-value must be given together")
    text, content_hash = _read_csv_bytes(csv_path)
    wanted = [group_by, value] + [c for c in (status_col, draw_col) if c]
    reader = _csv_reader(text, csv_path, wanted)

    groups: dict[str, dict[str, Any]] = {}
    seen: set[tuple[str, str]] = set()
    rows = 0
    try:
        for row in reader:
            rows += 1
            where = f"CSV '{csv_path}' row {rows + 1}"
            group = _csv_row_group(row, where, group_by, draw_col, seen)
            slot = groups.setdefault(group, {"samples": [], "errored": 0, "rows": 0})
            slot["rows"] += 1
            sample = _csv_row_sample(row, value, status_col, pass_value)
            if sample is None:
                slot["errored"] += 1
            else:
                slot["samples"].append(sample)
    except csv.Error as exc:
        raise YieldError(f"could not parse CSV '{csv_path}': {exc}") from exc
    if not groups:
        raise YieldError(f"CSV '{csv_path}' has a header but no data rows")

    out_measurements: list[dict[str, Any]] = []
    for name, slot in groups.items():
        if not slot["samples"]:
            raise YieldError(
                f"group '{name}' in '{csv_path}' has no usable draws "
                f"({slot['errored']} errored of {slot['rows']} rows)"
            )
        out_measurements.append(
            {
                "name": name,
                "unit": unit,
                "samples": slot["samples"],
                "errored": slot["errored"],
                "limits": dict(limits) if limits else None,
            }
        )
    return {
        "schema_version": SAMPLE_SET_SCHEMA_VERSION,
        "measurements": out_measurements,
        "derivation": {
            "csv": {
                "path": csv_path,
                "content_hash": content_hash,
                "rows": rows,
                "group_by": group_by,
                "value": value,
                "status_col": status_col,
                "pass_value": pass_value,
                "draw_col": draw_col,
            },
        },
    }
