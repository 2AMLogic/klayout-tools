"""Malformed and partially readable ASCII waveform artifacts."""

from io import StringIO

import pytest

from klayout_tools import sim_raw
from klayout_tools.sim_errors import SimError


def test_waveform_ignores_stray_variable_and_value_lines(tmp_path):
    raw = tmp_path / "dc.raw"
    raw.write_text(
        "Plotname: DC\nNo. Variables: 1\nVariables:\n"
        "unused entry\n0 v(out) voltage\nValues:\n"
        "stray row contents\n0 0.75\n"
    )

    waveform = sim_raw.parse_ascii_rawfile(str(raw))

    assert waveform["variables"] == [{"index": 0, "name": "v(out)", "type": "voltage"}]
    assert waveform["points"] == [[0.75]]


@pytest.mark.parametrize(
    ("header", "error"),
    [
        ("Variables:\n0 v(out) voltage\nValues:\n", "no variable count"),
        ("No. Variables: 1\nVariables:\n0 v(out) voltage\n", "no Variables/Values"),
        (
            "No. Variables: 2\nVariables:\n0 v(out) voltage\nValues:\n",
            "declared 2 variables, parsed 1",
        ),
    ],
)
def test_waveform_rejects_incomplete_header(tmp_path, header, error):
    raw = tmp_path / "incomplete.raw"
    raw.write_text("Plotname: DC\n" + header)
    with pytest.raises(SimError, match=error):
        sim_raw.parse_ascii_rawfile(str(raw))


def test_waveform_reports_read_error_after_opening(monkeypatch):
    class BrokenStream(StringIO):
        def __next__(self):
            raise OSError("waveform read interrupted")

    stream = BrokenStream()
    monkeypatch.setattr(sim_raw, "open", lambda *args, **kwargs: stream, raising=False)
    with pytest.raises(
        SimError, match="could not read rawfile: waveform read interrupted"
    ):
        sim_raw.parse_ascii_rawfile("capture.raw")
    assert stream.closed
