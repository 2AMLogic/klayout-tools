"""Issue #4984: ``klt lvs -o/--output`` and the ``{path, scope}`` ``reference``."""

import gzip
import json
import os
from pathlib import Path

import jsonschema
import pytest

from klayout_tools.cli import main
from klayout_tools.lvs import check_lvs_report, rerun_lvs_report, run_lvs
from test_lvs import (
    _COMBINE_DEVICES_PARTIAL_MATCH_ERROR,
    _INVERTER_SPICE,
    _MULTIFINGER_LAYOUT_SPICE,
    _write_request,
)

_SPICE = """
.subckt inv A Y VPWR VGND
M1 Y A VGND VGND nfet W=0.65U L=0.15U
M2 Y A VPWR VPWR pfet W=1.0U L=0.15U
.ends
"""


@pytest.fixture
def tmp_path(tmp_path):
    """Seed a `.git` marker so tmp_path is the repo root (`scope: "repo"`),
    as in tests/test_lvs.py."""
    (tmp_path / ".git").mkdir()
    return tmp_path


def _write(path, text):
    path.write_text(text)
    return str(path)


def _pair(tmp_path):
    pair = tmp_path / "pair"
    pair.mkdir()
    _write(pair / "layout.spice", _SPICE)
    _write(pair / "ref.spice", _SPICE)
    request = pair / "request.json"
    request.write_text(
        json.dumps(
            {
                "layout": {"netlist": "layout.spice", "top": "inv"},
                "reference": {"netlist": "ref.spice", "top": "inv"},
            }
        )
    )
    report = pair / "lvs.json"
    report.write_text(json.dumps(run_lvs(str(request))))
    return pair, report


def test_check_lvs_report_reference_resolves_from_any_cwd(tmp_path, monkeypatch):
    """Issue #4984: a v3 report's `reference` is `{path, scope}`, so
    `--check` from an unrelated cwd matches (no `input_not_found`)."""
    _pair(tmp_path)

    monkeypatch.chdir(tmp_path)
    result = check_lvs_report(os.path.join("pair", "lvs.json"))

    assert result["status"] == "match"
    assert not any("input_not_found" in check for check in result["checks"])


def test_lvs_output_flag_writes_envelope_and_rejects_check(tmp_path, capsys):
    """Issue #4984: `-o` mirrors stdout JSON; incompatible with --check;
    unwritable path is an error envelope."""
    layout_path, reference_path = (
        _write(tmp_path / "layout.spice", _SPICE),
        _write(tmp_path / "ref.spice", _SPICE),
    )
    request = json.dumps(
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
        }
    )
    out = tmp_path / "report.json"
    assert main(["lvs", request, "-o", str(out), "--format", "json"]) == 0
    stdout = capsys.readouterr().out
    assert json.loads(out.read_text()) == json.loads(stdout)

    gz = tmp_path / "report.json.gz"
    assert main(["lvs", request, "-o", str(gz), "--format", "json"]) == 0
    capsys.readouterr()
    assert json.loads(gzip.open(gz, "rt").read())["status"] == "match"

    assert main(["lvs", "--check", str(out), "-o", str(gz), "--format", "json"]) == 1
    capsys.readouterr()

    bad = tmp_path / "no_such_dir" / "r.json"
    assert main(["lvs", request, "-o", str(bad), "--format", "json"]) == 1
    capsys.readouterr()


# --------------------------------------------------------------------------- #
# `--omit-mismatches` / `request.omit_mismatches` (issue #5610)
# --------------------------------------------------------------------------- #

_LVS_SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "schemas"
        / "lvs-response.schema.json"
    ).read_text()
)

#: Every summary the tally produces from the full `mismatches[]` list before
#: the `--omit-mismatches` blanking (issue #5610) -- all must be identical
#: with and without the flag on the same inputs.
_SUMMARY_FIELDS = (
    "status",
    "mismatch_count",
    "error_count",
    "category_counts",
    "category_error_counts",
)


def _mismatching_request(tmp_path):
    """A pre-extracted-netlist request whose reference device width differs
    from the layout's -- `status: "mismatch"` with a real entry in
    `mismatches[]`."""
    layout = _write(tmp_path / "layout.spice", _SPICE)
    reference = _write(tmp_path / "ref.spice", _SPICE.replace("W=0.65U", "W=0.70U"))
    return json.dumps(
        {
            "layout": {"netlist": layout, "top": "inv"},
            "reference": {"netlist": reference, "top": "inv"},
        }
    )


def test_omit_mismatches_blanks_list_keeps_summaries(tmp_path):
    """Issue #5610: with the flag, `mismatches` is `[]` (still present,
    still a list) beside `mismatches_omitted: true`, while every summary
    field -- tallied over the full list before the blanking -- is
    byte-identical to the same run without it. Without the flag the report
    carries the full list and the `false` echo."""
    request = _mismatching_request(tmp_path)
    plain = run_lvs(request)
    flagged = run_lvs(request, omit_mismatches=True)

    assert plain["status"] == "mismatch"
    assert plain["mismatches_omitted"] is False
    assert plain["mismatches"], "the unflagged run keeps the full list"

    assert flagged["status"] == "mismatch"
    assert flagged["mismatches"] == []
    assert flagged["mismatches_omitted"] is True
    assert flagged["mismatch_count"] == plain["mismatch_count"]
    for field in _SUMMARY_FIELDS:
        assert flagged[field] == plain[field], field


def test_omit_mismatches_request_field_matches_flag(tmp_path):
    """The request document's top-level boolean `omit_mismatches` produces
    the same report as the flag; when both name it, the flag (a
    `store_true`, so it can only raise the value to `true`) wins."""
    request = json.loads(_mismatching_request(tmp_path))
    flagged = run_lvs(json.dumps(request), omit_mismatches=True)

    from_request = run_lvs(json.dumps({**request, "omit_mismatches": True}))
    assert from_request == flagged

    from_both = run_lvs(
        json.dumps({**request, "omit_mismatches": True}), omit_mismatches=True
    )
    assert from_both == flagged

    assert run_lvs(json.dumps(request))["mismatches_omitted"] is False


def test_omit_mismatches_non_boolean_request_field_raises(tmp_path):
    request = json.loads(_mismatching_request(tmp_path))

    from klayout_tools.lvs import LvsError

    with pytest.raises(LvsError, match="request.omit_mismatches must be a boolean"):
        run_lvs(json.dumps({**request, "omit_mismatches": "yes"}))


def test_omit_mismatches_on_clean_match_is_a_noop_with_true_echo(tmp_path):
    """On an already-empty list the flag changes nothing but the echo:
    `status` stays `match`, `mismatches` stays `[]`, and
    `mismatches_omitted` is still `true`."""
    layout = _write(tmp_path / "layout.spice", _SPICE)
    reference = _write(tmp_path / "ref.spice", _SPICE)
    request = json.dumps(
        {
            "layout": {"netlist": layout, "top": "inv"},
            "reference": {"netlist": reference, "top": "inv"},
        }
    )
    report = run_lvs(request, omit_mismatches=True)
    assert report["status"] == "match"
    assert report["mismatches"] == []
    assert report["mismatches_omitted"] is True


def test_omit_mismatches_report_conforms_to_schema(tmp_path):
    """The flagged report validates against the shipped response schema:
    `mismatches` stays a required array, `mismatches_omitted` is the new
    always-present boolean, and `schema_version` is unchanged."""
    report = run_lvs(_mismatching_request(tmp_path), omit_mismatches=True)
    jsonschema.validate(report, _LVS_SCHEMA, cls=jsonschema.Draft202012Validator)
    assert report["schema_version"] == 3
    assert "mismatches_omitted" in _LVS_SCHEMA["required"]


def test_omit_mismatches_still_exits_3(tmp_path):
    """A `status: "mismatch"` run exits 3 with the flag -- the verdict and
    every exit code are unchanged; only the per-entry echo is dropped."""
    assert main(["lvs", _mismatching_request(tmp_path), "--omit-mismatches"]) == 3


def test_omit_mismatches_check_passes(tmp_path, capsys):
    """Issue #5610's recorded run: cheap `--check` on a report produced with
    the flag still holds (`status: "match"`, exit 0). It reads only the
    `environment` hashes and the deck `content_hash`, never
    `mismatches[]`, so a blanked list checks like any other."""
    report_path = tmp_path / "lvs.json"
    report_path.write_text(
        json.dumps(run_lvs(_mismatching_request(tmp_path), omit_mismatches=True))
    )

    assert main(["lvs", "--check", str(report_path), "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "match"


def test_omit_mismatches_rerun_replays_no_drift(tmp_path):
    """The load-bearing test (issue #5610): `--check --rerun` on a flagged
    report replays the omission from the committed `mismatches_omitted`
    echo, so the fresh run is compacted the same way and reports no drift.
    Without the replay in `_reconstruct_lvs_request` this fails on
    `mismatches` -- the compacted committed list diffing against a fresh
    uncompacted one, the exact false-drift shape issue #1205 already
    produced for `combine_devices`."""
    report_path = tmp_path / "lvs.json"
    report_path.write_text(
        json.dumps(run_lvs(_mismatching_request(tmp_path), omit_mismatches=True))
    )

    result = rerun_lvs_report(str(report_path))

    assert result["status"] == "match"
    assert result["drift"] == []
    assert result["fresh"]["mismatches_omitted"] is True
    assert result["fresh"]["mismatches"] == []


def test_omit_mismatches_legacy_report_without_echo_reruns_clean(tmp_path):
    """A committed report predating the field (no `mismatches_omitted` key,
    uncompacted -- which is what every pre-#5610 report looks like) reruns
    with no drift on the field: the replay leaves it uncompacted, and the
    fresh run's new `false` echo is excluded from the diff."""
    report = run_lvs(_mismatching_request(tmp_path))
    del report["mismatches_omitted"]
    report_path = tmp_path / "lvs.json"
    report_path.write_text(json.dumps(report))

    result = rerun_lvs_report(str(report_path))

    assert result["status"] == "match"
    assert result["drift"] == []


def test_omit_mismatches_text_disclosure(tmp_path, capsys):
    """Text mode discloses the omission on its own line with the flag and
    prints no such line without it -- a compacted report's per-entry
    section is empty, so silence would read as a clean report's tail."""
    request = _mismatching_request(tmp_path)

    assert main(["lvs", request, "--omit-mismatches"]) == 3
    flagged = capsys.readouterr().out
    assert "mismatches_omitted: True (--omit-mismatches)" in flagged
    assert "[error] device.property" not in flagged

    assert main(["lvs", request]) == 3
    plain = capsys.readouterr().out
    assert "mismatches_omitted" not in plain
    assert "[error] device.property" in plain


def test_omit_mismatches_rejected_with_check(tmp_path, capsys):
    """`--omit-mismatches` applies to a fresh run; `--check` reads the
    committed report as-is (a flagged one already carries the omission, and
    `--rerun` replays it from the echo) -- the combination is a clean
    error, exit 1."""
    report_path = tmp_path / "lvs.json"
    report_path.write_text(json.dumps(run_lvs(_mismatching_request(tmp_path))))

    assert (
        main(
            [
                "lvs",
                "--check",
                str(report_path),
                "--omit-mismatches",
                "--format",
                "json",
            ]
        )
        == 1
    )
    error = json.loads(capsys.readouterr().err)
    assert "--omit-mismatches applies to a fresh run" in error["error"]["message"]


def test_reconstruct_replays_omit_mismatches():
    """`true` is replayed by `--check --rerun` from the committed echo; the
    default is not injected. The replay is a top-level request field (the
    response echo is outside `options`, which only carries compare-shaping
    inputs)."""
    committed = {
        "mismatches_omitted": True,
        "layout": "layout.spice",
        "reference": "ref.spice",
        "top": "inv",
    }
    from klayout_tools import lvs

    assert lvs._reconstruct_lvs_request(committed)["omit_mismatches"] is True

    committed["mismatches_omitted"] = False
    assert "omit_mismatches" not in lvs._reconstruct_lvs_request(committed)


def test_lvs_cli_exits_4_on_inconclusive(tmp_path, monkeypatch, capsys):
    """Issue #1370: `status: "inconclusive"` gets its own exit code, `4` --
    the same value `klt equiv` already uses for its own unreachable-verdict
    outcome (`cli/equiv_cmd.py`'s `EXIT_INCONCLUSIVE`). Folding it into `3`
    would leave an automation gate unable to tell "the design differs" from
    "the comparison could not be performed"."""
    import klayout.db as kdb

    from klayout_tools.cli import equiv_cmd, lvs_cmd

    # The two verbs agree on the numeric value, by construction.
    assert lvs_cmd.EXIT_INCONCLUSIVE == equiv_cmd.EXIT_INCONCLUSIVE == 4

    def _raise(self):
        raise RuntimeError(_COMBINE_DEVICES_PARTIAL_MATCH_ERROR)

    monkeypatch.setattr(kdb.Netlist, "combine_devices", _raise)

    layout_path = _write(tmp_path / "layout.spice", _MULTIFINGER_LAYOUT_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
            "options": {"combine_devices": True},
        },
    )

    import argparse

    args = argparse.Namespace(
        request=path,
        format="json",
        check=None,
        rerun=False,
        deck=None,
        deck_file=None,
        deck_var=None,
        layout=None,
        progress=None,
        omit_mismatches=False,
        output=None,
    )
    assert lvs_cmd.run(args) == lvs_cmd.EXIT_INCONCLUSIVE
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "inconclusive"
