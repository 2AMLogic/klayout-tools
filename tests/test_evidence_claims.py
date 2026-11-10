"""`klt evidence claims` / `klayout_tools.evidence_claims` (issue #5273)."""

from __future__ import annotations

import json

import pytest

from klayout_tools import evidence_claims as ec
from klayout_tools import evidence_lint as el
from klayout_tools.cli import main

OLD = "evidence/sim/blk/full/20260101-000000-aaaaaaa.json"
NEW = "evidence/sim/blk/full/20260201-000000-bbbbbbb.json"


def _w(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def root(tmp_path):
    _w(tmp_path, OLD, json.dumps({"supersedes": []}))
    _w(
        tmp_path,
        NEW,
        json.dumps(
            {
                "supersedes": [OLD],
                "result": {
                    "corners": [
                        {"corner_id": "tt", "gain": [1.2345, 2.0]},
                        {"corner_id": "ss", "gain": 0.5},
                    ]
                },
            }
        ),
    )
    return tmp_path


def _run(root, doc_text, registry=None, docs=("*.md",)):
    _w(root, "README.md", doc_text)
    return ec.claims(str(root), list(docs), registry)


def _codes(payload):
    return [f["code"] for f in payload["findings"]]


def test_current_citation_is_clean(root):
    payload = _run(root, f"See `{NEW}`.\n")
    assert payload["status"] == "clean"
    assert payload["citations_checked"] == 1


def test_missing_path(root):
    payload = _run(root, "See evidence/sim/blk/full/20260301-000000-ccccccc.json\n")
    assert _codes(payload) == [ec.F_CITE_MISSING]


def test_superseded_names_head(root):
    payload = _run(root, f"See {OLD}\n")
    assert _codes(payload) == [ec.F_SUPERSEDED]
    assert NEW in payload["findings"][0]["message"]


def test_historical_paragraph_marker(root):
    payload = _run(root, f"<!-- klt:historical -->\nOld: {OLD}\n\nNow: {NEW}\n")
    assert payload["status"] == "clean"


def test_historical_marker_does_not_leak_to_next_paragraph(root):
    payload = _run(root, f"<!-- klt:historical -->\nx\n\nStill {OLD}\n")
    assert _codes(payload) == [ec.F_SUPERSEDED]


def test_historical_heading_section(root):
    text = f"## Old\n<!-- klt:historical -->\n\nA {OLD}\n\n## New\n\nB {OLD}\n"
    payload = _run(root, text)
    assert [f["message"].split()[1] for f in payload["findings"]] == ["8"]


def test_fork_cites_either_tip_current(root):
    _w(
        root,
        "evidence/sim/blk/full/20260202-000000-ddddddd.json",
        json.dumps({"supersedes": [OLD]}),
    )
    payload = _run(root, f"{NEW}\n")
    assert payload["status"] == "clean"


def _reg(**row):
    base = {
        "literal": "1.23",
        "record": NEW,
        "pointer": "/gain",
        "corner": "tt",
        "agg": "min",
        "decimals": 2,
    }
    base.update(row)
    return {"mapping": [], "claims": [base]}


def test_registry_match_min_and_max(root):
    assert _run(root, "gain 1.23\n", _reg())["status"] == "clean"
    assert (
        _run(root, "gain 2.0\n", _reg(literal="2.0", agg="max", decimals=1))["status"]
        == "clean"
    )


def test_registry_mismatch(root):
    payload = _run(root, "gain 9.99\n", _reg(literal="9.99"))
    assert _codes(payload) == [ec.F_MISMATCH]


def test_registry_scale_and_decimals(root):
    reg = _reg(literal="0.5", corner="ss", pointer="/gain", scale=1000, decimals=0)
    payload = _run(root, "x 0.5\n", reg)
    assert _codes(payload) == [ec.F_MISMATCH]
    reg = _reg(literal="500", corner="ss", pointer="/gain", scale=1000, decimals=0)
    assert _run(root, "x 500\n", reg)["status"] == "clean"


def test_registry_literal_not_in_doc_and_unresolved(root):
    assert ec.F_LITERAL_MISSING in _codes(_run(root, "nothing\n", _reg()))
    payload = _run(root, "gain 1.23\n", _reg(pointer="/nope"))
    assert ec.F_UNRESOLVED in _codes(payload)
    payload = _run(root, "gain 1.23\n", _reg(agg=None))  # list without agg
    assert ec.F_UNRESOLVED in _codes(payload)


def test_legacy_mapping_superseded_and_twin(root):
    _w(root, "records/a.md", "old")
    _w(root, "records/a.json", "{}")
    _w(root, "records/b.md", "new")
    reg = {
        "mapping": {
            "records": [
                {"path": "records/a.md", "chain": "x"},
                {"path": "records/b.md", "chain": "x", "supersedes": ["records/a.md"]},
            ]
        },
        "claims": [],
    }
    p = _w(root, "reg.json", json.dumps(reg))
    registry = ec.load_registry(str(p))
    payload = _run(root, "see records/a.md and records/b.md\n", registry)
    assert sorted(_codes(payload)) == [ec.F_SUPERSEDED, ec.F_TWIN_MISSING]


def test_no_docs_match_is_error(root):
    with pytest.raises(ec.EvidenceClaimsError) as exc:
        ec.claims(str(root), ["nothing/*.md"])
    assert exc.value.code == ec.CODE_NO_DOCS


def test_unreadable_registry_and_record_fail_closed(root, tmp_path):
    with pytest.raises(ec.EvidenceClaimsError):
        ec.load_registry(str(tmp_path / "missing.json"))
    p = _w(root, "bad.json", "{")
    with pytest.raises(ec.EvidenceClaimsError):
        ec.load_registry(str(p))
    _w(root, "evidence/sim/blk/full/20260301-000000-ccccccc.json", "{not json")
    with pytest.raises(ec.EvidenceClaimsError):
        _run(root, "x\n")


def test_registry_unknown_keys_rejected(tmp_path):
    p = _w(tmp_path, "r.json", json.dumps({"bogus": 1}))
    with pytest.raises(ec.EvidenceClaimsError):
        ec.load_registry(str(p))


def test_current_heads_helper():
    heads = el.current_heads({"b": ["a"], "a": [], "c": ["b"]})
    assert heads["a"] == ["c"] and heads["c"] == ["c"]


def test_cli_json_and_exit_codes(root, capsys):
    _w(root, "README.md", f"{OLD}\n")
    rc = main(["evidence", "claims", str(root), "--docs", "*.md", "--format", "json"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["findings"][0]["code"] == ec.F_SUPERSEDED
    _w(root, "README.md", f"{NEW}\n")
    assert main(["evidence", "claims", str(root), "--docs", "*.md"]) == 0
    assert main(["evidence", "claims", str(root), "--docs", "zzz*"]) == 1
