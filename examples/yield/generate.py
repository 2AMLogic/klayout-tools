#!/usr/bin/env python3
"""Generate the `klt yield` worked-example fixtures (see `docs/cli/yield.md`).

Two input documents, one per shape `klt yield` accepts:

- `mc-samples.json` -- a plain **sample-set document**: the shortest way to
  hand `klt yield` a Monte Carlo draw that came from somewhere other than
  `klt sim`.
- `sim-report.json` -- a trimmed **`klt sim` Monte Carlo report**, the exact
  record format the canary blocks' MC harnesses already produce
  (`docs/cli/sim.md`'s "Monte Carlo sampling"). `klt yield` consumes it
  directly; there is no intermediate format.

Plus `spec-limits.json`, the spec-limits file that states what the design is
being held to.

**Synthetic on purpose, and deterministic.** The samples come from a fixed
seed through Python's own `random.gauss`, so the fixtures regenerate
byte-identically on any interpreter and `tests/test_yield.py` can assert real
numbers against them. The `vref` measurement is a bandgap-shaped reference
with a deliberate small positive mean offset, so its yield against the
asymmetric 1.15 V / 1.25 V window is high but genuinely below 100% -- the
interesting case, where the estimate, the interval, and the sample-size
verdict all say something. `iq_ua` is a one-sided (max-only) spec, which
exercises the infinite-limit path.

Run from the repo root:

    uv run python3 examples/yield/generate.py
"""

import json
import os
import random

_DIR = os.path.dirname(os.path.abspath(__file__))

#: Fixed so the fixtures are reproducible. Any seed works; this one is the
#: date the fixtures were first generated.
SEED = 20260812

N_SAMPLES = 300

#: (name, unit, mean, stddev) for each synthetic measurement.
SPECS = [
    ("vref", "V", 1.2035, 0.0165),
    ("iq_ua", "uA", 8.4, 0.55),
]


def draw() -> dict[str, list[float]]:
    rng = random.Random(SEED)
    # One independent stream per measurement, drawn in declaration order, so
    # adding a measurement later never perturbs an existing one's values.
    return {
        name: [round(rng.gauss(mean, stddev), 6) for _ in range(N_SAMPLES)]
        for name, _unit, mean, stddev in SPECS
    }


def write_sample_set(samples: dict[str, list[float]]) -> None:
    doc = {
        "measurements": [
            {"name": name, "unit": unit, "samples": samples[name]}
            for name, unit, _mean, _stddev in SPECS
        ]
    }
    _write("mc-samples.json", doc)


def write_spec_limits() -> None:
    doc = {
        "confidence": 0.95,
        "target_ci_halfwidth": 0.01,
        "measurements": {
            "vref": {"min": 1.15, "max": 1.25, "target_yield": 0.99},
            "iq_ua": {"max": 10.0, "target_yield": 0.99},
        },
    }
    _write("spec-limits.json", doc)


def write_sim_report(samples: dict[str, list[float]]) -> None:
    """A trimmed `klt sim --format json` Monte Carlo report over the same
    draw -- same numbers, the other input shape."""
    corners = [
        {
            "corner_id": f"tt/1.800V/27C/mc{i}",
            "process": "tt",
            "supply_v": {"vdd": 1.8},
            "temperature_c": 27,
            "status": "pass",
            "runtime_s": 0.05,
            "measurements": [
                {
                    "name": name,
                    "value": samples[name][i],
                    "unit": unit,
                    "status": "pass",
                    "margin": None,
                }
                for name, unit, _mean, _stddev in SPECS
            ],
            "diagnostics": [],
            "artifacts": {"log": None, "raw": None, "waveform": None, "deck": None},
            "monte_carlo": {
                "sample_index": i,
                "seed": SEED + i,
                "process_seed": 0,
                "mismatch_seed": SEED + i,
            },
        }
        for i in range(N_SAMPLES)
    ]
    doc = {
        "schema_version": 1,
        "netlist": "bandgap-tb.spice",
        "status": "pass",
        "corner_count": len(corners),
        "passed": len(corners),
        "failed": 0,
        "errored": 0,
        "environment": {
            "engine": "ngspice",
            "engine_version": "46",
            "monte_carlo": {"n": N_SAMPLES, "seed": SEED, "vary": "mismatch"},
        },
        "measurements": [
            {
                "name": "vref",
                "unit": "V",
                "limits": {"min": 1.15, "max": 1.25},
                "status": "pass",
            },
            {
                "name": "iq_ua",
                "unit": "uA",
                "limits": {"max": 10.0},
                "status": "pass",
            },
        ],
        "corners": corners,
    }
    _write_sim_report("sim-report.json", doc)


def _write_sim_report(name: str, doc: dict) -> None:
    """`json.dump(indent=2)` with one exception: each `corners[]` entry is
    written on a single line.

    A 300-sample report is 300 near-identical corner objects; fully indented
    that is ~12k lines of fixture, which buries every other file in the
    diff for no readability gain. One line per sample keeps the document
    skimmable and the diff proportional to what actually changed.
    """
    rest = {key: value for key, value in doc.items() if key != "corners"}
    head = json.dumps(rest, indent=2)
    assert head.endswith("\n}"), "unexpected json.dumps framing"
    body = ",\n".join(
        f"    {json.dumps(corner, separators=(', ', ': '))}"
        for corner in doc["corners"]
    )
    text = f'{head[:-2]},\n  "corners": [\n{body}\n  ]\n}}\n'
    path = os.path.join(_DIR, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    print(f"wrote {path}")


def _write(name: str, doc: object) -> None:
    path = os.path.join(_DIR, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=2)
        handle.write("\n")
    print(f"wrote {path}")


def main() -> None:
    samples = draw()
    write_sample_set(samples)
    write_spec_limits()
    write_sim_report(samples)


if __name__ == "__main__":
    main()
