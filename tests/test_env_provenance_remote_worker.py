"""Remote worker_environment exemption in the absolute-path lint (#5133)."""

import json

import pytest

from klayout_tools import env_provenance as ep
from klayout_tools.cli import main
from klayout_tools.pex_sim_evidence import project_testbench_sides


def _worker_env_block():
    return {
        "pdk": {"root": "/opt/pdk"},
        "simulator": {"selected_path": "/usr/bin/ngspice", "version_banner": "x"},
        "diagnostics": ["found /usr/bin/ngspice"],
    }


def test_lint_exempts_remote_worker_environment_single_job_and_fleet():
    """#5133: worker-observed paths under environment.remote are not flagged."""
    payload = {
        "environment": {
            "remote": {
                "worker_environment": _worker_env_block(),
                "fleet": [{"worker_environment": _worker_env_block()}],
            }
        }
    }
    assert ep.find_absolute_path_fields(payload) == []


def test_lint_still_flags_other_paths_beside_remote_worker_environment():
    payload = {
        "environment": {
            "remote": {"worker_environment": _worker_env_block()},
            "ngspice_binary": "/Users/x/bin/ngspice",
        },
        "provenance": {"input": {"path": "/Users/x/a.sp"}},
    }
    assert [f["field"] for f in ep.find_absolute_path_fields(payload)] == [
        "environment.ngspice_binary",
        "provenance.input.path",
    ]


def test_lint_worker_environment_exemption_is_path_scoped():
    payload = {
        "worker_environment": {"pdk": {"root": "/opt/pdk"}},
        "environment": {"worker_environment": {"pdk": {"root": "/opt/pdk"}}},
    }
    assert [f["field"] for f in ep.find_absolute_path_fields(payload)] == [
        "worker_environment.pdk.root",
        "environment.worker_environment.pdk.root",
    ]


def test_lint_remote_worker_environment_absent_capture_is_clean():
    payload = {
        "environment": {
            "remote": {"worker_environment": {"capture": "absent", "pdk": None}}
        }
    }
    assert ep.find_absolute_path_fields(payload) == []


@pytest.mark.parametrize("stream", [False, True])
def test_pex_side_observations_lint_clean_at_the_cli(tmp_path, capsys, stream):
    worker = _worker_env_block()
    single = {"environment": {"remote": {"worker_environment": worker}}}
    fleet = {"environment": {"remote": {"fleet": [{"worker_environment": worker}]}}}
    report = {
        "testbenches": [
            project_testbench_sides(single, fleet),
            project_testbench_sides(fleet, single),
        ]
    }
    path = tmp_path / "pex.json"
    path.write_text(json.dumps(report))
    args = [
        "env-provenance",
        "lint-envelope",
        str(path),
        "--format",
        "json",
        "--allow-prefix",
        "/usr/bin",
        "--allow-prefix",
        "/usr/local/bin",
    ]
    if stream:
        args.append("--json-stream")
        path.write_text(json.dumps(single) + "\n" + json.dumps(report))
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["finding_count"] == 0


def test_pex_exemption_is_limited_to_worker_observation_subtrees():
    root = {"pdk": {"root": "/opt/pdk"}}
    remote = {"worker_environment": root, "workdir": "/host/jobs"}
    side = {
        "environment": {"remote": remote, "client_ngspice_binary": "/host/bin/ngspice"},
        "worker_environment": root,
    }
    bench = {
        "sides": {
            "schematic": side,
            "other": {"environment": {"remote": {"worker_environment": root}}},
        },
        "environment": {"remote": {"worker_environment": root}},
    }
    dotted_key = "testbenches.0.sides.schematic.environment.remote.worker_environment"
    report = {
        "testbenches": [bench],
        "layout": {"path": "/client/design.gds"},
        dotted_key: "/client/elsewhere",
    }
    assert {f["field"] for f in ep.find_absolute_path_fields(report)} == {
        "testbenches.0.sides.schematic.environment.remote.workdir",
        "testbenches.0.sides.schematic.environment.client_ngspice_binary",
        "testbenches.0.sides.schematic.worker_environment.pdk.root",
        "testbenches.0.sides.other.environment.remote.worker_environment.pdk.root",
        "testbenches.0.environment.remote.worker_environment.pdk.root",
        "layout.path",
        dotted_key,
    }
    assert len(ep.find_absolute_path_fields({"testbenches": {"0": bench}})) == 6
