"""Tests for ``hints.layout_short_nets`` (issue #5632).

The layout-side dual of ``hints.short_nets`` (#2653): a caller-asserted,
warning-disclosed merge of named layout nets on the layout's selected top
circuit, applied before the comparer is built -- before ``flatten_layout``,
the ``combine_devices`` steps and the gate-level power-only prune. Its
reason to exist is the abstract-``--abstract-cell-lef`` substrate case: the
deck's synthesized substrate global (``vsubs``) is an isolated node when the
tap cell is itself abstracted, so a PG-pin reference that ties body pins to
the rails can never match without the caller asserting the tie.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools.cli import main
from klayout_tools.lvs import LvsError, run_lvs
from klayout_tools.lvs_layout_short_nets import (
    CATEGORY_HINTS_LAYOUT_SHORT_NETS_APPLIED,
    parse_layout_short_nets,
)

# Layout whose NMOS bulk sits on the isolated synthesized substrate global
# (as a LEF-merged abstract-cell body pin does, issue #4571's unprobed
# binding), with the real ground rail as a separate net.
_LAYOUT = """
.subckt inv A Y VGND vsubs
M1 Y A VGND vsubs nfet L=0.15U W=0.65U
.ends
"""
# The PG-pin reference form: body tied to the rail, no substrate node at all.
_REFERENCE = """
.subckt inv A Y VGND
M1 Y A VGND VGND nfet L=0.15u W=0.65u
.ends
"""

_HINT = {"layout_short_nets": {"nets": [["vsubs", "VGND"]]}}


def _request(tmp_path, hints=None, **kw):
    layout = tmp_path / "layout.spice"
    layout.write_text(_LAYOUT)
    reference = tmp_path / "ref.spice"
    reference.write_text(_REFERENCE)
    request = {
        "layout": {"netlist": str(layout), "top": "inv"},
        "reference": {"netlist": str(reference), "top": "inv", "form": "subckt-call"},
    }
    if hints is not None:
        request["hints"] = hints
    request.update(kw)
    return json.dumps(request)


def test_baseline_without_hint_reports_the_isolated_substrate(tmp_path):
    report = run_lvs(_request(tmp_path))
    assert report["status"] == "mismatch"
    assert not [
        m
        for m in report["mismatches"]
        if m["category"] == CATEGORY_HINTS_LAYOUT_SHORT_NETS_APPLIED
    ]
    # The isolated layout node the hint exists to tie away.
    assert [
        m
        for m in report["mismatches"]
        if m["category"] == "net.unmatched"
        and (m["net"] or {}).get("layout") == "VSUBS"
    ]


def test_merge_matches_and_discloses(tmp_path):
    report = run_lvs(_request(tmp_path, _HINT))
    assert report["status"] == "match"
    (entry,) = [
        m
        for m in report["mismatches"]
        if m["category"] == CATEGORY_HINTS_LAYOUT_SHORT_NETS_APPLIED
    ]
    assert entry["severity"] == "warning"
    assert entry["side"] == "layout"
    assert entry["details"]["members"] == ["vsubs", "VGND"]
    # The pipe-joined spelling klt extract's merged_net_labels already uses.
    assert entry["details"]["merged_net"] == "VGND|VSUBS"
    assert "asserted by the request" in entry["description"]


def test_disclosure_only_finding_is_match_with_nonzero_mismatch_count(tmp_path):
    report = run_lvs(_request(tmp_path, _HINT))
    assert report["status"] == "match"
    assert report["mismatch_count"] == 1
    assert report["error_count"] == 0


def test_absent_key_is_byte_identical(tmp_path):
    absent = run_lvs(_request(tmp_path))
    empty = run_lvs(_request(tmp_path, {}))
    for report in (absent, empty):
        report.pop("hints", None)
    assert absent == empty


def test_merge_survives_flatten_layout(tmp_path):
    report = run_lvs(_request(tmp_path, _HINT, options={"flatten_layout": True}))
    assert report["status"] == "match"
    assert [
        m
        for m in report["mismatches"]
        if m["category"] == CATEGORY_HINTS_LAYOUT_SHORT_NETS_APPLIED
    ]


def test_duplicate_name_within_one_group_is_noop(tmp_path):
    report = run_lvs(
        _request(
            tmp_path, {"layout_short_nets": {"nets": [["vsubs", "VGND", "vsubs"]]}}
        )
    )
    assert report["status"] == "match"
    (entry,) = [
        m
        for m in report["mismatches"]
        if m["category"] == CATEGORY_HINTS_LAYOUT_SHORT_NETS_APPLIED
    ]
    assert entry["details"]["members"] == ["vsubs", "VGND", "vsubs"]


def test_unresolvable_net_name_is_application_error(tmp_path):
    with pytest.raises(LvsError, match="'nope' not found"):
        run_lvs(_request(tmp_path, {"layout_short_nets": {"nets": [["nope", "VGND"]]}}))


def test_refolding_an_already_joined_net_is_application_error(tmp_path):
    with pytest.raises(LvsError, match="not found"):
        run_lvs(
            _request(
                tmp_path,
                {"layout_short_nets": {"nets": [["vsubs", "VGND"], ["vsubs", "VGND"]]}},
            )
        )


@pytest.mark.parametrize(
    "spec",
    [
        {},
        {"devices": ["S1"]},
        {"nets": [["only-one"]]},
        {"nets": ["VGND"]},
        {"nets": [["VGND", 3]]},
    ],
)
def test_malformed_spec_is_request_error(tmp_path, spec):
    with pytest.raises(LvsError, match="hints.layout_short_nets"):
        run_lvs(_request(tmp_path, {"layout_short_nets": spec}))


def test_non_object_value_is_request_error():
    assert parse_layout_short_nets({}, "klayout") is None
    with pytest.raises(LvsError, match="must be a JSON object"):
        parse_layout_short_nets({"layout_short_nets": "junk"}, "klayout")


def test_netgen_engine_is_application_error(tmp_path):
    with pytest.raises(LvsError, match="engine 'klayout'"):
        run_lvs(_request(tmp_path, _HINT, engine="netgen"))


def test_cli_unresolvable_name_exits_one_with_error_envelope(tmp_path, capsys):
    layout = tmp_path / "layout.spice"
    layout.write_text(_LAYOUT)
    reference = tmp_path / "ref.spice"
    reference.write_text(_REFERENCE)
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "layout": {"netlist": str(layout), "top": "inv"},
                "reference": {
                    "netlist": str(reference),
                    "top": "inv",
                    "form": "subckt-call",
                },
                "hints": {"layout_short_nets": {"nets": [["nope", "VGND"]]}},
            }
        )
    )
    exit_code = main(["lvs", str(request), "--format", "json"])
    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["command"] == "lvs"
    assert "layout_short_nets" in err["error"]["message"]


def test_cli_hinted_match_exits_zero(tmp_path, capsys):
    layout = tmp_path / "layout.spice"
    layout.write_text(_LAYOUT)
    reference = tmp_path / "ref.spice"
    reference.write_text(_REFERENCE)
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "layout": {"netlist": str(layout), "top": "inv"},
                "reference": {
                    "netlist": str(reference),
                    "top": "inv",
                    "form": "subckt-call",
                },
                "hints": {"layout_short_nets": {"nets": [["vsubs", "VGND"]]}},
            }
        )
    )
    exit_code = main(["lvs", str(request), "--format", "json"])
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "match"
