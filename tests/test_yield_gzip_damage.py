"""`klt yield` reports a damaged gzip input through the error envelope (#5543).

Split out of `tests/test_yield.py` deliberately: that module is already far
over the per-file size ratchet's trusted ceiling (`file-size-baseline.json`),
so growth there is a CI failure. These tests belong to the **input-reader**
tier -- they need no `klt_yield_native` build and always run.

Three gzip failure modes matter here, and only the first was broken:

- a corrupt deflate stream raises `zlib.error`, which subclasses none of the
  exceptions `yield_analysis._load_json` used to catch, so it escaped as a
  raw traceback with no JSON on either stream;
- a truncated stream raises `EOFError`;
- a bad CRC trailer raises `gzip.BadGzipFile` (an `OSError`).

The last two already reported correctly, and are pinned here as a guard that
widening the `except` tuple did not change how they are reported.
"""

from __future__ import annotations

import gzip
import json

import pytest

from klayout_tools.cli import main
from klayout_tools.yield_analysis import YieldError, run_yield

_GZIP_DOC = {
    "measurements": [
        {
            "name": "m",
            "unit": "V",
            "samples": [0.0, 0.1, 0.2, 0.3],
            "limits": {"min": -1.0, "max": 1.0},
        }
    ]
}


def _valid_samples(tmp_path, doc=None):
    """Write an undamaged sample-set document and return its path."""
    path = tmp_path / "samples.json"
    path.write_text(json.dumps(_GZIP_DOC if doc is None else doc))
    return str(path)


def _damaged_gzip(path, doc=None):
    """Write a gzip file whose **deflate stream** is corrupt, and return its path.

    Deliberately not the two failure modes that already reported correctly:
    the 10-byte gzip header (magic included) is left intact so `_load_json`
    still routes the file through `gzip.open`, and the stream is damaged in
    place rather than cut short -- so zlib fails mid-stream with `zlib.error`
    instead of `EOFError` (truncation) or `BadGzipFile` (bad CRC), neither of
    which reproduces this issue.
    """
    raw = bytearray(
        gzip.compress(json.dumps(_GZIP_DOC if doc is None else doc).encode())
    )
    for i in range(10, 24):
        raw[i] ^= 0xFF
    path.write_bytes(bytes(raw))
    return str(path)


def test_a_damaged_gzip_samples_file_raises_yield_error(tmp_path):
    """The library surface converts a decompression failure like every other
    unreadable input: `run_yield` must never leak `zlib.error` to its caller."""
    path = _damaged_gzip(tmp_path / "samples.json.gz")
    with pytest.raises(YieldError, match="could not read samples"):
        run_yield(path)


def test_cli_a_damaged_gzip_samples_file_uses_the_error_envelope(tmp_path, capsys):
    path = _damaged_gzip(tmp_path / "samples.json.gz")
    assert main(["yield", path, "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["error"]["command"] == "yield"
    assert path in error["error"]["message"]


def test_cli_a_damaged_gzip_limits_file_uses_the_error_envelope(tmp_path, capsys):
    """The same must hold in the `--limits` position, which reaches
    `_load_json` through `_read_spec` rather than `_read_samples`."""
    samples = _valid_samples(tmp_path)
    limits = _damaged_gzip(
        tmp_path / "limits.json.gz", {"measurements": {"m": {"min": -1.0, "max": 1.0}}}
    )
    assert main(["yield", samples, "--limits", limits, "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["error"]["command"] == "yield"
    assert limits in error["error"]["message"]


@pytest.mark.parametrize("damage", ["truncated", "bad-crc"])
def test_cli_other_damaged_gzip_inputs_keep_their_existing_envelope(
    tmp_path, capsys, damage
):
    """Guard the two gzip failure modes that already produced an envelope
    before this fix: widening the `except` tuple must not change how a
    truncated stream or a bad CRC trailer is reported."""
    raw = bytearray(gzip.compress(json.dumps(_GZIP_DOC).encode()))
    if damage == "truncated":
        payload = bytes(raw[: len(raw) // 2])
    else:
        raw[-8] ^= 0xFF  # a CRC32 trailer byte, leaving the stream itself valid
        payload = bytes(raw)
    path = tmp_path / "samples.json.gz"
    path.write_bytes(payload)
    assert main(["yield", str(path), "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["error"]["command"] == "yield"
    assert str(path) in error["error"]["message"]
