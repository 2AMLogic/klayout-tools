"""The shipped worked example requests must pass ``klt sim`` request validation."""

import json
import pathlib
import re
import subprocess

from klayout_tools import sim

EXAMPLES = pathlib.Path(__file__).resolve().parent.parent / "examples" / "sim"


def test_monte_carlo_instances_example_validates_offline(tmp_path, monkeypatch):
    # The example needs sky130A, which CI does not have: stand a stub library
    # in at the path `models.lib` resolves to, and stub the engine out.
    lib = tmp_path / "pdk" / "sky130A" / "libs.tech" / "ngspice" / "sky130.lib.spice"
    lib.parent.mkdir(parents=True)
    lib.write_text("* stub sky130 model library\n")
    monkeypatch.setenv("PDK_ROOT", str(tmp_path / "pdk"))

    real_run = subprocess.run

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        if "-o" not in cmd:
            return real_run(
                cmd, capture_output=capture_output, text=text, timeout=timeout, cwd=cwd
            )
        with open(cmd[cmd.index("-o") + 1], "w", encoding="utf-8") as handle:
            handle.write("")
        return subprocess.CompletedProcess(cmd, 0, "** ngspice-99\n", "")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)

    request = EXAMPLES / "request-monte-carlo-instances.json"
    report = sim.run_sim(str(request))

    gating = report["environment"]["monte_carlo"]["instance_mismatch"]
    assert {r["instance"]: r["randomised"] for r in gating["instances"]} == {
        "XM1": True,
        "XM2": True,
        "XM3": False,
    }


def test_monte_carlo_instances_example_uses_validated_shapes():
    doc = json.loads((EXAMPLES / "request-monte-carlo-instances.json").read_text())

    assert doc["analysis"].keys() >= {"kind", "args"}
    assert doc["models"]["lib"]
    for meas in doc["measurements"]:
        assert "expr" in meas
        assert not re.match(r"\s*\.meas\s+op\b", meas.get("spice", ""))
