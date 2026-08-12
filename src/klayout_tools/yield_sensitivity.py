"""Sensitivity ranking: which device/process parameters drive a completed
Monte Carlo campaign's output spread (``klt yield-sensitivity``, issue #923,
Phase 3 of the statistical/yield epic #710).

Turns a **sensitivity sample document** -- a completed campaign's per-sample
device/process parameter draws paired with the resulting output-metric
value -- into a ranked list of contributing parameters, each carrying a
stated contribution metric (a standardized regression coefficient when the
sample count supports it, else a Pearson-correlation fallback) plus Pearson
and Spearman corroborating numbers. See ``docs/cli/yield-sensitivity.md``
for the full input/output schema.

Pure library: :func:`run_sensitivity` returns plain Python data (a
JSON-serialisable ``dict``) and never prints -- serialisation and
human-readable formatting live in ``cli/yield_sensitivity_cmd.py``, matching
every other ``klt`` verb.

The Rust/Python split mirrors ``yield_analysis.py``: everything numeric
(pairwise correlations, the standardized-regression solve, the ranking
itself) runs in the ``klt_yield_native`` extension (``native/yield/``, the
same crate `klt yield` uses -- Phase 3 of one epic, one crate). This
module's job is only to read the input document into the JSON request that
extension expects, and hand its response back out as the command's payload.

**A dedicated document, not a `klt sim` report.** Unlike `klt yield`, this
command does not read a `klt sim` Monte Carlo report directly: today's `klt
sim` MC response records each corner's RNG seeds
(`monte_carlo.{seed,process_seed,mismatch_seed}`), not the individual
per-device parameter values those seeds resolved to inside ngspice's
`AGAUSS`/`GAUSS` calls -- so there is no per-sample parameter draw to read
out of it yet. Wiring `klt sim` to also emit those draws is tracked
separately; until then, the caller (a `klt sim`-driving harness, or a
one-off analysis script) is responsible for recording each sample's
parameter values alongside its output value in the sensitivity sample
document this module reads.
"""

from __future__ import annotations

import json
import os
from typing import Any

#: Mirrors ``native/yield/src/contract.rs``'s ``SENSITIVITY_SCHEMA_VERSION``
#: -- kept in sync manually, the same convention ``yield_analysis.py`` uses
#: for the constants it shares with the same crate.
SCHEMA_VERSION = 1


class SensitivityError(Exception):
    """Raised when ``klt yield-sensitivity`` cannot run: a bad/missing input
    document, a request the native extension rejects, a sample set too small
    to rank, or the native extension not being built.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- see ``docs/json-contract.md``.
    """


def _load_native() -> Any:
    try:
        import klt_yield_native
    except ImportError as exc:
        raise SensitivityError(
            "the klt_yield_native extension is not installed -- from a repo "
            "checkout, run `maturin develop --release` inside native/yield/ "
            "(or `uv sync --group yield`); see "
            "docs/cli/yield-sensitivity.md#building-the-native-extension"
        ) from exc
    return klt_yield_native


def _load_json(path: str) -> Any:
    if not os.path.exists(path):
        raise SensitivityError(f"samples file not found: {path}")
    if os.path.isdir(path):
        raise SensitivityError(f"not a file: {path}")
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise SensitivityError(f"could not read samples '{path}': {exc}") from exc


def _parameters_from(raw: Any, name: str, index: int) -> dict[str, float]:
    if not isinstance(raw, dict) or not raw:
        raise SensitivityError(
            f"measurement '{name}': sample {index} has no non-empty 'parameters' object"
        )
    parameters: dict[str, float] = {}
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SensitivityError(
                f"measurement '{name}': sample {index}'s parameter '{key}' is "
                "not a number"
            )
        parameters[key] = float(value)
    return parameters


def _measurements_from_doc(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Read a sensitivity sample document into this module's request shape.

    ``{"measurements": [{"name", "unit"?, "samples": [{"parameters":
    {name: value, ...}, "output": value}, ...]}, ...]}`` -- see
    ``docs/cli/yield-sensitivity.md``'s "Input" section for the full schema.
    """
    entries = doc.get("measurements")
    if not isinstance(entries, list) or not entries:
        raise SensitivityError(
            "samples document has no non-empty 'measurements' array -- see "
            "docs/cli/yield-sensitivity.md's 'Input' section"
        )
    out: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise SensitivityError(
                "samples document has a malformed 'measurements' entry"
            )
        name = entry["name"]
        raw_samples = entry.get("samples")
        if not isinstance(raw_samples, list) or not raw_samples:
            raise SensitivityError(
                f"measurement '{name}' has no non-empty 'samples' array"
            )
        parameters: list[dict[str, float]] = []
        output: list[float] = []
        for i, sample in enumerate(raw_samples):
            if not isinstance(sample, dict):
                raise SensitivityError(
                    f"measurement '{name}': sample {i} is not an object"
                )
            value = sample.get("output")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SensitivityError(
                    f"measurement '{name}': sample {i} has no numeric 'output' value"
                )
            output.append(float(value))
            parameters.append(_parameters_from(sample.get("parameters"), name, i))
        out.append(
            {
                "name": name,
                "unit": entry.get("unit"),
                "parameters": parameters,
                "output": output,
            }
        )
    return out


def run_sensitivity(
    samples_path: str,
    *,
    measurements: list[str] | None = None,
) -> dict[str, Any]:
    """Rank each measurement's parameters by their contribution to its
    output variance.

    ``samples_path`` is a sensitivity sample document (see module
    docstring). ``measurements`` restricts the analysis to the named
    measurements -- an explicitly named measurement absent from the document
    is an error rather than a silent skip.

    Returns this command's JSON payload (see
    ``docs/cli/yield-sensitivity.md``); raises :class:`SensitivityError` for
    anything that makes the analysis unrunnable.
    """
    doc = _load_json(samples_path)
    if not isinstance(doc, dict):
        raise SensitivityError(f"samples '{samples_path}' must be a JSON object")

    entries = _measurements_from_doc(doc)
    by_name = {e["name"]: e for e in entries}
    if measurements is not None:
        missing = [n for n in measurements if n not in by_name]
        if missing:
            raise SensitivityError(
                f"no such measurement in {samples_path}: {', '.join(sorted(missing))} "
                f"(available: {', '.join(sorted(by_name)) or 'none'})"
            )
        selected = [by_name[n] for n in measurements]
    else:
        selected = entries

    request = {"measurements": selected}

    native = _load_native()
    try:
        response_json = native.analyze_sensitivity_json(json.dumps(request))
    except ValueError as exc:
        raise SensitivityError(str(exc)) from exc
    response = json.loads(response_json)

    payload: dict[str, Any] = {"samples": samples_path}
    payload.update(response)
    # The native core owns the payload's shape version; this module's
    # constant mirrors it (and is asserted against it below) so a crate-side
    # bump that Python never noticed cannot ship silently.
    if response.get("schema_version") != SCHEMA_VERSION:
        raise SensitivityError(
            "klt_yield_native reports sensitivity schema_version "
            f"{response.get('schema_version')}, but this klt build expects "
            f"{SCHEMA_VERSION} -- rebuild the extension (`uv sync --group yield`)"
        )
    payload["schema_version"] = SCHEMA_VERSION
    return payload
