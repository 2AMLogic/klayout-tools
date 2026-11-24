"""Per-draw CSV metadata, record boundaries and source provenance (issue #7019)."""

import hashlib
import json

import pytest

from klayout_tools.cli import main


def _run(path, capsys, group="stage"):
    code = main(
        [
            "yield-csv",
            str(path),
            "--group-by",
            group,
            "--value",
            "value",
            "--format",
            "json",
        ]
    )
    streams = capsys.readouterr()
    if code:
        assert streams.out == ""
        return code, json.loads(streams.err)["error"]
    return code, json.loads(streams.out)


@pytest.mark.parametrize(
    ("prefix", "newline", "bom"),
    [
        ("", "\n", b""),
        ("# campaign: demo\n", "\n", b""),
        ('# campaign: demo\n# unmatched " metadata,ignored\n', "\r\n", b"\xef\xbb\xbf"),
    ],
)
def test_csv_preamble_samples_and_whole_file_digest(
    tmp_path, monkeypatch, capsys, prefix, newline, bom
):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "draws.csv"
    body = "stage,run,value\nfoo,1,1.23\nfoo,2,1.31\n"
    digests = []
    for metadata in (prefix, "# revised metadata\n" + prefix):
        content = bom + (metadata + body).replace("\n", newline).encode()
        path.write_bytes(content)
        code, result = _run("draws.csv", capsys)
        assert code == 0
        assert [(m["name"], m["samples"]) for m in result["measurements"]] == [
            ("foo", [1.23, 1.31])
        ]
        info = result["derivation"]["csv"]
        assert (info["path"], info["rows"]) == ("draws.csv", 2)
        assert info["content_hash"] == "sha256:" + hashlib.sha256(content).hexdigest()
        digests.append(info["content_hash"])
    assert digests[0] != digests[1]


@pytest.mark.parametrize(
    "tail", ["# trailing,9\nfoo,2\n", "# trailing,9", "\n# third body line\n"]
)
def test_csv_body_comment_is_an_error(tmp_path, capsys, tail):
    path = tmp_path / "draws.csv"
    path.write_text("stage,value\nfoo,1\n" + tail)
    code, error = _run(path, capsys)
    assert code == 1
    assert error["command"] == "yield-csv"
    assert "unexpected comment after the header" in error["message"]


def test_csv_hashes_in_quoted_and_multiline_fields(tmp_path, capsys):
    path = tmp_path / "draws.csv"
    path.write_bytes(
        b'# metadata\r\n"#stage",value\r\n"# first",1\r\n'
        b'"a""b\r\n# continuation",2\r\nwithin#field,3\r\n'
    )
    code, result = _run(path, capsys, "#stage")
    assert code == 0
    assert [(m["name"], m["samples"]) for m in result["measurements"]] == [
        ("# first", [1.0]),
        ('a"b\r\n# continuation', [2.0]),
        ("within#field", [3.0]),
    ]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("# metadata only", "no header row"),
        ("# metadata\nstage,value\n", "has a header but no data rows"),
    ],
)
def test_csv_preamble_without_draws(tmp_path, capsys, content, message):
    path = tmp_path / "empty.csv"
    path.write_text(content)
    code, error = _run(path, capsys)
    assert code == 1 and message in error["message"]
