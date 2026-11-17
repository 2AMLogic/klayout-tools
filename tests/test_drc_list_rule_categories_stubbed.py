"""``--list-rule-categories`` with the ``klayout`` *subprocess* stubbed (#5444).

The third tier beside the two in ``tests/test_drc_list_rule_categories.py``,
whose real-binary tier is gated on ``shutil.which("klayout")`` and is the
only honest home for the headline claim (KLayout, not klt, resolves a deck's
``%include`` closure, its ``-rd`` gates and its loop-generated names).

This tier stubs ``drc_klayout_launch.subprocess.run`` the way
``tests/test_drc_klayout_engine.py`` stubs the engine's, so everything
*around* the deck run -- the tool-generated empty layout, the launch argv,
work-dir cleanup, payload assembly, the fail-closed paths and the CLI's
text/JSON rendering -- is exercised on a host with no ``klayout`` binary.
That is the configuration CI's coverage leg runs in (the klayout *wheel* is
installed there, the standalone application is not), so without this tier
those paths are measured as untested.
"""

from __future__ import annotations

import json
import os

import klayout.db as kdb
import pytest

from helpers.drc_list_categories import write_deck as _write_deck
from helpers.subprocess_fakes import fake_completed
from klayout_tools import drc_klayout_launch as launch_module
from klayout_tools.cli import main
from klayout_tools.drc import DrcError
from klayout_tools.drc_klayout_list import (
    _not_listable,
    list_klayout_deck_rule_categories,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _categories_rdb(*names: str) -> str:
    """A finding-free report declaring ``names`` -- what KLayout writes for a
    deck whose every rule reached ``output(...)`` and matched nothing, which
    is exactly the empty-layout run this mode performs."""
    body = "".join(
        f"  <category>\n   <name>{name}</name>\n"
        f"   <description>d</description>\n  </category>\n"
        for name in names
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<report-database>\n"
        " <description>stub</description>\n"
        f" <categories>\n{body} </categories>\n"
        " <cells>\n  <cell><name>KLT_EMPTY</name></cell>\n </cells>\n"
        " <items>\n </items>\n"
        "</report-database>\n"
    )


def _stub_klayout(
    monkeypatch,
    *,
    categories: tuple[str, ...] = ("M2.a", "M1.a"),
    write_report: bool = True,
    returncode: int = 0,
    stderr: str = "",
    calls: list | None = None,
):
    """Stand in for the ``klayout -b -r <deck>`` launch.

    Writes the report at whatever path the module chose (located by scanning
    argv for the ``report=`` token, as the engine's own stub does -- the work
    dir is private to :func:`list_klayout_deck_rule_categories`).
    """

    def fake_run(cmd, capture_output, text, timeout):
        if calls is not None:
            calls.append(list(cmd))
        report_arg = next((a for a in cmd if a.startswith("report=")), None)
        if report_arg is None:
            # Not the deck launch -- `subprocess` is one module object, so
            # this stub also intercepts the `klayout -v` version probe.
            return fake_completed()
        if write_report:
            with open(report_arg.split("=", 1)[1], "w", encoding="utf-8") as handle:
                handle.write(_categories_rdb(*categories))
        return fake_completed(returncode=returncode, stderr=stderr)

    monkeypatch.setattr(launch_module.subprocess, "run", fake_run)
    return calls


def _deck_launches(calls):
    """The `klayout -b -r <deck>` launches among `calls`, dropping the
    `klayout -v` version probe."""
    return [c for c in calls if any(a.startswith("report=") for a in c)]


def _deck(tmp_path, name="stub.drc"):
    return _write_deck(tmp_path, name, "# content never read: the run is stubbed\n")


# --------------------------------------------------------------------------- #
# The derivation's own machinery: empty layout, launch argv, work dir.
# --------------------------------------------------------------------------- #


def test_deck_runs_against_a_single_cell_geometry_free_layout(tmp_path, monkeypatch):
    """The tool-generated layout is what makes the derived set non-circular,
    so assert it really is empty -- one cell, no shapes, on any layer."""
    saved: dict[str, str] = {}

    def fake_run(cmd, capture_output, text, timeout):
        input_path = next(a for a in cmd if a.startswith("input=")).split("=", 1)[1]
        saved["bytes"] = open(input_path, "rb").read()
        report_path = next(a for a in cmd if a.startswith("report=")).split("=", 1)[1]
        with open(report_path, "w", encoding="utf-8") as handle:
            handle.write(_categories_rdb("M1.a"))
        return fake_completed()

    monkeypatch.setattr(launch_module.subprocess, "run", fake_run)
    list_klayout_deck_rule_categories(_deck(tmp_path))

    copy = tmp_path / "handed-to-klayout.gds"
    copy.write_bytes(saved["bytes"])
    layout = kdb.Layout()
    layout.read(str(copy))
    assert [c.name for c in layout.each_cell()] == ["KLT_EMPTY"]
    assert layout.dbu == pytest.approx(0.001)
    assert layout.top_cell().bbox().empty()
    assert all(layout.top_cell().shapes(i).size() == 0 for i in layout.layer_indexes())


def test_launch_passes_the_deck_vars_as_rd_globals(tmp_path, monkeypatch):
    calls = _stub_klayout(monkeypatch, calls=[])
    deck = _deck(tmp_path)

    list_klayout_deck_rule_categories(
        deck, deck_vars={"feol": "true", "metal_top": "9"}, timeout_s=12.0
    )

    (cmd,) = _deck_launches(calls)
    assert cmd[0] == "klayout"
    assert cmd[1:3] == ["-b", "-r"]
    assert cmd[3] == deck
    assert "feol=true" in cmd and "metal_top=9" in cmd
    # The caller's vars come *after* the input/report pair the mode sets.
    assert cmd.index("feol=true") > cmd.index(
        next(a for a in cmd if a.startswith("report="))
    )


def test_private_work_dir_is_removed_even_though_the_listing_succeeded(
    tmp_path, monkeypatch
):
    seen: dict[str, str] = {}

    def fake_run(cmd, capture_output, text, timeout):
        report_path = next(a for a in cmd if a.startswith("report=")).split("=", 1)[1]
        seen["work_dir"] = os.path.dirname(report_path)
        with open(report_path, "w", encoding="utf-8") as handle:
            handle.write(_categories_rdb("M1.a"))
        return fake_completed()

    monkeypatch.setattr(launch_module.subprocess, "run", fake_run)
    list_klayout_deck_rule_categories(_deck(tmp_path))

    assert not os.path.exists(seen["work_dir"])


# --------------------------------------------------------------------------- #
# Payload assembly.
# --------------------------------------------------------------------------- #


def test_listing_payload_shape(tmp_path, monkeypatch):
    _stub_klayout(monkeypatch, categories=("M2.a", "M1.a", "M1.a"))
    deck = _deck(tmp_path)

    listing = list_klayout_deck_rule_categories(deck, deck_vars={"feol": "true"})

    assert listing["mode"] == "list_rule_categories"
    assert listing["engine"] == "klayout"
    assert listing["basis"] == "empty_layout_run"
    assert listing["deck_file"] == deck
    assert listing["deck_vars"] == {"feol": "true"}
    # Sorted and de-duplicated by `_parse_klayout_rdb_report`.
    assert listing["rule_categories"] == ["M1.a", "M2.a"]
    assert listing["rule_category_count"] == 2
    assert [e["category"] for e in listing["limitations"]] == [
        "geometry_dependent_declarations",
        "deck_var_dependent",
        "deck_revision_scoped",
    ]
    assert all(e["text"] for e in listing["limitations"])
    assert listing["provenance"]["deck"]["content_hash"].startswith("sha256:")
    # A listing is not a verdict, and claims no PDK it was not given.
    for absent in ("status", "violations", "coverage", "violation_count"):
        assert absent not in listing
    assert listing["provenance"]["pdk"] is None


def test_limitations_are_copies_no_caller_can_mutate_for_the_next_one(
    tmp_path, monkeypatch
):
    _stub_klayout(monkeypatch)
    deck = _deck(tmp_path)

    first = list_klayout_deck_rule_categories(deck)
    first["limitations"][0]["text"] = "clobbered"

    assert list_klayout_deck_rule_categories(deck)["limitations"][0]["text"] != (
        "clobbered"
    )


def test_klayout_binary_override_is_recorded_and_used(tmp_path, monkeypatch):
    monkeypatch.delenv("KLT_KLAYOUT_BINARY", raising=False)
    exe = tmp_path / "pinned-klayout"
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    calls = _stub_klayout(monkeypatch, calls=[])

    listing = list_klayout_deck_rule_categories(
        _deck(tmp_path), klayout_binary=str(exe)
    )

    assert listing["environment"] == {"klayout_binary": str(exe)}
    assert _deck_launches(calls)[0][0] == str(exe)


def test_no_override_leaves_the_environment_block_off(tmp_path, monkeypatch):
    monkeypatch.delenv("KLT_KLAYOUT_BINARY", raising=False)
    _stub_klayout(monkeypatch)

    assert "environment" not in list_klayout_deck_rule_categories(_deck(tmp_path))


def test_deck_globals_warning_is_surfaced_on_the_listing(tmp_path, monkeypatch):
    """A wrong switch set is the one way this listing is silently wrong in
    the same direction a run is, so the run's warning is carried here."""
    _stub_klayout(monkeypatch, categories=("M1.a",))
    # `$feol` is read but never default-guarded, so the scan reports it as
    # required_by_caller -- the shape `_missing_deck_globals_warning` warns on.
    deck = _write_deck(
        tmp_path,
        "needs_var.drc",
        'if $feol == "true"\n'
        '  input(3, 0).width(0.5.um).output("diff.1", "gated")\n'
        "end\n",
    )

    assert any(
        w["category"] == "deck_globals_not_supplied"
        for w in list_klayout_deck_rule_categories(deck)["warnings"]
    )
    # Supplying it clears the warning rather than merely renaming it.
    assert "warnings" not in list_klayout_deck_rule_categories(
        deck, deck_vars={"feol": "true"}
    )


# --------------------------------------------------------------------------- #
# Fail-closed: never a short list.
# --------------------------------------------------------------------------- #


def test_deck_that_errors_is_reported_as_not_listable(tmp_path, monkeypatch):
    _stub_klayout(monkeypatch, returncode=1, stderr="ERROR: no such method\n")
    deck = _deck(tmp_path)

    with pytest.raises(DrcError) as excinfo:
        list_klayout_deck_rule_categories(deck)

    message = str(excinfo.value)
    assert "could not list rule categories" in message
    assert "did not complete" in message
    # KLayout's own diagnostic is carried through, not swallowed.
    assert "no such method" in message


def test_missing_report_is_reported_as_not_listable(tmp_path, monkeypatch):
    _stub_klayout(monkeypatch, write_report=False)

    with pytest.raises(DrcError, match="could not list rule categories"):
        list_klayout_deck_rule_categories(_deck(tmp_path))


def test_not_listable_preserves_the_underlying_error_code(tmp_path):
    wrapped = _not_listable("/d.drc", DrcError("boom", code="klayout_unavailable"))

    assert wrapped.code == "klayout_unavailable"
    assert "/d.drc" in str(wrapped)
    assert "boom" in str(wrapped)


def test_zero_declared_categories_is_an_error_not_an_empty_manifest(
    tmp_path, monkeypatch
):
    _stub_klayout(monkeypatch, categories=())

    with pytest.raises(DrcError, match="declared no rule category at all"):
        list_klayout_deck_rule_categories(_deck(tmp_path))


def test_missing_deck_file_is_refused_before_anything_is_launched(
    tmp_path, monkeypatch
):
    calls = _stub_klayout(monkeypatch, calls=[])

    with pytest.raises(DrcError, match="deck file not found"):
        list_klayout_deck_rule_categories(str(tmp_path / "nope.drc"))

    assert calls == []


def _unrunnable(tmp_path):
    """A `klayout` that exists but answers nothing useful to `-v`."""
    path = tmp_path / "not-a-klayout"
    path.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def test_version_floor_is_preflighted_before_the_deck_is_launched(
    tmp_path, monkeypatch
):
    """The application-version floor aborts a listing as surely as a run
    (issue #2689), and does so before anything is launched."""
    monkeypatch.delenv("KLT_KLAYOUT_BINARY", raising=False)
    calls = _stub_klayout(monkeypatch, calls=[])

    with pytest.raises(DrcError):
        list_klayout_deck_rule_categories(
            _deck(tmp_path),
            min_klayout_version="0.30.9",
            klayout_binary=_unrunnable(tmp_path),
        )

    assert _deck_launches(calls) == []


def test_missing_host_tool_is_preflighted_before_the_deck_is_launched(
    tmp_path, monkeypatch
):
    """A vendor deck's host assumptions abort a listing too (issue #2333) --
    and `--allow-missing-host-tools` waives it, as it does for a run."""
    calls = _stub_klayout(monkeypatch, categories=("M1.a",), calls=[])
    deck = _write_deck(
        tmp_path,
        "shells_out.drc",
        "`klt_no_such_host_tool_5444 --version`\n"
        'input(1, 0).width(0.5.um).output("M1.a", "always")\n',
    )

    with pytest.raises(DrcError, match="klt_no_such_host_tool_5444"):
        list_klayout_deck_rule_categories(deck)
    assert _deck_launches(calls) == []

    listing = list_klayout_deck_rule_categories(deck, allow_missing_host_tools=True)
    assert listing["rule_categories"] == ["M1.a"]


# --------------------------------------------------------------------------- #
# CLI rendering: `--format text` is the manifest, `--format json` the payload.
# --------------------------------------------------------------------------- #


def test_cli_text_stdout_is_exactly_the_manifest(tmp_path, capsys, monkeypatch):
    """stdout must be feedable to `--expect-rule-categories-file` byte for
    byte: names only, one per line, every note on stderr."""
    _stub_klayout(monkeypatch, categories=("M2.a", "M1.a"))
    deck = _deck(tmp_path)

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
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out == "M1.a\nM2.a\n"
    assert "2 rule category/categories declared" in captured.err
    assert "basis: empty_layout_run" in captured.err
    assert "deck vars: feol=true" in captured.err
    assert "limitation [geometry_dependent_declarations]" in captured.err
    assert "limitation [deck_var_dependent]" in captured.err


def test_cli_text_notes_report_an_empty_deck_var_set_and_any_warning(
    tmp_path, capsys, monkeypatch
):
    _stub_klayout(monkeypatch, categories=("M1.a",))
    deck = _write_deck(
        tmp_path,
        "needs_var.drc",
        'if $feol == "true"\n'
        '  input(3, 0).width(0.5.um).output("diff.1", "gated")\n'
        "end\n",
    )

    exit_code = main(
        ["drc", "--engine", "klayout", "--deck-file", deck, "--list-rule-categories"]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out == "M1.a\n"
    assert "deck vars: (none)" in captured.err
    assert "warning [deck_globals_not_supplied]" in captured.err


def test_cli_json_emits_the_listing_and_the_clean_exit_code(
    tmp_path, capsys, monkeypatch
):
    """A listing has no verdict, so success is always the clean code -- it
    never borrows a DRC status."""
    _stub_klayout(monkeypatch, categories=("M1.a",))

    exit_code = main(
        [
            "drc",
            "--engine",
            "klayout",
            "--deck-file",
            _deck(tmp_path),
            "--list-rule-categories",
            "--format",
            "json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "list_rule_categories"
    assert payload["rule_categories"] == ["M1.a"]


def test_cli_without_any_deck_selector_says_how_to_pick_one(
    tmp_path, capsys, monkeypatch
):
    """No `--deck-file`, and a `--pdk-root` whose install ships no native
    deck: the refusal names `--deck-file` rather than failing obscurely."""
    monkeypatch.setattr(
        "klayout_tools.cli._drc_native.pdk.drc_deck_file", lambda **kw: None
    )
    monkeypatch.setattr(
        "klayout_tools.cli._drc_native.native_drc_main_script", lambda **kw: None
    )

    exit_code = main(
        ["drc", "--engine", "klayout", "--list-rule-categories", "--format", "json"]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)["error"]
    assert "no PDK-native klayout DRC deck" in error["message"]
    assert "--deck-file" in error["message"]


def test_cli_reports_an_unresolvable_pdk_as_a_clean_error(tmp_path, capsys):
    """`--pdk-root` pointing at no install at all: an error envelope on
    stdout and nothing else, never a traceback."""
    empty_root = tmp_path / "no-pdk-here"
    empty_root.mkdir()

    exit_code = main(
        [
            "drc",
            "--engine",
            "klayout",
            "--pdk-root",
            str(empty_root),
            "--list-rule-categories",
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)["error"]
    assert error["command"] == "drc"
    assert "PDK" in error["message"] or "pdk" in error["message"]
