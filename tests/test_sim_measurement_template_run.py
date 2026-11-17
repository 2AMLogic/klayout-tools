"""Run-level tests for `klt sim`'s measurement templates (issue #5731).

What an expanded request does when it actually runs: `limits`/`plausible_range`
and Monte Carlo grade each expanded per-case result through the existing
paths, an untemplated request's report stays free of the `case` field, the
batch backend is handed the already-expanded document (fake transport, no
fleet submit), and -- on a real `ngspice -b` -- a templated replicated deck is
equivalent to the hand-declared grid it replaces.

The expansion/refusal unit tests live in
`tests/test_sim_measurement_template.py`.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

import test_sim_batch as tb
from klayout_tools import sim

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

HAVE_NGSPICE = shutil.which("ngspice") is not None and (
    os.environ.get("KLT_SKIP_NGSPICE_TESTS") != "1"
)
_SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE, reason="ngspice is not installed on this machine"
)


def _write_request(tmp_path: Path, request: dict) -> Path:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return path


# --------------------------------------------------------------------------- #
# Grading: limits, plausible_range, Monte Carlo (engine-free stubs)
# --------------------------------------------------------------------------- #


def _stub_log_values(monkeypatch, values_per_call: dict[str, float]) -> None:
    """Stub ngspice so each corner run's log reports every expanded name."""

    def fake_run(cmd, capture_output, text, timeout, cwd=None, env=None):
        log_path = cmd[cmd.index("-o") + 1]
        with open(log_path, "w", encoding="utf-8") as handle:
            for name, value in values_per_call.items():
                handle.write(f"{name} = {value!r}\n")
        return tb.fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)


def _replicated_stub_request(tmp_path: Path, **overrides) -> Path:
    body = tmp_path / "body.spice"
    body.write_text("V1 in 0 1\nR1 in out 1k\nC1 out 0 1p\n")
    request: dict[str, object] = {
        "netlist": "body.spice",
        "analysis": {"kind": "tran", "args": "1n 10n"},
        "options": {"measurement_cases": ["case0_", "case1_"]},
        "measurements": [
            {
                "name": "tp_{case}",
                "spice": ".meas tran tp_{case} FIND v({case}out) AT=1u",
                "unit": "V",
            }
        ],
    }
    request.update(overrides)
    return _write_request(tmp_path, request)


def test_limits_grade_expanded_results_independently(tmp_path, monkeypatch):
    request = _replicated_stub_request(
        tmp_path,
        measurements=[
            {
                "name": "tp_{case}",
                "spice": ".meas tran tp_{case} FIND v({case}out) AT=1u",
                "unit": "V",
                "limits": {"min": 0.0, "max": 0.5},
            }
        ],
    )
    _stub_log_values(monkeypatch, {"tp_case0_": 0.4, "tp_case1_": 0.9})

    report = sim.run_sim(str(request))

    assert [e["name"] for e in report["measurements"]] == ["tp_case0_", "tp_case1_"]
    assert [e["status"] for e in report["measurements"]] == ["pass", "fail"]
    assert [e["case"] for e in report["measurements"]] == ["case0_", "case1_"]


def test_plausible_range_grades_expanded_results(tmp_path, monkeypatch):
    request = _replicated_stub_request(
        tmp_path,
        measurements=[
            {
                "name": "tp_{case}",
                "spice": ".meas tran tp_{case} FIND v({case}out) AT=1u",
                "unit": "V",
                "plausible_range": {"min": 0.0, "max": 0.5},
            }
        ],
    )
    _stub_log_values(monkeypatch, {"tp_case0_": 0.4, "tp_case1_": 0.9})

    report = sim.run_sim(str(request))

    implausible = [
        m for m in report["corners"][0]["measurements"] if m["status"] == "inconclusive"
    ]
    assert [m["name"] for m in implausible] == ["tp_case1_"]
    # Pre-existing corner semantics: one implausible value makes the whole
    # corner (hence both rollup entries) inconclusive.
    assert [
        e["name"] for e in report["measurements"] if e["status"] == "inconclusive"
    ] == [
        "tp_case0_",
        "tp_case1_",
    ]


def test_monte_carlo_rollup_covers_every_expanded_name(tmp_path, monkeypatch):
    request = _replicated_stub_request(
        tmp_path,
        monte_carlo={"n": 5, "seed": 1, "vary": "mismatch"},
    )
    samples = [3.0, 1.0, 5.0, 2.0, 4.0]
    state = {"index": 0}

    def fake_run(cmd, capture_output, text, timeout, cwd=None, env=None):
        log_path = cmd[cmd.index("-o") + 1]
        value = samples[state["index"]]
        state["index"] += 1
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(f"tp_case0_ = {value!r}\n")
            handle.write(f"tp_case1_ = {value * 2!r}\n")
        return tb.fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)

    report = sim.run_sim(str(request))

    assert len(report["corners"]) == 5  # one process per sample, N cases each
    by_name = {e["name"]: e for e in report["measurements"]}
    assert by_name["tp_case0_"]["monte_carlo"]["mean"] == pytest.approx(3.0)
    assert by_name["tp_case1_"]["monte_carlo"]["mean"] == pytest.approx(6.0)
    assert by_name["tp_case0_"]["case"] == "case0_"
    assert by_name["tp_case1_"]["case"] == "case1_"


# --------------------------------------------------------------------------- #
# Identity: a request declaring neither changes nothing
# --------------------------------------------------------------------------- #


def test_untemplated_request_keeps_report_entries_free_of_the_case_field(
    tmp_path, monkeypatch
):
    body = tmp_path / "body.spice"
    body.write_text("V1 in 0 1\nR1 in out 1k\nC1 out 0 1p\n")
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 10n"},
            "measurements": [{"name": "a", "spice": ".meas tran a FIND v(out) AT=1u"}],
        },
    )
    _stub_log_values(monkeypatch, {"a": 1.0})

    report = sim.run_sim(str(request))

    (entry,) = report["measurements"]
    assert "case" not in entry
    (corner,) = report["corners"]
    assert "case" not in corner["measurements"][0]


# --------------------------------------------------------------------------- #
# Batch backend: the forwarded document is already expanded (fake runner,
# no fleet submit)
# --------------------------------------------------------------------------- #


def test_batch_forwarded_document_carries_expanded_measurements(tmp_path, monkeypatch):
    runner = tb._FakeRunner()
    runner.default = tb.fake_completed(tb._status("done"))
    runner.outputs = {"report.json": tb._sim_report()}
    tb._install_fake_batch_transport(monkeypatch, runner=runner)
    uploaded = tb._capture_uploads(monkeypatch, runner)
    body = tmp_path / "body.spice"
    body.write_text(".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\nC1 out 0 1n\n")
    request = tb._batch_request(tmp_path)
    request["options"] = {"measurement_cases": ["c0_", "c1_"]}
    request["measurements"] = [
        {
            "name": "vdd_{case}",
            "spice": ".meas tran vdd_{case} AVG v({case}out) FROM=1n TO=900n",
        }
    ]

    sim.run_sim(str(_write_request(tmp_path, request)))

    forwarded = json.loads(uploaded["request.json"])
    assert [m["name"] for m in forwarded["measurements"]] == ["vdd_c0_", "vdd_c1_"]
    assert [m.get("case") for m in forwarded["measurements"]] == ["c0_", "c1_"]
    assert "measurement_cases" not in forwarded.get("options", {})


# --------------------------------------------------------------------------- #
# Integration: replicated deck, templated == hand-declared (real ngspice)
# --------------------------------------------------------------------------- #


_CASES = ["case0_", "case1_", "case2_"]

_REPLICATED_BODY = """\
* replicated 3-case RC bench: independent copies sharing only ideal sources
Vcase0_drive case0_in 0 PWL(0 0 100p 1)
Rcase0_r case0_in case0_out 1k
Ccase0_c case0_out 0 1p
.ic v(case0_out)=0.2
Vcase1_drive case1_in 0 PWL(0 0 100p 1)
Rcase1_r case1_in case1_out 1k
Ccase1_c case1_out 0 1p
.ic v(case1_out)=0.2
Vcase2_drive case2_in 0 PWL(0 0 100p 1)
Rcase2_r case2_in case2_out 1k
Ccase2_c case2_out 0 1p
.ic v(case2_out)=0.2
"""

_TEMPLATED_MEASUREMENTS = [
    {
        "name": "vstart_{case}",
        "spice": ".meas tran vstart_{case} FIND v({case}out) AT=2p",
        "unit": "V",
    },
    {
        "name": "vfinal_{case}",
        "spice": ".meas tran vfinal_{case} FIND v({case}out) AT=5n",
        "unit": "V",
    },
]


def _hand_declared_measurements() -> list[dict]:
    out = []
    for spec in _TEMPLATED_MEASUREMENTS:
        for case in _CASES:
            out.append(
                {
                    "name": spec["name"].replace("{case}", case),
                    "spice": spec["spice"].replace("{case}", case),
                    "unit": spec["unit"],
                }
            )
    return out


def _replicated_request(
    tmp_path: Path, measurements: list[dict], cases=None, keep_artifacts=False
) -> Path:
    body = tmp_path / "body.spice"
    body.write_text(_REPLICATED_BODY)
    request: dict[str, object] = {
        "netlist": "body.spice",
        "analysis": {"kind": "tran", "args": "1p 5n 0 5p"},
        "measurements": measurements,
    }
    options: dict[str, object] = {}
    if cases is not None:
        options["measurement_cases"] = cases
    if keep_artifacts:
        options["keep_artifacts"] = True
    if options:
        request["options"] = options
    return _write_request(tmp_path, request)


@_SKIP_NO_NGSPICE
def test_templated_replicated_deck_matches_the_hand_declared_grid(tmp_path):
    template_cases = [c for c in _CASES]
    templated = sim.run_sim(
        str(_replicated_request(tmp_path, _TEMPLATED_MEASUREMENTS, template_cases))
    )
    hand = sim.run_sim(
        str(_replicated_request(tmp_path, _hand_declared_measurements()))
    )

    def values(report):
        corner = report["corners"][0]
        return {m["name"]: m["value"] for m in corner["measurements"]}

    assert values(templated) == values(hand)
    # Every expanded result and rollup entry carries its `case` (spec-major
    # expansion order: each templated entry expands across all its cases).
    corner = templated["corners"][0]
    assert [m["case"] for m in corner["measurements"]] == [
        c for _ in range(2) for c in _CASES
    ]
    assert [e["case"] for e in templated["measurements"]] == [
        c for _ in range(2) for c in _CASES
    ]
    # The declared-once `limits` grade per expanded case.
    assert all(e["status"] == "pass" for e in templated["measurements"])


@_SKIP_NO_NGSPICE
def test_replicated_deck_body_level_ic_constrains_each_copy(tmp_path):
    """Each copy's own body-level `.ic` sets its start state (no `uic`):
    `vstart_<case>` reads the 0.2 V the card declares, while the solve runs
    the ordinary OP for everything else -- see the docs' measured note."""
    request = _replicated_request(
        tmp_path,
        [
            {
                "name": "vstart_{case}",
                "spice": ".meas tran vstart_{case} FIND v({case}out) AT=2p",
                "unit": "V",
            }
        ],
        _CASES,
    )
    report = sim.run_sim(str(request))
    for entry in report["corners"][0]["measurements"]:
        assert entry["value"] == pytest.approx(0.2, abs=1e-3)


@_SKIP_NO_NGSPICE
def test_templated_and_hand_declared_decks_emit_the_same_cards(tmp_path):
    reports = []
    for make_request in (
        lambda: _replicated_request(
            tmp_path, _TEMPLATED_MEASUREMENTS, _CASES, keep_artifacts=True
        ),
        lambda: _replicated_request(
            tmp_path, _hand_declared_measurements(), keep_artifacts=True
        ),
    ):
        report = sim.run_sim(str(make_request()))
        deck_path = report["corners"][0]["artifacts"]["deck"]
        assert deck_path and Path(deck_path).exists()
        reports.append(deck_path)
    cards = [
        [
            line
            for line in Path(p).read_text().splitlines()
            if line.startswith(".meas") or line.startswith(".ic")
        ]
        for p in reports
    ]
    assert cards[0] == cards[1]
    assert "{case}" not in Path(reports[0]).read_text()
