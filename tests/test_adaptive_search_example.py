"""Decision-loop tests for `examples/sim-batch/adaptive-search.py` (#2716).

The driver's runner is injected, so these tests need neither AWS nor a
simulator: each fake `klt sim` response is derived from the request file the
driver just wrote.
"""

from __future__ import annotations

import importlib.util
import json
import math
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "examples" / "sim-batch" / "adaptive-search.py"

_spec = importlib.util.spec_from_file_location("adaptive_search", EXAMPLE)
ad = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ad)

TEMPLATE = {
    "netlist": "/abs/tb.spice",
    "analysis": {"kind": "op"},
    "models": {"pdk": "sky130A"},
    "monte_carlo": {"n": 5, "seed": 1},
    "batch": {"bucket": "<your-batch-jobs-bucket>"},
}


def _report(vdd, vout, *, job, **over):
    doc = {
        "schema_version": 3,
        "status": "pass",
        "environment": {"remote": {"job_id": job}},
        "corners": [
            {
                "status": "pass",
                "supply_v": {"vdd": vdd},
                "diagnostics": [],
                "measurements": [
                    {"name": "vout_v", "value": vout, "unit": "V", "status": "pass"}
                ],
            }
        ],
    }
    doc.update(over)
    return doc


class FakeKlt:
    """Maps the written request's source value to a fake fleet response."""

    def __init__(self, fn=lambda v: v, *, exit_code=0, mutate=None):
        self.fn, self.exit_code, self.mutate = fn, exit_code, mutate
        self.calls, self.requests = [], []

    def __call__(self, argv):
        self.calls.append(argv)
        request = json.loads(Path(argv[argv.index("sim") + 1]).read_text())
        self.requests.append(request)
        vdd = request["corners"]["supply_v"]["vdd"][0]
        report = _report(vdd, self.fn(vdd), job=f"klt-sim-{len(self.calls):04d}")
        if self.mutate:
            report = self.mutate(len(self.calls), report)
        return self.exit_code, json.dumps(report), ""


def _search(tmp_path, runner, **over):
    args = dict(
        source="vdd",
        measurement="vout_v",
        target=0.9,
        lo=0.0,
        hi=2.0,
        tol=0.1,
        max_probes=10,
        workdir=tmp_path / "w",
        process="tt",
        temperature_c=27.0,
        runner=runner,
    )
    args.update(over)
    return ad.bisect(TEMPLATE, **args)


def test_next_input_depends_on_prior_measurement_and_converges(tmp_path):
    klt = FakeKlt(lambda v: v * 0.6)  # crosses 0.9 at vdd = 1.5
    out = _search(tmp_path, klt)
    assert out["outcome"] == "converged"
    assert abs(out["estimate"] - 1.5) <= 0.1
    values = [p["value"] for p in out["probes"]]
    assert values[:3] == [1.0, 1.5, 1.75]  # 0.6 < 0.9 -> lo=1.0; 0.9 >= 0.9 -> hi=1.5
    jobs = [p["job_id"] for p in out["probes"]]
    assert len(set(jobs)) == len(jobs)
    for p in out["probes"]:
        assert Path(p["request"]).exists() and Path(p["report"]).exists()


def test_every_request_is_single_unit_batch_and_template_untouched(tmp_path):
    klt = FakeKlt(lambda v: v * 0.6)
    _search(tmp_path, klt)
    for req, argv in zip(klt.requests, klt.calls, strict=True):
        assert "monte_carlo" not in req and req["backend"] == "batch"
        assert req["corners"]["process"] == ["tt"]
        assert req["corners"]["temperature_c"] == [27.0]
        assert len(req["corners"]["supply_v"]["vdd"]) == 1
        assert argv[-4:] == ["--backend", "batch", "--format", "json"]
    assert "backend" not in TEMPLATE and "corners" not in TEMPLATE
    assert "monte_carlo" in TEMPLATE


def test_decreasing_direction(tmp_path):
    out = _search(tmp_path, FakeKlt(lambda v: 1.8 - v * 0.6), direction="decreasing")
    assert out["outcome"] == "converged"
    assert abs(out["estimate"] - 1.5) <= 0.1


def test_exit_3_with_usable_measurement_is_a_valid_observation(tmp_path):
    def mutate(n, report):
        report["status"] = "fail"
        report["corners"][0]["status"] = "fail"
        report["corners"][0]["measurements"][0]["status"] = "fail"
        return report

    out = _search(tmp_path, FakeKlt(lambda v: v * 0.6, exit_code=3, mutate=mutate))
    assert out["outcome"] == "converged"


def _stop_case(tmp_path, mutate, *, exit_code=4, probes_expected=1):
    klt = FakeKlt(lambda v: v * 0.6, exit_code=exit_code, mutate=mutate)
    out = _search(tmp_path, klt)
    assert out["outcome"] == "stopped"
    assert out["reason"]
    assert len(klt.calls) == probes_expected  # no further submission
    assert out["estimate"] is None
    return out


def _set(path_fn):
    def make(value):
        def mutate(n, report):
            path_fn(report, value)
            return report

        return mutate

    return make


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(status="error"),
        lambda r: r.update(status="inconclusive"),
        lambda r: r.update(status="not_checked"),
        lambda r: r.update(status="pass_partial"),
        lambda r: r["corners"][0].update(status="error"),
        lambda r: r["corners"][0].update(status="inconclusive"),
        lambda r: r["corners"].append(dict(r["corners"][0])),
        lambda r: r["corners"].clear(),
        lambda r: r["corners"][0]["measurements"].clear(),
        lambda r: r["corners"][0]["measurements"][0].update(value=None),
        lambda r: r["corners"][0]["measurements"][0].update(value=math.nan),
        lambda r: r["corners"][0]["measurements"][0].update(value=math.inf),
        lambda r: r["corners"][0]["measurements"][0].update(value=True),
        lambda r: r["corners"][0]["measurements"][0].update(status="error"),
        lambda r: r["corners"][0]["measurements"][0].update(name="other"),
        lambda r: r["corners"][0].update(supply_v={"vdd": 9.9}),
        lambda r: r["corners"][0].update(
            diagnostics=[{"severity": "error", "code": "batch_poll_timeout"}]
        ),
        lambda r: r["environment"].pop("remote"),
        lambda r: r.clear() or r.update(error={"command": "sim", "message": "x"}),
    ],
)
def test_unusable_first_probe_stops_the_campaign(tmp_path, mutation):
    def mutate(n, report):
        mutation(report)
        return report

    _stop_case(tmp_path, mutate)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(environment="bad"),
        lambda r: r.update(environment=[]),
        lambda r: r["environment"].update(remote="bad"),
        lambda r: r["environment"].update(remote=[]),
        lambda r: r["corners"][0].update(supply_v=[]),
        lambda r: r["corners"][0].update(supply_v="bad"),
        lambda r: r["corners"][0]["supply_v"].update(vdd=True),
        lambda r: r["corners"][0].update(measurements={"vout_v": 1.0}),
        lambda r: r["corners"][0].update(measurements=5),
        lambda r: r["corners"][0].update(diagnostics=5),
        lambda r: r["corners"][0].update(diagnostics="bad"),
        lambda r: r.update(status=[]),
        lambda r: r["corners"][0].update(status={}),
        lambda r: r["corners"][0]["measurements"][0].update(status=[]),
    ],
)
def test_malformed_nested_containers_stop_not_crash(tmp_path, mutation):
    """Valid JSON with a wrong-typed nested container takes the controlled stop
    path (ProbeError), never an AttributeError/TypeError traceback."""

    def mutate(n, report):
        mutation(report)
        return report

    out = _stop_case(tmp_path, mutate)
    probe = out["probes"][0]
    assert Path(probe["request"]).is_file() and Path(probe["report"]).is_file()


def test_malformed_nested_container_on_later_probe_stops(tmp_path):
    def mutate(n, report):
        if n == 2:
            report["environment"] = "bad"
        return report

    out = _stop_case(tmp_path, mutate, probes_expected=2)
    assert out["probes"][0]["job_id"] == "klt-sim-0001"
    assert out["probes"][1]["job_id"] is None


def test_poll_timeout_stops_and_reports_job_identity(tmp_path):
    def mutate(n, report):
        report["status"] = "error"
        report["corners"][0].update(
            status="error",
            measurements=[{"name": "vout_v", "value": None, "status": "error"}],
            diagnostics=[{"severity": "error", "code": "batch_poll_timeout"}],
        )
        return report

    out = _stop_case(tmp_path, mutate)
    assert out["probes"][0]["job_id"] == "klt-sim-0001"


def test_repeated_job_id_stops_before_third_probe(tmp_path):
    def mutate(n, report):
        report["environment"]["remote"]["job_id"] = "same"
        return report

    _stop_case(tmp_path, mutate, exit_code=0, probes_expected=2)


def test_stops_on_later_failure_without_further_submission(tmp_path):
    def mutate(n, report):
        if n == 2:
            report["status"] = "error"
        return report

    _stop_case(tmp_path, mutate, probes_expected=2)


def test_non_json_stdout_stops(tmp_path):
    out = _search(tmp_path, lambda argv: (1, "", "klt sim: boom"))
    assert out["outcome"] == "stopped" and len(out["probes"]) == 1
    # Input and output files are retained in the summary on the stop path too.
    probe = out["probes"][0]
    assert Path(probe["request"]).is_file() and Path(probe["report"]).is_file()
    assert probe["job_id"] is None


def test_max_probe_exhaustion(tmp_path):
    klt = FakeKlt(lambda v: v * 0.6)
    out = _search(tmp_path, klt, tol=1e-9, max_probes=3)
    assert out["outcome"] == "exhausted" and len(klt.calls) == 3
    assert out["estimate"] is None


def test_workdir_collision_stops_before_submitting(tmp_path):
    klt = FakeKlt()
    (tmp_path / "w").mkdir()
    (tmp_path / "w" / "probe-000.report.json").write_text("{}")
    out = _search(tmp_path, klt)
    assert out["outcome"] == "stopped" and klt.calls == []


@pytest.mark.parametrize(
    "over",
    [
        dict(lo=2.0, hi=1.0),
        dict(lo=1.0, hi=1.0),
        dict(lo=math.nan),
        dict(hi=math.inf),
        dict(tol=0.0),
        dict(tol=math.nan),
        dict(target=math.inf),
        dict(max_probes=0),
        dict(direction="sideways"),
    ],
)
def test_invalid_arguments_submit_nothing(tmp_path, over):
    klt = FakeKlt()
    with pytest.raises(ValueError):
        _search(tmp_path, klt, **over)
    assert klt.calls == []


def test_main_resolves_netlist_and_prints_summary(tmp_path, capsys):
    (tmp_path / "tb.spice").write_text("* tb\n")
    template = dict(TEMPLATE, netlist="tb.spice")
    (tmp_path / "t.json").write_text(json.dumps(template))
    klt = FakeKlt(lambda v: v * 0.6)
    code = ad.main(
        [
            "--template",
            str(tmp_path / "t.json"),
            "--source",
            "vdd",
            "--measurement",
            "vout_v",
            "--target",
            "0.9",
            "--lo",
            "0",
            "--hi",
            "2",
            "--tol",
            "0.1",
            "--max-probes",
            "10",
            "--workdir",
            str(tmp_path / "w"),
            "--process",
            "tt",
            "--temperature",
            "27",
            "--klt",
            "uv run klt",
        ],
        runner=klt,
    )
    assert code == 0
    assert klt.requests[0]["netlist"] == str(tmp_path / "tb.spice")
    assert klt.calls[0][:3] == ["uv", "run", "klt"]
    assert json.loads(capsys.readouterr().out)["outcome"] == "converged"


def test_docs_and_example_stay_deployment_neutral_and_consistent():
    docs = (REPO_ROOT / "docs" / "cli" / "sim.md").read_text()
    readme = (REPO_ROOT / "examples" / "sim-batch" / "README.md").read_text()
    for text in (docs, readme):
        assert "adaptive-search.py" in text
    flags = set(re.findall(r'"(--[a-z-]+)"', EXAMPLE.read_text()))
    for flag in flags - {"--klt", "--direction"}:
        assert flag in readme
    assert "<your-batch-jobs-bucket>" in readme
    assert re.search(r"\b\d{12}\b", EXAMPLE.read_text()) is None
