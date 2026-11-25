"""Analytic and Picard checks through the public CLI and real tagged mesh."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from helpers.em_validation_oracles import geode_or_skip, gmsh_or_skip
from klayout_tools.cli.parser import create_parser

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/em/heated_bar"


@pytest.fixture(scope="module")
def heated_bar(tmp_path_factory):
    binary = geode_or_skip()
    help_text = subprocess.check_output([binary, "--help"], text=True, timeout=15)
    if "self-heating" not in help_text:
        pytest.skip("Geode CLI predates the thermal subcommands (geode-fem#1806)")
    gmsh_or_skip()
    work = tmp_path_factory.mktemp("heated_bar")
    mesh = work / "bar.msh"
    subprocess.run(
        [
            os.environ.get("GEODE_GMSH", "gmsh"),
            "-3",
            str(EXAMPLE / "bar.geo"),
            "-nt",
            "1",
            "-v",
            "0",
            "-o",
            str(mesh),
        ],
        check=True,
        timeout=45,
    )
    return mesh, binary


def _run(tmp_path, heated_bar, request, capsys):
    mesh, binary = heated_bar
    request["mesh"] = {"path": str(mesh), "length_unit_m": 1e-3}
    request["options"] = {"geode_binary": binary, "keep_artifacts": False}
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    args = create_parser().parse_args(["em", str(path), "--format", "json"])
    code = args.func(args)
    output = capsys.readouterr()
    return code, json.loads(output.err if code else output.out)


def _node_x_mm(mesh):
    """X in file order for Gmsh's non-parametric ASCII 4.1 node blocks."""
    lines = iter(mesh.read_text().split("$Nodes\n", 1)[1].splitlines())
    blocks, nodes, _, _ = map(int, next(lines).split())
    coords = []
    for _ in range(blocks):
        _, _, parametric, count = map(int, next(lines).split())
        assert parametric == 0
        for _ in range(count):
            next(lines)  # node tag
        coords.extend(float(next(lines).split()[0]) for _ in range(count))
    assert len(coords) == nodes
    return coords


def test_public_heated_bar_profile_and_balance(tmp_path, heated_bar, capsys):
    request = json.loads((EXAMPLE / "request.json").read_text())
    code, report = _run(tmp_path, heated_bar, request, capsys)
    assert code == 0, report
    result = report["results"]["temperature"]
    # 2 x 0.5 x 0.5 mm, k = 40 W/(m K), q = 4e8 W/m³, 300 K ends.
    length = 2e-3
    expected_peak_rise = 4e8 * length**2 / (8 * 40)
    xs = _node_x_mm(heated_bar[0])
    errors = []
    for mm, temperature in zip(xs, result["temperature_k"], strict=True):
        x = mm * 1e-3
        exact = 300 + 4e8 * x * (length - x) / (2 * 40)
        errors.append(abs(temperature - exact))
    assert max(errors) < 0.02 * expected_peak_rise
    power = 4e8 * length * (0.5e-3) ** 2
    assert result["source_power_w"] == pytest.approx(power, rel=1e-8)
    assert result["boundary_power_w"] == pytest.approx(power, rel=1e-8)
    assert result["energy_rel_mismatch"] < 1e-8


@pytest.mark.parametrize(
    ("volts", "picard", "expected"),
    [
        (0.01, {}, "converged"),
        (0.01, {"max_iter": 1}, "not_converged"),
        (10, {}, "runaway"),
    ],
)
def test_public_joule_termination(
    tmp_path, heated_bar, capsys, volts, picard, expected
):
    settings = {
        "terminals": [
            {"surface": "left", "voltage_v": volts},
            {"surface": "right", "voltage_v": 0},
        ],
        "regions": [
            {
                "physical_group": "bar",
                "sigma_ref_s_m": 5.8e7,
                "alpha_per_k": 0.004,
                "t_ref_k": 300,
                "conductivity_w_m_k": 400,
            }
        ],
        "fixed_temperatures": [
            {"surface": end, "temperature_k": 300} for end in ("left", "right")
        ],
        "initial_temperature_k": 300,
        "picard": picard,
    }
    code, report = _run(
        tmp_path,
        heated_bar,
        {"solve_mode": "self_heating", "self_heating": settings},
        capsys,
    )
    if expected == "converged":
        assert code == 0, report
        t = report["results"]["temperature"]
        assert t["termination"] == expected and t["peak_temperature_k"] > 300
        assert t["boundary_power_w"] == pytest.approx(t["source_power_w"], rel=1e-8)
    else:
        assert code == 1 and expected in report["error"]["message"]
        assert "results" not in report
