"""CLI-wiring tests for `klt deck diff` (issue #5680): exit codes, the
shared envelope, text rendering, and the literal ``installed`` side,
through ``cli/deck_cmd.py`` + ``cli/parser.py``.

The pure-comparison tests live in ``test_deck_diff.py``; the inline
fixtures are imported from there, shaped exactly like a saved
``klt deck rules --format json`` report. These cases exercise the real
envelope helpers (exit codes, stderr error shape) through ``main()``,
plus the literal ``installed`` side against the real sky130 table (the
same unmocked call ``test_deck_history.py`` already makes, so it is
CI-safe).
"""

from __future__ import annotations

import json

import pytest

from klayout_tools.cli import main
from test_deck_diff import _report, _rule

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


def test_cli_text_rendering_deck_level_not_comparable_and_containers(tmp_path, capsys):
    # Two different revisions (distinct content hashes), the newer one a
    # later schema: a deck-level move, a field and an envelope field the
    # older report predates, and a container-valued rule field.
    a = _save(tmp_path / "a.json", _report())
    b = _save(
        tmp_path / "b.json",
        _report(
            content_hash="sha256:" + "b" * 64,
            schema_version=2,
            nominal_dbu_um=0.001,
            density_window_um=5.0,
            rules=[_rule(layers=["poly.drawing", "li1.drawing"], notes="new")],
        ),
    )

    assert main(["deck", "diff", "--from", a, "--to", b]) == 3
    text = capsys.readouterr().out
    # Different revisions: the "same revision" line is not printed.
    assert "same revision" not in text
    assert "deck-level: nominal_dbu_um: 0.005 -> 0.001" in text
    assert "not comparable (envelope): density_window_um" in text
    assert "not comparable: poly.width.1 field notes" in text
    # Containers render as sorted-key JSON, scalars as repr().
    assert '  layers: ["poly.drawing"] -> ["poly.drawing", "li1.drawing"]' in text


def test_cli_text_rendering_same_revision_identical(tmp_path, capsys):
    a = _save(tmp_path / "a.json", _report())
    b = _save(tmp_path / "b.json", _report())
    assert main(["deck", "diff", "--from", a, "--to", b]) == 0
    text = capsys.readouterr().out
    assert "same revision: content_hash is identical on both sides" in text
    assert "no differences" in text


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


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (None, "cannot read saved report"),
        ("{not json", "is not valid JSON"),
        ('{"hello": "world"}', "carries no deck name"),
        ("[]", "carries no deck name"),
    ],
    ids=["missing", "invalid-json", "no-deck-field", "not-an-object"],
)
def test_cli_installed_side_needs_a_usable_saved_report_for_its_deck(
    tmp_path, capsys, content, expected
):
    # With no --deck, the `installed` side takes its deck name from the other
    # side's saved report, so that report must be readable and name a deck.
    saved = tmp_path / "saved.json"
    if content is not None:
        saved.write_text(content)
    rc = main(
        ["deck", "diff", "--from", str(saved), "--to", "installed", "--format", "json"]
    )
    assert rc == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["command"] == "deck diff"
    assert expected in err["error"]["message"]


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
