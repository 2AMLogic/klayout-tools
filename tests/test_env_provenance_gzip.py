"""Compressed evidence should yield the same privacy findings as its source."""

import gzip
import json

import pytest

from klayout_tools import env_provenance as ep
from klayout_tools import env_provenance_json_stream as stream
from klayout_tools.cli import main

_READERS = (
    ep.scan_files,
    ep.lint_envelope_files,
    stream.lint_envelope_files_json_stream,
)


@pytest.mark.parametrize("read", _READERS)
@pytest.mark.parametrize(
    "value", ["build/result.spice", "/home/sample/build/result.spice"]
)
@pytest.mark.parametrize("name", ["report.json.gz", "renamed.json"])
def test_compressed_file_preserves_findings(tmp_path, read, value, name):
    path = tmp_path / name
    source = json.dumps({"netlist": value}, indent=2).encode()
    path.write_bytes(source)
    expected = read([str(path)])
    path.write_bytes(gzip.compress(source))
    assert read([str(path)]) == expected
    assert expected["files"][0]["file"] == str(path)
    assert (expected["status"] == "clean") == (value == "build/result.spice")
    if read is ep.scan_files and value.startswith("/"):
        assert expected["files"][0]["leaks"][0]["line"] == 2


@pytest.mark.parametrize("read", _READERS)
@pytest.mark.parametrize("damage", ["short", "bad_crc", "invalid_deflate"])
def test_damaged_stream_is_a_read_error(tmp_path, read, damage):
    path = tmp_path / "report.json"
    data = bytearray(gzip.compress(b'{"netlist":"build/ref.spice"}'))
    if damage == "short":
        del data[-4:]
    elif damage == "bad_crc":
        data[-8] ^= 1
    else:
        data[10] |= 7  # invalid DEFLATE block type
    path.write_bytes(data)
    with pytest.raises(ep.EnvironmentProvenanceError, match="could not read"):
        read([str(path)])


def test_plain_non_utf8_text_keeps_scanning(tmp_path):
    file = tmp_path / "build.log"
    file.write_bytes(b"\xff\n/home/sample/build/result.spice\n")
    assert ep.scan_files([str(file)])["files"][0]["leaks"][0]["line"] == 2


def test_lint_modes_keep_their_document_boundaries(tmp_path):
    path = tmp_path / "results.json.gz"
    path.write_bytes(
        gzip.compress(b'{"netlist":"a.sp"}\n{"netlist":"/home/sample/b.sp"}')
    )
    with pytest.raises(ep.EnvironmentProvenanceError, match="not valid JSON"):
        ep.lint_envelope_files([str(path)])
    (finding,) = stream.lint_envelope_files_json_stream([str(path)])["files"][0][
        "findings"
    ]
    assert finding["document"] == 1


@pytest.mark.parametrize(
    "mode", [["scan"], ["lint-envelope"], ["lint-envelope", "--json-stream"]]
)
def test_cli_damage_exits_with_error(tmp_path, capsys, mode):
    path = tmp_path / "damaged.json.gz"
    path.write_bytes(gzip.compress(b"{}")[:-1])
    assert main(["env-provenance", *mode, str(path), "--format", "json"]) == 1
    capture = capsys.readouterr()
    assert not capture.out
    assert "could not read" in json.loads(capture.err)["error"]["message"]
