"""Guardrail tests for ``klt extract --engine klayout-runset`` (issue
#5481): the ``--check`` drift gate on runset reports, the refusal edges the
review round added (a literal zero flag value, a batch request document
selecting the engine), and the refused-flag table's integrity in both
directions.

Split out of ``tests/test_extract_runset.py`` (file-size ratchet: that file
carries the faked-subprocess engine tests and sits just under its limit).
"""

from __future__ import annotations

import json

import test_extract_runset as base
from klayout_tools import extract_runset
from klayout_tools.extract import check_extract_report, run_extract
from test_extract_runset import _inputs

fake_klayout = base.fake_klayout  # pytest collects the fixture by module-level name


def test_check_report_matches_a_freshly_written_runset_report(tmp_path, fake_klayout):
    """`klt extract --check` must verify a pristine runset report, not grade
    every one "drifted": there is no curated deck to hash (`provenance.deck.
    name` is the runset's filename, unresolvable through `deck_source_path`),
    so the deck check is replaced by a `provenance.runset.content_hash`
    check against the `deck_file` runset -- the branch `klt lvs --check` has
    carried since issue #2849. The layout lives outside the repo, so its
    `file` entry is `scope: "external"` and the input is supplied through
    the explicit-input override (issue #2699), exactly as documented for
    that scope."""
    gds, deck = _inputs(tmp_path)
    report = run_extract(
        gds,
        None,
        engine="klayout-runset",
        deck_file=deck,
        output=str(tmp_path / "out.spice"),
    )
    report_path = tmp_path / "out.extract.json"
    report_path.write_text(json.dumps(report))

    result = check_extract_report(str(report_path), input_path=gds)

    assert result["status"] == "match"
    by_field = {check["field"]: check for check in result["checks"]}
    assert set(by_field) == {
        "provenance.input.content_hash",
        "provenance.runset.content_hash",
    }
    assert all(check["match"] for check in result["checks"])


def test_check_report_detects_a_changed_runset(tmp_path, fake_klayout):
    """The runset is an input like any other: change it after the report was
    committed and only `provenance.runset.content_hash` drifts."""
    gds, deck = _inputs(tmp_path)
    report = run_extract(
        gds,
        None,
        engine="klayout-runset",
        deck_file=deck,
        output=str(tmp_path / "out.spice"),
    )
    report_path = tmp_path / "out.extract.json"
    report_path.write_text(json.dumps(report))

    with open(deck, "a") as runset:
        runset.write("# changed after the report was committed\n")

    result = check_extract_report(str(report_path), input_path=gds)

    assert result["status"] == "drifted"
    by_field = {check["field"]: check for check in result["checks"]}
    assert by_field["provenance.input.content_hash"]["match"] is True
    assert by_field["provenance.runset.content_hash"]["match"] is False


def test_a_literal_zero_flag_value_is_refused_not_read_as_unset():
    """`0 == False` in Python, so an "unset" test built on plain equality
    against the default spellings would swallow a literal zero the caller
    *did* give -- the narrow hole in "silence here would be worse than
    refusal" the review measured. Only a real `False`/`None`/empty default
    means "flag not given"."""
    assert extract_runset.refused_flags({"substrate_tie_ohms": 0}) == [
        "--substrate-tie-ohms"
    ]
    assert extract_runset.refused_flags({"resistance_graph_r_cut_ohm": 0.0}) == [
        "--resistance-graph-r-cut"
    ]
    assert extract_runset.refused_flags({"resistance_path_max_nodes": 0}) == [
        "--resistance-path-max-nodes"
    ]


def test_cli_refuses_batch_backend_for_a_request_document_engine(
    tmp_path, capsys, monkeypatch
):
    """The argv-form batch guard runs before the request document is applied,
    so a request carrying `"engine": "klayout-runset"` used to sail past it
    into `_run_batch` -- failing later on a missing `deck` or en route to a
    submit, never with the designed message. The guard is re-run on the
    engine the request itself declares."""
    from klayout_tools.cli import main

    _, deck = _inputs(tmp_path)
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "schema": "klt.extract.request/1",
                "file": "l.gds",
                "engine": "klayout-runset",
                "deck_file": deck,
                "output": str(tmp_path / "o.spice"),
            }
        )
    )
    monkeypatch.setenv("KLT_EXTRACT_BACKEND", "batch")
    rc = main(["extract", str(request), "--format", "json"])
    assert rc == 1
    message = json.loads(capsys.readouterr().err)["error"]["message"]
    assert "--backend batch cannot run engine 'klayout-runset'" in message


def test_refused_flag_table_names_real_run_extract_parameters():
    """`run_extract` reads the refused set out of its own `locals()` against
    this table, so a key that is not a parameter would silently never be
    checked -- and a flag the runset engine cannot honour would be accepted
    and dropped, which is the one outcome issue #5481 rules out. Both
    directions are checked: a parameter absent from the table must be
    classified in ``allowed`` below (honoured, runset-only, or a named
    exception), so a new curated-only parameter cannot slip in unrefused."""
    import inspect

    parameters = set(inspect.signature(run_extract).parameters)
    table = set(extract_runset.REFUSED_FLAG_BY_PARAM)
    unknown = sorted(table - parameters)
    assert unknown == [], unknown

    allowed = {
        # Shared inputs the runset engine honours itself, then the engine's
        # own parameters (`engine` selects it).
        *(
            "path",
            "output",
            "top",
            "no_netlist",
            "engine",
            "deck_file",
            "deck_vars",
            "device_models",
            "klayout_binary",
            "runset_timeout",
        ),
        # `--mom-background-permittivity` tunes the mom parasitics model,
        # and `mom_net` (the switch that turns it on) is itself refused, so
        # the value can never take effect; its non-unset default (3.9)
        # would also defeat the locals()-driven table.
        "mom_background_permittivity",
    }
    unclassified = sorted(parameters - table - allowed)
    assert unclassified == [], unclassified
