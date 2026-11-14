"""Remote worker_environment exemption in the absolute-path lint (#5133)."""

from klayout_tools import env_provenance as ep


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
