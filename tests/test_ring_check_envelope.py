"""Envelope tests for `klt ring-check`: anchored `file` + `provenance` (issue #5650).

Split out of `tests/test_ring_check.py` (a pure move) so that file stays under
its per-file size ceiling; fixtures are shared from it.
"""

from __future__ import annotations

import json

from klayout_tools.cli import main
from klayout_tools.ring_check import run_ring_check
from test_ring_check import _COMP, _write_broken_ring, _write_closed_ring

# --- Envelope: anchored file + provenance (issue #5650) ----------------------


def test_envelope_has_provenance_and_anchored_file(tmp_path):
    path = tmp_path / "closed.gds"
    _write_closed_ring(path)

    report = run_ring_check(str(path), [_COMP])

    assert report["schema_version"] == 2
    assert report["file"]["scope"] in {"repo", "external"}
    assert str(tmp_path) not in json.dumps(report)
    prov = report["provenance"]
    assert prov["pdk"] is None
    assert prov["deck"] is None
    assert prov["input"]["role"] == "layout"
    assert prov["input"]["content_hash"].startswith("sha256:")


def test_external_file_is_not_leaked_and_broken_carries_envelope(tmp_path):
    path = tmp_path / "broken.gds"
    _write_broken_ring(path)

    report = run_ring_check(str(path), [_COMP])

    assert report["status"] == "broken"
    assert report["file"] == {"path": None, "scope": "external"}
    assert "provenance" in report


def test_cli_text_prints_path_not_dict(tmp_path, capsys):
    path = tmp_path / "closed.gds"
    _write_closed_ring(path)

    main(["ring-check", str(path), "--layers", "[[22,0],[34,0]]"])

    out = capsys.readouterr().out
    assert "file: " in out
    assert "{" not in out.splitlines()[0]
