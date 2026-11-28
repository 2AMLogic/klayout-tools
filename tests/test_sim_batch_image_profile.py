"""Fleet image selection through batch preflight, job upload and reporting."""

import json
from pathlib import Path

import jsonschema
import pytest

from klayout_tools import sim
from klayout_tools import sim_batch as sb
from klayout_tools.sim_batch_plan import plan_batch_job


@pytest.fixture
def request_on_fleet(tmp_path, monkeypatch):
    script = tmp_path / "provision.sh"
    script.touch(mode=0o755)
    (tmp_path / "batch-fleet.env").write_text(
        "BATCH_MAX_JOB_SECONDS=3600\nBATCH_SIMLONG_MAX_JOB_SECONDS=7200\n"
    )
    monkeypatch.delenv(sb.MAX_JOB_SECONDS_ENV, raising=False)
    return {"batch": {"provision_script_path": str(script), "bucket": "example-bucket"}}


def resolve(request):
    return sb.resolve_batch_config(
        request, corner_count=1, timeout_s=4000, check_runner=False
    )


def uploaded_job(config):
    from subprocess import CompletedProcess

    uploads = []

    def capture(argv, timeout_s):
        assert "cp" in argv
        path = Path(argv[argv.index("cp") + 1])
        assert path.name == "job.json"
        uploads.append(path.read_bytes())
        return CompletedProcess(argv, 0, "", "")

    sb.submit_job(config, "fixture", sb.BatchJobSpec(cmd="true"), runner=capture)
    (document,) = uploads
    return document


def test_opt_in_controls_the_uploaded_job_and_ceiling(request_on_fleet):
    batch = request_on_fleet["batch"]
    batch["job_ceiling_check"] = "enforce"
    default = resolve(request_on_fleet)
    batch["image_profile"] = "sim"
    assert (
        uploaded_job(resolve(request_on_fleet))
        == uploaded_job(default)
        == b'{"cmd": "true"}'
    )
    with pytest.raises(sim.SimError) as exc:
        plan_batch_job(default, unit_count=1, timeout_s=4000)
    assert exc.value.code == "batch_job_ceiling_exceeded"

    batch["image_profile"] = "simlong"
    config = resolve(request_on_fleet)
    document = json.loads(uploaded_job(config))
    assert document == {"cmd": "true", "image_profile": "simlong"}
    schema = (
        Path(__file__).resolve().parents[1] / "docs/schemas/batch-job-spec.schema.json"
    )
    jsonschema.validate(document, json.loads(schema.read_text()))
    ceiling, hosts = plan_batch_job(config, unit_count=1, timeout_s=4000)
    assert (ceiling["image_profile"], hosts) == ("simlong", 1)
    assert (ceiling["max_job_seconds"], ceiling["max_job_seconds_source"]) == (
        7200,
        "fleet_config",
    )
    assert sb.batch_environment(config, "fixture", {})["image_profile"] == "simlong"
    batch.update(job_ceiling_check="split", job_timeout_s=6000)
    bound, _ = plan_batch_job(resolve(request_on_fleet), unit_count=2, timeout_s=4000)
    assert (bound["image_profile"], bound["job_bound_s"]) == ("simlong", 6000)


@pytest.mark.parametrize(
    "value",
    [
        "heavy",
        "lean",
        "kicad",
        "radiant",
        "fpga",
        "other",
        "SIMLONG",
        " simlong ",
        [],
        False,
        None,
    ],
)
def test_invalid_profile_fails_on_submit_and_request_only_preflight(
    request_on_fleet, value
):
    request_on_fleet["batch"]["image_profile"] = value
    with pytest.raises(sim.SimError, match="simlong"):
        resolve(request_on_fleet)
    with pytest.raises(sim.SimError, match="simlong"):
        sb._validate_batch_request_knobs({"image_profile": value})


def test_profile_ceiling_is_required_even_with_override(
    request_on_fleet, tmp_path, monkeypatch
):
    request_on_fleet["batch"]["image_profile"] = "simlong"
    monkeypatch.setenv(sb.MAX_JOB_SECONDS_ENV, "7400")
    assert resolve(request_on_fleet).max_job_seconds_source == "env"
    request_on_fleet["batch"]["max_job_seconds"] = 7500
    cfg = resolve(request_on_fleet)
    assert (cfg.max_job_seconds, cfg.max_job_seconds_source) == (7500, "request")
    (tmp_path / "batch-fleet.env").write_text("BATCH_MAX_JOB_SECONDS=3600\n")
    with pytest.raises(sim.SimError, match="BATCH_SIMLONG_MAX_JOB_SECONDS"):
        resolve(request_on_fleet)
    with pytest.raises(sim.SimError, match="BATCH_SIMLONG_MAX_JOB_SECONDS"):
        sb.dry_run_runner_check(request_on_fleet, corner_count=1, timeout_s=4000)


def test_dry_run_exercises_the_selected_fleet_profile(tmp_path, request_on_fleet):
    netlist = tmp_path / "body.spice"
    netlist.write_text(".param vdd=1\nVdd vdd 0 {vdd}\nR1 vdd 0 1k\n")
    (tmp_path / "corners.lib").write_text("* models\n")
    request = {
        **request_on_fleet,
        "netlist": netlist.name,
        "analysis": {"kind": "tran", "args": "1n 10n"},
        "models": {"lib": "corners.lib"},
        "corners": {"process": ["TYP"]},
        "options": {"timeout_s": 4000},
    }
    path = tmp_path / "request.json"

    def dry_run():
        path.write_text(json.dumps(request))
        return sim.run_sim(str(path), backend="batch", dry_run=True)

    request["batch"]["job_ceiling_check"] = "enforce"
    with pytest.raises(sim.SimError) as exc:
        dry_run()
    assert exc.value.code == "batch_job_ceiling_exceeded"
    request["batch"]["image_profile"] = "simlong"
    result = dry_run()
    assert result["status"] == "valid"
    assert result["job_ceiling"]["image_profile"] == "simlong"
    assert result["job_ceiling"]["job_bound_s"] == 4120
    assert result["job_ceiling"]["max_job_seconds"] == 7200

    request["batch"]["job_ceiling_check"] = "split"
    request["corners"]["params"] = {"vdd": [1.0, 1.1]}
    result = dry_run()
    assert result["job_ceiling"]["effective_hosts"] == 2
    assert result["job_ceiling"]["image_profile"] == "simlong"


def test_other_batch_verbs_keep_their_fleet_ceiling(request_on_fleet):
    request_on_fleet["batch"]["image_profile"] = "simlong"
    config = sb.resolve_batch_config(
        request_on_fleet,
        corner_count=1,
        timeout_s=4000,
        check_runner=False,
        sim_image_profiles=False,
    )
    assert config.max_job_seconds == 3600
    assert "image_profile" not in json.loads(uploaded_job(config))
    assert "image_profile" not in sb.batch_environment(config, "fixture", {})
