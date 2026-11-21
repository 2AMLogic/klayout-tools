"""Tests for ``klt drc --engine klayout --list-rule-categories`` (#5444).

Two tiers, mirroring `tests/test_drc_klayout_engine.py`:

- **Usage/parser tier** -- needs no ``klayout`` binary at all and runs in CI
  unconditionally: mode exclusivity, the accepted-flag set, and the
  ``--engine klayout`` requirement.
- **Real-binary tier** -- gated by ``skipif(shutil.which("klayout") is
  None)``, driving the temp-dir fixture deck in
  ``tests/helpers/drc_list_categories.py`` whose categories are known by
  hand. The whole point of this mode is that KLayout, not klt, resolves the
  deck's ``%include`` closure, its ``-rd`` gates and its loop-generated
  names, so a stubbed ``subprocess.run`` could not test what matters here.

The fail-closed half lives in
``tests/test_drc_list_rule_categories_failures.py``.
"""

from __future__ import annotations

import json

import pytest

from helpers.drc_list_categories import (
    SKIP_NO_KLAYOUT_BINARY as _SKIP_NO_KLAYOUT_BINARY,
)
from helpers.drc_list_categories import (
    drc_response_validator as _drc_response_validator,
)
from helpers.drc_list_categories import (
    fixture_deck as _fixture_deck,
)
from helpers.drc_list_categories import (
    write_deck as _write_deck,
)
from helpers.drc_list_categories import (
    write_gds as _write_gds,
)
from klayout_tools.cli import main
from klayout_tools.drc_klayout_list import list_klayout_deck_rule_categories

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

# --------------------------------------------------------------------------- #
# Usage errors: no `klayout` binary needed, so these run everywhere.
# --------------------------------------------------------------------------- #


def test_list_rule_categories_requires_engine_klayout(tmp_path, capsys):
    deck = _write_deck(tmp_path, "d.drc", "# deck\n")

    exit_code = main(["drc", "--list-rule-categories", "--deck-file", deck])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "--list-rule-categories requires --engine klayout" in err
    assert "--list-rules" in err


@pytest.mark.parametrize(
    "extra",
    [
        ["--top", "TOP"],
        ["--rules", "met1.*"],
        ["--tile", "3x1"],
        ["--max-violations", "0"],
        ["--allow-deck-errors"],
        ["--expect-rule-categories", "3"],
        ["-o", "out.json"],
    ],
)
def test_list_rule_categories_rejects_flags_outside_its_set(tmp_path, capsys, extra):
    """Rejected, never silently ignored -- a caller passing one of these
    almost certainly meant to run DRC."""
    deck = _write_deck(tmp_path, "d.drc", "# deck\n")

    exit_code = main(
        ["drc", "--engine", "klayout", "--deck-file", deck, "--list-rule-categories"]
        + extra
    )

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "--list-rule-categories takes only" in err
    assert extra[0] in err


@pytest.mark.parametrize(
    "argv",
    [
        ["drc", "design.gds", "--engine", "klayout", "--list-rule-categories"],
        ["drc", "--engine", "klayout", "--check", "r.json", "--list-rule-categories"],
        ["drc", "--engine", "klayout", "--list-rules", "--list-rule-categories"],
    ],
)
def test_list_rule_categories_is_an_exclusive_input_mode(argv):
    """Combining it with another input mode is a *usage* error (exit 2),
    exactly as for `--list-rules`."""
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == 2


def test_list_rule_categories_missing_deck_file(tmp_path, capsys):
    exit_code = main(
        [
            "drc",
            "--engine",
            "klayout",
            "--deck-file",
            str(tmp_path / "nope.drc"),
            "--list-rule-categories",
        ]
    )

    assert exit_code == 1
    assert "deck file not found" in capsys.readouterr().err


def test_list_rule_categories_is_not_in_the_request_document_surface():
    """Out of scope for #5444 and asserted so it cannot drift in silently:
    the mode is argv-only, like `--list-rules`."""
    from klayout_tools.cli.parser import create_parser

    args = create_parser().parse_args(["drc", "x.gds"])

    assert "list_rule_categories" not in args.request_argv_flags
    assert args.list_rule_categories is False


# --------------------------------------------------------------------------- #
# Real-binary tier.
# --------------------------------------------------------------------------- #


@_SKIP_NO_KLAYOUT_BINARY
def test_listing_resolves_deck_vars_includes_and_loop_names(tmp_path):
    """The headline claim: a `-rd` gate, a `# %include`d file and
    loop-generated names all resolve, and a geometry-gated category does
    not appear."""
    deck = _fixture_deck(tmp_path)

    gated = list_klayout_deck_rule_categories(
        deck, deck_vars={"feol": "true"}, timeout_s=60.0
    )
    ungated = list_klayout_deck_rule_categories(deck, timeout_s=60.0)

    assert gated["rule_categories"] == ["M1.a", "M2.a", "diff.1"]
    assert ungated["rule_categories"] == ["M1.a", "M2.a"]
    # Declared only inside `if lay.area > 100.0`: not determinable from an
    # empty-layout run, and the payload says so rather than implying it is.
    assert "M1.dens" not in gated["rule_categories"]
    assert any(
        e["category"] == "geometry_dependent_declarations" for e in gated["limitations"]
    )


@_SKIP_NO_KLAYOUT_BINARY
def test_listing_payload_shape_and_schema(tmp_path, capsys):
    deck = _fixture_deck(tmp_path)

    exit_code = main(
        [
            "drc",
            "--engine",
            "klayout",
            "--deck-file",
            deck,
            "--deck-var",
            "feol=true",
            "--list-rule-categories",
            "--timeout-s",
            "60",
            "--format",
            "json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "list_rule_categories"
    assert payload["engine"] == "klayout"
    assert payload["basis"] == "empty_layout_run"
    assert payload["deck_file"] == deck
    assert payload["deck_vars"] == {"feol": "true"}
    assert payload["rule_categories"] == sorted(payload["rule_categories"])
    assert payload["rule_category_count"] == len(payload["rule_categories"])
    assert payload["limitations"]
    # A listing is not a verdict.
    for absent in ("status", "violations", "coverage", "violation_count"):
        assert absent not in payload
    assert payload["provenance"]["deck"]["content_hash"].startswith("sha256:")

    _drc_response_validator().validate(payload)


@_SKIP_NO_KLAYOUT_BINARY
def test_text_stdout_is_exactly_the_manifest(tmp_path, capsys):
    """`--format text` stdout must be feedable to
    `--expect-rule-categories-file` byte for byte: names only, one per
    line, with every note on stderr."""
    deck = _fixture_deck(tmp_path)

    exit_code = main(
        [
            "drc",
            "--engine",
            "klayout",
            "--deck-file",
            deck,
            "--deck-var",
            "feol=true",
            "--list-rule-categories",
            "--timeout-s",
            "60",
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out == "M1.a\nM2.a\ndiff.1\n"
    assert "limitation [geometry_dependent_declarations]" in captured.err


@_SKIP_NO_KLAYOUT_BINARY
def test_listing_round_trips_into_the_set_assertion(tmp_path, capsys):
    """The acceptance criterion in full: list -> manifest -> `clean`."""
    deck = _fixture_deck(tmp_path)
    gds = _write_gds(tmp_path / "clean.gds")

    assert (
        main(
            [
                "drc",
                "--engine",
                "klayout",
                "--deck-file",
                deck,
                "--deck-var",
                "feol=true",
                "--list-rule-categories",
                "--timeout-s",
                "60",
            ]
        )
        == 0
    )
    manifest = tmp_path / "deck.categories.txt"
    manifest.write_text(capsys.readouterr().out, encoding="utf-8")

    exit_code = main(
        [
            "drc",
            gds,
            "--engine",
            "klayout",
            "--deck-file",
            deck,
            "--deck-var",
            "feol=true",
            "--expect-rule-categories-file",
            str(manifest),
            "--timeout-s",
            "60",
            "--format",
            "json",
        ]
    )

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "clean"
    assert report["coverage"]["known"] is True
    assert report["coverage_assertion"]["satisfied"] is True


@_SKIP_NO_KLAYOUT_BINARY
def test_listing_for_the_wrong_switch_set_fails_the_assertion_closed(tmp_path, capsys):
    """A listing made under the wrong `--deck-var` set does not silently
    pass: the assertion names the missing category."""
    deck = _fixture_deck(tmp_path)
    gds = _write_gds(tmp_path / "clean.gds")
    manifest = tmp_path / "ungated.txt"
    manifest.write_text("M1.a\nM2.a\n", encoding="utf-8")

    exit_code = main(
        [
            "drc",
            gds,
            "--engine",
            "klayout",
            "--deck-file",
            deck,
            "--deck-var",
            "feol=true",
            "--expect-rule-categories-file",
            str(manifest),
            "--timeout-s",
            "60",
        ]
    )

    assert exit_code == 1
    assert "diff.1" in capsys.readouterr().err


@_SKIP_NO_KLAYOUT_BINARY
def test_deck_globals_warning_is_surfaced_on_the_listing(tmp_path):
    """A wrong switch set is the one way this listing is silently wrong in
    the same direction a run is, so the run's warning is carried here."""
    # `$feol` is read but never default-guarded, so the scan reports it as
    # required_by_caller -- the shape `_missing_deck_globals_warning` warns on.
    deck = _write_deck(
        tmp_path,
        "needs_var.drc",
        "if $input\n  source($input)\nend\n"
        'if $report\n  report("w", $report)\nend\n'
        'if $feol == "true"\n'
        '  input(3, 0).width(0.5.um).output("diff.1", "gated")\n'
        "end\n"
        'input(1, 0).width(0.5.um).output("M1.a", "always")\n',
    )

    listing = list_klayout_deck_rule_categories(deck, timeout_s=60.0)

    assert listing["rule_categories"] == ["M1.a"]
    assert any(
        w["category"] == "deck_globals_not_supplied" for w in listing["warnings"]
    )
