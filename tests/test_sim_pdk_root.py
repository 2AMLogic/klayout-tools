"""Tests for ``klt sim --pdk-root`` (issue #4758).

Split out of ``test_sim.py`` (file-size ratchet); shares its PDK-store helpers.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools.cli import main
from test_sim import (
    _isolate_pdk_search,
    _make_pdk_store,
    _stub_subprocess_run,
    _write_body,
    _write_request,
)

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)


def _two_store_request(tmp_path, monkeypatch, **models):
    _write_body(tmp_path)
    ciel = _make_pdk_store(tmp_path, "ciel", stamp="open_pdks aaaa111")
    volare = _make_pdk_store(tmp_path, "volare", stamp="open_pdks bbbb222")
    _isolate_pdk_search(monkeypatch, [ciel, volare])
    _stub_subprocess_run(monkeypatch, log_text="clean run\n")
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "models": {
                "pdk": "sky130A",
                "lib": "libs.tech/ngspice/sky130.lib.spice",
                **models,
            },
        },
    )
    return request, ciel, volare


def test_cli_pdk_root_flag_pins_the_second_install(tmp_path, monkeypatch, capsys):
    """Issue #4758: `klt sim --pdk-root` is the CLI route the ambiguity
    warning names; it pins the root and silences the warning."""
    request, _ciel, volare = _two_store_request(tmp_path, monkeypatch)

    main(["sim", str(request), "--pdk-root", str(volare), "--format", "json"])

    captured = capsys.readouterr()
    block = json.loads(captured.out)["provenance"]["pdk"]
    assert block["version"] == "open_pdks bbbb222"
    assert "ambiguous_sources" not in block
    assert "installs provide PDK variant" not in captured.err


def test_cli_pdk_root_flag_wins_over_request_models_pdk_root(
    tmp_path, monkeypatch, capsys
):
    request, ciel, volare = _two_store_request(tmp_path, monkeypatch)
    # Point the request's own pin at ciel; the flag must still pick volare.
    data = json.loads(request.read_text())
    data["models"]["pdk_root"] = str(ciel)
    request.write_text(json.dumps(data))

    main(["sim", str(request), "--pdk-root", str(volare), "--format", "json"])

    block = json.loads(capsys.readouterr().out)["provenance"]["pdk"]
    assert block["version"] == "open_pdks bbbb222"


def test_cli_pdk_root_flag_relative_path_resolves_against_cwd(
    tmp_path, monkeypatch, capsys
):
    request, _ciel, volare = _two_store_request(tmp_path, monkeypatch)
    monkeypatch.chdir(volare.parent)

    main(["sim", str(request), "--pdk-root", volare.name, "--format", "json"])

    block = json.loads(capsys.readouterr().out)["provenance"]["pdk"]
    assert block["version"] == "open_pdks bbbb222"


def test_cli_pdk_root_flag_rejected_with_job_status(tmp_path, monkeypatch, capsys):
    request, _ciel, volare = _two_store_request(tmp_path, monkeypatch)

    code = main(["sim", str(request), "--pdk-root", str(volare), "--job-status", "j-1"])
    assert code == 2
    assert "--pdk-root has no effect with --job-status" in capsys.readouterr().err


def test_sim_ambiguity_text_names_a_cli_route(capsys):
    from klayout_tools.cli import sim_cmd

    report = {
        "provenance": {
            "pdk": {
                "name": "sky130A",
                "version": None,
                "source": "search root: a",
                "ambiguous_sources": ["search root: b"],
            }
        }
    }
    sim_cmd._print_pdk(report)
    assert "--pdk-root" in capsys.readouterr().out
