"""``--expect-deck-hash`` / request field ``expect_deck_hash`` (issue #5806).

A caller vouches for the deck bytes a fresh ``klt drc``/``klt lvs`` run used
by passing the exact ``provenance.deck.content_hash`` value to expect. The
comparison happens after the run, in the CLI adapter (``deck_hash_assertion``):
a matching run is silent and byte-identical, a mismatching one (or an
assertion against a run that recorded no deck hash at all) is an error
envelope on exit 1 -- so a deck bump re-scores nothing silently.

Tiers, mirroring the repo's usual split (this file holds the unit tests and
the ``klt drc`` CLI level; ``tests/test_deck_hash_assertion_lvs.py`` holds
the ``klt lvs`` CLI level):

- **Unit tests** exercise ``parse_expected`` (the ``sha256:<64 hex>`` form)
  and ``assert_deck_hash`` (match/mismatch/null deck) as pure functions.
- **CLI-level tests** for ``klt drc --engine klayout`` stub the klayout
  subprocess (``test_drc_klayout_engine``'s helpers), which produces a real
  ``provenance.deck`` to assert against.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools.cli import main
from klayout_tools.deck_hash_assertion import assert_deck_hash, parse_expected
from test_drc_klayout_engine import (
    _CLEAN_WITH_CATEGORIES_RDB,
    _assert_setup,
    _stub_klayout_drc_subprocess,
)

# The drc CLI tests stub the shared stdlib ``subprocess.run``; keep the
# provenance build-identity Git probes on the real runner (conftest).
pytestmark = pytest.mark.usefixtures("real_build_identity_git")

# --------------------------------------------------------------------------- #
# Unit: parse_expected
# --------------------------------------------------------------------------- #


def test_parse_expected_passes_the_documented_form_through():
    value = "sha256:" + "0a1b" * 16
    assert parse_expected(value, ValueError) == value


def test_parse_expected_none_is_no_assertion():
    assert parse_expected(None, ValueError) is None


@pytest.mark.parametrize(
    "value",
    [
        "beef",
        "sha256:" + "0a1b" * 15,  # 60 hex -- too short
        "sha256:" + "0a1b" * 17,  # 68 hex -- too long
        "sha256:" + "A" * 64,  # uppercase hex is not what content_hash emits
        "md5:" + "0a1b" * 16,
        "",
        7,
        True,
    ],
)
def test_parse_expected_rejects_malformed_values_naming_the_form(value):
    with pytest.raises(ValueError, match="sha256:<64 hex>"):
        parse_expected(value, ValueError)


# --------------------------------------------------------------------------- #
# Unit: assert_deck_hash
# --------------------------------------------------------------------------- #


def _deck_report(content_hash: str | None, name: str = "sky130") -> dict:
    deck = {"name": name}
    if content_hash is not None:
        deck["content_hash"] = content_hash
    return {"provenance": {"deck": deck}}


def test_assert_deck_hash_match_is_silent():
    expected = "sha256:" + "0a1b" * 16
    assert assert_deck_hash(_deck_report(expected), expected, ValueError) is None


def test_assert_deck_hash_no_expectation_is_a_no_op():
    assert assert_deck_hash(_deck_report(None), None, ValueError) is None


def test_assert_deck_hash_mismatch_names_deck_expected_observed_and_resolve():
    expected = "sha256:" + "0a1b" * 16
    observed = "sha256:" + "0a1b" * 15 + "0c"
    with pytest.raises(ValueError, match="deck-hash assertion failed") as exc:
        assert_deck_hash(_deck_report(observed), expected, ValueError)
    message = str(exc.value)
    assert "sky130" in message
    assert expected in message
    assert observed in message
    assert "klt deck resolve --content-hash" in message


@pytest.mark.parametrize(
    "report",
    [
        {},
        {"provenance": {}},
        {"provenance": {"deck": None}},
        {"provenance": {"deck": {"name": "sky130"}}},
        {"provenance": {"deck": {"name": "sky130", "content_hash": None}}},
    ],
)
def test_assert_deck_hash_with_no_recorded_hash_never_passes(report):
    expected = "sha256:" + "0a1b" * 16
    with pytest.raises(ValueError, match="no deck content hash"):
        assert_deck_hash(report, expected, ValueError)


# --------------------------------------------------------------------------- #
# CLI: klt drc (stubbed klayout subprocess)
# --------------------------------------------------------------------------- #


def _drc_argv(gds: str, deck: str, *extra: str) -> list[str]:
    return [
        "drc",
        gds,
        "--engine",
        "klayout",
        "--deck-file",
        deck,
        "--format",
        "json",
        *extra,
    ]


def test_drc_expect_deck_hash_match_is_byte_identical(tmp_path, monkeypatch, capsys):
    # The stubbed RDB declares exactly two categories; asserting them is what
    # makes the engine's rule execution measured, so the clean baseline run
    # is exit 0 rather than coverage_unknown (exit 4) on both runs.
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_CLEAN_WITH_CATEGORIES_RDB)
    gds, deck = _assert_setup(tmp_path)

    assert main(_drc_argv(gds, deck, "--expect-rule-categories", "2")) == 0
    baseline = capsys.readouterr().out
    observed = json.loads(baseline)["provenance"]["deck"]["content_hash"]

    capsys.readouterr()  # drain
    assert (
        main(
            _drc_argv(
                gds,
                deck,
                "--expect-rule-categories",
                "2",
                "--expect-deck-hash",
                observed,
            )
        )
        == 0
    )
    assert capsys.readouterr().out == baseline


def test_drc_expect_deck_hash_mismatch_fails_loudly(tmp_path, monkeypatch, capsys):
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_CLEAN_WITH_CATEGORIES_RDB)
    gds, deck = _assert_setup(tmp_path)

    assert main(_drc_argv(gds, deck, "--expect-rule-categories", "2")) == 0
    observed = json.loads(capsys.readouterr().out)["provenance"]["deck"]["content_hash"]
    tampered = observed[:-1] + ("0" if observed[-1] != "0" else "1")

    code = main(_drc_argv(gds, deck, "--expect-deck-hash", tampered))
    assert code == 1
    captured = capsys.readouterr()
    envelope = json.loads(captured.err)
    message = envelope["error"]["message"]
    assert "deck-hash assertion failed" in message
    assert "sky130" in message or "deck.lydrc" in message
    assert tampered in message
    assert observed in message
    assert "klt deck resolve --content-hash" in message


def test_drc_expect_deck_hash_mismatch_writes_no_output_file(
    tmp_path, monkeypatch, capsys
):
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_CLEAN_WITH_CATEGORIES_RDB)
    gds, deck = _assert_setup(tmp_path)
    out = tmp_path / "report.json"
    observed = "sha256:" + "0a1b" * 16

    code = main(_drc_argv(gds, deck, "--expect-deck-hash", observed, "-o", str(out)))

    assert code == 1
    assert not out.exists()


def test_drc_expect_deck_hash_malformed_is_a_usage_error_before_any_run(
    tmp_path, monkeypatch, capsys
):
    captured: list = []
    _stub_klayout_drc_subprocess(monkeypatch, captured_cmds=captured)
    gds, deck = _assert_setup(tmp_path)

    with pytest.raises(SystemExit) as exc:
        main(_drc_argv(gds, deck, "--expect-deck-hash", "beef"))

    assert exc.value.code == 2
    assert "sha256" in capsys.readouterr().err
    assert captured == []


def test_drc_expect_deck_hash_rejected_with_check(tmp_path, monkeypatch, capsys):
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_CLEAN_WITH_CATEGORIES_RDB)
    gds, deck = _assert_setup(tmp_path)
    report_path = tmp_path / "committed.json"
    assert (
        main(
            _drc_argv(
                gds, deck, "--expect-rule-categories", "2", "-o", str(report_path)
            )
        )
        == 0
    )
    capsys.readouterr()

    code = main(
        [
            "drc",
            "--check",
            str(report_path),
            "--expect-deck-hash",
            "sha256:" + "0a1b" * 16,
            "--format",
            "json",
        ]
    )

    assert code == 1
    assert (
        "--expect-deck-hash cannot be combined with --check" in capsys.readouterr().err
    )


def test_drc_expect_deck_hash_rejected_with_engine_both(tmp_path, capsys):
    gds, deck = _assert_setup(tmp_path)

    code = main(
        _drc_argv(
            gds, deck, "--engine", "both", "--expect-deck-hash", "sha256:" + "0a1b" * 16
        )
    )

    assert code == 1
    assert "--engine both" in capsys.readouterr().err


def test_drc_request_document_expect_deck_hash(tmp_path, monkeypatch, capsys):
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_CLEAN_WITH_CATEGORIES_RDB)
    gds, deck = _assert_setup(tmp_path)

    assert main(_drc_argv(gds, deck, "--expect-rule-categories", "2")) == 0
    observed = json.loads(capsys.readouterr().out)["provenance"]["deck"]["content_hash"]

    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "schema": "klt.drc.request/1",
                "file": gds,
                "engine": "klayout",
                "deck_file": deck,
                "expect_rule_categories": 2,
                "expect_deck_hash": observed,
            }
        ),
        encoding="utf-8",
    )
    assert main(["drc", str(request), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "clean"


def test_drc_request_document_expect_deck_hash_malformed(tmp_path, monkeypatch, capsys):
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_CLEAN_WITH_CATEGORIES_RDB)
    gds, deck = _assert_setup(tmp_path)
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "schema": "klt.drc.request/1",
                "file": gds,
                "engine": "klayout",
                "deck_file": deck,
                "expect_deck_hash": "beef",
            }
        ),
        encoding="utf-8",
    )

    code = main(["drc", str(request), "--format", "json"])

    assert code == 1
    assert "expect_deck_hash" in capsys.readouterr().err


def test_drc_request_document_and_flag_are_mutually_exclusive(
    tmp_path, monkeypatch, capsys
):
    _stub_klayout_drc_subprocess(monkeypatch, rdb_xml=_CLEAN_WITH_CATEGORIES_RDB)
    gds, deck = _assert_setup(tmp_path)
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps({"file": gds, "engine": "klayout", "deck_file": deck}),
        encoding="utf-8",
    )

    code = main(
        [
            "drc",
            str(request),
            "--expect-deck-hash",
            "sha256:" + "0a1b" * 16,
            "--format",
            "json",
        ]
    )

    assert code == 1
    assert "mutually exclusive" in capsys.readouterr().err


def test_drc_expect_deck_hash_is_in_the_request_argv_table():
    """The assertion is a first-class input flag: it must stay in
    ``request_argv_flags`` so the listing modes' "takes only --deck" check
    and the request-document precedence rule keep covering it."""
    from klayout_tools.cli.parser import create_parser

    args = create_parser().parse_args(["drc", "x.gds"])
    assert "expect_deck_hash" in args.request_argv_flags


def test_drc_list_rules_rejects_expect_deck_hash(capsys):
    """A listing runs no deck, so there is nothing to assert against -- it
    must be rejected like every other input flag, not silently ignored."""
    code = main(
        [
            "drc",
            "--list-rules",
            "--deck",
            "sky130",
            "--expect-deck-hash",
            "sha256:" + "0a1b" * 16,
        ]
    )

    assert code == 1
    assert "takes only --deck" in capsys.readouterr().err
