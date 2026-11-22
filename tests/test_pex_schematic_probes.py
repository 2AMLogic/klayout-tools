"""PEX measures a schematic-only internal node alongside post-layout comparisons."""

import json
import shutil

import pytest

from klayout_tools.cli import main
from klayout_tools.pex import PexError, run_pex
from test_pex import _make_sky130_poly_resistor_layout, _write_gds, _write_testbench


def _inputs(
    tmp_path, suffix="", *, port=True, sided=True, node="xres.vref", **probe_fields
):
    dut = tmp_path / "dut.spice"
    dut.write_text(".SUBCKT RES RA RB\nR1 RA vref 144.6\nR2 vref RB 144.6\n.ENDS RES\n")
    tb = _write_testbench(tmp_path / "bench.spice", dut)
    layout = _write_gds(_make_sky130_poly_resistor_layout(), tmp_path / "res.gds")
    probes = (
        [{"name": "vout", "spice": ".meas tran vout FIND v(rb) AT=1u"}] if port else []
    )
    probes.append(
        {
            "name": "internal",
            "spice": f".meas tran internal FIND v({node}) AT=1u",
            **({"sides": "schematic"} if sided else {}),
            **probe_fields,
        }
    )
    request = tmp_path / f"request{suffix}.json"
    request.write_text(
        json.dumps(
            {
                "netlist": str(tb),
                "analysis": {"kind": "tran", "args": "1n 1u"},
                "corners": {"temperature_c": [27, 85]},
                "measurements": probes,
            }
        )
    )
    return layout, request


@pytest.mark.skipif(shutil.which("ngspice") is None, reason="needs ngspice")
@pytest.mark.parametrize(
    ("port", "code", "status"), [(True, 0, "pass"), (False, 4, "not_checked")]
)
def test_schematic_probe_with_real_ngspice(tmp_path, capsys, port, code, status):
    layout, request = _inputs(tmp_path, port=port)
    saved = request.read_bytes()
    outdir = tmp_path / "artifacts"
    assert (
        main(
            [
                "pex",
                layout,
                str(request),
                "--deck",
                "sky130",
                "--outdir",
                str(outdir),
                "--device-only-leg",
                "--format",
                "json",
            ]
        )
        == code
    )
    report = json.loads(capsys.readouterr().out)
    assert request.read_bytes() == saved
    assert report["status"] == status
    assert report["corner_count"] == 2
    rows = [r for r in report["delta"] if r["measurement"] == "internal"]
    assert len(rows) == 2
    assert {r["corner_id"] for r in rows} == {r["corner_id"] for r in report["delta"]}
    for row in rows:
        assert row["schematic_value"] == pytest.approx(1.8 * 1144.6 / 1289.2, rel=1e-4)
        assert row["spec_row"] == "internal" and row["testbench"] == "request"
        assert row["schematic_status"] == "pass"
        assert (
            row["status"]
            == row["extracted_status"]
            == row["device_only_status"]
            == "skipped"
        )
        assert (
            row["extracted_value"]
            is row["delta_pct"]
            is row["device_only_value"]
            is None
        )
        assert "error" not in row
    assert report["testbenches"][0]["measurement_names"] == (
        ["vout", "internal"] if port else ["internal"]
    )
    assert len(report["coverage"]["inapplicable"]) == 2
    assert {x["reason"] for x in report["coverage"]["inapplicable"]} == {
        "schematic_only_measurement"
    }
    assert len(report["coverage"]["checked"]) == (2 if port else 0)
    for leg in ("extracted", "device-only"):
        (written,) = outdir.glob(f"*.{leg}-request.json")
        assert [m["name"] for m in json.loads(written.read_text())["measurements"]] == (
            ["vout"] if port else []
        )


@pytest.mark.skipif(shutil.which("ngspice") is None, reason="needs ngspice")
@pytest.mark.parametrize(
    ("sided", "node", "side"),
    [
        (False, "xres.vref", "extracted"),
        (True, "xres.missing", "schematic"),
    ],
)
def test_requested_measurement_error_names_its_actual_side(tmp_path, sided, node, side):
    layout, request = _inputs(tmp_path, sided=sided, node=node)
    report = run_pex(layout, [str(request)], "sky130")
    assert report["status"] == "error"
    for row in report["delta"]:
        if row["measurement"] == "internal":
            assert row["status"] == "error" and row["error"]["side"] == side
        else:
            assert row["status"] == "pass"


def test_refuses_unsupported_selection_before_simulation(tmp_path):
    layout, request = _inputs(tmp_path, sides="extracted")
    with pytest.raises(PexError, match="internal.*sides"):
        run_pex(layout, [str(request)], "sky130")


@pytest.mark.skipif(shutil.which("ngspice") is None, reason="needs ngspice")
def test_limits_and_standalone_schematic_measurement(tmp_path, capsys):
    layout, request = _inputs(tmp_path, limits={"max": 0.01})
    assert main(["sim", str(request), "--format", "json"]) == 3
    sim = json.loads(capsys.readouterr().out)
    for corner in sim["corners"]:
        probe = next(m for m in corner["measurements"] if m["name"] == "internal")
        assert probe["value"] > 1 and probe["status"] == "fail"
    report = run_pex(layout, [str(request)], "sky130")
    assert report["status"] == "pass" and report["schematic_failed"] == 2
    content = json.loads(request.read_text())
    content["measurements"][0]["limits"] = {"max": 0.01}
    request.write_text(json.dumps(content))
    report = run_pex(layout, [str(request)], "sky130")
    assert report["status"] == "fail" and report["failed"] == 2


@pytest.mark.skipif(shutil.which("ngspice") is None, reason="needs ngspice")
def test_step_scoped_probe_uses_the_run_sim_measurement_name(tmp_path):
    layout, request = _inputs(tmp_path)
    content = json.loads(request.read_text())
    content["analysis_steps"] = [
        {
            "name": "pulse",
            "analysis": content.pop("analysis"),
            "measurements": content.pop("measurements"),
        }
    ]
    request.write_text(json.dumps(content))
    report = run_pex(layout, [str(request)], "sky130")
    assert report["status"] == "pass"
    assert {(row["measurement"], row["status"]) for row in report["delta"]} == {
        ("pulse.internal", "skipped"),
        ("pulse.vout", "pass"),
    }


def test_required_extracted_failure_keeps_probe_and_blames_port(tmp_path, monkeypatch):
    from klayout_tools import pex
    from klayout_tools.sim import SimError

    layout, request = _inputs(tmp_path)

    def run_leg(path, **kwargs):
        if "extracted-request" in str(path):
            raise SimError("worker failed", code="batch_job_failed")
        measurements = [
            {"name": n, "value": 1.3, "status": "pass"} for n in ("vout", "internal")
        ]
        return {
            "corners": [{"corner_id": "tt", "measurements": measurements}],
            "corner_count": 1,
            "measurements": measurements,
        }

    monkeypatch.setattr(pex, "run_sim", run_leg)
    report = run_pex(layout, [str(request)], "sky130")
    rows = {r["measurement"]: r for r in report["delta"]}
    assert rows["vout"]["error"]["side"] == "extracted" and report["status"] == "error"
    assert rows["internal"]["status"] == "skipped" and "error" not in rows["internal"]
    assert report["backend_failure"]["code"] == "batch_job_failed"
