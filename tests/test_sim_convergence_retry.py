"""Tests for `klt sim`'s `options.retry_on_convergence_error` (issue #2973).

An opt-in per-unit retry: a unit that ends `status: error` purely from a
`nonconvergence` failure is re-run once (or `max_attempts` times) on the same
`CornerPoint` -- so `sample_index` and the Monte Carlo seeds are unchanged --
with tighter solver options on an extra `.options` card. Unit tests stub
`subprocess.run`; no real ngspice is needed.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import _paths as paths_module
from klayout_tools import sim

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


@pytest.fixture(autouse=True)
def _bare_name_ngspice_resolution(monkeypatch):
    real_which = shutil.which

    def fake_which(cmd, *args, **kwargs):
        if cmd == "ngspice":
            return cmd
        return real_which(cmd, *args, **kwargs)

    monkeypatch.setattr(paths_module.shutil, "which", fake_which)


def _request(tmp_path: Path, options: dict | None, *, n: int = 2) -> Path:
    (tmp_path / "body.spice").write_text("Vdd vdd 0 DC 1\nR1 vdd out 1k\nC1 out 0 1n\n")
    request = {
        "netlist": "body.spice",
        "analysis": {"kind": "op", "args": ""},
        "monte_carlo": {"n": n, "seed": 7, "vary": "mismatch"},
        "measurements": [{"name": "vout_op", "expr": "v(out)", "unit": "V"}],
    }
    if options is not None:
        request["options"] = options
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return path


def _stub(monkeypatch, *, fail_first: dict[int, str], decks: list[str]):
    """Sample ``i`` logs ``fail_first[i]`` on its first attempt (unless the
    deck already carries the retry card); every other run succeeds."""
    attempts: dict[int, int] = {}

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        deck = Path(cmd[2]).read_text()
        decks.append(deck)
        index = int(re.search(r"mc_sample_index=(\d+)", deck).group(1))
        attempts[index] = attempts.get(index, 0) + 1
        log = Path(cmd[cmd.index("-o") + 1])
        message = fail_first.get(index)
        retried = ".options reltol=0.003" in deck
        if message is not None and (attempts[index] == 1 or "always" in message):
            log.write_text(f"{message.replace('always:', '')}\n")
        else:
            log.write_text(f"No. of Data Rows : 1\nvout_op = {1.0 + index:.6e}\n")
        assert retried == (attempts[index] > 1)
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)
    return attempts


def test_without_option_nothing_is_retried(tmp_path, monkeypatch):
    decks: list[str] = []
    attempts = _stub(
        monkeypatch, fail_first={1: "doAnalyses: Timestep too small"}, decks=decks
    )
    report = sim.run_sim(str(_request(tmp_path, None)))
    assert attempts[1] == 1
    assert "retry_on_convergence_error" not in report["environment"]
    assert not any(".options reltol" in d for d in decks)
    assert not any(
        d["code"] == "convergence_retry"
        for c in report["corners"]
        for d in c["diagnostics"]
    )


def test_nonconvergence_unit_is_retried_with_same_seeds(tmp_path, monkeypatch):
    decks: list[str] = []
    attempts = _stub(
        monkeypatch, fail_first={1: "Error: time step too small"}, decks=decks
    )
    baseline = sim.run_sim(str(_request(tmp_path, None)))
    unit = next(c for c in baseline["corners"] if c["monte_carlo"]["sample_index"] == 1)
    assert unit["status"] == "error"

    attempts.clear()
    decks.clear()
    report = sim.run_sim(str(_request(tmp_path, {"retry_on_convergence_error": True})))

    assert attempts == {0: 1, 1: 2}
    retried = next(
        c for c in report["corners"] if c["monte_carlo"]["sample_index"] == 1
    )
    assert retried["status"] == "pass"
    assert retried["monte_carlo"] == unit["monte_carlo"]
    (note,) = [d for d in retried["diagnostics"] if d["code"] == "convergence_retry"]
    assert "reltol=0.003 method=gear" in note["message"]
    assert "recovered" in note["message"]
    assert report["environment"]["retry_on_convergence_error"] == {
        "max_attempts": 1,
        "reltol": 0.003,
        "method": "gear",
    }
    # The retried deck carries the same seed card as the first attempt.
    seeds = {
        re.search(r"\.options seed=\d+", d).group(0)
        for d in decks
        if "mc_sample_index=1" in d
    }
    assert len(seeds) == 1


def test_unit_still_failing_stays_error_with_retry_recorded(tmp_path, monkeypatch):
    decks: list[str] = []
    _stub(monkeypatch, fail_first={0: "always:Error: time step too small"}, decks=decks)
    report = sim.run_sim(
        str(_request(tmp_path, {"retry_on_convergence_error": {"max_attempts": 2}}))
    )
    unit = next(c for c in report["corners"] if c["monte_carlo"]["sample_index"] == 0)
    assert unit["status"] == "error"
    (note,) = [d for d in unit["diagnostics"] if d["code"] == "convergence_retry"]
    assert "retry 2/2" in note["message"]
    assert "still failed" in note["message"]
    assert sum(".options reltol=" in d for d in decks if "mc_sample_index=0" in d) == 2


def test_non_convergence_errors_are_never_retried(tmp_path, monkeypatch):
    decks: list[str] = []
    attempts = _stub(
        monkeypatch,
        fail_first={0: "Warning: singular matrix", 1: "Error: no convergence"},
        decks=decks,
    )
    # Sample 0: singular_matrix only -> measurement missing, different class.
    sim.run_sim(str(_request(tmp_path, {"retry_on_convergence_error": True})))
    assert attempts[0] == 1
    assert attempts[1] == 2


@pytest.mark.parametrize(
    "value, match",
    [
        ("yes", "boolean or an object"),
        ({"bogus": 1}, "unknown key"),
        ({"max_attempts": 0}, "max_attempts"),
        ({"max_attempts": True}, "max_attempts"),
        ({"reltol": 0}, "reltol"),
        ({"reltol": 1.5}, "reltol"),
        ({"method": "euler"}, "method"),
    ],
)
def test_option_validation(value, match):
    with pytest.raises(sim.SimError, match=match):
        sim._validate_convergence_retry(value)


def test_option_normalisation():
    assert sim._validate_convergence_retry(None) is None
    assert sim._validate_convergence_retry(False) is None
    assert sim._validate_convergence_retry({"reltol": 0.01}) == {
        "max_attempts": 1,
        "reltol": 0.01,
        "method": "gear",
    }


def test_xyce_refuses_the_option(tmp_path):
    path = _request(tmp_path, {"retry_on_convergence_error": True})
    doc = json.loads(path.read_text())
    doc["engine"] = "xyce"
    doc.pop("monte_carlo")
    path.write_text(json.dumps(doc))
    with pytest.raises(sim.SimError, match="retry_on_convergence_error"):
        sim.run_sim(str(path))


def test_retry_card_is_emitted_after_body_and_ngspice_options(tmp_path, monkeypatch):
    """The retry policy must win when two `.options` cards set the same key,
    so its card follows `.include`, `.temp` and `options.ngspice_options`."""
    decks: list[str] = []
    _stub(monkeypatch, fail_first={1: "Error: time step too small"}, decks=decks)
    sim.run_sim(
        str(
            _request(
                tmp_path,
                {
                    "retry_on_convergence_error": True,
                    "ngspice_options": {"reltol": 0.01, "method": "trap"},
                },
            )
        )
    )
    retried = [
        d for d in decks if "mc_sample_index=1" in d and ".options reltol=0.003" in d
    ]
    assert retried
    for deck in retried:
        lines = deck.splitlines()
        retry_idx = next(
            i
            for i, line in enumerate(lines)
            if line.startswith(".options reltol=0.003")
        )
        include_idx = next(
            i for i, line in enumerate(lines) if line.startswith(".include")
        )
        temp_idx = next(i for i, line in enumerate(lines) if line.startswith(".temp"))
        user_idx = next(
            i for i, line in enumerate(lines) if line.startswith(".options reltol=0.01")
        )
        assert retry_idx > max(include_idx, temp_idx, user_idx)
