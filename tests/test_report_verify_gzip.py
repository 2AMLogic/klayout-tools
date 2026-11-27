"""Committed reports accept gzip by content and reject damaged streams (#5717)."""

import gzip
import json
import zlib

import klayout.db as kdb
import pytest

from helpers.extract_inverter_fixtures import _make_gf180mcu_inverter_layout
from klayout_tools import drc, erc, extract, lvs
from klayout_tools._report_verify import load_committed_report
from klayout_tools.cli import main

_VERBS = [
    ("extract", extract.ExtractError),
    ("drc", drc.DrcError),
    ("lvs", lvs.LvsError),
    ("erc", erc.ErcError),
]
_GZIP = gzip.compress(b"{}", mtime=0)
_BAD_REPORTS = [
    pytest.param(_GZIP[:12], EOFError, "could not read", id="truncated"),
    # A valid gzip header followed by DEFLATE's reserved block type (BTYPE=3).
    pytest.param(_GZIP[:10] + b"\x07", zlib.error, "could not read", id="deflate"),
    pytest.param(
        _GZIP[:-8] + bytes([_GZIP[-8] ^ 1]) + _GZIP[-7:],
        gzip.BadGzipFile,
        "could not read",
        id="crc",
    ),
    pytest.param(b"\xff\xfe{}", UnicodeDecodeError, "could not read", id="non-utf8"),
    pytest.param(
        gzip.compress(b"\xff\xfe{}"),
        UnicodeDecodeError,
        "could not read",
        id="gzip-non-utf8",
    ),
    pytest.param(b"", json.JSONDecodeError, "not valid JSON", id="empty"),
    pytest.param(b"\x1f", json.JSONDecodeError, "not valid JSON", id="one-byte"),
    pytest.param(
        gzip.compress(b"{invalid"),
        json.JSONDecodeError,
        "not valid JSON",
        id="gzip-invalid-json",
    ),
    pytest.param(gzip.compress(b"[]"), None, "must be a JSON object", id="array"),
]


@pytest.mark.parametrize("verb,error_cls", _VERBS)
@pytest.mark.parametrize("payload,cause,message", _BAD_REPORTS)
def test_bad_committed_report_raises_callers_error(
    tmp_path, verb, error_cls, payload, cause, message
):
    path = tmp_path / f"{verb}.json.gz"
    path.write_bytes(payload)
    with pytest.raises(error_cls, match=message) as caught:
        load_committed_report(str(path), error_cls)
    assert str(path) in str(caught.value)
    if cause is not None:
        assert isinstance(caught.value.__cause__, cause)


@pytest.mark.parametrize("verb,error_cls", _VERBS)
@pytest.mark.parametrize("payload,cause,message", _BAD_REPORTS)
@pytest.mark.parametrize("output_format", ["text", "json"])
def test_bad_committed_report_cli_error(
    tmp_path, capsys, verb, error_cls, payload, cause, message, output_format
):
    path = tmp_path / f"{verb}.json.gz"
    path.write_bytes(payload)
    assert main([verb, "--check", str(path), "--format", output_format]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "Traceback" not in output.err
    assert str(path) in output.err
    assert message in output.err
    if output_format == "json":
        envelope = json.loads(output.err)
        assert envelope["schema_version"] == 1
        assert envelope["error"]["command"] == verb
        assert str(path) in envelope["error"]["message"]


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("name", ["report.json", "report.json.gz"])
def test_committed_report_compression_is_detected_by_content(
    tmp_path, compressed, name
):
    payload = b'{"status":"match","label":"\xc2\xb5"}'
    path = tmp_path / name
    path.write_bytes(gzip.compress(payload) if compressed else payload)
    assert load_committed_report(str(path), extract.ExtractError) == json.loads(payload)


@pytest.fixture(params=[verb for verb, _ in _VERBS])
def committed_report(request, tmp_path):
    """Generate actual engine reports, with inputs anchored in their own repo."""
    (tmp_path / ".git").mkdir()
    verb = request.param
    if verb == "lvs":
        layout_path = tmp_path / "layout.spice"
        reference_path = tmp_path / "reference.spice"
        netlist = ".subckt TOP A B\nR1 A B 1000\n.ends TOP\n"
        layout_path.write_text(netlist)
        reference_path.write_text(netlist)
        report = lvs.run_lvs(
            json.dumps(
                {
                    "layout": {"netlist": str(layout_path), "top": "TOP"},
                    "reference": {"netlist": str(reference_path), "top": "TOP"},
                }
            )
        )
    elif verb == "erc":
        layout_path = tmp_path / "layout.gds"
        layout = kdb.Layout()
        top = layout.create_cell("TOP")
        top.shapes(layout.layer(1, 0)).insert(kdb.Box(0, 0, 1000, 2000))
        layout.write(str(layout_path))
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(
            json.dumps(
                {
                    "stackup": [
                        {"name": "poly", "layer": "1/0", "role": "gate"},
                        {"name": "metal", "layer": "2/0"},
                    ],
                    "vias": [
                        {
                            "name": "contact",
                            "layer": "3/0",
                            "between": ["poly", "metal"],
                        }
                    ],
                }
            )
        )
        report = erc.run_erc(str(layout_path), str(spec_path))
    else:
        layout_path = tmp_path / "layout.gds"
        _make_gf180mcu_inverter_layout().write(str(layout_path))
        if verb == "extract":
            report = extract.run_extract(
                str(layout_path), "gf180mcu", output=str(tmp_path / "layout.spice")
            )
        else:
            report = drc.run_drc(str(layout_path), "gf180mcu")
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report))
    return verb, layout_path, report_path


@pytest.mark.parametrize("drifted", [False, True])
@pytest.mark.parametrize("name", ["report.json.gz", "renamed.json"])
def test_cli_gzip_report_verifies_like_plain_report(
    committed_report, capsys, drifted, name
):
    verb, layout_path, plain = committed_report
    compressed = plain.with_name(name)
    compressed.write_bytes(gzip.compress(plain.read_bytes()))
    if drifted:
        layout_path.write_bytes(layout_path.read_bytes() + b"\n")
    results = []
    for path in (plain, compressed):
        assert main([verb, "--check", str(path), "--format", "json"]) == (
            3 if drifted else 0
        )
        output = capsys.readouterr()
        assert output.err == ""
        result = json.loads(output.out)
        assert result["status"] == ("drifted" if drifted else "match")
        assert result.pop("report") == str(path)
        results.append(result)
    assert results[0] == results[1]
