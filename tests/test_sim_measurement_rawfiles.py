"""Sidecar readers distinguish absent, malformed, empty, and usable waveforms."""

import pytest

from klayout_tools.sim_measurement_kind import read_complex_rawfile, read_real_columns
from test_sim_transfer_meas import _rawfile


@pytest.mark.parametrize("case", ["missing", "malformed", "too_few", "empty", "valid"])
def test_sidecar_readers(tmp_path, case):
    path = tmp_path / "sidecar.raw"
    columns = 4 if case == "too_few" else 3
    if case == "malformed":
        path.write_text("not a waveform\n")
    elif case == "empty":
        path.write_text(_rawfile([], [], []))
    elif case != "missing":
        path.write_text(_rawfile([1, 2], [3 - 4j, -1j], [2, 3]))
    real, message = read_real_columns(
        str(path),
        columns=columns,
        name="gain",
        missing_error="absent",
        empty_error="empty",
    )
    raw, diagnostic = read_complex_rawfile(
        str(path),
        columns=columns,
        form="transfer",
        vector_desc="num or den",
        analysis_label="AC",
        expected_columns="a frequency axis, num and den",
    )
    if case == "valid":
        assert (message, diagnostic) == (None, None)
        assert real == [[1, 2], [5, 1], [2, 3]]
        assert raw.column(1) == [3 - 4j, -1j]
        return
    assert real is raw is None
    code, error = diagnostic
    if case == "missing":
        assert message == "absent"
        assert code == "transfer_vector_missing"
        assert "its num or den vector did not resolve against this AC analysis" in error
    elif case == "malformed":
        assert message.startswith("measurement 'gain': could not read its rawfile:")
        assert code == "transfer_raw_invalid"
        assert error.startswith("could not read its rawfile:")
    else:
        assert message == "empty"
        assert (code, error) == (
            "transfer_raw_invalid",
            "its rawfile must hold a frequency axis, num and den with samples",
        )
