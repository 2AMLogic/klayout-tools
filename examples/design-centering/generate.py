#!/usr/bin/env python3
"""Generate the `klt design-centering` worked-example request (see
`docs/cli/design-centering.md`).

Assembles `request.json` from three pieces:

- `sensitivity` -- the **real** `klt yield-sensitivity` JSON output for
  `examples/yield-sensitivity/dominant-mismatch-samples.json` (issue #923's
  own known-dominant-parameter validation case: `vth_mismatch_m1` is
  injected with 10x the coefficient of the other three mismatch terms).
  Regenerating this piece requires the `klt_yield_native` extension to be
  built (`maturin develop --release` in `native/yield/`, or `uv sync --group
  yield`) -- the same requirement `klt yield-sensitivity` itself has. If the
  extension is not built, this script leaves the committed `request.json`'s
  `sensitivity` block untouched and only reports that it skipped
  regenerating it (never silently drops the field).
- `sized_device` -- `sized-device.json`, a hand-built, synthetic `klt size`
  topology-mode response (not an actual ngspice run -- see this directory's
  README.md), copied in verbatim.
- `parameter_map` -- `parameter-map.json`, the small
  `{mismatch_parameter: instance_name}` bridge this worked example uses,
  copied in verbatim.

Run from the repo root:

    uv run python3 examples/design-centering/generate.py
"""

import json
import os
import subprocess
import sys

_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_DIR))
# Relative to `_REPO_ROOT` (the subprocess below runs with cwd=_REPO_ROOT) so
# the embedded `sensitivity.samples` echo matches the path
# `docs/cli/yield-sensitivity.md`'s own worked example quotes, not an
# absolute path specific to this checkout.
_SAMPLES = os.path.join(
    "examples", "yield-sensitivity", "dominant-mismatch-samples.json"
)


def _run_yield_sensitivity() -> dict | None:
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "klayout_tools.cli",
                "yield-sensitivity",
                _SAMPLES,
                "--format",
                "json",
            ],
            cwd=_REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(
            "klt_yield_native is not built -- skipping regeneration of "
            "request.json's 'sensitivity' block (see this script's "
            f"docstring). stderr: {exc.stderr}\n"
        )
        return None
    return json.loads(result.stdout)


def main() -> None:
    sensitivity = _run_yield_sensitivity()

    request_path = os.path.join(_DIR, "request.json")
    if sensitivity is None:
        if not os.path.exists(request_path):
            raise SystemExit(
                "no committed request.json to fall back to, and "
                "klt_yield_native is not built -- cannot generate the "
                "'sensitivity' block"
            )
        with open(request_path, encoding="utf-8") as handle:
            existing = json.load(handle)
        sensitivity = existing["sensitivity"]

    with open(os.path.join(_DIR, "sized-device.json"), encoding="utf-8") as handle:
        sized_device = json.load(handle)
    with open(os.path.join(_DIR, "parameter-map.json"), encoding="utf-8") as handle:
        parameter_map = json.load(handle)

    request = {
        "sensitivity": sensitivity,
        "sized_device": sized_device,
        "parameter_map": parameter_map,
        "measurement": "offset_mv",
    }
    with open(request_path, "w", encoding="utf-8") as handle:
        json.dump(request, handle, indent=2)
        handle.write("\n")


if __name__ == "__main__":
    main()
