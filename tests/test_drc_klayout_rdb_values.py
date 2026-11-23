"""Native KLayout findings retain non-geometry RDB values (#6823)."""

import json

import pytest

from klayout_tools.cli import main
from klayout_tools.drc_crosscheck import run_drc_crosscheck
from klayout_tools.drc_klayout_engine import _parse_klayout_rdb_report
from test_drc_crosscheck import _schema_validator
from test_drc_klayout_engine import (
    _stub_klayout_drc_subprocess,
    _write_deck_file,
    _write_gds,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _rdb(values):
    return f"""<report-database>
      <categories><category><name>metric</name></category></categories>
      <items><item><cell>TOP</cell><category>metric</category>{values}</item></items>
    </report-database>"""


def _values(*values):
    return "<values>" + "".join(f"<value>{s}</value>" for s in values) + "</values>"


def _parse(tmp_path, values):
    p = tmp_path / "findings.lyrdb"
    p.write_text(_rdb(values))
    violations, counts, categories = _parse_klayout_rdb_report(str(p), 0.001)
    assert counts == {"metric": 1} and categories == ["metric"]
    return violations[0]


def test_measured_values_verbatim_and_in_document_order(tmp_path):
    geometry = "polygon: (0,0;0,1;1,1;1,0)"
    row = _parse(
        tmp_path,
        _values(
            "float: 123.5",
            geometry,
            "text: 'ratio=456'",
            "[#RATIO] float: 16",
            "[#GATE_AREA] float: 1",
            " float: 0 ",
            "text: 'a &amp; b &lt; c'",
            "vendor: polygon: evidence",
            "float: 123.5",
        ),
    )
    assert row["values"] == [
        "float: 123.5",
        "text: 'ratio=456'",
        "[#RATIO] float: 16",
        "[#GATE_AREA] float: 1",
        " float: 0 ",
        "text: 'a & b < c'",
        "vendor: polygon: evidence",
        "float: 123.5",
    ]
    assert row["bbox"] == {"left": 0, "bottom": 0, "right": 1000, "top": 1000}
    assert row["polygon"] == [[0, 0], [0, 1000], [1000, 1000], [1000, 0]]


@pytest.mark.parametrize(
    "values",
    [
        "",
        "<values/>",
        "<values><value/></values>",
        _values(
            "box: (0,0;1,1)",
            "edge: (0,0;1,0)",
            "path: (0,0;1,0) w=0.2",
            "edge-pair: (0,0;0,1)|(1,0;1,1)",
            " polygon: (0,0;0,1;1,1)",
            "[gate] box: (0,0;1,1)",
            "['inner] region'] polygon: (0,0;0,1;1,1)",
        ),
    ],
)
def test_no_evidence_key_for_absent_empty_or_only_geometry(tmp_path, values):
    assert "values" not in _parse(tmp_path, values)


def test_both_embeds_values_on_native_side(tmp_path, monkeypatch):
    gds = _write_gds(tmp_path / "shape.gds")
    deck = _write_deck_file(tmp_path / "metric.lydrc")
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_rdb(_values("float: 123.5")))
    report = run_drc_crosscheck(gds, "sky130", deck)
    assert report["reports"]["native"]["violations"][0]["values"] == ["float: 123.5"]
    assert all("values" not in v for v in report["reports"]["curated"]["violations"])
    _schema_validator().validate(report)


@pytest.mark.parametrize(
    "old,new,expected",
    [
        (None, None, "match"),
        ("float: 123.5", "float: 123.5", "match"),
        ("float: 123.5", "float: 123.6", "drifted"),
    ],
)
def test_rerun_compares_values(tmp_path, monkeypatch, capsys, old, new, expected):
    (tmp_path / ".git").mkdir()
    gds = _write_gds(tmp_path / "shape.gds")
    deck = _write_deck_file(tmp_path / "metric.lydrc")

    def stub(metric):
        values = ["box: (0,0;1,1)"]
        if metric is not None:
            values.append(metric)
        _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_rdb(_values(*values)))

    stub(old)
    assert (
        main(
            ["drc", gds, "--engine", "klayout", "--deck-file", deck, "--format", "json"]
        )
        == 3
    )
    path = tmp_path / "committed.json"
    path.write_text(capsys.readouterr().out)
    stub(new)
    code = main(["drc", "--check", str(path), "--rerun", "--format", "json"])
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == expected
    if expected == "match":
        assert code == 0 and result["drift"] == []
    else:
        assert code == 3
        assert all(d["field"].startswith("violations") for d in result["drift"])
        assert result["fresh"]["violations"][0]["values"] == [new]
