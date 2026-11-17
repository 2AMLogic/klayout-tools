"""Tests for `klt deck diff` (issue #5680): the pure comparison
(`klayout_tools.deck_diff`) and its CLI wiring (`cli/deck_cmd.py` +
`cli/parser.py`).

All fixtures are inline dicts shaped exactly like a saved
`klt deck rules --format json` report -- no network, no installs, no deck
modules imported for the pure cases. The CLI cases exercise the real
envelope helpers (exit codes, stderr error shape) through `main()`, plus
the literal `installed` side against the real sky130 table (the same
unmocked call `test_deck_history.py` already makes, so it is CI-safe).
"""

from __future__ import annotations

import json

import pytest

from klayout_tools.cli import main
from klayout_tools.deck_diff import DeckDiffError, compare_deck_rules


def _rule(rule_id="poly.width.1", **overrides):
    """One rule entry, field-for-field what `deck_rules()` projects."""
    rule = {
        "id": rule_id,
        "description": f"Minimum {rule_id}",
        "check": "width",
        "layers": ["poly.drawing"],
        "layer_refs": [{"name": "poly.drawing", "layer": 66, "datatype": 20}],
        "scope": None,
        "value_um": 0.15,
        "value_dbu": 30,
        "limits": {},
        "provenance": {
            "source_repo": "git@github.com:google/skywater-pdk.git",
            "source_path": "rules/periphery.poly.w.ttl",
            "rule_id": "poly.w.1",
            "commit": None,
        },
    }
    rule.update(overrides)
    return rule


def _report(rules=None, unmodeled=None, deck="sky130", **overrides):
    """One full `deck rules` envelope; override any field by keyword."""
    report = {
        "schema_version": 1,
        "deck": deck,
        "content_hash": "sha256:" + "a" * 64,
        "nominal_dbu_um": 0.005,
        "rules": list(rules if rules is not None else [_rule()]),
        "unmodeled_rules": list(unmodeled if unmodeled is not None else []),
        "marker_blind_layers": [],
        "layer_table": [{"name": "poly.drawing", "layer": 66, "datatype": 20}],
        "ambiguous_names": [],
        "unresolved": [],
    }
    report.update(overrides)
    return report


# --------------------------------------------------------------------------- #
# Pure comparison: field-level `changed`
# --------------------------------------------------------------------------- #


def test_value_um_change_is_the_only_reported_field():
    to = _report(rules=[_rule(value_um=0.3, value_dbu=60)])
    report = compare_deck_rules(_report(), to)
    assert report["status"] == "different"
    assert report["changed"] == [
        {
            "id": "poly.width.1",
            "fields": {
                "value_um": {"from": 0.15, "to": 0.3},
                "value_dbu": {"from": 30, "to": 60},
            },
        }
    ]
    assert report["summary"]["changed"] == 1
    assert report["added"] == [] and report["removed"] == []
    assert report["enforcement_changed"] == []


def test_limits_change_on_a_rule_whose_value_um_is_none():
    # area/density rules carry `value_um`/`value_dbu` as None and their
    # number lives in `limits` -- the case an id-only diff misses.
    def area(limits):
        return _rule(
            "met1.area.1",
            check="area",
            value_um=None,
            value_dbu=None,
            limits=limits,
        )

    report = compare_deck_rules(
        _report(rules=[area({"area_min_um2": 0.08})]),
        _report(rules=[area({"area_min_um2": 0.1})]),
    )
    assert report["changed"] == [
        {
            "id": "met1.area.1",
            "fields": {
                "limits": {"from": {"area_min_um2": 0.08}, "to": {"area_min_um2": 0.1}}
            },
        }
    ]


def test_layers_change():
    to = _report(
        rules=[
            _rule(
                layers=["poly.drawing", "diff.drawing"],
                layer_refs=[
                    {"name": "poly.drawing", "layer": 66, "datatype": 20},
                    {"name": "diff.drawing", "layer": 65, "datatype": 20},
                ],
            )
        ]
    )
    report = compare_deck_rules(_report(), to)
    fields = report["changed"][0]["fields"]
    assert fields["layers"]["from"] == ["poly.drawing"]
    assert fields["layers"]["to"] == ["poly.drawing", "diff.drawing"]
    assert "layer_refs" in fields


def test_provenance_change_and_none_on_one_side():
    cited = _rule(
        provenance={
            "source_repo": "r",
            "source_path": "p",
            "rule_id": "x",
            "commit": "c",
        }
    )
    uncited = _rule(provenance=None)
    report = compare_deck_rules(_report(), _report(rules=[cited]))
    assert "provenance" in report["changed"][0]["fields"]

    # `None` on one side is a compared value, not a missing field: it is a
    # real move between cited and uncited, never `not_comparable`.
    report = compare_deck_rules(_report(rules=[uncited]), _report(rules=[cited]))
    assert report["not_comparable"]["rule_fields"] == []
    assert report["changed"][0]["fields"]["provenance"]["from"] is None


def test_description_check_scope_changes_are_caught():
    to = _report(
        rules=[_rule(description="New prose", check="space", scope="interior")]
    )
    report = compare_deck_rules(_report(), to)
    fields = report["changed"][0]["fields"]
    assert set(fields) == {"description", "check", "scope"}


# --------------------------------------------------------------------------- #
# Pure comparison: added / removed / enforcement
# --------------------------------------------------------------------------- #


def test_added_and_removed():
    report = compare_deck_rules(
        _report(rules=[_rule("poly.width.1"), _rule("diff.width.1")]),
        _report(rules=[_rule("poly.width.1"), _rule("poly.width.2")]),
    )
    assert report["added"] == ["poly.width.2"]
    assert report["removed"] == ["diff.width.1"]
    assert report["changed"] == []
    assert report["summary"]["added"] == 1
    assert report["summary"]["removed"] == 1


def test_rules_to_unmodeled_is_enforcement_changed_not_removed():
    report = compare_deck_rules(
        _report(rules=[_rule("poly.width.1")], unmodeled=[]),
        _report(rules=[], unmodeled=["poly.width.1"]),
    )
    assert report["enforcement_changed"] == [
        {"id": "poly.width.1", "from": "enforced", "to": "unmodeled"}
    ]
    assert report["added"] == [] and report["removed"] == []
    assert report["summary"]["rules_enforced"] == {"from": 1, "to": 0}


def test_unmodeled_to_rules_is_enforcement_changed_not_added():
    report = compare_deck_rules(
        _report(rules=[], unmodeled=["poly.width.1"]),
        _report(rules=[_rule("poly.width.1")], unmodeled=[]),
    )
    assert report["enforcement_changed"] == [
        {"id": "poly.width.1", "from": "unmodeled", "to": "enforced"}
    ]
    assert report["added"] == [] and report["removed"] == []


def test_dropping_a_known_id_entirely_is_removed():
    # An id that leaves `unmodeled_rules` without appearing in `rules` is a
    # removal from the deck's known-rule universe, not a silent omission.
    report = compare_deck_rules(
        _report(rules=[], unmodeled=["gone.rule.1"]),
        _report(rules=[], unmodeled=[]),
    )
    assert report["removed"] == ["gone.rule.1"]
    assert report["enforcement_changed"] == []


# --------------------------------------------------------------------------- #
# Pure comparison: ordering, identity, deck level
# --------------------------------------------------------------------------- #


def test_reordering_the_rule_array_is_not_a_change():
    rules = [_rule("poly.width.1"), _rule("diff.width.1"), _rule("tap.width.1")]
    report = compare_deck_rules(
        _report(rules=rules), _report(rules=list(reversed(rules)))
    )
    assert report["status"] == "identical"
    assert report["summary"]["identical"] is True
    for category in ("added", "removed", "changed", "enforcement_changed"):
        assert report[category] == []
    assert report["deck_level_changed"] == {}
    assert report["not_comparable"] == {"envelope_fields": [], "rule_fields": []}


def test_identical_content_hash_reports_same_revision():
    report = compare_deck_rules(_report(), _report())
    assert report["identity"]["same_revision"] is True
    assert report["status"] == "identical"
    assert report["identity"]["from"]["deck"] == "sky130"
    assert report["identity"]["from"]["content_hash"] == "sha256:" + "a" * 64
    assert report["identity"]["from"]["schema_version"] == 1
    assert report["identity"]["to"]["schema_version"] == 1


def test_nominal_dbu_um_change_is_deck_level():
    report = compare_deck_rules(_report(), _report(nominal_dbu_um=0.001))
    assert report["deck_level_changed"] == {
        "nominal_dbu_um": {"from": 0.005, "to": 0.001}
    }
    assert report["summary"]["deck_level_changed"] == 1
    assert report["changed"] == []


def test_marker_blind_and_layer_table_changes_are_deck_level():
    report = compare_deck_rules(
        _report(),
        _report(
            marker_blind_layers=["81/2"],
            layer_table=[
                {"name": "poly.drawing", "layer": 66, "datatype": 20},
                {"name": "areaid.ce", "layer": 81, "datatype": 2},
            ],
        ),
    )
    assert set(report["deck_level_changed"]) == {"marker_blind_layers", "layer_table"}
    assert report["summary"]["deck_level_changed"] == 2


def test_mismatched_deck_names_raise():
    with pytest.raises(DeckDiffError, match="two different decks"):
        compare_deck_rules(_report(deck="sky130"), _report(deck="gf180mcu"))


# --------------------------------------------------------------------------- #
# Pure comparison: schema drift and validation
# --------------------------------------------------------------------------- #


def test_differing_schema_versions_list_not_comparable_without_phantom_changes():
    # A newer release carrying fields the older report predates: listed,
    # never read as a rule change.
    report = compare_deck_rules(
        _report(),
        _report(
            content_hash="sha256:" + "b" * 64,
            schema_version=2,
            rules=[_rule(notes="new field")],
            density_window_um=5.0,
        ),
    )
    assert report["not_comparable"]["envelope_fields"] == ["density_window_um"]
    assert report["not_comparable"]["rule_fields"] == [
        {"id": "poly.width.1", "field": "notes"}
    ]
    assert report["changed"] == []
    assert report["added"] == [] and report["removed"] == []
    assert report["enforcement_changed"] == []
    # Any reported difference -- not_comparable included -- is `different`.
    assert report["status"] == "different"
    assert report["summary"]["not_comparable"] == 2


def test_empty_unmodeled_rules_on_both_sides():
    report = compare_deck_rules(_report(), _report(content_hash="sha256:" + "b" * 64))
    assert report["status"] == "identical"
    assert report["summary"]["unmodeled_rules"] == {"from": 0, "to": 0}


@pytest.mark.parametrize(
    "report",
    [
        "not a dict",
        {"deck": "sky130"},  # no rules
        {"deck": "sky130", "rules": "nope", "unmodeled_rules": []},
        {"deck": "sky130", "rules": [{"no": "id"}], "unmodeled_rules": []},
        {"rules": [], "unmodeled_rules": []},  # no deck name
        {"deck": "sky130", "rules": [], "unmodeled_rules": "nope"},
    ],
)
def test_malformed_inputs_raise_with_the_side_named(report):
    with pytest.raises(DeckDiffError, match="--from"):
        compare_deck_rules(report, _report())


# --------------------------------------------------------------------------- #
# CLI wiring: exit codes and envelope
# --------------------------------------------------------------------------- #


def _save(path, report):
    path.write_text(json.dumps(report))
    return str(path)


def test_cli_identical_exits_0(tmp_path, capsys):
    a = _save(tmp_path / "a.json", _report())
    b = _save(tmp_path / "b.json", _report())
    assert main(["deck", "diff", "--from", a, "--to", b, "--format", "json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema_version"] == 1
    assert out["status"] == "identical"
    assert out["summary"]["identical"] is True


def test_cli_difference_exits_3_with_payload_on_stdout(tmp_path, capsys):
    a = _save(tmp_path / "a.json", _report())
    b = _save(tmp_path / "b.json", _report(rules=[_rule(value_um=0.2)]))
    assert main(["deck", "diff", "--from", a, "--to", b, "--format", "json"]) == 3
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "different"
    assert out["changed"][0]["fields"]["value_um"]["from"] == 0.15


def test_cli_deck_mismatch_exits_1_no_diff_emitted(tmp_path, capsys):
    a = _save(tmp_path / "a.json", _report(deck="sky130"))
    b = _save(tmp_path / "b.json", _report(deck="gf180mcu"))
    assert main(["deck", "diff", "--from", a, "--to", b, "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert captured.out.strip() == ""
    err = json.loads(captured.err)
    assert err["error"]["command"] == "deck diff"
    assert "different decks" in err["error"]["message"]


def test_cli_unreadable_and_invalid_inputs_exit_1(tmp_path, capsys):
    missing = str(tmp_path / "missing.json")
    assert (
        main(["deck", "diff", "--from", missing, "--to", missing, "--format", "json"])
        == 1
    )
    assert "cannot read" in json.loads(capsys.readouterr().err)["error"]["message"]

    garbage = tmp_path / "garbage.json"
    garbage.write_text("{not json")
    assert (
        main(
            [
                "deck",
                "diff",
                "--from",
                str(garbage),
                "--to",
                str(garbage),
                "--format",
                "json",
            ]
        )
        == 1
    )
    assert "not valid JSON" in json.loads(capsys.readouterr().err)["error"]["message"]

    not_a_report = _save(tmp_path / "other.json", {"hello": "world"})
    assert (
        main(
            [
                "deck",
                "diff",
                "--from",
                not_a_report,
                "--to",
                not_a_report,
                "--format",
                "json",
            ]
        )
        == 1
    )
    assert "deck rules" in json.loads(capsys.readouterr().err)["error"]["message"]


def test_cli_text_rendering(tmp_path, capsys):
    a = _save(
        tmp_path / "a.json",
        _report(
            rules=[
                _rule("poly.width.1"),
                _rule("gone.rule.1", value_um=1.0),
                _rule("poly.space.1", check="space", value_um=0.2),
            ],
            unmodeled=["moved.rule.1"],
        ),
    )
    b = _save(
        tmp_path / "b.json",
        _report(
            rules=[
                _rule("poly.width.1", value_um=0.3, value_dbu=60),
                _rule("new.rule.1"),
            ],
            unmodeled=["moved.rule.1", "poly.space.1"],
        ),
    )

    assert main(["deck", "diff", "--from", a, "--to", b]) == 3
    text = capsys.readouterr().out
    assert "deck: sky130" in text
    assert "added (1): new.rule.1" in text
    assert "removed (1): gone.rule.1" in text
    assert "changed: poly.width.1" in text
    assert "0.15 -> 0.3" in text
    assert "enforcement changed: poly.space.1 enforced -> unmodeled" in text


# --------------------------------------------------------------------------- #
# CLI wiring: the literal `installed` side
# --------------------------------------------------------------------------- #


def test_cli_installed_side_identical_to_own_report(tmp_path, capsys):
    saved = tmp_path / "sky130.json"
    assert main(["deck", "rules", "--deck", "sky130", "--format", "json"]) == 0
    saved.write_text(capsys.readouterr().out)
    rc = main(
        ["deck", "diff", "--from", str(saved), "--to", "installed", "--format", "json"]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "identical"
    assert out["identity"]["same_revision"] is True
    # The deck name came from the saved report: no --deck flag needed.
    assert out["identity"]["from"]["deck"] == "sky130"


def test_cli_installed_side_reports_hand_edit_as_changed(tmp_path, capsys):
    saved = tmp_path / "sky130.json"
    assert main(["deck", "rules", "--deck", "sky130", "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    report["rules"][0]["value_um"] = 9.99
    saved.write_text(json.dumps(report))
    assert main(["deck", "diff", "--from", str(saved), "--to", "installed"]) == 3
    text = capsys.readouterr().out
    assert f"changed: {report['rules'][0]['id']}" in text
    assert "9.99" in text


def test_cli_installed_side_with_explicit_deck_flag(tmp_path, capsys):
    assert main(["deck", "rules", "--deck", "sky130", "--format", "json"]) == 0
    saved = tmp_path / "sky130.json"
    saved.write_text(capsys.readouterr().out)
    rc = main(
        ["deck", "diff", "--from", "installed", "--to", str(saved), "--deck", "sky130"]
    )
    assert rc == 0


def test_cli_both_sides_installed_without_deck_errors(capsys):
    assert (
        main(
            [
                "deck",
                "diff",
                "--from",
                "installed",
                "--to",
                "installed",
                "--format",
                "json",
            ]
        )
        == 1
    )
    err = json.loads(capsys.readouterr().err)
    assert "--deck" in err["error"]["message"]


def test_cli_installed_side_unknown_deck_errors(tmp_path, capsys):
    saved = _save(
        tmp_path / "nonsense.json", {"deck": "nope", "rules": [], "unmodeled_rules": []}
    )
    assert (
        main(
            [
                "deck",
                "diff",
                "--from",
                str(saved),
                "--to",
                "installed",
                "--format",
                "json",
            ]
        )
        == 1
    )
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["command"] == "deck diff"
