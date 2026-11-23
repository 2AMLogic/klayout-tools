"""Issue #6969: the ``joint_grid`` spec-row form on ``klt signoff``.

A coverage-style row -- "every implemented bench exercised the full
ratified PVT grid, with the supply and temperature verified as applied" --
is a property of a *set* of envelopes, one per bench. The ``joint_grid``
row derives the row from the named members' ``corners[]`` plus one
condition probe (a passing, ``limits``-bearing measurement tagged with the
row id, the #3778 tag), so the joint claim is never asserted on one
bench's evidence. These cover the grading hook, the refusal edges, the
``require_derived_rows`` interplay and the label/matching rules; the
per-spec-row table's projection is in ``test_signoff_joint_rows_table.py``
and the shared fixtures in ``helpers/signoff_joint_fixtures.py``. Fixture
conventions mirror ``test_signoff.py`` (hand-built JSON envelopes).
"""

from __future__ import annotations

import json

import pytest

from helpers.signoff_joint_fixtures import (
    _GRID,
    _grid_envelope,
    _item5,
    _joint_members,
    _write,
)
from klayout_tools.signoff import build_tier_report
from klayout_tools.signoff_joint_rows import (
    _point_label,
    citation_label,
    member_shortfall,
    required_grid_points,
)

# --------------------------------------------------------------------------- #
# The covered and the shortfall outcomes.
# --------------------------------------------------------------------------- #


def test_two_full_members_with_probes_grade_derived_and_met(tmp_path):
    a = _write(tmp_path, "a.json", _grid_envelope())
    b = _write(tmp_path, "b.json", _grid_envelope())

    item = _item5(
        {"5": [{"file": a}, {"file": b}]},
        [{"id": "pvt_grid", "joint_grid": _joint_members(a, b)}],
    )

    assert item["status"] == "met"
    assert item["row_coverage"]["row_sources"] == {"pvt_grid": "derived"}
    assert item["row_coverage"]["uncovered_rows"] == []
    assert "joint_rows" not in item["row_coverage"]


def test_a_member_missing_one_grid_point_lists_it(tmp_path):
    a = _write(tmp_path, "a.json", _grid_envelope())
    b = _write(tmp_path, "b.json", _grid_envelope(drop=("ff", 1.98, -40)))

    item = _item5(
        {"5": [{"file": a}, {"file": b}]},
        [{"id": "pvt_grid", "joint_grid": _joint_members(a, b)}],
    )

    assert item["status"] == "unmet"
    assert item["reason"] == "partial_coverage"
    assert item["detail"]["uncovered_rows"] == ["pvt_grid"]
    assert item["detail"]["joint_rows"] == [
        {
            "row": "pvt_grid",
            "member": b,
            "missing_points": ["ff/1.980V/-40C"],
            "missing_probe": False,
        }
    ]


def test_a_member_with_one_failing_corner_lists_the_point(tmp_path):
    a = _write(tmp_path, "a.json", _grid_envelope())
    b = _write(tmp_path, "b.json", _grid_envelope(fail=("tt", 1.62, 27)))

    item = _item5(
        {"5": [{"file": a}, {"file": b}]},
        [{"id": "pvt_grid", "joint_grid": _joint_members(a, b)}],
    )

    assert item["reason"] == "partial_coverage"
    assert item["detail"]["joint_rows"][0]["missing_points"] == ["tt/1.620V/27C"]


def test_a_member_with_no_probe_lists_missing_probe(tmp_path):
    a = _write(tmp_path, "a.json", _grid_envelope())
    b = _write(tmp_path, "b.json", _grid_envelope(probe=False))

    item = _item5(
        {"5": [{"file": a}, {"file": b}]},
        [{"id": "pvt_grid", "joint_grid": _joint_members(a, b)}],
    )

    assert item["reason"] == "partial_coverage"
    entry = item["detail"]["joint_rows"][0]
    assert entry["member"] == b
    assert entry["missing_points"] == []
    assert entry["missing_probe"] is True


def test_a_member_with_a_failing_probe_lists_missing_probe(tmp_path):
    a = _write(tmp_path, "a.json", _grid_envelope())
    b = _write(
        tmp_path,
        "b.json",
        {
            **_grid_envelope(),
            "measurements": [
                {
                    "name": "vdd_applied",
                    "spec_row": "pvt_grid",
                    "status": "fail",
                    "limits": {"min": 1.6},
                }
            ],
        },
    )

    item = _item5(
        {"5": [{"file": a}, {"file": b}]},
        [{"id": "pvt_grid", "joint_grid": _joint_members(a, b)}],
    )

    assert item["reason"] == "partial_coverage"
    assert item["detail"]["joint_rows"][0]["missing_probe"] is True


def test_a_probe_without_limits_does_not_qualify(tmp_path):
    a = _write(tmp_path, "a.json", _grid_envelope())
    b = _write(tmp_path, "b.json", _grid_envelope(probe_limits=False))

    item = _item5(
        {"5": [{"file": a}, {"file": b}]},
        [{"id": "pvt_grid", "joint_grid": _joint_members(a, b)}],
    )

    assert item["reason"] == "partial_coverage"
    assert item["detail"]["joint_rows"][0]["missing_probe"] is True


def test_a_failing_member_leaves_the_row_uncovered(tmp_path):
    a = _write(tmp_path, "a.json", _grid_envelope())
    failing = {**_grid_envelope(), "status": "fail", "passed": 26, "failed": 1}
    b = _write(tmp_path, "b.json", failing)

    item = _item5(
        {"5": [{"file": a}, {"file": b}]},
        [{"id": "pvt_grid", "joint_grid": _joint_members(a, b)}],
    )

    # The member's own failure is the item's reason; the joint row is
    # uncovered and every point is unproven -- the failed envelope is not
    # evidence for the joint property.
    assert item["status"] == "unmet"
    assert item["reason"] == "check_failed"
    assert item["detail"]["failed_citations"] == [b]
    entry = next(e for e in item["detail"]["joint_rows"] if e["member"] == b)
    assert len(entry["missing_points"]) == 27
    assert entry["missing_probe"] is True


def test_control_dropping_the_member_from_members_would_pass(tmp_path):
    """The two full envelopes grade `met` as the joint row's only members;
    adding the deficient third makes it fail -- the check is reached and
    judges every named member, not just the first."""
    a = _write(tmp_path, "a.json", _grid_envelope())
    b = _write(tmp_path, "b.json", _grid_envelope())
    c = _write(tmp_path, "c.json", _grid_envelope(drop=("ff", 1.98, -40)))
    evidence = {"5": [{"file": a}, {"file": b}, {"file": c}]}

    without_c = _item5(
        evidence, [{"id": "pvt_grid", "joint_grid": _joint_members(a, b)}]
    )
    with_c = _item5(
        evidence, [{"id": "pvt_grid", "joint_grid": _joint_members(a, b, c)}]
    )

    assert without_c["status"] == "met"
    assert with_c["status"] == "unmet"
    assert with_c["reason"] == "partial_coverage"


def test_a_point_swept_twice_must_pass_at_every_corner(tmp_path):
    doubled = _grid_envelope()
    corners = doubled["corners"]
    # A params-axis style repeat of one point: one pass, one fail.
    corners.append(
        {
            **corners[0],
            "corner_id": corners[0]["corner_id"] + "/params:x=1",
            "status": "fail",
        }
    )
    a = _write(tmp_path, "a.json", _grid_envelope())
    b = _write(tmp_path, "b.json", doubled)

    item = _item5(
        {"5": [{"file": a}, {"file": b}]},
        [{"id": "pvt_grid", "joint_grid": _joint_members(a, b)}],
    )

    assert item["reason"] == "partial_coverage"
    assert item["detail"]["joint_rows"][0]["missing_points"] == ["tt/1.620V/-40C"]


# --------------------------------------------------------------------------- #
# The refusal edges.
# --------------------------------------------------------------------------- #


def test_a_citation_naming_a_joint_row_in_rows_is_invalid(tmp_path):
    a = _write(tmp_path, "a.json", _grid_envelope())
    b = _write(tmp_path, "b.json", _grid_envelope())

    item = _item5(
        {"5": [{"file": a, "rows": ["pvt_grid"]}, {"file": b}]},
        [{"id": "pvt_grid", "joint_grid": _joint_members(a, b)}],
    )

    assert item["status"] == "unmet"
    assert item["reason"] == "invalid_evidence"
    assert item["detail"]["problem"] == (
        "a member names a row that is undeclared or a declared gap"
    )


def test_an_uncited_member_name_is_invalid(tmp_path):
    a = _write(tmp_path, "a.json", _grid_envelope())

    item = _item5(
        {"5": [{"file": a}]},
        [{"id": "pvt_grid", "joint_grid": _joint_members(a, "/nope.json")}],
    )

    assert item["reason"] == "invalid_evidence"
    assert item["detail"]["problem"] == (
        "joint_grid row 'pvt_grid' names an uncited member: /nope.json"
    )


def test_a_monte_carlo_member_is_stated_unsupported(tmp_path):
    a = _write(tmp_path, "a.json", _grid_envelope())
    mc = _write(tmp_path, "mc.json", _grid_envelope(monte_carlo=True))

    item = _item5(
        {"5": [{"file": a}, {"file": mc}]},
        [{"id": "pvt_grid", "joint_grid": _joint_members(a, mc)}],
    )

    assert item["reason"] == "invalid_evidence"
    assert "Monte Carlo member, which is unsupported" in item["detail"]["problem"]
    assert mc in item["detail"]["problem"]


@pytest.mark.parametrize(
    "bad",
    [
        {"members": [], **_GRID},
        {"members": ["only-one"], **_GRID},
        {"members": ["m", "m"], **_GRID},
        {"members": ["m", 7], **_GRID},
        {
            "members": ["m", "n"],
            "process": [],
            "supply_v": _GRID["supply_v"],
            "temperature_c": _GRID["temperature_c"],
        },
        {
            "members": ["m", "n"],
            "process": [7],
            "supply_v": _GRID["supply_v"],
            "temperature_c": _GRID["temperature_c"],
        },
        {
            "members": ["m", "n"],
            "process": ["tt"],
            "supply_v": {},
            "temperature_c": _GRID["temperature_c"],
        },
        {
            "members": ["m", "n"],
            "process": ["tt"],
            "supply_v": {"vdd": ["x"]},
            "temperature_c": _GRID["temperature_c"],
        },
        {
            "members": ["m", "n"],
            "process": ["tt"],
            "supply_v": {"vdd": [1.8], "vssa": [0.0, 0.0]},
            "temperature_c": _GRID["temperature_c"],
        },
        {
            "members": ["m", "n"],
            "process": ["tt"],
            "supply_v": {"vdd": [1.8]},
            "temperature_c": [],
        },
        {
            "members": ["m", "n"],
            "process": ["tt"],
            "supply_v": {"vdd": [1.8]},
            "temperature_c": [27, "hot"],
        },
        {
            "members": ["m", "n"],
            "process": ["tt"],
            "supply_v": {"vdd": [1.8]},
            "temperature_c": [27],
            "bogus": True,
        },
    ],
)
def test_a_malformed_joint_grid_degrades_item5_alone(tmp_path, bad):
    item = _item5({}, [{"id": "pvt_grid", "joint_grid": bad}])
    assert item["reason"] == "invalid_evidence"
    assert item["detail"]["problem"] == "'spec_rows' for this item is malformed"


@pytest.mark.parametrize(
    "disposition",
    [
        {"gap": "why"},
        {
            "structural": {
                "claim": "constant",
                "artifact": {"file": "ref", "content_hash": "sha256:" + "0" * 64},
            }
        },
    ],
)
def test_other_dispositions_and_joint_grid_are_mutually_exclusive(
    tmp_path, disposition
):
    a = _write(tmp_path, "a.json", _grid_envelope())
    b = _write(tmp_path, "b.json", _grid_envelope())

    item = _item5(
        {"5": [{"file": a}, {"file": b}]},
        [{"id": "pvt_grid", **disposition, "joint_grid": _joint_members(a, b)}],
    )

    assert item["reason"] == "invalid_evidence"


def test_the_form_is_item5_only(tmp_path):
    a = _write(tmp_path, "a.json", _grid_envelope())
    b = _write(tmp_path, "b.json", _grid_envelope())
    manifest = {
        "block": "demo-block",
        "kind": "analog",
        "evidence": {},
        "spec_rows": {"6": [{"id": "pvt_grid", "joint_grid": _joint_members(a, b)}]},
    }
    item6 = next(i for i in build_tier_report(manifest)["items"] if i["id"] == 6)
    assert item6["reason"] == "invalid_evidence"
    assert item6["detail"]["problem"] == "'spec_rows' for this item is malformed"


def test_partition_qualified_keys_share_the_reader(tmp_path):
    a = _write(tmp_path, "a.json", _grid_envelope())
    b = _write(tmp_path, "b.json", _grid_envelope(drop=("ff", 1.98, -40)))
    manifest = {
        "block": "demo-block",
        "kind": "mixed-signal",
        "evidence": {"5.analog": [{"file": a}, {"file": b}]},
        "spec_rows": {
            "5.analog": [{"id": "pvt_grid", "joint_grid": _joint_members(a, b)}]
        },
    }
    analog = next(
        i
        for i in build_tier_report(manifest)["items"]
        if i["id"] == 5 and i.get("partition") == "analog"
    )
    assert analog["status"] == "unmet"
    assert analog["reason"] == "partial_coverage"
    assert analog["detail"]["joint_rows"][0]["missing_points"] == ["ff/1.980V/-40C"]


# --------------------------------------------------------------------------- #
# The ratchet and the byte-identical baseline.
# --------------------------------------------------------------------------- #


def test_require_derived_rows_passes_a_covered_joint_row(tmp_path):
    a = _write(tmp_path, "a.json", _grid_envelope())
    b = _write(tmp_path, "b.json", _grid_envelope())

    item = _item5(
        {"5": [{"file": a}, {"file": b}]},
        [{"id": "pvt_grid", "joint_grid": _joint_members(a, b)}],
        require_derived_rows=True,
    )

    assert item["status"] == "met"
    assert item["row_coverage"]["row_sources"] == {"pvt_grid": "derived"}
    assert "asserted_rows" not in item.get("detail", {})


def test_a_manifest_without_joint_rows_renders_no_joint_keys(tmp_path):
    a = _write(tmp_path, "a.json", _grid_envelope())

    report = build_tier_report(
        {
            "block": "demo-block",
            "kind": "analog",
            "evidence": {"5": [{"file": a, "rows": ["tdec"]}]},
            "spec_rows": {"5": [{"id": "tdec"}]},
        }
    )

    rendered = json.dumps(report, sort_keys=True)
    # Quoted keys, not the bare substring: a tmp path can spell "joint".
    assert '"joint"' not in rendered
    assert "joint_grid" not in rendered
    assert "joint_rows" not in rendered
    assert report["spec_rows"][0]["state"] == "no_evidence"


# --------------------------------------------------------------------------- #
# The label and matching rules, pinned against `klt sim`'s own spellings.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("process", "supply", "temp", "expected"),
    [
        ("tt", {"vdd": 1.8}, 27, "tt/1.800V/27C"),
        ("ss", {"vdd": 1.62}, -40, "ss/1.620V/-40C"),
        (None, {}, 27, "default/novdd/27C"),
        (None, {"vdd": 1.8, "vssa": 0.0}, 125, "default/vdd=1.800_vssa=0.000V/125C"),
        ("tt", {"vdd": 1.7987}, 27, "tt/1.7987V/27C"),
    ],
)
def test_point_labels_mirror_the_corner_id_spelling(process, supply, temp, expected):
    assert _point_label(process, supply, temp) == expected


def test_supply_and_temperature_match_as_numbers():
    envelope = _grid_envelope()
    # JSON round-trips 27 as int, 27.0 as float: one number either way.
    corner = envelope["corners"][4]
    assert corner["temperature_c"] == 27
    corner["temperature_c"] = 27.0
    corner["supply_v"] = {"vdd": 1.80}
    joint = {"process": ["tt"], "supply_v": {"vdd": [1.8]}, "temperature_c": [27]}
    assert member_shortfall(envelope, joint, "pvt_grid") == ([], False)


def test_rails_the_grid_does_not_name_do_not_disqualify():
    envelope = _grid_envelope()
    for corner in envelope["corners"]:
        corner["supply_v"] = {"vdd": corner["supply_v"]["vdd"], "vssa": 0.0}
    joint = {"process": ["tt"], "supply_v": {"vdd": [1.62]}, "temperature_c": [-40]}
    assert member_shortfall(envelope, joint, "pvt_grid") == ([], False)


def test_multi_rail_grids_sweep_zipped_not_crossed():
    # Two rails, zipped by index (a `corners` block's semantics): the
    # required points are the two declared rows, not their cross product.
    joint = {
        "members": ["m", "n"],
        "process": ["tt"],
        "supply_v": {"vdd": [1.8, 1.62], "vssa": [0.0, -0.1]},
        "temperature_c": [27],
    }
    points = required_grid_points(joint)
    assert points == [
        ("tt", {"vdd": 1.8, "vssa": 0.0}, 27),
        ("tt", {"vdd": 1.62, "vssa": -0.1}, 27),
    ]


def test_citation_label_matches_file_or_command():
    assert citation_label({"kind": "file", "file": "sim/a.json"}) == "sim/a.json"
    assert citation_label({"kind": "command", "command": ["klt", "sim"]}) == "klt sim"


def test_an_absent_axis_takes_the_request_side_defaults():
    assert required_grid_points({"members": ["m", "n"]}) == [(None, {}, 27)]
