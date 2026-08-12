#!/usr/bin/env python3
"""Generate the `klt yield-sensitivity` worked-example fixture (see
`docs/cli/yield-sensitivity.md`).

A synthetic, seeded 200-sample campaign for a differential-pair input-offset
measurement (`offset_mv`), driven by four independent mismatch-term draws.
One term (`vth_mismatch_m1`) is injected with **10x** the coefficient of the
other three -- issue #923's own acceptance criterion: "a campaign where one
injected mismatch term is scaled 10x the others -- the ranking must
correctly surface it first". `tests/test_yield_sensitivity.py` asserts the
committed fixture's ranking does exactly that.

Synthetic on purpose, and deterministic: the draws come from a fixed seed
through Python's own `random.gauss`, so the fixture regenerates
byte-identically on any interpreter and the tests can assert real ranking
numbers against it -- the same reasoning `examples/yield/generate.py` uses.

Run from the repo root:

    uv run python3 examples/yield-sensitivity/generate.py
"""

import json
import os
import random

_DIR = os.path.dirname(os.path.abspath(__file__))

#: Fixed so the fixture is reproducible. Any seed works; this one is the
#: date the fixture was first generated.
SEED = 20260812

N_SAMPLES = 200

#: (parameter name, coefficient) -- `vth_mismatch_m1`'s coefficient is 10x
#: every other term's, the dominant-parameter case issue #923 asks for.
TERMS = [
    ("vth_mismatch_m1", 10.0),
    ("vth_mismatch_m2", 1.0),
    ("beta_mismatch_m1", 1.0),
    ("beta_mismatch_m2", -1.0),
]

#: Measurement noise stddev, in the same mV units as the terms below --
#: small relative to the dominant term's spread, so the injected ranking
#: survives it comfortably (the point of the fixture), while still being
#: nonzero (a perfectly noise-free linear map would make every method agree
#: trivially).
NOISE_STDDEV_MV = 0.15


def draw() -> dict:
    rng = random.Random(SEED)
    samples = []
    for _ in range(N_SAMPLES):
        # Independent unit-normal mismatch-term draws, in mV.
        parameters = {name: round(rng.gauss(0.0, 1.0), 6) for name, _coef in TERMS}
        offset_mv = sum(coef * parameters[name] for name, coef in TERMS)
        offset_mv += rng.gauss(0.0, NOISE_STDDEV_MV)
        samples.append({"parameters": parameters, "output": round(offset_mv, 6)})
    return samples


def write_samples(samples: list) -> None:
    doc = {
        "measurements": [
            {
                "name": "offset_mv",
                "unit": "mV",
                "samples": samples,
            }
        ]
    }
    path = os.path.join(_DIR, "dominant-mismatch-samples.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=2)
        handle.write("\n")
    print(f"wrote {path}")


def main() -> None:
    write_samples(draw())


if __name__ == "__main__":
    main()
