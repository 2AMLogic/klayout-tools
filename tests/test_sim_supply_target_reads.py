"""Supply validation must inspect a coherent snapshot of each declaration file."""

from pathlib import Path

import pytest

from klayout_tools import sim


@pytest.mark.parametrize("param", ["", ".param vbias=1\n"])
def test_included_supply_is_read_once(tmp_path, monkeypatch, param):
    root = tmp_path / "root.sp"
    child = tmp_path / "rails.sp"
    root.write_text(f"* top\n{param}.include rails.sp\n")
    child.write_text("Vbias out 0 1\n")
    reads = []

    def count_open(path, *args, **kwargs):
        reads.append(Path(path))
        return open(path, *args, **kwargs)

    monkeypatch.setattr(sim, "open", count_open, raising=False)
    points = [sim.CornerPoint(None, {"Vbias": 1.5}, 27)]
    sim._validate_supply_override_targets(str(root), points)
    assert reads == [root, child]


def test_supply_error_precedence(tmp_path):
    root = tmp_path / "root.sp"
    root.write_text(
        ".param level=1\nVrail v 0 {level}\nVclock c 0 PULSE(0 1 0 1n 1n 1u 2u)\n"
    )
    targets = {"absent": 1, "level": 1, "Vclock": 1}
    for target, diagnostic in (
        ("Vclock", "explicit PULSE waveform"),
        ("level", "'vrail' reads 'level'"),
        ("absent", "names no element"),
    ):
        points = [sim.CornerPoint(None, targets.copy(), 27)]
        with pytest.raises(sim.SimError, match=diagnostic):
            sim._validate_supply_override_targets(str(root), points)
        del targets[target]
