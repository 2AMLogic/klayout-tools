"""SDC comparison and provenance for exception-backed timing evidence."""

import hashlib
import json
from pathlib import Path

import pytest

from klayout_tools.post_route_sta import run_sta
from klayout_tools.signoff import build_signoff
from test_post_route_sta import _setup_multi_corner_env, _setup_verilog_success_env
from test_sdc_check import _block
from test_signoff import STA_SINGLE_CORNER_CLEAN_ENVELOPE, _grade_sta_item_5, _write
from test_signoff_sta_constraints import (
    _CC_COMPLETE,
    _CC_INCOMPLETE,
    _sta_with_constraints,
)
from test_sta_multicycle_run import (
    _mcp_stdout,
    _multicycle_constraints,
    _stub_openroad_multicycle,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")
ENTRY = {"from": "bank/a[0]", "through": "mux/Z", "to": "ff*/D", "setup": 4, "hold": 3}
PAIR = [
    "set_multicycle_path -from [get_cells bank/a[0]] -through mux/Z"
    " -to [get_pins ff*/D] -setup 4",
    "set_multicycle_path 3 -hold -from bank/a[0] -to ff*/D -through [get_pins mux/Z]",
]


def drift(tmp_path, lines, entries):
    return _block(tmp_path, "\n".join(lines), clocks=[], multicycle=entries)


def test_sdc_multicycle_matching_and_mismatch_values(tmp_path):
    assert drift(tmp_path, PAIR, [ENTRY])["status"] == "match"
    assert drift(tmp_path, PAIR, [ENTRY])["unchecked"] == []
    (missing,) = drift(tmp_path, PAIR[:1], [ENTRY])["mismatches"]
    assert (missing["bound"], missing["sdc_value"], missing["request_value"]) == (
        "hold",
        None,
        3,
    )
    assert "line" not in missing
    extras = drift(tmp_path, PAIR, [])["mismatches"]
    assert [(r["line"], r["sdc_value"], r["request_value"]) for r in extras] == [
        (1, 4, None),
        (2, 3, None),
    ]
    changed = drift(tmp_path, PAIR, [{**ENTRY, "setup": 5, "hold": 1}])["mismatches"]
    assert [(r["bound"], r["sdc_value"], r["request_value"]) for r in changed] == [
        ("setup", 4, 5),
        ("hold", 3, 1),
    ]
    for field in ("from", "through", "to"):
        rows = drift(tmp_path, PAIR, [{**ENTRY, field: "different"}])["mismatches"]
        assert len(rows) == 4
        assert {r[field] for r in rows} == {ENTRY[field], "different"}
        assert all(
            (r["sdc_value"] is None) != (r["request_value"] is None) for r in rows
        )


def test_repeated_selection_and_optional_through(tmp_path):
    first = {"from": "a", "to": "b", "setup": 3, "hold": 2}
    second = {**first, "setup": 4}
    lines = [
        f"set_multicycle_path {n} -{side} -from a -to b"
        for n, side in [(4, "setup"), (2, "hold"), (3, "setup"), (2, "hold")]
    ]
    assert drift(tmp_path, lines, [first, second])["status"] == "match"
    (row,) = drift(tmp_path, lines[1:], [first, second])["mismatches"]
    assert (row["through"], row["request_value"], row["subject"]) == (None, 4, "a -> b")


@pytest.mark.parametrize(
    "args",
    [
        "2 -to x",
        "2 -start",
        "2 -end",
        "-setup 2 -rise_from y",
        "-setup 2 -reset_path",
        "-setup 2 -from {a b} -to x",
        "-setup 2 -from $a -to x",
        "-setup 2 -from a -to x;puts",
        "-setup 2 -from [get_cells a b] -to x",
        "-setup 2 -from [all_registers] -to x",
        "-setup 2 -from a -from b -to x",
        "-setup -hold 2 -from a -to b",
        "-setup 0 -from a -to b",
        "-hold 1.5 -from a -to b",
    ],
)
def test_unchecked_multicycle_suppresses_only_inferred_absence(tmp_path, args):
    out = drift(
        tmp_path, [PAIR[0], "set_multicycle_path " + args], [{**ENTRY, "setup": 5}]
    )
    (unchecked,) = out["unchecked"]
    assert unchecked["line"] == 2 and unchecked["reason"]
    (different,) = out["mismatches"]
    assert (different["line"], different["sdc_value"], different["request_value"]) == (
        1,
        4,
        5,
    )


@pytest.mark.parametrize(
    "prepare", [_setup_verilog_success_env, _setup_multi_corner_env]
)
def test_response_hashes_loaded_request_and_compares_sdc(
    tmp_path, monkeypatch, prepare
):
    path = Path(prepare(tmp_path, monkeypatch, constraints=_multicycle_constraints()))
    _stub_openroad_multicycle(monkeypatch, _mcp_stdout(5.4, -0.5, 2.3))
    request = json.loads(path.read_bytes())
    request["sdc_check"] = {"path": "constraints.sdc"}
    (tmp_path / "constraints.sdc").write_text(
        "create_clock -period 1 [get_ports clk]\n"
    )
    observed = []
    for field, value in [(None, None), ("from", "other/Q"), ("setup", 5)]:
        if field:
            request["constraints"]["multicycle_paths"][0][field] = value
        path.write_text(json.dumps(request, indent=2))
        report = run_sta(str(path))
        canonical = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        digest = report["provenance"]["request_sha256"]
        assert digest == hashlib.sha256(canonical).hexdigest()
        assert (
            len(
                [
                    r
                    for r in report["sdc_drift"]["mismatches"]
                    if r["command"] == "set_multicycle_path"
                ]
            )
            == 2
        )
        observed.append((digest, report["provenance"]["input"]["content_hash"]))
    assert len({d for d, _ in observed}) == 3 and len({h for _, h in observed}) == 1
    path.write_text(json.dumps(request, sort_keys=True))
    assert run_sta(str(path))["provenance"]["request_sha256"] == observed[-1][0]


def test_sta_citation_discloses_each_graded_corner(tmp_path):
    doc = _sta_with_constraints([_CC_COMPLETE, _CC_INCOMPLETE])
    doc["provenance"] = {**doc["provenance"], "request_sha256": "a" * 64}
    row = {"index": 0, **ENTRY}
    for c in doc["corners"]:
        c["multicycle_paths"] = [{**row, "matched_endpoints": 1}]
    expected = {
        "request_sha256": "a" * 64,
        "corners": [
            {"corner": c["corner"], "multicycle_paths": [row]} for c in doc["corners"]
        ],
    }
    detail = build_signoff([_write(tmp_path, "report.json", doc)])["checks"][0][
        "detail"
    ]
    assert detail["applied_exceptions"] == expected
    item = _grade_sta_item_5(tmp_path, doc)
    assert item["status"] == "met"
    assert item["detail"]["applied_exceptions"] == expected
    assert item["citation"]["detail"]["applied_exceptions"] == expected
    assert (
        _grade_sta_item_5(tmp_path, doc, require_complete_constraints=True)["status"]
        == "unmet"
    )


def test_flat_legacy_and_malformed_disclosure(tmp_path):
    def detail(doc):
        return build_signoff([_write(tmp_path, "flat.json", doc)])["checks"][0][
            "detail"
        ]

    doc = {**STA_SINGLE_CORNER_CLEAN_ENVELOPE}
    assert "applied_exceptions" not in detail(doc)
    assert "applied_exceptions" not in detail({**doc, "multicycle_paths": []})
    row = {"index": 0, **ENTRY}
    got = detail({**doc, "multicycle_paths": [row]})["applied_exceptions"]
    assert got == {
        "request_sha256": None,
        "corners": [{"corner": doc.get("corner"), "multicycle_paths": [row]}],
    }
    for malformed in (None, "bad", [{}], [False], [{**row, "setup": True}]):
        assert (
            detail({**doc, "multicycle_paths": malformed})["applied_exceptions"][
                "corners"
            ][0]["multicycle_paths"]
            is None
        )
