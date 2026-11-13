"""``klt pex --attach MANIFEST --dry-run`` is refused through the CLI
(issue #5464).

``tests/test_pex_dry_run.py``'s ``test_dry_run_refuses_the_async_forms``
calls :func:`run_pex_dry_run` directly, so it never goes through
``cli/pex_cmd._rehydrate_bare_attach``. These cases drive ``main()``
instead.

A bare ``--attach MANIFEST`` (no layout, no ``--deck``, no testbenches)
copies the request the ``--submit-only`` run recorded back onto the
namespace. A ``dry_run`` key in that request would overwrite the
``--dry-run`` the caller just typed, sending the invocation down the real
``run_pex`` path -- taking the output-directory lock, collecting the jobs
and writing the report -- which is exactly what the refusal exists to
prevent. These tests pin both halves of the fix: ``--dry-run`` is not
recorded into a manifest, and a manifest that carries it anyway (written
by a different ``klt``, or by hand) cannot clobber the flag.

Engine-free: nothing here extracts or simulates.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools.cli import main, pex_cmd
from klayout_tools.cli.parser import create_parser
from test_pex import (
    _make_sky130_poly_resistor_layout,
    _write_gds,
    _write_request,
    _write_schematic_dut,
    _write_testbench,
)


@pytest.fixture
def bench(tmp_path):
    """A valid ``klt pex`` invocation's inputs -- the same fixture shape
    ``tests/test_pex_dry_run.py`` uses."""
    layout = _write_gds(_make_sky130_poly_resistor_layout(), tmp_path / "res.gds")
    dut = _write_schematic_dut(tmp_path / "dut.spice")
    testbench = _write_testbench(tmp_path / "tb.spice", dut)
    request = _write_request(tmp_path / "req.json", testbench)
    return {
        "root": tmp_path,
        "layout": str(layout),
        "request": str(request),
        "output": str(tmp_path / "pex.json"),
    }


def _submit_manifest(bench, path, *, request_extra=None):
    """A schema-2 pex submit manifest whose ``request`` is exactly what
    ``cli/pex_cmd._submit_request`` records for a real ``--submit-only`` run.

    ``request_extra`` overlays keys on top of it, standing in for a manifest
    written by a different ``klt`` than the one reading it.
    """
    args = create_parser().parse_args(
        [
            "pex",
            bench["layout"],
            bench["request"],
            "--deck",
            "sky130",
            "--output",
            bench["output"],
            "--submit-only",
            "--backend",
            "batch",
        ]
    )
    request = pex_cmd._submit_request(args)
    request.update(request_extra or {})
    Path(path).write_text(
        json.dumps(
            {
                "schema_version": 2,
                "legs": [],
                "testbenches": [],
                "extracted_netlist_sha256": None,
                "request": request,
            }
        ),
        encoding="utf-8",
    )
    return request


def test_submit_request_does_not_record_dry_run(bench, tmp_path):
    """``--dry-run`` is per-invocation plumbing, like ``--format`` and the
    async flags -- a ``--submit-only`` manifest must not carry it."""
    assert "dry_run" not in _submit_manifest(bench, tmp_path / "pex-submit.json")


@pytest.mark.parametrize(
    ("request_extra", "label"),
    [
        (None, "manifest written by this klt (no dry_run key)"),
        ({"dry_run": False}, "manifest written before the fix (dry_run: false)"),
        ({"dry_run": True}, "manifest recording dry_run: true"),
    ],
)
def test_cli_bare_attach_dry_run_is_refused(
    bench, tmp_path, capsys, monkeypatch, request_extra, label
):
    manifest = tmp_path / "pex-submit.json"
    _submit_manifest(bench, manifest, request_extra=request_extra)

    reached = []
    monkeypatch.setattr(
        pex_cmd, "run_pex", lambda *a, **k: reached.append("run_pex") or {}
    )

    exit_code = main(
        ["pex", "--attach", str(manifest), "--dry-run", "--format", "json"]
    )

    assert exit_code == 1, label
    envelope = json.loads(capsys.readouterr().err)
    assert envelope["error"]["command"] == "pex"
    assert "--dry-run cannot be combined with --attach" in envelope["error"]["message"]
    # "Nothing is extracted or simulated, and `klt pex` writes nothing of its own."
    assert reached == [], label
    assert not Path(bench["output"]).exists()


def test_cli_bare_attach_without_dry_run_still_rehydrates(bench, tmp_path, monkeypatch):
    """Skipping ``_REQUEST_EXCLUDED`` keys must not break the bare-attach path
    itself (issue #4692): a plain ``--attach MANIFEST`` still rebuilds the
    layout, deck and testbenches from the recorded request."""
    manifest = tmp_path / "pex-submit.json"
    _submit_manifest(bench, manifest, request_extra={"dry_run": False})

    seen = {}

    def _fake_run_pex(layout, testbenches, deck, **kwargs):
        seen.update(layout=layout, testbenches=list(testbenches), deck=deck)
        return {"schema_version": 2, "status": "submitted"}

    monkeypatch.setattr(pex_cmd, "run_pex", _fake_run_pex)
    monkeypatch.setattr(pex_cmd, "_print_submitted_text", lambda report: None)

    exit_code = main(["pex", "--attach", str(manifest), "--format", "json"])

    assert exit_code == 0
    assert seen["layout"] == str(Path(bench["layout"]).resolve())
    assert seen["deck"] == "sky130"
    assert seen["testbenches"] == [str(Path(bench["request"]).resolve())]
