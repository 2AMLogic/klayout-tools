"""Tests for `klt yield-campaign` and the `run_campaign` library function
(issue #906, Phase 2a of the statistical/yield epic #710).

Three tiers, mirroring `tests/test_yield.py`'s own split:

- **Spec parsing / seed derivation** (no Rust, no ngspice) always run --
  loading a campaign spec, requiring its `monte_carlo` block, and deriving
  a deterministic default seed are pure Python.
- **Dispatch wiring** (no Rust, no ngspice) monkeypatches `sim.run_sim` and
  `yield_analysis.run_yield` directly, so `--backend`/`--hosts`/`--seed`
  plumbing and the `campaign` response block are exercised without either
  external dependency.
- **End-to-end** (`requires_native`, mirrors `test_yield.py`) stubs
  `subprocess.run` exactly like `test_sim.py` does (no real `ngspice`
  needed) but runs the *real* `sim.run_sim` -> `yield_analysis.run_yield`
  pipeline, proving Phase 1's fit/CI/Cpk pipeline actually runs, unmodified,
  against a campaign-orchestrated sample set.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim, yield_campaign
from klayout_tools.yield_campaign import CampaignError, run_campaign

requires_native = pytest.mark.skipif(
    importlib.util.find_spec("klt_yield_native") is None,
    reason=(
        "klt_yield_native is not built -- run `maturin develop --release` in "
        "native/yield/ (or `uv sync --group yield`); see "
        "docs/cli/yield.md#building-the-native-extension"
    ),
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _write_body(tmp_path: Path, name: str = "body.spice") -> Path:
    path = tmp_path / name
    path.write_text(".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\nC1 out 0 1n\n")
    return path


def _write_spec(tmp_path: Path, spec: dict, name: str = "campaign.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(spec))
    return path


def _base_spec(**overrides) -> dict:
    spec: dict = {
        "netlist": "body.spice",
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "measurements": [
            {
                "name": "vout",
                "spice": ".meas tran vout FIND v(out) AT=1u",
                "limits": {"min": 0.3, "max": 0.7, "target_yield": 0.9},
            }
        ],
        "monte_carlo": {"n": 6, "vary": "mismatch"},
    }
    spec.update(overrides)
    return spec


def _stub_subprocess_run(monkeypatch, *, log_text: str) -> None:
    def fake_run(cmd, capture_output, text, timeout):
        log_path = cmd[cmd.index("-o") + 1]
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(log_text)
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)


# --------------------------------------------------------------------------- #
# Spec loading / validation
# --------------------------------------------------------------------------- #


def test_run_campaign_missing_spec_file_raises(tmp_path):
    with pytest.raises(CampaignError, match="not found"):
        run_campaign(str(tmp_path / "nope.json"))


def test_run_campaign_spec_not_json_raises(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not json {{{")
    with pytest.raises(CampaignError, match="not valid JSON"):
        run_campaign(str(path))


def test_run_campaign_spec_missing_required_fields_raises(tmp_path):
    path = _write_spec(tmp_path, {"monte_carlo": {"n": 4, "seed": 1}})
    with pytest.raises(CampaignError, match="missing required field"):
        run_campaign(str(path))


def test_run_campaign_spec_without_monte_carlo_block_raises(tmp_path):
    _write_body(tmp_path)
    spec = _base_spec()
    del spec["monte_carlo"]
    path = _write_spec(tmp_path, spec)
    with pytest.raises(CampaignError, match="requires a 'monte_carlo' object"):
        run_campaign(str(path))


def test_run_campaign_spec_with_non_object_monte_carlo_raises(tmp_path):
    _write_body(tmp_path)
    spec = _base_spec(monte_carlo="not-an-object")
    path = _write_spec(tmp_path, spec)
    with pytest.raises(CampaignError, match="requires a 'monte_carlo' object"):
        run_campaign(str(path))


# --------------------------------------------------------------------------- #
# Seed derivation
# --------------------------------------------------------------------------- #


def test_default_campaign_seed_is_deterministic():
    spec = _base_spec()
    assert yield_campaign._default_campaign_seed(
        spec
    ) == yield_campaign._default_campaign_seed(spec)


def test_default_campaign_seed_changes_with_sampling_content():
    base = yield_campaign._default_campaign_seed(_base_spec())
    varied = yield_campaign._default_campaign_seed(
        _base_spec(monte_carlo={"n": 7, "vary": "mismatch"})
    )
    assert base != varied


def test_default_campaign_seed_ignores_execution_only_fields():
    """Two specs that agree on everything sampling-relevant derive the same
    default seed even if their backend/hosts/remote/yield-only fields
    differ -- dispatch mechanics never change *what* is sampled."""
    plain = yield_campaign._default_campaign_seed(_base_spec())
    with_backend = yield_campaign._default_campaign_seed(
        _base_spec(
            backend="local-parallel",
            confidence=0.9,
            target_ci_halfwidth=0.02,
            min_samples=10,
        )
    )
    assert plain == with_backend


def test_default_campaign_seed_ignores_an_explicit_seed_value():
    """The seed that would be *derived* must not depend on whether the spec
    also happens to declare its own explicit seed -- `_campaign_identity_json`
    strips `monte_carlo.seed` before hashing."""
    without_seed = yield_campaign._default_campaign_seed(_base_spec())
    with_seed = yield_campaign._default_campaign_seed(
        _base_spec(monte_carlo={"n": 6, "vary": "mismatch", "seed": 999})
    )
    assert without_seed == with_seed


# --------------------------------------------------------------------------- #
# Dispatch wiring (sim.run_sim / yield_analysis.run_yield mocked)
# --------------------------------------------------------------------------- #


def _fake_sim_report(**overrides) -> dict:
    report = {
        "schema_version": 1,
        "status": "pass",
        "corner_count": 6,
        "passed": 6,
        "failed": 0,
        "errored": 0,
        "environment": {"engine": "ngspice", "engine_version": "46"},
        "measurements": [
            {
                "name": "vout",
                "unit": None,
                "limits": {"min": 0.3, "max": 0.7, "target_yield": 0.9},
            }
        ],
        "corners": [
            {
                "corner_id": f"default/novdd/27C/mc{i}",
                "status": "pass",
                "measurements": [{"name": "vout", "value": 0.5, "unit": None}],
                "monte_carlo": {
                    "sample_index": i,
                    "seed": 1000 + i,
                    "process_seed": 1,
                    "mismatch_seed": 2,
                },
            }
            for i in range(6)
        ],
    }
    report.update(overrides)
    return report


def _fake_yield_report(**overrides) -> dict:
    report = {
        "schema_version": 1,
        "confidence": 0.95,
        "target_ci_halfwidth": 0.01,
        "min_samples": 2,
        "status": "pass",
        "measurement_count": 1,
        "measurements": [],
        "warnings": [],
    }
    report.update(overrides)
    return report


def test_run_campaign_passes_backend_and_hosts_to_sim_run_sim(tmp_path, monkeypatch):
    _write_body(tmp_path)
    spec_path = _write_spec(tmp_path, _base_spec())

    captured: dict = {}

    def fake_run_sim(request_path, *, artifacts_dir, backend, hosts):
        captured["request_path"] = request_path
        captured["backend"] = backend
        captured["hosts"] = hosts
        with open(request_path, encoding="utf-8") as handle:
            captured["request"] = json.load(handle)
        return _fake_sim_report()

    def fake_run_yield(samples_path, **kwargs):
        captured["samples_path"] = samples_path
        captured["yield_kwargs"] = kwargs
        return _fake_yield_report()

    monkeypatch.setattr(yield_campaign.sim, "run_sim", fake_run_sim)
    monkeypatch.setattr(yield_campaign, "run_yield", fake_run_yield)

    report = run_campaign(str(spec_path), backend="local-parallel", hosts=3)

    assert captured["backend"] == "local-parallel"
    assert captured["hosts"] == 3
    # netlist absolutized against the spec's own directory, not `out_dir`.
    assert captured["request"]["netlist"] == str(tmp_path / "body.spice")
    assert report["campaign"]["backend"] == "local-parallel"
    assert report["campaign"]["hosts"] == 3
    assert report["campaign"]["sim_status"] == "pass"
    assert report["campaign"]["corner_count"] == 6
    assert os.path.isfile(captured["samples_path"])


def test_run_campaign_backend_and_hosts_default_from_spec_when_omitted(
    tmp_path, monkeypatch
):
    _write_body(tmp_path)
    spec_path = _write_spec(
        tmp_path,
        _base_spec(backend="local-parallel", remote={"hosts": 4}),
    )

    captured: dict = {}

    def fake_run_sim(request_path, *, artifacts_dir, backend, hosts):
        captured["backend"] = backend
        captured["hosts"] = hosts
        return _fake_sim_report()

    monkeypatch.setattr(yield_campaign.sim, "run_sim", fake_run_sim)
    monkeypatch.setattr(
        yield_campaign, "run_yield", lambda *a, **k: _fake_yield_report()
    )

    report = run_campaign(str(spec_path))

    # backend/hosts kwargs to sim.run_sim are None when omitted -- sim.py
    # itself resolves the request's own fields, same precedence as `klt sim`.
    assert captured["backend"] is None
    assert captured["hosts"] is None
    assert report["campaign"]["backend"] == "local-parallel"
    assert report["campaign"]["hosts"] == 4


def test_run_campaign_seed_precedence_cli_beats_spec_beats_derived(
    tmp_path, monkeypatch
):
    _write_body(tmp_path)

    captured: dict = {}

    def fake_run_sim(request_path, *, artifacts_dir, backend, hosts):
        with open(request_path, encoding="utf-8") as handle:
            captured["seed"] = json.load(handle)["monte_carlo"]["seed"]
        return _fake_sim_report()

    monkeypatch.setattr(yield_campaign.sim, "run_sim", fake_run_sim)
    monkeypatch.setattr(
        yield_campaign, "run_yield", lambda *a, **k: _fake_yield_report()
    )

    # No seed anywhere -> derived.
    spec_path = _write_spec(tmp_path, _base_spec(), name="a.json")
    report = run_campaign(str(spec_path))
    derived_seed = report["campaign"]["seed"]
    assert report["campaign"]["seed_source"] == "derived"
    assert captured["seed"] == derived_seed

    # Spec's own seed wins over the derived default.
    spec_path = _write_spec(
        tmp_path,
        _base_spec(monte_carlo={"n": 6, "vary": "mismatch", "seed": 777}),
        name="b.json",
    )
    report = run_campaign(str(spec_path))
    assert report["campaign"]["seed"] == 777
    assert report["campaign"]["seed_source"] == "spec"
    assert captured["seed"] == 777

    # --seed (the `seed` kwarg) beats the spec's own value.
    report = run_campaign(str(spec_path), seed=555)
    assert report["campaign"]["seed"] == 555
    assert report["campaign"]["seed_source"] == "cli"
    assert captured["seed"] == 555


def test_run_campaign_same_spec_rerun_reproduces_same_seed(tmp_path, monkeypatch):
    _write_body(tmp_path)
    spec_path = _write_spec(tmp_path, _base_spec())

    monkeypatch.setattr(
        yield_campaign.sim, "run_sim", lambda *a, **k: _fake_sim_report()
    )
    monkeypatch.setattr(
        yield_campaign, "run_yield", lambda *a, **k: _fake_yield_report()
    )

    first = run_campaign(str(spec_path), out_dir=str(tmp_path / "run1"))
    second = run_campaign(str(spec_path), out_dir=str(tmp_path / "run2"))

    assert first["campaign"]["seed"] == second["campaign"]["seed"]
    assert (
        first["campaign"]["seed_source"]
        == second["campaign"]["seed_source"]
        == ("derived")
    )


def test_run_campaign_yield_only_fields_never_reach_sim_run(tmp_path, monkeypatch):
    """`confidence`/`target_ci_halfwidth`/`min_samples` are `klt yield`'s own
    knobs, not `klt sim` request fields -- they must be stripped before the
    request is dispatched, and threaded into `run_yield` instead."""
    _write_body(tmp_path)
    spec_path = _write_spec(
        tmp_path,
        _base_spec(confidence=0.9, target_ci_halfwidth=0.05, min_samples=10),
    )

    captured: dict = {}

    def fake_run_sim(request_path, *, artifacts_dir, backend, hosts):
        with open(request_path, encoding="utf-8") as handle:
            captured["request"] = json.load(handle)
        return _fake_sim_report()

    def fake_run_yield(samples_path, **kwargs):
        captured["yield_kwargs"] = kwargs
        return _fake_yield_report()

    monkeypatch.setattr(yield_campaign.sim, "run_sim", fake_run_sim)
    monkeypatch.setattr(yield_campaign, "run_yield", fake_run_yield)

    run_campaign(str(spec_path))

    for key in ("confidence", "target_ci_halfwidth", "min_samples"):
        assert key not in captured["request"]
    assert captured["yield_kwargs"]["confidence"] == 0.9
    assert captured["yield_kwargs"]["target_ci_halfwidth"] == 0.05
    assert captured["yield_kwargs"]["min_samples"] == 10


def test_run_campaign_sim_dispatch_failure_raises_campaign_error(tmp_path, monkeypatch):
    _write_body(tmp_path)
    spec_path = _write_spec(tmp_path, _base_spec())

    def fake_run_sim(*a, **k):
        raise sim.SimError("boom")

    monkeypatch.setattr(yield_campaign.sim, "run_sim", fake_run_sim)

    with pytest.raises(CampaignError, match="campaign dispatch failed: boom"):
        run_campaign(str(spec_path))


def test_run_campaign_yield_analysis_failure_raises_campaign_error(
    tmp_path, monkeypatch
):
    _write_body(tmp_path)
    spec_path = _write_spec(tmp_path, _base_spec())

    monkeypatch.setattr(
        yield_campaign.sim, "run_sim", lambda *a, **k: _fake_sim_report()
    )

    def fake_run_yield(*a, **k):
        raise yield_campaign.YieldError("not enough samples")

    monkeypatch.setattr(yield_campaign, "run_yield", fake_run_yield)

    with pytest.raises(CampaignError, match="not enough samples"):
        run_campaign(str(spec_path))


# --------------------------------------------------------------------------- #
# End-to-end (real sim.run_sim -> yield_analysis.run_yield, ngspice stubbed,
# klt_yield_native required)
# --------------------------------------------------------------------------- #


@requires_native
def test_run_campaign_end_to_end_runs_phase1_pipeline_unmodified(tmp_path, monkeypatch):
    _write_body(tmp_path)
    spec_path = _write_spec(tmp_path, _base_spec())
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "vout                =  5.00000e-01\n"
        ),
    )

    report = run_campaign(str(spec_path))

    assert report["schema_version"] == 1
    assert report["measurement_count"] == 1
    measurement = report["measurements"][0]
    assert measurement["n"] == 6
    assert measurement["distribution"]["mean"] == pytest.approx(0.5)
    assert measurement["yield"]["empirical"]["n"] == 6

    campaign = report["campaign"]
    assert campaign["requested_samples"] == 6
    assert campaign["vary"] == "mismatch"
    assert campaign["sim_status"] == "pass"
    assert campaign["corner_count"] == 6
    assert os.path.isfile(campaign["sim_report_path"])
    assert os.path.isfile(campaign["request_path"])

    with open(campaign["sim_report_path"], encoding="utf-8") as handle:
        sim_report = json.load(handle)
    assert sim_report["corner_count"] == 6
    assert len(sim_report["corners"]) == 6


@requires_native
def test_run_campaign_end_to_end_same_spec_rerun_reproduces_sample_set(
    tmp_path, monkeypatch
):
    _write_body(tmp_path)
    spec_path = _write_spec(tmp_path, _base_spec())
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "vout                =  5.00000e-01\n"
        ),
    )

    first = run_campaign(str(spec_path), out_dir=str(tmp_path / "run1"))
    second = run_campaign(str(spec_path), out_dir=str(tmp_path / "run2"))

    assert first["campaign"]["seed"] == second["campaign"]["seed"]

    with open(first["campaign"]["sim_report_path"], encoding="utf-8") as handle:
        first_corners = json.load(handle)["corners"]
    with open(second["campaign"]["sim_report_path"], encoding="utf-8") as handle:
        second_corners = json.load(handle)["corners"]

    first_seeds = [(c["corner_id"], c["monte_carlo"]) for c in first_corners]
    second_seeds = [(c["corner_id"], c["monte_carlo"]) for c in second_corners]
    assert first_seeds == second_seeds
