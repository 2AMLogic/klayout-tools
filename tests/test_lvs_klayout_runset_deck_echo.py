"""Issue #5795 runset-report echoes: the caller-given ``--deck-file`` form in
``deck_file``/``provenance.runset.deck_file``, and the basename
``klayout_binary``. Split out of ``test_lvs_klayout_runset.py`` to keep that
file under its per-file size ratchet ceiling; shares its fixtures."""

import json
import os

import pytest

from klayout_tools import lvs_runset
from klayout_tools.env_provenance import lint_envelope_files
from klayout_tools.lvs import check_lvs_report, rerun_lvs_report, run_lvs
from test_lvs_klayout_runset import (  # noqa: F401  (fixture used via pytestmark)
    _request,
    fake_klayout,
)

pytestmark = pytest.mark.usefixtures("fake_klayout")


def _cwd_relative_request(tmp_path, n_dev, *, layout, reference):
    """A request whose layout/reference echo relative names; the inline-JSON
    form resolves them against the current directory."""
    request = json.loads(_request(tmp_path, n_dev))
    request["layout"]["file"] = layout
    request["reference"]["netlist"] = reference
    request.pop("deck_file")
    return request


def _relative_deck_report(tmp_path, monkeypatch, deck_file):
    """Run from ``tmp_path`` with the given relative ``--deck-file`` form."""
    monkeypatch.chdir(tmp_path)
    request = _cwd_relative_request(tmp_path, 1, layout="l.gds", reference="ref.sp")
    return run_lvs(json.dumps(request), deck_file=deck_file)


@pytest.mark.parametrize("deck_file", ["pdk.lvs", "./pdk.lvs", "link.lvs"])
def test_relative_deck_file_flag_echoes_caller_form(tmp_path, monkeypatch, deck_file):
    if deck_file == "link.lvs":
        os.symlink(str(tmp_path / "pdk.lvs"), str(tmp_path / "link.lvs"))
    report = _relative_deck_report(tmp_path, monkeypatch, deck_file)
    assert report["status"] == "match"
    assert report["deck_file"] == deck_file
    assert report["provenance"]["runset"]["deck_file"] == deck_file
    # the runset still resolved and hashed the flag against the CWD
    assert report["provenance"]["runset"]["content_hash"] == lvs_runset._sha_prefixed(
        str(tmp_path / "pdk.lvs")
    )


def test_deck_file_flag_edge_forms_echo_verbatim(tmp_path, monkeypatch, request):
    fake = request.getfixturevalue("fake_klayout")
    monkeypatch.chdir(tmp_path)
    request = _cwd_relative_request(tmp_path, 1, layout="./l.gds", reference="./ref.sp")
    dotted = run_lvs(
        json.dumps(request), deck_file="./pdk.lvs", deck_vars={"feol": "true"}
    )
    assert dotted["deck_file"] == "./pdk.lvs"
    assert dotted["provenance"]["runset"]["deck_file"] == "./pdk.lvs"
    assert dotted["layout"] == "./l.gds" and dotted["reference"] == "./ref.sp"
    assert dotted["deck_vars"] == {"feol": "true"}
    assert "feol=true" in fake["cmd"]

    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path / "sub")
    parent = _cwd_relative_request(
        tmp_path, 1, layout="../l.gds", reference="../ref.sp"
    )
    parent_relative = run_lvs(json.dumps(parent), deck_file="../pdk.lvs")
    assert parent_relative["deck_file"] == "../pdk.lvs"
    assert parent_relative["provenance"]["runset"]["deck_file"] == "../pdk.lvs"


def test_default_mode_binary_basename_and_absolute_deck_echo(tmp_path):
    deck = str(tmp_path / "pdk.lvs")
    document = run_lvs(_request(tmp_path, 1))
    assert document["deck_file"] == deck
    assert document["provenance"]["runset"]["deck_file"] == deck
    assert document["environment"]["klayout_binary"] == "klayout"
    assert document["environment"]["engine_version"] == "0.30.12"
    assert document["provenance"]["runset"]["klayout_application_version"] == "0.30.12"

    portable = run_lvs(_request(tmp_path, 1, portable_paths=True))
    assert portable["environment"]["klayout_binary"] == "klayout"

    request = json.loads(_request(tmp_path, 1))
    deck_flag = request.pop("deck_file")
    flag = run_lvs(json.dumps(request), deck_file=deck_flag)
    assert flag["deck_file"] == deck
    assert flag["provenance"]["runset"]["deck_file"] == deck


def test_relative_deck_report_lints_clean_and_reruns_same_cwd(tmp_path, monkeypatch):
    report = _relative_deck_report(tmp_path, monkeypatch, "pdk.lvs")
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    lint = lint_envelope_files([str(path)])
    assert lint["status"] == "clean", lint["files"]
    assert lint["finding_count"] == 0
    checked = check_lvs_report(str(path))
    assert checked["status"] == "match", [
        c for c in checked["checks"] if not c["match"]
    ]
    assert rerun_lvs_report(str(path))["status"] == "match"
