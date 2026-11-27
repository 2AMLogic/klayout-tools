"""Public thermal solve modes, including Geode's nonconverged last iterate."""

import json

import pytest

from klayout_tools.cli.parser import create_parser


def _invoke(request_path, fmt="json"):
    args = create_parser().parse_args(["em", str(request_path), "--format", fmt])
    return args.func(args)


def _request(tmp_path, mode, report):
    mesh = tmp_path / "bar.msh"
    mesh.touch()
    stub = tmp_path / "geode"
    stub.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "geode 0.9.0"; exit; fi\n'
        f'[ "$1" = "{mode.replace("_", "-")}" ] && [ "$3" = "--output" ] || exit 7\n'
        f"cat <<'REPORT' > \"$4\"\n{json.dumps(report)}\nREPORT\n"
    )
    stub.chmod(0o755)
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "mesh": {"path": mesh.name, "length_unit_m": 1e-3},
                "solve_mode": mode,
                mode: {
                    "regions": [{"physical_group": "bar", "conductivity_w_m_k": 40}],
                    "fixed_temperatures": [{"surface": "left", "temperature_k": 300}],
                },
                "options": {"geode_binary": str(stub), "output_dir": "out"},
            }
        )
    )
    return request


def _report(mode):
    return {
        "kind": mode,
        "status": "ok",
        "mesh": {"n_nodes": 2},
        "temperature_k": [300.0, 301.0],
        "source_power_w": 2,
        "boundary_power_w": 2,
        "energy_rel_mismatch": 0,
        "termination": "converged",
        "has_thermal_solution": True,
    }


@pytest.mark.parametrize("mode", ["thermal", "self_heating"])
def test_public_thermal_modes(tmp_path, capsys, mode):
    path = _request(tmp_path, mode, _report(mode))
    assert _invoke(path) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["environment"]["solve_mode"] == mode
    t = report["results"]["temperature"]
    assert (t["temperature_k"], t["peak_temperature_k"]) == ([300, 301], 301)
    assert (t["source_power_w"], t["boundary_power_w"], t["energy_rel_mismatch"]) == (
        2,
        2,
        0,
    )
    if mode == "self_heating":
        assert t["termination"] == "converged"
    (spec,) = (tmp_path / "out").rglob("spec.json")
    assert json.loads(spec.read_text()) == {
        "schema_version": 1,
        "mesh": {"path": str(tmp_path / "bar.msh"), "length_unit_m": 1e-3},
        mode: json.loads(path.read_text())[mode],
    }
    assert _invoke(path, "text") == 0
    assert "301 K" in capsys.readouterr().out


@pytest.mark.parametrize("status", ["runaway", "not_converged"])
def test_nonconverged_last_iterate_is_an_application_error(tmp_path, capsys, status):
    report = _report("self_heating")
    report["termination"] = status
    path = _request(tmp_path, "self_heating", report)
    assert _invoke(path) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["command"] == "em" and status in error["error"]["message"]
    assert "results" not in error


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "capacitance"},
        {"mesh": {"n_nodes": 3}},
        {"temperature_k": [300, True]},
        {"source_power_w": None},
        {"status": "error"},
        {"energy_rel_mismatch": float("inf")},
        {"has_thermal_solution": False},
    ],
)
def test_invalid_thermal_report(tmp_path, capsys, change):
    path = _request(tmp_path, "self_heating", {**_report("self_heating"), **change})
    assert _invoke(path) == 1
    assert json.loads(capsys.readouterr().err)["error"]["command"] == "em"


@pytest.mark.parametrize(
    "extra",
    [
        {"outputs": ["c"]},
        {"checks": [{"output": "c"}]},
        {"layout": "chip.gds"},
        {"thermal": {}},
        {"self_heating": {}},
        {"ground": [{"name": "gnd", "surface": "left"}]},
    ],
)
def test_rejects_incompatible_thermal_request(tmp_path, capsys, extra):
    path = _request(tmp_path, "thermal", _report("thermal"))
    request = json.loads(path.read_text())
    path.write_text(json.dumps({**request, **extra}))
    assert _invoke(path) == 1
    assert json.loads(capsys.readouterr().err)["error"]["command"] == "em"
    assert not (tmp_path / "out").exists()
