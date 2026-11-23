"""Same-named rawfile columns in the noise contributor reduction (#6978).

ngspice's variable table may carry two columns under one vector name (the
reporter's PSP103 OSDI deck). A name-keyed reduction resolves both leaves to
the *last* column -- its power counted twice, the first column's never read
(the reporter's 195.83% power sum). The reduction is keyed by rawfile column
index instead; these synthetic flat-spectrum fixtures guard that. The leaf
vocabulary being classified is covered by ``test_sim_noise_leaves.py``, the
general ranking by ``test_sim_noise_contributors.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from klayout_tools import sim_noise, sim_noise_mechanism

#: A linear 3-point sweep, for the flat-spectrum rawfiles below.
LIN = {"kind": "noise", "args": "v(out) V1 lin 3 1 3 1"}

#: Two columns under one name; the plot's own total squares with both read
#: (3.0**2 + 4.0**2 == 5.0**2).
DUP_NAMES = [
    "frequency",
    "onoise_x1",  # device total
    "onoise_x1_1overf",  # flicker, 3.0 -- first column
    "onoise_x1_1overf",  # flicker, 4.0 -- same name, distinct column
    "onoise_spectrum",
]
DUP_ROW = [None, 5.0, 3.0, 4.0, 5.0]


def _rawfile(tmp_path: Path, names: list[str], row: list[float | None]) -> None:
    """A minimal ngspice ASCII rawfile of a noise spectral-density plot, one
    flat row of contributor values at each of three frequencies."""
    body = [
        "Title: synthetic",
        "Plotname: Noise Spectral Density Curves",
        "Flags: real",
        f"No. Variables: {len(names)}",
        "No. Points: 3",
        "Variables:",
    ]
    for index, name in enumerate(names):
        body.append(f"\t{index}\t{name}\tvoltage-density")
    body.append("Values:")
    for point, frequency in enumerate((1.0, 2.0, 3.0)):
        body.append(f" {point}\t{frequency:.15e}")
        body.extend(f"\t{value:.15e}" for value in row[1:])
    (tmp_path / "noise_mechanism.raw").write_text("\n".join(body) + "\n")


def _outcome(tmp_path, names, row, **noise):
    spec = {"name": "vn", "noise": {"quantity": "onoise", "top_n": 1000, **noise}}
    if "at_hz" not in noise and "band_hz" not in noise:
        spec["noise"]["at_hz"] = 2.0
    _rawfile(tmp_path, names, row)
    (outcome,) = sim_noise_mechanism.extract_corner(
        sim_noise.lower_measurements([spec], LIN), str(tmp_path), LIN
    ).values()
    assert outcome.error is None, outcome.error
    return outcome


def test_same_named_columns_are_both_read_and_close_in_power(tmp_path):
    """Not one column twice: each column is a leaf, so the ranked list is
    the plot's own decomposition (the reporter's 195.83% was this, broken)."""
    ranked = _outcome(tmp_path, DUP_NAMES, DUP_ROW).contributors
    assert [(c["name"], c["value"]) for c in ranked] == [
        ("x1_1overf", 4.0),
        ("x1_1overf", 3.0),
    ]
    assert sum(c["value"] ** 2 for c in ranked) == pytest.approx(5.0**2)


def test_same_named_columns_leave_no_mechanism_residual(tmp_path):
    outcome = _outcome(tmp_path, DUP_NAMES, DUP_ROW, mechanism="flicker")
    assert outcome.value == pytest.approx(5.0)  # sqrt(3.0**2 + 4.0**2)
    assert outcome.total == pytest.approx(5.0)
    assert outcome.residual_relative == pytest.approx(0.0, abs=1e-12)
    assert not outcome.incomplete


def test_duplicate_device_total_columns_stay_dropped(tmp_path):
    names = [
        "frequency",
        "onoise_r1",
        "onoise_r1",  # a device total under a repeated name too
        "onoise_r1_thermal",
        "onoise_spectrum",
    ]
    outcome = _outcome(tmp_path, names, [None, 5.0, 5.0, 2.0, 2.0])
    assert [c["name"] for c in outcome.contributors] == ["r1_thermal"]


def test_a_zero_column_among_same_named_leaves_is_omitted_alone(tmp_path):
    names = [
        "frequency",
        "onoise_r1",
        "onoise_r1_thermal",  # carries power
        "onoise_r1_thermal",  # identically zero -- omitted per column
        "onoise_spectrum",
    ]
    outcome = _outcome(tmp_path, names, [None, 2.0, 2.0, 0.0, 2.0])
    assert [(c["name"], c["value"]) for c in outcome.contributors] == [
        ("r1_thermal", 2.0)
    ]
