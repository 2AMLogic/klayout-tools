"""Issue #6840: ``klt lvs --check`` on a ``klayout-runset`` report whose
recorded ``deck_file`` is the verbatim echo of a *relative* ``--deck-file``
must not tell the reader to ``pass --layout``: no flag overrides the runset
on ``--check`` (``--deck-file`` is refused there), so the ``input_not_found``
remedy names no flag at all. Also pins, end to end, the default remedy the
layout entry keeps -- nothing has asserted it since #6802/#6830 retargeted
``tests/test_lvs.py``'s reference-side assertion.

Fixture preamble shared with ``tests/test_lvs_klayout_runset.py``
(``helpers/lvs_runset_fixtures.py``); the new tests live here, not there,
because that file sits at its 500-line size-ratchet ceiling.
"""

from __future__ import annotations

import json
import os

import pytest

import lvs_tmp_repo
from helpers import lvs_runset_fixtures as fx
from klayout_tools.lvs import check_lvs_report, run_lvs

tmp_path = lvs_tmp_repo.tmp_path  # pytest collects the fixture by module-level name


@pytest.fixture
def fake_klayout(monkeypatch, tmp_path):
    return fx.install_fake_klayout(monkeypatch, tmp_path)


def _report_with_relative_deck_echo(tmp_path, monkeypatch, fake_klayout):
    """A runset report committed in ``pair`` whose ``deck_file`` echo is the
    relative ``pdk.lvs`` (a relative ``--deck-file`` is echoed verbatim),
    the deck beside it; the layout echo rewritten to a legacy bare string
    so both remedy shapes are observable from one report."""
    pair = tmp_path / "pair"
    pair.mkdir()
    fx.make_gds(str(pair / "l.gds"), 1)
    (pair / "ref.sp").write_text(fx.SPICE_ONE)
    (pair / "pdk.lvs").write_text("# runset\n")
    monkeypatch.chdir(pair)
    request = json.dumps(
        {
            "engine": "klayout-runset",
            "layout": {"file": "l.gds"},
            "reference": {"netlist": str(pair / "ref.sp")},
        }
    )
    report = run_lvs(request, deck_file="pdk.lvs")
    assert report["deck_file"] == "pdk.lvs"
    report["layout"] = "l.gds"  # legacy bare-string layout (default remedy's shape)
    report_path = pair / "report.json"
    report_path.write_text(json.dumps(report))
    return report_path


def test_runset_deck_remedy_names_no_flag(tmp_path, monkeypatch, fake_klayout):
    report_path = _report_with_relative_deck_echo(tmp_path, monkeypatch, fake_klayout)
    report_dir = os.path.dirname(os.path.abspath(str(report_path)))
    monkeypatch.chdir(tmp_path)  # --check from another cwd: the echo misses

    by_field = {c["field"]: c for c in check_lvs_report(str(report_path))["checks"]}

    runset = by_field["provenance.runset.content_hash"]["input_not_found"]
    assert runset["path"] == "pdk.lvs"
    assert runset["hint"] == (
        "run --check from the report's directory; "
        f"request-file-relative paths resolve against {report_dir}"
    )

    layout = by_field["environment.layout_sha256"]["input_not_found"]
    assert layout["hint"] == (
        "run --check from the report's directory, or pass --layout/an absolute path; "
        f"request-file-relative paths resolve against {report_dir}"
    )


def test_runset_deck_absent_everywhere_keeps_null_hint(
    tmp_path, monkeypatch, fake_klayout
):
    report_path = _report_with_relative_deck_echo(tmp_path, monkeypatch, fake_klayout)
    os.rename(report_path.parent / "pdk.lvs", report_path.parent / "moved.lvs")
    monkeypatch.chdir(tmp_path)

    by_field = {c["field"]: c for c in check_lvs_report(str(report_path))["checks"]}

    runset = by_field["provenance.runset.content_hash"]["input_not_found"]
    assert runset["found_relative_to_report"] is None
    assert runset["hint"] is None
